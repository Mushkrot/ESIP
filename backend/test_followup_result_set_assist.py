"""Tests for Project Phase 6H saved result-set follow-up contracts."""

from __future__ import annotations

import unittest

from app.services import followup_result_set_assist


def _result_set() -> dict:
    return {
        "memory_id": "mem-test",
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


class FollowupResultSetAssistTest(unittest.TestCase):
    def test_result_set_refinement_filters_saved_rows(self):
        contract = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones for store 2002",
            routing_question="only the open ones for store 2002",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        answer = followup_result_set_assist.build_result_set_refinement_answer(contract, _result_set())

        self.assertTrue(contract["accepted"])
        self.assertFalse(contract["answer_affecting"])
        self.assertEqual(contract["request_kind"], "result_set_refinement")
        self.assertEqual(contract["filters"]["store_ids"], ["2002"])
        self.assertEqual(contract["filters"]["statuses"], ["open"])
        self.assertEqual(contract["selected_ticket_ids"], [950053])
        self.assertIn("saved previous result set", answer or "")
        self.assertIn("#950053", answer or "")
        self.assertNotIn("#950052", answer or "")
        self.assertNotIn("RAW TICKET TEXT", answer or "")

    def test_omitted_request_distinguishes_displayed_and_omitted_ids(self):
        contract = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="which tickets did you omit and why?",
            routing_question="which tickets did you omit and why?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        answer = followup_result_set_assist.build_result_set_refinement_answer(contract, _result_set())

        self.assertTrue(contract["accepted"])
        self.assertEqual(contract["request_kind"], "omitted_result_set")
        self.assertEqual(contract["displayed_ticket_ids"], [950052])
        self.assertEqual(contract["omitted_ticket_ids"], [950051, 950053])
        self.assertIn("Displayed", answer or "")
        self.assertIn("Omitted", answer or "")
        self.assertIn("#950051", answer or "")
        self.assertIn("#950053", answer or "")
        self.assertNotIn("RAW TICKET TEXT", answer or "")

    def test_ticket_reference_accepts_explicit_ordinal_only(self):
        contract = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        answer = followup_result_set_assist.build_ticket_reference_lookup_answer(
            contract,
            {
                "requested_ids": [950052],
                "found_ids": [950052],
                "missing_ids": [],
                "rows": [_result_set()["tickets"][0]],
                "cap": 1,
                "truncated": False,
            },
            _result_set(),
        )

        self.assertTrue(contract["accepted"])
        self.assertEqual(contract["request_kind"], "ticket_reference_lookup")
        self.assertEqual(contract["target_ticket_id"], 950052)
        self.assertEqual(contract["lookup_cap"], 1)
        self.assertIn("#950052", answer or "")
        self.assertIn("cap=1", answer or "")
        self.assertNotIn("RAW TICKET TEXT", answer or "")

    def test_rejects_missing_context_and_competing_intents(self):
        missing = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones",
            routing_question="only the open ones",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            result_set=None,
        ).to_dict()
        date_scope = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        bulk = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="compare 950051 and 950053",
            routing_question="compare 950051 and 950053",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        fact_check = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="where did you find that claim?",
            routing_question="where did you find that claim?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        resolution = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what fixed the first one?",
            routing_question="what fixed the first one?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        ambiguous = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what about that one?",
            routing_question="what about that one?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()

        self.assertFalse(missing["accepted"])
        self.assertIn("missing_result_set", missing["reason_codes"])
        self.assertFalse(date_scope["accepted"])
        self.assertIn("date_scope_followup_intent", date_scope["reason_codes"])
        self.assertFalse(bulk["accepted"])
        self.assertIn("bulk_ticket_lookup_intent", bulk["reason_codes"])
        self.assertFalse(fact_check["accepted"])
        self.assertIn("challenge_fact_check_intent", fact_check["reason_codes"])
        self.assertFalse(resolution["accepted"])
        self.assertIn("ticket_resolution_lookup_intent", resolution["reason_codes"])
        self.assertFalse(ambiguous["accepted"])
        self.assertIn("ambiguous_ticket_reference", ambiguous["reason_codes"])

    def test_resolved_status_filter_is_not_resolution_lookup(self):
        contract = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the resolved ones",
            routing_question="only the resolved ones",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()

        self.assertTrue(contract["accepted"])
        self.assertEqual(contract["request_kind"], "result_set_refinement")
        self.assertEqual(contract["filters"]["statuses"], ["solved"])
        self.assertEqual(contract["selected_ticket_ids"], [950051])

    def test_refinement_shadow_observation_is_compact_and_non_answer_affecting(self):
        refinement = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones for store 2002",
            routing_question="only the open ones for store 2002",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        omitted = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="which tickets did you omit?",
            routing_question="which tickets did you omit?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()

        refinement_shadow = followup_result_set_assist.build_result_set_refinement_shadow_observation(
            refinement
        )
        omitted_shadow = followup_result_set_assist.build_result_set_refinement_shadow_observation(omitted)

        self.assertTrue(refinement_shadow["accepted"])
        self.assertTrue(refinement_shadow["enabled_as_shadow"])
        self.assertFalse(refinement_shadow["answer_affecting"])
        self.assertEqual(refinement_shadow["selected_count"], 1)
        self.assertEqual(refinement_shadow["filter_names"], ["statuses", "store_ids"])
        self.assertEqual(refinement_shadow["evidence_limits"]["ticket_count"], 3)
        self.assertTrue(omitted_shadow["accepted"])
        self.assertEqual(omitted_shadow["selected_count"], 2)
        self.assertNotIn("selected_ticket_ids", refinement_shadow)
        self.assertNotIn("memory_id", repr(refinement_shadow))
        self.assertNotIn("RAW TICKET TEXT", repr(refinement_shadow))

    def test_refinement_shadow_records_three_safe_rejections(self):
        missing = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones",
            routing_question="only the open ones",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            result_set=None,
        ).to_dict()
        ticket_reference = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()
        unsupported = followup_result_set_assist.build_followup_result_set_assist(
            latest_question="more details",
            routing_question="more details",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="Prior result.",
            result_set=_result_set(),
        ).to_dict()

        traces = [
            followup_result_set_assist.build_result_set_refinement_shadow_observation(payload)
            for payload in (missing, ticket_reference, unsupported)
        ]

        self.assertEqual([trace["accepted"] for trace in traces], [False, False, False])
        self.assertIn("missing_result_set", traces[0]["reason_codes"])
        self.assertEqual(traces[1]["reason_codes"], ["different_shadow_class_ticket_reference"])
        self.assertIn("unsupported_result_set_followup", traces[2]["reason_codes"])
        self.assertTrue(all(trace["rejected"] for trace in traces))
        self.assertTrue(all(trace["answer_affecting"] is False for trace in traces))

    def test_ticket_reference_shadow_resolves_visible_ordinals_only(self):
        result_set = _result_set()
        result_set["displayed_ticket_ids"] = [950052, 950051, 950053, 950051]
        resolved: dict[str, dict] = {}
        for ordinal in ("first", "second", "third", "last"):
            contract = followup_result_set_assist.build_followup_result_set_assist(
                latest_question=f"what about the {ordinal} one?",
                routing_question=f"what about the {ordinal} one?",
                semantic_frame={"target_context": "prior_turn"},
                previous_assistant_text="Prior result.",
                result_set=result_set,
            ).to_dict()
            resolved[ordinal] = (
                followup_result_set_assist.build_ticket_reference_shadow_observation(contract)
            )

        self.assertEqual(resolved["first"]["target_ticket_id"], 950052)
        self.assertEqual(resolved["second"]["target_ticket_id"], 950051)
        self.assertEqual(resolved["third"]["target_ticket_id"], 950053)
        self.assertEqual(resolved["last"]["target_ticket_id"], 950053)
        self.assertTrue(all(item["accepted"] for item in resolved.values()))
        self.assertTrue(all(item["lookup_performed"] is False for item in resolved.values()))
        self.assertTrue(all(item["answer_affecting"] is False for item in resolved.values()))
        self.assertNotIn("RAW TICKET TEXT", repr(resolved))
        self.assertNotIn("previous_assistant_text", repr(resolved))

    def test_ticket_reference_shadow_rejects_ambiguity_scope_and_hidden_ordinals(self):
        cases = {
            "ambiguous": ("what about that one?", _result_set()),
            "hidden_second": ("what about the second one?", _result_set()),
            "out_of_range": ("what about the fourth one?", _result_set()),
            "explicit_ticket": ("what about ticket 950012?", _result_set()),
            "missing_set": ("what about the first one?", None),
            "date": ("what about the first one in July?", _result_set()),
            "resolution": ("what fixed the first one?", _result_set()),
            "procedure": ("how do I fix the first one?", _result_set()),
            "challenge": ("where is the evidence for the first one?", _result_set()),
            "bulk": ("compare 950052 and 950053", _result_set()),
        }
        observations: dict[str, dict] = {}
        for name, (question, result_set) in cases.items():
            contract = followup_result_set_assist.build_followup_result_set_assist(
                latest_question=question,
                routing_question=question,
                semantic_frame={"target_context": "prior_turn"},
                previous_assistant_text="Prior result." if result_set else "",
                result_set=result_set,
            ).to_dict()
            observations[name] = (
                followup_result_set_assist.build_ticket_reference_shadow_observation(contract)
            )

        self.assertIn("ambiguous_ticket_reference", observations["ambiguous"]["reason_codes"])
        self.assertIn("ordinal_out_of_range", observations["hidden_second"]["reason_codes"])
        self.assertIn("ordinal_out_of_range", observations["out_of_range"]["reason_codes"])
        self.assertIn("explicit_ticket_lookup_intent", observations["explicit_ticket"]["reason_codes"])
        self.assertIn("missing_result_set", observations["missing_set"]["reason_codes"])
        self.assertIn("date_scope_followup_intent", observations["date"]["reason_codes"])
        self.assertIn("ticket_resolution_lookup_intent", observations["resolution"]["reason_codes"])
        self.assertIn("procedure_lookup_intent", observations["procedure"]["reason_codes"])
        self.assertIn("challenge_fact_check_intent", observations["challenge"]["reason_codes"])
        self.assertIn("bulk_ticket_lookup_intent", observations["bulk"]["reason_codes"])
        self.assertTrue(all(not item["accepted"] for item in observations.values()))
        self.assertTrue(all(item["target_ticket_id"] is None for item in observations.values()))


if __name__ == "__main__":
    unittest.main()
