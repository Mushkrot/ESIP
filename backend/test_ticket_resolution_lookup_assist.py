"""Tests for ticket resolution lookup assist guards.

Run from project root:
scripts/backend_python.sh backend/test_ticket_resolution_lookup_assist.py
"""

from __future__ import annotations

import unittest

from app.services.ticket_resolution_lookup_assist import (
    build_ticket_resolution_lookup_answer,
    build_ticket_resolution_lookup_assist,
    documented_resolution_text,
    summarize_ticket_resolution_lookup,
)


class TicketResolutionLookupAssistTest(unittest.TestCase):
    def test_current_ticket_typo_resolution_followup_is_accepted(self):
        assist = build_ticket_resolution_lookup_assist(
            latest_question="How to resolved this?",
            routing_question="How to resolved this?",
            semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
            target_ticket_id=950050,
            open_ticket_id=950050,
        ).to_dict()

        self.assertTrue(assist["accepted"])
        self.assertFalse(assist["answer_affecting"])
        self.assertEqual(assist["target_ticket_id"], 950050)
        self.assertIn("target_ticket_available", assist["reason_codes"])
        self.assertIn("resolution_intent", assist["reason_codes"])

    def test_explicit_single_ticket_resolution_question_is_accepted(self):
        assist = build_ticket_resolution_lookup_assist(
            latest_question="How was ticket 950011 resolved?",
            routing_question="How was ticket 950011 resolved?",
            semantic_frame={"intent": "ticket_details", "target_context": "global"},
        ).to_dict()

        self.assertTrue(assist["accepted"])
        self.assertEqual(assist["target_ticket_id"], 950011)
        self.assertEqual(assist["target_source"], "explicit_ticket")

    def test_rejects_generic_no_target_and_incompatible_intents(self):
        generic = build_ticket_resolution_lookup_assist(
            latest_question="How do I fix a pinpad?",
            routing_question="How do I fix a pinpad?",
            semantic_frame={"intent": "procedure_lookup"},
        ).to_dict()
        bulk = build_ticket_resolution_lookup_assist(
            latest_question="How were tickets 950011 and 950053 resolved?",
            routing_question="How were tickets 950011 and 950053 resolved?",
            semantic_frame={"intent": "ticket_details"},
        ).to_dict()
        challenge = build_ticket_resolution_lookup_assist(
            latest_question="Where did you find that claim? Verify 950050.",
            routing_question="Where did you find that claim? Verify 950050.",
            semantic_frame={"intent": "fact_check_prior_answer"},
        ).to_dict()
        date_scope = build_ticket_resolution_lookup_assist(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={"intent": "ticket_retrieval", "target_context": "prior_turn"},
            target_ticket_id=950050,
        ).to_dict()
        analytics = build_ticket_resolution_lookup_assist(
            latest_question="How many open tickets does this store have?",
            routing_question="How many open tickets does this store have?",
            semantic_frame={"intent": "store_analytics"},
            target_ticket_id=950050,
        ).to_dict()

        self.assertFalse(generic["accepted"])
        self.assertIn("missing_target_ticket", generic["reason_codes"])
        self.assertFalse(bulk["accepted"])
        self.assertIn("bulk_ticket_lookup_intent", bulk["reason_codes"])
        self.assertFalse(challenge["accepted"])
        self.assertIn("challenge_fact_check_intent", challenge["reason_codes"])
        self.assertFalse(date_scope["accepted"])
        self.assertIn("date_scope_followup_intent", date_scope["reason_codes"])
        self.assertFalse(analytics["accepted"])
        self.assertIn("ticket_search_or_analytics_intent", analytics["reason_codes"])

    def test_documented_resolution_classifier_filters_generic_values(self):
        self.assertEqual(documented_resolution_text("resolved"), "")
        self.assertEqual(documented_resolution_text(" closed "), "")
        self.assertEqual(documented_resolution_text("Issue resolved"), "")
        self.assertEqual(
            documented_resolution_text("Rebooted the Telecom Partner modem and confirmed stable connectivity."),
            "Rebooted the Telecom Partner modem and confirmed stable connectivity.",
        )

    def test_answer_uses_documented_resolution_without_raw_full_text(self):
        lookup = {
            "requested_ids": [950050],
            "found_ids": [950050],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950050,
                    "subject": "Internet down",
                    "status": "solved",
                    "store_id": "2012",
                    "issue_category": "Network",
                    "solved_at": "2026-07-01",
                    "resolution": "Telecom Partner replaced the modem and verified connectivity.",
                    "full_text": "private raw text should never render",
                }
            ],
            "cap": 75,
            "truncated": False,
        }
        answer = build_ticket_resolution_lookup_answer(lookup, target_ticket_id=950050)
        summary = summarize_ticket_resolution_lookup(lookup)

        self.assertIn("Documented resolution: Telecom Partner replaced the modem", answer)
        self.assertIn("documented_resolution=true", answer)
        self.assertNotIn("private raw text", answer)
        self.assertTrue(summary["documented_resolution"])

    def test_answer_is_honest_for_open_or_missing_resolution(self):
        lookup = {
            "requested_ids": [950050],
            "found_ids": [950050],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950050,
                    "subject": "Internet down",
                    "status": "open",
                    "store_id": "2012",
                    "issue_category": "Network",
                    "resolution": "resolved",
                }
            ],
            "cap": 75,
            "truncated": False,
        }
        missing_lookup = {
            "requested_ids": [950058],
            "found_ids": [],
            "missing_ids": [950058],
            "rows": [],
            "cap": 75,
            "truncated": False,
        }

        answer = build_ticket_resolution_lookup_answer(lookup, target_ticket_id=950050)
        missing_answer = build_ticket_resolution_lookup_answer(missing_lookup, target_ticket_id=950058)

        self.assertIn("did not find a useful documented resolution", answer)
        self.assertIn("documented_resolution=false", answer)
        self.assertIn("did not find it in the bounded lookup", missing_answer)
        self.assertIn("I will not invent a resolution", missing_answer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
