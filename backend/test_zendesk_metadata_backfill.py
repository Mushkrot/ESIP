#!/usr/bin/env python3
"""Zendesk metadata backfill unit tests.

Run from project root:
  scripts/backend_python.sh backend/test_zendesk_metadata_backfill.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

from app.services import zendesk_metadata_backfill as backfill


class ZendeskMetadataBackfillTest(unittest.TestCase):
    def test_completed_ticket_ids_from_phase_aware_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            path.write_text(json.dumps({
                "completed_ticket_ids": [101, 102],
                "completed_ticket_phases": {
                    "101": ["sla_author", "audits"],
                    "103": ["audits"],
                    "104": ["sla_author"],
                },
            }))

            self.assertEqual(
                backfill.completed_ticket_ids_from_checkpoint(path, backfill.AUDITS_PHASE),
                {101, 103},
            )
            self.assertEqual(
                backfill.completed_ticket_ids_from_checkpoint(path, backfill.SLA_AUTHOR_PHASE),
                {101, 102, 104},
            )

    def test_missing_checkpoint_returns_empty_completed_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(backfill.completed_ticket_ids_from_checkpoint(missing, backfill.AUDITS_PHASE), set())

    def test_phase_payload_is_stable_and_phase_specific(self):
        payload = backfill._phase_payload({
            200: {backfill.AUDITS_PHASE},
            100: {backfill.SLA_AUTHOR_PHASE, backfill.AUDITS_PHASE},
        })

        self.assertEqual(json.loads(payload), [
            {"ticket_id": 100, "phase": "audits"},
            {"ticket_id": 100, "phase": "sla_author"},
            {"ticket_id": 200, "phase": "audits"},
        ])

    def test_build_author_rows_uses_zendesk_user_metadata(self):
        rows = backfill.build_author_rows(
            {
                950043: [
                    {
                        "id": 9001,
                        "author_id": 501,
                        "via": {"channel": "web"},
                    }
                ]
            },
            {
                501: {
                    "name": "Casey Nguyen",
                    "email": "casey.nguyen@northstar.example.com",
                    "role": "agent",
                }
            },
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket_id"], 950043)
        self.assertEqual(rows[0]["comment_id"], 9001)
        self.assertEqual(rows[0]["author_email"], "casey.nguyen@northstar.example.com")
        self.assertEqual(rows[0]["author_role"], "agent")
        self.assertTrue(rows[0]["author_is_agent"])
        self.assertEqual(rows[0]["via_channel"], "web")


if __name__ == "__main__":
    unittest.main(verbosity=2)
