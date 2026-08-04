"""Regression tests for Universal Chat ticket-example routing policy."""

from __future__ import annotations

import unittest

from app.services import chat_policy, semantic_contract_v2, semantic_interpreter, temporal_resolver
from app.routers import universal


def _ticket_examples_frame() -> dict:
    return {
        "intent": "ticket_retrieval",
        "target_context": "global",
        "retrieval": {
            "output": "ticket_examples",
            "search_goal": (
                "Find examples of tickets opened by Jamie Rivera and escalated "
                "to Service Desk asking Service Desk to open a Telecom Partner ticket"
            ),
            "required_concepts": ["Jamie Rivera", "Service Desk", "Telecom Partner"],
        },
    }


class ChatPolicyTicketExampleRoutingTest(unittest.TestCase):
    def test_ticket_examples_do_not_force_drive_even_with_escalated_wording(self):
        question = (
            "Find some examples of tickets opened by Jonathan (Jon) Kastoris and "
            "escalated to Service Desk. In the ticket, Jon should have asked "
            "Service Desk to open a ticket with Telecom Partner regarding degradation of "
            "the connection line."
        )
        frame = _ticket_examples_frame()

        self.assertTrue(chat_policy.looks_like_drive_tabular_lookup(question))
        self.assertTrue(chat_policy.semantic_frame_requests_ticket_examples(frame))
        self.assertFalse(
            chat_policy.should_force_drive_tabular_lookup(question, semantic_frame=frame)
        )
        self.assertFalse(chat_policy.should_suppress_similar_tickets(question, semantic_frame=frame))

    def test_drive_escalation_lookup_still_forces_drive(self):
        question = "who do I escalate DispatchFlow issues to?"

        self.assertTrue(chat_policy.should_force_drive_tabular_lookup(question))
        self.assertTrue(chat_policy.should_suppress_similar_tickets(question))

    def test_row_provenance_still_forces_drive(self):
        question = "which row did you use for that answer?"

        self.assertTrue(chat_policy.should_force_drive_tabular_lookup(question))
        self.assertTrue(chat_policy.should_suppress_similar_tickets(question))

    def test_plain_ticket_list_is_not_ticket_examples(self):
        frame = {"intent": "general", "target_context": "global", "retrieval": {}}

        self.assertFalse(chat_policy.semantic_frame_requests_ticket_examples(frame))
        self.assertFalse(
            chat_policy.should_force_drive_tabular_lookup(
                "show tickets opened today",
                semantic_frame=frame,
            )
        )

    def test_multi_store_list_preserves_all_requested_store_ids(self):
        question = (
            "For every store 2001 2002 2003 2004 2005 2006 find all "
            "internet/speed/connection interruption related tickets for the last 365 days"
        )

        self.assertEqual(
            chat_policy.extract_store_ids(question),
            ["2001", "2002", "2003", "2004", "2005", "2006"],
        )
        self.assertEqual(chat_policy.extract_store_id(question), "2001")

    def test_location_store_id_does_not_collect_ticket_id(self):
        question = "Check also location 2002. I see ticket 950012. Did you mention it?"

        self.assertEqual(chat_policy.extract_store_ids(question), ["2002"])
        self.assertEqual(chat_policy.extract_store_id(question), "2002")

    def test_russian_restaurant_problem_question_extracts_store_scope(self):
        question = "Для ресторана 2008 есть ли у него проблемы с интернетом, сетью, подключением?"

        self.assertEqual(chat_policy.extract_store_ids(question), ["2008"])
        self.assertEqual(chat_policy.extract_store_id(question), "2008")
        self.assertTrue(chat_policy.question_requests_issue_breakdown(question))
        self.assertTrue(chat_policy.is_store_issue_breakdown_question(question))
        self.assertFalse(chat_policy.should_use_open_ticket_context(question))

    def test_multilingual_restaurant_problem_questions_extract_store_scope(self):
        cases = [
            "Para el restaurante 2008, hay problemas de internet, red o conexión?",
            "Pour le restaurant 2008, y a-t-il des problèmes d'internet, de réseau ou de connexion?",
            "Dlya restorana 2008 est problemy s internetom, setyu ili podklyucheniem?",
        ]

        for question in cases:
            with self.subTest(question=question):
                self.assertEqual(chat_policy.extract_store_ids(question), ["2008"])
                self.assertEqual(chat_policy.extract_store_id(question), "2008")
                self.assertTrue(chat_policy.question_requests_issue_breakdown(question))
                self.assertTrue(chat_policy.is_store_issue_breakdown_question(question))
                self.assertFalse(chat_policy.should_use_open_ticket_context(question))

    def test_multilingual_multi_store_lists_preserve_connectors(self):
        cases = [
            (
                "Para las tiendas 2012, 2011 y 2002, busca tickets de internet.",
                ["2012", "2011", "2002"],
            ),
            (
                "Pour les restaurants 2012, 2011 et 2002, trouve les tickets internet.",
                ["2012", "2011", "2002"],
            ),
        ]

        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(chat_policy.extract_store_ids(question), expected)
                self.assertEqual(chat_policy.extract_store_id(question), expected[0])

    def test_ticket_retrieval_cannot_enter_global_event_discovery(self):
        semantic = semantic_interpreter.SemanticInterpretation(
            normalized_question_en="Find tickets for server outages where technicians reimaged servers.",
            intent="ticket_retrieval",
            target_context="global",
            entities={"issue_or_category": "server outage"},
            retrieval={"output": "ticket_examples", "required_concepts": ["server outage", "reimage"]},
            confidence="high",
        )

        request = universal._looks_like_global_event_discovery(
            "how many tickets in the past month for server outage where a tech reimaged servers?",
            semantic.normalized_question_en,
            semantic,
            explicit_store_context=None,
            explicit_ticket_id=None,
        )

        self.assertIsNone(request)

    def test_explicit_global_restaurant_event_remains_available(self):
        semantic = semantic_interpreter.SemanticInterpretation(
            normalized_question_en="Which restaurant reported a fire incident?",
            intent="event_fact_check",
            target_context="global",
            entities={"issue_or_category": "restaurant fire"},
            confidence="high",
        )

        request = universal._looks_like_global_event_discovery(
            semantic.normalized_question_en,
            semantic.normalized_question_en,
            semantic,
            explicit_store_context=None,
            explicit_ticket_id=None,
        )

        self.assertIsNotNone(request)
        self.assertEqual(request["terms"], ["fire"])

    def test_ticket_contract_preserves_required_concepts_actions_and_past_month(self):
        period = temporal_resolver.resolve_period_text(
            "how many tickets in the past month for server outage where a technician reimaged servers?"
        )
        frame = {
            "intent": "ticket_retrieval",
            "target_context": "global",
            "confidence": "high",
            "entities": {"issue_or_category": "server outage"},
            "retrieval": {
                "output": "ticket_examples",
                "required_concepts": ["server outage"],
                "devices": ["server"],
                "actions": ["reimage"],
            },
        }

        contract = semantic_contract_v2.build_shadow_contract(
            latest_question="how many tickets in the past month for server outage where a technician reimaged servers?",
            routing_question="Find ticket examples for server outages where a technician reimaged servers.",
            semantic_frame=frame,
            temporal_resolution=period,
        )

        self.assertEqual(contract.executor, "ticket_semantic_search")
        self.assertEqual(contract.filters["required_concepts"], ["server outage"])
        self.assertEqual(contract.filters["devices"], ["server"])
        self.assertEqual(contract.filters["actions"], ["reimage"])
        self.assertEqual(contract.time_range["start_at"][:10], period.start_date.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
