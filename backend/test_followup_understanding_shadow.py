"""Tests for follow-up understanding shadow contracts."""

from __future__ import annotations

import unittest
from datetime import datetime

from app.services.followup_understanding_shadow import build_followup_understanding_shadow


NOW = datetime.fromisoformat("2026-07-07T12:00:00-04:00")


class FollowupUnderstandingShadowTest(unittest.TestCase):
    def test_chat_118_style_month_followup_emits_shadow_contract(self):
        contract = build_followup_understanding_shadow(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={
                "target_context": "prior_turn",
                "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
                "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
            },
            previous_assistant_text="I found the degraded internet-speed cases for store 2008.",
            conversation_entities={
                "trace_id": "trace-prior",
                "turn_id": "turn-prior",
                "semantic_labels": ["internet speed", "connection drops"],
                "store_id": "2009",
                "period": {
                    "display_label": "May 2026",
                    "start_date": "2026-05-01",
                    "end_date_exclusive": "2026-06-01",
                },
            },
            messages=[
                {"role": "user", "content": "Show me internet-speed cases for store 2008 in May"},
                {"role": "assistant", "content": "I found the relevant cases."},
                {"role": "user", "content": "i just want the one for July and Jun"},
            ],
            now=NOW,
        )

        payload = contract.to_dict()
        self.assertTrue(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertTrue(payload["needs_assistive_promotion"])
        self.assertEqual(payload["target_context"], "prior_turn")
        self.assertEqual(
            [(item["start_date"], item["end_date_exclusive"]) for item in payload["month_windows"]],
            [("2026-07-01", "2026-08-01"), ("2026-06-01", "2026-07-01")],
        )
        self.assertTrue(payload["month_windows"][0]["current_month_to_date"])
        self.assertEqual(payload["prior_topic"]["store_id"], "2009")
        self.assertEqual(payload["prior_topic"]["prior_trace_id"], "trace-prior")
        self.assertEqual(payload["prior_topic"]["prior_turn_id"], "turn-prior")
        self.assertIn("internet speed", payload["prior_topic"]["semantic_labels"])

    def test_no_context_negative_does_not_emit(self):
        contract = build_followup_understanding_shadow(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            conversation_entities=None,
            messages=[{"role": "user", "content": "i just want the one for July and Jun"}],
            now=NOW,
        )

        payload = contract.to_dict()
        self.assertFalse(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertIn("insufficient_prior_context", payload["reason_codes"])

    def test_explicit_date_negative_does_not_treat_as_month_shorthand(self):
        contract = build_followup_understanding_shadow(
            latest_question="i just want the one for July 1 to July 2",
            routing_question="i just want the one for July 1 to July 2",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Previous result.",
            conversation_entities={"trace_id": "trace-prior", "turn_id": "turn-prior"},
            messages=[
                {"role": "assistant", "content": "Previous result."},
                {"role": "user", "content": "i just want the one for July 1 to July 2"},
            ],
            now=NOW,
        )

        payload = contract.to_dict()
        self.assertFalse(payload["emitted"])
        self.assertEqual(payload["month_windows"], [])
        self.assertIn("explicit_date_range_present", payload["reason_codes"])

    def test_prior_topic_extraction_from_semantic_and_memory(self):
        contract = build_followup_understanding_shadow(
            latest_question="what about those for June",
            routing_question="what about those for June",
            semantic_frame={
                "target_context": "prior_turn",
                "entities": {"issue_or_category": "payment terminal", "store_id": "2012"},
                "task": {"labels": ["pinpad offline"]},
            },
            previous_assistant_text="The earlier answer listed pinpad offline tickets.",
            conversation_entities={
                "trace_id": "trace-1",
                "turn_id": "turn-1",
                "semantic_labels": ["pinpad offline", "payment terminal"],
                "store_id": "2012",
            },
            messages=[
                {"role": "assistant", "content": "The earlier answer listed pinpad offline tickets."},
                {"role": "user", "content": "what about those for June"},
            ],
            now=NOW,
        )

        payload = contract.to_dict()
        self.assertTrue(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertEqual(payload["prior_topic"]["source"], "conversation_entities")
        self.assertEqual(payload["prior_topic"]["issue_or_category"], "payment terminal")
        self.assertEqual(payload["prior_topic"]["store_id"], "2012")
        self.assertEqual(payload["prior_topic"]["semantic_labels"], ["pinpad offline", "payment terminal"])
        self.assertEqual(payload["month_windows"][0]["start_date"], "2026-06-01")


if __name__ == "__main__":
    unittest.main()
