"""Tests for the Universal Chat output critic shadow contract."""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from app.services import output_critic_shadow
from app.services.cost_tracker import CostGateError


class FakeUsage:
    prompt_token_count = 100
    candidates_token_count = 50
    thoughts_token_count = 0


class FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = FakeUsage()


class FakeModel:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def generate_content(self, prompt, generation_config=None):
        self.calls.append({"prompt": prompt, "generation_config": generation_config})
        return FakeResponse(self.text)


class OutputCriticShadowContractTest(unittest.TestCase):
    def test_normalized_result_is_shadow_only_and_compact(self):
        result = output_critic_shadow.normalize_output_critic_payload(
            {
                "risk_level": "high",
                "risk_codes": ["context_loss", "unknown_risk", "context_loss"],
                "confidence": 1.4,
                "reason_short": "The answer lost the prior date scope.",
                "answer_affecting": True,
                "raw_model_response": "must not survive",
                "latency_ms": "42",
                "estimated_cost_cad": "0.001",
                "actual_cost_cad": "0.0008",
                "correlation_id": "corr-123",
            }
        )
        payload = result.to_trace_dict()

        self.assertTrue(payload["emitted"])
        self.assertTrue(payload["attempted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertEqual(payload["risk_level"], "high")
        self.assertEqual(payload["risk_codes"], ["context_loss"])
        self.assertEqual(payload["confidence"], 1.0)
        self.assertNotIn("raw_model_response", payload)
        self.assertEqual(payload["latency_ms"], 42)

    def test_skipped_and_error_results_fail_open(self):
        skipped = output_critic_shadow.skipped_result("disabled by config")
        failed = output_critic_shadow.error_result("provider timeout", latency_ms=2001)

        self.assertFalse(skipped.to_trace_dict()["answer_affecting"])
        self.assertFalse(skipped.to_trace_dict()["attempted"])
        self.assertEqual(skipped.to_trace_dict()["skipped_reason"], "disabled by config")
        self.assertFalse(failed.to_trace_dict()["answer_affecting"])
        self.assertIn("output_critic_error", failed.to_trace_dict()["risk_codes"])
        self.assertEqual(failed.to_trace_dict()["latency_ms"], 2001)

    def test_prompt_bundle_removes_raw_ticket_text_and_prompts(self):
        bundle = output_critic_shadow.build_critic_input_bundle(
            latest_question="i just want the one for July and Jun",
            visible_answer="Visible answer with bounded evidence.",
            trace_context={
                "policy_flags": {
                    "safe": True,
                    "raw_prompt": "SECRET PROMPT",
                    "nested": {"full_text": "RAW TICKET FULL TEXT", "safe_count": 2},
                },
                "tickets": [
                    {
                        "ticket_id": 950050,
                        "subject": "Internet down",
                        "full_text": "RAW TICKET FULL TEXT",
                        "raw_model_response": "MODEL RAW",
                    }
                ],
                "phase1_decision": {"tools": []},
                "private_note": "DO NOT KEEP",
            },
        )
        rendered = repr(bundle)

        self.assertIn("visible_answer_excerpt", bundle)
        self.assertIn("policy_flags", bundle["trace"])
        self.assertNotIn("SECRET PROMPT", rendered)
        self.assertNotIn("RAW TICKET FULL TEXT", rendered)
        self.assertNotIn("MODEL RAW", rendered)
        self.assertNotIn("private_note", rendered)

    def test_trace_metadata_removes_raw_fields(self):
        metadata = output_critic_shadow.compact_trace_metadata(
            {
                "risk_level": "medium",
                "risk_codes": ["weak_evidence"],
                "confidence": 0.7,
                "reason_short": "Evidence was thin.",
                "raw_response": "RAW",
                "prompt": "PROMPT",
            }
        )
        rendered = repr(metadata)

        self.assertFalse(metadata["answer_affecting"])
        self.assertIn("weak_evidence", metadata["risk_codes"])
        self.assertNotIn("RAW", rendered)
        self.assertNotIn("PROMPT", rendered)

    def test_confusion_fixtures_preserve_expected_shadow_risks(self):
        fixtures = [
            ("supported_good", "none", [], "none"),
            ("cautious_no_data", "none", [], "none"),
            ("followup_scope_loss", "high", ["followup_scope_loss"], "high"),
            ("wrong_source", "high", ["source_family_mismatch"], "high"),
            ("tool_error_as_absence", "high", ["tool_error_as_absence"], "high"),
            ("resolution_claim", "high", ["wrong_resolution_claim"], "high"),
        ]

        for name, risk_level, risk_codes, expected_level in fixtures:
            with self.subTest(name=name):
                payload = output_critic_shadow.normalize_output_critic_payload(
                    {
                        "attempted": True,
                        "emitted": True,
                        "answer_affecting": True,
                        "risk_level": risk_level,
                        "risk_codes": risk_codes,
                        "confidence": 0.9 if risk_codes else 0.8,
                        "reason_short": f"Synthetic {name} fixture.",
                    }
                ).to_trace_dict()

                self.assertEqual(payload["risk_level"], expected_level)
                self.assertEqual(payload.get("risk_codes", []), risk_codes)
                self.assertFalse(payload["answer_affecting"])

    def test_response_schema_is_strict_and_shadow_only(self):
        schema = output_critic_shadow.OUTPUT_CRITIC_RESPONSE_SCHEMA

        self.assertEqual(schema["type"], "object")
        self.assertIn("risk_level", schema["required"])
        self.assertIn("risk_codes", schema["required"])
        self.assertNotIn("final_answer", schema["properties"])
        self.assertNotIn("tool_calls", schema["properties"])

    def test_generation_config_has_explicit_thinking_and_max_output(self):
        fake_model = FakeModel(
            '{"risk_level":"medium","risk_codes":["context_loss"],'
            '"confidence":0.8,"reason_short":"Lost prior context."}'
        )
        captured_configs: list[dict] = []

        def capture_config(payload):
            captured_configs.append(payload)
            return payload

        with (
            mock.patch.object(output_critic_shadow, "_get_model", return_value=fake_model),
            mock.patch.object(output_critic_shadow.GenerationConfig, "from_dict", side_effect=capture_config),
            mock.patch.object(output_critic_shadow, "calc_cost", return_value=0.001),
            mock.patch.object(output_critic_shadow, "get_gate_reserve", return_value=0.002),
            mock.patch.object(output_critic_shadow, "check_cost_gate"),
            mock.patch.object(output_critic_shadow, "record_estimate"),
            mock.patch.object(output_critic_shadow, "record_usage", return_value=0.0004),
            mock.patch.object(output_critic_shadow, "check_divergence"),
            mock.patch.object(output_critic_shadow, "new_correlation_id", return_value="critic-corr"),
        ):
            result = output_critic_shadow.run_output_critic_shadow(
                latest_question="i just want the one for July and Jun",
                visible_answer="Only July 1-2 is shown.",
                trace_context={"policy_flags": {"selected_executor": "ticket_semantic_search"}},
                enabled=True,
            )

        config = captured_configs[0]
        self.assertEqual(config["max_output_tokens"], 1024)
        self.assertEqual(config["thinking_config"], {"thinking_budget": 0})
        self.assertEqual(config["response_mime_type"], "application/json")
        self.assertEqual(config["response_schema"], output_critic_shadow.OUTPUT_CRITIC_RESPONSE_SCHEMA)
        self.assertFalse(result.to_trace_dict()["answer_affecting"])
        self.assertEqual(result.to_trace_dict()["risk_codes"], ["context_loss"])
        self.assertEqual(result.to_trace_dict()["model_surface"], output_critic_shadow.MODEL_SURFACE)
        self.assertEqual(result.to_trace_dict()["estimated_cost_cad"], 0.001)
        self.assertEqual(result.to_trace_dict()["actual_cost_cad"], 0.0004)

    def test_disabled_and_skip_paths_do_not_call_model(self):
        with mock.patch.object(output_critic_shadow, "_get_model", side_effect=AssertionError("model should not run")):
            disabled = output_critic_shadow.run_output_critic_shadow(
                latest_question="Question",
                visible_answer="Answer",
                enabled=False,
            )
            skipped = output_critic_shadow.run_output_critic_shadow(
                latest_question="Question",
                visible_answer="Answer",
                enabled=True,
                skip_reason="timeout_before_start",
            )

        self.assertEqual(disabled.to_trace_dict()["skipped_reason"], "disabled_by_config")
        self.assertEqual(skipped.to_trace_dict()["skipped_reason"], "timeout_before_start")

    def test_cost_and_provider_fail_open(self):
        with (
            mock.patch.object(output_critic_shadow, "_get_model", side_effect=AssertionError("provider down")),
            mock.patch.object(output_critic_shadow, "calc_cost", return_value=0.001),
            mock.patch.object(output_critic_shadow, "get_gate_reserve", return_value=0.002),
            mock.patch.object(output_critic_shadow, "check_cost_gate"),
            mock.patch.object(output_critic_shadow, "record_estimate"),
        ):
            provider_error = output_critic_shadow.run_output_critic_shadow(
                latest_question="Question",
                visible_answer="Answer",
                enabled=True,
            )

        with (
            mock.patch.object(output_critic_shadow, "_get_model", side_effect=AssertionError("model should not run")),
            mock.patch.object(output_critic_shadow, "calc_cost", return_value=0.001),
            mock.patch.object(output_critic_shadow, "get_gate_reserve", return_value=0.002),
            mock.patch.object(output_critic_shadow, "check_cost_gate", side_effect=CostGateError("blocked")),
        ):
            cost_error = output_critic_shadow.run_output_critic_shadow(
                latest_question="Question",
                visible_answer="Answer",
                enabled=True,
            )

        self.assertFalse(provider_error.to_trace_dict()["answer_affecting"])
        self.assertIn("output_critic_error", provider_error.to_trace_dict()["risk_codes"])
        self.assertFalse(cost_error.to_trace_dict()["answer_affecting"])
        self.assertIn("output_critic_error", cost_error.to_trace_dict()["risk_codes"])

    def test_invalid_json_fails_open(self):
        fake_model = FakeModel("not json")

        with (
            mock.patch.object(output_critic_shadow, "_get_model", return_value=fake_model),
            mock.patch.object(output_critic_shadow.GenerationConfig, "from_dict", side_effect=lambda payload: payload),
            mock.patch.object(output_critic_shadow, "calc_cost", return_value=0.001),
            mock.patch.object(output_critic_shadow, "get_gate_reserve", return_value=0.002),
            mock.patch.object(output_critic_shadow, "check_cost_gate"),
            mock.patch.object(output_critic_shadow, "record_estimate"),
            mock.patch.object(output_critic_shadow, "record_usage", return_value=0.0004),
            mock.patch.object(output_critic_shadow, "check_divergence"),
        ):
            result = output_critic_shadow.run_output_critic_shadow(
                latest_question="Question",
                visible_answer="Answer",
                enabled=True,
            )

        self.assertFalse(result.to_trace_dict()["answer_affecting"])
        self.assertIn("output_critic_error", result.to_trace_dict()["risk_codes"])

    def test_live_call_path_uses_current_vertex_sdk_only(self):
        source = inspect.getsource(output_critic_shadow)

        self.assertIn("vertexai", source)
        self.assertIn("GenerativeModel", source)
        self.assertIn("GenerationConfig", source)
        self.assertNotIn("google_genai", source)
        self.assertNotIn("openai", source)


if __name__ == "__main__":
    unittest.main()
