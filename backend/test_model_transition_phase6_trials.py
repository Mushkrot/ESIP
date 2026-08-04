import inspect
import json
import tempfile
import unittest
from pathlib import Path

from scripts import model_transition_phase6_trials as trials


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def synthetic_phase2_dir(root: Path, *, include_lane_a: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    packets = []
    evaluator = []
    for index in range(10):
        lane = "A_bounded_structured" if include_lane_a or index >= 2 else "B_grounded_answer"
        case_id = f"SYN-{index:04d}"
        packets.append(
            {
                "case_id": case_id,
                "lane": lane,
                "current_question": f"Question {index}",
                "conversation_history": [],
                "frozen_evidence": {"source": f"source-{index}"},
                "sources": [],
                "tickets": [],
                "target_ticket_id": None,
                "counts": {},
                "schema_version": "universal_chat_turn.v1",
            }
        )
        evaluator.append(
            {
                "case_id": case_id,
                "label": "verified_good" if index < 2 else "ambiguous",
            }
        )
    families = ("context_loss", "date_scope", "empty_evidence", "bad_identifier", "prompt_injection")
    adversarial = [
        {
            "case_id": f"SYN-000{index}--{family}",
            "parent_case_id": f"SYN-000{index}",
            "lane": "A_bounded_structured",
            "adversarial_family": family,
        }
        for index, family in enumerate(families)
    ]
    write_jsonl(root / "candidate_packets.jsonl", packets)
    write_jsonl(root / "reference_evaluator.jsonl", evaluator)
    write_jsonl(root / "adversarial_packets.jsonl", adversarial)
    return root


class Phase6TrialTests(unittest.TestCase):
    def test_inventory_is_deterministic_protected_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = trials.build_inventory(synthetic_phase2_dir(Path(tmp)))
            again = trials.build_inventory(Path(tmp))

        self.assertEqual(inventory, again)
        self.assertEqual(inventory["lane"], "A_bounded_structured")
        self.assertEqual(inventory["selected_base_case_count"], 5)
        self.assertEqual(inventory["protected_verified_good_count"], 2)
        self.assertEqual(inventory["selected_adversarial_case_count"], 5)
        self.assertEqual(
            {case_id.split("--", 1)[0] for case_id in inventory["selected_adversarial_case_ids"]},
            {"SYN-0000", "SYN-0001", "SYN-0002", "SYN-0003", "SYN-0004"},
        )
        self.assertTrue(inventory["rankable_protected_lane"])
        self.assertNotIn("current_question", inventory)
        self.assertNotIn("frozen_evidence", inventory)
        self.assertEqual(len(inventory["selection_manifest_sha256"]), 64)

    def test_wave_requires_exact_packet_and_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = trials.build_wave_spec(
                synthetic_phase2_dir(Path(tmp)),
                wave_id="MT-P6-A-20250115-W1",
                cad_fx_rate=1.40,
            )

        self.assertEqual(spec.approval_status, "pending_owner_approval")
        self.assertEqual(spec.candidate_profile_id, "candidate-gemini-3.5-flash-lite-lane-a-bounded")
        self.assertEqual(spec.max_attempts, 1)
        self.assertEqual(spec.transport_status, "quarantined")
        self.assertEqual(len(spec.base_case_ids), 5)
        self.assertEqual(len(spec.adversarial_case_ids), 5)
        self.assertEqual(spec.usd_hard_cap, 0.32)
        self.assertEqual(spec.cad_hard_cap, 0.45)
        with self.assertRaises(trials.TrialAuthorityError):
            trials.require_approved(spec)

    def test_budget_overflow_and_invalid_lane_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = synthetic_phase2_dir(Path(tmp))
            with self.assertRaises(trials.TrialPlanningError):
                trials.build_wave_spec(corpus, wave_id="MT-P6-A-LOW-CAP", usd_hard_cap=0.001)

            with self.assertRaises(trials.TrialPlanningError):
                trials.build_wave_spec(corpus, lane="B_grounded_answer")

    def test_decision_matrix_never_ranks_ambiguous_results(self):
        self.assertEqual(
            trials.classify_observation(evidence_available=False),
            {"classification": "evidence_gap", "rankable": False},
        )
        self.assertEqual(
            trials.classify_observation(harness_ok=False),
            {"classification": "harness_or_route_drift", "rankable": False},
        )
        self.assertEqual(
            trials.classify_observation(product_rule_clear=False),
            {"classification": "product_rule_ambiguity", "rankable": False},
        )
        self.assertEqual(
            trials.classify_observation(protected_regressions=1),
            {"classification": "model_capability", "rankable": True},
        )
        self.assertEqual(
            trials.classify_observation(),
            {"classification": "candidate_pass", "rankable": True},
        )

    def test_candidate_request_fingerprint_is_blind_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = synthetic_phase2_dir(Path(tmp))
            packet = json.loads((corpus / "candidate_packets.jsonl").read_text().splitlines()[0])
            fingerprint = trials.candidate_request_fingerprint(packet)

        self.assertEqual(fingerprint["profile_id"], "candidate-gemini-3.5-flash-lite-lane-a-bounded")
        self.assertEqual(fingerprint["transport_status"], "quarantined")
        self.assertEqual(len(fingerprint["request_sha256"]), 64)
        self.assertNotIn("current_question", fingerprint)
        self.assertNotIn("source-0", json.dumps(fingerprint))

    def test_module_has_no_live_transport_imports(self):
        source = inspect.getsource(trials)
        for marker in ("import google", "from google", "import requests", "import socket", "import subprocess", "import urllib"):
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
