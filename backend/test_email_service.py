"""Tests for ESIP email notification transport and safe test sends."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest import mock

from fastapi.testclient import TestClient


class TestEmailService(unittest.TestCase):
    def test_send_email_uses_gmail_without_implicit_sendgrid_fallback(self):
        from app.services import email_service

        with (
            mock.patch.object(email_service.settings, "email_transport", "gmail_api"),
            mock.patch.object(email_service.settings, "email_sendgrid_fallback_enabled", False),
            mock.patch("app.services.email_service._send_via_gmail", return_value=False) as gmail_send,
            mock.patch("app.services.email_service._send_via_sendgrid", return_value=True) as sendgrid_send,
        ):
            sent = email_service.send_email(
                recipients=["Morgan.Chen@northstar.example.com"],
                cc_recipients=["Priya.Shah@northstar.example.com"],
                subject="Test",
                body="Body",
            )

        self.assertFalse(sent)
        gmail_send.assert_called_once()
        self.assertEqual(gmail_send.call_args.kwargs["recipients"], ["morgan.chen@northstar.example.com"])
        self.assertEqual(gmail_send.call_args.kwargs["cc_recipients"], ["priya.shah@northstar.example.com"])
        sendgrid_send.assert_not_called()

    def test_send_alert_can_target_explicit_test_recipient(self):
        from app.services import email_service

        with (
            mock.patch("app.services.email_service._get_recipients") as get_recipients,
            mock.patch("app.services.email_service.send_email", return_value=True) as send_email,
        ):
            sent = email_service.send_alert(
                "Test notification",
                "Body",
                level="info",
                recipients=["priya.shah@northstar.example.com"],
            )

        self.assertTrue(sent)
        get_recipients.assert_not_called()
        kwargs = send_email.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["priya.shah@northstar.example.com"])
        self.assertIn("[ESIP] Info", kwargs["subject"])

    def test_feedback_notification_targets_only_feedback_owner(self):
        from app.services import email_service

        with mock.patch("app.services.email_service.send_email", return_value=True) as send_email:
            sent = email_service.send_feedback_notification(
                user_name="Feedback User",
                user_email="casey.nguyen@northstar.example.com",
                text="The search result needs more detail.",
            )

        self.assertTrue(sent)
        kwargs = send_email.call_args.kwargs
        self.assertEqual(kwargs["recipients"], ["priya.shah@northstar.example.com"])
        self.assertEqual(kwargs["subject"], "[ESIP] New feedback from Feedback User")
        self.assertIn("casey.nguyen@northstar.example.com", kwargs["body"])
        self.assertIn("The search result needs more detail.", kwargs["body"])

    def test_build_mime_message_supports_attachment(self):
        from app.services.email_service import EmailAttachment, _build_mime_message

        message = _build_mime_message(
            recipients=["jordan.lee@northstar.example.com"],
            cc_recipients=["sofia.reyes@northstar.example.com", "priya.shah@northstar.example.com"],
            subject="Report",
            body="Attached.",
            attachments=[
                EmailAttachment(
                    filename="report.xlsx",
                    content=b"xlsx bytes",
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            ],
        )

        self.assertEqual(message["To"], "jordan.lee@northstar.example.com")
        self.assertEqual(
            message["Cc"],
            "sofia.reyes@northstar.example.com, priya.shah@northstar.example.com",
        )
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "report.xlsx")


class TestNotificationTestEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    def _auth_patches(self, email: str = "jordan.lee@northstar.example.com"):
        return (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "u1", "email": email, "name": "Admin"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value="admin"),
        )

    def test_notification_test_defaults_to_safe_caller_recipient(self):
        with ExitStack() as stack:
            for patcher in self._auth_patches("priya.shah@northstar.example.com"):
                stack.enter_context(patcher)
            send_alert = stack.enter_context(mock.patch("app.services.email_service.send_alert", return_value=True))
            response = self.client.post(
                "/api/admin/notifications/test",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recipient"], "priya.shah@northstar.example.com")
        self.assertEqual(send_alert.call_args.kwargs["recipients"], ["priya.shah@northstar.example.com"])

    def test_notification_test_non_safe_caller_defaults_to_platform_admin(self):
        with ExitStack() as stack:
            for patcher in self._auth_patches("alex.morgan@northstar.example.com"):
                stack.enter_context(patcher)
            send_alert = stack.enter_context(mock.patch("app.services.email_service.send_alert", return_value=True))
            response = self.client.post(
                "/api/admin/notifications/test",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recipient"], "morgan.chen@northstar.example.com")
        self.assertEqual(send_alert.call_args.kwargs["recipients"], ["morgan.chen@northstar.example.com"])

    def test_notification_test_rejects_unsafe_requested_recipient(self):
        with ExitStack() as stack:
            for patcher in self._auth_patches("jordan.lee@northstar.example.com"):
                stack.enter_context(patcher)
            send_alert = stack.enter_context(mock.patch("app.services.email_service.send_alert", return_value=True))
            response = self.client.post(
                "/api/admin/notifications/test",
                headers={"Authorization": "Bearer fake-token"},
                json={"recipient": "noah.bennett@northstar.example.com"},
            )

        self.assertEqual(response.status_code, 422)
        send_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
