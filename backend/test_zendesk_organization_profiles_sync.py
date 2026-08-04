"""Zendesk organization profile sync tests.

Run from project root:
scripts/backend_python.sh backend/test_zendesk_organization_profiles_sync.py
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from app.services import zendesk_organization_profiles as profiles


def _org(
    org_id: int,
    *,
    name: str = "Metro Burger 2002",
    fields: dict | None = None,
    tags: list[str] | None = None,
    updated_at: str = "2026-06-08T12:30:00Z",
) -> dict:
    return {
        "id": org_id,
        "name": name,
        "organization_fields": fields or {},
        "tags": tags or [],
        "updated_at": updated_at,
        "suspended": False,
    }


class _FakeJob:
    def result(self, timeout=None):
        return []


class _FakeBigQueryClient:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []
        self.inserts: list[tuple[str, list[dict]]] = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return _FakeJob()

    def insert_rows_json(self, table, rows):
        self.inserts.append((table, list(rows)))
        return []


class _FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.calls: list[tuple[str, dict | None, int]] = []

    def get(self, url, params=None, timeout=30):
        self.calls.append((url, params, timeout))
        if not self.pages:
            raise AssertionError("No fake Zendesk pages left")
        return _FakeResponse(self.pages.pop(0))


class ZendeskOrganizationProfilesSyncTest(unittest.TestCase):
    def test_parser_normalizes_pos_type_but_preserves_raw_fields(self):
        synced_at = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
        row = profiles.build_profile_row(
            _org(
                99000000001,
                fields={
                    "pos_type": "  simphony ",
                    "simphony_store": "true",
                    "simphony_install_date": "2026-05-01",
                    "pos_server_ip": " 192.0.2.61 ",
                    "kds": True,
                    "dmb": "false",
                    "guest_wifi": "yes",
                    "phone_type": " VOIP ",
                    "internet_connection": "Centralized VOI",
                    "store_id": "2002",
                },
                tags=["Metro Burger", "simphony_store"],
            ),
            synced_at=synced_at,
        )

        self.assertEqual(row["org_id"], 99000000001)
        self.assertEqual(row["store_id"], "2002")
        self.assertEqual(row["brand"], "Metro Burger")
        self.assertEqual(row["pos_type"], "Simphony")
        self.assertTrue(row["simphony_store"])
        self.assertTrue(row["kds"])
        self.assertFalse(row["dmb"])
        self.assertTrue(row["guest_wifi"])
        self.assertEqual(row["pos_server_ip"], "192.0.2.61")
        self.assertEqual(row["phone_type"], "VOIP")
        self.assertEqual(row["simphony_install_date"], "2026-05-01T00:00:00+00:00")
        self.assertIn('"pos_type": "  simphony "', row["raw_fields_json"])

    def test_parser_handles_blank_and_malformed_fields(self):
        row = profiles.build_profile_row(
            _org(
                1,
                name="Unknown Org 9999",
                fields={
                    "pos_type": " ",
                    "simphony_store": "not sure",
                    "simphony_install_date": "not-a-date",
                    "guest_wifi": "",
                },
                tags=[],
                updated_at="broken",
            )
        )

        self.assertEqual(row["store_id"], "9999")
        self.assertIsNone(row["pos_type"])
        self.assertIsNone(row["simphony_store"])
        self.assertIsNone(row["simphony_install_date"])
        self.assertIsNone(row["guest_wifi"])
        self.assertIsNone(row["source_updated_at"])
        self.assertEqual(row["status"], "active")

    def test_dry_run_does_not_touch_bigquery(self):
        organizations = [
            _org(1, fields={"pos_type": "Simphony", "simphony_store": True}),
            _org(2, fields={"pos_type": "Micros 5.5"}),
        ]

        with (
            mock.patch.object(profiles, "upsert_profile_rows") as upsert,
            mock.patch.object(profiles.bq_service, "_get_client") as get_client,
        ):
            result = profiles.sync_zendesk_organization_profiles(
                dry_run=True,
                organizations=organizations,
            )

        upsert.assert_not_called()
        get_client.assert_not_called()
        self.assertFalse(result.write_executed)
        self.assertEqual(result.prepared_count, 2)
        self.assertEqual(result.pos_type_distribution, {"Micros 5.5": 1, "Simphony": 1})
        self.assertEqual(result.simphony_store_count, 1)
        self.assertEqual(len(result.simphony_samples), 1)
        self.assertEqual(result.simphony_samples[0]["org_id"], 1)

    def test_iter_zendesk_organizations_supports_limited_sample_pagination(self):
        session = _FakeSession([
            {
                "organizations": [_org(1)],
                "next_page": "https://support.example.com/api/v2/organizations.json?page=2",
            },
            {
                "organizations": [_org(2), _org(3)],
                "next_page": None,
            },
        ])

        orgs = list(profiles.iter_zendesk_organizations(limit=2, page_size=1, session=session))

        self.assertEqual([org["id"] for org in orgs], [1, 2])
        self.assertEqual(session.calls[0][1], {"per_page": 1})
        self.assertIsNone(session.calls[1][1])

    def test_apply_upsert_is_idempotent_by_org_id_and_targets_only_profile_table(self):
        client = _FakeBigQueryClient()
        rows = [
            profiles.build_profile_row(_org(1, fields={"pos_type": "simphony"})),
            profiles.build_profile_row(_org(1, fields={"pos_type": "micros 5.5"})),
        ]

        with mock.patch.object(profiles.bq_service, "ensure_zendesk_organization_profiles_table") as ensure:
            written = profiles.upsert_profile_rows(client, rows)

        ensure.assert_called_once_with(client)
        self.assertEqual(written, 1)
        self.assertEqual(len(client.queries), 1)
        self.assertIn("zendesk_organization_profiles", client.queries[0][0])
        self.assertNotIn(".tickets", client.queries[0][0])
        self.assertEqual(len(client.inserts), 1)
        target_table, inserted_rows = client.inserts[0]
        self.assertTrue(target_table.endswith(".zendesk_organization_profiles"))
        self.assertEqual(len(inserted_rows), 1)
        self.assertEqual(inserted_rows[0]["org_id"], 1)
        self.assertEqual(inserted_rows[0]["pos_type"], "Micros 5.5")


if __name__ == "__main__":
    unittest.main()
