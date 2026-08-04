"""Universal Chat integration helpers for output critic bounded repair."""

from __future__ import annotations

import unittest
from unittest import mock

from app.routers import universal
from app.services import output_critic_bounded_repair


class UniversalOutputCriticBoundedRepairTest(unittest.TestCase):
    def test_disabled_config_preserves_visible_answer(self):
        answer = "No matching tickets found."
        with mock.patch.object(
            output_critic_bounded_repair,
            "output_critic_bounded_repair_enabled",
            return_value=False,
        ):
            repaired_answer, stream_delta, metadata = universal._resolve_output_critic_bounded_repair_payload(
                latest_question="Question",
                visible_answer=answer,
                output_critic_trace={
                    "attempted": True,
                    "risk_level": "high",
                    "risk_codes": ["tool_error_as_absence"],
                    "confidence": 0.92,
                },
                trace_context={"policy_flags": {"bq_error": "query failed"}},
            )

        self.assertEqual(repaired_answer, answer)
        self.assertEqual(stream_delta, "")
        self.assertFalse(metadata["answer_affecting"])
        self.assertFalse(metadata["attempted"])
        self.assertEqual(metadata["skipped_reason"], "disabled_by_config")

    def test_enabled_repair_appends_stream_safe_correction_delta(self):
        answer = "No matching tickets found."
        with mock.patch.object(
            output_critic_bounded_repair,
            "output_critic_bounded_repair_enabled",
            return_value=True,
        ):
            repaired_answer, stream_delta, metadata = universal._resolve_output_critic_bounded_repair_payload(
                latest_question="Question",
                visible_answer=answer,
                output_critic_trace={
                    "attempted": True,
                    "risk_level": "high",
                    "risk_codes": ["tool_error_as_absence"],
                    "confidence": 0.92,
                },
                trace_context={"policy_flags": {"bq_error": "query failed"}},
            )

        self.assertTrue(metadata["answer_affecting"])
        self.assertTrue(metadata["repair_applied"])
        self.assertEqual(metadata["repair_kind"], "false_no_data_or_tool_error")
        self.assertIn(answer, repaired_answer)
        self.assertIn("Correction:", stream_delta)
        self.assertIn("unverified", repaired_answer)
        self.assertNotIn("query failed", repr(metadata))

    def test_unsupported_or_low_risk_does_not_change_answer(self):
        answer = "This answer is supported by the cited evidence."
        with mock.patch.object(
            output_critic_bounded_repair,
            "output_critic_bounded_repair_enabled",
            return_value=True,
        ):
            repaired_answer, stream_delta, metadata = universal._resolve_output_critic_bounded_repair_payload(
                latest_question="Question",
                visible_answer=answer,
                output_critic_trace={
                    "attempted": True,
                    "risk_level": "low",
                    "risk_codes": ["weak_evidence"],
                    "confidence": 0.7,
                },
                trace_context={"counts": {"sources": 2}},
            )

        self.assertEqual(repaired_answer, answer)
        self.assertEqual(stream_delta, "")
        self.assertFalse(metadata["answer_affecting"])
        self.assertEqual(metadata["skipped_reason"], "risk_level_not_high")


if __name__ == "__main__":
    unittest.main()
