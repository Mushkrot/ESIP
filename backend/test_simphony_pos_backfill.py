"""Simphony-only POS Type backfill candidate tests.

Run from project root:
scripts/backend_python.sh backend/test_simphony_pos_backfill.py
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from app.services import simphony_pos_backfill as backfill


def _registry(
    store_id: str,
    pos_system_name: str,
    *,
    pos_type: str = "Store POS",
) -> backfill.RegistryPosRecord:
    return backfill.RegistryPosRecord(
        source_location_id=store_id,
        pos_system_name=pos_system_name,
        pos_type=pos_type,
    )


def _profile(
    org_id: int,
    store_id: str,
    pos_type: str,
    *,
    org_name: str | None = None,
    ticket_count: int = 0,
    open_ticket_count: int = 0,
) -> backfill.ZendeskProfileRecord:
    return backfill.ZendeskProfileRecord(
        org_id=org_id,
        org_name=org_name or f"Metro Burger {store_id}",
        store_id=store_id,
        brand="Metro Burger",
        pos_type=pos_type,
        ticket_count=ticket_count,
        open_ticket_count=open_ticket_count,
    )


class SimphonyPosBackfillTest(unittest.TestCase):
    def test_simphony_family_values_map_to_canonical_zendesk_value(self):
        self.assertEqual(backfill.proposed_pos_type_for_registry_value("Simphony"), "Simphony")
        self.assertEqual(backfill.proposed_pos_type_for_registry_value(" Simphony 2 "), "Simphony")
        self.assertEqual(backfill.proposed_pos_type_for_registry_value("sImPhOnY"), "Simphony")

    def test_non_simphony_registry_values_are_excluded(self):
        excluded_values = [
            "Micros",
            "Maitre'D",
            "PixelPoint",
            "System 3",
            "Part of Cross Concept",
            "",
            "Unknown POS",
        ]

        for value in excluded_values:
            self.assertIsNone(backfill.proposed_pos_type_for_registry_value(value), value)

    def test_build_candidates_separates_missing_mismatch_already_and_unmatched(self):
        result = backfill.build_simphony_pos_candidates(
            registry_records=[
                _registry("2001", "Simphony"),
                _registry("2002", "Simphony 2"),
                _registry("2003", "Simphony"),
                _registry("2004", "Simphony 2"),
                _registry("2005", "Micros"),
            ],
            profile_records=[
                _profile(99000000001, "2001", "", ticket_count=7, open_ticket_count=1),
                _profile(99000000002, "2002", "Micros 5.5", ticket_count=4),
                _profile(99000000003, "2003", "Simphony"),
                _profile(99000000005, "2005", ""),
            ],
            zendesk_base_url="https://support.example.com/api/v2",
        )

        self.assertEqual(result.reason_counts, {"missing_pos_type": 1, "pos_type_mismatch": 1})
        self.assertEqual(result.already_canonical_profiles, 1)
        self.assertEqual([item.store_id for item in result.unmatched_registry_records], ["2004"])
        self.assertEqual([item.store_id for item in result.excluded_registry_records], ["2005"])

        missing, mismatch = result.candidates
        self.assertEqual(missing.store_id, "2001")
        self.assertEqual(missing.reason, "missing_pos_type")
        self.assertEqual(missing.proposed_pos_type, "Simphony")
        self.assertEqual(missing.ticket_count, 7)
        self.assertEqual(missing.open_ticket_count, 1)
        self.assertEqual(missing.org_url, "https://support.example.com/agent/organizations/99000000001")

        self.assertEqual(mismatch.store_id, "2002")
        self.assertEqual(mismatch.reason, "pos_type_mismatch")
        self.assertEqual(mismatch.current_pos_type, "Micros 5.5")
        self.assertEqual(mismatch.registry_pos_system_name, "Simphony 2")

    def test_duplicate_registry_store_prefers_simphony_2_evidence(self):
        result = backfill.build_simphony_pos_candidates(
            registry_records=[
                _registry("2254", "Simphony"),
                _registry("2254", "Simphony 2"),
            ],
            profile_records=[_profile(2254, "2254", "Micros 5.5")],
            zendesk_base_url="https://support.example.com",
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].registry_pos_system_name, "Simphony 2")

    def test_row_parsers_normalize_sheet_and_profile_rows(self):
        registry = backfill.registry_record_from_row(
            {
                "SourceLocationID": " 2002.0 ",
                "POSSystemName": " Simphony 2 ",
                "POSType": " POS ",
            },
            row_number=42,
        )
        profile = backfill.profile_record_from_row(
            {
                "org_id": "99000000002",
                "org_name": " Metro Burger 2002 ",
                "store_id": "Store 2002",
                "brand": " Metro Burger ",
                "pos_type": "",
                "ticket_count": "12",
                "open_ticket_count": "3.0",
                "simphony_store": "true",
            }
        )

        self.assertEqual(registry.store_id, "2002")
        self.assertEqual(registry.pos_system_name, "Simphony 2")
        self.assertEqual(registry.source_row_number, 42)
        self.assertEqual(profile.org_id, 99000000002)
        self.assertEqual(profile.store_id, "2002")
        self.assertEqual(profile.ticket_count, 12)
        self.assertEqual(profile.open_ticket_count, 3)
        self.assertTrue(profile.simphony_store)

    def test_candidate_export_has_stable_review_columns(self):
        result = backfill.build_simphony_pos_candidates(
            registry_records=[_registry("2005", "Simphony 2")],
            profile_records=[_profile(99000000002, "2005", "Micros 5.5", ticket_count=10)],
            zendesk_base_url="https://support.example.com",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidates.csv"
            written = backfill.write_candidate_csv(path, result.candidates)
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

        self.assertEqual(written, 1)
        self.assertEqual(list(rows[0].keys()), backfill.CANDIDATE_EXPORT_FIELDS)
        self.assertEqual(rows[0]["org_id"], "99000000002")
        self.assertEqual(rows[0]["org_url"], "https://support.example.com/agent/organizations/99000000002")
        self.assertEqual(rows[0]["current_pos_type"], "Micros 5.5")
        self.assertEqual(rows[0]["proposed_pos_type"], "Simphony")
        self.assertEqual(rows[0]["reason"], "pos_type_mismatch")


if __name__ == "__main__":
    unittest.main()
