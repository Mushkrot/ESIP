"""Universal Chat helper tests for Project Phase 6H result-set follow-ups.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_universal_followup_result_set_assist.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.routers import universal
from app.services import ai_dispatcher_runtime
from app.services.ai_dispatcher_planner import DispatcherMode


def _result_set() -> dict:
    return {
        "memory_id": "mem-universal-6h",
        "turn_id": "turn-prior",
        "trace_id": "trace-prior",
        "requested_store_ids": ["2012", "2002"],
        "criteria": ["internet", "speed", "connection"],
        "ticket_ids": [950052, 950051, 950053],
        "displayed_ticket_ids": [950052],
        "counts": {"ticket_ids": 3, "displayed_ticket_ids": 1, "ticket_rows": 3},
        "tickets": [
            {
                "ticket_id": 950052,
                "subject": "Internet speed degraded",
                "store_id": "2012",
                "status": "pending",
                "created_at": "2026-07-03T14:00:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET TEXT",
            },
            {
                "ticket_id": 950051,
                "subject": "Connection drops",
                "store_id": "2012",
                "status": "solved",
                "created_at": "2026-06-18T09:30:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET TEXT",
            },
            {
                "ticket_id": 950053,
                "subject": "Internet outage",
                "store_id": "2002",
                "status": "open",
                "created_at": "2026-07-05T16:45:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET TEXT",
            },
        ],
    }


class UniversalFollowupResultSetAssistTest(unittest.TestCase):
    def test_disabled_config_does_not_enable_result_set_refinement(self):
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig()
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="only the open ones for store 2002",
            routing_question="only the open ones for store 2002",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )

        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertFalse(ai_dispatcher_runtime.followup_result_set_refinement_enabled(disabled))

    def test_enabled_refinement_contract_builds_deterministic_answer(self):
        enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.ASSISTIVE,
            assistive_task_classes=("followup_result_set_refinement",),
        )
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="only the open ones for store 2002",
            routing_question="only the open ones for store 2002",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )
        answer = universal._build_followup_result_set_refinement_answer(
            assist_payload=payload,
            result_set=_result_set(),
            answer_language="English",
        )

        self.assertTrue(ai_dispatcher_runtime.followup_result_set_refinement_enabled(enabled))
        self.assertEqual(payload["request_kind"], "result_set_refinement")
        self.assertEqual(payload["selected_ticket_ids"], [950053])
        self.assertIsNotNone(answer)
        self.assertIn("saved previous result set", answer or "")
        self.assertIn("#950053", answer or "")
        self.assertNotIn("#950052", answer or "")
        self.assertNotIn("RAW TICKET TEXT", answer or "")

    def test_omitted_contract_builds_displayed_vs_omitted_answer(self):
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="which tickets did you omit and why?",
            routing_question="which tickets did you omit and why?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )
        answer = universal._build_followup_result_set_refinement_answer(
            assist_payload=payload,
            result_set=_result_set(),
            answer_language="English",
        )

        self.assertEqual(payload["request_kind"], "omitted_result_set")
        self.assertIn("#950052", answer or "")
        self.assertIn("#950051", answer or "")
        self.assertIn("#950053", answer or "")
        self.assertIn("Displayed", answer or "")
        self.assertIn("Omitted", answer or "")

    def test_missing_result_set_fails_closed(self):
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="only the open ones",
            routing_question="only the open ones",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="",
            result_set=None,
        )

        self.assertFalse(payload["accepted"])
        self.assertIn("missing_result_set", payload["reason_codes"])
        self.assertIsNone(
            universal._build_followup_result_set_refinement_answer(
                assist_payload=payload,
                result_set=None,
                answer_language="English",
            )
        )

    def test_enabled_ticket_reference_contract_builds_bounded_answer(self):
        enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.ASSISTIVE,
            assistive_task_classes=("followup_ticket_reference",),
        )
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )
        lookup_result = {
            "requested_ids": [950052],
            "found_ids": [950052],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950052,
                    "subject": "Internet speed degraded",
                    "store_id": "2012",
                    "status": "pending",
                    "created_at": "2026-07-03",
                    "issue_category": "Network",
                    "full_text": "RAW TICKET TEXT",
                }
            ],
            "cap": 1,
            "truncated": False,
        }
        answer = universal._build_followup_ticket_reference_answer(
            assist_payload=payload,
            lookup_result=lookup_result,
            result_set=_result_set(),
            answer_language="English",
        )
        summary = universal._summarize_followup_ticket_reference_lookup(lookup_result)

        self.assertTrue(ai_dispatcher_runtime.followup_ticket_reference_enabled(enabled))
        self.assertEqual(payload["request_kind"], "ticket_reference_lookup")
        self.assertEqual(payload["target_ticket_id"], 950052)
        self.assertEqual(payload["lookup_cap"], 1)
        self.assertEqual(summary["requested_ids"], [950052])
        self.assertEqual(summary["found_ids"], [950052])
        self.assertEqual(summary["cap"], 1)
        self.assertTrue(summary["bounded_ticket_lookup"])
        self.assertIn("saved previous result set", answer or "")
        self.assertIn("#950052", answer or "")
        self.assertIn("cap=1", answer or "")
        self.assertNotIn("RAW TICKET TEXT", answer or "")

    def test_disabled_ticket_reference_config_does_not_enable_lookup(self):
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.ASSISTIVE,
            assistive_task_classes=("followup_result_set_refinement",),
        )
        payload = universal._build_followup_result_set_assist_payload(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )

        self.assertEqual(payload["request_kind"], "ticket_reference_lookup")
        self.assertFalse(ai_dispatcher_runtime.followup_ticket_reference_enabled(disabled))

    def test_shadow_helper_emits_metadata_without_answer_or_external_calls(self):
        configured = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.PRIMARY_FOR_ELIGIBLE,
            shadow_task_classes=("followup_result_set_refinement",),
        )
        forbidden = AssertionError("result-set refinement shadow must not execute this path")
        counters = [
            mock.patch.object(ai_dispatcher_runtime, "_get_model", side_effect=forbidden),
            mock.patch.object(universal.bigquery, "bulk_lookup_ticket_ids", side_effect=forbidden),
            mock.patch.object(universal.vertex_search, "search_kb", side_effect=forbidden),
            mock.patch.object(universal.vertex_search, "search_tickets", side_effect=forbidden),
            mock.patch.object(universal, "_run_drive_exact", side_effect=forbidden),
            mock.patch.object(universal, "_run_store_zendesk_history", side_effect=forbidden),
            mock.patch.object(universal.ai_dispatcher_result_sets, "save_result_set", side_effect=forbidden),
            mock.patch.object(universal, "_build_followup_result_set_refinement_answer", side_effect=forbidden),
        ]
        started = [patcher.start() for patcher in counters]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(counters)])

        shadow = universal._build_followup_result_set_refinement_shadow_payload(
            latest_question="only the open ones for store 2002",
            routing_question="only the open ones for store 2002",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )

        self.assertTrue(ai_dispatcher_runtime.followup_result_set_refinement_shadow_enabled(configured))
        self.assertTrue(shadow["accepted"])
        self.assertFalse(shadow["answer_affecting"])
        self.assertEqual(shadow["selected_count"], 1)
        self.assertEqual([item.call_count for item in started], [0, 0, 0, 0, 0, 0, 0, 0])
        self.assertNotIn("RAW TICKET TEXT", repr(shadow))
        self.assertNotIn("950053", repr(shadow))

    def test_shadow_helper_records_missing_and_incompatible_rejections(self):
        missing = universal._build_followup_result_set_refinement_shadow_payload(
            rejection_reason="missing_result_set"
        )
        incompatible = universal._build_followup_result_set_refinement_shadow_payload(
            rejection_reason="incompatible_route"
        )

        self.assertFalse(missing["accepted"])
        self.assertIn("missing_result_set", missing["reason_codes"])
        self.assertFalse(incompatible["accepted"])
        self.assertIn("incompatible_route", incompatible["reason_codes"])
        self.assertTrue(missing["rejected"])
        self.assertFalse(incompatible["answer_affecting"])

    def test_ticket_reference_shadow_resolves_target_without_lookup_or_answer(self):
        configured = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.PRIMARY_FOR_ELIGIBLE,
            shadow_task_classes=("followup_ticket_reference",),
        )
        forbidden = AssertionError("ticket-reference shadow must not execute this path")
        counters = [
            mock.patch.object(ai_dispatcher_runtime, "_get_model", side_effect=forbidden),
            mock.patch.object(universal.bigquery, "bulk_lookup_ticket_ids", side_effect=forbidden),
            mock.patch.object(universal.vertex_search, "search_kb", side_effect=forbidden),
            mock.patch.object(universal.vertex_search, "search_tickets", side_effect=forbidden),
            mock.patch.object(universal, "_run_drive_exact", side_effect=forbidden),
            mock.patch.object(universal, "_run_store_zendesk_history", side_effect=forbidden),
            mock.patch.object(universal.ai_dispatcher_result_sets, "save_result_set", side_effect=forbidden),
            mock.patch.object(universal, "_build_followup_ticket_reference_answer", side_effect=forbidden),
        ]
        started = [patcher.start() for patcher in counters]
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(counters)])

        shadow = universal._build_followup_ticket_reference_shadow_payload(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )

        self.assertTrue(ai_dispatcher_runtime.followup_ticket_reference_shadow_enabled(configured))
        self.assertFalse(ai_dispatcher_runtime.followup_ticket_reference_enabled(configured))
        self.assertTrue(shadow["accepted"])
        self.assertEqual(shadow["target_ticket_id"], 950052)
        self.assertEqual(shadow["target_source"], "displayed_ordinal_first")
        self.assertFalse(shadow["lookup_performed"])
        self.assertFalse(shadow["answer_affecting"])
        self.assertEqual([item.call_count for item in started], [0, 0, 0, 0, 0, 0, 0, 0])
        self.assertNotIn("answer", shadow)
        self.assertNotIn("cards", shadow)
        self.assertNotIn("sources", shadow)
        self.assertNotIn("bq_sql", shadow)
        self.assertNotIn("RAW TICKET TEXT", repr(shadow))

    def test_two_followup_shadow_classes_remain_independent(self):
        ticket_payload = universal._build_followup_result_set_assist_payload(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )
        refinement_payload = universal._build_followup_result_set_assist_payload(
            latest_question="only the open ones for store 2003",
            routing_question="only the open ones for store 2003",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        )

        ticket_on_ticket = universal.followup_result_set_assist.build_ticket_reference_shadow_observation(
            ticket_payload
        )
        refinement_on_ticket = (
            universal.followup_result_set_assist.build_result_set_refinement_shadow_observation(
                ticket_payload
            )
        )
        ticket_on_refinement = universal.followup_result_set_assist.build_ticket_reference_shadow_observation(
            refinement_payload
        )
        refinement_on_refinement = (
            universal.followup_result_set_assist.build_result_set_refinement_shadow_observation(
                refinement_payload
            )
        )

        self.assertTrue(ticket_on_ticket["accepted"])
        self.assertFalse(refinement_on_ticket["accepted"])
        self.assertFalse(ticket_on_refinement["accepted"])
        self.assertTrue(refinement_on_refinement["accepted"])
        self.assertEqual(ticket_on_ticket["task_class"], "followup_ticket_reference")
        self.assertEqual(refinement_on_refinement["task_class"], "followup_result_set_refinement")


if __name__ == "__main__":
    unittest.main(verbosity=2)
