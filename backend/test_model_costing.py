"""Model-aware cost estimate tests.

Run from project root:
scripts/backend_python.sh backend/test_model_costing.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.config import settings
from app.services import cost_tracker, model_routing


class ModelCostingTest(unittest.TestCase):
    def test_reference_runtime_uses_bootstrap_rates_without_cloud_lookup(self):
        original_rates = cost_tracker._RUNTIME_RATES
        original_rates_mono = cost_tracker._RUNTIME_RATES_MONO
        cost_tracker._RUNTIME_RATES = None
        cost_tracker._RUNTIME_RATES_MONO = 0.0

        try:
            with (
                mock.patch.object(settings, "runtime_profile", "reference"),
                mock.patch.object(
                    cost_tracker,
                    "_try_load_rates_from_bq",
                    side_effect=AssertionError("reference runtime must not contact BigQuery"),
                ),
            ):
                rates = cost_tracker._runtime_rates()
        finally:
            cost_tracker._RUNTIME_RATES = original_rates
            cost_tracker._RUNTIME_RATES_MONO = original_rates_mono

        self.assertEqual(rates, cost_tracker.BOOTSTRAP_RATES)

    def test_calc_cost_current_behavior_remains_backward_compatible(self):
        legacy = cost_tracker.calc_cost(1000, 2000, 300)
        expected = (
            (1000 / 1_000_000) * cost_tracker.BOOTSTRAP_RATES["gemini_input_per_m"]
            + (2000 / 1_000_000) * cost_tracker.BOOTSTRAP_RATES["gemini_output_per_m"]
            + (300 / 1_000_000) * cost_tracker.BOOTSTRAP_RATES["gemini_thinking_per_m"]
        )

        self.assertAlmostEqual(legacy, expected, places=8)

    def test_candidate_estimates_are_deterministic_and_model_specific(self):
        lite = cost_tracker.estimate_model_cost_cad(
            model_id="gemini-3.1-flash-lite",
            input_tokens=10_000,
            output_tokens=2_000,
        )
        stronger = cost_tracker.estimate_model_cost_cad(
            model_id="gemini-3.5-flash",
            input_tokens=10_000,
            output_tokens=2_000,
        )

        self.assertGreater(stronger.expected_cost_cad, lite.expected_cost_cad)
        self.assertEqual(lite.pricing_snapshot_date, cost_tracker.MODEL_PRICING_SNAPSHOT_DATE)
        self.assertIn("non-global", stronger.price_basis)

    def test_estimates_change_with_output_tokens(self):
        small = cost_tracker.estimate_model_cost_cad(
            model_id="gemini-3.1-flash-lite",
            input_tokens=1_000,
            output_tokens=100,
        )
        large = cost_tracker.estimate_model_cost_cad(
            model_id="gemini-3.1-flash-lite",
            input_tokens=1_000,
            output_tokens=10_000,
        )

        self.assertGreater(large.expected_cost_cad, small.expected_cost_cad)

    def test_cost_gate_reserve_comes_from_candidate_pricing(self):
        estimate = cost_tracker.estimate_model_cost_cad(
            model_id="gemini-3.5-flash",
            input_tokens=5_000,
            output_tokens=1_000,
            reserve_multiplier=3.0,
        )

        self.assertAlmostEqual(estimate.reserved_cost_cad, estimate.expected_cost_cad * 3.0, places=8)

    def test_unknown_model_requires_pricing_before_use(self):
        with self.assertRaises(ValueError):
            cost_tracker.estimate_model_cost_cad(
                model_id="gemini-future",
                input_tokens=1,
                output_tokens=1,
            )

    def test_routing_telemetry_excludes_sensitive_fields(self):
        class Settings:
            gemini_model = "gemini-2.5-flash"
            universal_review_model = ""

        profile = model_routing.resolve_surface_model_profile(
            Settings,
            model_routing.ModelSurface.CHAT_QUALITY_REVIEW,
        )
        payload = model_routing.telemetry_payload(
            profile,
            candidate_mode="baseline",
            escalation_reason="validator_weak_evidence",
        )

        self.assertEqual(payload["model_id"], "gemini-2.5-flash")
        self.assertEqual(payload["model_surface"], "chat_quality_review")
        self.assertEqual(payload["escalation_reason"], "validator_weak_evidence")
        for forbidden in ("prompt", "answer", "raw_answer", "tool_output", "secret"):
            self.assertNotIn(forbidden, payload)

    def test_pricing_snapshot_has_required_candidate_models(self):
        self.assertIn("gemini-2.5-flash", cost_tracker.MODEL_TOKEN_PRICING_USD)
        self.assertIn("gemini-3.1-flash-lite", cost_tracker.MODEL_TOKEN_PRICING_USD)
        self.assertIn("gemini-3.5-flash", cost_tracker.MODEL_TOKEN_PRICING_USD)
        self.assertEqual(cost_tracker.MODEL_PRICING_SOURCE_URL, "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
