import json
import unittest

from scripts import model_transition_phase3_verified_corpus as corpus


def _lookup_case(answer: str) -> dict:
    return {
        "latest_question": "Find ticket IDs: 950053, 950054",
        "assistant_answer": answer,
        "evidence_snapshot": {"tickets": [{"id": 950053}, {"id": 950054}]},
        "tickets": [{"id": 950053}, {"id": 950054}],
        "messages": [{"role": "user", "content": "Find ticket IDs: 950053, 950054"}],
        "sources": [],
        "counts": {},
    }


class VerifiedTraceCorpusTests(unittest.TestCase):
    def test_independent_ticket_evidence_labels_good(self):
        result = corpus.classify_universal_trace(_lookup_case("Tickets #950053 and #950054."))
        self.assertEqual(result["label"], "verified_good")

    def test_missing_ticket_from_answer_labels_known_bad(self):
        result = corpus.classify_universal_trace(_lookup_case("Ticket #950053."))
        self.assertEqual(result["label"], "known_bad")

    def test_general_quality_signal_stays_ambiguous(self):
        result = corpus.classify_universal_trace({"latest_question": "Why did this happen?", "assistant_answer": "Because."})
        self.assertEqual(result["label"], "ambiguous")

    def test_activity_observations_cover_every_surface_and_stay_nonranking(self):
        observations = corpus.activity_observations({"analytics_answer": {"actual_calls": 3, "first_seen": "a", "last_seen": "b"}})
        self.assertEqual({item["surface"] for item in observations}, set(corpus.SURFACE_CALL_TYPES))
        analytics = next(item for item in observations if item["surface"] == "analytics_answer")
        self.assertEqual(analytics["actual_calls"], 3)
        self.assertEqual(analytics["label"], "ambiguous")
        self.assertFalse(analytics["ranking_eligible"])

    def test_candidate_packet_excludes_evaluator_only_fields(self):
        case = _lookup_case("Tickets #950053 and #950054.")
        case.update({"case_id": "UC-0001", "phase1_decision": {"tools": []}, "policy_flags": {"secret": "x"}})
        packet = corpus.candidate_packet(case)
        self.assertTrue(set(packet).isdisjoint(corpus.EVALUATOR_ONLY_KEYS))
        self.assertNotIn("assistant_answer", packet)
        self.assertNotIn("phase1_decision", packet)

    def test_summary_is_redacted_and_counts_labels(self):
        evaluator, packets, _ = corpus.build_trace_cases([_lookup_case("Tickets #950053 and #950054.")])
        observations = corpus.activity_observations({})
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = corpus.write_corpus(Path(temp_dir), evaluator, packets, observations, [], None)
            summary = manifest["summary"]
            self.assertEqual(summary["labels"]["verified_good"], 1)
            self.assertNotIn("950053", json.dumps(summary))

    def test_selection_keeps_known_bad_before_ambiguous_and_bounds_each_label(self):
        rows = [
            _lookup_case("Ticket #950053."),
            _lookup_case("Tickets #950053 and #950054."),
            {"latest_question": "General question", "assistant_answer": "General answer"},
        ]
        selected = corpus.select_trace_rows(rows, per_label=1)
        labels = [corpus.classify_universal_trace(item)["label"] for item in selected]
        self.assertEqual(labels, ["known_bad", "verified_good", "ambiguous"])

    def test_ocr_header_contract_failures_are_known_bad_but_not_replay_eligible(self):
        cases = corpus.ocr_contract_cases([{"ticket_id": 950053, "processed_at": "2026-07-01T00:00:00Z"}])
        self.assertEqual(cases[0]["label"], "known_bad")
        self.assertFalse(cases[0]["ranking_eligible"])
        self.assertEqual(cases[0]["assertion"]["required_prefix"], "[IMAGE DESCRIPTION:]")
