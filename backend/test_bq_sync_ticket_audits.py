#!/usr/bin/env python3
"""Tests for Zendesk audit-event capture in nightly BQ sync.

Run from project root:
  scripts/backend_python.sh backend/test_bq_sync_ticket_audits.py
"""

from __future__ import annotations

import unittest

from app.services import bq_sync


class BqSyncTicketAuditsTest(unittest.TestCase):
    def test_extracts_only_kpi_relevant_change_events(self):
        audits = [
            {
                "id": 10,
                "created_at": "2026-05-28T12:00:00Z",
                "author_id": 123,
                "via": {"channel": "web"},
                "events": [
                    {"id": 1, "type": "Change", "field_name": "assignee_id", "previous_value": None, "value": 456},
                    {"id": 2, "type": "Change", "field_name": "status", "previous_value": "open", "value": "solved"},
                    {"id": 3, "type": "Change", "field_name": "subject", "previous_value": "A", "value": "B"},
                    {"id": 4, "type": "Comment", "field_name": "status", "value": "ignored"},
                ],
            }
        ]

        events = bq_sync._extract_audit_change_events(950043, audits)

        self.assertEqual([event["field_name"] for event in events], ["assignee_id", "status"])
        self.assertEqual(events[0]["ticket_id"], 950043)
        self.assertEqual(events[0]["via_channel"], "web")

    def test_build_ticket_audit_event_rows_dedupes_and_stringifies(self):
        event = {
            "ticket_id": 950043,
            "audit_id": 10,
            "event_id": 20,
            "created_at": "2026-05-28T12:00:00Z",
            "author_id": 123,
            "via_channel": "web",
            "field_name": "assignee_id",
            "previous_value": None,
            "value": 456,
        }

        rows = bq_sync._build_ticket_audit_event_rows({950043: [event, dict(event)]})

        self.assertEqual(len(rows), 1)
        self.assertIn("950043|10|20|assignee_id", rows[0]["event_key"])
        self.assertEqual(rows[0]["value"], "456")
        self.assertIsNone(rows[0]["previous_value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
