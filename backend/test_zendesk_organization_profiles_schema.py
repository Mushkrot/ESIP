"""Zendesk organization profile BigQuery schema tests.

Run from project root:
scripts/backend_python.sh backend/test_zendesk_organization_profiles_schema.py
"""

from __future__ import annotations

import unittest

from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery

from app.services import bigquery as bq


def _schema_map() -> dict[str, bigquery.SchemaField]:
    return {field.name: field for field in bq.ZENDESK_ORGANIZATION_PROFILES_SCHEMA}


def _table(schema: list[bigquery.SchemaField]) -> bigquery.Table:
    return bigquery.Table(bq.zendesk_organization_profiles_table_id(), schema=schema)


class _FakeBigQueryClient:
    def __init__(self, table: bigquery.Table | None = None, *, missing_first: bool = False):
        self.table = table
        self.missing_first = missing_first
        self.get_table_calls: list[str] = []
        self.created_table: bigquery.Table | None = None
        self.updated_table: bigquery.Table | None = None
        self.update_fields: list[str] | None = None

    def get_table(self, table_id: str) -> bigquery.Table:
        self.get_table_calls.append(table_id)
        if self.missing_first:
            self.missing_first = False
            raise google_exceptions.NotFound("missing")
        if self.table is None:
            raise AssertionError("Fake client has no table to return")
        return self.table

    def create_table(self, table: bigquery.Table) -> bigquery.Table:
        self.created_table = table
        self.table = table
        return table

    def update_table(self, table: bigquery.Table, fields: list[str]) -> bigquery.Table:
        self.updated_table = table
        self.update_fields = fields
        self.table = table
        return table


class ZendeskOrganizationProfilesSchemaTest(unittest.TestCase):
    def test_schema_contains_required_profile_fields(self):
        fields = _schema_map()

        expected = {
            "org_id": ("INT64", "REQUIRED"),
            "org_name": ("STRING", "NULLABLE"),
            "store_id": ("STRING", "NULLABLE"),
            "brand": ("STRING", "NULLABLE"),
            "status": ("STRING", "NULLABLE"),
            "pos_type": ("STRING", "NULLABLE"),
            "simphony_store": ("BOOL", "NULLABLE"),
            "simphony_install_date": ("TIMESTAMP", "NULLABLE"),
            "pos_server_ip": ("STRING", "NULLABLE"),
            "kds": ("BOOL", "NULLABLE"),
            "dmb": ("BOOL", "NULLABLE"),
            "guest_wifi": ("BOOL", "NULLABLE"),
            "phone_type": ("STRING", "NULLABLE"),
            "internet_connection": ("STRING", "NULLABLE"),
            "tags": ("STRING", "REPEATED"),
            "source_updated_at": ("TIMESTAMP", "NULLABLE"),
            "synced_at": ("TIMESTAMP", "NULLABLE"),
            "raw_fields_json": ("STRING", "NULLABLE"),
            "raw_profile_json": ("STRING", "NULLABLE"),
        }

        for name, (field_type, mode) in expected.items():
            self.assertIn(name, fields)
            self.assertEqual(fields[name].field_type, field_type)
            self.assertEqual(fields[name].mode, mode)
        self.assertEqual(fields["pos_type"].field_type, "STRING")

    def test_ensure_creates_missing_profile_table(self):
        client = _FakeBigQueryClient(missing_first=True)

        table = bq.ensure_zendesk_organization_profiles_table(client)

        self.assertIs(table, client.created_table)
        self.assertEqual(client.get_table_calls, [bq.zendesk_organization_profiles_table_id()])
        self.assertEqual(
            {field.name for field in table.schema},
            {field.name for field in bq.ZENDESK_ORGANIZATION_PROFILES_SCHEMA},
        )
        self.assertEqual(table.clustering_fields, ["org_id", "store_id", "pos_type"])

    def test_ensure_existing_complete_table_is_idempotent(self):
        existing = _table(list(bq.ZENDESK_ORGANIZATION_PROFILES_SCHEMA))
        client = _FakeBigQueryClient(existing)

        table = bq.ensure_zendesk_organization_profiles_table(client)

        self.assertIs(table, existing)
        self.assertIsNone(client.created_table)
        self.assertIsNone(client.updated_table)

    def test_ensure_existing_table_adds_missing_nullable_fields(self):
        partial_schema = [
            field
            for field in bq.ZENDESK_ORGANIZATION_PROFILES_SCHEMA
            if field.name in {"org_id", "org_name"}
        ]
        client = _FakeBigQueryClient(_table(partial_schema))

        table = bq.ensure_zendesk_organization_profiles_table(client)

        self.assertIs(table, client.updated_table)
        self.assertEqual(client.update_fields, ["schema"])
        field_names = {field.name for field in table.schema}
        self.assertIn("pos_type", field_names)
        self.assertIn("raw_profile_json", field_names)

    def test_ensure_existing_table_missing_org_id_fails(self):
        existing = _table([bigquery.SchemaField("org_name", "STRING")])
        client = _FakeBigQueryClient(existing)

        with self.assertRaisesRegex(RuntimeError, "org_id"):
            bq.ensure_zendesk_organization_profiles_table(client)

        self.assertIsNone(client.updated_table)

    def test_profile_schema_never_targets_tickets_table(self):
        self.assertTrue(
            bq.zendesk_organization_profiles_table_id().endswith(
                ".zendesk_organization_profiles"
            )
        )
        self.assertNotEqual(
            bq.zendesk_organization_profiles_table_id(),
            f"{bq.GCP_PROJECT}.{bq.BQ_DATASET}.tickets",
        )


if __name__ == "__main__":
    unittest.main()
