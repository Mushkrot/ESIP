"""
Service Desk KPI report tests.
Run from project root: scripts/backend_python.sh backend/test_service_desk_kpi.py
"""

from __future__ import annotations

import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo
from io import BytesIO
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.services import firestore, service_desk_kpi, service_desk_kpi_export, service_desk_kpi_snapshot


def _kpi_row(
    name: str,
    *,
    group_name: str | None = None,
    assigned: int = 0,
    solved: int = 0,
    outstanding: int = 0,
    calls: int = 0,
) -> dict:
    return {
        "email": f"{service_desk_kpi.NAME_OWNER_PREFIX}{service_desk_kpi._display_key(name)}",
        "display_name": name,
        "group_name": group_name,
        "inbound_calls": calls,
        "avg_call_time_seconds": 60 if calls else None,
        "new_assigned_tickets": assigned,
        "same_day_solved_tickets": solved,
        "solved_tickets": solved,
        "outstanding_tickets": outstanding,
        "median_resolution_hours": 4 if solved else None,
        "single_cycle_solved": solved,
        "single_cycle_total": solved,
        "single_cycle_solve_rate_pct": 100 if solved else 0,
        "sla_breached": 0,
        "sla_known": solved,
        "sla_breach_rate_pct": 0,
        "first_reply_tickets": assigned,
        "median_first_public_reply_hours": 1 if assigned else None,
        "customer_reply_count": solved,
        "median_customer_reply_hours": 2 if solved else None,
    }


class ServiceDeskKpiTest(unittest.TestCase):
    def test_day_bounds_use_business_time_date(self):
        start_at, end_at = service_desk_kpi._day_bounds_utc(date(2026, 5, 26))

        self.assertEqual(start_at.isoformat(), "2026-05-26T04:00:00+00:00")
        self.assertEqual(end_at.isoformat(), "2026-05-27T04:00:00+00:00")

    def test_snapshot_at_uses_next_day_8am_business_time_for_past_dates(self):
        snapshot_at = service_desk_kpi._snapshot_at_utc(
            date(2026, 5, 26),
            now=datetime(2026, 5, 29, 12, 0, tzinfo=ZoneInfo("America/New_York")),
        )

        self.assertEqual(snapshot_at.isoformat(), "2026-05-27T12:00:00+00:00")

    def test_snapshot_at_uses_now_when_next_day_report_time_is_future(self):
        now = datetime(2026, 5, 26, 15, 30, tzinfo=ZoneInfo("America/New_York"))
        snapshot_at = service_desk_kpi._snapshot_at_utc(date(2026, 5, 26), now=now)

        self.assertEqual(snapshot_at.isoformat(), "2026-05-26T19:30:00+00:00")

    def test_outstanding_at_uses_report_day_end_for_past_dates(self):
        outstanding_at = service_desk_kpi._outstanding_at_utc(
            date(2026, 6, 8),
            now=datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo("America/New_York")),
        )

        self.assertEqual(outstanding_at.isoformat(), "2026-06-09T04:00:00+00:00")

    def test_outstanding_at_uses_now_when_report_day_end_is_future(self):
        now = datetime(2026, 6, 8, 15, 30, tzinfo=ZoneInfo("America/New_York"))
        outstanding_at = service_desk_kpi._outstanding_at_utc(date(2026, 6, 8), now=now)

        self.assertEqual(outstanding_at.isoformat(), "2026-06-08T19:30:00+00:00")

    def test_phone_kpi_sql_includes_cxone_call_media(self):
        sql = service_desk_kpi._phone_kpi_sql()

        self.assertIn("c.media_type_id = 4", sql)
        self.assertIn("= 'call'", sql)
        self.assertIn("SUPPORT_General Support EN", sql)
        self.assertIn("Support_Voicemail", sql)
        self.assertIn("COALESCE(c.agent_seconds, 0) > 0", sql)
        self.assertIn("c.agent_id IS NOT NULL", sql)
        self.assertIn("AVG(agent_seconds)", sql)

    def test_ticket_kpi_sql_reconstructs_snapshot_status_and_owner(self):
        sql = service_desk_kpi._ticket_kpi_sql()

        self.assertIn("zendesk_ticket_audit_events", sql)
        self.assertIn("first_assignee_after_snapshot", sql)
        self.assertIn("first_group_after_snapshot", sql)
        self.assertIn("first_status_after_outstanding", sql)
        self.assertIn("first_assignee_after_outstanding", sql)
        self.assertIn("first_group_after_outstanding", sql)
        self.assertIn("group_identities", sql)
        self.assertIn("solve_events", sql)
        self.assertIn("first_assignee_after_solve", sql)
        self.assertIn("first_group_after_solve", sql)
        self.assertIn("group_name_at_solve", sql)
        self.assertIn("FULL OUTER JOIN solved_aggregates USING(owner_email, group_key)", sql)
        self.assertIn("LOWER(COALESCE(value, '')) = 'solved'", sql)
        self.assertNotIn("LOWER(COALESCE(value, '')) = 'closed'", sql)
        self.assertIn("@snapshot_at", sql)
        self.assertIn("@outstanding_at", sql)
        self.assertIn("tickets.created_at < @outstanding_at", sql)
        self.assertIn("status_asof_at_day_end IN ('new', 'open', 'pending', 'hold')", sql)
        self.assertIn("FULL OUTER JOIN outstanding_aggregates USING(owner_email, group_key)", sql)
        self.assertIn("group_identity.group_name", sql)
        self.assertIn("solve_group_identity.group_name", sql)

    def test_merge_rows_uses_real_profile_name_and_aliases(self):
        profiles = {
            "alex.morgan@northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
            "alex.legacy@support.northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
        }
        aliases = service_desk_kpi._profile_alias_map(profiles)

        rows = service_desk_kpi._merge_rows(
            ticket_rows=[{
                "owner_email": "alex.legacy@support.northstar.example.com",
                "assignee_name": "Alex Morgan",
                "new_assigned_tickets": 3,
                "same_day_solved_tickets": 2,
                "solved_tickets": 2,
                "outstanding_tickets": 5,
                "median_resolution_hours": 4.5,
                "single_cycle_solved": 1,
                "sla_breached": 0,
                "sla_known": 2,
            }],
            first_reply_rows=[{
                "owner_email": "alex.morgan@northstar.example.com",
                "first_reply_tickets": 2,
                "median_first_public_reply_hours": 0.75,
            }],
            customer_reply_rows=[{
                "owner_email": "alex.morgan@northstar.example.com",
                "customer_reply_count": 4,
                "median_customer_reply_hours": 1.25,
            }],
            profiles=profiles,
            aliases=aliases,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "alex.morgan@northstar.example.com")
        self.assertEqual(rows[0]["display_name"], "Alex Morgan")
        self.assertEqual(rows[0]["new_assigned_tickets"], 3)
        self.assertEqual(rows[0]["same_day_solved_tickets"], 2)
        self.assertEqual(rows[0]["solved_tickets"], 2)
        self.assertEqual(rows[0]["single_cycle_solved"], 1)
        self.assertEqual(rows[0]["single_cycle_total"], 2)
        self.assertEqual(rows[0]["single_cycle_solve_rate_pct"], 50)
        self.assertEqual(rows[0]["sla_breached"], 0)
        self.assertEqual(rows[0]["sla_known"], 2)
        self.assertEqual(rows[0]["sla_breach_rate_pct"], 0)
        self.assertEqual(rows[0]["median_customer_reply_hours"], 1.25)

    def test_merge_rows_accumulates_sla_counts_across_aliases(self):
        profiles = {
            "alex.morgan@northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
            "alex.legacy@support.northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
        }
        aliases = service_desk_kpi._profile_alias_map(profiles)

        rows = service_desk_kpi._merge_rows(
            ticket_rows=[
                {
                    "owner_email": "alex.legacy@support.northstar.example.com",
                    "assignee_name": "Alex Morgan",
                    "new_assigned_tickets": 0,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 2,
                    "outstanding_tickets": 0,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 1,
                    "sla_breached": 1,
                    "sla_known": 2,
                },
                {
                    "owner_email": "alex.morgan@northstar.example.com",
                    "assignee_name": "Alex Morgan",
                    "new_assigned_tickets": 0,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 2,
                    "outstanding_tickets": 0,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 2,
                    "sla_breached": 1,
                    "sla_known": 2,
                },
            ],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles=profiles,
            aliases=aliases,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["solved_tickets"], 4)
        self.assertEqual(rows[0]["single_cycle_solved"], 3)
        self.assertEqual(rows[0]["single_cycle_total"], 4)
        self.assertEqual(rows[0]["single_cycle_solve_rate_pct"], 75)
        self.assertEqual(rows[0]["sla_breached"], 2)
        self.assertEqual(rows[0]["sla_known"], 4)
        self.assertEqual(rows[0]["sla_breach_rate_pct"], 50)

    def test_merge_rows_keeps_blank_unassigned_row(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[{
                "owner_email": service_desk_kpi.BLANK_OWNER_EMAIL,
                "assignee_name": service_desk_kpi.BLANK_OWNER_LABEL,
                "new_assigned_tickets": 3,
                "same_day_solved_tickets": 2,
                "solved_tickets": 2,
                "outstanding_tickets": 5,
                "median_resolution_hours": None,
                "single_cycle_solved": 0,
                "sla_breached": 0,
                "sla_known": 0,
            }],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], service_desk_kpi.BLANK_OWNER_EMAIL)
        self.assertEqual(rows[0]["display_name"], service_desk_kpi.BLANK_OWNER_LABEL)
        self.assertEqual(rows[0]["new_assigned_tickets"], 3)
        self.assertEqual(rows[0]["same_day_solved_tickets"], 2)
        self.assertEqual(rows[0]["outstanding_tickets"], 5)

    def test_merge_rows_normalizes_legacy_menu_maintenance_ba_row(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[{
                "owner_email": f"{service_desk_kpi.NAME_OWNER_PREFIX}menu maintenance and ba",
                "assignee_name": "Menu Maintenance",
                "new_assigned_tickets": 16,
                "same_day_solved_tickets": 0,
                "solved_tickets": 0,
                "outstanding_tickets": 42,
                "median_resolution_hours": None,
                "single_cycle_solved": 0,
                "sla_breached": 0,
                "sla_known": 0,
            }],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], f"{service_desk_kpi.NAME_OWNER_PREFIX}menu maintenance")
        self.assertEqual(rows[0]["display_name"], "Menu Maintenance")
        self.assertEqual(rows[0]["new_assigned_tickets"], 16)
        self.assertEqual(rows[0]["outstanding_tickets"], 42)

    def test_merge_rows_keeps_zendesk_queue_name_as_menu_maintenance_row(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[{
                "owner_email": f"{service_desk_kpi.NAME_OWNER_PREFIX}menu maintenance",
                "assignee_name": "Menu Maintenance",
                "new_assigned_tickets": 6,
                "same_day_solved_tickets": 0,
                "solved_tickets": 0,
                "outstanding_tickets": 38,
                "median_resolution_hours": None,
                "single_cycle_solved": 0,
                "sla_breached": 0,
                "sla_known": 0,
            }],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], f"{service_desk_kpi.NAME_OWNER_PREFIX}menu maintenance")
        self.assertEqual(rows[0]["display_name"], "Menu Maintenance")
        self.assertEqual(rows[0]["new_assigned_tickets"], 6)
        self.assertEqual(rows[0]["outstanding_tickets"], 38)

    def test_group_rows_roll_up_fixed_service_desk_groups_without_double_counting(self):
        individual_rows = [
            _kpi_row("Jamie Rivera", assigned=2, solved=1, outstanding=3, calls=2),
            _kpi_row("Morgan Chen", assigned=1, solved=2, outstanding=4, calls=1),
            _kpi_row("Sofia Reyes", assigned=5, solved=3, outstanding=7),
            _kpi_row("Noah Bennett", assigned=4, solved=1, outstanding=6),
            _kpi_row("Technical Services", assigned=1, solved=0, outstanding=2),
            _kpi_row("Field Services Partner", assigned=9, solved=0, outstanding=5),
        ]

        groups = service_desk_kpi._group_rows(individual_rows)
        by_name = {group["display_name"]: group for group in groups}

        self.assertEqual(by_name["Netops"]["new_assigned_tickets"], 3)
        self.assertEqual(by_name["Netops"]["solved_tickets"], 3)
        self.assertEqual(by_name["Netops"]["outstanding_tickets"], 7)
        self.assertEqual(by_name["Menu Maintenance"]["new_assigned_tickets"], 5)
        self.assertEqual(by_name["Business Analysts"]["new_assigned_tickets"], 0)
        self.assertIn("Sofia Reyes", by_name["Business Analysts"]["member_names"])
        self.assertEqual(by_name["Technical Services"]["new_assigned_tickets"], 5)
        self.assertEqual(by_name["Technical Services"]["outstanding_tickets"], 8)
        self.assertEqual(by_name["Field Services Partner"]["new_assigned_tickets"], 9)
        self.assertEqual(by_name["Field Services Partner"]["outstanding_tickets"], 5)
        self.assertNotIn("Unmapped / queues", by_name)
        self.assertEqual(
            sum(group["new_assigned_tickets"] for group in groups),
            sum(row["new_assigned_tickets"] for row in individual_rows),
        )

    def test_group_rows_use_ticket_group_for_ticket_counts(self):
        individual_rows = [
            _kpi_row("Sofia Reyes", assigned=5, solved=3, outstanding=7, calls=2),
            _kpi_row("Field Services Partner", assigned=1, outstanding=2),
        ]
        ticket_rows = [
            _kpi_row("Sofia Reyes", group_name="Menu Maintenance", assigned=2, solved=1, outstanding=4),
            _kpi_row("Sofia Reyes", group_name="Business Analysts", assigned=3, solved=2, outstanding=3),
            _kpi_row("Blank / unassigned", group_name="Service Desk Supervisors", outstanding=6),
            _kpi_row("Field Services Partner", group_name="Field Services Partner", assigned=1, outstanding=2),
        ]

        groups = service_desk_kpi._group_rows(individual_rows, ticket_rows=ticket_rows)
        by_name = {group["display_name"]: group for group in groups}

        self.assertEqual(by_name["Menu Maintenance"]["new_assigned_tickets"], 2)
        self.assertEqual(by_name["Menu Maintenance"]["inbound_calls"], 2)
        self.assertEqual(by_name["Business Analysts"]["new_assigned_tickets"], 3)
        self.assertEqual(by_name["Business Analysts"]["solved_tickets"], 2)
        self.assertEqual(by_name["Service Desk Supervisors"]["outstanding_tickets"], 6)
        self.assertEqual(by_name["Field Services Partner"]["new_assigned_tickets"], 1)
        self.assertEqual(
            sum(group["new_assigned_tickets"] for group in groups),
            sum(row["new_assigned_tickets"] for row in ticket_rows),
        )

    def test_group_sections_include_totals_members_and_queue_rows(self):
        individual_rows = [
            _kpi_row("Sofia Reyes", assigned=5, solved=3, outstanding=7, calls=2),
        ]
        ticket_rows = [
            _kpi_row("Sofia Reyes", group_name="Menu Maintenance", assigned=2, solved=1, outstanding=4),
            _kpi_row("Sofia Reyes", group_name="Business Analysts", assigned=3, solved=2, outstanding=3),
            _kpi_row("Blank / unassigned", group_name="Service Desk Supervisors", outstanding=6),
            _kpi_row("Field Services Partner", group_name="Field Services Partner", assigned=1, outstanding=2),
        ]
        groups = service_desk_kpi._group_rows(individual_rows, ticket_rows=ticket_rows)

        sections = service_desk_kpi._group_sections(individual_rows, ticket_rows=ticket_rows, group_rows=groups)
        by_group = {section["group_name"]: section for section in sections}

        self.assertEqual(by_group["Menu Maintenance"]["total"]["new_assigned_tickets"], 2)
        self.assertEqual(by_group["Business Analysts"]["total"]["new_assigned_tickets"], 3)
        ba_rows = {row["display_name"]: row for row in by_group["Business Analysts"]["rows"]}
        self.assertEqual(ba_rows["Sofia Reyes"]["solved_tickets"], 2)
        queue_rows = {row["display_name"]: row for row in by_group["Service Desk Supervisors"]["rows"]}
        self.assertEqual(queue_rows["Blank / unassigned"]["outstanding_tickets"], 6)
        field_services_partner_rows = {row["display_name"]: row for row in by_group["Field Services Partner"]["rows"]}
        self.assertEqual(field_services_partner_rows["Field Services Partner queue"]["new_assigned_tickets"], 1)

    def test_known_service_desk_assignees_do_not_fall_into_unmapped_for_group_aliases(self):
        ticket_rows = [
            _kpi_row("Alex Morgan", group_name="Service Desk", assigned=18, solved=17, outstanding=10),
            _kpi_row("Casey Nguyen", group_name="Service Desk Analysts", assigned=14, solved=12, outstanding=2),
        ]
        groups = service_desk_kpi._group_rows([], ticket_rows=ticket_rows)
        by_name = {group["display_name"]: group for group in groups}

        self.assertEqual(by_name["ServiceDesk"]["new_assigned_tickets"], 32)
        self.assertEqual(by_name["ServiceDesk"]["solved_tickets"], 29)
        self.assertEqual(by_name["ServiceDesk"]["outstanding_tickets"], 12)
        self.assertNotIn("Unmapped / queues", by_name)

        sections = service_desk_kpi._group_sections([], ticket_rows=ticket_rows, group_rows=groups)
        by_group = {section["group_name"]: section for section in sections}
        service_desk_rows = {row["display_name"]: row for row in by_group["ServiceDesk"]["rows"]}

        self.assertEqual(service_desk_rows["Alex Morgan"]["new_assigned_tickets"], 18)
        self.assertEqual(service_desk_rows["Casey Nguyen"]["new_assigned_tickets"], 14)

    def test_phone_only_known_name_alias_rolls_up_to_service_desk(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[],
            first_reply_rows=[],
            customer_reply_rows=[],
            phone_rows=[{
                "owner_email": "__name__:sam patel",
                "assignee_name": "Samir Patel",
                "inbound_calls": 4,
                "avg_call_time_seconds": 368,
            }],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "__name__:samir patel")
        self.assertEqual(rows[0]["display_name"], "Samir Patel")

        groups = service_desk_kpi._group_rows(rows, ticket_rows=[])
        by_name = {group["display_name"]: group for group in groups}

        self.assertEqual(by_name["ServiceDesk"]["inbound_calls"], 4)
        self.assertNotIn("Unmapped / queues", by_name)

    def test_known_queue_aliases_do_not_fall_into_unmapped(self):
        ticket_rows = [
            _kpi_row("Deployment Analysts", group_name="Deployment Analysts", outstanding=5),
            _kpi_row("Operations Support", group_name="Operations Support", assigned=1, solved=5, outstanding=12),
            _kpi_row("Store Operations Support", group_name="Store Operations Support", assigned=14, solved=11, outstanding=8),
        ]
        groups = service_desk_kpi._group_rows([], ticket_rows=ticket_rows)
        by_name = {group["display_name"]: group for group in groups}

        self.assertEqual(by_name["DeploymentTeam"]["outstanding_tickets"], 5)
        self.assertEqual(by_name["Operations Support"]["solved_tickets"], 5)
        self.assertEqual(by_name["TrainingTeam"]["new_assigned_tickets"], 14)
        self.assertNotIn("Unmapped / queues", by_name)

        sections = service_desk_kpi._group_sections([], ticket_rows=ticket_rows, group_rows=groups)
        by_group = {section["group_name"]: section for section in sections}
        self.assertEqual(
            {row["display_name"] for row in by_group["DeploymentTeam"]["rows"] if service_desk_kpi._row_has_activity(row)},
            {"DeploymentTeam"},
        )
        self.assertEqual(
            {row["display_name"] for row in by_group["Operations Support"]["rows"] if service_desk_kpi._row_has_activity(row)},
            {"Operations Support"},
        )
        self.assertEqual(
            {row["display_name"] for row in by_group["TrainingTeam"]["rows"] if service_desk_kpi._row_has_activity(row)},
            {"TrainingTeam"},
        )

    def test_group_sections_follow_manual_report_display_order(self):
        ticket_rows = [
            _kpi_row("Menu Maintenance", group_name="Menu Maintenance", assigned=1),
            _kpi_row("Jamie Rivera", group_name="Netops", assigned=1),
            _kpi_row("Noah Bennett", group_name="Technical Services", assigned=1),
            _kpi_row("Alex Morgan", group_name="ServiceDesk", assigned=1),
        ]
        groups = service_desk_kpi._group_rows([], ticket_rows=ticket_rows)

        sections = service_desk_kpi._group_sections([], ticket_rows=ticket_rows, group_rows=groups)

        self.assertEqual(
            [section["group_name"] for section in sections[:4]],
            ["ServiceDesk", "Menu Maintenance", "Technical Services", "Netops"],
        )
        service_desk_names = [row["display_name"] for row in sections[0]["rows"][:4]]
        self.assertEqual(
            service_desk_names,
            [
                "Alex Morgan",
                "Casey Nguyen",
                "Samir Patel",
                "Riley Brooks",
            ],
        )
        self.assertEqual(sections[1]["rows"][0]["display_name"], "Menu Maintenance")

    def test_merge_rows_combines_name_only_owner_with_profile(self):
        profiles = {
            "alex.morgan@northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
        }
        aliases = service_desk_kpi._profile_alias_map(profiles)

        rows = service_desk_kpi._merge_rows(
            ticket_rows=[
                {
                    "owner_email": "alex.morgan@northstar.example.com",
                    "assignee_name": "Alex Morgan",
                    "new_assigned_tickets": 2,
                    "same_day_solved_tickets": 1,
                    "solved_tickets": 1,
                    "outstanding_tickets": 3,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 1,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
                {
                    "owner_email": f"{service_desk_kpi.NAME_OWNER_PREFIX}alex morgan",
                    "assignee_name": "Alex Morgan",
                    "new_assigned_tickets": 0,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 0,
                    "outstanding_tickets": 4,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 0,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
            ],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles=profiles,
            aliases=aliases,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "alex.morgan@northstar.example.com")
        self.assertEqual(rows[0]["outstanding_tickets"], 7)

    def test_merge_rows_combines_name_only_first_name_with_ticket_row_alias(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[
                {
                    "owner_email": "casey.nguyen@northstar.example.com",
                    "assignee_name": "Casey Nguyen",
                    "new_assigned_tickets": 10,
                    "same_day_solved_tickets": 9,
                    "solved_tickets": 52,
                    "outstanding_tickets": 9,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 21,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
                {
                    "owner_email": f"{service_desk_kpi.NAME_OWNER_PREFIX}casey",
                    "assignee_name": "Casey",
                    "new_assigned_tickets": 0,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 0,
                    "outstanding_tickets": 0,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 0,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
            ],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "casey.nguyen@northstar.example.com")
        self.assertEqual(rows[0]["display_name"], "Casey Nguyen")
        self.assertEqual(rows[0]["same_day_solved_tickets"], 9)

    def test_merge_rows_combines_explicit_legacy_alias_without_profile(self):
        rows = service_desk_kpi._merge_rows(
            ticket_rows=[
                {
                    "owner_email": "jordan.lee@northstar.example.com",
                    "assignee_name": "Jordan Lee",
                    "new_assigned_tickets": 1,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 2,
                    "outstanding_tickets": 3,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 0,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
                {
                    "owner_email": "jordan.legacy@support.northstar.example.com",
                    "assignee_name": "Jordan Lee",
                    "new_assigned_tickets": 0,
                    "same_day_solved_tickets": 0,
                    "solved_tickets": 0,
                    "outstanding_tickets": 1,
                    "median_resolution_hours": None,
                    "single_cycle_solved": 0,
                    "sla_breached": 0,
                    "sla_known": 0,
                },
            ],
            first_reply_rows=[],
            customer_reply_rows=[],
            profiles={},
            aliases={},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "jordan.lee@northstar.example.com")
        self.assertEqual(rows[0]["display_name"], "Jordan Lee")
        self.assertEqual(rows[0]["outstanding_tickets"], 4)

    def test_report_notes_do_not_include_report_build_process(self):
        with (
            mock.patch("app.services.service_desk_kpi._get_client"),
            mock.patch("app.services.service_desk_kpi._load_identity_profiles", return_value={}),
            mock.patch("app.services.service_desk_kpi.cxone_kpi.ensure_cxone_day_loaded", return_value={"status": "not_configured", "available": False}),
            mock.patch("app.services.service_desk_kpi._query", side_effect=[[], [], [], [{}]]),
        ):
            report = service_desk_kpi.get_daily_report(report_date="2026-05-26")

        self.assertEqual(report["date"], "2026-05-26")
        self.assertEqual(report["notes"], [])
        note_text = " ".join(report["notes"])
        self.assertNotIn("Generated", note_text)
        self.assertNotIn("Grouped", note_text)
        self.assertNotIn("CXone", note_text)
        self.assertNotIn("snapshot", note_text)
        self.assertNotIn("Closed status is intentionally ignored", note_text)
        self.assertEqual(report["summary"]["inbound_calls"], 0)
        self.assertIsNone(report["summary"]["avg_call_time_seconds"])

    def test_report_cached_only_mode_skips_cxone_write_path(self):
        with (
            mock.patch("app.services.service_desk_kpi._get_client"),
            mock.patch("app.services.service_desk_kpi._load_identity_profiles", return_value={}),
            mock.patch(
                "app.services.service_desk_kpi.cxone_kpi.get_cached_cxone_day_status",
                return_value={"status": "cache_missing", "available": False},
            ) as cached_status,
            mock.patch("app.services.service_desk_kpi.cxone_kpi.ensure_cxone_day_loaded") as ensure_loaded,
            mock.patch("app.services.service_desk_kpi._query", side_effect=[[], [], [], [{}]]),
        ):
            report = service_desk_kpi.get_daily_report(
                report_date="2026-05-26",
                telephony_mode="cached_only",
            )

        cached_status.assert_called_once()
        ensure_loaded.assert_not_called()
        self.assertEqual(report["summary"]["inbound_calls"], 0)

    def test_range_export_uses_default_report_builder(self):
        with mock.patch(
            "app.services.service_desk_kpi_export.service_desk_kpi.get_daily_report",
            return_value=_sample_export_report(),
        ) as get_daily_report:
            export = service_desk_kpi_export.get_export_file(
                report_date="2026-05-27",
                file_format="xlsx",
            )

        self.assertEqual(export.media_type, service_desk_kpi_export.XLSX_MEDIA_TYPE)
        get_daily_report.assert_called_once_with(
            report_date="2026-05-27",
        )

    def test_daily_excel_export_uses_default_report_builder(self):
        with mock.patch(
            "app.services.service_desk_kpi_export.service_desk_kpi.get_daily_report",
            return_value=_sample_export_report(),
        ) as get_daily_report:
            export = service_desk_kpi_export.get_daily_excel_export("2026-05-27")

        self.assertEqual(export.media_type, service_desk_kpi_export.XLSX_MEDIA_TYPE)
        get_daily_report.assert_called_once_with(
            report_date="2026-05-27",
        )

    def test_report_can_skip_profile_loading(self):
        with (
            mock.patch("app.services.service_desk_kpi._get_client", return_value=mock.Mock()),
            mock.patch("app.services.service_desk_kpi._load_identity_profiles") as load_profiles,
            mock.patch(
                "app.services.service_desk_kpi.cxone_kpi.get_cached_cxone_day_status",
                return_value={"status": "cache_missing", "available": False},
            ),
            mock.patch("app.services.service_desk_kpi._query", side_effect=[[], [], [], [{}]]),
        ):
            service_desk_kpi.get_daily_report(
                report_date="2026-05-26",
                telephony_mode="cached_only",
                load_profiles=False,
            )

        load_profiles.assert_not_called()

    def test_merge_rows_adds_phone_metrics_by_alias(self):
        profiles = {
            "alex.morgan@northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
            "alex.legacy@support.northstar.example.com": {
                "display_name": "Alex Morgan",
                "active": True,
            },
        }
        aliases = service_desk_kpi._profile_alias_map(profiles)

        rows = service_desk_kpi._merge_rows(
            ticket_rows=[{
                "owner_email": "alex.morgan@northstar.example.com",
                "assignee_name": "Alex Morgan",
                "new_assigned_tickets": 1,
                "same_day_solved_tickets": 1,
                "solved_tickets": 1,
                "outstanding_tickets": 0,
                "median_resolution_hours": None,
                "single_cycle_solved": 1,
                "sla_breached": 0,
                "sla_known": 0,
            }],
            first_reply_rows=[],
            customer_reply_rows=[],
            phone_rows=[{
                "owner_email": "alex.legacy@support.northstar.example.com",
                "assignee_name": "Alex Morgan",
                "inbound_calls": 3,
                "avg_call_time_seconds": 125.5,
            }],
            profiles=profiles,
            aliases=aliases,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "alex.morgan@northstar.example.com")
        self.assertEqual(rows[0]["inbound_calls"], 3)
        self.assertEqual(rows[0]["avg_call_time_seconds"], 125.5)


def _sample_export_report(report_date: str = "2026-05-27") -> dict:
    return {
        "date": report_date,
        "timezone": "America/New_York",
        "generated_at": f"{report_date}T11:43:00-04:00",
        "summary": {
            "active_engineers": 1,
            "inbound_calls": 2,
            "avg_call_time_seconds": 95,
            "new_assigned_tickets": 3,
            "same_day_solved_tickets": 2,
            "solved_tickets": 2,
            "outstanding_tickets": 5,
            "customer_replies": 6,
            "median_first_public_reply_hours": 0.5,
            "median_customer_reply_hours": 1.25,
            "single_cycle_solved": 2,
            "single_cycle_total": 2,
            "single_cycle_solve_rate_pct": 100,
            "sla_breached": 1,
            "sla_known": 4,
            "sla_breach_rate_pct": 25,
        },
        "coverage": {
            "relevant_tickets": 10,
            "tickets_with_internal_assignee_email": 9,
            "assignee_email_coverage_pct": 90,
            "public_agent_replies": 6,
            "public_agent_replies_with_email": 6,
            "comment_author_email_coverage_pct": 100,
        },
        "rows": [{
            "email": "alex.morgan@northstar.example.com",
            "display_name": "Alex Morgan",
            "inbound_calls": 2,
            "avg_call_time_seconds": 95,
            "new_assigned_tickets": 3,
            "same_day_solved_tickets": 2,
            "solved_tickets": 2,
            "outstanding_tickets": 5,
            "median_resolution_hours": 4.5,
            "single_cycle_solved": 2,
            "single_cycle_total": 2,
            "single_cycle_solve_rate_pct": 100,
            "sla_breached": 1,
            "sla_known": 4,
            "sla_breach_rate_pct": 25,
            "first_reply_tickets": 3,
            "median_first_public_reply_hours": 0.5,
            "customer_reply_count": 6,
            "median_customer_reply_hours": 1.25,
        }],
        "group_rows": [{
            "email": "__group__:servicedesk",
            "group_key": "servicedesk",
            "display_name": "ServiceDesk",
            "member_names": ["Alex Morgan"],
            "mapped_row_names": ["Alex Morgan"],
            "active_rows": 1,
            "unmapped": False,
            "assignment_notes": [],
            "inbound_calls": 2,
            "avg_call_time_seconds": 95,
            "new_assigned_tickets": 3,
            "same_day_solved_tickets": 2,
            "solved_tickets": 2,
            "outstanding_tickets": 5,
            "median_resolution_hours": 4.5,
            "single_cycle_solved": 2,
            "single_cycle_total": 2,
            "single_cycle_solve_rate_pct": 100,
            "sla_breached": 1,
            "sla_known": 4,
            "sla_breach_rate_pct": 25,
            "first_reply_tickets": 3,
            "median_first_public_reply_hours": 0.5,
            "customer_reply_count": 6,
            "median_customer_reply_hours": 1.25,
        }],
        "group_sections": [{
            "group_name": "ServiceDesk",
            "group_key": "servicedesk",
            "total": {
                "email": "__group__:servicedesk",
                "group_key": "servicedesk",
                "display_name": "ServiceDesk",
                "member_names": ["Alex Morgan"],
                "mapped_row_names": ["Alex Morgan"],
                "active_rows": 1,
                "unmapped": False,
                "assignment_notes": [],
                "inbound_calls": 2,
                "avg_call_time_seconds": 95,
                "new_assigned_tickets": 3,
                "same_day_solved_tickets": 2,
                "solved_tickets": 2,
                "outstanding_tickets": 5,
                "median_resolution_hours": 4.5,
                "single_cycle_solved": 2,
                "single_cycle_total": 2,
                "single_cycle_solve_rate_pct": 100,
                "sla_breached": 1,
                "sla_known": 4,
                "sla_breach_rate_pct": 25,
                "first_reply_tickets": 3,
                "median_first_public_reply_hours": 0.5,
                "customer_reply_count": 6,
                "median_customer_reply_hours": 1.25,
            },
            "rows": [{
                "email": "__group_detail__:servicedesk:alex morgan",
                "display_name": "Alex Morgan",
                "group_name": "ServiceDesk",
                "row_type": "assignee",
                "inbound_calls": 2,
                "avg_call_time_seconds": 95,
                "new_assigned_tickets": 3,
                "same_day_solved_tickets": 2,
                "solved_tickets": 2,
                "outstanding_tickets": 5,
                "median_resolution_hours": 4.5,
                "single_cycle_solved": 2,
                "single_cycle_total": 2,
                "single_cycle_solve_rate_pct": 100,
                "sla_breached": 1,
                "sla_known": 4,
                "sla_breach_rate_pct": 25,
                "first_reply_tickets": 3,
                "median_first_public_reply_hours": 0.5,
                "customer_reply_count": 6,
                "median_customer_reply_hours": 1.25,
            }],
        }],
        "grouping": {"warnings": []},
        "notes": ["Daily KPI report: phone columns come from CXone data."],
        "warnings": [],
    }


class ServiceDeskKpiExportTest(unittest.TestCase):
    def test_excel_export_matches_email_style_table_shape(self):
        content = service_desk_kpi_export.build_excel_export(
            _sample_export_report("2026-05-27"),
            _sample_export_report("2026-05-26"),
        )

        wb = load_workbook(BytesIO(content))
        ws = wb["KPI Report"]

        self.assertEqual(wb.sheetnames, ["KPI Report"])
        self.assertEqual(ws["A1"].value, "KPI Report for May 26, 2026 - May 27, 2026")
        self.assertEqual(ws["A2"].value, "Date")
        self.assertEqual(ws["A3"].value, "Groups")
        self.assertEqual(ws["B3"].value, "Assignee")
        self.assertEqual(ws["E3"].value, "Assigned")
        self.assertEqual(ws["F3"].value, "Solved")
        self.assertEqual(ws["A4"].value, "Total")
        self.assertEqual(ws["B4"].value, "Report total")
        self.assertEqual(ws["A5"].value, "ServiceDesk")
        self.assertEqual(ws["B5"].value, "Group total")
        self.assertEqual(ws["B6"].value, "Alex Morgan")
        self.assertEqual(ws["C6"].value, 2)
        self.assertEqual(ws["D6"].value, "1:35")
        self.assertEqual(ws["K6"].value, 3)
        self.assertEqual(ws["L6"].value, 2)
        self.assertEqual(ws["N6"].value, 1)
        self.assertEqual(ws.freeze_panes, "A4")
        self.assertEqual(ws["A2"].fill.fgColor.rgb, "00CFC3DF")
        self.assertEqual(ws["C2"].fill.fgColor.rgb, "00CFC3DF")
        self.assertTrue(ws["A2"].font.bold)
        self.assertTrue(ws["C2"].font.bold)
        self.assertEqual(ws["A3"].fill.fgColor.rgb, "00E2E8F0")
        self.assertEqual(ws["A3"].border.bottom.style, "double")
        self.assertEqual(ws["C3"].border.left.style, "double")
        self.assertEqual(ws["H3"].border.right.style, "double")
        self.assertEqual(ws["I3"].border.left.style, "thin")
        self.assertEqual(ws["A4"].fill.fgColor.rgb, "00244C6D")
        self.assertEqual(ws["A4"].font.color.rgb, "00FFFFFF")
        self.assertIsNone(ws["A4"].border.bottom)
        self.assertEqual(ws["C4"].border.left.style, "double")
        self.assertEqual(ws["H4"].border.right.style, "double")
        self.assertEqual(ws["A5"].fill.fgColor.rgb, "00EEF3F8")
        self.assertEqual(ws["A5"].border.top.style, "double")
        self.assertEqual(ws["C5"].border.left.style, "double")
        self.assertEqual(ws["H5"].border.right.style, "double")
        text_values = "\n".join(str(cell.value) for row in ws.iter_rows() for cell in row if cell.value)
        self.assertNotIn("Grouped by Service Desk management list", text_values)
        self.assertNotIn("Generated", text_values)
        self.assertNotIn("Daily KPI", text_values)
        self.assertNotIn("Group total rows use the Zendesk group", text_values)

    def test_excel_export_can_render_one_day_only(self):
        content = service_desk_kpi_export.build_excel_export_for_reports([
            _sample_export_report("2026-05-27"),
        ])

        wb = load_workbook(BytesIO(content))
        ws = wb["KPI Report"]

        self.assertEqual(ws["A1"].value, "KPI Report for May 27, 2026")
        self.assertEqual(ws["A2"].value, "Date")
        self.assertEqual(ws["C2"].value, "5/27/2026")
        self.assertEqual(ws["I2"].value, None)
        self.assertEqual(ws["A4"].value, "Total")
        self.assertEqual(ws["C4"].value, 2)
        self.assertEqual(ws["H4"].value, 1)

    def test_pdf_export_is_server_generated_pdf(self):
        content = service_desk_kpi_export.build_pdf_export(
            _sample_export_report("2026-05-27"),
            _sample_export_report("2026-05-26"),
        )

        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"KPI Report", content)
        self.assertIn(b"Service Desk KPI by group and assignee", content)
        self.assertNotIn(b"Grouped by Service Desk management list", content)
        self.assertNotIn(b"Generated", content)
        self.assertNotIn(b"Daily KPI", content)


class ServiceDeskKpiEndpointTest(unittest.TestCase):
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

    def test_admin_can_read_service_desk_kpi(self):
        payload = {
            "date": "2026-05-26",
            "summary": {"solved_tickets": 4},
            "rows": [{"email": "alex.morgan@northstar.example.com", "display_name": "Alex Morgan"}],
            "notes": [],
            "warnings": [],
        }
        mocked_report = mock.Mock(return_value=payload)
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi.get_daily_report", mocked_report),
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi?date=2026-05-26&refresh_telephony=1",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["rows"][0]["display_name"], "Alex Morgan")
        mocked_report.assert_called_once_with(report_date="2026-05-26", refresh_telephony=True)

    def test_non_admin_cannot_read_service_desk_kpi(self):
        with self._auth_patches(role=firestore.ROLE_USER):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi?date=2026-05-26",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 403)

    def test_admin_can_download_service_desk_kpi_export(self):
        export = service_desk_kpi_export.KpiExportFile(
            content=b"%PDF-1.4\nfake",
            filename="service-desk-kpi-2026-05-26.pdf",
            media_type=service_desk_kpi_export.PDF_MEDIA_TYPE,
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_export.get_export_file", return_value=export),
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/export?date=2026-05-26&format=pdf",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"%PDF-1.4\nfake")
        self.assertEqual(res.headers["content-type"], service_desk_kpi_export.PDF_MEDIA_TYPE)
        self.assertIn("service-desk-kpi-2026-05-26.pdf", res.headers["content-disposition"])

    def test_admin_daily_excel_download_uses_canonical_daily_excel_helper(self):
        export = service_desk_kpi_export.KpiExportFile(
            content=b"xlsx",
            filename="service-desk-kpi-2026-05-26.xlsx",
            media_type=service_desk_kpi_export.XLSX_MEDIA_TYPE,
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_export.get_daily_excel_export", return_value=export) as get_daily_excel_export,
            mock.patch("app.routers.admin.service_desk_kpi_export.get_export_file") as get_export_file,
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/export?date=2026-05-26&format=xlsx",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"xlsx")
        self.assertEqual(res.headers["content-type"], service_desk_kpi_export.XLSX_MEDIA_TYPE)
        self.assertIn("service-desk-kpi-2026-05-26.xlsx", res.headers["content-disposition"])
        get_daily_excel_export.assert_called_once_with("2026-05-26")
        get_export_file.assert_not_called()

    def test_admin_single_day_excel_range_uses_canonical_daily_excel_helper(self):
        export = service_desk_kpi_export.KpiExportFile(
            content=b"xlsx",
            filename="service-desk-kpi-2026-05-26.xlsx",
            media_type=service_desk_kpi_export.XLSX_MEDIA_TYPE,
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_export.get_daily_excel_export", return_value=export) as get_daily_excel_export,
            mock.patch("app.routers.admin.service_desk_kpi_export.get_export_file") as get_export_file,
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/export?start_date=2026-05-26&end_date=2026-05-26&format=xlsx",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"xlsx")
        get_daily_excel_export.assert_called_once_with("2026-05-26")
        get_export_file.assert_not_called()

    def test_invalid_service_desk_kpi_export_format_returns_400(self):
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_export.get_export_file", side_effect=ValueError("Unsupported KPI export format")),
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/export?date=2026-05-26&format=csv",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 400)

    def test_admin_can_list_service_desk_kpi_snapshots(self):
        artifact = service_desk_kpi_snapshot.SnapshotArtifact(
            report_date="2026-06-05",
            bucket="esip-reference-data",
            blob_name="reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            gcs_uri="gs://esip-reference-data/reference_kpi_reports/2026-06/service-desk-kpi-2026-06-05.xlsx",
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type=service_desk_kpi_export.XLSX_MEDIA_TYPE,
            size_bytes=43210,
            updated_at="2026-06-06T12:34:56+00:00",
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_snapshot.list_snapshots", return_value=[artifact]) as list_snapshots,
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/snapshots?limit=20",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["items"][0]["report_date"], "2026-06-05")
        self.assertEqual(body["items"][0]["filename"], "service-desk-kpi-2026-06-05.xlsx")
        list_snapshots.assert_called_once_with(limit=20)

    def test_admin_can_download_service_desk_kpi_snapshot(self):
        export = service_desk_kpi_snapshot.SnapshotDownload(
            filename="service-desk-kpi-2026-06-05.xlsx",
            media_type=service_desk_kpi_export.XLSX_MEDIA_TYPE,
            content=b"xlsx-data",
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.service_desk_kpi_snapshot.download_snapshot", return_value=export),
        ):
            res = self.client.get(
                "/api/admin/usage/service-desk-kpi/snapshots/2026-06-05/download",
                headers={"Authorization": "Bearer fake"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"xlsx-data")
        self.assertEqual(res.headers["content-type"], service_desk_kpi_export.XLSX_MEDIA_TYPE)
        self.assertIn("service-desk-kpi-2026-06-05.xlsx", res.headers["content-disposition"])


class ServiceDeskKpiTimeoutTest(unittest.TestCase):
    def test_query_raises_clear_timeout_error(self):
        client = mock.Mock()
        job = mock.Mock()
        job.result.side_effect = FuturesTimeoutError()
        client.query.return_value = job

        with self.assertRaises(service_desk_kpi.ServiceDeskKpiError) as ctx:
            service_desk_kpi._query(client, "SELECT 1", [])

        self.assertIn("BigQuery timed out", str(ctx.exception))
        self.assertIn("Service Desk KPI data", str(ctx.exception))
        client.query.assert_called_once()
        self.assertEqual(client.query.call_args.kwargs["retry"], None)
        self.assertEqual(client.query.call_args.kwargs["job_retry"], None)
        self.assertEqual(
            client.query.call_args.kwargs["timeout"],
            service_desk_kpi.BIGQUERY_QUERY_TIMEOUT_SECONDS,
        )
        job.result.assert_called_once_with(timeout=service_desk_kpi.BIGQUERY_QUERY_TIMEOUT_SECONDS)

    def test_query_wraps_query_start_failure(self):
        client = mock.Mock()
        client.query.side_effect = RuntimeError("network blocked")

        with self.assertRaises(service_desk_kpi.ServiceDeskKpiError) as ctx:
            service_desk_kpi._query(client, "SELECT 1", [])

        self.assertIn("BigQuery request failed", str(ctx.exception))
        self.assertIn("RuntimeError: network blocked", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
