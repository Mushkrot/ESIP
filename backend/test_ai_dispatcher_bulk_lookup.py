"""AI dispatcher bounded bulk ticket lookup tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_bulk_lookup.py
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import bigquery


class _Row:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def result(self, timeout=None):
        return self._rows


class _FakeClient:
    def __init__(self, rows=None, exc: Exception | None = None):
        self.rows = rows or []
        self.exc = exc
        self.calls: list[dict] = []

    def query(self, sql, job_config=None):
        self.calls.append({"sql": sql, "job_config": job_config})
        if self.exc:
            raise self.exc
        return _Result(self.rows)


class DispatcherBulkLookupTest(unittest.TestCase):
    def test_bulk_lookup_is_parameterized_bounded_and_reports_missing_ids(self):
        rows = [
            _Row(
                id=950053,
                subject="Internet connection interrupted",
                status="solved",
                org_name="Store 2003",
                brand="Metro Burger",
                store_id="2003",
                issue_category="infrastructure_issue__internet_connection__primary",
                created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
                solved_at=None,
                resolution="Rebooted the Telecom Partner modem and confirmed the connection recovered.",
                evidence_snippet="Internet connection interrupted at store.",
                full_text_len=1200,
            ),
            _Row(
                id=950027,
                subject="Network slow",
                status="closed",
                org_name="Store 2004",
                brand="Cedar Grill",
                store_id="2004",
                issue_category="network__speed",
                created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                solved_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
                resolution="Replaced failing network cable.",
                evidence_snippet="Network slow and service drops.",
                full_text_len=900,
            ),
        ]
        fake_client = _FakeClient(rows)

        with patch("app.services.bigquery._get_client", return_value=fake_client):
            result = bigquery.bulk_lookup_ticket_ids([950053, 950058, 950027, 950053], cap=3)

        call = fake_client.calls[0]
        params = call["job_config"].query_parameters
        self.assertIn("UNNEST(@ticket_ids)", call["sql"])
        self.assertEqual(params[0].name, "ticket_ids")
        self.assertEqual(params[0].values, [950053, 950058, 950027])
        self.assertEqual(result["requested_ids"], [950053, 950058, 950027])
        self.assertEqual(result["found_ids"], [950053, 950027])
        self.assertEqual(result["missing_ids"], [950058])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["rows"][0]["ticket_id"], 950053)
        self.assertEqual(result["rows"][0]["resolution"], "Rebooted the Telecom Partner modem and confirmed the connection recovered.")
        self.assertNotIn("full_text", result["rows"][0])
        self.assertIn("parameterized=true", result["bq_summary"])

    def test_bulk_lookup_caps_ids_before_query(self):
        fake_client = _FakeClient([])

        with patch("app.services.bigquery._get_client", return_value=fake_client):
            result = bigquery.bulk_lookup_ticket_ids([950044 + i for i in range(80)], cap=75)

        params = fake_client.calls[0]["job_config"].query_parameters
        self.assertEqual(len(params[0].values), 75)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["missing_ids"]), 75)

    def test_bulk_lookup_does_not_convert_query_errors_to_no_data(self):
        fake_client = _FakeClient(exc=RuntimeError("BQ unavailable"))

        with patch("app.services.bigquery._get_client", return_value=fake_client):
            with self.assertRaisesRegex(RuntimeError, "BQ unavailable"):
                bigquery.bulk_lookup_ticket_ids([950053])


if __name__ == "__main__":
    unittest.main()
