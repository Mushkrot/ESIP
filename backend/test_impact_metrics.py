"""
ESIP impact measurement tests.
Run from project root: scripts/backend_python.sh backend/test_impact_metrics.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.services import firestore
from app.services import impact_metrics


class ImpactMetricsTest(unittest.TestCase):
    def test_rollout_windows_exclude_ramp_from_post(self):
        windows = impact_metrics.resolve_windows("2026-05-26", "2026-06-16")

        self.assertEqual(windows["baseline"]["start_date"], "2026-04-28")
        self.assertEqual(windows["baseline"]["end_date"], "2026-05-26")
        self.assertEqual(windows["ramp"]["start_date"], "2026-05-26")
        self.assertEqual(windows["ramp"]["end_date"], "2026-06-02")
        self.assertEqual(windows["post"]["start_date"], "2026-06-02")

    def test_existing_user_access_defaults_to_mass_rollout_date(self):
        with mock.patch("app.services.impact_metrics.firestore.get_db") as get_db:
            doc = mock.Mock()
            doc.id = "alex.morgan@northstar.example.com"
            doc.to_dict.return_value = {"active": True, "role": "user"}
            get_db.return_value.collection.return_value.stream.return_value = [doc]

            profiles = impact_metrics._load_profiles()

        self.assertEqual(profiles["alex.morgan@northstar.example.com"]["esip_access_granted_at"], "2026-05-26")

    def test_profile_display_name_falls_back_to_login_profile(self):
        user_doc = mock.Mock()
        user_doc.id = "alex.morgan@northstar.example.com"
        user_doc.to_dict.return_value = {"active": True, "role": "user"}
        profile_doc = mock.Mock()
        profile_doc.to_dict.return_value = {
            "email": "alex.morgan@northstar.example.com",
            "display_name": "Alex Morgan",
        }

        def collection(name: str):
            ref = mock.Mock()
            ref.stream.return_value = [profile_doc] if name == "profiles" else [user_doc]
            return ref

        with mock.patch("app.services.impact_metrics.firestore.get_db") as get_db:
            get_db.return_value.collection.side_effect = collection

            profiles = impact_metrics._load_profiles()

        self.assertEqual(profiles["alex.morgan@northstar.example.com"]["display_name"], "Alex Morgan")

    def test_profile_alias_map_prefers_primary_email(self):
        profiles = {
            "alex.morgan@northstar.example.com": {"active": True},
            "alex.legacy@support.northstar.example.com": {"active": True},
        }

        aliases = impact_metrics._profile_alias_map(profiles)

        self.assertEqual(aliases["alex.morgan@northstar.example.com"], "alex.morgan@northstar.example.com")
        self.assertEqual(aliases["alex.legacy@support.northstar.example.com"], "alex.morgan@northstar.example.com")
        self.assertEqual(
            impact_metrics._canonical_owner_key("alex.legacy@support.northstar.example.com", aliases),
            "alex.morgan@northstar.example.com",
        )

    def test_external_owner_key_is_excluded_from_impact_rows(self):
        self.assertEqual(impact_metrics._canonical_owner_key("vendor.user@external.example.org", {}), "")

    def test_reply_latency_uses_public_customer_to_public_agent_only(self):
        comments = [
            {
                "ticket_id": 10,
                "created_at": datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": False,
            },
            {
                "ticket_id": 10,
                "created_at": datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc),
                "is_internal": True,
                "author_is_agent": True,
                "author_email": "casey.nguyen@northstar.example.com",
            },
            {
                "ticket_id": 10,
                "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": True,
                "author_email": "casey.nguyen@northstar.example.com",
            },
        ]
        touches = [{
            "ticket_id": 10,
            "owner_email": "casey.nguyen@northstar.example.com",
            "created_at": datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        }]

        rows = impact_metrics.compute_reply_latencies_from_comments(comments, touches)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["latency_hours"], 2.0)
        self.assertTrue(rows[0]["esip_assisted"])

    def test_reply_latency_counts_only_next_public_agent_reply(self):
        comments = [
            {
                "ticket_id": 11,
                "created_at": datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": False,
            },
            {
                "ticket_id": 11,
                "created_at": datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": False,
            },
            {
                "ticket_id": 11,
                "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": True,
                "author_email": "casey.nguyen@northstar.example.com",
            },
            {
                "ticket_id": 11,
                "created_at": datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
                "is_internal": False,
                "author_is_agent": True,
                "author_email": "casey.nguyen@northstar.example.com",
            },
        ]

        rows = impact_metrics.compute_reply_latencies_from_comments(comments, [])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["latency_hours"], 1.5)

    def test_public_warnings_do_not_expose_query_errors(self):
        warning = impact_metrics._public_warning("response_speed", RuntimeError("SELECT failed at x.y"))

        self.assertIn("Response-speed metrics are not available", warning)
        self.assertNotIn("SELECT", warning)


class ImpactEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "admin", "email": "priya.shah@northstar.example.com", "name": "Admin"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=firestore.ROLE_ADMIN),
        ):
            yield

    def test_admin_can_read_impact_dashboard_without_raw_transcripts(self):
        payload = {
            "summary": {"active_users_post": 1},
            "users": [{"display_name": "Agent One", "email": "casey.nguyen@northstar.example.com"}],
            "warnings": [],
        }
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.impact_metrics.get_impact_dashboard", return_value=payload),
        ):
            res = self.client.get(
                "/api/admin/impact/dashboard?rollout_date=2026-05-26",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["users"][0]["display_name"], "Agent One")
        self.assertNotIn("assistant_answer", str(body))
        self.assertNotIn("gcs_payload_uri", str(body))

    def test_non_admin_cannot_read_impact_dashboard(self):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "user", "email": "morgan.chen@northstar.example.com", "name": "User"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=firestore.ROLE_USER),
        ):
            res = self.client.get(
                "/api/admin/impact/dashboard?rollout_date=2026-05-26",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 403)

    def test_telemetry_identity_comes_from_auth_not_request_body(self):
        with (
            self._auth_patches(),
            mock.patch("app.routers.telemetry.impact_telemetry.save_event", return_value=True) as save_event,
        ):
            res = self.client.post(
                "/api/telemetry/impact-event",
                headers={"Authorization": "Bearer fake"},
                json={
                    "event_type": "ticket_page_opened",
                    "surface": "web",
                    "ticket_id": 950032,
                    "owner_email": "sofia.reyes@northstar.example.com",
                },
            )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["accepted"])
        event = save_event.call_args.kwargs["event"]
        user = save_event.call_args.kwargs["user"]
        self.assertNotIn("owner_email", event)
        self.assertEqual(user["email"], "priya.shah@northstar.example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
