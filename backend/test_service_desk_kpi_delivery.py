"""Tests for Service Desk KPI email delivery domain service."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from google.api_core.exceptions import AlreadyExists

from app.services import service_desk_kpi_delivery as delivery
from app.services import service_desk_kpi_snapshot as snapshot
from app.services.email_service import EmailAttachment


class _FakeSnapshot:
    def __init__(self, exists: bool, data: dict | None = None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _FakeRunDoc:
    def __init__(self, store: dict[str, dict], run_id: str):
        self.store = store
        self.run_id = run_id

    def create(self, payload: dict):
        if self.run_id in self.store:
            raise AlreadyExists("already exists")
        self.store[self.run_id] = dict(payload)

    def get(self):
        return _FakeSnapshot(self.run_id in self.store, self.store.get(self.run_id))

    def set(self, payload: dict, merge: bool = False):
        if merge:
            current = self.store.get(self.run_id, {})
            current.update(payload)
            self.store[self.run_id] = current
        else:
            self.store[self.run_id] = dict(payload)


class _FakeRunCollection:
    def __init__(self):
        self.store: dict[str, dict] = {}

    def document(self, run_id: str):
        return _FakeRunDoc(self.store, run_id)


class _FakeDb:
    def __init__(self):
        self.runs = _FakeRunCollection()

    def collection(self, name: str):
        self.collection_name = name
        return self.runs


def _snapshot_result(status: str = "wrote") -> snapshot.SnapshotResult:
    return snapshot.SnapshotResult(
        status=status,
        report_date="2026-06-05",
        bucket="esip-reference-data",
        blob_name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
        gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
        filename="service-desk-kpi-2026-06-05.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=123,
    )


class ServiceDeskKpiDeliveryConfigTest(unittest.TestCase):
    def test_config_validates_email_timezone_and_slot_time(self):
        config = delivery.normalize_config(
            {
                "enabled": True,
                "timezone": "America/New_York",
                "recipients": [" priya.shah@northstar.example.com "],
                "cc_recipients": [" Sofia.Reyes@northstar.example.com "],
                "slots": [{"time": "10:30", "enabled": True, "label": "Morning"}],
            }
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.timezone, "America/New_York")
        self.assertEqual(config.recipients, ("priya.shah@northstar.example.com",))
        self.assertEqual(config.cc_recipients, ("sofia.reyes@northstar.example.com",))
        self.assertEqual(delivery.config_to_dict(config)["cc_recipients"], ["sofia.reyes@northstar.example.com"])
        self.assertEqual(config.slots[0].time, "10:30")
        self.assertTrue(config.slots[0].id.endswith("1030"))
        self.assertEqual(delivery.normalize_config({"slots": []}).slots, ())
        with self.assertRaises(delivery.DeliveryValidationError):
            delivery.normalize_config({"timezone": "Not/AZone"})
        with self.assertRaises(delivery.DeliveryValidationError):
            delivery.normalize_config({"recipients": ["not-email"]})

    def test_slot_times_must_be_15_minute_increments(self):
        self.assertEqual(delivery.normalize_slot_time("09:45"), "09:45")
        with self.assertRaises(delivery.DeliveryValidationError):
            delivery.normalize_slot_time("09:10")
        with self.assertRaises(delivery.DeliveryValidationError):
            delivery.normalize_slot_time("9:10")

    def test_due_slots_are_deterministic_for_business_time_datetime(self):
        config = delivery.normalize_config(
            {
                "enabled": True,
                "timezone": "America/New_York",
                "slots": [
                    {"id": "morning", "time": "10:30", "enabled": True},
                    {"id": "disabled", "time": "10:30", "enabled": False},
                    {"id": "afternoon", "time": "14:45", "enabled": True},
                ],
            }
        )
        now = datetime(2026, 6, 24, 10, 37, tzinfo=ZoneInfo("America/New_York"))

        due = delivery.due_slots(config, now)

        self.assertEqual([slot.id for slot in due], ["morning"])
        self.assertEqual(delivery.current_dispatch_time(now, "America/New_York"), "10:30")


class ServiceDeskKpiDeliveryRunTest(unittest.TestCase):
    def test_claim_delivery_run_is_atomic_and_skips_duplicate(self):
        db = _FakeDb()
        slot = delivery.DeliverySlot(id="morning", time="10:30")

        first = delivery.claim_delivery_run(report_date="2026-06-05", slot=slot, db=db)
        second = delivery.claim_delivery_run(report_date="2026-06-05", slot=slot, db=db)

        self.assertTrue(first.claimed)
        self.assertFalse(second.claimed)
        self.assertEqual(second.status, "claimed")
        self.assertEqual(len(db.runs.store), 1)

    def test_disabled_or_empty_recipients_skip_production_delivery(self):
        db = _FakeDb()
        slot = delivery.DeliverySlot(id="morning", time="10:30")
        disabled = delivery.normalize_config(
            {"enabled": False, "recipients": ["jordan.lee@northstar.example.com"], "slots": [{"id": "morning", "time": "10:30"}]}
        )
        empty = delivery.normalize_config({"enabled": True, "recipients": [], "slots": [{"id": "morning", "time": "10:30"}]})

        with mock.patch.object(delivery.email_service, "send_email") as send_email:
            disabled_outcome = delivery.deliver_slot(disabled, slot, report_date="2026-06-05", db=db)
            empty_outcome = delivery.deliver_slot(empty, slot, report_date="2026-06-05", db=db)

        self.assertEqual(disabled_outcome.status, "skipped")
        self.assertEqual(disabled_outcome.message, "disabled")
        self.assertEqual(empty_outcome.status, "skipped")
        self.assertEqual(empty_outcome.message, "no recipients")
        send_email.assert_not_called()
        self.assertEqual(db.runs.store, {})

    def test_delivery_attaches_expected_excel_filename(self):
        db = _FakeDb()
        config = delivery.normalize_config(
            {
                "enabled": True,
                "recipients": ["jordan.lee@northstar.example.com"],
                "cc_recipients": ["sofia.reyes@northstar.example.com"],
                "slots": [{"id": "morning", "time": "10:30"}],
            }
        )
        slot = config.slots[0]
        download = snapshot.SnapshotDownload(
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            content=b"xlsx-bytes",
        )

        with (
            mock.patch.object(delivery.service_desk_kpi_snapshot, "write_daily_snapshot", return_value=_snapshot_result("wrote")),
            mock.patch.object(delivery.service_desk_kpi_snapshot, "download_snapshot", return_value=download),
            mock.patch.object(delivery.email_service, "send_email", return_value=True) as send_email,
        ):
            outcome = delivery.deliver_slot(config, slot, report_date="2026-06-05", db=db)

        self.assertEqual(outcome.status, "sent")
        self.assertEqual(outcome.recipient_count, 1)
        self.assertEqual(outcome.cc_recipient_count, 1)
        self.assertEqual(send_email.call_args.kwargs["recipients"], ("jordan.lee@northstar.example.com",))
        self.assertEqual(send_email.call_args.kwargs["cc_recipients"], ("sofia.reyes@northstar.example.com",))
        self.assertEqual(send_email.call_args.kwargs["subject"], "Daily KPI Report June 5, 2026")
        self.assertEqual(
            send_email.call_args.kwargs["body"],
            "Hi Team,\n\nPlease see the KPI report attached.",
        )
        attachment = send_email.call_args.kwargs["attachments"][0]
        self.assertIsInstance(attachment, EmailAttachment)
        self.assertEqual(attachment.filename, "service-desk-kpi-2026-06-05.xlsx")
        self.assertEqual(attachment.content, b"xlsx-bytes")
        self.assertEqual(db.runs.store[outcome.run_id]["status"], "sent")
        self.assertEqual(db.runs.store[outcome.run_id]["cc_recipient_count"], 1)

    def test_skip_snapshot_still_downloads_existing_bytes(self):
        db = _FakeDb()
        config = delivery.normalize_config(
            {
                "enabled": True,
                "recipients": ["jordan.lee@northstar.example.com"],
                "slots": [{"id": "morning", "time": "10:30"}],
            }
        )
        download = snapshot.SnapshotDownload(
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            content=b"existing-xlsx",
        )

        with (
            mock.patch.object(delivery.service_desk_kpi_snapshot, "write_daily_snapshot", return_value=_snapshot_result("skip")) as write_daily_snapshot,
            mock.patch.object(delivery.service_desk_kpi_snapshot, "download_snapshot", return_value=download) as download_snapshot,
            mock.patch.object(delivery.email_service, "send_email", return_value=True) as send_email,
        ):
            outcome = delivery.deliver_slot(config, config.slots[0], report_date="2026-06-05", db=db)

        self.assertEqual(outcome.status, "sent")
        self.assertEqual(outcome.snapshot_status, "skip")
        write_daily_snapshot.assert_called_once_with(report_date="2026-06-05")
        download_snapshot.assert_called_once_with("2026-06-05")
        self.assertEqual(send_email.call_args.kwargs["attachments"][0].content, b"existing-xlsx")

    def test_safe_test_send_rejects_unsafe_recipient(self):
        with mock.patch.object(delivery.email_service, "send_email") as send_email:
            with self.assertRaises(delivery.DeliveryValidationError):
                delivery.send_test_delivery(recipient="noah.bennett@northstar.example.com", report_date="2026-06-05")

        send_email.assert_not_called()

    def test_safe_test_send_allows_platform_admin_path_with_attachment(self):
        download = snapshot.SnapshotDownload(
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            content=b"test-xlsx",
        )
        with (
            mock.patch.object(delivery.service_desk_kpi_snapshot, "write_daily_snapshot", return_value=_snapshot_result("skip")),
            mock.patch.object(delivery.service_desk_kpi_snapshot, "download_snapshot", return_value=download),
            mock.patch.object(delivery.email_service, "send_email", return_value=True) as send_email,
        ):
            outcome = delivery.send_test_delivery(recipient="Morgan.Chen@northstar.example.com", report_date="2026-06-05")

        self.assertEqual(outcome.status, "sent")
        self.assertEqual(send_email.call_args.kwargs["recipients"], ["morgan.chen@northstar.example.com"])
        self.assertEqual(send_email.call_args.kwargs["subject"], "Daily KPI Report June 5, 2026")
        self.assertEqual(
            send_email.call_args.kwargs["body"],
            "Hi Team,\n\nPlease see the KPI report attached.",
        )
        self.assertEqual(send_email.call_args.kwargs["attachments"][0].content, b"test-xlsx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
