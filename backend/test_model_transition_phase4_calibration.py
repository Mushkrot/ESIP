import importlib.util
import json
import sys
import unittest

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module(
    "model_transition_phase4_calibrate",
    ROOT / "scripts" / "model_transition_phase4_calibrate.py",
)
PHASE3 = load_module(
    "model_transition_phase3_replay_for_phase4_tests",
    ROOT / "scripts" / "model_transition_phase3_replay.py",
)


def baseline_case(case_id: str = "P1-0001") -> dict:
    return {
        "case_id": case_id,
        "lane": "A_bounded_structured",
        "latest_question": "Look up these ticket IDs: 950053, 950027.",
        "assistant_answer": "Ticket #950053 and ticket #950027 are present.",
        "messages": [{"role": "user", "content": "Look up these ticket IDs: 950053, 950027."}],
        "evidence_snapshot": {
            "tickets": [{"id": 950053}, {"id": 950027}],
            "semantic_frame": {},
        },
        "sources": [],
        "tickets": [{"id": 950053}, {"id": 950027}],
        "target_ticket_id": None,
        "counts": {"tickets": 2},
        "phase1_decision": {"tools": ["lookup"]},
        "policy_flags": {
            "selected_executor": "phase1_tools",
            "semantic_frame": {
                "intent": "ticket_details",
                "target_context": "explicit_ticket",
                "task": {"task_type": "general_answer"},
                "answer_language": "English",
            },
        },
        "schema_version": "universal_chat_turn.v1",
    }


def bundle(*, label: str = "verified_good") -> tuple[list[dict], list[dict], list[dict]]:
    case = baseline_case()
    phase2 = load_module(
        "model_transition_phase2_reference_for_phase4_tests",
        ROOT / "scripts" / "model_transition_phase2_reference.py",
    )
    packet = phase2.candidate_packet(case)
    evaluator = {
        "case_id": case["case_id"],
        "label": label,
        "rationale": "test",
        "assertion": {},
        "reference_answer": case["assistant_answer"],
        "route_metadata": {
            "phase1_decision": case["phase1_decision"],
            "policy_flags": case["policy_flags"],
        },
        "quality_events": [],
    }
    contract = PHASE3.ReplayContract.for_lane(packet["lane"])
    attempts = [{
        "schema": "esip.model_transition.phase3.attempt.v1",
        "run_id": "test:A_bounded_structured",
        "case_id": case["case_id"],
        "attempt_id": "test:A_bounded_structured:P1-0001:1",
        "attempt_index": 1,
        "retry_of": None,
        "status": "ok",
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thought_tokens": 0,
        "cost_usd": 0.0,
        "output_hash": PHASE3.sha256_text("test"),
        "error_code": None,
        "fingerprints": PHASE3.freeze_fingerprints(packet, contract),
    }]
    return [case], [packet], [evaluator], attempts


class Phase4CalibrationTests(unittest.TestCase):
    def test_missing_or_extra_case_ids_fail_closed(self):
        phase1, packets, evaluators, attempts = bundle()
        packets[0] = dict(packets[0], case_id="P1-extra")
        with self.assertRaises(MODULE.CalibrationError):
            MODULE.calibrate_records(phase1, packets, evaluators, attempts)

    def test_route_projection_drift_fails_closed(self):
        phase1, packets, evaluators, attempts = bundle()
        evaluators[0] = dict(evaluators[0], route_metadata={
            "phase1_decision": phase1[0]["phase1_decision"],
            "policy_flags": {"selected_executor": "wrong_route"},
        })
        with self.assertRaises(MODULE.CalibrationDriftError):
            MODULE.calibrate_records(phase1, packets, evaluators, attempts)

    def test_fingerprint_drift_fails_closed(self):
        phase1, packets, evaluators, attempts = bundle()
        attempts[0] = dict(attempts[0], fingerprints=dict(attempts[0]["fingerprints"], prompt_sha256="0" * 64))
        with self.assertRaises(MODULE.CalibrationDriftError):
            MODULE.calibrate_records(phase1, packets, evaluators, attempts)

    def test_evaluator_fields_in_packet_are_rejected(self):
        phase1, packets, evaluators, attempts = bundle()
        packets[0] = dict(packets[0], reference_answer="leak")
        with self.assertRaises(MODULE.EvaluatorBoundaryError):
            MODULE.calibrate_records(phase1, packets, evaluators, attempts)

    def test_ambiguous_rows_are_excluded_and_prompt_gap_is_explicit(self):
        phase1, packets, evaluators, attempts = bundle(label="ambiguous")
        result = MODULE.calibrate_records(phase1, packets, evaluators, attempts)
        self.assertEqual(result["labels"], {"ambiguous": 1})
        self.assertEqual(result["verified_good"]["selected"], 0)
        self.assertEqual(result["verified_good"]["scored"], 0)
        self.assertFalse(result["prompt_reconstruction"]["serialized_production_prompt_available"])
        self.assertFalse(result["prompt_reconstruction"]["model_variance_claim_permitted"])

    def test_verified_good_scope_evidence_and_language_checks_pass(self):
        phase1, packets, evaluators, attempts = bundle()
        result = MODULE.calibrate_records(phase1, packets, evaluators, attempts)
        self.assertEqual(result["verified_good"]["selected"], 1)
        self.assertEqual(result["verified_good"]["scored"], 1)
        self.assertEqual(result["verified_good"]["scope_pass"], 1)
        self.assertEqual(result["verified_good"]["evidence_pass"], 1)
        self.assertEqual(result["verified_good"]["language_pass"], 1)

    def test_calibrator_has_no_live_transport_imports(self):
        source = (ROOT / "scripts" / "model_transition_phase4_calibrate.py").read_text(encoding="utf-8")
        self.assertNotIn("google.genai", source)
        self.assertNotIn("vertexai", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("gcloud", source)
        self.assertNotIn("subprocess", source)


if __name__ == "__main__":
    unittest.main()
