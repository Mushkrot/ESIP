"""
ESIP Usage report export tests.
Run from project root: scripts/backend_python.sh backend/test_usage_insights_export.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.services import firestore, usage_insights_export


def _sample_usage_dashboard() -> dict:
    return {
        "range": {
            "mode": "custom",
            "start_at": "2026-06-01T00:00:00+00:00",
            "end_at": "2026-06-07T23:59:59+00:00",
            "include_translation_turns": False,
            "user_email": None,
            "question_type": None,
        },
        "controls": {
            "compare_previous": True,
            "group_by": "user",
            "min_activity": 0,
            "problem_signal": "all",
            "category_focus": "all",
            "technical_quality_enabled": False,
        },
        "summary": {
            "total_queries_to_date": 125,
            "total_queries_in_range": 42,
            "active_users_in_range": 6,
            "avg_queries_per_active_user": 7,
            "weak_or_no_answer_turns": 0,
            "no_source_turns": 3,
            "top_question_type": "Document lookup",
            "usage_by_role": [{"role": "admin", "count": 42}],
        },
        "comparison": None,
        "users": [
            {
                "email": "alex.morgan@northstar.example.com",
                "display_name": "Agent One",
                "role": "admin",
                "active": True,
                "first_seen_at": "2026-05-26T12:00:00+00:00",
                "last_active_at": "2026-06-07T15:30:00+00:00",
                "queries_to_date": 50,
                "queries_in_range": 12,
                "avg_latency_ms": 4500,
                "weak_or_no_answer": 0,
                "no_source_count": 1,
                "evidence_rate_pct": 91.7,
                "documentation_opportunities": 1,
                "active_days": 4,
                "top_question_type": "Document lookup",
            }
        ],
        "users_unfiltered_count": 1,
        "question_types": [
            {
                "question_type": "Document lookup",
                "count": 20,
                "users": 4,
                "weak_or_no_answer": 0,
                "no_source_count": 2,
                "evidence_rate_pct": 90,
            }
        ],
        "timeseries": [
            {"date": "2026-06-01", "queries": 8, "active_users": 3, "weak_or_no_answer": 0, "no_source": 1},
            {"date": "2026-06-02", "queries": 7, "active_users": 2, "weak_or_no_answer": 0, "no_source": 0},
        ],
        "grouped_breakdown": [],
        "source_coverage": {
            "total_turns": 42,
            "with_sources": 35,
            "with_tickets": 9,
            "with_analytics": 4,
            "with_evidence": 39,
            "without_sources": 3,
            "source_coverage_pct": 83.3,
            "evidence_coverage_pct": 92.9,
            "ticket_usage_pct": 21.4,
        },
        "document_gaps": [
            {
                "theme": "Document lookup",
                "question_type": "Document lookup",
                "count": 2,
                "users_affected": 2,
                "no_source_count": 2,
                "weak_or_no_answer": 0,
                "example_question": "",
                "suggested_action": "Review source coverage for this topic.",
            }
        ],
        "filters": {"users": [], "question_types": []},
        "notes": ["Super Admin usage is excluded from management Usage Insights."],
    }


class UsageInsightsExportTest(unittest.TestCase):
    def test_excel_export_has_period_title_and_management_sheets(self):
        content = usage_insights_export.build_excel_export(_sample_usage_dashboard())

        wb = load_workbook(BytesIO(content))
        self.assertEqual(wb.sheetnames, ["ESIP Usage Report", "Users", "Details"])
        ws = wb["ESIP Usage Report"]
        self.assertEqual(ws["A1"].value, "ESIP Usage Report for June 1, 2026 - June 7, 2026")
        self.assertEqual(ws["B2"].value, "June 1, 2026 - June 7, 2026")
        self.assertEqual(ws["A6"].value, "Queries to date")
        details = wb["Details"]
        detail_values = [cell.value for row in details.iter_rows() for cell in row if cell.value]
        self.assertIn("Question types", detail_values)
        self.assertIn("Daily trend", detail_values)
        self.assertIn("Documentation opportunities", detail_values)

    def test_excel_export_uses_compact_preformatted_column_widths(self):
        content = usage_insights_export.build_excel_export(_sample_usage_dashboard())

        wb = load_workbook(BytesIO(content))
        summary = wb["ESIP Usage Report"]
        users = wb["Users"]
        details = wb["Details"]

        self.assertEqual(summary.max_column, 3)
        self.assertEqual(users.max_column, 11)
        self.assertEqual(details.max_column, 6)
        self.assertEqual(list(summary.merged_cells.ranges), [])
        self.assertEqual(list(users.merged_cells.ranges), [])
        self.assertEqual(list(details.merged_cells.ranges), [])

        self.assertLess(summary.column_dimensions["B"].width, 40)
        self.assertLess(summary.column_dimensions["C"].width, 50)
        self.assertLess(users.column_dimensions["H"].width, 20)
        self.assertLess(details.column_dimensions["B"].width, 24)
        self.assertLess(details.column_dimensions["F"].width, 48)

    def test_get_export_file_reuses_usage_dashboard_definitions(self):
        with mock.patch(
            "app.services.usage_insights_export.usage_insights.get_usage_dashboard",
            return_value=_sample_usage_dashboard(),
        ) as get_usage_dashboard:
            export = usage_insights_export.get_export_file(
                range_mode="custom",
                start_date="2026-06-01",
                end_date="2026-06-07",
                file_format="xlsx",
            )

        self.assertEqual(export.filename, "esip-usage-2026-06-01_to_2026-06-07.xlsx")
        self.assertEqual(export.media_type, usage_insights_export.XLSX_MEDIA_TYPE)
        get_usage_dashboard.assert_called_once()
        _, kwargs = get_usage_dashboard.call_args
        self.assertEqual(kwargs["range_mode"], "custom")
        self.assertEqual(kwargs["start_date"], "2026-06-01")
        self.assertEqual(kwargs["end_date"], "2026-06-07")
        self.assertFalse(kwargs["include_translation_turns"])
        self.assertFalse(kwargs["include_super_admin_usage"])

    def test_get_export_file_rejects_pdf_format(self):
        with mock.patch(
            "app.services.usage_insights_export.usage_insights.get_usage_dashboard",
            return_value=_sample_usage_dashboard(),
        ):
            with self.assertRaisesRegex(ValueError, "Excel only"):
                usage_insights_export.get_export_file(file_format="pdf")


class UsageInsightsExportEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self, role: str = firestore.ROLE_ADMIN):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={"uid": "admin", "email": "priya.shah@northstar.example.com", "name": "Admin"},
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
        ):
            yield

    def test_admin_can_download_usage_export(self):
        export = usage_insights_export.UsageExportFile(
            content=b"fake-xlsx",
            filename="esip-usage-2026-06-01_to_2026-06-07.xlsx",
            media_type=usage_insights_export.XLSX_MEDIA_TYPE,
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.usage_insights_export.get_export_file", return_value=export) as get_export_file,
        ):
            res = self.client.get(
                "/api/admin/usage/export?range=custom&start_date=2026-06-01&end_date=2026-06-07&format=xlsx",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"fake-xlsx")
        self.assertEqual(res.headers["content-type"], usage_insights_export.XLSX_MEDIA_TYPE)
        self.assertIn("esip-usage-2026-06-01_to_2026-06-07.xlsx", res.headers["content-disposition"])
        _, kwargs = get_export_file.call_args
        self.assertEqual(kwargs["start_date"], "2026-06-01")
        self.assertEqual(kwargs["end_date"], "2026-06-07")
        self.assertFalse(kwargs["include_super_admin_usage"])

    def test_pdf_usage_export_format_returns_400(self):
        with (
            self._auth_patches(),
            mock.patch(
                "app.routers.admin.usage_insights_export.get_export_file",
                side_effect=ValueError("ESIP Usage export supports Excel only"),
            ),
        ):
            res = self.client.get(
                "/api/admin/usage/export?start_date=2026-06-01&end_date=2026-06-07&format=pdf",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["detail"], "ESIP Usage export supports Excel only")

    def test_invalid_usage_export_format_returns_400(self):
        with (
            self._auth_patches(),
            mock.patch(
                "app.routers.admin.usage_insights_export.get_export_file",
                side_effect=ValueError("ESIP Usage export supports Excel only"),
            ),
        ):
            res = self.client.get(
                "/api/admin/usage/export?start_date=2026-06-01&end_date=2026-06-07&format=csv",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
