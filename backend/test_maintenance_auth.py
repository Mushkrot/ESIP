"""
Unit tests for maintenance mode auth (Firebase + ZAF).
Run from backend/:  python3 test_maintenance_auth.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient


class TestMaintenanceAuth(unittest.TestCase):
    """Patch Firestore / Google auth; do not require real credentials or Firestore."""

    @classmethod
    def setUpClass(cls):
        # Import app once; .env may be loaded by main
        from app.main import app

        cls.app = app
        cls.client = TestClient(app)

    def test_get_current_user_maintenance_returns_503(self):
        """Firebase + allowed domain + regular user in maintenance → 503."""
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": "u1",
                    "email": "alex.morgan@northstar.example.com",
                    "name": "Agent",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=True),
            mock.patch("app.routers.auth.firestore.get_or_create_profile"),
        ):
            r = self.client.get("/api/auth/me", headers={"Authorization": "Bearer fake-token"})
            self.assertEqual(r.status_code, 503)
            self.assertIn("maintenance", (r.json() or {}).get("detail", "").lower())

    def test_get_current_user_provisioned_returns_200(self):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": "u1",
                    "email": "priya.shah@northstar.example.com",
                    "name": "Admin",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.routers.auth.firestore.get_or_create_profile"),
            mock.patch("app.routers.auth.firestore.sync_user_identity_from_auth"),
            mock.patch(
                "app.routers.auth.firestore.get_user_role",
                return_value="admin",
            ),
        ):
            r = self.client.get("/api/auth/me", headers={"Authorization": "Bearer fake-token"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["email"], "priya.shah@northstar.example.com")

    def test_get_any_user_firebase_maintenance_returns_503(self):
        """AnyUser + Firebase: same as /me — 503, not 401."""
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "u1", "email": "alex.morgan@northstar.example.com"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=True),
        ):
            r = self.client.get(
                "/api/tickets/1/similar",
                headers={"Authorization": "Bearer fake-token"},
                params={"limit": 1},
            )
            self.assertEqual(r.status_code, 503)
            self.assertIn("maintenance", (r.json() or {}).get("detail", "").lower())

    def test_get_any_user_firebase_not_provisioned_returns_403(self):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "u1", "email": "casey.nguyen@northstar.example.com"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=False),
        ):
            r = self.client.get(
                "/api/tickets/1/similar",
                headers={"Authorization": "Bearer fake-token"},
            )
            self.assertEqual(r.status_code, 403)

    def test_zaf_auth_maintenance_returns_503(self):
        with (
            mock.patch("app.routers.zaf_auth.firestore.is_maintenance_blocked", return_value=True),
            self.assertLogs("app.routers.zaf_auth", level="WARNING") as logs,
        ):
            r = self.client.post(
                "/api/zaf/auth",
                json={"email": "alex.morgan@northstar.example.com", "name": "A", "role": "agent"},
            )
            self.assertEqual(r.status_code, 503)
        joined = "\n".join(logs.output)
        self.assertIn("alex.morgan@northstar.example.com", joined)
        self.assertIn("reason=maintenance_blocked", joined)

    def test_zaf_auth_bad_domain_logs_denied_email_and_reason(self):
        with self.assertLogs("app.routers.zaf_auth", level="WARNING") as logs:
            r = self.client.post(
                "/api/zaf/auth",
                json={"email": "vendor.user@external.example.org", "name": "Vendor", "role": "agent"},
            )

        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "Access restricted to approved Northstar accounts.")
        joined = "\n".join(logs.output)
        self.assertIn("vendor.user@external.example.org", joined)
        self.assertIn("role=agent", joined)
        self.assertIn("reason=bad_domain", joined)

    def test_zaf_auth_not_allowlisted_logs_denied_email_and_reason(self):
        with (
            mock.patch("app.routers.zaf_auth.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.routers.zaf_auth.firestore.is_user_allowed", return_value=False),
            self.assertLogs("app.routers.zaf_auth", level="WARNING") as logs,
        ):
            r = self.client.post(
                "/api/zaf/auth",
                json={"email": "morgan.chen@northstar.example.com", "name": "New Agent", "role": "agent"},
            )

        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "Your account has not been provisioned for ESIP. Contact IT.")
        joined = "\n".join(logs.output)
        self.assertIn("morgan.chen@northstar.example.com", joined)
        self.assertIn("role=agent", joined)
        self.assertIn("reason=not_allowlisted", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
