"""Tests for the cloud-side Service Desk KPI snapshot job."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from app.jobs import service_desk_kpi_snapshot as snapshot_job
from app.services import service_desk_kpi_delivery as delivery_service
from app.services import service_desk_kpi_snapshot as snapshot_service


class _FakeBlob:
    def __init__(
        self,
        *,
        exists: bool,
        name: str = "reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
        size: int = 0,
        updated: datetime | None = None,
        content_type: str | None = None,
        download_body: bytes = b"",
    ):
        self._exists = exists
        self.name = name
        self.size = size
        self.updated = updated
        self.content_type = content_type
        self.cache_control = None
        self.content_disposition = None
        self.metadata = None
        self.upload_body = None
        self.upload_content_type = None
        self.exists_client = None
        self.download_body = download_body
        self.reload_client = None

    def exists(self, client=None):
        self.exists_client = client
        return self._exists

    def reload(self, client=None):
        self.reload_client = client

    def upload_from_string(self, body, content_type=None):
        self.upload_body = body
        self.upload_content_type = content_type

    def download_as_bytes(self):
        return self.download_body


class _FakeBucket:
    def __init__(self, blob: _FakeBlob):
        self.blob_obj = blob
        self.requested_blob_names: list[str] = []

    def blob(self, name: str):
        self.requested_blob_names.append(name)
        return self.blob_obj


class _FakeClient:
    def __init__(self, bucket: _FakeBucket, *, list_blobs_result=None):
        self.bucket_obj = bucket
        self.requested_bucket_names: list[str] = []
        self.list_blobs_calls: list[tuple[str, str | None]] = []
        self.list_blobs_result = list_blobs_result or []

    def bucket(self, name: str):
        self.requested_bucket_names.append(name)
        return self.bucket_obj

    def list_blobs(self, bucket_name: str, prefix: str | None = None):
        self.list_blobs_calls.append((bucket_name, prefix))
        return list(self.list_blobs_result)


class ServiceDeskKpiSnapshotServiceTest(unittest.TestCase):
    def test_list_snapshots_returns_recent_artifacts_desc(self):
        blobs = [
            _FakeBlob(
                exists=True,
                name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-04.xlsx",
                size=2002,
                updated=datetime(2026, 6, 5, 12, 34, 56, tzinfo=timezone.utc),
            ),
            _FakeBlob(
                exists=True,
                name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
                size=2345,
                updated=datetime(2026, 6, 6, 12, 34, 56, tzinfo=timezone.utc),
            ),
            _FakeBlob(
                exists=True,
                name="reference_kpi_reports/ignore-me.txt",
                size=99,
            ),
        ]
        client = _FakeClient(_FakeBucket(blobs[0]), list_blobs_result=blobs)

        with mock.patch.object(snapshot_service, "_get_storage_client", return_value=client):
            results = snapshot_service.list_snapshots(limit=10)

        self.assertEqual([item.report_date for item in results], ["2026-06-05", "2026-06-04"])
        self.assertEqual(results[0].size_bytes, 2345)
        self.assertEqual(
            results[0].gcs_uri,
            "gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
        )
        self.assertEqual(
            client.list_blobs_calls,
            [("esip-reference-data", "reference_kpi_reports/")],
        )

    def test_download_snapshot_reads_existing_blob_bytes(self):
        blob = _FakeBlob(
            exists=True,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            download_body=b"xlsx",
        )
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)

        with mock.patch.object(snapshot_service, "_get_storage_client", return_value=client):
            result = snapshot_service.download_snapshot("2026-06-05")

        self.assertEqual(result.filename, "service-desk-kpi-2026-06-05.xlsx")
        self.assertEqual(result.content, b"xlsx")
        self.assertEqual(result.media_type, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIs(blob.exists_client, client)

    def test_write_daily_snapshot_skips_existing_fresh_blob(self):
        blob = _FakeBlob(exists=True)
        blob.metadata = {
            "report_date": "2026-06-05",
            "cxone_cache_fresh": "true",
        }
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)

        with (
            mock.patch.object(snapshot_service, "_get_storage_client", return_value=client),
            mock.patch.object(snapshot_service.service_desk_kpi_export, "get_daily_excel_export") as get_daily_excel_export,
        ):
            result = snapshot_service.write_daily_snapshot(report_date="2026-06-05")

        self.assertEqual(result.status, "skip")
        self.assertEqual(result.gcs_uri, "gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx")
        self.assertEqual(client.requested_bucket_names, ["esip-reference-data"])
        self.assertEqual(
            bucket.requested_blob_names,
            ["reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx"],
        )
        get_daily_excel_export.assert_not_called()
        self.assertIs(blob.exists_client, client)
        self.assertIs(blob.reload_client, client)
        self.assertIsNone(blob.upload_body)

    def test_write_daily_snapshot_uploads_canonical_export(self):
        blob = _FakeBlob(exists=False)
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)
        export = mock.Mock(
            content=b"xlsx-bytes",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            metadata={
                "generated_at": "2026-06-07T11:30:00-04:00",
                "telephony_status": {
                    "status": "loaded",
                    "is_fresh": True,
                    "started_at": "2026-06-07T15:29:00+00:00",
                    "completed_at": "2026-06-07T15:30:00+00:00",
                },
                "cxone_cache_fresh": True,
                "cxone_run_started_at": "2026-06-07T15:29:00+00:00",
                "cxone_run_completed_at": "2026-06-07T15:30:00+00:00",
            },
        )

        with (
            mock.patch.object(snapshot_service, "_get_storage_client", return_value=client),
            mock.patch.object(snapshot_service, "_source_revision", return_value="rev-1"),
            mock.patch.object(
                snapshot_service.service_desk_kpi_export,
                "get_daily_excel_export",
                return_value=export,
            ) as get_daily_excel_export,
        ):
            result = snapshot_service.write_daily_snapshot(report_date="2026-06-06")

        self.assertEqual(result.status, "wrote")
        self.assertEqual(result.size_bytes, len(b"xlsx-bytes"))
        self.assertEqual(blob.upload_body, b"xlsx-bytes")
        self.assertEqual(blob.upload_content_type, export.media_type)
        self.assertEqual(blob.cache_control, "no-store")
        self.assertEqual(
            blob.content_disposition,
            'attachment; filename="service-desk-kpi-2026-06-06.xlsx"',
        )
        self.assertEqual(
            blob.metadata,
            {
                "report_date": "2026-06-06",
                "generated_at": "2026-06-07T11:30:00-04:00",
                "generated_by": "esip-service-desk-kpi-snapshot",
                "media_type": export.media_type,
                "telephony_status": "loaded",
                "telephony_status_json": (
                    '{"completed_at": "2026-06-07T15:30:00+00:00", "is_fresh": true, '
                    '"started_at": "2026-06-07T15:29:00+00:00", "status": "loaded"}'
                ),
                "cxone_run_started_at": "2026-06-07T15:29:00+00:00",
                "cxone_run_completed_at": "2026-06-07T15:30:00+00:00",
                "cxone_cache_fresh": "true",
                "source_revision": "rev-1",
            },
        )
        get_daily_excel_export.assert_called_once_with("2026-06-06")

    def test_write_daily_snapshot_refreshes_existing_stale_blob(self):
        blob = _FakeBlob(exists=True)
        blob.metadata = {
            "report_date": "2026-06-06",
            "cxone_cache_fresh": "false",
        }
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)
        export = mock.Mock(
            content=b"fresh-xlsx",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            metadata={
                "generated_at": "2026-06-07T11:30:00-04:00",
                "telephony_status": {"status": "loaded", "is_fresh": True},
                "cxone_cache_fresh": True,
            },
        )

        with (
            mock.patch.object(snapshot_service, "_get_storage_client", return_value=client),
            mock.patch.object(snapshot_service.service_desk_kpi_export, "get_daily_excel_export", return_value=export),
        ):
            result = snapshot_service.write_daily_snapshot(report_date="2026-06-06")

        self.assertEqual(result.status, "refreshed")
        self.assertEqual(blob.upload_body, b"fresh-xlsx")
        self.assertEqual(blob.metadata["cxone_cache_fresh"], "true")

    def test_write_daily_snapshot_refuses_stale_telephony_in_production_prefix(self):
        blob = _FakeBlob(exists=False)
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)
        export = mock.Mock(
            content=b"stale-xlsx",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            metadata={
                "generated_at": "2026-06-07T05:47:00-04:00",
                "telephony_status": {"status": "loaded", "is_fresh": False},
                "cxone_cache_fresh": False,
            },
        )

        with (
            mock.patch.object(snapshot_service, "_get_storage_client", return_value=client),
            mock.patch.object(snapshot_service.service_desk_kpi_export, "get_daily_excel_export", return_value=export),
        ):
            with self.assertRaises(snapshot_service.SnapshotStaleTelephonyError):
                snapshot_service.write_daily_snapshot(report_date="2026-06-06")

        self.assertIsNone(blob.upload_body)

    def test_write_daily_snapshot_allows_stale_output_only_in_test_prefix(self):
        blob = _FakeBlob(exists=False)
        bucket = _FakeBucket(blob)
        client = _FakeClient(bucket)
        export = mock.Mock(
            content=b"stale-test-xlsx",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            metadata={
                "generated_at": "2026-06-07T05:47:00-04:00",
                "telephony_status": {"status": "loaded", "is_fresh": False},
                "cxone_cache_fresh": False,
            },
        )

        with (
            mock.patch.object(snapshot_service, "_get_storage_client", return_value=client),
            mock.patch.object(snapshot_service.service_desk_kpi_export, "get_daily_excel_export", return_value=export),
        ):
            result = snapshot_service.write_daily_snapshot(
                report_date="2026-06-06",
                prefix="reference_kpi_reports_test",
                allow_stale_test_output=True,
            )

        self.assertEqual(result.status, "wrote")
        self.assertEqual(blob.upload_body, b"stale-test-xlsx")
        self.assertEqual(blob.metadata["cxone_cache_fresh"], "false")
        self.assertEqual(blob.metadata["stale_test_output"], "true")


class ServiceDeskKpiSnapshotJobTest(unittest.TestCase):
    def test_job_main_prints_wrote_marker_and_json(self):
        result = snapshot_service.SnapshotResult(
            status="wrote",
            report_date="2026-06-05",
            bucket="esip-reference-data",
            blob_name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=123,
        )

        with (
            mock.patch.object(snapshot_job.snapshot, "write_daily_snapshot", return_value=result) as write_daily_snapshot,
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "run", "--date", "2026-06-05"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("WROTE gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx", out)
        self.assertIn('"status": "wrote"', out)
        write_daily_snapshot.assert_called_once_with(
            report_date="2026-06-05",
            bucket_name=None,
            prefix=None,
            overwrite=False,
            allow_stale_test_output=False,
        )

    def test_job_main_prints_refreshed_marker(self):
        result = snapshot_service.SnapshotResult(
            status="refreshed",
            report_date="2026-06-06",
            bucket="esip-reference-data",
            blob_name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=123,
        )

        with (
            mock.patch.object(snapshot_job.snapshot, "write_daily_snapshot", return_value=result),
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "run", "--date", "2026-06-06"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn(
            "REFRESHED stale report: gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            stdout.getvalue(),
        )

    def test_job_main_prints_skip_marker(self):
        result = snapshot_service.SnapshotResult(
            status="skip",
            report_date="2026-06-06",
            bucket="esip-reference-data",
            blob_name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            filename="service-desk-kpi-2026-06-06.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=0,
        )

        with (
            mock.patch.object(snapshot_job.snapshot, "write_daily_snapshot", return_value=result),
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "run", "--date", "2026-06-06"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn(
            "SKIP fresh existing report: gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-06.xlsx",
            stdout.getvalue(),
        )

    def test_job_help_includes_run_and_delivery_commands(self):
        with (
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "--help"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            with self.assertRaises(SystemExit) as cm:
                snapshot_job.main()

        self.assertEqual(cm.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("run", help_text)
        self.assertIn("deliver-due", help_text)
        self.assertIn("send-test", help_text)

    def test_deliver_due_prints_no_due_marker(self):
        config = delivery_service.DeliveryConfig(
            enabled=True,
            recipients=("priya.shah@northstar.example.com",),
            slots=(delivery_service.DeliverySlot(id="morning", time="10:30"),),
        )

        with (
            mock.patch.object(snapshot_job.delivery, "get_delivery_config", return_value=config),
            mock.patch.object(snapshot_job.delivery, "due_slots", return_value=[]),
            mock.patch.object(snapshot_job.delivery, "deliver_slot") as deliver_slot,
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "deliver-due", "--now", "2026-06-24T09:00:00-04:00"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn("EMAIL_SKIP no due slots", stdout.getvalue())
        deliver_slot.assert_not_called()

    def test_deliver_due_prints_sent_marker(self):
        slot = delivery_service.DeliverySlot(id="morning", time="10:30")
        config = delivery_service.DeliveryConfig(
            enabled=True,
            recipients=("priya.shah@northstar.example.com",),
            slots=(slot,),
        )
        outcome = delivery_service.DeliveryOutcome(
            status="sent",
            report_date="2026-06-05",
            slot_id="morning",
            slot_time="10:30",
            recipient_count=2,
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            run_id="2026-06-05_morning",
            snapshot_status="skip",
            message="sent",
        )

        with (
            mock.patch.object(snapshot_job.delivery, "get_delivery_config", return_value=config),
            mock.patch.object(snapshot_job.delivery, "due_slots", return_value=[slot]),
            mock.patch.object(snapshot_job.delivery, "deliver_slot", return_value=outcome) as deliver_slot,
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "deliver-due", "--date", "2026-06-05"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn(
            "EMAIL_SENT 2 gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            stdout.getvalue(),
        )
        deliver_slot.assert_called_once_with(config, slot, report_date="2026-06-05", now=None)

    def test_deliver_due_prints_already_sent_skip_marker(self):
        slot = delivery_service.DeliverySlot(id="morning", time="10:30")
        config = delivery_service.DeliveryConfig(
            enabled=True,
            recipients=("priya.shah@northstar.example.com",),
            slots=(slot,),
        )
        outcome = delivery_service.DeliveryOutcome(
            status="skipped",
            report_date="2026-06-05",
            slot_id="morning",
            slot_time="10:30",
            run_id="2026-06-05_morning",
            message="already sent",
        )

        with (
            mock.patch.object(snapshot_job.delivery, "get_delivery_config", return_value=config),
            mock.patch.object(snapshot_job.delivery, "due_slots", return_value=[slot]),
            mock.patch.object(snapshot_job.delivery, "deliver_slot", return_value=outcome),
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "deliver-due", "--date", "2026-06-05"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn("EMAIL_SKIP already sent", stdout.getvalue())

    def test_deliver_due_failure_prints_email_fail_and_exits_nonzero(self):
        slot = delivery_service.DeliverySlot(id="morning", time="10:30")
        config = delivery_service.DeliveryConfig(
            enabled=True,
            recipients=("priya.shah@northstar.example.com",),
            slots=(slot,),
        )

        with (
            mock.patch.object(snapshot_job.delivery, "get_delivery_config", return_value=config),
            mock.patch.object(snapshot_job.delivery, "due_slots", return_value=[slot]),
            mock.patch.object(snapshot_job.delivery, "deliver_slot", side_effect=delivery_service.DeliverySendError("boom")),
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "deliver-due", "--date", "2026-06-05"]),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 1)
        self.assertIn("EMAIL_FAIL DeliverySendError: boom", stderr.getvalue())

    def test_send_test_uses_safe_delivery_helper_only(self):
        outcome = delivery_service.DeliveryOutcome(
            status="sent",
            report_date="2026-06-05",
            recipient_count=1,
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            snapshot_status="skip",
            message="test sent to morgan.chen@northstar.example.com",
        )

        with (
            mock.patch.object(snapshot_job.delivery, "send_test_delivery", return_value=outcome) as send_test_delivery,
            mock.patch.object(sys, "argv", ["service_desk_kpi_snapshot", "send-test", "--date", "2026-06-05", "--recipient", "morgan.chen@northstar.example.com"]),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = snapshot_job.main()

        self.assertEqual(code, 0)
        self.assertIn(
            "EMAIL_SENT 1 gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            stdout.getvalue(),
        )
        send_test_delivery.assert_called_once_with(
            recipient="morgan.chen@northstar.example.com",
            report_date="2026-06-05",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
