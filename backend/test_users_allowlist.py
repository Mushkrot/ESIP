"""
Allowlist + email normalization (Firestore users/ docs are keyed by lowercased email).
Run:  python3 test_users_allowlist.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.routers import admin
from app.services import firestore


def _make_user_doc(
    active: bool = True,
    role: str = firestore.ROLE_USER,
) -> dict:
    return {"active": active, "role": role}


class TestUserEmailNormalization(unittest.TestCase):
    """is_user_allowed / get_user_role must find users/{lowercase} even if email has mixed case."""

    def test_norm_user_email(self):
        self.assertEqual(
            "priya.shah@northstar.example.com",
            firestore._norm_user_email("  Priya.Shah@northstar.example.com  "),
        )

    @mock.patch("app.services.firestore.is_maintenance_mode", return_value=False)
    @mock.patch("app.services.firestore.get_db")
    def test_is_user_allowed_mixed_case_matches_doc(self, mock_get_db, _maint):
        doc = mock.MagicMock()
        doc.exists = True
        doc.to_dict.return_value = _make_user_doc(True, firestore.ROLE_ADMIN)
        mock_get_db.return_value.collection.return_value.document.return_value.get.return_value = (
            doc
        )

        self.assertTrue(
            firestore.is_user_allowed("Sofia.Reyes@northstar.example.com"),
        )
        # Must query normalized id
        mock_get_db.return_value.collection.assert_called_with("users")
        doc_ref = mock_get_db.return_value.collection.return_value.document
        doc_ref.assert_called_with("sofia.reyes@northstar.example.com")

    @mock.patch("app.services.firestore.is_maintenance_mode", return_value=False)
    @mock.patch("app.services.firestore.get_db")
    def test_inactive_denied(self, mock_get_db, _maint):
        doc = mock.MagicMock()
        doc.exists = True
        doc.to_dict.return_value = _make_user_doc(
            active=False, role=firestore.ROLE_ADMIN
        )
        mock_get_db.return_value.collection.return_value.document.return_value.get.return_value = (
            doc
        )
        self.assertFalse(firestore.is_user_allowed("morgan.chen@northstar.example.com"))

    def test_admin_user_payload_includes_display_name_from_profile(self):
        payload = admin._user_payload(
            "casey.nguyen@northstar.example.com",
            _make_user_doc(True, firestore.ROLE_USER),
            profile_names={"casey.nguyen@northstar.example.com": "Casey Nguyen"},
        )

        self.assertEqual(payload["email"], "casey.nguyen@northstar.example.com")
        self.assertEqual(payload["display_name"], "Casey Nguyen")

    def test_admin_user_payload_does_not_invent_email_name(self):
        payload = admin._user_payload(
            "jordan.lee@northstar.example.com",
            _make_user_doc(True, firestore.ROLE_USER),
        )

        self.assertEqual(payload["display_name"], "")

    def test_clean_auth_display_name_rejects_email_fallback(self):
        self.assertEqual(
            firestore.clean_auth_display_name("casey.nguyen@northstar.example.com", "casey.nguyen@northstar.example.com"),
            "",
        )
        self.assertEqual(
            firestore.clean_auth_display_name("Casey Nguyen", "casey.nguyen@northstar.example.com"),
            "Casey Nguyen",
        )

    def test_clean_auth_display_name_rejects_synthetic_proof_labels(self):
        self.assertEqual(
            firestore.clean_auth_display_name("ESIP Pilot Proof", "alex.morgan@northstar.example.com"),
            "",
        )
        self.assertEqual(
            firestore.clean_auth_display_name("QA User", "casey.nguyen@northstar.example.com"),
            "",
        )
        self.assertEqual(
            firestore.clean_auth_display_name("Agent Proof", "casey.nguyen@northstar.example.com"),
            "Agent Proof",
        )

    @mock.patch("app.services.firestore.get_db")
    def test_sync_user_identity_from_auth_updates_allowlist_doc(self, mock_get_db):
        doc = mock.MagicMock()
        doc.exists = True
        doc.to_dict.return_value = _make_user_doc(True, firestore.ROLE_USER)
        doc_ref = mock.MagicMock()
        doc_ref.get.return_value = doc
        mock_get_db.return_value.collection.return_value.document.return_value = doc_ref

        changed = firestore.sync_user_identity_from_auth(
            email="casey.nguyen@northstar.example.com",
            name="Casey Nguyen",
            picture="https://example.com/avatar.png",
            source="firebase",
            uid="uid-1",
        )

        self.assertTrue(changed)
        update = doc_ref.set.call_args.args[0]
        self.assertEqual(update["display_name"], "Casey Nguyen")
        self.assertEqual(update["name"], "Casey Nguyen")
        self.assertEqual(update["picture"], "https://example.com/avatar.png")
        self.assertEqual(update["display_name_source"], "firebase")
        self.assertEqual(update["firebase_uid"], "uid-1")
        self.assertTrue(doc_ref.set.call_args.kwargs["merge"])

    @mock.patch("app.services.firestore.get_db")
    def test_sync_user_identity_from_auth_skips_synthetic_proof_label(self, mock_get_db):
        doc_ref = mock.MagicMock()
        mock_get_db.return_value.collection.return_value.document.return_value = doc_ref

        changed = firestore.sync_user_identity_from_auth(
            email="alex.morgan@northstar.example.com",
            name="ESIP Pilot Proof",
            source="zaf",
        )

        self.assertFalse(changed)
        doc_ref.get.assert_not_called()
        doc_ref.set.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
