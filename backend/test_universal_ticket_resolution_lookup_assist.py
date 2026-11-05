"""Universal Chat integration tests for ticket resolution lookup assist.

Run from project root:
scripts/backend_python.sh backend/test_universal_ticket_resolution_lookup_assist.py
"""

from __future__ import annotations

import unittest

from app.routers import universal
from app.services import ai_dispatcher_runtime


class UniversalTicketResolutionLookupAssistTest(unittest.TestCase):
    def test_disabled_config_does_not_make_resolution_payload_answer_affecting(self):
        payload = universal._build_ticket_resolution_lookup_assist_payload(
            latest_question="How to resolved this?",
            routing_question="How to resolved this?",
            semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
            explicit_ticket_id=None,
            target_ticket_id=950050,
            open_ticket_id=950050,
        )
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig()

        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["answer_affecting"])
        self.assertFalse(ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(disabled))

    def test_enabled_contract_builds_documented_resolution_answer(self):
        payload = universal._build_ticket_resolution_lookup_assist_payload(
            latest_question="How to resolved this?",
            routing_question="How to resolved this?",
            semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
            explicit_ticket_id=None,
            target_ticket_id=950050,
            open_ticket_id=950050,
        )
        enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=ai_dispatcher_runtime.ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
            assistive_task_classes=("ticket_resolution_lookup",),
        )
        answer = universal._build_ticket_resolution_lookup_answer(
            assist_payload=payload,
            lookup_result={
                "requested_ids": [950050],
                "found_ids": [950050],
                "missing_ids": [],
                "rows": [
                    {
                        "ticket_id": 950050,
                        "subject": "Internet down",
                        "status": "solved",
                        "store_id": "2012",
                        "issue_category": "Network",
                        "solved_at": "2026-07-01",
                        "resolution": "Telecom Partner replaced the modem and verified connectivity.",
                        "full_text": "private raw text should not render",
                    }
                ],
                "cap": 75,
                "truncated": False,
            },
            answer_language="English",
        )

        self.assertTrue(ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(enabled))
        self.assertIsNotNone(answer)
        self.assertIn("Documented resolution: Telecom Partner replaced the modem", answer or "")
        self.assertIn("documented_resolution=true", answer or "")
        self.assertNotIn("private raw text", answer or "")

    def test_open_ticket_without_documented_resolution_is_honest(self):
        payload = universal._build_ticket_resolution_lookup_assist_payload(
            latest_question="What fixed this?",
            routing_question="What fixed this?",
            semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
            explicit_ticket_id=None,
            target_ticket_id=950050,
            open_ticket_id=950050,
        )
        answer = universal._build_ticket_resolution_lookup_answer(
            assist_payload=payload,
            lookup_result={
                "requested_ids": [950050],
                "found_ids": [950050],
                "missing_ids": [],
                "rows": [
                    {
                        "ticket_id": 950050,
                        "subject": "Internet down",
                        "status": "open",
                        "store_id": "2012",
                        "resolution": "resolved",
                    }
                ],
                "cap": 75,
                "truncated": False,
            },
            answer_language="English",
        )

        self.assertIn("did not find a useful documented resolution", answer or "")
        self.assertIn("documented_resolution=false", answer or "")

    def test_incompatible_generic_and_bulk_requests_do_not_build_resolution_answer(self):
        generic = universal._build_ticket_resolution_lookup_assist_payload(
            latest_question="How do I fix a pinpad?",
            routing_question="How do I fix a pinpad?",
            semantic_frame={"intent": "procedure_lookup"},
            explicit_ticket_id=None,
            target_ticket_id=None,
            open_ticket_id=None,
        )
        bulk = universal._build_ticket_resolution_lookup_assist_payload(
            latest_question="How were tickets 950011 and 950053 resolved?",
            routing_question="How were tickets 950011 and 950053 resolved?",
            semantic_frame={"intent": "ticket_details"},
            explicit_ticket_id=None,
            target_ticket_id=None,
            open_ticket_id=None,
        )

        self.assertFalse(generic["accepted"])
        self.assertIn("missing_target_ticket", generic["reason_codes"])
        self.assertFalse(bulk["accepted"])
        self.assertIn("bulk_ticket_lookup_intent", bulk["reason_codes"])
        self.assertIsNone(
            universal._build_ticket_resolution_lookup_answer(
                assist_payload=generic,
                lookup_result={"requested_ids": [], "found_ids": [], "missing_ids": [], "rows": []},
                answer_language="English",
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
