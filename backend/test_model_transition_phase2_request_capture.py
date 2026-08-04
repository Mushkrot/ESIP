import copy
import inspect
import math
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import model_transition_request_capture as capture
from app.services import universal_chat
from scripts import model_transition_phase2_collect as collector


def _envelope():
    return capture.build_envelope(
        surface="universal_chat.phase2_final_answer",
        model="gemini-2.5-flash",
        input_kind="contents",
        input_value=[{"role": "user", "parts": [{"text": "private question"}]}],
        generation_config={"max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}},
        tool_config={"function_calling_config": {"mode": "NONE"}},
        stream=True,
    )


class ModelTransitionPhase2RequestCaptureTests(unittest.TestCase):
    def test_build_is_stable_and_does_not_mutate_input(self):
        contents = [{"role": "user", "parts": [{"text": "private question"}]}]
        before = copy.deepcopy(contents)
        first = capture.build_envelope(
            surface="universal_chat.phase2_final_answer",
            model="gemini-2.5-flash",
            input_kind="contents",
            input_value=contents,
            generation_config={"max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}},
            tool_config={"function_calling_config": {"mode": "NONE"}},
            stream=True,
        )
        second = _envelope()
        self.assertEqual(contents, before)
        self.assertEqual(first["request_sha256"], second["request_sha256"])
        self.assertNotEqual(first["request"], {})

    def test_receipt_never_contains_raw_input(self):
        receipt = capture.compact_receipt(_envelope())
        self.assertNotIn("request", receipt)
        self.assertNotIn("private question", str(receipt))
        self.assertEqual(receipt["model"], "gemini-2.5-flash")

    def test_rejects_nonfinite_and_unknown_values(self):
        with self.assertRaises(capture.CaptureValueError):
            capture.canonical_json_bytes({"value": math.nan})
        with self.assertRaises(capture.CaptureValueError):
            capture.canonical_json_bytes({"value": object()})

    def test_collector_returns_receipts_only_and_validates_hash(self):
        envelope = _envelope()
        payload = {"trace_id": "trace-1", "turn_id": "turn-1", "model_transition_request_captures": [envelope]}
        summary = collector.summarize_payload(payload, "gemini-2.5-flash")
        self.assertTrue(summary["captures"][0]["hash_valid"])
        self.assertTrue(summary["captures"][0]["expected_model"])
        self.assertNotIn("private question", str(summary))

    def test_collector_detects_tampered_capture(self):
        envelope = _envelope()
        envelope["request"]["generation_config"]["max_output_tokens"] = 1
        summary = collector.summarize_payload(
            {"model_transition_request_captures": [envelope]}, "gemini-2.5-flash"
        )
        self.assertFalse(summary["captures"][0]["hash_valid"])

    def test_capture_callback_failure_is_fail_open(self):
        universal_chat._try_capture_request(
            lambda _envelope: (_ for _ in ()).throw(RuntimeError("capture storage down")),
            surface="universal_chat.phase1_planner",
            model="gemini-2.5-flash",
            input_kind="text",
            input_value="question",
            generation_config={"max_output_tokens": 1024},
            tool_config=None,
            stream=False,
        )

    def test_final_capture_is_immediately_before_provider_transport(self):
        source = inspect.getsource(universal_chat.chat_stream)
        self.assertLess(source.index("_try_capture_request("), source.index("_model.generate_content("))
        self.assertIn('surface="universal_chat.phase2_final_answer"', source)

    def test_final_model_boundary_receives_unchanged_request_after_capture(self):
        captured = []

        def generate_content(contents, **kwargs):
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["request"]["input_value"], contents)
            self.assertTrue(kwargs["stream"])
            return [SimpleNamespace(text="answer", usage_metadata=None)]

        with (
            mock.patch.object(universal_chat, "estimate_tokens_from_text", return_value=1),
            mock.patch.object(universal_chat, "calc_cost", return_value=0),
            mock.patch.object(universal_chat, "get_gate_reserve", return_value=0),
            mock.patch.object(universal_chat, "new_correlation_id", return_value="corr"),
            mock.patch.object(universal_chat, "check_cost_gate"),
            mock.patch.object(universal_chat, "record_estimate"),
            mock.patch.object(universal_chat, "_model", SimpleNamespace(generate_content=generate_content)),
        ):
            answer = "".join(universal_chat.chat_stream(
                messages=[{"role": "user", "content": "question"}],
                capture_callback=captured.append,
            ))

        self.assertEqual(answer, "answer")
        self.assertEqual(captured[0]["surface"], "universal_chat.phase2_final_answer")

    def test_planner_boundary_captures_exact_prompt_before_transport(self):
        captured = []

        def generate_content(prompt, **kwargs):
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["request"]["input_value"], prompt)
            return SimpleNamespace(
                text='{"tools": [], "reasoning": "none"}',
                usage_metadata=None,
            )

        with (
            mock.patch.object(universal_chat, "estimate_tokens_from_text", return_value=1),
            mock.patch.object(universal_chat, "calc_cost", return_value=0),
            mock.patch.object(universal_chat, "get_gate_reserve", return_value=0),
            mock.patch.object(universal_chat, "new_correlation_id", return_value="corr"),
            mock.patch.object(universal_chat, "check_cost_gate"),
            mock.patch.object(universal_chat, "record_estimate"),
            mock.patch.object(universal_chat, "_model", SimpleNamespace(generate_content=generate_content)),
        ):
            decision = universal_chat.decide_tools(
                [{"role": "user", "content": "question"}],
                capture_callback=captured.append,
            )

        self.assertEqual(decision["tools"], [])
        self.assertEqual(captured[0]["surface"], "universal_chat.phase1_planner")
