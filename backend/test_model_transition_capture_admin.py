"""Unit tests for the default-off exact request capture control."""

from __future__ import annotations

import unittest
from unittest import mock

from app.config import settings
from app.services import model_transition_capture_admin as capture_admin


class CaptureConfigTests(unittest.TestCase):
    def tearDown(self):
        capture_admin.reset_runtime_cache()

    def test_default_is_disabled_and_has_no_users(self):
        config = capture_admin.default_config()
        self.assertFalse(config["enabled"])
        self.assertEqual(config["allowed_emails"], [])

    def test_reference_runtime_does_not_open_firestore(self):
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch.object(
                capture_admin.firestore,
                "get_db",
                side_effect=AssertionError("reference runtime must not contact Firestore"),
            ),
        ):
            config = capture_admin.read_config()

        self.assertFalse(config["enabled"])
        self.assertEqual(config["allowed_emails"], [])

    def test_reference_runtime_keeps_admin_update_in_process(self):
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch.object(
                capture_admin.firestore,
                "get_db",
                side_effect=AssertionError("reference runtime must not contact Firestore"),
            ),
        ):
            updated = capture_admin.update_config(
                {"enabled": True, "allowed_emails": ["riley.brooks@northstar.example.com"]},
                actor_email="morgan.chen@northstar.example.com",
            )
            reread = capture_admin.read_config()

        self.assertTrue(updated["enabled"])
        self.assertEqual(reread["allowed_emails"], ["riley.brooks@northstar.example.com"])

    def test_enabled_requires_named_corporate_user(self):
        with self.assertRaisesRegex(capture_admin.CaptureConfigValidationError, "at least one"):
            capture_admin.validate_config_patch({"enabled": True, "allowed_emails": []})
        with self.assertRaisesRegex(capture_admin.CaptureConfigValidationError, "northstar.example.com"):
            capture_admin.validate_config_patch({"enabled": False, "allowed_emails": ["vendor.user@external.example.org"]})

    def test_valid_patch_normalizes_and_deduplicates_emails(self):
        result = capture_admin.validate_config_patch(
            {"enabled": True, "allowed_emails": [" riley.brooks@northstar.example.com ", "riley.brooks@northstar.example.com"]},
            actor_email="morgan.chen@northstar.example.com",
        )
        self.assertTrue(result["enabled"])
        self.assertEqual(result["allowed_emails"], ["riley.brooks@northstar.example.com"])
        self.assertEqual(result["updated_by"], "morgan.chen@northstar.example.com")

    def test_malformed_persisted_config_fails_closed(self):
        normalized = capture_admin._normalize({"enabled": True, "allowed_emails": ["not-an-email"]})
        self.assertFalse(normalized["enabled"])
        self.assertEqual(normalized["allowed_emails"], [])

    def test_firestore_read_failure_fails_closed(self):
        class BrokenDb:
            def collection(self, _name):
                raise RuntimeError("Firestore unavailable")

        config = capture_admin.get_runtime_config(db=BrokenDb(), now=10.0)
        self.assertFalse(config["enabled"])
        self.assertEqual(config["allowed_emails"], [])

    def test_cache_does_not_repeatedly_read_before_expiry(self):
        class Db:
            def __init__(self):
                self.calls = 0
            def collection(self, _name):
                return self
            def document(self, _name):
                return self
            def get(self):
                self.calls += 1
                return type("Snapshot", (), {"exists": True, "to_dict": lambda _self: {"enabled": False, "allowed_emails": []}})()

        db = Db()
        first = capture_admin.get_runtime_config(db=db, now=10.0)
        second = capture_admin.get_runtime_config(db=db, now=20.0)
        self.assertFalse(first["enabled"])
        self.assertEqual(first, second)
        self.assertEqual(db.calls, 1)


if __name__ == "__main__":
    unittest.main()
