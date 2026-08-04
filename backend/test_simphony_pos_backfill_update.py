"""Guardrail tests for the Simphony-only Zendesk POS Type updater.

Run from project root:
scripts/backend_python.sh backend/test_simphony_pos_backfill_update.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import simphony_pos_backfill as backfill

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import update_zendesk_org_pos_type_from_registry as cli


def _candidate(**overrides) -> backfill.SimphonyPosCandidate:
    values = {
        "org_id": 99000000002,
        "org_name": "Metro Burger 2002",
        "org_url": "https://support.example.com/agent/organizations/99000000002",
        "store_id": "2002",
        "brand": "Metro Burger",
        "current_pos_type": "Micros 5.5",
        "registry_pos_system_name": "Simphony 2",
        "registry_pos_type": "Store POS",
        "proposed_pos_type": "Simphony",
        "reason": "pos_type_mismatch",
        "ticket_count": 12,
        "open_ticket_count": 1,
    }
    values.update(overrides)
    return backfill.SimphonyPosCandidate(**values)


class _FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200, headers: dict | None = None):
        self.payload = payload or {"organization": {"id": 99000000002}}
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse] | None = None):
        self.responses = responses or [_FakeResponse()]
        self.put_calls: list[tuple[str, dict, int]] = []
        self.get_calls: list[tuple] = []
        self.patch_calls: list[tuple] = []

    def put(self, url, json=None, timeout=30):
        self.put_calls.append((url, json, timeout))
        if not self.responses:
            raise AssertionError("No fake responses left")
        return self.responses.pop(0)

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("GET should not be used by updater")

    def patch(self, *args, **kwargs):
        self.patch_calls.append((args, kwargs))
        raise AssertionError("PATCH should not be used by updater")


class SimphonyPosBackfillUpdateTest(unittest.TestCase):
    def test_update_payload_sets_only_org_pos_type_on_organization_endpoint(self):
        session = _FakeSession()

        backfill.update_zendesk_org_pos_type(
            session=session,
            zendesk_base_url="https://support.example.com/api/v2",
            candidate=_candidate(),
            sleep_fn=lambda _: None,
        )

        self.assertEqual(len(session.put_calls), 1)
        url, payload, timeout = session.put_calls[0]
        self.assertEqual(
            url,
            "https://support.example.com/api/v2/organizations/99000000002.json",
        )
        self.assertNotIn("/tickets", url)
        self.assertNotIn("/comments", url)
        self.assertEqual(timeout, 30)
        self.assertEqual(
            payload,
            {"organization": {"organization_fields": {"pos_type": "Simphony"}}},
        )
        self.assertEqual(session.get_calls, [])
        self.assertEqual(session.patch_calls, [])

    def test_manifest_validation_refuses_out_of_scope_rows(self):
        bad_proposed = _candidate(proposed_pos_type="Micros")
        bad_source = _candidate(registry_pos_system_name="Micros")
        bad_reason = _candidate(reason="manual_note")
        already = _candidate(current_pos_type="Simphony")

        for item in (bad_proposed, bad_source, bad_reason, already):
            with self.assertRaises(backfill.ManifestValidationError):
                backfill.validate_candidate_for_apply(item)

    def test_update_retries_429_and_5xx_before_success(self):
        session = _FakeSession(
            [
                _FakeResponse(status_code=429, headers={"Retry-After": "1"}),
                _FakeResponse(status_code=500),
                _FakeResponse(),
            ]
        )
        sleeps: list[float] = []

        backfill.update_zendesk_org_pos_type(
            session=session,
            zendesk_base_url="https://support.example.com/api/v2",
            candidate=_candidate(),
            sleep_fn=sleeps.append,
        )

        self.assertEqual(len(session.put_calls), 3)
        self.assertEqual(sleeps, [1, backfill.ZENDESK_UPDATE_BACKOFF_SECONDS * 2])

    def test_cli_dry_run_builds_manifest_and_never_opens_zendesk_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            registry_csv = tmp / "registry.csv"
            profiles_csv = tmp / "profiles.csv"
            output_csv = tmp / "candidates.csv"
            with registry_csv.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["SourceLocationID", "POSType", "POSSystemName"])
                writer.writeheader()
                writer.writerow(
                    {
                        "SourceLocationID": "2002",
                        "POSType": "Store POS",
                        "POSSystemName": "Simphony 2",
                    }
                )
            with profiles_csv.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "org_id",
                        "org_name",
                        "store_id",
                        "brand",
                        "pos_type",
                        "simphony_store",
                        "ticket_count",
                        "open_ticket_count",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "org_id": "99000000002",
                        "org_name": "Metro Burger 2002",
                        "store_id": "2002",
                        "brand": "Metro Burger",
                        "pos_type": "Micros 5.5",
                        "simphony_store": "false",
                        "ticket_count": "12",
                        "open_ticket_count": "1",
                    }
                )

            stdout = io.StringIO()
            with (
                mock.patch.object(cli.zendesk_organization_profiles, "_session") as session_factory,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--dry-run",
                        "--registry-csv",
                        str(registry_csv),
                        "--profiles-csv",
                        str(profiles_csv),
                        "--output",
                        str(output_csv),
                    ]
                )

            self.assertEqual(exit_code, 0)
            session_factory.assert_not_called()
            self.assertIn("NO_WRITE dry run: Zendesk organizations were not modified.", stdout.getvalue())
            with output_csv.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["proposed_pos_type"], "Simphony")
            self.assertEqual(rows[0]["reason"], "pos_type_mismatch")

    def test_cli_apply_refuses_without_reviewed_manifest_path(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = cli.main(["--apply"])

        self.assertEqual(exit_code, 2)
        self.assertIn("REFUSED apply requires --reviewed-manifest PATH", stderr.getvalue())

    def test_cli_apply_uses_reviewed_manifest_and_mocked_org_put_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "reviewed.csv"
            backfill.write_candidate_csv(manifest, [_candidate()])
            fake_session = _FakeSession()
            stdout = io.StringIO()
            with (
                mock.patch.object(cli.zendesk_organization_profiles, "_session", return_value=fake_session),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cli.main(
                    [
                        "--apply",
                        "--reviewed-manifest",
                        str(manifest),
                        "--confirm-reviewed-manifest",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(fake_session.put_calls), 1)
        url, payload, _timeout = fake_session.put_calls[0]
        self.assertTrue(url.endswith("/organizations/99000000002.json"))
        self.assertEqual(
            payload,
            {"organization": {"organization_fields": {"pos_type": "Simphony"}}},
        )
        self.assertIn("WROTE_ZENDESK organizations=1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
