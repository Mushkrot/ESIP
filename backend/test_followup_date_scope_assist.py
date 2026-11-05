"""Tests for follow-up date scope assist guards."""

from __future__ import annotations

import unittest
from datetime import datetime

from app.services.followup_date_scope_assist import build_followup_date_scope_assist
from app.services.followup_understanding_shadow import build_followup_understanding_shadow


NOW = datetime.fromisoformat("2026-07-07T12:00:00-04:00")


def _shadow_payload(question: str = "i just want the one for July and Jun") -> dict:
    return build_followup_understanding_shadow(
        latest_question=question,
        routing_question=question,
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
            {"role": "user", "content": question},
        ],
        now=NOW,
    ).to_dict()


class FollowupDateScopeAssistTest(unittest.TestCase):
    def test_chat_118_style_shadow_promotes_to_assist_candidate(self):
        payload = _shadow_payload()
        assist = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="I found the degraded internet-speed cases for store 2008.",
            conversation_entities={"trace_id": "trace-prior", "turn_id": "turn-prior"},
            messages=[
                {"role": "assistant", "content": "I found the degraded internet-speed cases for store 2008."},
                {"role": "user", "content": "i just want the one for July and Jun"},
            ],
        ).to_dict()

        self.assertTrue(assist["accepted"])
        self.assertFalse(assist["answer_affecting"])
        self.assertEqual(assist["contract_type"], "followup_date_scope_assist")
        self.assertEqual(
            [(item["start_date"], item["end_date_exclusive"]) for item in assist["month_windows"]],
            [("2026-07-01", "2026-08-01"), ("2026-06-01", "2026-07-01")],
        )
        self.assertTrue(assist["current_month_to_date"])
        self.assertEqual(assist["display_label"], "July 2026 and June 2026")
        self.assertEqual(assist["prior_topic"]["store_id"], "2009")
        self.assertIn("internet speed", assist["prior_topic"]["semantic_labels"])
        self.assertEqual(assist["prior_topic"]["issue_or_category"], "internet speed")

    def test_explicit_day_range_rejected(self):
        payload = _shadow_payload("i just want the one for July 1 to July 2")
        assist = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="i just want the one for July 1 to July 2",
            routing_question="i just want the one for July 1 to July 2",
            previous_assistant_text="Previous result.",
            conversation_entities={"trace_id": "trace-prior", "turn_id": "turn-prior"},
        ).to_dict()

        self.assertFalse(assist["accepted"])
        self.assertIn("shadow_not_emitted", assist["reason_codes"])

    def test_no_prior_context_rejected(self):
        payload = _shadow_payload()
        payload["prior_topic"].pop("prior_trace_id", None)
        payload["prior_topic"].pop("prior_turn_id", None)
        assist = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="",
            conversation_entities=None,
            messages=[{"role": "user", "content": "i just want the one for July and Jun"}],
        ).to_dict()

        self.assertFalse(assist["accepted"])
        self.assertIn("insufficient_prior_context", assist["reason_codes"])

    def test_missing_prior_topic_rejected(self):
        payload = _shadow_payload()
        payload["prior_topic"] = {"source": "messages"}
        assist = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="Previous answer.",
        ).to_dict()

        self.assertFalse(assist["accepted"])
        self.assertIn("missing_prior_topic", assist["reason_codes"])

    def test_fact_check_and_bulk_ticket_followups_rejected(self):
        payload = _shadow_payload()
        fact_check = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="Where did you find that claim? Verify 950053 and 950027.",
            routing_question="Where did you find that claim? Verify 950053 and 950027.",
            previous_assistant_text="Previous answer.",
            conversation_entities={"trace_id": "trace-prior"},
        ).to_dict()
        bulk_lookup = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="check tickets 950012 and 950027",
            routing_question="check tickets 950012 and 950027",
            previous_assistant_text="Previous answer.",
            conversation_entities={"trace_id": "trace-prior"},
        ).to_dict()

        self.assertFalse(fact_check["accepted"])
        self.assertIn("challenge_fact_check_intent", fact_check["reason_codes"])
        self.assertFalse(bulk_lookup["accepted"])
        self.assertIn("bulk_ticket_lookup_intent", bulk_lookup["reason_codes"])

    def test_too_many_month_windows_rejected(self):
        payload = _shadow_payload()
        payload["month_windows"] = payload["month_windows"] + [
            {
                "display_label": "May 2026",
                "start_date": "2026-05-01",
                "end_date_exclusive": "2026-06-01",
            },
            {
                "display_label": "April 2026",
                "start_date": "2026-04-01",
                "end_date_exclusive": "2026-05-01",
            },
        ]
        assist = build_followup_date_scope_assist(
            shadow_payload=payload,
            latest_question="i just want the one for July Jun May and April",
            routing_question="i just want the one for July Jun May and April",
            previous_assistant_text="Previous answer.",
            conversation_entities={"trace_id": "trace-prior"},
        ).to_dict()

        self.assertFalse(assist["accepted"])
        self.assertIn("too_many_month_windows", assist["reason_codes"])


if __name__ == "__main__":
    unittest.main()
