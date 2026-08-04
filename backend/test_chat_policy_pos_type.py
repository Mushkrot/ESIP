"""POS Type profile analytics routing tests.

Run from project root:
scripts/backend_python.sh backend/test_chat_policy_pos_type.py
"""

from __future__ import annotations

import unittest

from app.routers import universal
from app.services import chat_policy, pos_type_analytics


class ChatPolicyPosTypeTest(unittest.TestCase):
    def test_golden_prompt_routes_to_profile_analytics(self):
        question = (
            "Summarize all tickets for stores where POS Type = Simphony. "
            "Identify repeated problems and repeated stores."
        )

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "single")
        self.assertEqual(request.pos_type, "Simphony")

    def test_negative_text_search_does_not_become_profile_filter(self):
        question = "Search tickets that mention the word Simphony."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNone(request)

    def test_contextual_simphony_troubleshooting_is_not_profile_analytics(self):
        """CHAT-145: POS context must not override troubleshooting intent."""
        question = (
            "extra bacon modifier not appearing on kitchen KDS from POS, "
            "store is a Simphony store"
        )

        self.assertIsNone(chat_policy.extract_pos_type_profile_analytics_request(question))

    def test_explicit_pos_type_context_without_analytics_ask_is_not_profile_analytics(self):
        question = "The store uses POS Type Simphony and the kitchen KDS is not receiving modifiers."

        self.assertIsNone(chat_policy.extract_pos_type_profile_analytics_request(question))

    def test_simphony_stores_shorthand_routes_to_profile_filter(self):
        question = "Which Simphony stores have the most repeated issues in the last 90 days?"

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "single")
        self.assertEqual(request.pos_type, "Simphony")
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_explicit_pos_type_simphony_stores_does_not_capture_stores_as_value(self):
        question = "For POS Type Simphony stores, which stores have the most repeated recent issues?"

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "single")
        self.assertEqual(request.pos_type, "Simphony")
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_no_punctuation_before_natural_clause_still_extracts_pos_type(self):
        question = "For POS Type Simphony have the most repeated recent issues?"

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "single")
        self.assertEqual(request.pos_type, "Simphony")
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_compare_to_second_pos_type_without_second_label(self):
        question = "For POS Type Simphony, compare to POS Type Micros 5.5 over the last 7 days."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "comparison")
        self.assertEqual(request.pos_types, ("Simphony", "Micros 5.5"))
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_compare_to_pos_type_without_word_compare(self):
        question = "For POS Type Simphony to POS Type Micros 5.5 over the last 7 days."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "comparison")
        self.assertEqual(request.pos_types, ("Simphony", "Micros 5.5"))
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_pos_type_open_pending_question_extracts_status_filter(self):
        question = "Show open or pending tickets for stores where POS Type = Simphony."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "single")
        self.assertEqual(request.pos_type, "Simphony")
        self.assertEqual(request.status_filter, ("open", "pending"))

    def test_pos_type_not_closed_question_means_unresolved_status_filter(self):
        question = "Which Simphony stores have tickets that are not closed?"

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.pos_type, "Simphony")
        self.assertEqual(request.status_filter, ("new", "open", "pending", "hold"))

    def test_simphony_2_maps_to_canonical_zendesk_pos_type(self):
        question = "Summarize tickets for stores where POS Type = Simphony 2."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.pos_type, "Simphony")

    def test_simphony2_shorthand_routes_to_canonical_zendesk_pos_type(self):
        question = "Which Simphony2 stores have the most repeated issues?"

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.pos_type, "Simphony")

    def test_comparison_prompt_extracts_arbitrary_pos_types(self):
        question = "Compare top problems for POS Type Simphony vs POS Type Micros 5.5 over the last 90 days."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "comparison")
        self.assertEqual(request.pos_types, ("Simphony", "Micros 5.5"))
        self.assertIsNotNone(request.start_date)
        self.assertIsNotNone(request.end_date)

    def test_comparison_prompt_canonicalizes_simphony_2(self):
        question = "Compare top problems for POS Type Simphony 2 vs POS Type Micros 5.5."

        request = chat_policy.extract_pos_type_profile_analytics_request(question)

        self.assertIsNotNone(request)
        self.assertEqual(request.mode, "comparison")
        self.assertEqual(request.pos_types, ("Simphony", "Micros 5.5"))

    def test_generated_sql_uses_organization_profile_join(self):
        queries = pos_type_analytics.build_pos_type_analytics_queries("Simphony")
        sql = "\n".join(query for query, _ in queries.values())

        self.assertIn("zendesk_organization_profiles", sql)
        self.assertIn("store_profiles.pos_type", sql)
        self.assertIn("t.org_id = store_profiles.org_id", sql)
        self.assertNotIn("t.pos_type", sql)

    def test_deterministic_answer_includes_filter_coverage_stores_and_problems(self):
        request = chat_policy.PosTypeAnalyticsRequest(
            mode="single",
            pos_type="Simphony",
            start_date="2026-06-01",
            end_date="2026-06-15",
            status_filter=("open", "pending"),
        )
        result = {
            "available": True,
            "filters": {
                "pos_type": "Simphony",
                "date_window": {
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-15",
                    "timezone": "America/New_York",
                },
            },
            "coverage": {
                "orgs_matched": 3,
                "tickets_matched": 9,
                "org_id_join_ticket_count": 8,
                "store_id_fallback_ticket_count": 1,
                "profile_orgs_without_tickets_count": 0,
            },
            "repeated_stores": [
                {
                    "org_name": "Metro Burger 2002",
                    "store_id": "2002",
                    "ticket_count": 4,
                    "recent_ticket_count": 2,
                    "open_ticket_count": 1,
                }
            ],
            "repeated_problems": [
                {
                    "issue_category": "applications__pos",
                    "product_type": "pos",
                    "caused_by": "rootcause_hardware",
                    "ticket_count": 5,
                    "store_count": 2,
                    "open_ticket_count": 1,
                }
            ],
            "open_tickets": [],
        }

        answer = universal._pos_type_analytics_answer(result, request)

        self.assertIn("3** matching organizations", answer)
        self.assertIn("9** matching tickets", answer)
        self.assertIn("Ticket status filter: `open`, `pending`.", answer)
        self.assertIn("store_id_fallback", answer)
        self.assertIn("Metro Burger 2002 / Store 2002", answer)
        self.assertIn("Applications --> POS", answer)
        self.assertIn('SOURCES_JSON: {"sources": []}', answer)


if __name__ == "__main__":
    unittest.main()
