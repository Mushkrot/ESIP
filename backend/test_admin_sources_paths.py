"""
Smoke tests for Admin source path rules endpoint.
Run from backend/:  python3 test_admin_sources_paths.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient


class TestAdminSourcePathRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fake-token"}

    def test_patch_source_paths_ok(self):
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
            mock.patch("app.dependencies.firestore.get_user_role", return_value="admin"),
            mock.patch("app.routers.admin.firestore.is_user_admin", return_value=True),
            mock.patch(
                "app.routers.admin.firestore.update_data_source_paths",
                return_value={
                    "display_name": "Store Operations Job Aids",
                    "type": "vertex_ai",
                    "enabled": True,
                    "exclude_path_prefixes": ["2. General/Archived"],
                },
            ) as upd_mock,
        ):
            r = self.client.patch(
                "/api/admin/sources/gdrive_operations_job_aids/paths",
                headers=self._auth_headers(),
                json={"exclude_path_prefixes": ["2. General/Archived"]},
            )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["source_id"], "gdrive_operations_job_aids")
        self.assertEqual(body["exclude_path_prefixes"], ["2. General/Archived"])
        upd_mock.assert_called_once_with(
            "gdrive_operations_job_aids",
            ["2. General/Archived"],
        )

    def test_patch_source_paths_rejects_non_list(self):
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
            mock.patch("app.dependencies.firestore.get_user_role", return_value="admin"),
            mock.patch("app.routers.admin.firestore.is_user_admin", return_value=True),
        ):
            r = self.client.patch(
                "/api/admin/sources/gdrive_operations_job_aids/paths",
                headers=self._auth_headers(),
                json={"exclude_path_prefixes": "2. General/Archived"},
            )

        self.assertEqual(r.status_code, 422)
        self.assertIn("must be a list", (r.json() or {}).get("detail", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
