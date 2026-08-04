"""Universal Chat integration tests for the output critic shadow layer."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from app.routers import universal
from app.services import output_critic_shadow


class UniversalOutputCriticShadowTest(unittest.TestCase):
    def test_disabled_path_does_not_call_critic_model(self):
        async def run_case() -> dict:
            loop = asyncio.get_running_loop()
            with mock.patch.object(
                output_critic_shadow,
                "run_output_critic_shadow",
                side_effect=AssertionError("critic must not run when disabled"),
            ):
                return await universal._resolve_output_critic_shadow_payload(
                    loop=loop,
                    latest_question="Question",
                    visible_answer="Visible answer",
                    trace_context={"counts": {"sources": 1}},
                    timeout_ms=250,
                )

        with mock.patch.object(output_critic_shadow, "output_critic_shadow_enabled", return_value=False):
            payload = asyncio.run(run_case())

        self.assertFalse(payload["answer_affecting"])
        self.assertFalse(payload["attempted"])
        self.assertEqual(payload["skipped_reason"], "disabled_by_config")

    def test_enabled_path_persists_metadata_without_changing_answer(self):
        visible_answer = "The visible answer is already finalized."
        result = output_critic_shadow.normalize_output_critic_payload(
            {
                "risk_level": "high",
                "risk_codes": ["followup_scope_loss"],
                "confidence": 0.88,
                "reason_short": "Follow-up scope may have been narrowed incorrectly.",
                "latency_ms": 41,
            }
        )

        async def run_case() -> dict:
            loop = asyncio.get_running_loop()
            with (
                mock.patch.object(output_critic_shadow, "output_critic_shadow_enabled", return_value=True),
                mock.patch.object(output_critic_shadow, "run_output_critic_shadow", return_value=result),
                mock.patch.object(
                    universal.chat_answer_validator,
                    "apply_validation_result_to_answer",
                    side_effect=AssertionError("critic must not mutate answers"),
                ),
                mock.patch.object(
                    universal.chat_repair_loop,
                    "run_revalidation_repair_loop",
                    side_effect=AssertionError("critic must not trigger repair"),
                ),
                mock.patch.object(
                    universal.output_critic_bounded_repair,
                    "evaluate_output_critic_bounded_repair",
                    side_effect=AssertionError("shadow observation must not invoke bounded repair"),
                ),
            ):
                return await universal._resolve_output_critic_shadow_payload(
                    loop=loop,
                    latest_question="i just want the one for July and Jun",
                    visible_answer=visible_answer,
                    trace_context={"counts": {"sources": 2}, "policy_flags": {"selected_executor": "ticket_semantic_search"}},
                    timeout_ms=1000,
                )

        payload = asyncio.run(run_case())

        self.assertEqual(visible_answer, "The visible answer is already finalized.")
        self.assertFalse(payload["answer_affecting"])
        self.assertEqual(payload["risk_level"], "high")
        self.assertEqual(payload["risk_codes"], ["followup_scope_loss"])
        self.assertEqual(payload["latency_ms"], 41)

    def test_timeout_fails_open(self):
        def slow_critic(**_kwargs):
            time.sleep(0.3)
            return output_critic_shadow.normalize_output_critic_payload(
                {"risk_level": "none", "risk_codes": [], "confidence": 0.0, "reason_short": ""}
            )

        async def run_case() -> dict:
            loop = asyncio.get_running_loop()
            with (
                mock.patch.object(output_critic_shadow, "output_critic_shadow_enabled", return_value=True),
                mock.patch.object(output_critic_shadow, "run_output_critic_shadow", side_effect=slow_critic),
            ):
                return await universal._resolve_output_critic_shadow_payload(
                    loop=loop,
                    latest_question="Question",
                    visible_answer="Answer",
                    trace_context={"counts": {"sources": 0}},
                    timeout_ms=250,
                )

        payload = asyncio.run(run_case())

        self.assertFalse(payload["answer_affecting"])
        self.assertIn("output_critic_error", payload["risk_codes"])
        self.assertIn("output_critic_timeout", payload["error"])
        self.assertEqual(payload["latency_ms"], 250)

    def test_context_builder_strips_raw_fields(self):
        context = universal._build_output_critic_shadow_context(
            routing_question="i just want the one for July and Jun",
            semantic_frame={"goal": "follow_up"},
            phase1_decision={"selected_executor": "ticket_semantic_search"},
            policy_flags={"raw_prompt": "SECRET PROMPT", "selected_executor": "ticket_semantic_search"},
            quality_gate_flags={"answer_validation": {"risk": "low"}},
            answer_validation={"raw_model_response": "MODEL RAW", "status": "ok"},
            counts={"sources": 2, "tickets": 1},
            sources=[
                {"title": "Procedure", "snippet": "safe", "html_body": "RAW HTML"},
            ],
            tickets=[
                {"ticket_id": 950050, "subject": "POS issue", "full_text": "RAW TICKET FULL TEXT"},
            ],
            ai_dispatcher_trace={"answer_affecting": False, "raw_response": "RAW"},
            followup_shadow_payload={"status": "accepted"},
            followup_date_scope_payload={"accepted": True},
            ticket_resolution_lookup_payload={"accepted": False},
            conversation_entities_loaded=True,
            conversation_entities_saved={"ticket_ids": [950050]},
        )
        rendered = repr(context)

        self.assertEqual(context["counts"]["sources"], 2)
        self.assertIn("routing_question", context)
        self.assertNotIn("SECRET PROMPT", rendered)
        self.assertNotIn("MODEL RAW", rendered)
        self.assertNotIn("RAW HTML", rendered)
        self.assertNotIn("RAW TICKET FULL TEXT", rendered)


if __name__ == "__main__":
    unittest.main()
