import hashlib
import unittest

from app.services import model_transition_request_capture as capture
from scripts import model_transition_phase4_exact_gate as gate


def complete_packet() -> dict:
    envelope = capture.build_envelope(
        surface="universal_chat.phase2_final_answer",
        model="gemini-2.5-flash",
        input_kind="contents",
        input_value=[{"role": "user", "parts": [{"text": "private question"}]}],
        generation_config={"max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}},
        tool_config={"function_calling_config": {"mode": "NONE"}},
        stream=True,
    )
    answer = "Private answer"
    return {
        "case_id": "P3-verified-1",
        "source": "live_phase2_capture",
        "gcs_payload_uri": "gs://esip-reference-private/turn.json",
        "collector_hash_valid": True,
        "envelope": envelope,
        "assistant_answer": answer,
        "assistant_answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "final_behavior": {"answer_policy": {}, "route": "universal_chat"},
    }


class ExactBaselineGateTests(unittest.TestCase):
    def test_no_live_packets_blocks_phase(self):
        result = gate.evaluate_exact_baseline_gate([], eligible_case_ids={"P3-verified-1"})
        self.assertEqual(result.status, "blocked")
        self.assertIn("no fresh live Phase 2 capture packets are available", result.gaps)

    def test_complete_live_packet_is_ready_but_does_not_claim_replay(self):
        result = gate.evaluate_exact_baseline_gate(
            [complete_packet()], eligible_case_ids={"P3-verified-1"}
        )
        self.assertEqual(result.status, "ready_for_owner_gated_replay")
        self.assertEqual(result.accepted_case_ids, ("P3-verified-1",))

    def test_tampered_request_or_answer_fails_closed(self):
        packet = complete_packet()
        packet["envelope"]["request"]["generation_config"]["max_output_tokens"] = 1
        packet["assistant_answer_sha256"] = "0" * 64
        result = gate.evaluate_exact_baseline_gate([packet], eligible_case_ids={"P3-verified-1"})
        self.assertEqual(result.status, "blocked")
        self.assertTrue(any("hash" in gap for gap in result.gaps))

    def test_historical_or_wrong_surface_is_rejected(self):
        packet = complete_packet()
        packet["source"] = "historical_reconstruction"
        packet["envelope"]["surface"] = "universal_chat.phase1_planner"
        result = gate.evaluate_exact_baseline_gate([packet], eligible_case_ids={"P3-verified-1"})
        self.assertEqual(result.status, "blocked")
        self.assertTrue(any("live Phase 2" in gap for gap in result.gaps))
        self.assertTrue(any("final Universal Chat" in gap for gap in result.gaps))

    def test_non_eligible_case_cannot_open_gate(self):
        packet = complete_packet()
        packet["case_id"] = "P3-ambiguous-1"
        result = gate.evaluate_exact_baseline_gate([packet], eligible_case_ids={"P3-verified-1"})
        self.assertEqual(result.status, "blocked")
        self.assertTrue(any("ranking-eligible" in gap for gap in result.gaps))


if __name__ == "__main__":
    unittest.main()
