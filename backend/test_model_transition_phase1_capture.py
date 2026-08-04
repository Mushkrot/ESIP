import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_transition_phase1_capture",
    ROOT / "scripts" / "model_transition_phase1_capture.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def row(**overrides):
    base = {
        "created_at": "2026-07-01T12:00:00+00:00",
        "conversation_id": "conversation-private",
        "turn_id": "turn-private",
        "trace_id": "trace-private",
        "owner_email": "morgan.chen@northstar.example.com",
        "latest_question": "How many tickets are open?",
        "assistant_answer": "There are 3 tickets.",
        "messages": [{"role": "user", "content": "How many tickets are open?"}],
        "phase1_decision": {"selected_executor": "ticket_semantic_search", "tools": ["search_similar_tickets"]},
        "policy_flags": {},
        "evidence_snapshot": {"tickets": [{"id": 950053}]},
        "sources": [],
        "tickets": [{"id": 950053}],
        "counts": {"tickets": 1, "sources": 0},
        "latency_ms": 1200,
        "model": "gemini-2.5-flash",
        "backend_revision": "rev-1",
        "gcs_payload_uri": "gs://esip-reference-private/payload.json",
    }
    base.update(overrides)
    return base


class Phase1CaptureTests(unittest.TestCase):
    def test_lane_detection_prefers_multimodal_and_followup(self):
        self.assertEqual(MODULE.lane_for_row(row(latest_question="Please inspect this screenshot")), "D_multimodal")
        self.assertEqual(MODULE.lane_for_row(row(latest_question="What about that one?")), "C_contextual_tool_workflow")
        self.assertNotEqual(MODULE.lane_for_row(row(latest_question="Employee Not Found Error", tickets=[{"attachment": "photo.png"}])), "D_multimodal")

    def test_risk_signals_are_deterministic_and_non_destructive(self):
        item = row(assistant_answer="I could not find enough data.", latency_ms=25000)
        self.assertEqual(
            MODULE.risk_signals(item),
            ["weak_or_no_answer_language", "slow_20s_or_more"],
        )
        self.assertEqual(item["assistant_answer"], "I could not find enough data.")
        self.assertNotIn(
            "tool_or_query_error_language",
            MODULE.risk_signals(row(assistant_answer="The ticket is open.", evidence_snapshot={"note": "ticket reported an error"})),
        )

    def test_selection_is_bounded_per_lane(self):
        rows = [row(latest_question=f"What about that one? {index}") for index in range(20)]
        selected = MODULE.select_cases(rows, per_lane=3)
        counts = {}
        for item in selected:
            lane = MODULE.lane_for_row(item)
            counts[lane] = counts.get(lane, 0) + 1
        self.assertLessEqual(max(counts.values()), 3)

    def test_manifest_and_private_snapshot_are_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot"
            summary = MODULE.summarize([row()], [row()], start="2026-07-01T00:00:00+00:00", end="2026-07-02T00:00:00+00:00", limit=10, per_lane=1)
            manifest = MODULE.write_snapshot(output, [row()], [row()], summary)
            self.assertRegex(manifest["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(json.loads((output / "manifest.json").read_text())["files"]["baseline_cases.jsonl"]["rows"], 1)

    def test_no_raw_content_is_emitted_by_summary(self):
        raw = row(latest_question="PRIVATE QUESTION", assistant_answer="PRIVATE ANSWER")
        summary = MODULE.summarize([raw], [raw], start="a", end="b", limit=1, per_lane=1)
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("PRIVATE QUESTION", text)
        self.assertNotIn("PRIVATE ANSWER", text)


if __name__ == "__main__":
    unittest.main()
