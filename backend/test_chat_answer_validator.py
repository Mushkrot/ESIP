"""Automatic Universal Chat answer-validator contract tests.

Run from project root:
scripts/backend_python.sh backend/test_chat_answer_validator.py
"""

from __future__ import annotations

import unittest

from app.services import chat_answer_validator


class _Usage:
    prompt_token_count = 11
    candidates_token_count = 7
    thoughts_token_count = 0


class _Response:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = _Usage()


class _FakeModel:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.last_prompt = ""
        self.last_config = None

    def generate_content(self, prompt, generation_config=None):
        self.calls += 1
        self.last_prompt = prompt
        self.last_config = generation_config
        return _Response(self.text)


class _RecordingFactory:
    def __init__(self, text: str):
        self.text = text
        self.model_ids: list[str] = []
        self.instances: list[_FakeModel] = []

    def __call__(self, model_id: str):
        self.model_ids.append(model_id)
        fake = _FakeModel(self.text)
        self.instances.append(fake)
        return fake


class ChatAnswerValidatorTest(unittest.TestCase):
    def setUp(self):
        self._orig_model = chat_answer_validator._model
        self._orig_model_factory = chat_answer_validator._model_factory
        self._orig_check_cost_gate = chat_answer_validator.check_cost_gate
        self._orig_record_estimate = chat_answer_validator.record_estimate
        self._orig_record_usage = chat_answer_validator.record_usage
        self._orig_check_divergence = chat_answer_validator.check_divergence
        self._orig_new_correlation_id = chat_answer_validator.new_correlation_id
        self._orig_validator_model = chat_answer_validator.settings.universal_validator_model
        chat_answer_validator.check_cost_gate = lambda *args, **kwargs: None
        chat_answer_validator.record_estimate = lambda *args, **kwargs: None
        chat_answer_validator.record_usage = lambda *args, **kwargs: 0.001
        chat_answer_validator.check_divergence = lambda *args, **kwargs: None
        chat_answer_validator.new_correlation_id = lambda: "validator-test-corr"

    def tearDown(self):
        chat_answer_validator._model = self._orig_model
        chat_answer_validator._model_factory = self._orig_model_factory
        chat_answer_validator.check_cost_gate = self._orig_check_cost_gate
        chat_answer_validator.record_estimate = self._orig_record_estimate
        chat_answer_validator.record_usage = self._orig_record_usage
        chat_answer_validator.check_divergence = self._orig_check_divergence
        chat_answer_validator.new_correlation_id = self._orig_new_correlation_id
        chat_answer_validator.settings.universal_validator_model = self._orig_validator_model

    def test_valid_json_contract_and_telemetry(self):
        fake = _FakeModel(
            """{
              "quality": "suspect",
              "confidence": 0.73,
              "risk_classes": ["false_no_data", "stale_data"],
              "recommended_action": "zendesk_fallback",
              "answer_policy": "caveated",
              "reason_short": "BQ was stale and primary result was empty.",
              "user_status": "Checking another source...",
              "rewrite_from_supplied_evidence": null
            }"""
        )
        chat_answer_validator._model = fake

        result, telemetry = chat_answer_validator.validate_answer_candidate_with_telemetry(
            {"candidate_answer": "No tickets were found.", "risk_flags": ["stale_data"]},
        )

        self.assertEqual(result.quality, "suspect")
        self.assertEqual(result.risk_classes, ["false_no_data", "stale_data"])
        self.assertEqual(result.recommended_action, "zendesk_fallback")
        self.assertEqual(result.answer_policy, "caveated")
        self.assertTrue(result.validator_enabled)
        self.assertEqual(fake.calls, 1)
        self.assertIn("Answer-candidate bundle", fake.last_prompt)
        self.assertEqual(telemetry["correlation_id"], "validator-test-corr")
        self.assertEqual(telemetry["thinking_budget"], 0)
        self.assertIsNone(telemetry["thinking_level"])
        self.assertEqual(telemetry["max_output_tokens"], chat_answer_validator.MAX_VALIDATOR_OUTPUT_TOKENS)
        self.assertEqual(telemetry["actual_thinking_tokens"], 0)
        self.assertEqual(telemetry["recommended_action"], "zendesk_fallback")

    def test_candidate_validator_model_routes_through_factory(self):
        factory = _RecordingFactory(
            """{
              "quality": "good",
              "confidence": 0.91,
              "risk_classes": [],
              "recommended_action": "none",
              "answer_policy": "normal",
              "reason_short": "supported",
              "user_status": null,
              "rewrite_from_supplied_evidence": null
            }"""
        )
        chat_answer_validator.settings.universal_validator_model = "gemini-3.1-flash-lite"
        chat_answer_validator._model_factory = factory
        orig_from_dict = chat_answer_validator.GenerationConfig.from_dict
        chat_answer_validator.GenerationConfig.from_dict = staticmethod(lambda payload: payload)

        try:
            result, telemetry = chat_answer_validator.validate_answer_candidate_with_telemetry(
                {"candidate_answer": "Supported answer.", "risk_flags": ["weak_evidence"]},
            )
        finally:
            chat_answer_validator.GenerationConfig.from_dict = orig_from_dict

        self.assertEqual(result.quality, "good")
        self.assertEqual(factory.model_ids, ["gemini-3.1-flash-lite"])
        self.assertEqual(telemetry["model"], "gemini-3.1-flash-lite")
        self.assertEqual(telemetry["model_surface"], "answer_validator")
        self.assertEqual(telemetry["model_family"], "gemini_3")
        self.assertEqual(telemetry["max_output_tokens"], chat_answer_validator.MAX_VALIDATOR_OUTPUT_TOKENS)
        self.assertIsNone(telemetry["thinking_budget"])
        self.assertEqual(telemetry["thinking_level"], "none")

    def test_invalid_json_degrades_to_safe_caveat(self):
        result = chat_answer_validator._normalize_validator_data(
            chat_answer_validator._parse_validator_json("not json"),
        )

        self.assertEqual(result.quality, "suspect")
        self.assertEqual(result.recommended_action, "final_with_caveat")
        self.assertEqual(result.answer_policy, "caveated")
        self.assertIn("validator_error", result.risk_classes)

    def test_unknown_values_are_normalized_to_safe_defaults(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "perfect",
            "confidence": 7,
            "risk_classes": ["false_no_data", "made_up"],
            "recommended_action": "send_email",
            "answer_policy": "trust_me",
            "reason_short": "bad values",
        })

        self.assertEqual(result.quality, "suspect")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.risk_classes, ["false_no_data"])
        self.assertEqual(result.recommended_action, "final_with_caveat")
        self.assertEqual(result.answer_policy, "caveated")

    def test_unsafe_rewrite_is_sanitized(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "unsafe",
            "confidence": 0.9,
            "risk_classes": ["raw_artifact"],
            "recommended_action": "rewrite_with_existing_sources",
            "answer_policy": "caveated",
            "reason_short": "strip artifacts",
            "rewrite_from_supplied_evidence": "Clean answer.\n\nTool Call:\nsecret\n\nDEBUG: no\nSOURCES_JSON: {}",
        })

        self.assertEqual(result.rewrite_from_supplied_evidence, "Clean answer.")

    def test_rewrite_is_dropped_for_non_rewrite_action(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.4,
            "risk_classes": ["weak_evidence"],
            "recommended_action": "rerun_bq",
            "answer_policy": "partial",
            "reason_short": "needs data",
            "rewrite_from_supplied_evidence": "This should not be used yet.",
        })

        self.assertIsNone(result.rewrite_from_supplied_evidence)

    def test_kill_switch_skips_model_without_disabling_contract(self):
        fake = _FakeModel('{"quality": "unsafe"}')
        chat_answer_validator._model = fake

        result, telemetry = chat_answer_validator.validate_answer_candidate_with_telemetry(
            {"candidate_answer": "anything"},
            enabled=False,
        )

        self.assertEqual(fake.calls, 0)
        self.assertFalse(result.validator_enabled)
        self.assertEqual(result.quality, "good")
        self.assertEqual(result.recommended_action, "none")
        self.assertEqual(telemetry["skipped_reason"], "disabled_by_config")
        self.assertEqual(telemetry["thinking_budget"], 0)

    def test_routine_supported_answer_skips_ai_validation(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="The top issue was **Server --> Database** with 3 tickets.",
            latest_question="What were the top issues yesterday?",
            tools=["run_bq_query"],
            bq_rows=[{"issue_category": "Server --> Database", "ticket_count": 3}],
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertFalse(result.should_validate)
        self.assertEqual(result.risk_classes, [])
        self.assertIn("analytics", result.source_families)

    def test_risk_detector_maps_false_no_data_and_tool_error(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="No matching tickets were found.",
            latest_question="How many tickets were opened today?",
            tools=["run_bq_query"],
            bq_rows="Query error: Syntax error near WHERE",
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("tool_error_as_absence", result.risk_classes)
        self.assertIn("false_no_data", result.risk_classes)

    def test_risk_detector_maps_stale_current_no_data(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="No tickets were found today.",
            latest_question="Show tickets logged today.",
            tools=["run_bq_query"],
            bq_rows=[],
            policy_flags={"bq_stale": True},
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("stale_data", result.risk_classes)

    def test_risk_detector_maps_raw_artifacts(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Answer\n\nSOURCES_JSON: {}",
            latest_question="What should I do?",
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("raw_artifact", result.risk_classes)

    def test_risk_detector_maps_source_family_mismatch(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="The procedure says to reboot the router.",
            latest_question="Find example tickets where FiberLink dispatched a technician.",
            tools=["search_knowledge_base"],
            kb_results=[{"title": "Router reboot"}],
            expected_source_family="tickets",
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("source_family_mismatch", result.risk_classes)

    def test_risk_detector_maps_context_loss(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Those tickets are unrelated.",
            latest_question="What about those same issues?",
            tools=["search_similar_tickets"],
            similar_tickets=[],
            conversation_entities=None,
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("context_loss", result.risk_classes)

    def test_risk_detector_maps_unsupported_strong_claim(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Ticket #950033 breached SLA because no public agent reply was sent.",
            latest_question="Did this ticket breach SLA?",
            tools=["run_bq_query"],
            bq_rows=[{"id": 950033, "status": "open"}],
            available_fields=["id", "status"],
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("unsupported_strong_claim", result.risk_classes)

    def test_direct_ticket_context_counts_as_ticket_evidence(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer=(
                "Ticket #950050 is currently open and the latest note says the store "
                "is waiting for IT to confirm the next step."
            ),
            latest_question="How to resolved this?",
            direct_ticket_context=True,
            risk_hints=["weak_evidence"],
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertEqual(context["counts"]["direct_ticket_context"], 1)
        self.assertIn("tickets", result.source_families)
        self.assertFalse(result.should_validate)
        self.assertNotIn("weak_evidence", result.risk_classes)

    def test_direct_ticket_context_keeps_unsupported_strong_claim_risk(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Ticket #950050 breached SLA because no public agent reply was sent.",
            latest_question="Did this ticket breach SLA?",
            direct_ticket_context=True,
            available_fields=["id", "status"],
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertIn("tickets", result.source_families)
        self.assertTrue(result.should_validate)
        self.assertIn("unsupported_strong_claim", result.risk_classes)

    def test_risk_detector_maps_scope_and_weak_evidence_hints(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="This is a confirmed repeated issue across all stores.",
            latest_question="Compare store 2008 to other stores.",
            risk_hints=["evidence_mismatch", "denominator_drift", "weak_evidence"],
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertIn("evidence_mismatch", result.risk_classes)
        self.assertIn("denominator_drift", result.risk_classes)
        self.assertIn("weak_evidence", result.risk_classes)

    def test_risk_detector_skips_query_error_already_resolved_by_quality_gate(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer=(
                "I could not complete the analytics check because the data query failed. "
                "I did not treat that failure as proof that there is no data."
            ),
            latest_question="How many tickets were opened today?",
            tools=["run_bq_query"],
            bq_rows="Query error: Syntax error near WHERE",
            quality_gate_flags={"changed": True, "action": "replaced_query_error_no_data"},
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertFalse(result.should_validate)
        self.assertEqual(result.risk_classes, [])

    def test_risk_detector_keeps_unresolved_risks_after_quality_gate(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer=(
                "I could not complete the analytics check because the data query failed. "
                "I did not treat that failure as proof that there is no data."
            ),
            latest_question="Find example tickets where FiberLink dispatched a technician.",
            tools=["run_bq_query"],
            bq_rows="Query error: Syntax error near WHERE",
            expected_source_family="tickets",
            quality_gate_flags={"changed": True, "action": "replaced_query_error_no_data"},
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertTrue(result.should_validate)
        self.assertNotIn("tool_error_as_absence", result.risk_classes)
        self.assertIn("source_family_mismatch", result.risk_classes)

    def test_apply_validation_result_rewrites_from_supplied_evidence(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "unsafe",
            "confidence": 0.9,
            "risk_classes": ["unsupported_strong_claim"],
            "recommended_action": "rewrite_with_existing_sources",
            "answer_policy": "caveated",
            "reason_short": "unsupported claim",
            "rewrite_from_supplied_evidence": "Safer answer from supplied fields only.",
        })

        answer, flags = chat_answer_validator.apply_validation_result_to_answer(
            "Original unsupported answer.",
            result,
        )

        self.assertEqual(answer, "Safer answer from supplied fields only.")
        self.assertTrue(flags["validator_changed_answer"])
        self.assertEqual(flags["validator_applied"], "rewrite_from_supplied_evidence")

    def test_apply_validation_result_adds_caveat_for_deferred_repair(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.6,
            "risk_classes": ["source_family_mismatch"],
            "recommended_action": "drive_exact_retry",
            "answer_policy": "partial",
            "reason_short": "needs exact source",
        })

        answer, flags = chat_answer_validator.apply_validation_result_to_answer(
            "Use the generic KB procedure.",
            result,
        )

        self.assertIn("Data limitation:", answer)
        self.assertTrue(flags["validator_changed_answer"])
        self.assertEqual(flags["validator_applied"], "deferred_repair")

    def test_apply_validation_result_disabled_leaves_answer_unchanged(self):
        result, _telemetry = chat_answer_validator.validate_answer_candidate_with_telemetry(
            {"candidate_answer": "anything"},
            enabled=False,
        )

        answer, flags = chat_answer_validator.apply_validation_result_to_answer("Stable answer.", result)

        self.assertEqual(answer, "Stable answer.")
        self.assertFalse(flags["validator_enabled"])
        self.assertFalse(flags["validator_changed_answer"])

    def test_bounded_repair_allowlist_only_runs_for_repair_actions(self):
        repair_result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.6,
            "risk_classes": ["source_family_mismatch"],
            "recommended_action": "drive_exact_retry",
            "answer_policy": "partial",
            "reason_short": "wrong source family",
        })
        caveat_result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.6,
            "risk_classes": ["weak_evidence"],
            "recommended_action": "final_with_caveat",
            "answer_policy": "caveated",
            "reason_short": "not enough evidence",
        })

        self.assertTrue(chat_answer_validator.should_attempt_bounded_repair(repair_result))
        self.assertFalse(chat_answer_validator.should_attempt_bounded_repair(caveat_result))

    def test_bounded_repair_review_seed_reuses_improve_answer_contract(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.7,
            "risk_classes": ["source_family_mismatch", "weak_evidence"],
            "recommended_action": "drive_exact_retry",
            "answer_policy": "partial",
            "reason_short": "KB answer used for a Drive/table lookup.",
        })

        seed = chat_answer_validator.bounded_repair_review_seed(
            result,
            latest_question="Find the contact table for store 2008.",
        )

        self.assertEqual(seed.quality, "insufficient_evidence")
        self.assertEqual(seed.recommended_action, "run_semantic_plus_exact")
        self.assertEqual(seed.reformulated_query, "Find the contact table for store 2008.")
        self.assertIn("wrong_routing", seed.problem_types)
        self.assertIn("weak_sources", seed.problem_types)
        self.assertIsNone(seed.improved_answer)

    def test_bounded_repair_review_seed_does_not_treat_prior_answer_as_evidence(self):
        result = chat_answer_validator._normalize_validator_data({
            "quality": "suspect",
            "confidence": 0.6,
            "risk_classes": ["weak_evidence"],
            "recommended_action": "rerun_bq",
            "answer_policy": "partial",
            "reason_short": "candidate relied on weak evidence",
        })

        seed = chat_answer_validator.bounded_repair_review_seed(
            result,
            latest_question="Which stores had repeated Simphony issues last week?",
        )

        self.assertEqual(
            seed.reformulated_query,
            "Which stores had repeated Simphony issues last week?",
        )
        self.assertEqual(seed.recommended_action, "reroute")
        self.assertIn("weak_sources", seed.problem_types)
        self.assertIsNone(seed.improved_answer)
        self.assertNotIn("candidate", seed.reformulated_query.lower())
        self.assertNotIn("first answer", seed.reformulated_query.lower())

    def test_profile_analytics_on_general_troubleshooting_is_route_task_mismatch(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Found many profile-analytics rows.",
            latest_question="The KDS is not receiving modifiers at this Simphony store.",
            semantic_frame={"intent": "general", "task": {"task_type": "general_answer"}},
            policy_flags={"selected_executor": "pos_type_profile_analytics"},
            quality_gate_flags={"pos_type_profile_count": 15536},
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertIn("route_task_mismatch", result.risk_classes)

    def test_genuine_profile_analytics_does_not_get_route_task_mismatch(self):
        context = chat_answer_validator.build_answer_validation_context(
            candidate_answer="Top repeated issues for Simphony stores.",
            latest_question="Which Simphony stores have the most repeated issues?",
            semantic_frame={"intent": "store_analytics", "task": {"task_type": "count_by_dimension"}},
            policy_flags={"selected_executor": "pos_type_profile_analytics"},
            quality_gate_flags={"pos_type_profile_count": 12},
        )

        result = chat_answer_validator.detect_answer_risks(context)

        self.assertNotIn("route_task_mismatch", result.risk_classes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
