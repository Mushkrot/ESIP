"""
Feedback authorization tests.
Run from project root: scripts/backend_python.sh backend/test_feedback_auth.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.services import firestore


class TestFeedbackAuthorization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self, role: str, uid: str = "u-admin"):
        email = (
            "morgan.chen@northstar.example.com"
            if role == firestore.ROLE_SUPER_ADMIN
            else "priya.shah@northstar.example.com"
        )
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": uid,
                    "email": email,
                    "name": "Morgan Chen" if role == firestore.ROLE_SUPER_ADMIN else "Priya Shah",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
        ):
            yield

    def test_admin_cannot_read_all_feedback(self):
        with (
            self._auth_patches(firestore.ROLE_ADMIN),
            mock.patch("app.routers.feedback.firestore.get_all_feedback_entries") as get_all,
        ):
            r = self.client.get("/api/feedback/all", headers={"Authorization": "Bearer fake-token"})

        self.assertEqual(r.status_code, 403)
        self.assertIn("super admin", r.json()["detail"].lower())
        get_all.assert_not_called()

    def test_admin_can_read_only_own_feedback_history(self):
        entry = {
            "id": "f1",
            "text": "my feedback",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "user_email": "priya.shah@northstar.example.com",
            "user_name": "Priya Shah",
        }
        with (
            self._auth_patches(firestore.ROLE_ADMIN, uid="u-admin"),
            mock.patch("app.routers.feedback.firestore.get_feedback_entries", return_value=[entry]) as get_own,
            mock.patch("app.routers.feedback.firestore.get_feedback_replies", return_value=[]),
        ):
            r = self.client.get("/api/feedback", headers={"Authorization": "Bearer fake-token"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["id"], "f1")
        get_own.assert_called_once_with("u-admin")

    def test_feedback_submission_notifies_owner_after_persisting(self):
        entry = {
            "id": "f-new",
            "text": "Please improve ticket search.",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "user_email": "priya.shah@northstar.example.com",
            "user_name": "Priya Shah",
        }
        with (
            self._auth_patches(firestore.ROLE_ADMIN, uid="u-user"),
            mock.patch("app.routers.feedback.firestore.add_feedback_entry", return_value=entry) as add_entry,
            mock.patch("app.routers.feedback.send_feedback_notification", return_value=True) as notify,
        ):
            r = self.client.post(
                "/api/feedback",
                headers={"Authorization": "Bearer fake-token"},
                json={"text": "Please improve ticket search."},
            )

        self.assertEqual(r.status_code, 201)
        add_entry.assert_called_once()
        notify.assert_called_once_with(
            user_name="Priya Shah",
            user_email="priya.shah@northstar.example.com",
            text="Please improve ticket search.",
        )

    def test_feedback_submission_succeeds_when_notification_fails(self):
        entry = {
            "id": "f-mail-failure",
            "text": "Still save this feedback.",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "user_email": "morgan.chen@northstar.example.com",
            "user_name": "Priya Shah",
        }
        with (
            self._auth_patches(firestore.ROLE_ADMIN, uid="u-user"),
            mock.patch("app.routers.feedback.firestore.add_feedback_entry", return_value=entry),
            mock.patch("app.routers.feedback.send_feedback_notification", return_value=False),
        ):
            r = self.client.post(
                "/api/feedback",
                headers={"Authorization": "Bearer fake-token"},
                json={"text": "Still save this feedback."},
            )

        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["id"], "f-mail-failure")

    def test_admin_cannot_reply_to_other_feedback(self):
        with (
            self._auth_patches(firestore.ROLE_ADMIN),
            mock.patch("app.routers.feedback.firestore.get_feedback_entry") as get_entry,
        ):
            r = self.client.post(
                "/api/feedback/u-user/f1/reply",
                headers={"Authorization": "Bearer fake-token"},
                json={"text": "reply"},
            )

        self.assertEqual(r.status_code, 403)
        self.assertIn("super admin", r.json()["detail"].lower())
        get_entry.assert_not_called()

    def test_super_admin_can_read_all_feedback(self):
        entry = {
            "id": "f1",
            "uid": "u-user",
            "text": "team feedback",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "user_email": "morgan.chen@northstar.example.com",
            "user_name": "Morgan Chen",
        }
        with (
            self._auth_patches(firestore.ROLE_SUPER_ADMIN, uid="u-super"),
            mock.patch("app.routers.feedback.firestore.get_all_feedback_entries", return_value=[entry]),
            mock.patch("app.routers.feedback.firestore.get_feedback_replies", return_value=[]),
        ):
            r = self.client.get("/api/feedback/all", headers={"Authorization": "Bearer fake-token"})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()[0]["uid"], "u-user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
