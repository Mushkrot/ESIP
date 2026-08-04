"""Offline contract tests for the Phase 1 official guide vault."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "model_transition_phase1_guide_vault.py"
SPEC = importlib.util.spec_from_file_location("phase1_guide_vault", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GuideVaultContractTest(unittest.TestCase):
    def test_source_registry_has_the_required_coverage(self) -> None:
        categories = {source["category"] for source in MODULE.SOURCES}
        self.assertTrue({"model_card", "family_guide", "sdk", "thinking", "tools", "pricing"} <= categories)
        ids = {source["id"] for source in MODULE.SOURCES}
        self.assertTrue(
            {
                "model-card-gemini-2-5-flash",
                "model-card-gemini-3-5-flash-lite",
                "model-card-gemini-3-5-flash",
                "model-card-gemini-3-6-flash",
            }
            <= ids
        )
        self.assertEqual(len(ids), len(MODULE.SOURCES))

    def test_manifest_verification_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            vault = Path(temporary_directory)
            entries = []
            for source in MODULE.SOURCES:
                payload = source["id"].encode("utf-8")
                (vault / source["filename"]).write_bytes(payload)
                entries.append(
                    {
                        **source,
                        "source_url": source["url"],
                        "retrieved_at": "2026-07-23T00:00:00+00:00",
                        "sha256": MODULE.sha256_bytes(payload),
                    }
                )
            (vault / MODULE.MANIFEST_NAME).write_text(json.dumps({"entries": entries}), encoding="utf-8")
            self.assertEqual(MODULE.validate_manifest(vault), [])
            (vault / entries[0]["filename"]).write_text("tampered", encoding="utf-8")
            self.assertIn("sha256 mismatch", MODULE.validate_manifest(vault)[0])


if __name__ == "__main__":
    unittest.main()
