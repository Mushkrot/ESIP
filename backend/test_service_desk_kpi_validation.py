#!/usr/bin/env python3
"""Service Desk KPI validation helper tests.

Run from project root: scripts/backend_python.sh backend/test_service_desk_kpi_validation.py
"""

from __future__ import annotations

import unittest

from app.services import service_desk_kpi, service_desk_kpi_validation


class ServiceDeskKpiValidationTest(unittest.TestCase):
    def test_payload_explains_audit_event_solved_metric(self):
        payload = service_desk_kpi_validation.build_validation_payload(
            {
                "date": "2026-05-27",
                "summary": {
                    "new_assigned_tickets": 154,
                    "same_day_solved_tickets": 121,
                    "solved_tickets": 121,
                    "outstanding_tickets": 195,
                },
                "coverage": {"assignee_email_coverage_pct": 100},
                "rows": [],
                "warnings": [],
            }
        )

        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["totals"]["assigned"], 154)
        self.assertEqual(payload["totals"]["solved"], 121)
        self.assertEqual(payload["totals"]["outstanding_now"], 195)
        self.assertNotIn("closed_that_day", payload["totals"])
        solved_def = next(item for item in payload["definitions"] if item["metric"] == "Solved")
        self.assertIn("status changed to Solved", solved_def["definition"])

    def test_manual_comparison_and_synthetic_owner_hiding(self):
        payload = service_desk_kpi_validation.build_validation_payload(
            {
                "date": "2026-05-27",
                "summary": {
                    "new_assigned_tickets": 2,
                    "same_day_solved_tickets": 1,
                    "solved_tickets": 1,
                    "outstanding_tickets": 3,
                },
                "coverage": {"assignee_email_coverage_pct": 89},
                "rows": [
                    {
                        "display_name": service_desk_kpi.BLANK_OWNER_LABEL,
                        "email": service_desk_kpi.BLANK_OWNER_EMAIL,
                        "new_assigned_tickets": 2,
                        "same_day_solved_tickets": 1,
                        "solved_tickets": 1,
                        "outstanding_tickets": 3,
                    }
                ],
                "warnings": [],
            },
            manual_totals={"assigned": 2, "solved": 2, "outstanding": 3},
            top_n=1,
        )

        comparisons = {item["metric"]: item for item in payload["manual_comparison"]}
        self.assertTrue(comparisons["Assigned"]["matches"])
        self.assertFalse(comparisons["Solved"]["matches"])
        self.assertEqual(comparisons["Solved"]["delta"], -1)
        self.assertEqual(payload["top_rows"][0]["email"], "")
        self.assertTrue(any("Owner email coverage" in warning for warning in payload["warnings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
