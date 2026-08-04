"""Model routing contract tests.

Run from project root:
scripts/backend_python.sh backend/test_model_routing.py
"""

from __future__ import annotations

import unittest

from app.services import model_routing


class _Settings:
    gemini_model = "gemini-2.5-flash"
    universal_validator_model = ""
    universal_review_model = ""
    universal_model_escalation_model = ""


class ModelRoutingTest(unittest.TestCase):
    def test_default_surfaces_resolve_to_global_model(self):
        for surface in model_routing.ModelSurface:
            with self.subTest(surface=surface.value):
                profile = model_routing.resolve_surface_model_profile(_Settings, surface)
                self.assertEqual(profile.model_id, _Settings.gemini_model)
                self.assertEqual(profile.family, model_routing.ModelFamily.GEMINI_2_5)

    def test_gemini_2_5_profile_uses_thinking_budget(self):
        profile = model_routing.resolve_surface_model_profile(
            _Settings,
            model_routing.ModelSurface.ANSWER_VALIDATOR,
        )
        payload = model_routing.generation_config_payload(profile)

        self.assertEqual(payload["max_output_tokens"], 2048)
        self.assertEqual(payload["thinking_config"], {"thinking_budget": 0})
        self.assertNotIn("thinking_level", payload["thinking_config"])

    def test_gemini_3_flash_lite_profile_uses_thinking_level(self):
        profile = model_routing.resolve_surface_model_profile(
            _Settings,
            model_routing.ModelSurface.ANSWER_VALIDATOR,
            override_model_id="gemini-3.1-flash-lite",
        )
        payload = model_routing.generation_config_payload(profile)

        self.assertEqual(profile.family, model_routing.ModelFamily.GEMINI_3)
        self.assertEqual(profile.cost_tier_label, "candidate_low")
        self.assertEqual(payload["thinking_config"], {"thinking_level": "none"})
        self.assertNotIn("thinking_budget", payload["thinking_config"])

    def test_gemini_3_5_profile_uses_stronger_cost_label(self):
        profile = model_routing.resolve_surface_model_profile(
            _Settings,
            model_routing.ModelSurface.REPAIR_ESCALATION,
            override_model_id="gemini-3.5-flash",
        )

        self.assertEqual(profile.family, model_routing.ModelFamily.GEMINI_3)
        self.assertEqual(profile.cost_tier_label, "stronger_candidate")
        self.assertEqual(model_routing.generation_config_payload(profile)["thinking_config"], {"thinking_level": "none"})

    def test_unknown_override_falls_back_to_global_model(self):
        profile = model_routing.resolve_surface_model_profile(
            _Settings,
            model_routing.ModelSurface.CHAT_QUALITY_REVIEW,
            override_model_id="some-future-model",
        )

        self.assertEqual(profile.model_id, _Settings.gemini_model)
        self.assertEqual(profile.fallback_reason, "unknown_override_model_family")

    def test_unknown_global_model_fails_closed(self):
        class BadSettings:
            gemini_model = "some-future-model"
            universal_validator_model = ""

        with self.assertRaises(model_routing.ModelRoutingError):
            model_routing.resolve_surface_model_profile(BadSettings, model_routing.ModelSurface.ANSWER_VALIDATOR)

    def test_disabled_empty_override_preserves_global_model(self):
        class SettingsWithOverride:
            gemini_model = "gemini-2.5-flash"
            universal_validator_model = ""

        profile = model_routing.resolve_surface_model_profile(
            SettingsWithOverride,
            model_routing.ModelSurface.ANSWER_VALIDATOR,
        )

        self.assertEqual(profile.model_id, "gemini-2.5-flash")

    def test_illegal_config_rejects_mixed_thinking_controls(self):
        profile = model_routing.ModelProfile(
            surface=model_routing.ModelSurface.ANSWER_VALIDATOR,
            model_id="gemini-3.1-flash-lite",
            family=model_routing.ModelFamily.GEMINI_3,
            max_output_tokens=2048,
            temperature=0.0,
            thinking_budget=0,
            thinking_level="none",
        )

        with self.assertRaises(model_routing.ModelRoutingError):
            model_routing.generation_config_payload(profile)

    def test_telemetry_payload_excludes_raw_text(self):
        profile = model_routing.resolve_surface_model_profile(_Settings, model_routing.ModelSurface.CHAT_QUALITY_REVIEW)
        payload = model_routing.telemetry_payload(profile, candidate_mode="baseline")

        self.assertEqual(payload["model_id"], "gemini-2.5-flash")
        self.assertEqual(payload["model_surface"], "chat_quality_review")
        self.assertNotIn("prompt", payload)
        self.assertNotIn("answer", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
