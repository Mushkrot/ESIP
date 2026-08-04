"""Tests for Firebase Admin initialization helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from app.services import firestore


class FirestoreInitTest(unittest.TestCase):
    def test_ensure_firebase_app_uses_adc_when_credentials_path_missing(self):
        with (
            mock.patch.object(firestore.firebase_admin, "get_app", side_effect=ValueError),
            mock.patch.object(firestore.firebase_admin, "initialize_app") as initialize_app,
            mock.patch.object(firestore.fb_credentials, "Certificate") as certificate,
            mock.patch.object(firestore.settings, "google_application_credentials", ""),
            mock.patch.object(firestore.settings, "firebase_project_id", "esip-reference-project"),
        ):
            firestore._ensure_firebase_app()

        certificate.assert_not_called()
        initialize_app.assert_called_once_with(options={"projectId": "esip-reference-project"})

    def test_ensure_firebase_app_uses_certificate_when_credentials_path_present(self):
        cred = object()

        with (
            mock.patch.object(firestore.firebase_admin, "get_app", side_effect=ValueError),
            mock.patch.object(firestore.firebase_admin, "initialize_app") as initialize_app,
            mock.patch.object(firestore.fb_credentials, "Certificate", return_value=cred) as certificate,
            mock.patch.object(
                firestore.settings,
                "google_application_credentials",
                " /example/config/firebase.json ",
            ),
            mock.patch.object(firestore.settings, "firebase_project_id", "esip-reference-project"),
        ):
            firestore._ensure_firebase_app()

        certificate.assert_called_once_with("/example/config/firebase.json")
        initialize_app.assert_called_once_with(cred, {"projectId": "esip-reference-project"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
