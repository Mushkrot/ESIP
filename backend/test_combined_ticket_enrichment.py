#!/usr/bin/env python3
"""Combined Zendesk ticket enrichment runner tests.

Run from project root:
  scripts/backend_python.sh backend/test_combined_ticket_enrichment.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backfill_ticket_sla_and_comment_authors as runner


class CombinedTicketEnrichmentTest(unittest.TestCase):
    def test_legacy_checkpoint_does_not_skip_new_audit_phase(self):
        candidates = {
            101: {runner.SLA_AUTHOR_PHASE, runner.AUDITS_PHASE},
            102: {runner.AUDITS_PHASE},
            103: {runner.SLA_AUTHOR_PHASE},
        }
        checkpoint = {"completed_ticket_ids": [101, 103]}

        pending = runner._pending_phase_items(candidates, runner._completed_phase_map(checkpoint))

        self.assertEqual(pending, {
            101: {runner.AUDITS_PHASE},
            102: {runner.AUDITS_PHASE},
        })

    def test_record_completed_phases_keeps_phase_specific_state(self):
        checkpoint = {"completed_ticket_ids": [101]}

        runner._record_completed_phases(
            checkpoint,
            {
                101: {runner.AUDITS_PHASE},
                102: {runner.SLA_AUTHOR_PHASE},
            },
        )

        self.assertEqual(checkpoint["completed_ticket_ids"], [101, 102])
        self.assertEqual(checkpoint["completed_ticket_phases"]["101"], [
            runner.AUDITS_PHASE,
            runner.SLA_AUTHOR_PHASE,
        ])
        self.assertEqual(checkpoint["completed_ticket_phases"]["102"], [runner.SLA_AUTHOR_PHASE])

    def test_build_audit_rows_deduplicates_and_stringifies_values(self):
        event = {
            "ticket_id": 950043,
            "audit_id": 10,
            "event_id": 20,
            "created_at": "2026-05-28T10:00:00Z",
            "author_id": 30,
            "via_channel": "web",
            "field_name": "assignee_id",
            "previous_value": None,
            "value": 930100000004,
        }

        rows = runner.build_audit_rows({950043: [event, dict(event)]})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket_id"], 950043)
        self.assertEqual(rows[0]["field_name"], "assignee_id")
        self.assertIsNone(rows[0]["previous_value"])
        self.assertEqual(rows[0]["value"], "930100000004")
        self.assertIn("950043|10|20|assignee_id", rows[0]["event_key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
