#!/usr/bin/env python3
"""Zendesk Ticket Audits probe tests.

Run from project root: scripts/backend_python.sh backend/test_zendesk_ticket_audit_sample.py
"""

from __future__ import annotations

import unittest

from app.services import zendesk_ticket_audit_sample


class ZendeskTicketAuditSampleTest(unittest.TestCase):
    def test_extract_change_events_keeps_only_kpi_relevant_fields(self):
        events = zendesk_ticket_audit_sample.extract_change_events(
            123,
            [
                {
                    "id": 10,
                    "created_at": "2026-05-27T14:00:00Z",
                    "author_id": 555,
                    "via": {"channel": "web"},
                    "events": [
                        {
                            "id": 1,
                            "type": "Change",
                            "field_name": "assignee_id",
                            "previous_value": 11,
                            "value": 22,
                        },
                        {
                            "id": 2,
                            "type": "Change",
                            "field_name": "subject",
                            "previous_value": "old",
                            "value": "new",
                        },
                        {
                            "id": 3,
                            "type": "Comment",
                            "public": True,
                        },
                        {
                            "id": 4,
                            "type": "Change",
                            "field_name": "status",
                            "previous_value": "open",
                            "value": "solved",
                        },
                    ],
                }
            ],
        )

        self.assertEqual([event["field_name"] for event in events], ["assignee_id", "status"])
        self.assertEqual(events[0]["ticket_id"], 123)
        self.assertEqual(events[0]["audit_id"], 10)
        self.assertEqual(events[0]["via_channel"], "web")

    def test_probe_payload_counts_fields(self):
        class FakeSession:
            auth = None

        def fake_fetch(_session, ticket_id, *, max_pages):
            return [
                {
                    "id": ticket_id * 10,
                    "created_at": "2026-05-27T14:00:00Z",
                    "events": [
                        {"id": 1, "type": "Change", "field_name": "assignee_id", "value": 22},
                        {"id": 2, "type": "Change", "field_name": "group_id", "value": 33},
                    ],
                }
            ]

        original_session = zendesk_ticket_audit_sample.requests.Session
        original_fetch = zendesk_ticket_audit_sample.fetch_ticket_audits
        try:
            zendesk_ticket_audit_sample.requests.Session = lambda: FakeSession()
            zendesk_ticket_audit_sample.fetch_ticket_audits = fake_fetch
            payload = zendesk_ticket_audit_sample.probe_ticket_audits([101, 102], sleep_seconds=0)
        finally:
            zendesk_ticket_audit_sample.requests.Session = original_session
            zendesk_ticket_audit_sample.fetch_ticket_audits = original_fetch

        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["ticket_count"], 2)
        self.assertEqual(payload["event_count"], 4)
        self.assertEqual(payload["field_counts"], {"assignee_id": 2, "group_id": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
