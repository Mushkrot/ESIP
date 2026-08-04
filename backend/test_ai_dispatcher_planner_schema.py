"""AI dispatcher planner schema/parser tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_planner_schema.py
"""

from __future__ import annotations

import json
import unittest

from app.services.ai_dispatcher_planner import (
    DispatcherActionName,
    DispatcherFallback,
    DispatcherSourceFamily,
    DispatcherTaskType,
    DispatcherValidationError,
    parse_dispatcher_plan,
    parse_dispatcher_plan_or_raise,
)


def _valid_payload() -> dict:
    return {
        "task_type": "ticket_set_compare",
        "answer_language": "user",
        "confidence": 0.82,
        "requires_dispatcher": True,
        "scope": {
            "store_ids": ["2012", "2011", 2012],
            "ticket_ids": [950053, "950027", 950053],
            "period": {
                "start": "2025-07-01",
                "end": "2026-06-30",
                "timezone": "America/New_York",
            },
            "source_family": "tickets",
        },
        "prior_result_refs": [
            {
                "turn_id": "turn-previous",
                "use_full_result_set": True,
                "reason": "User asks to compare with the previous ticket list.",
            }
        ],
        "actions": [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {
                    "ticket_ids": [950053, 950027],
                    "fields": ["store_id", "subject", "created_at"],
                },
                "max_rows": 50,
                "required": True,
            }
        ],
        "comparison_contract": {
            "left_label": "ESIP previous result set",
            "right_label": "User supplied IDs",
            "criteria": ["internet", "speed", "connection interruption", "speed"],
            "must_report": ["common_ids", "left_only", "right_only"],
        },
        "answer_contract": {
            "must_include": ["checked_scope", "criteria", "evidence_limits"],
            "must_not_claim": ["cannot_access_ticket_details_without_attempting_lookup"],
        },
        "risk_flags": ["prior_result_truncation", "bulk_id_followup"],
        "cost_plan": {
            "max_planner_calls": 1,
            "max_tool_actions": 4,
            "max_ticket_ids": 75,
            "max_rows": 100,
        },
        "fallback": "legacy_universal_chat",
    }


class DispatcherPlannerSchemaTest(unittest.TestCase):
    def test_valid_dict_payload_parses_and_normalizes(self):
        result = parse_dispatcher_plan(_valid_payload())

        self.assertTrue(result.accepted)
        self.assertEqual(result.issues, ())
        self.assertIsNotNone(result.plan)
        assert result.plan is not None
        self.assertEqual(result.plan.task_type, DispatcherTaskType.TICKET_SET_COMPARE)
        self.assertEqual(result.plan.fallback, DispatcherFallback.LEGACY_UNIVERSAL_CHAT)
        self.assertEqual(result.plan.scope.source_family, DispatcherSourceFamily.TICKETS)
        self.assertEqual(result.plan.scope.store_ids, ("2012", "2011"))
        self.assertEqual(result.plan.scope.ticket_ids, (950053, 950027))
        self.assertEqual(result.plan.prior_result_refs[0].turn_id, "turn-previous")
        self.assertTrue(result.plan.prior_result_refs[0].use_full_result_set)
        self.assertEqual(result.plan.actions[0].name, DispatcherActionName.BULK_TICKET_ID_LOOKUP)
        self.assertEqual(result.plan.actions[0].max_rows, 50)
        self.assertEqual(result.plan.comparison_contract.criteria, ("internet", "speed", "connection interruption"))
        self.assertEqual(result.plan.answer_contract.must_include, ("checked_scope", "criteria", "evidence_limits"))
        self.assertEqual(result.plan.cost_plan.max_ticket_ids, 75)

        payload = result.plan.to_dict()
        self.assertEqual(payload["scope"]["store_ids"], ["2012", "2011"])
        self.assertEqual(payload["actions"][0]["name"], "bulk_ticket_id_lookup")
        self.assertEqual(payload["fallback"], "legacy_universal_chat")

    def test_json_string_payload_parses(self):
        payload = _valid_payload()

        result = parse_dispatcher_plan(json.dumps(payload))

        self.assertTrue(result.is_valid)
        self.assertEqual(result.plan.to_dict()["task_type"], "ticket_set_compare")

    def test_unknown_enum_and_action_values_are_rejected_with_redacted_issues(self):
        payload = _valid_payload()
        payload["task_type"] = "send_email_to_user"
        payload["scope"]["source_family"] = "secret_source"
        payload["actions"][0]["name"] = "run_arbitrary_tool"
        payload["fallback"] = "freeform_tool_call"

        result = parse_dispatcher_plan(payload)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.plan)
        issue_payload = result.to_dict()["issues"]
        self.assertEqual(
            {issue["field"] for issue in issue_payload},
            {"task_type", "scope.source_family", "actions[0].name", "fallback"},
        )
        rendered_issues = json.dumps(issue_payload)
        self.assertNotIn("send_email_to_user", rendered_issues)
        self.assertNotIn("secret_source", rendered_issues)
        self.assertNotIn("run_arbitrary_tool", rendered_issues)
        self.assertNotIn("freeform_tool_call", rendered_issues)

    def test_common_model_shape_drift_normalizes_without_widening_contract(self):
        payload = _valid_payload()
        payload["task_type"] = "compare_ticket_sets"
        payload["confidence"] = "high"
        payload["scope"] = "tickets"
        payload["actions"] = [
            {
                "tool_name": "lookup_ticket_ids",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 25,
                "required": True,
            },
            "compare_tickets",
        ]
        payload["comparison_contract"] = "n/a"
        payload["answer_contract"] = None
        payload["fallback"] = "universal_chat"

        result = parse_dispatcher_plan(payload)

        self.assertTrue(result.accepted)
        assert result.plan is not None
        self.assertEqual(result.plan.task_type, DispatcherTaskType.TICKET_SET_COMPARE)
        self.assertEqual(result.plan.confidence, 0.8)
        self.assertEqual(result.plan.scope.source_family, DispatcherSourceFamily.TICKETS)
        self.assertEqual([action.name for action in result.plan.actions], [
            DispatcherActionName.BULK_TICKET_ID_LOOKUP,
            DispatcherActionName.COMPARE_TICKET_SETS,
        ])
        self.assertEqual(result.plan.comparison_contract.criteria, ())
        self.assertEqual(result.plan.answer_contract.must_include, ())
        self.assertEqual(result.plan.fallback, DispatcherFallback.LEGACY_UNIVERSAL_CHAT)

    def test_invalid_confidence_and_malformed_fields_are_rejected(self):
        payload = _valid_payload()
        payload["confidence"] = 1.2
        payload["scope"]["store_ids"] = "2012"
        payload["scope"]["ticket_ids"] = ["950053", "not-a-ticket"]
        payload["prior_result_refs"] = [{"turn_id": "t1", "use_full_result_set": "yes"}]
        payload["actions"] = [{"name": "bulk_ticket_id_lookup", "inputs": [], "required": "true"}]
        payload["comparison_contract"]["criteria"] = "internet"
        payload["answer_contract"]["must_include"] = [None, "checked_scope"]
        payload["cost_plan"]["max_ticket_ids"] = 0

        result = parse_dispatcher_plan(payload)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.plan)
        codes_by_field = {issue.field: issue.code for issue in result.issues}
        self.assertEqual(codes_by_field["confidence"], "out_of_range")
        self.assertEqual(codes_by_field["scope.store_ids"], "invalid_type")
        self.assertEqual(codes_by_field["scope.ticket_ids[1]"], "invalid_type")
        self.assertEqual(codes_by_field["prior_result_refs[0].use_full_result_set"], "invalid_type")
        self.assertEqual(codes_by_field["actions[0].inputs"], "invalid_type")
        self.assertEqual(codes_by_field["actions[0].required"], "invalid_type")
        self.assertEqual(codes_by_field["comparison_contract.criteria"], "invalid_type")
        self.assertEqual(codes_by_field["answer_contract.must_include[0]"], "invalid_type")
        self.assertEqual(codes_by_field["cost_plan.max_ticket_ids"], "out_of_range")

    def test_invalid_json_and_non_object_payload_are_safe_failures(self):
        bad_json = parse_dispatcher_plan("{not json")
        list_payload = parse_dispatcher_plan([])

        self.assertFalse(bad_json.accepted)
        self.assertEqual(bad_json.issues[0].code, "invalid_json")
        self.assertFalse(list_payload.accepted)
        self.assertEqual(list_payload.issues[0].code, "invalid_type")

    def test_parse_or_raise_returns_plan_or_raises_without_raw_details(self):
        plan = parse_dispatcher_plan_or_raise(_valid_payload())
        self.assertEqual(plan.task_type, DispatcherTaskType.TICKET_SET_COMPARE)

        with self.assertRaisesRegex(DispatcherValidationError, "dispatcher plan validation failed"):
            parse_dispatcher_plan_or_raise({"task_type": "run_arbitrary_tool"})


if __name__ == "__main__":
    unittest.main()
