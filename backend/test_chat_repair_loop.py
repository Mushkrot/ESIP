"""Pure automatic repair-loop policy tests.

Run from project root:
scripts/backend_python.sh backend/test_chat_repair_loop.py
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.models.chat_quality import ChatAnswerValidationResult
from app.models.chat_quality import ChatRepairLoopState
from app.services import chat_repair_loop


class ChatRepairLoopTest(unittest.TestCase):
    def setUp(self):
        self._orig_escalation_enabled = chat_repair_loop.settings.universal_model_escalation_enabled
        self._orig_escalation_max = chat_repair_loop.settings.universal_model_escalation_max_per_turn
        self._orig_escalation_actions = chat_repair_loop.settings.universal_model_escalation_allowed_actions

    def tearDown(self):
        chat_repair_loop.settings.universal_model_escalation_enabled = self._orig_escalation_enabled
        chat_repair_loop.settings.universal_model_escalation_max_per_turn = self._orig_escalation_max
        chat_repair_loop.settings.universal_model_escalation_allowed_actions = self._orig_escalation_actions

    def test_max_repairs_and_validations_are_enforced(self):
        state = ChatRepairLoopState(enabled=True, max_repairs=2, max_validations=3)

        self.assertTrue(chat_repair_loop.validation_decision(state).allowed)
        state = chat_repair_loop.record_validation(state)
        state = chat_repair_loop.record_validation(state)
        state = chat_repair_loop.record_validation(state)
        validation_decision = chat_repair_loop.validation_decision(state)

        self.assertFalse(validation_decision.allowed)
        self.assertEqual(validation_decision.stop_reason, "max_validations")

        signature_1 = chat_repair_loop.build_repair_signature(action="rerun_bq")
        signature_2 = chat_repair_loop.build_repair_signature(action="zendesk_fallback")
        state = chat_repair_loop.record_repair(state, repair_signature=signature_1)
        state = chat_repair_loop.record_repair(state, repair_signature=signature_2)
        repair_decision = chat_repair_loop.repair_decision(
            state,
            action="drive_exact_retry",
            repair_signature=chat_repair_loop.build_repair_signature(action="drive_exact_retry"),
        )

        self.assertFalse(repair_decision.allowed)
        self.assertEqual(repair_decision.stop_reason, "max_repairs")

    def test_loop_disabled_stops_validation_and_repair(self):
        state = ChatRepairLoopState(enabled=False)
        signature = chat_repair_loop.build_repair_signature(action="rerun_bq")

        validation_decision = chat_repair_loop.validation_decision(state)
        repair_decision = chat_repair_loop.repair_decision(
            state,
            action="rerun_bq",
            repair_signature=signature,
        )

        self.assertFalse(validation_decision.allowed)
        self.assertEqual(validation_decision.stop_reason, "loop_disabled")
        self.assertFalse(repair_decision.allowed)
        self.assertEqual(repair_decision.stop_reason, "loop_disabled")

    def test_repeated_repair_signature_stops_loop(self):
        scope = chat_repair_loop.build_scope_fingerprint(
            latest_question="Show Simphony repeated issues",
            pos_type="Simphony",
            date_start="2026-06-01",
            date_end="2026-06-17",
            source_family="pos_type_profile",
        )
        signature = chat_repair_loop.build_repair_signature(
            action="rerun_bq",
            scope_fingerprint=scope,
            risk_classes=["weak_evidence"],
        )
        state = chat_repair_loop.record_repair(
            ChatRepairLoopState(scope_fingerprint=scope),
            repair_signature=signature,
        )

        decision = chat_repair_loop.repair_decision(
            state,
            action="rerun_bq",
            repair_signature=signature,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stop_reason, "repeated_action")

    def test_no_progress_detects_unchanged_and_near_empty_answers(self):
        self.assertTrue(chat_repair_loop.no_progress("Same answer.", "Same answer."))
        self.assertTrue(chat_repair_loop.no_progress("Useful previous answer.", ""))
        self.assertTrue(chat_repair_loop.no_progress("Useful previous answer.", "Too short"))
        self.assertFalse(
            chat_repair_loop.no_progress(
                "No tickets were found.",
                "Checked BigQuery ticket analytics and Zendesk live fallback. "
                "The result is limited to the requested window.",
            )
        )

    def test_scope_fingerprint_detects_target_filter_drift(self):
        original = chat_repair_loop.build_scope_fingerprint(
            latest_question="Show open Simphony tickets for store 2008 last week",
            target_ticket_id=950033,
            store_id=2008,
            pos_type="Simphony",
            status_filter=["open", "pending"],
            date_start="2026-06-08",
            date_end="2026-06-14",
            source_family="pos_type_profile",
            denominator_key="org_id_join",
        )
        same = chat_repair_loop.build_scope_fingerprint(
            latest_question="Show open Simphony tickets for store 2008 last week",
            target_ticket_id="950033",
            store_id="2008",
            pos_type=" simphony ",
            status_filter=["pending", "open"],
            date_start="2026-06-08",
            date_end="2026-06-14",
            source_family="POS_TYPE_PROFILE",
            denominator_key="org_id_join",
        )
        changed_store = chat_repair_loop.build_scope_fingerprint(
            latest_question="Show open Simphony tickets for store 2008 last week",
            target_ticket_id=950033,
            store_id=2011,
            pos_type="Simphony",
            status_filter=["open", "pending"],
            date_start="2026-06-08",
            date_end="2026-06-14",
            source_family="pos_type_profile",
            denominator_key="org_id_join",
        )
        changed_status = chat_repair_loop.build_scope_fingerprint(
            latest_question="Show open Simphony tickets for store 2008 last week",
            target_ticket_id=950033,
            store_id=2008,
            pos_type="Simphony",
            status_filter=["solved"],
            date_start="2026-06-08",
            date_end="2026-06-14",
            source_family="pos_type_profile",
            denominator_key="org_id_join",
        )

        self.assertFalse(chat_repair_loop.scope_drifted(original, same))
        self.assertTrue(chat_repair_loop.scope_drifted(original, changed_store))
        self.assertTrue(chat_repair_loop.scope_drifted(original, changed_status))

    def test_compact_telemetry_excludes_raw_answers_and_prompts(self):
        state = ChatRepairLoopState(
            validation_count=2,
            repair_count=1,
            repair_signatures=[chat_repair_loop.build_repair_signature(action="rerun_bq")],
            stop_reason="max_validations",
        )

        telemetry = chat_repair_loop.compact_loop_telemetry(state)

        self.assertEqual(telemetry["validation_count"], 2)
        self.assertEqual(telemetry["repair_count"], 1)
        self.assertEqual(telemetry["stop_reason"], "max_validations")
        self.assertNotIn("prompt", telemetry)
        self.assertNotIn("answer", telemetry)

    def test_answer_now_checkpoint_waits_until_safe_candidate(self):
        state = chat_repair_loop.request_answer_now(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            checkpoint="before_optional_validator",
        )

        wait = chat_repair_loop.answer_now_checkpoint_decision(
            state,
            current_answer="",
            checkpoint="before_optional_validator",
        )
        finalize = chat_repair_loop.answer_now_checkpoint_decision(
            state,
            current_answer="A safe answer candidate exists.",
            checkpoint="before_revalidation",
        )
        normal = chat_repair_loop.answer_now_checkpoint_decision(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            current_answer="A safe answer candidate exists.",
            checkpoint="before_revalidation",
        )
        telemetry = chat_repair_loop.compact_loop_telemetry(state)

        self.assertTrue(wait.allowed)
        self.assertEqual(wait.stop_reason, "answer_now_waiting_for_candidate")
        self.assertFalse(finalize.allowed)
        self.assertEqual(finalize.stop_reason, "answer_now")
        self.assertTrue(normal.allowed)
        self.assertFalse(normal.stop_reason == "answer_now")
        self.assertTrue(telemetry["answer_now_requested"])
        self.assertEqual(telemetry["answer_now_checkpoint"], "before_optional_validator")

    def test_escalation_disabled_by_default(self):
        chat_repair_loop.settings.universal_model_escalation_enabled = False
        result = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs stronger review",
        )

        decision = chat_repair_loop.escalation_decision(validation_result=result)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stop_reason, "escalation_disabled")

    def test_escalation_allowlist_cap_and_routine_skip(self):
        chat_repair_loop.settings.universal_model_escalation_enabled = True
        chat_repair_loop.settings.universal_model_escalation_max_per_turn = 1
        chat_repair_loop.settings.universal_model_escalation_allowed_actions = "rerun_bq,zendesk_fallback"
        result = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs stronger review",
        )

        self.assertTrue(chat_repair_loop.escalation_decision(validation_result=result).allowed)
        capped = chat_repair_loop.escalation_decision(validation_result=result, escalation_count=1)
        routine = chat_repair_loop.escalation_decision(validation_result=result, should_validate=False)

        self.assertFalse(capped.allowed)
        self.assertEqual(capped.stop_reason, "escalation_cap")
        self.assertFalse(routine.allowed)
        self.assertEqual(routine.stop_reason, "routine_answer")


class ChatRepairLoopAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_pass_controller_applies_single_repair_without_revalidation(self):
        validation_result = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["source_family_mismatch"],
            recommended_action="drive_exact_retry",
            answer_policy="partial",
            reason_short="needs exact source",
        )
        calls: list[str] = []

        def seed_builder(result, latest_question):
            calls.append("seed")
            self.assertEqual(result.recommended_action, "drive_exact_retry")
            self.assertEqual(latest_question, "Find the escalation table row.")
            return SimpleNamespace(recommended_action="run_semantic_plus_exact")

        async def refresh_builder(seed):
            calls.append("refresh")
            self.assertEqual(seed.recommended_action, "run_semantic_plus_exact")
            return {
                "review_refresh": {
                    "query": "Find the escalation table row.",
                    "counts": {"drive": 1},
                    "phase1_decision": {"tools": ["search_drive_exact"]},
                }
            }

        async def review_runner(trace):
            calls.append("review")
            self.assertTrue(trace["review_refresh"]["counts"]["drive"])
            return SimpleNamespace(
                quality="weak",
                confidence=0.8,
                problem_types=["weak_sources"],
                recommended_action="rewrite_with_existing_sources",
                improved_answer="Use the exact table row.",
            ), {"actual_cost_cad": 0.001}

        def answer_gate(answer):
            calls.append("gate")
            return answer, {"changed": False, "action": "unchanged"}

        next_state, repair_state, repaired_answer, gate_flags = await chat_repair_loop.run_one_pass_repair(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            validation_result=validation_result,
            latest_question="Find the escalation table row.",
            repair_seed_builder=seed_builder,
            refresh_trace_builder=refresh_builder,
            review_runner=review_runner,
            answer_gate=answer_gate,
        )

        self.assertEqual(calls, ["seed", "refresh", "review", "gate"])
        self.assertEqual(next_state.repair_count, 1)
        self.assertEqual(next_state.validation_count, 1)
        self.assertEqual(next_state.stop_reason, "accepted")
        self.assertTrue(repair_state["attempted"])
        self.assertTrue(repair_state["applied"])
        self.assertEqual(repair_state["max_passes"], 1)
        self.assertEqual(repair_state["loop"]["repair_count"], 1)
        self.assertEqual(repaired_answer, "Use the exact table row.")
        self.assertEqual(gate_flags["action"], "unchanged")

    async def test_one_pass_controller_records_stop_reason_for_repeated_signature(self):
        validation_result = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics refresh",
        )
        signature = chat_repair_loop.build_repair_signature(
            action="rerun_bq",
            risk_classes=["weak_evidence"],
        )
        state = ChatRepairLoopState(repair_signatures=[signature])

        async def unused_refresh(_seed):
            raise AssertionError("refresh should not run for repeated signature")

        async def unused_review(_trace):
            raise AssertionError("review should not run for repeated signature")

        next_state, repair_state, repaired_answer, gate_flags = await chat_repair_loop.run_one_pass_repair(
            state,
            validation_result=validation_result,
            latest_question="Count open tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=unused_refresh,
            review_runner=unused_review,
            answer_gate=lambda answer: (answer, {}),
        )

        self.assertFalse(repair_state["attempted"])
        self.assertEqual(repair_state["skip_reason"], "repeated_action")
        self.assertEqual(next_state.stop_reason, "repeated_action")
        self.assertIsNone(repaired_answer)
        self.assertIsNone(gate_flags)

    async def test_revalidation_loop_accepts_repaired_answer_after_good_check(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["source_family_mismatch"],
            recommended_action="drive_exact_retry",
            answer_policy="partial",
            reason_short="needs exact source",
        )
        good = ChatAnswerValidationResult(
            quality="good",
            confidence=0.95,
            risk_classes=[],
            recommended_action="none",
            answer_policy="normal",
            reason_short="repaired answer is supported",
        )
        validator_calls = 0

        async def validator_runner(answer):
            nonlocal validator_calls
            validator_calls += 1
            self.assertEqual(answer, "Use the exact table row with supporting evidence.")
            return good, {"actual_cost_cad": 0.001}

        state, repair_state, final_answer, final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Use the generic KB answer.",
            latest_question="Find the escalation table row.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Use the exact table row with supporting evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=validator_runner,
            validation_result_applier=lambda answer, _result: (answer, {"validator_applied": "none"}),
            caveat_builder=self._caveat,
        )

        self.assertEqual(validator_calls, 1)
        self.assertEqual(state.validation_count, 2)
        self.assertEqual(state.repair_count, 1)
        self.assertEqual(state.stop_reason, "accepted")
        self.assertTrue(repair_state["applied"])
        self.assertEqual(repair_state["loop"]["stop_reason"], "accepted")
        self.assertEqual(len(repair_state["loop"]["iterations"]), 1)
        self.assertEqual(final_answer, "Use the exact table row with supporting evidence.")
        self.assertEqual(final_flags["validator_applied"], "none")
        self.assertFalse(repair_state["answer_now"]["requested"])

    async def test_revalidation_loop_can_run_second_repair_then_accept(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )
        second_repair = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["source_family_mismatch"],
            recommended_action="drive_exact_retry",
            answer_policy="partial",
            reason_short="needs exact source",
        )
        good = ChatAnswerValidationResult(
            quality="good",
            confidence=0.9,
            risk_classes=[],
            recommended_action="none",
            answer_policy="normal",
            reason_short="supported",
        )
        validation_results = [second_repair, good]
        review_answers = [
            "Analytics evidence narrowed the answer but still needs exact source.",
            "Final answer with exact source and analytics evidence.",
        ]

        async def validator_runner(_answer):
            return validation_results.pop(0), {"actual_cost_cad": 0.001}

        async def review_runner(_trace):
            return await self._review(review_answers.pop(0))

        state, repair_state, final_answer, _final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Weak answer.",
            latest_question="Compare scoped populations.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=review_runner,
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=validator_runner,
            validation_result_applier=lambda answer, _result: (answer, {"validator_applied": "none"}),
            caveat_builder=self._caveat,
        )

        self.assertEqual(state.validation_count, 3)
        self.assertEqual(state.repair_count, 2)
        self.assertEqual(state.stop_reason, "accepted")
        self.assertEqual(final_answer, "Final answer with exact source and analytics evidence.")
        self.assertEqual(len(repair_state["revalidations"]), 2)
        self.assertEqual(len(repair_state["loop"]["iterations"]), 2)

    async def test_answer_now_before_repair_finalizes_initial_candidate(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )

        state, repair_state, final_answer, final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Initial safe candidate.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: self.fail("repair should not start"),
            refresh_trace_builder=lambda _seed: self.fail("refresh should not run"),
            review_runner=lambda _trace: self.fail("review should not run"),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=lambda _answer: self.fail("validator should not run"),
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
            answer_now_checker=lambda checkpoint: checkpoint == "before_optional_repair",
        )

        self.assertEqual(state.stop_reason, "answer_now")
        self.assertEqual(state.repair_count, 0)
        self.assertEqual(state.answer_now_checkpoint, "before_optional_repair")
        self.assertEqual(state.answer_now_final_policy, "partial")
        self.assertEqual(repair_state["skip_reason"], "answer_now")
        self.assertTrue(repair_state["answer_now"]["requested"])
        self.assertIn("Initial safe candidate.", final_answer)
        self.assertIn("because you asked to answer now", final_answer)
        self.assertEqual(final_flags["answer_policy"], "partial")

    async def test_answer_now_before_revalidation_returns_repaired_candidate(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )

        state, repair_state, final_answer, final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Weak candidate.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Repaired candidate with supporting evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=lambda _answer: self.fail("validator should not run after Answer Now checkpoint"),
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
            answer_now_checker=lambda checkpoint: checkpoint == "before_revalidation",
        )

        self.assertEqual(state.stop_reason, "answer_now")
        self.assertEqual(state.repair_count, 1)
        self.assertEqual(state.answer_now_checkpoint, "before_revalidation")
        self.assertEqual(repair_state["skip_reason"], "answer_now")
        self.assertEqual(len(repair_state["loop"]["iterations"]), 1)
        self.assertIn("Repaired candidate with supporting evidence.", final_answer)
        self.assertTrue(final_flags["partial_caveat_added"])

    async def test_answer_now_after_first_repair_skips_second_optional_pass(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )
        second_repair = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["source_family_mismatch"],
            recommended_action="drive_exact_retry",
            answer_policy="partial",
            reason_short="would normally need another repair",
        )
        checkpoints: list[str] = []

        def answer_now_checker(checkpoint: str) -> bool:
            checkpoints.append(checkpoint)
            return checkpoint == "before_optional_repair" and checkpoints.count("before_optional_repair") == 2

        async def validator_runner(_answer):
            return second_repair, {"actual_cost_cad": 0.001}

        state, repair_state, final_answer, final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Weak candidate.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("First repaired candidate with evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=validator_runner,
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
            answer_now_checker=answer_now_checker,
        )

        self.assertEqual(state.stop_reason, "answer_now")
        self.assertEqual(state.repair_count, 1)
        self.assertEqual(state.validation_count, 2)
        self.assertEqual(state.answer_now_checkpoint, "before_optional_repair")
        self.assertEqual(repair_state["skip_reason"], "answer_now")
        self.assertIn("First repaired candidate with evidence.", final_answer)
        self.assertTrue(final_flags["partial_caveat_added"])

    async def test_revalidation_loop_stops_when_validation_cap_would_be_exceeded(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )

        state, repair_state, final_answer, final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=1),
            initial_validation_result=initial,
            initial_answer="Weak answer.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Repaired answer with new evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=lambda _answer: self.fail("validator should not run after cap"),
            validation_result_applier=lambda answer, _result: (answer, {"validator_applied": "none"}),
            caveat_builder=self._caveat,
        )

        self.assertEqual(state.stop_reason, "max_validations")
        self.assertEqual(repair_state["skip_reason"], "max_validations")
        self.assertIn("max_validations", final_answer)
        self.assertEqual(final_flags["stop_reason"], "max_validations")

    async def test_revalidation_loop_stops_repeated_action_after_recheck(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )
        repeated = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="same repair again",
        )

        async def validator_runner(_answer):
            return repeated, {}

        state, repair_state, _final_answer, _final_flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Weak answer.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Repaired answer with new evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=validator_runner,
            validation_result_applier=lambda answer, _result: (answer, {"validator_applied": "none"}),
            caveat_builder=self._caveat,
        )

        self.assertEqual(state.stop_reason, "repeated_action")
        self.assertEqual(repair_state["skip_reason"], "repeated_action")

    async def test_revalidation_loop_stops_no_progress_scope_drift_and_validator_failure(self):
        initial = ChatAnswerValidationResult(
            quality="suspect",
            confidence=0.7,
            risk_classes=["weak_evidence"],
            recommended_action="rerun_bq",
            answer_policy="partial",
            reason_short="needs analytics",
        )
        original_scope = chat_repair_loop.build_scope_fingerprint(store_id=2008, source_family="analytics")
        changed_scope = chat_repair_loop.build_scope_fingerprint(store_id=2011, source_family="analytics")

        no_progress_state, no_progress_repair, _answer, _flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Same useful answer with enough length.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Same useful answer with enough length."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=lambda _answer: self.fail("validator should not run after no-progress"),
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
        )
        self.assertEqual(no_progress_state.stop_reason, "no_progress")
        self.assertEqual(no_progress_repair["skip_reason"], "no_progress")
        self.assertFalse(no_progress_repair["applied"])

        drift_state, drift_repair, _answer, _flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3, scope_fingerprint=original_scope),
            initial_validation_result=initial,
            initial_answer="Weak answer with enough length.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Different answer with useful new supporting evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=lambda _answer: self.fail("validator should not run after scope drift"),
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
            candidate_scope_builder=lambda: changed_scope,
        )
        self.assertEqual(drift_state.stop_reason, "scope_drift")
        self.assertEqual(drift_repair["skip_reason"], "scope_drift")
        self.assertFalse(drift_repair["applied"])

        async def failing_validator(_answer):
            raise RuntimeError("validator unavailable")

        failure_state, failure_repair, _answer, _flags = await chat_repair_loop.run_revalidation_repair_loop(
            ChatRepairLoopState(max_repairs=2, max_validations=3),
            initial_validation_result=initial,
            initial_answer="Weak answer with enough length.",
            latest_question="Count scoped tickets.",
            repair_seed_builder=lambda _result, _question: object(),
            refresh_trace_builder=lambda _seed: self._refresh_trace(),
            review_runner=lambda _trace: self._review("Different answer with useful new supporting evidence."),
            answer_gate=lambda answer: (answer, {"action": "unchanged"}),
            validator_runner=failing_validator,
            validation_result_applier=lambda answer, _result: (answer, {}),
            caveat_builder=self._caveat,
        )
        self.assertEqual(failure_state.stop_reason, "validator_failure")
        self.assertIn("validator unavailable", failure_repair["error"])

    async def _refresh_trace(self):
        return {
            "review_refresh": {
                "query": "refresh query",
                "counts": {"bq": 1},
                "phase1_decision": {"tools": ["run_bq_query"]},
            }
        }

    async def _review(self, improved_answer):
        return SimpleNamespace(
            quality="weak",
            confidence=0.8,
            problem_types=["weak_sources"],
            recommended_action="rewrite_with_existing_sources",
            improved_answer=improved_answer,
        ), {"actual_cost_cad": 0.001}

    def _caveat(self, answer, stop_reason):
        return f"{answer}\n\nData limitation: repair loop stopped ({stop_reason}).", {
            "stop_reason": stop_reason,
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
