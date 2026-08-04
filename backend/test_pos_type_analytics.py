"""POS Type analytics helper tests.

Run from project root:
scripts/backend_python.sh backend/test_pos_type_analytics.py
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from google.api_core import exceptions as google_exceptions

from app.services import pos_type_analytics


class _FakeJob:
    def __init__(self, rows=None, exc: Exception | None = None):
        self.rows = rows or []
        self.exc = exc

    def result(self):
        if self.exc:
            raise self.exc
        return self.rows


class _FakeBigQueryClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries: list[tuple[str, object]] = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        if not self.responses:
            raise AssertionError("No fake BigQuery responses left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            return _FakeJob(exc=response)
        return _FakeJob(response)


def _param_value(job_config, name: str):
    params = job_config.query_parameters if hasattr(job_config, "query_parameters") else job_config
    for param in params:
        if param.name == name:
            return getattr(param, "value", getattr(param, "values", None))
    raise AssertionError(f"Missing query parameter {name}")


class PosTypeAnalyticsTest(unittest.TestCase):
    def test_generated_sql_uses_profile_pos_type_and_org_id_join(self):
        queries = pos_type_analytics.build_pos_type_analytics_queries(
            "Simphony",
            start_date="2026-06-01",
            end_date="2026-06-15",
            brand="City Fries",
        )
        combined_sql = "\n".join(sql for sql, _ in queries.values())

        self.assertIn("store_profiles.pos_type", combined_sql)
        self.assertIn("t.org_id = store_profiles.org_id", combined_sql)
        self.assertIn("store_id_fallback", combined_sql)
        self.assertNotIn("t.pos_type", combined_sql)
        self.assertIn("DATE(t.created_at, 'America/New_York') >= @start_date", combined_sql)
        self.assertIn("DATE(t.created_at, 'America/New_York') <= @end_date", combined_sql)
        self.assertNotIn("LOWER(COALESCE(t.status, '')) IN UNNEST(@status_filter)", combined_sql)
        self.assertIn("REGEXP_REPLACE(LOWER(COALESCE(store_profiles.brand, '')), r'[^a-z0-9]', '')", combined_sql)

    def test_arbitrary_pos_type_is_parameterized_not_hardcoded(self):
        queries = pos_type_analytics.build_pos_type_analytics_queries("Micros 5.5")
        _, params = queries["coverage"]

        self.assertEqual(_param_value(params, "pos_type"), "Micros 5.5")
        self.assertNotIn("Micros 5.5", queries["coverage"][0])

    def test_status_filter_is_parameterized(self):
        queries = pos_type_analytics.build_pos_type_analytics_queries(
            "Simphony",
            status_filter=("open", "pending"),
        )
        sql, job_config = queries["coverage"]

        self.assertIn("@status_filter", sql)
        self.assertIn("LOWER(COALESCE(t.status, '')) IN UNNEST(@status_filter)", sql)
        self.assertEqual(_param_value(job_config, "status_filter"), ["open", "pending"])

    def test_structured_result_includes_coverage_stores_problems_and_open_tickets(self):
        client = _FakeBigQueryClient([
            [{
                "orgs_matched": 2,
                "tickets_matched": 4,
                "org_id_join_ticket_count": 3,
                "store_id_fallback_ticket_count": 1,
                "profile_orgs_without_tickets_count": 0,
            }],
            [{
                "org_id": 99000000002,
                "org_name": "Metro Burger 2002",
                "store_id": "2002",
                "brand": "Metro Burger",
                "ticket_count": 3,
                "open_ticket_count": 1,
                "recent_ticket_count": 2,
                "sample_tickets": [
                    {
                        "id": 950046,
                        "subject": "POS offline",
                        "status": "solved",
                        "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                    }
                ],
            }],
            [{
                "issue_category": "applications__pos",
                "product_type": "pos",
                "caused_by": "rootcause_hardware",
                "ticket_count": 4,
                "store_count": 2,
                "open_ticket_count": 1,
                "subject_samples": [{"id": 950046, "subject": "POS offline"}],
            }],
            [{
                "id": 950047,
                "subject": "POS down again",
                "status": "open",
                "org_id": 99000000002,
                "org_name": "Metro Burger 2002",
                "store_id": "2002",
                "brand": "Metro Burger",
                "issue_category": "applications__pos",
                "product_type": "pos",
                "caused_by": "unknown",
                "created_at": datetime(2026, 6, 8, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 6, 9, tzinfo=timezone.utc),
                "match_method": "org_id",
            }],
        ])

        result = pos_type_analytics.get_pos_type_analytics(
            "Simphony",
            start_date="2026-06-01",
            end_date="2026-06-15",
            status_filter=("open", "pending"),
            client=client,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["coverage"]["orgs_matched"], 2)
        self.assertEqual(result["filters"]["status_filter"], ("open", "pending"))
        self.assertEqual(result["coverage"]["store_id_fallback_ticket_count"], 1)
        self.assertEqual(result["coverage"]["date_window"]["timezone"], "America/New_York")
        self.assertEqual(result["repeated_stores"][0]["ticket_count"], 3)
        self.assertEqual(result["repeated_problems"][0]["issue_category"], "applications__pos")
        self.assertEqual(result["open_tickets"][0]["match_method"], "org_id")
        self.assertEqual(result["open_tickets"][0]["created_at"], "2026-06-08T00:00:00+00:00")

    def test_missing_profile_table_returns_unavailable_result(self):
        client = _FakeBigQueryClient([google_exceptions.NotFound("missing")])

        result = pos_type_analytics.get_pos_type_analytics("Simphony", client=client)

        self.assertFalse(result["available"])
        self.assertIn("organization profile table is not available", result["warning"])
        self.assertEqual(result["coverage"]["tickets_matched"], 0)

    def test_comparison_groups_arbitrary_pos_types(self):
        client = _FakeBigQueryClient([
            [
                {
                    "pos_type": "Simphony",
                    "issue_category": "applications__pos",
                    "product_type": "pos",
                    "caused_by": "unknown",
                    "ticket_count": 4,
                    "store_count": 2,
                    "open_ticket_count": 1,
                    "subject_samples": [],
                },
                {
                    "pos_type": "Micros 5.5",
                    "issue_category": "network",
                    "product_type": "pos",
                    "caused_by": "unknown",
                    "ticket_count": 2,
                    "store_count": 1,
                    "open_ticket_count": 0,
                    "subject_samples": [],
                },
            ]
        ])

        result = pos_type_analytics.get_pos_type_comparison(
            ["Simphony", "Micros 5.5"],
            start_date="2026-06-01",
            client=client,
        )

        self.assertEqual(set(result["problems_by_pos_type"]), {"Simphony", "Micros 5.5"})
        self.assertEqual(result["problems_by_pos_type"]["Simphony"][0]["ticket_count"], 4)
        sql, job_config = client.queries[0]
        self.assertIn("store_profiles.pos_type", sql)
        self.assertNotIn("t.pos_type", sql)
        self.assertEqual(_param_value(job_config, "pos_type_keys"), ["simphony", "micros 5.5"])


if __name__ == "__main__":
    unittest.main()
