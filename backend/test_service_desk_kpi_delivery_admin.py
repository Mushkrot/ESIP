"""Admin API tests for Service Desk KPI email delivery settings."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest import mock

from fastapi.testclient import TestClient

from app.services import firestore
from app.services import service_desk_kpi_delivery as delivery


class ServiceDeskKpiDeliveryAdminApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    def _auth_patches(self, role: str = firestore.ROLE_SUPER_ADMIN, email: str = "jordan.lee@northstar.example.com"):
        return (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "u1", "email": email, "name": "Admin"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
        )

    def _authorized(self, role: str = firestore.ROLE_SUPER_ADMIN):
        stack = ExitStack()
        for patcher in self._auth_patches(role):
            stack.enter_context(patcher)
        stack.enter_context(
            mock.patch(
                "app.routers.admin._get_kpi_delivery_scheduler_state",
                return_value={"job_id": "esip-service-desk-kpi-snapshot-daily", "enabled": False, "state": "PAUSED", "schedule_time": "10:30"},
            )
        )
        return stack

    def test_get_delivery_returns_default_ui_payload(self):
        config = delivery.normalize_config({})
        with self._authorized(firestore.ROLE_ADMIN) as stack:
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.get_delivery_config", return_value=config)
            )
            response = self.client.get(
                "/api/admin/usage/service-desk-kpi/delivery",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["timezone"], "America/New_York")
        self.assertEqual(body["recipients"], [])
        self.assertEqual(body["cc_recipients"], [])
        self.assertEqual(body["slots"][0]["time"], "10:30")
        self.assertIn("transport", body)
        self.assertIn("last_delivery", body)
        self.assertIn("last_test", body)
        self.assertIn("safe_test_recipients", body)
        self.assertEqual(body["scheduler"]["state"], "PAUSED")

    def test_enabled_toggle_syncs_cloud_scheduler(self):
        config = delivery.normalize_config({"enabled": False, "slots": [{"id": "slot-1030", "time": "10:30"}]})
        saved = delivery.normalize_config({"enabled": True, "slots": [{"id": "slot-1030", "time": "10:30"}]})
        scheduler_state = {
            "job_id": "esip-service-desk-kpi-snapshot-daily",
            "enabled": True,
            "state": "ENABLED",
            "schedule_time": "10:30",
        }
        with self._authorized() as stack:
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.get_delivery_config", return_value=config)
            )
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.save_delivery_config", return_value=saved)
            )
            sync_scheduler = stack.enter_context(
                mock.patch("app.routers.admin._sync_kpi_delivery_scheduler", return_value=scheduler_state)
            )
            response = self.client.patch(
                "/api/admin/usage/service-desk-kpi/delivery",
                headers={"Authorization": "Bearer fake-token"},
                json={"enabled": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.assertEqual(response.json()["scheduler"]["state"], "ENABLED")
        sync_scheduler.assert_called_once()
        self.assertTrue(sync_scheduler.call_args.args[0].enabled)

    def test_scheduler_sync_skips_cron_update_when_aligned(self):
        from app.routers import admin as admin_router

        config = delivery.normalize_config(
            {
                "enabled": True,
                "slots": [{"id": "slot-1030", "time": "10:30", "label": "KPI report morning distribution"}],
            }
        )
        scheduler_state = {
            "job_id": "esip-service-desk-kpi-snapshot-daily",
            "enabled": True,
            "state": "ENABLED",
            "schedule_time": "10:30",
            "time_zone": "America/New_York",
        }
        with mock.patch("app.routers.admin.scheduler_admin.get_scheduler", return_value=scheduler_state):
            with mock.patch("app.routers.admin.scheduler_admin.update_scheduler_time") as update_time:
                with mock.patch("app.routers.admin.scheduler_admin.resume_scheduler") as resume_scheduler:
                    with mock.patch("app.routers.admin.scheduler_admin.pause_scheduler") as pause_scheduler:
                        result = admin_router._sync_kpi_delivery_scheduler(config)

        self.assertEqual(result, scheduler_state)
        update_time.assert_not_called()
        resume_scheduler.assert_not_called()
        pause_scheduler.assert_not_called()

    def test_update_rejects_invalid_and_duplicate_inputs(self):
        config = delivery.normalize_config({})
        cases = [
            ({"recipients": ["not-email"]}, "Invalid email"),
            ({"recipients": ["jordan.lee@northstar.example.com", "jordan.lee@northstar.example.com"]}, "Duplicate recipient"),
            ({"cc_recipients": ["not-email"]}, "Invalid email"),
            ({"recipients": ["jordan.lee@northstar.example.com"], "cc_recipients": ["jordan.lee@northstar.example.com"]}, "both To and CC"),
            ({"slots": [{"id": "bad", "time": "10:10"}]}, "15-minute"),
            ({"timezone": "Mars/Base"}, "Invalid timezone"),
        ]
        with self._authorized() as stack:
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.get_delivery_config", return_value=config)
            )
            save_delivery_config = stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.save_delivery_config")
            )
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    response = self.client.patch(
                        "/api/admin/usage/service-desk-kpi/delivery",
                        headers={"Authorization": "Bearer fake-token"},
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 422)
                    self.assertIn(expected, response.text)

        save_delivery_config.assert_not_called()

    def test_write_endpoints_call_role_preview_guard(self):
        config = delivery.normalize_config({"recipients": [], "slots": []})
        saved = delivery.normalize_config({"recipients": ["jordan.lee@northstar.example.com"], "cc_recipients": ["sofia.reyes@northstar.example.com"], "slots": []})
        with self._authorized() as stack:
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.get_delivery_config", return_value=config)
            )
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.save_delivery_config", return_value=saved)
            )
            reject_role_preview_write = stack.enter_context(
                mock.patch("app.routers.admin.reject_role_preview_write", return_value=None)
            )
            response = self.client.post(
                "/api/admin/usage/service-desk-kpi/delivery/recipients",
                headers={"Authorization": "Bearer fake-token"},
                json={"email": "jordan.lee@northstar.example.com"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cc_recipients"], ["sofia.reyes@northstar.example.com"])
        reject_role_preview_write.assert_called_once()

    def test_production_recipient_writes_require_super_admin(self):
        with self._authorized(firestore.ROLE_ADMIN) as stack:
            save_delivery_config = stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.save_delivery_config")
            )
            response = self.client.post(
                "/api/admin/usage/service-desk-kpi/delivery/recipients",
                headers={"Authorization": "Bearer fake-token"},
                json={"email": "jordan.lee@northstar.example.com"},
            )

        self.assertEqual(response.status_code, 403)
        save_delivery_config.assert_not_called()

    def test_safe_test_endpoint_never_uses_production_recipient_list(self):
        production_config = delivery.normalize_config(
            {
                "enabled": True,
                "recipients": ["noah.bennett@northstar.example.com"],
                "cc_recipients": ["sofia.reyes@northstar.example.com"],
                "slots": [{"id": "morning", "time": "10:30"}],
            }
        )
        saved = delivery.normalize_config(
            {
                "enabled": True,
                "recipients": ["noah.bennett@northstar.example.com"],
                "cc_recipients": ["sofia.reyes@northstar.example.com"],
                "slots": [{"id": "morning", "time": "10:30"}],
                "last_test": {"status": "sent"},
            }
        )
        outcome = delivery.DeliveryOutcome(
            status="sent",
            report_date="2026-06-05",
            recipient_count=1,
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            snapshot_status="skip",
            message="test sent",
        )

        with self._authorized() as stack:
            send_test_delivery = stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.send_test_delivery", return_value=outcome)
            )
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.get_delivery_config", return_value=production_config)
            )
            stack.enter_context(
                mock.patch("app.routers.admin.service_desk_kpi_delivery.save_delivery_config", return_value=saved)
            )
            response = self.client.post(
                "/api/admin/usage/service-desk-kpi/delivery/test",
                headers={"Authorization": "Bearer fake-token"},
                json={"recipient": "taylor.kim@northstar.example.com", "report_date": "2026-06-05"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cc_recipients"], ["sofia.reyes@northstar.example.com"])
        send_test_delivery.assert_called_once_with(
            recipient="taylor.kim@northstar.example.com",
            report_date="2026-06-05",
        )
        self.assertEqual(response.json()["test"]["recipient_count"], 1)

    def test_safe_test_endpoint_rejects_unsafe_recipient(self):
        with self._authorized() as stack:
            send_test_delivery = stack.enter_context(
                mock.patch(
                    "app.routers.admin.service_desk_kpi_delivery.send_test_delivery",
                    side_effect=delivery.DeliveryValidationError("unsafe recipient"),
                )
            )
            response = self.client.post(
                "/api/admin/usage/service-desk-kpi/delivery/test",
                headers={"Authorization": "Bearer fake-token"},
                json={"recipient": "noah.bennett@northstar.example.com"},
            )

        self.assertEqual(response.status_code, 422)
        send_test_delivery.assert_called_once_with(
            recipient="noah.bennett@northstar.example.com",
            report_date=None,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
