"""AI dispatcher result-set memory tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_result_sets.py
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services import ai_dispatcher_result_sets


class _FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store: dict, key: str):
        self.store = store
        self.key = key

    def get(self):
        return _FakeSnapshot(self.store.get(self.key))

    def set(self, data: dict, merge: bool = False):
        if merge and isinstance(self.store.get(self.key), dict):
            self.store[self.key] = {**self.store[self.key], **data}
        else:
            self.store[self.key] = dict(data)


class _FakeCollection:
    def __init__(self, store: dict):
        self.store = store

    def document(self, key: str):
        return _FakeDocRef(self.store, key)


class _FakeDb:
    def __init__(self):
        self.collections: dict[str, dict] = {}

    def collection(self, name: str):
        return _FakeCollection(self.collections.setdefault(name, {}))


class DispatcherResultSetTest(unittest.TestCase):
    def test_payload_is_bounded_redacted_and_structured_only(self):
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        rows = [
            {
                "ticket_id": 950053,
                "subject": "Internet issue contact priya.shah@northstar.example.com",
                "store_id": "2002",
                "evidence_snippet": "Call 202-555-0100 about interrupted internet.",
            }
        ]
        payload = ai_dispatcher_result_sets.build_result_set_payload(
            conversation_id="conv-1",
            turn_id="turn-1",
            trace_id="trace-1",
            source_family="tickets",
            requested_store_ids=[str(7000 + i) for i in range(40)],
            criteria=[f"criterion-{i}" for i in range(20)],
            ticket_rows=rows,
            ticket_ids=list(range(950001, 950151)),
            displayed_ticket_ids=list(range(950001, 950051)),
            freshness={"start_date": "2025-07-01", "end_date": "2026-07-01"},
            now=now,
        )

        rendered = repr(payload)
        self.assertIn("[EMAIL]", rendered)
        self.assertIn("[PHONE]", rendered)
        self.assertNotIn("priya.shah@northstar.example.com", rendered)
        self.assertNotIn("202-555-0100", rendered)
        self.assertNotIn("messages", payload)
        self.assertNotIn("final_answer", payload)
        self.assertEqual(len(payload["ticket_ids"]), 100)
        self.assertEqual(len(payload["displayed_ticket_ids"]), 30)
        self.assertEqual(len(payload["requested_store_ids"]), 30)
        self.assertEqual(len(payload["criteria"]), 12)
        self.assertEqual(payload["expires_at"], "2026-07-08T00:00:00+00:00")

    def test_select_latest_ignores_expired_and_empty_sets(self):
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        document = {
            "sets": [
                {
                    "memory_id": "expired",
                    "created_at": "2026-06-29T00:00:00+00:00",
                    "expires_at": "2026-06-30T00:00:00+00:00",
                    "ticket_ids": [1],
                },
                {
                    "memory_id": "empty",
                    "created_at": "2026-07-01T01:00:00+00:00",
                    "expires_at": "2026-07-08T00:00:00+00:00",
                    "ticket_ids": [],
                },
                {
                    "memory_id": "latest",
                    "created_at": "2026-07-01T00:00:00+00:00",
                    "expires_at": "2026-07-08T00:00:00+00:00",
                    "ticket_ids": [950053],
                },
            ]
        }

        selected = ai_dispatcher_result_sets.select_latest_result_set(document, now=now)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["memory_id"], "latest")

    def test_save_and_load_are_bounded_by_conversation_document(self):
        fake_db = _FakeDb()
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        payload = ai_dispatcher_result_sets.build_result_set_payload(
            conversation_id="conv-1",
            turn_id="turn-1",
            trace_id="trace-1",
            source_family="tickets",
            ticket_rows=[{"ticket_id": 950053, "subject": "Internet"}],
            now=now,
        )

        with patch("app.services.ai_dispatcher_result_sets.firestore.get_db", return_value=fake_db):
            save_status = ai_dispatcher_result_sets.save_result_set(payload)
            loaded = ai_dispatcher_result_sets.load_latest_result_set("conv-1", now=now)
            missing = ai_dispatcher_result_sets.load_latest_result_set("", now=now)

        self.assertEqual(save_status["status"], "saved")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["ticket_ids"], [950053])
        self.assertIsNone(missing)

    def test_detect_prior_result_comparison_requires_bulk_ids_and_prior_reference(self):
        positive = ai_dispatcher_result_sets.detect_result_set_comparison_request(
            "Compare 950053 950027 with your previous list"
        )
        routine = ai_dispatcher_result_sets.detect_result_set_comparison_request(
            "Can you explain ticket 950012?"
        )

        self.assertTrue(positive.is_comparison)
        self.assertEqual(positive.ticket_ids, (950053, 950027))
        self.assertIn("prior_result_reference", positive.reason_codes)
        self.assertFalse(routine.is_comparison)

    def test_detect_bulk_ticket_lookup_requires_prior_context(self):
        positive = ai_dispatcher_result_sets.detect_bulk_ticket_lookup_request(
            "950053 950027",
            previous_assistant_text="Earlier result discussed a ticket list.",
            semantic_frame={"target_context": "prior_turn"},
        )
        no_history = ai_dispatcher_result_sets.detect_bulk_ticket_lookup_request("950053 950027")
        comparison = ai_dispatcher_result_sets.detect_bulk_ticket_lookup_request(
            "Compare 950053 950027 with your previous list",
            previous_assistant_text="Earlier result discussed a ticket list.",
        )

        self.assertTrue(positive.is_lookup)
        self.assertEqual(positive.ticket_ids, (950053, 950027))
        self.assertIn("bare_ticket_ids", positive.reason_codes)
        self.assertFalse(no_history.is_lookup)
        self.assertFalse(comparison.is_lookup)

    def test_detect_fact_check_prior_answer_requires_challenge_and_ticket_ids(self):
        positive = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
            "Where did you find that claim? Verify 950053 and 950027.",
            previous_assistant_text="Earlier answer claimed the tickets supported the date.",
            semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        )
        no_ids = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
            "Where did you find that claim?",
            previous_assistant_text="Earlier answer claimed something.",
        )
        routine = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
            "Please summarize ticket 950012."
        )

        self.assertTrue(positive.is_fact_check)
        self.assertEqual(positive.ticket_ids, (950053, 950027))
        self.assertIn("explicit_ticket_ids", positive.reason_codes)
        self.assertIn("semantic_fact_check", positive.reason_codes)
        self.assertFalse(no_ids.is_fact_check)
        self.assertFalse(routine.is_fact_check)

    def test_compare_and_answer_include_required_sets_and_limits(self):
        previous = {
            "memory_id": "mem-1",
            "trace_id": "trace-1",
            "turn_id": "turn-1",
            "criteria": ["internet", "speed"],
            "requested_store_ids": ["2012", "2002"],
            "ticket_ids": [950053, 950027],
        }
        lookup = {
            "found_ids": [950053, 950030],
            "missing_ids": [950058],
            "rows": [
                {"ticket_id": 950053, "store_id": "2002", "subject": "Internet interrupted"},
                {"ticket_id": 950030, "store_id": "2011", "subject": "Slow network"},
            ],
            "cap": 75,
        }

        comparison = ai_dispatcher_result_sets.compare_ticket_sets(previous, [950053, 950030, 950058], lookup)
        answer = ai_dispatcher_result_sets.build_comparison_answer(comparison, answer_language="English")

        self.assertEqual(comparison["common_ids"], [950053])
        self.assertEqual(comparison["previous_only_ids"], [950027])
        self.assertEqual(comparison["user_only_ids"], [950030, 950058])
        self.assertEqual(comparison["missing_lookup_ids"], [950058])
        self.assertIn("#950053", answer)
        self.assertIn("#950027", answer)
        self.assertIn("#950030", answer)
        self.assertIn("lookup_missing=1", answer)

    def test_bulk_lookup_answer_includes_partial_limits_without_raw_snippet(self):
        lookup = {
            "requested_ids": [950053, 950027, 950058],
            "found_ids": [950053, 950027],
            "missing_ids": [950058],
            "rows": [
                {
                    "ticket_id": 950053,
                    "store_id": "2002",
                    "status": "solved",
                    "created_at": "2026-06-01",
                    "issue_category": "Network",
                    "subject": "Internet interrupted",
                    "evidence_snippet": "private raw details should not be rendered",
                },
            ],
            "cap": 75,
            "truncated": False,
        }

        answer = ai_dispatcher_result_sets.build_bulk_lookup_answer(lookup)
        summary = ai_dispatcher_result_sets.summarize_bulk_lookup(lookup)

        self.assertIn("Requested: #950053, #950027, #950058", answer)
        self.assertIn("Found: #950053, #950027", answer)
        self.assertIn("Missing from bounded lookup: #950058", answer)
        self.assertIn("cap=75", answer)
        self.assertNotIn("private raw details", answer)
        self.assertEqual(summary["requested_count"], 3)
        self.assertEqual(summary["found_count"], 2)
        self.assertEqual(summary["missing_count"], 1)

    def test_fact_check_answer_uses_lookup_evidence_not_prior_assistant_text(self):
        lookup = {
            "requested_ids": [950053, 950027, 950058],
            "found_ids": [950053, 950027],
            "missing_ids": [950058],
            "rows": [
                {
                    "ticket_id": 950053,
                    "store_id": "2002",
                    "status": "solved",
                    "created_at": "2026-06-01",
                    "issue_category": "Network",
                    "subject": "Internet interrupted",
                    "evidence_snippet": "private raw details should not be rendered",
                },
            ],
            "cap": 75,
            "truncated": False,
        }

        answer = ai_dispatcher_result_sets.build_fact_check_answer(lookup)

        self.assertIn("bounded ticket-ID lookup", answer)
        self.assertIn("Checked ticket IDs: #950053, #950027, #950058", answer)
        self.assertIn("Ticket records found: #950053, #950027", answer)
        self.assertIn("Missing from bounded lookup: #950058", answer)
        self.assertIn("previous_assistant_text_used_as_evidence=false", answer)
        self.assertIn("cap=75", answer)
        self.assertNotIn("private raw details", answer)


if __name__ == "__main__":
    unittest.main()
