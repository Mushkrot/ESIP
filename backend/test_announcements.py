"""
Product announcement audience and admin route tests.
Run from project root: scripts/backend_python.sh backend/test_announcements.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from fastapi.testclient import TestClient

from app.services import announcements, firestore


class AnnouncementAudienceTest(unittest.TestCase):
    def test_admin_audience_excludes_users_and_super_admin(self):
        item = {"audience_roles": ["admin"], "surfaces": ["web"], "status": "published"}

        self.assertFalse(announcements._matches_user(item, role="user", surface="web"))
        self.assertTrue(announcements._matches_user(item, role="admin", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="super_admin", surface="web"))

    def test_all_audience_excludes_super_admin(self):
        item = {"audience_roles": ["all"], "surfaces": ["web"], "status": "published"}

        self.assertTrue(announcements._matches_user(item, role="user", surface="web"))
        self.assertTrue(announcements._matches_user(item, role="admin", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="super_admin", surface="web"))

    def test_user_audience_excludes_admins(self):
        item = {"audience_roles": ["user"], "surfaces": ["web"], "status": "published"}

        self.assertTrue(announcements._matches_user(item, role="user", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="admin", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="super_admin", surface="web"))

    def test_missing_audience_defaults_to_users_and_admins(self):
        item = {"surfaces": ["web"], "status": "published"}

        self.assertTrue(announcements._matches_user(item, role="user", surface="web"))
        self.assertTrue(announcements._matches_user(item, role="admin", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="super_admin", surface="web"))

    def test_legacy_super_admin_audience_matches_no_viewers(self):
        item = {"audience_roles": ["super_admin"], "surfaces": ["web"], "status": "published"}

        self.assertFalse(announcements._matches_user(item, role="user", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="admin", surface="web"))
        self.assertFalse(announcements._matches_user(item, role="super_admin", surface="web"))


class AnnouncementAdminEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self, role: str = firestore.ROLE_SUPER_ADMIN):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "admin", "email": "priya.shah@northstar.example.com", "name": "Admin"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
        ):
            yield

    def test_super_admin_can_delete_announcement(self):
        payload = {"id": "ann_test", "deleted": True, "deleted_read_count": 2}
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.announcements_service.delete_announcement", return_value=payload) as delete_mock,
        ):
            res = self.client.delete(
                "/api/admin/announcements/ann_test",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["deleted"])
        delete_mock.assert_called_once()

    def test_regular_admin_cannot_delete_announcement(self):
        with (
            self._auth_patches(role=firestore.ROLE_ADMIN),
            mock.patch("app.routers.admin.announcements_service.delete_announcement") as delete_mock,
        ):
            res = self.client.delete(
                "/api/admin/announcements/ann_test",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 403)
        delete_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
