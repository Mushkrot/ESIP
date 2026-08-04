import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_transition_phase3_replay",
    ROOT / "scripts" / "model_transition_phase3_replay.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def packet(**overrides):
    value = {
        "case_id": "P1-0001",
        "lane": "A_bounded_structured",
        "current_question": "Look up these ticket IDs: 950053, 950027.",
        "conversation_history": [
            {"role": "user", "content": "Look up these ticket IDs: 950053, 950027."},
        ],
        "frozen_evidence": {"tickets": [{"id": 950053}, {"id": 950027}]},
        "sources": [{"kind": "zendesk", "id": "ticket-950012"}, {"kind": "zendesk", "id": "ticket-950007"}],
        "tickets": [{"id": 950053}, {"id": 950027}],
        "target_ticket_id": None,
        "counts": {"tickets": 2},
        "schema_version": "universal_chat_turn.v1",
    }
    value.update(overrides)
    return value


class CaptureAdapter:
    transport_kind = "fake"

    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return MODULE.AdapterResult(
            status="ok",
            latency_ms=1,
            input_tokens=10,
            output_tokens=20,
            thought_tokens=0,
            cost_usd=0.0,
            output_hash=MODULE.sha256_text("fake-output:" + request["request_hash"]),
        )


class LiveLikeAdapter:
    transport_kind = "live"

    def execute(self, request):  # pragma: no cover - must never be called
        raise AssertionError("live adapter must be rejected before execution")


class UnsafeResultAdapter:
    transport_kind = "fake"

    def __init__(self, **result):
        self.result = result

    def execute(self, request):
        return MODULE.AdapterResult(**self.result)


class Phase3ReplayTests(unittest.TestCase):
    def setUp(self):
        self.contract = MODULE.ReplayContract.for_lane("A_bounded_structured")

    def test_request_preserves_real_inputs_and_excludes_answer(self):
        request = MODULE.build_candidate_request(packet(), self.contract)
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True)
        self.assertEqual(request["input"]["conversation_history"], packet()["conversation_history"])
        self.assertEqual(request["input"]["sources"][1]["id"], "ticket-950007")
        self.assertNotIn("assistant_answer", payload)
        self.assertNotIn("reference answer", payload.lower())
        self.assertNotIn("expected_route", payload)

    def test_missing_history_and_evaluator_metadata_are_rejected(self):
        with self.assertRaises(MODULE.PacketValidationError):
            MODULE.build_candidate_request(packet(conversation_history=None), self.contract)
        with self.assertRaises(MODULE.EvaluatorBoundaryError):
            MODULE.build_candidate_request(
                packet(metadata={"reference_answer": "Ticket 950012 is present."}),
                self.contract,
            )

    def test_fingerprints_are_stable_and_drift_sensitive(self):
        first = MODULE.freeze_fingerprints(packet(), self.contract)
        second = MODULE.freeze_fingerprints(packet(), self.contract)
        self.assertEqual(first, second)
        changed = packet(conversation_history=[{"role": "user", "content": "Changed"}])
        changed_fingerprints = MODULE.freeze_fingerprints(changed, self.contract)
        self.assertNotEqual(first["context_sha256"], changed_fingerprints["context_sha256"])
        with self.assertRaises(MODULE.ReplayDriftError):
            MODULE.assert_fingerprint(first, changed_fingerprints)

    def test_evidence_is_not_augmented_and_source_order_is_frozen(self):
        original = packet()
        request = MODULE.build_candidate_request(original, self.contract)
        self.assertEqual(request["input"]["frozen_evidence"], original["frozen_evidence"])
        self.assertEqual(request["input"]["sources"], original["sources"])
        changed_sources = packet(sources=list(reversed(original["sources"])))
        changed = MODULE.freeze_fingerprints(changed_sources, self.contract)
        frozen = MODULE.freeze_fingerprints(original, self.contract)
        self.assertNotEqual(frozen["tool_results_sha256"], changed["tool_results_sha256"])

    def test_generation_config_and_route_profile_drift_is_visible(self):
        original = MODULE.freeze_fingerprints(packet(), self.contract)
        changed_contract = MODULE.ReplayContract(
            lane="A_bounded_structured",
            route_profile_id="candidate-profile-2",
            generation_config={
                "max_output_tokens": 512,
                "thinking_config": {"thinking_budget": 0},
                "temperature": 0.0,
            },
        )
        changed = MODULE.freeze_fingerprints(packet(), changed_contract)
        self.assertNotEqual(original["generation_config_sha256"], changed["generation_config_sha256"])
        self.assertNotEqual(original["route_profile_sha256"], changed["route_profile_sha256"])
        with self.assertRaises(MODULE.ReplayDriftError):
            MODULE.assert_fingerprint(original, changed)

    def test_unknown_profile_live_transport_retry_and_budget_fail_closed(self):
        with self.assertRaises(MODULE.ReplaySafetyError):
            MODULE.ReplayContract(model_id="unknown-model")
        with self.assertRaises(MODULE.ReplaySafetyError):
            MODULE.ReplayContract(max_retries=1)
        with self.assertRaises(MODULE.ReplaySafetyError):
            MODULE.ReplayContract(cost_cap_usd=0.01)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MODULE.ReplaySafetyError):
                runner = MODULE.ReplayRunner(self.contract, LiveLikeAdapter(), Path(tmp) / "attempts.jsonl")
                runner.run([packet()])

    def test_evaluator_rows_stay_outside_capture_adapter(self):
        adapter = CaptureAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            runner = MODULE.ReplayRunner(self.contract, adapter, Path(tmp) / "attempts.jsonl", run_id="run-1")
            summary = runner.run(
                [packet()],
                evaluator_rows=[
                    {
                        "case_id": "P1-0001",
                        "label": "verified_good",
                        "reference_answer": "Ticket 950012 is present.",
                        "assertion": {"must_appear": ["950053"]},
                    }
                ],
            )
        self.assertEqual(summary["completed"], 1)
        serialized_request = json.dumps(adapter.requests[0], ensure_ascii=False, sort_keys=True)
        self.assertNotIn("verified_good", serialized_request)
        self.assertNotIn("reference_answer", serialized_request)
        self.assertNotIn("assertion", serialized_request)

    def test_attempt_record_is_metadata_only_and_binds_all_hashes(self):
        adapter = CaptureAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "attempts.jsonl"
            MODULE.ReplayRunner(self.contract, adapter, journal_path, run_id="run-meta").run([packet()])
            record = MODULE.AttemptJournal(journal_path).records()[0]
        self.assertEqual(set(record["fingerprints"]), set(MODULE.HASH_KEYS))
        self.assertEqual(record["cost_usd"], 0.0)
        self.assertNotIn("output", record)
        self.assertNotIn("answer", json.dumps(record, ensure_ascii=False).lower())

    def test_retry_and_output_overflow_are_rejected_before_recording(self):
        base = {
            "status": "ok",
            "latency_ms": 1,
            "input_tokens": 1,
            "output_tokens": 20,
            "thought_tokens": 0,
            "cost_usd": 0.0,
            "output_hash": MODULE.sha256_text("output"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MODULE.ReplaySafetyError):
                MODULE.ReplayRunner(
                    self.contract,
                    UnsafeResultAdapter(**{**base, "retry_index": 1}),
                    Path(tmp) / "retry.jsonl",
                ).run([packet()])
            with self.assertRaises(MODULE.ReplaySafetyError):
                MODULE.ReplayRunner(
                    self.contract,
                    UnsafeResultAdapter(**{**base, "output_tokens": 2048}),
                    Path(tmp) / "overflow.jsonl",
                ).run([packet()])

    def test_partial_journal_resume_has_no_duplicate_success(self):
        adapter = CaptureAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "attempts.jsonl"
            journal.write_bytes(b'{"attempt_id":"broken"')
            runner = MODULE.ReplayRunner(self.contract, adapter, journal, run_id="run-2")
            first = runner.run([packet()])
            second = runner.run([packet()])
            records = MODULE.AttemptJournal(journal).records()
        self.assertEqual(first["completed"], 1)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(len([row for row in records if row.get("status") == "ok"]), 1)
        self.assertEqual(len(adapter.requests), 1)

    def test_private_runner_handles_mixed_lanes_and_redacts_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packets_path = root / "candidate_packets.jsonl"
            evaluator_path = root / "reference_evaluator.jsonl"
            packets_path.write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False)
                    for item in (packet(), packet(case_id="P1-0002", lane="B_grounded_answer"))
                )
                + "\n",
                encoding="utf-8",
            )
            evaluator_path.write_text(
                json.dumps(
                    {
                        "case_id": "P1-0001",
                        "label": "verified_good",
                        "reference_answer": "private",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "replay"
            first = MODULE.run_private_replay(packets_path, evaluator_path, output, adapter_name="recorded", run_id="mixed")
            second = MODULE.run_private_replay(packets_path, evaluator_path, output, adapter_name="recorded", run_id="mixed")
            manifest = (output / "manifest.json").read_text(encoding="utf-8")
        self.assertEqual(first["completed"], 2)
        self.assertEqual(second["skipped"], 2)
        self.assertNotIn("reference_answer", manifest)
        self.assertNotIn("verified_good", manifest)


if __name__ == "__main__":
    unittest.main()
