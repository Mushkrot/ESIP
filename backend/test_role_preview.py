"""
Role preview auth tests.
Run from project root: scripts/backend_python.sh backend/test_role_preview.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.dependencies import (
    attach_role_context,
    reject_role_preview_write,
    require_super_admin,
)
from app.services import firestore


class TestRolePreviewHelpers(unittest.TestCase):
    def test_super_admin_can_preview_admin(self):
        with mock.patch(
            "app.dependencies.firestore.get_user_role",
            return_value=firestore.ROLE_SUPER_ADMIN,
        ):
            user = attach_role_context(
                {"uid": "u1", "email": "morgan.chen@northstar.example.com"},
                firestore.ROLE_ADMIN,
            )

        self.assertEqual(user["email"], "morgan.chen@northstar.example.com")
        self.assertEqual(user["actual_role"], firestore.ROLE_SUPER_ADMIN)
        self.assertEqual(user["effective_role"], firestore.ROLE_ADMIN)
        self.assertEqual(user["role"], firestore.ROLE_ADMIN)
        self.assertTrue(user["role_preview"])
        self.assertTrue(user["can_preview_roles"])

        with self.assertRaises(HTTPException) as ctx:
            require_super_admin(user)
        self.assertEqual(ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as write_ctx:
            reject_role_preview_write(user)
        self.assertEqual(write_ctx.exception.status_code, 403)

    def test_non_super_admin_cannot_preview(self):
        with (
            mock.patch(
                "app.dependencies.firestore.get_user_role",
                return_value=firestore.ROLE_ADMIN,
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            attach_role_context(
                {"uid": "u2", "email": "priya.shah@northstar.example.com"},
                firestore.ROLE_USER,
            )

        self.assertEqual(ctx.exception.status_code, 403)


class TestRolePreviewRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self, role: str = firestore.ROLE_SUPER_ADMIN):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": "u1",
                    "email": "morgan.chen@northstar.example.com",
                    "name": "Boss",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
            mock.patch("app.routers.auth.firestore.get_or_create_profile"),
            mock.patch("app.routers.auth.firestore.sync_user_identity_from_auth"),
        ):
            yield

    def test_me_reports_actual_and_effective_roles(self):
        with self._auth_patches():
            r = self.client.get(
                "/api/auth/me",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "admin",
                },
            )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["actual_role"], firestore.ROLE_SUPER_ADMIN)
        self.assertEqual(body["effective_role"], firestore.ROLE_ADMIN)
        self.assertEqual(body["role"], firestore.ROLE_ADMIN)
        self.assertTrue(body["role_preview"])
        self.assertTrue(body["can_preview_roles"])

    def test_preview_admin_cannot_read_super_admin_chat_logs(self):
        with self._auth_patches():
            r = self.client.get(
                "/api/admin/chat-logs/recent",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "admin",
                },
            )

        self.assertEqual(r.status_code, 403)
        self.assertIn("super", r.json()["detail"].lower())

    def test_preview_admin_cannot_read_super_admin_quality_dashboard(self):
        with self._auth_patches():
            r = self.client.get(
                "/api/admin/quality/dashboard",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "admin",
                },
            )

        self.assertEqual(r.status_code, 403)
        self.assertIn("super", r.json()["detail"].lower())

    def test_preview_admin_write_is_read_only(self):
        with self._auth_patches():
            r = self.client.patch(
                "/api/admin/users/alex.morgan@northstar.example.com",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "admin",
                },
                json={"active": False},
            )

        self.assertEqual(r.status_code, 403)
        self.assertIn("read-only", r.json()["detail"])

    def test_regular_admin_cannot_enable_preview(self):
        with self._auth_patches(role=firestore.ROLE_ADMIN):
            r = self.client.get(
                "/api/auth/me",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "user",
                },
            )

        self.assertEqual(r.status_code, 403)
        self.assertIn("super admin", r.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
