"""Tests for deterministic output critic bounded repair decisions."""

from __future__ import annotations

import inspect
import unittest

from app.services import output_critic_bounded_repair


class OutputCriticBoundedRepairTest(unittest.TestCase):
    def _critic(self, *, risk_codes, confidence=0.91, risk_level="high") -> dict:
        return {
            "emitted": True,
            "attempted": True,
            "answer_affecting": False,
            "risk_level": risk_level,
            "risk_codes": list(risk_codes),
            "confidence": confidence,
            "reason_short": "Synthetic critic finding.",
        }

    def test_default_disabled_is_noop(self):
        decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer="No matching tickets found.",
            output_critic_trace=self._critic(risk_codes=["false_no_data"]),
            trace_context={"policy_flags": {"bq_error": "query failed"}},
            enabled=False,
        )

        payload = decision.to_trace_dict()
        self.assertFalse(payload["answer_affecting"])
        self.assertFalse(payload["attempted"])
        self.assertEqual(payload["skipped_reason"], "disabled_by_config")
        self.assertNotIn("stream_delta", payload)
        self.assertNotIn("repaired_answer", payload)

    def test_low_confidence_and_unsupported_risk_skip(self):
        low = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer="No matching tickets found.",
            output_critic_trace=self._critic(risk_codes=["false_no_data"], confidence=0.5),
            trace_context={"policy_flags": {"bq_error": "query failed"}},
            enabled=True,
        )
        unsupported = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer="Answer",
            output_critic_trace=self._critic(risk_codes=["weak_evidence"]),
            trace_context={"counts": {"sources": 0}},
            enabled=True,
        )

        self.assertEqual(low.to_trace_dict()["skipped_reason"], "low_confidence")
        self.assertEqual(unsupported.to_trace_dict()["skipped_reason"], "unsupported_risk")
        self.assertFalse(low.repair_applied)
        self.assertFalse(unsupported.repair_applied)

    def test_critic_error_or_not_attempted_skips(self):
        not_attempted = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer="Answer",
            output_critic_trace={"attempted": False, "risk_level": "none", "risk_codes": []},
            enabled=True,
        )
        critic_error = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer="Answer",
            output_critic_trace=self._critic(risk_codes=["output_critic_error"]),
            enabled=True,
        )

        self.assertEqual(not_attempted.to_trace_dict()["skipped_reason"], "critic_not_attempted")
        self.assertEqual(critic_error.to_trace_dict()["skipped_reason"], "critic_error")

    def test_false_no_data_with_tool_error_adds_bounded_correction(self):
        answer = "No matching tickets found for that store."
        decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Find matching tickets",
            visible_answer=answer,
            output_critic_trace=self._critic(risk_codes=["tool_error_as_absence"]),
            trace_context={"policy_flags": {"bq_error": "query failed"}},
            enabled=True,
        )
        payload = decision.to_trace_dict()

        self.assertTrue(decision.repair_applied)
        self.assertTrue(payload["answer_affecting"])
        self.assertEqual(payload["repair_kind"], "false_no_data_or_tool_error")
        self.assertIn("Correction:", decision.stream_delta)
        self.assertIn("no-data statement above as unverified", decision.repaired_answer)
        self.assertNotIn("query failed", repr(payload))

    def test_false_no_data_without_error_evidence_skips(self):
        decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Find matching tickets",
            visible_answer="No matching tickets found.",
            output_critic_trace=self._critic(risk_codes=["false_no_data"]),
            trace_context={"counts": {"tickets": 0}},
            enabled=True,
        )

        self.assertFalse(decision.repair_applied)
        self.assertEqual(decision.to_trace_dict()["skipped_reason"], "missing_tool_error_evidence")

    def test_raw_artifact_repair_uses_clean_answer_and_keeps_trace_compact(self):
        answer = "Useful answer.\nTRACE_JSON: {\"raw\":\"secret\"}\ninternal\n\nKeep this."
        decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="Question",
            visible_answer=answer,
            output_critic_trace=self._critic(risk_codes=["raw_artifact"]),
            enabled=True,
        )
        payload = decision.to_trace_dict()
        rendered_payload = repr(payload)

        self.assertTrue(decision.repair_applied)
        self.assertEqual(payload["repair_kind"], "raw_artifact_strip")
        self.assertIn("Use this cleaned answer instead", decision.stream_delta)
        self.assertIn("Keep this.", decision.stream_delta)
        self.assertNotIn("TRACE_JSON", rendered_payload)
        self.assertNotIn("secret", rendered_payload)

    def test_followup_scope_loss_requires_deterministic_assist_context(self):
        missing = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="i just want July and Jun",
            visible_answer="Only July 1-2.",
            output_critic_trace=self._critic(risk_codes=["followup_scope_loss"]),
            trace_context={"followup_date_scope_assist": {"accepted": True, "answer_affecting": False}},
            enabled=True,
        )
        applied = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="i just want July and Jun",
            visible_answer="Only July 1-2.",
            output_critic_trace=self._critic(risk_codes=["followup_scope_loss"]),
            trace_context={
                "followup_date_scope_assist": {
                    "accepted": True,
                    "answer_affecting": True,
                    "display_label": "June 2026 and July 2026",
                }
            },
            enabled=True,
        )

        self.assertEqual(missing.to_trace_dict()["skipped_reason"], "missing_deterministic_followup_answer")
        self.assertTrue(applied.repair_applied)
        self.assertIn("June 2026 and July 2026", applied.stream_delta)

    def test_resolution_claim_guard_requires_no_documented_resolution_summary(self):
        decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
            latest_question="How was this resolved?",
            visible_answer="This was fixed by rebooting the router.",
            output_critic_trace=self._critic(risk_codes=["wrong_resolution_claim"]),
            trace_context={
                "ticket_resolution_lookup_assist": {"accepted": True, "target_ticket_id": 950050},
                "policy_flags": {
                    "ai_dispatcher_ticket_resolution_lookup": {
                        "found_count": 1,
                        "documented_resolution": False,
                    }
                },
            },
            enabled=True,
        )

        self.assertTrue(decision.repair_applied)
        self.assertEqual(decision.to_trace_dict()["repair_kind"], "resolution_claim_guard")
        self.assertIn("did not find a useful documented resolution", decision.stream_delta)

    def test_service_is_pure_and_has_no_external_client_calls(self):
        source = inspect.getsource(output_critic_bounded_repair)

        self.assertNotIn("vertexai", source)
        self.assertNotIn("GenerativeModel", source)
        self.assertNotIn("bigquery", source)
        self.assertNotIn("zendesk_client", source.lower())
        self.assertNotIn("zendesk_api", source.lower())
        self.assertNotIn("requests.", source)
        self.assertNotIn("httpx", source)


if __name__ == "__main__":
    unittest.main()
