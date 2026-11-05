"""Universal Chat integration tests for follow-up date scope assist.

Run from project root:
scripts/backend_python.sh backend/test_universal_followup_date_scope_assist.py
"""

from __future__ import annotations

import unittest

from app.routers import universal
from app.services import ai_dispatcher_runtime


class UniversalFollowupDateScopeAssistTest(unittest.TestCase):
    def _shadow_payload(self) -> dict:
        return universal._build_followup_understanding_shadow_payload(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={
                "target_context": "prior_turn",
                "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
                "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
            },
            previous_assistant_text="Prior answer for store 2009 internet speed tickets.",
            conversation_entities={
                "trace_id": "trace-prior",
                "turn_id": "turn-prior",
                "semantic_labels": ["internet speed", "connection drops"],
                "store_id": "2009",
            },
            messages=[
                {"role": "assistant", "content": "Prior answer for store 2009 internet speed tickets."},
                {"role": "user", "content": "i just want the one for July and Jun"},
            ],
        )

    def test_disabled_config_does_not_make_shadow_answer_affecting(self):
        shadow = self._shadow_payload()
        assist = universal._build_followup_date_scope_assist_payload(
            shadow_payload=shadow,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="Prior answer for store 2009 internet speed tickets.",
            conversation_entities={"trace_id": "trace-prior"},
            messages=[
                {"role": "assistant", "content": "Prior answer for store 2009 internet speed tickets."},
                {"role": "user", "content": "i just want the one for July and Jun"},
            ],
        )
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig()

        self.assertTrue(shadow["emitted"])
        self.assertFalse(shadow["answer_affecting"])
        self.assertTrue(assist["accepted"])
        self.assertFalse(assist["answer_affecting"])
        self.assertFalse(ai_dispatcher_runtime.followup_date_scope_assist_enabled(disabled))

    def test_enabled_contract_builds_bounded_sql_and_answer_for_june_july(self):
        shadow = self._shadow_payload()
        assist = universal._build_followup_date_scope_assist_payload(
            shadow_payload=shadow,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="Prior answer for store 2009 internet speed tickets.",
            conversation_entities={"trace_id": "trace-prior"},
            messages=[
                {"role": "assistant", "content": "Prior answer for store 2009 internet speed tickets."},
                {"role": "user", "content": "i just want the one for July and Jun"},
            ],
        )
        sql = universal._followup_date_scope_assist_bq_sql(assist)
        answer = universal._build_followup_date_scope_answer(
            assist_payload=assist,
            rows=[
                {
                    "id": 950052,
                    "subject": "Internet speed degraded",
                    "store_id": "2009",
                    "status": "open",
                    "issue_category": "Network",
                    "created_at": "2026-07-03T14:00:00+00:00",
                    "full_text": "Internet speed degraded at store 2009.",
                },
                {
                    "id": 950051,
                    "subject": "Connection drops",
                    "store_id": "2009",
                    "status": "solved",
                    "issue_category": "Network",
                    "created_at": "2026-06-18T09:30:00+00:00",
                    "full_text": "Connection drops and internet speed issues.",
                },
            ],
        )

        self.assertIsNotNone(sql)
        self.assertIn("DATE '2026-07-01'", sql)
        self.assertIn("DATE '2026-08-01'", sql)
        self.assertIn("DATE '2026-06-01'", sql)
        self.assertIn("DATE '2026-07-01'", sql)
        self.assertIn("store_id = '2009'", sql)
        self.assertIn("internet speed", sql)
        self.assertIsNotNone(answer)
        self.assertIn("July 2026 and June 2026", answer)
        self.assertIn("not as July 1-2 only", answer)
        self.assertIn("#950052", answer)
        self.assertIn("#950051", answer)

    def test_insufficient_context_does_not_build_bounded_sql(self):
        shadow = self._shadow_payload()
        shadow["prior_topic"] = {"source": "messages"}
        assist = universal._build_followup_date_scope_assist_payload(
            shadow_payload=shadow,
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            previous_assistant_text="",
            conversation_entities=None,
            messages=[{"role": "user", "content": "i just want the one for July and Jun"}],
        )

        self.assertFalse(assist["accepted"])
        self.assertIsNone(universal._followup_date_scope_assist_bq_sql(assist))

    def test_primary_dispatcher_trace_requires_deterministic_answer(self):
        trace = {
            "mode": "primary_for_eligible",
            "accepted": True,
            "answer_affecting": True,
            "assistive_task_class": ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
            "reason_codes": ["primary_prior_result_comparison"],
        }
        flags: dict = {}

        positive = universal._suppress_ai_dispatcher_trace_without_deterministic_answer(
            trace,
            result_set_comparison_answer="Deterministic comparison answer.",
            policy_flags=flags,
        )
        suppressed = universal._suppress_ai_dispatcher_trace_without_deterministic_answer(
            trace,
            result_set_comparison_answer="",
        )

        self.assertTrue(positive["answer_affecting"])
        self.assertEqual(flags["ai_dispatcher_primary_answer_affecting"], "prior_result_comparison")
        self.assertFalse(suppressed["answer_affecting"])
        self.assertIn("primary_no_deterministic_answer", suppressed["reason_codes"])
        self.assertIn("primary_no_deterministic_result_set_comparison", suppressed["reason_codes"])

    def test_unselected_followup_candidate_cannot_claim_answer_provenance(self):
        source, answer = universal._select_deterministic_answer(
            [
                ("global_event_discovery", "Selected event answer."),
                ("followup_date_scope", "Unused follow-up answer."),
            ]
        )
        trace = {
            "mode": "primary_for_eligible",
            "accepted": True,
            "answer_affecting": True,
            "assistive_task_class": ai_dispatcher_runtime.ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE,
        }

        resolved = universal._suppress_ai_dispatcher_trace_without_deterministic_answer(
            trace,
            followup_date_scope_answer=(answer if source == "followup_date_scope" else None),
        )

        self.assertEqual(source, "global_event_discovery")
        self.assertEqual(answer, "Selected event answer.")
        self.assertFalse(resolved["answer_affecting"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
