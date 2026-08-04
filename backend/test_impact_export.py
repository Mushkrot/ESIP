"""
ESIP Impact export tests.
Run from project root: scripts/backend_python.sh backend/test_impact_export.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.services import firestore, impact_export


def _sample_impact_report() -> dict:
    return {
        "range": {
            "rollout_date": "2026-05-26",
            "as_of_date": "2026-06-12",
            "display_period": "post",
            "baseline": {"label": "Baseline", "start_date": "2026-04-28", "end_date": "2026-05-26"},
            "ramp": {"label": "Ramp-up", "start_date": "2026-05-26", "end_date": "2026-06-02"},
            "post": {"label": "Stable post-ramp", "start_date": "2026-06-02", "end_date": "2026-06-12"},
        },
        "summary": {
            "display_period": "post",
            "active_users_post": 17,
            "repeat_users_post": 16,
            "esip_assisted_tickets_post": 1169,
            "median_reply_latency_post_hours": 1.4,
            "solved_tickets_post": 1687,
            "outstanding_tickets": 0,
            "cautious_label": "Early signal",
        },
        "report": {
            "title": "ESIP Early Impact Report",
            "subtitle": "Cautious before/ramp/after view for leadership",
            "headline": "ESIP shows early operational impact signals after the ramp-up window.",
            "generated_at": "2026-06-12T21:10:43-04:00",
            "kpis": [
                {"label": "Active users after ramp", "value": 17, "detail": "16 repeat users"},
                {"label": "ESIP-assisted tickets", "value": 1169, "detail": "Tickets with a ESIP touch after ramp"},
                {"label": "Median reply latency", "value": 1.4, "suffix": "h", "detail": "+40.0% vs baseline"},
                {"label": "Solved tickets after ramp", "value": 1687, "detail": "0 currently outstanding"},
            ],
        },
        "adoption": {
            "periods": {
                "baseline": {"events": 2300, "active_users": 1, "assisted_tickets": 1},
                "ramp": {"events": 3700, "active_users": 9, "assisted_tickets": 700},
                "post": {"events": 6965, "active_users": 17, "assisted_tickets": 1169},
            },
            "workflows": [
                {"period": "post", "workflow": "Zendesk app", "events": 3300, "users": 12, "tickets": 900},
                {"period": "post", "workflow": "Customer Risk", "events": 3100, "users": 9, "tickets": 250},
            ],
        },
        "response_speed": {
            "periods": {
                "baseline": {"replies": 100, "median_latency_hours": 1.0},
                "ramp": {"replies": 120, "median_latency_hours": 1.2},
                "post": {"replies": 140, "median_latency_hours": 1.4},
                "headline_delta": {"label": "+40.0% vs baseline"},
            }
        },
        "outcomes": {
            "periods": {
                "baseline": {"solved_tickets": 1000, "median_resolution_hours": 12.0},
                "ramp": {"solved_tickets": 600, "median_resolution_hours": 11.0},
                "post": {"solved_tickets": 1687, "median_resolution_hours": 10.0},
            },
            "headline_solved_delta": {"label": "0 currently outstanding"},
        },
        "quality": {
            "periods": {
                "baseline": {"documentation_gap_signals": 0},
                "ramp": {"documentation_gap_signals": 2},
                "post": {"documentation_gap_signals": 6},
            }
        },
        "users": [
            {
                "key": "alex.morgan@northstar.example.com",
                "email": "alex.morgan@northstar.example.com",
                "display_name": "Agent One",
                "confidence": "Enough data",
                "confidence_reason": "Stable usage",
                "usage": {
                    "post": {"events": 20, "active_days": 4, "assisted_tickets": 8},
                },
                "replies": {
                    "post": {"median_latency_hours": 1.2},
                },
                "solved": {
                    "post": {"solved_tickets": 12},
                },
                "outstanding_tickets": 0,
            }
        ],
        "insights": {
            "wins": ["17 users generated 6965 ESIP product interactions after ramp-up."],
            "concerns": ["6 post-ramp turns look like documentation-gap signals."],
            "caveats": ["Ramp-up is shown separately."],
        },
        "warnings": [],
    }


class ImpactExportTest(unittest.TestCase):
    def test_excel_export_has_expected_sheets_and_title(self):
        content = impact_export.build_excel_export(_sample_impact_report())

        wb = load_workbook(BytesIO(content))
        self.assertIn("Impact Report", wb.sheetnames)
        self.assertIn("Periods", wb.sheetnames)
        self.assertIn("Engineer View", wb.sheetnames)
        self.assertEqual(wb["Impact Report"]["A1"].value, "ESIP Early Impact Report")
        self.assertEqual(wb["Impact Report"]["A9"].value, "Active users after ramp")

    def test_pdf_export_is_server_generated_pdf(self):
        content = impact_export.build_pdf_export(_sample_impact_report())

        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"ESIP Early Impact Report", content)
        self.assertIn(b"Before, ramp-up, after", content)

    def test_get_export_file_reuses_impact_report_definitions(self):
        with mock.patch(
            "app.services.impact_export.impact_metrics.get_impact_report",
            return_value=_sample_impact_report(),
        ) as get_impact_report:
            export = impact_export.get_export_file(
                rollout_date="2026-05-26",
                file_format="xlsx",
            )

        self.assertEqual(export.filename, "esip-impact-2026-05-26_to_2026-06-12.xlsx")
        self.assertEqual(export.media_type, impact_export.XLSX_MEDIA_TYPE)
        get_impact_report.assert_called_once_with(rollout_date="2026-05-26", end_date=None)


class ImpactExportEndpointTest(unittest.TestCase):
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

    def test_admin_can_download_impact_export(self):
        export = impact_export.ImpactExportFile(
            content=b"%PDF-1.4\nfake",
            filename="esip-impact-2026-05-26_to_2026-06-12.pdf",
            media_type=impact_export.PDF_MEDIA_TYPE,
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.impact_export.get_export_file", return_value=export) as get_export_file,
        ):
            res = self.client.get(
                "/api/admin/impact/export?rollout_date=2026-05-26&format=pdf",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"%PDF-1.4\nfake")
        self.assertEqual(res.headers["content-type"], impact_export.PDF_MEDIA_TYPE)
        self.assertIn("esip-impact-2026-05-26_to_2026-06-12.pdf", res.headers["content-disposition"])
        _, kwargs = get_export_file.call_args
        self.assertEqual(kwargs["rollout_date"], "2026-05-26")
        self.assertEqual(kwargs["file_format"], "pdf")

    def test_invalid_impact_export_format_returns_400(self):
        with (
            self._auth_patches(),
            mock.patch(
                "app.routers.admin.impact_export.get_export_file",
                side_effect=ValueError("Unsupported Impact export format"),
            ),
        ):
            res = self.client.get(
                "/api/admin/impact/export?rollout_date=2026-05-26&format=csv",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
