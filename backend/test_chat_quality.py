"""Chat quality review model-routing tests.

Run from project root:
scripts/backend_python.sh backend/test_chat_quality.py
"""

from __future__ import annotations

import unittest

from app.services import chat_quality


class _Usage:
    prompt_token_count = 13
    candidates_token_count = 11
    thoughts_token_count = 0


class _Response:
    text = """{
      "quality": "good",
      "confidence": 0.95,
      "problem_types": [],
      "recommended_action": "keep",
      "reformulated_query": null,
      "explanation": "The answer is supported.",
      "user_visible_note": "The answer is supported.",
      "improved_answer": "The original answer is supported by the checked evidence."
    }"""
    usage_metadata = _Usage()


class _RecordingModel:
    def __init__(self):
        self.last_config = None

    def generate_content(self, prompt, generation_config=None):
        self.last_config = generation_config
        return _Response()


class _RecordingFactory:
    def __init__(self):
        self.model_ids: list[str] = []
        self.instances: list[_RecordingModel] = []

    def __call__(self, model_id: str):
        self.model_ids.append(model_id)
        model = _RecordingModel()
        self.instances.append(model)
        return model


class ChatQualityModelRoutingTest(unittest.TestCase):
    def setUp(self):
        self._orig_model = chat_quality._model
        self._orig_model_factory = chat_quality._model_factory
        self._orig_review_model = chat_quality.settings.universal_review_model
        self._orig_escalation_enabled = chat_quality.settings.universal_model_escalation_enabled
        self._orig_escalation_model = chat_quality.settings.universal_model_escalation_model
        self._orig_check_cost_gate = chat_quality.check_cost_gate
        self._orig_get_gate_reserve = chat_quality.get_gate_reserve
        self._orig_calc_cost = chat_quality.calc_cost
        self._orig_record_estimate = chat_quality.record_estimate
        self._orig_record_usage = chat_quality.record_usage
        self._orig_check_divergence = chat_quality.check_divergence
        self._orig_new_correlation_id = chat_quality.new_correlation_id
        chat_quality._model = _RecordingModel()
        chat_quality.check_cost_gate = lambda *args, **kwargs: None
        chat_quality.get_gate_reserve = lambda _call_type, estimated_cost: estimated_cost
        chat_quality.calc_cost = lambda *args, **kwargs: 0.001
        chat_quality.record_estimate = lambda *args, **kwargs: None
        chat_quality.record_usage = lambda *args, **kwargs: 0.001
        chat_quality.check_divergence = lambda *args, **kwargs: None
        chat_quality.new_correlation_id = lambda: "review-test-corr"

    def tearDown(self):
        chat_quality._model = self._orig_model
        chat_quality._model_factory = self._orig_model_factory
        chat_quality.settings.universal_review_model = self._orig_review_model
        chat_quality.settings.universal_model_escalation_enabled = self._orig_escalation_enabled
        chat_quality.settings.universal_model_escalation_model = self._orig_escalation_model
        chat_quality.check_cost_gate = self._orig_check_cost_gate
        chat_quality.get_gate_reserve = self._orig_get_gate_reserve
        chat_quality.calc_cost = self._orig_calc_cost
        chat_quality.record_estimate = self._orig_record_estimate
        chat_quality.record_usage = self._orig_record_usage
        chat_quality.check_divergence = self._orig_check_divergence
        chat_quality.new_correlation_id = self._orig_new_correlation_id

    def test_candidate_review_model_routes_through_factory(self):
        factory = _RecordingFactory()
        chat_quality.settings.universal_review_model = "gemini-3.1-flash-lite"
        chat_quality._model_factory = factory
        orig_from_dict = chat_quality.GenerationConfig.from_dict
        chat_quality.GenerationConfig.from_dict = staticmethod(lambda payload: payload)

        try:
            result, telemetry = chat_quality.review_answer_trace_with_telemetry({
                "latest_question": "What did we check?",
                "final_answer": "The original answer is supported.",
            })
        finally:
            chat_quality.GenerationConfig.from_dict = orig_from_dict

        self.assertEqual(result.quality, "good")
        self.assertEqual(factory.model_ids, ["gemini-3.1-flash-lite"])
        self.assertEqual(telemetry["model"], "gemini-3.1-flash-lite")
        self.assertEqual(telemetry["model_surface"], "chat_quality_review")
        self.assertEqual(telemetry["model_family"], "gemini_3")
        self.assertIsNone(telemetry["thinking_budget"])
        self.assertEqual(telemetry["thinking_level"], "none")
        self.assertEqual(telemetry["max_output_tokens"], 4096)

    def test_default_off_escalation_uses_review_model(self):
        factory = _RecordingFactory()
        chat_quality.settings.universal_model_escalation_enabled = False
        chat_quality.settings.universal_review_model = "gemini-3.1-flash-lite"
        chat_quality._model_factory = factory
        orig_from_dict = chat_quality.GenerationConfig.from_dict
        chat_quality.GenerationConfig.from_dict = staticmethod(lambda payload: payload)

        try:
            _result, telemetry = chat_quality.review_answer_trace_with_telemetry(
                {"latest_question": "Review this answer.", "final_answer": "A"},
                use_escalation=True,
                escalation_reason="weak_review",
            )
        finally:
            chat_quality.GenerationConfig.from_dict = orig_from_dict

        self.assertEqual(factory.model_ids, ["gemini-3.1-flash-lite"])
        self.assertEqual(telemetry["model_surface"], "chat_quality_review")
        self.assertFalse(telemetry["escalation_attempted"])

    def test_enabled_escalation_routes_to_stronger_model_when_mocked(self):
        factory = _RecordingFactory()
        chat_quality.settings.universal_model_escalation_enabled = True
        chat_quality.settings.universal_model_escalation_model = "gemini-3.5-flash"
        chat_quality._model_factory = factory
        orig_from_dict = chat_quality.GenerationConfig.from_dict
        chat_quality.GenerationConfig.from_dict = staticmethod(lambda payload: payload)

        try:
            _result, telemetry = chat_quality.review_answer_trace_with_telemetry(
                {"latest_question": "Review this answer.", "final_answer": "A"},
                use_escalation=True,
                escalation_reason="weak_review",
            )
        finally:
            chat_quality.GenerationConfig.from_dict = orig_from_dict

        self.assertEqual(factory.model_ids, ["gemini-3.5-flash"])
        self.assertEqual(telemetry["model_surface"], "repair_escalation")
        self.assertEqual(telemetry["candidate_mode"], "escalation")
        self.assertTrue(telemetry["escalation_attempted"])

    def test_escalation_cost_gate_failure_falls_back_to_normal_review(self):
        factory = _RecordingFactory()
        chat_quality.settings.universal_model_escalation_enabled = True
        chat_quality.settings.universal_model_escalation_model = "gemini-2.5-flash"
        chat_quality._model_factory = factory
        original_gate = chat_quality.check_cost_gate
        calls = {"gate": 0}

        def flaky_gate(*args, **kwargs):
            calls["gate"] += 1
            if calls["gate"] == 1:
                raise RuntimeError("blocked")
            return None

        chat_quality.check_cost_gate = flaky_gate

        _result, telemetry = chat_quality.review_answer_trace_with_telemetry(
            {"latest_question": "Review this answer.", "final_answer": "A"},
            use_escalation=True,
            escalation_reason="weak_review",
        )

        chat_quality.check_cost_gate = original_gate
        self.assertEqual(telemetry["fallback_reason"], "escalation_cost_gate")
        self.assertTrue(telemetry["escalation_attempted"])
        self.assertEqual(telemetry["model_surface"], "chat_quality_review")

    def test_escalation_provider_failure_falls_back_to_normal_review(self):
        class FailingFactory:
            def __init__(self):
                self.calls = 0

            def __call__(self, model_id: str):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(f"provider failed for {model_id}")
                return _RecordingModel()

        factory = FailingFactory()
        chat_quality.settings.universal_model_escalation_enabled = True
        chat_quality.settings.universal_model_escalation_model = "gemini-3.5-flash"
        chat_quality.settings.universal_review_model = "gemini-3.1-flash-lite"
        chat_quality._model_factory = factory
        orig_from_dict = chat_quality.GenerationConfig.from_dict
        chat_quality.GenerationConfig.from_dict = staticmethod(lambda payload: payload)

        try:
            _result, telemetry = chat_quality.review_answer_trace_with_telemetry(
                {"latest_question": "Review this answer.", "final_answer": "A"},
                use_escalation=True,
                escalation_reason="weak_review",
            )
        finally:
            chat_quality.GenerationConfig.from_dict = orig_from_dict

        self.assertEqual(telemetry["fallback_reason"], "escalation_provider_error")
        self.assertTrue(telemetry["escalation_attempted"])
        self.assertEqual(telemetry["model_surface"], "chat_quality_review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
