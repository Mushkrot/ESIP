"""Universal Chat follow-up shadow integration tests.

Run from project root:
scripts/backend_python.sh backend/test_universal_followup_shadow.py
"""

from __future__ import annotations

import unittest

from app.routers import universal


class UniversalFollowupShadowIntegrationTest(unittest.TestCase):
    def test_chat_118_style_turn_adds_private_shadow_without_route_mutation(self):
        routing_question = "i just want the one for July and Jun"
        selected_executor = "phase1_tools"
        tools_before_policy = ["search_tickets"]
        semantic_frame = {
            "target_context": "prior_turn",
            "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
            "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
        }
        conversation_entities = {
            "trace_id": "trace-prior",
            "turn_id": "turn-prior",
            "semantic_labels": ["internet speed", "connection drops"],
            "store_id": "2009",
        }
        messages = [
            {"role": "assistant", "content": "Prior result for store 2008 internet speed."},
            {"role": "user", "content": routing_question},
        ]

        payload = universal._build_followup_understanding_shadow_payload(
            latest_question=routing_question,
            routing_question=routing_question,
            semantic_frame=semantic_frame,
            previous_assistant_text="Prior result for store 2008 internet speed.",
            conversation_entities=conversation_entities,
            messages=messages,
        )
        trace_like = {
            "routing_question": routing_question,
            "tools_before_policy": tools_before_policy,
            "policy_flags": {"followup_understanding_shadow": payload},
            "selected_executor": selected_executor,
        }

        self.assertTrue(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertEqual(payload["contract_type"], "followup_understanding_shadow")
        self.assertEqual(len(payload["month_windows"]), 2)
        self.assertEqual(payload["prior_topic"]["store_id"], "2009")
        self.assertEqual(trace_like["routing_question"], routing_question)
        self.assertEqual(trace_like["tools_before_policy"], ["search_tickets"])
        self.assertEqual(trace_like["selected_executor"], "phase1_tools")

    def test_no_context_shadow_payload_carries_private_reason(self):
        payload = universal._build_followup_understanding_shadow_payload(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            conversation_entities=None,
            messages=[{"role": "user", "content": "i just want the one for July and Jun"}],
        )

        self.assertFalse(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertIn("insufficient_prior_context", payload["reason_codes"])

    def test_self_contained_ticket_retrieval_is_not_promoted_as_followup(self):
        payload = universal._build_followup_understanding_shadow_payload(
            latest_question="can you find tickets where servers were reimaged in June",
            routing_question="find tickets where servers were reimaged in June",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Earlier answer about a different server issue.",
            conversation_entities={"semantic_labels": ["server outage"]},
            messages=[
                {"role": "assistant", "content": "Earlier answer about a different server issue."},
                {"role": "user", "content": "can you find tickets where servers were reimaged in June"},
            ],
        )

        self.assertFalse(payload["emitted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertIn("self_contained_ticket_retrieval", payload["reason_codes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
