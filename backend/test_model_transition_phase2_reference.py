import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_transition_phase2_reference",
    ROOT / "scripts" / "model_transition_phase2_reference.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def case(**overrides):
    value = {
        "case_id": "P1-0001",
        "lane": "A_bounded_structured",
        "latest_question": "Look up these ticket IDs: 950053, 950027.",
        "assistant_answer": "Ticket #950053 and ticket #950027 are present.",
        "messages": [{"role": "user", "content": "Look up these ticket IDs: 950053, 950027."}],
        "evidence_snapshot": {"tickets": [{"id": 950053}, {"id": 950027}]},
        "sources": [],
        "tickets": [{"id": 950053}, {"id": 950027}],
        "target_ticket_id": None,
        "counts": {"tickets": 2},
        "phase1_decision": {"tools": ["lookup"]},
        "policy_flags": {},
        "trace_id": "trace-1",
        "turn_id": "turn-1",
    }
    value.update(overrides)
    return value


class Phase2ReferenceTests(unittest.TestCase):
    def test_exact_ticket_contract_is_verified_good(self):
        self.assertEqual(MODULE.classify_case(case())["label"], "verified_good")

    def test_missing_explicit_ticket_from_answer_is_known_bad_only_when_evidence_has_it(self):
        result = MODULE.classify_case(case(assistant_answer="Ticket #950053 is present."))
        self.assertEqual(result["label"], "known_bad")

    def test_missing_evidence_is_ambiguous_not_known_bad(self):
        result = MODULE.classify_case(case(evidence_snapshot={}, tickets=[], assistant_answer="I cannot find it."))
        self.assertEqual(result["label"], "ambiguous")

    def test_candidate_packet_excludes_evaluator_only_keys(self):
        packet = MODULE.candidate_packet(case())
        self.assertTrue(set(packet).isdisjoint(MODULE.EVALUATOR_ONLY_KEYS))
        self.assertNotIn("assistant_answer", packet)

    def test_adversarial_families_are_complete_and_deterministic(self):
        evaluator, packets, adversarial = MODULE.build_corpus([case()], [])
        self.assertEqual(len(evaluator), 1)
        self.assertEqual(len(packets), 1)
        self.assertEqual(
            {item["adversarial_family"] for item in adversarial},
            set(MODULE.REQUIRED_FAMILIES),
        )
        self.assertEqual(MODULE.build_summary(evaluator, packets, adversarial)["ambiguous_excluded_from_ranking"], 0)

    def test_manifest_hash_and_candidate_output_are_private_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = MODULE.write_corpus(Path(tmp), *MODULE.build_corpus([case()], []))
            self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
            candidate = (Path(tmp) / "candidate_packets.jsonl").read_text()
            self.assertNotIn('"assistant_answer"', candidate)
            self.assertNotIn('"label"', candidate)
            self.assertEqual(json.loads((Path(tmp) / "summary.json").read_text())["cases"], 1)


if __name__ == "__main__":
    unittest.main()
