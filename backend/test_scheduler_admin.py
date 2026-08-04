"""Tests for Cloud Scheduler admin helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from app.services import scheduler_admin


class SchedulerAdminAuthTest(unittest.TestCase):
    def test_cloud_run_token_uses_runtime_identity(self):
        class FakeCredentials:
            token = "runtime-token"

            def refresh(self, _request):
                self.token = "runtime-token"

        with mock.patch.dict("os.environ", {"K_SERVICE": "esip-backend"}):
            with mock.patch("google.auth.compute_engine.Credentials", return_value=FakeCredentials()) as runtime_creds:
                with mock.patch("google.oauth2.service_account.Credentials.from_service_account_file") as file_creds:
                    token = scheduler_admin._token()

        self.assertEqual(token, "runtime-token")
        runtime_creds.assert_called_once_with(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        file_creds.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
