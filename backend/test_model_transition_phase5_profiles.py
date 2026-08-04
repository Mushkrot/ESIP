import inspect
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from scripts import model_transition_phase5_profiles as profiles


class Phase5ProfileTests(unittest.TestCase):
    def test_catalog_is_deterministic_frozen_and_unique(self):
        first = profiles.catalog_manifest()
        second = profiles.catalog_manifest()

        self.assertEqual(first, second)
        self.assertEqual(profiles.catalog_hash(), profiles.catalog_hash())
        ids = [entry["profile_id"] for entry in first]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":")),
            json.dumps(second, sort_keys=True, separators=(",", ":")),
        )
        with self.assertRaises(FrozenInstanceError):
            profiles.catalog()[0].model = "changed"

    def test_family_safe_thinking_and_output_controls(self):
        for profile in profiles.catalog():
            config = profiles.generation_config(profile)
            self.assertIn("max_output_tokens", config)
            self.assertGreater(config["max_output_tokens"], 0)
            if profile.family == profiles.GEMINI_2_5:
                self.assertIn("thinking_budget", config)
                self.assertNotIn("thinking_level", config)
            else:
                self.assertIn("thinking_level", config)
                self.assertIn(config["thinking_level"], profiles.GEMINI_3_THINKING_LEVELS)
                self.assertNotIn("thinking_budget", config)
                self.assertNotEqual(config["thinking_level"], "none")

    def test_gemini_3_sampling_fields_are_omitted_and_rejected(self):
        candidate = next(
            profile for profile in profiles.catalog() if profile.family == profiles.GEMINI_3
        )
        config = profiles.generation_config(candidate)
        self.assertFalse(profiles.GEMINI_3_FORBIDDEN_SAMPLING_FIELDS.intersection(config))

        with self.assertRaises(profiles.ProfileError):
            profiles.validate_profile(
                replace(candidate, sampling_config=(("temperature", 0.2),))
            )
        with self.assertRaises(profiles.ProfileError):
            profiles.validate_profile(
                replace(candidate, thinking_level="none")
            )

    def test_unknown_and_mixed_family_controls_fail_closed(self):
        candidate = next(
            profile for profile in profiles.catalog() if profile.family == profiles.GEMINI_3
        )
        with self.assertRaises(profiles.ProfileError):
            profiles.validate_profile(replace(candidate, model="unknown-model"))
        with self.assertRaises(profiles.ProfileError):
            profiles.validate_profile(replace(candidate, thinking_budget=0))

        baseline = next(
            profile for profile in profiles.catalog() if profile.family == profiles.GEMINI_2_5
        )
        with self.assertRaises(profiles.ProfileError):
            profiles.validate_profile(replace(baseline, thinking_level="LOW"))

    def test_transport_contract_requires_explicit_bounded_settings(self):
        profile = profiles.catalog()[0]
        invalid_profiles = (
            replace(profile, api_version="v1beta1"),
            replace(profile, location=""),
            replace(profile, timeout_seconds=0),
            replace(profile, max_attempts=2),
            replace(profile, retry_enabled=True),
            replace(profile, automatic_function_calling=True),
            replace(profile, built_in_tools=True),
            replace(profile, explicit_cache=True),
            replace(profile, safety_settings=()),
            replace(profile, max_output_tokens=0),
        )
        for invalid in invalid_profiles:
            with self.subTest(invalid=invalid):
                with self.assertRaises(profiles.ProfileError):
                    profiles.validate_profile(invalid)

    def test_prompt_variants_are_separate_contracts(self):
        legacy = profiles.prompt_contract_hash("legacy_fidelity")
        native = profiles.prompt_contract_hash("model_native")
        self.assertNotEqual(legacy, native)
        self.assertEqual(len(legacy), 64)
        self.assertEqual(len(native), 64)

    def test_evaluator_fields_cannot_enter_candidate_contents(self):
        profile = profiles.catalog()[1]
        with self.assertRaises(profiles.ProfileError):
            profiles.serialize_candidate_request(
                profile,
                {"question": "How does this work?", "reference_answer": "hidden"},
            )
        with self.assertRaises(profiles.ProfileError):
            profiles.serialize_candidate_request(
                profile,
                {"question": "How does this work?", "metadata": {"expected_route": "x"}},
            )

        request = profiles.serialize_candidate_request(
            profile,
            {
                "question": "Use the attached record.",
                "data": {"reference_answer": "raw source text, not an evaluator field"},
            },
        )
        self.assertEqual(
            request["contents"]["data"]["reference_answer"],
            "raw source text, not an evaluator field",
        )

    def test_serialized_request_is_explicit_and_transport_quarantined(self):
        profile = profiles.catalog()[1]
        request = profiles.serialize_candidate_request(
            profile,
            {"question": "Summarize this source.", "evidence": ["source text"]},
            response_schema={"type": "object"},
            input_tokens=100,
            output_tokens=100,
        )
        self.assertEqual(request["transport"]["api_version"], "v1")
        self.assertEqual(request["transport"]["location"], "us-central1")
        self.assertEqual(request["transport"]["max_attempts"], 1)
        self.assertEqual(request["transport"]["status"], "quarantined")
        self.assertEqual(request["generation_config"]["max_output_tokens"], profile.max_output_tokens)
        self.assertIn("safety_settings", request)
        self.assertEqual(request["response_schema"], {"type": "object"})

    def test_cost_cap_is_checked_before_transport(self):
        profile = profiles.catalog()[1]
        self.assertGreater(profiles.estimate_cost_usd(profile, 1000, 1000), 0)
        with self.assertRaises(profiles.ProfileError):
            profiles.serialize_candidate_request(
                profile,
                {"question": "large request"},
                input_tokens=10_000_000,
                output_tokens=10_000_000,
            )

    def test_lane_inventory_is_not_a_quality_ranking(self):
        manifest = profiles.catalog_manifest()
        by_lane = {entry["lane"]: entry for entry in manifest}
        self.assertEqual(by_lane["A"]["model"], "gemini-3.5-flash-lite")
        self.assertEqual(by_lane["B"]["model"], "gemini-3.6-flash")
        self.assertEqual(by_lane["C"]["model"], "gemini-3.6-flash")
        self.assertEqual(by_lane["D-control"]["model"], "gemini-2.5-flash")
        self.assertTrue(all("quality" not in entry for entry in manifest))
        self.assertTrue(all(entry["transport_status"] == "quarantined" for entry in manifest))

    def test_module_has_no_live_transport_imports(self):
        source = inspect.getsource(profiles)
        forbidden_imports = (
            "import google",
            "from google",
            "import requests",
            "import socket",
            "import subprocess",
            "import urllib",
        )
        for marker in forbidden_imports:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
