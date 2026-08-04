"""AI dispatcher policy compiler tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_policy_compiler.py
"""

from __future__ import annotations

import unittest

from app.services.ai_dispatcher_planner import (
    DispatcherDeterministicEntities,
    DispatcherMode,
    DispatcherPolicyLimits,
    compile_dispatcher_payload,
    compile_dispatcher_plan,
    parse_dispatcher_plan_or_raise,
)


def _payload() -> dict:
    return {
        "task_type": "ticket_set_compare",
        "answer_language": "user",
        "confidence": 0.9,
        "requires_dispatcher": True,
        "scope": {
            "store_ids": ["2011"],
            "ticket_ids": [950053],
            "source_family": "tickets",
        },
        "prior_result_refs": [
            {
                "turn_id": "turn-previous",
                "use_full_result_set": True,
                "reason": "Compare with prior result set.",
            }
        ],
        "actions": [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053]},
                "max_rows": 25,
                "required": True,
            },
            {
                "name": "compare_ticket_sets",
                "inputs": {"criteria": ["internet", "speed"]},
                "max_rows": 25,
                "required": True,
            },
        ],
        "comparison_contract": {
            "left_label": "ESIP previous result set",
            "right_label": "User supplied IDs",
            "criteria": ["internet", "speed"],
            "must_report": ["common_ids", "left_only", "right_only"],
        },
        "answer_contract": {
            "must_include": ["checked_scope", "criteria"],
            "must_not_claim": ["cannot_access_ticket_details_without_attempting_lookup"],
        },
        "risk_flags": ["bulk_id_followup"],
        "cost_plan": {
            "max_planner_calls": 1,
            "max_tool_actions": 4,
            "max_ticket_ids": 75,
            "max_rows": 100,
        },
        "fallback": "legacy_universal_chat",
    }


class DispatcherPolicyCompilerTest(unittest.TestCase):
    def test_valid_prior_result_comparison_compiles_in_shadow_without_execution(self):
        plan = parse_dispatcher_plan_or_raise(_payload())

        decision = compile_dispatcher_plan(plan, mode=DispatcherMode.SHADOW)

        self.assertTrue(decision.accepted)
        self.assertFalse(decision.execute_actions)
        self.assertEqual(decision.mode, DispatcherMode.SHADOW)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.effective_scope.store_ids, ("2011",))
        self.assertEqual(decision.effective_scope.ticket_ids, (950053,))
        self.assertEqual([action.name.value for action in decision.actions], ["bulk_ticket_id_lookup", "compare_ticket_sets"])
        self.assertEqual(decision.actions[0].inputs["ticket_ids"], [950053])
        self.assertEqual(decision.to_dict()["fallback"], "legacy_universal_chat")

    def test_fake_action_is_rejected_before_compile_accepts_execution(self):
        payload = _payload()
        payload["actions"][0]["name"] = "run_arbitrary_tool"

        decision = compile_dispatcher_payload(payload, mode=DispatcherMode.ASSISTIVE)

        self.assertFalse(decision.accepted)
        self.assertFalse(decision.execute_actions)
        self.assertEqual(decision.reason_codes, ("planner_validation_failed",))
        self.assertIn("actions[0].name", {issue.field for issue in decision.issues})
        self.assertEqual(decision.actions, ())

    def test_ticket_ids_and_rows_are_trimmed_with_cap_details(self):
        payload = _payload()
        payload["scope"]["ticket_ids"] = [950053, 950054, 950055]
        payload["actions"][0]["inputs"]["ticket_ids"] = [950053, 950054, 950055]
        payload["actions"][0]["max_rows"] = 50
        limits = DispatcherPolicyLimits(max_explicit_ticket_ids=2, max_action_rows=10)

        decision = compile_dispatcher_payload(payload, mode=DispatcherMode.ASSISTIVE, limits=limits)

        self.assertTrue(decision.accepted)
        self.assertTrue(decision.execute_actions)
        self.assertEqual(decision.effective_scope.ticket_ids, (950053, 950054))
        self.assertEqual(decision.actions[0].inputs["ticket_ids"], [950053, 950054])
        self.assertEqual(decision.actions[0].max_rows, 10)
        self.assertEqual(
            {detail.code for detail in decision.cap_details},
            {"ticket_ids_trimmed", "action_rows_trimmed", "cost_ticket_cap_clamped", "cost_rows_cap_clamped"},
        )
        self.assertIn("ticket_ids_trimmed", decision.reason_codes)
        self.assertIn("action_rows_trimmed", decision.reason_codes)

    def test_cost_plan_tool_action_overage_is_clamped_but_actual_action_overage_rejects(self):
        payload = _payload()
        payload["cost_plan"]["max_tool_actions"] = 8

        clamped = compile_dispatcher_payload(payload, mode=DispatcherMode.ASSISTIVE)

        self.assertTrue(clamped.accepted)
        self.assertTrue(clamped.execute_actions)
        self.assertIn("cost_tool_actions_cap_clamped", clamped.reason_codes)
        self.assertIn("cost_tool_actions_cap_clamped", {detail.code for detail in clamped.cap_details})

        payload["actions"] = payload["actions"] + [
            {"name": "load_prior_result_set", "inputs": {}, "required": True},
            {"name": "criterion_score_ticket_set", "inputs": {"ticket_ids": [950053]}, "required": True},
            {"name": "search_knowledge_base", "inputs": {"query": "internet speed"}, "required": False},
        ]
        rejected = compile_dispatcher_payload(payload, mode=DispatcherMode.ASSISTIVE)

        self.assertFalse(rejected.accepted)
        self.assertFalse(rejected.execute_actions)
        self.assertIn("too_many_actions", rejected.reason_codes)

    def test_too_many_actions_and_planner_calls_are_rejected(self):
        payload = _payload()
        payload["cost_plan"]["max_planner_calls"] = 2
        payload["actions"] = payload["actions"] + [
            {"name": "load_prior_result_set", "inputs": {}, "required": True},
            {"name": "criterion_score_ticket_set", "inputs": {"ticket_ids": [950053]}, "required": True},
            {"name": "search_knowledge_base", "inputs": {"query": "internet speed"}, "required": False},
        ]

        decision = compile_dispatcher_payload(payload)

        self.assertFalse(decision.accepted)
        self.assertIn("too_many_planner_calls", decision.reason_codes)
        self.assertIn("too_many_actions", decision.reason_codes)
        self.assertEqual(decision.actions, ())

    def test_missing_required_scope_is_rejected_for_bulk_lookup(self):
        payload = _payload()
        payload["task_type"] = "bulk_ticket_lookup"
        payload["scope"]["ticket_ids"] = []
        payload["actions"] = [{"name": "bulk_ticket_id_lookup", "inputs": {}, "required": True}]
        payload["prior_result_refs"] = []

        decision = compile_dispatcher_payload(payload)

        self.assertFalse(decision.accepted)
        self.assertIn("missing_required_scope", decision.reason_codes)
        self.assertIn("scope", {issue.field for issue in decision.issues})

    def test_deterministic_entities_augment_planner_scope_and_action_inputs(self):
        payload = _payload()
        plan = parse_dispatcher_plan_or_raise(payload)
        deterministic = DispatcherDeterministicEntities(
            store_ids=("2002",),
            ticket_ids=(999001,),
            reason="explicit user entities",
        )

        decision = compile_dispatcher_plan(plan, mode=DispatcherMode.PRIMARY_FOR_ELIGIBLE, deterministic_entities=deterministic)

        self.assertTrue(decision.accepted)
        self.assertTrue(decision.execute_actions)
        self.assertEqual(decision.effective_scope.store_ids, ("2002", "2011"))
        self.assertEqual(decision.effective_scope.ticket_ids, (999001, 950053))
        self.assertEqual(decision.actions[0].inputs["ticket_ids"], [999001, 950053])

    def test_disabled_mode_returns_legacy_fallback_without_compile(self):
        plan = parse_dispatcher_plan_or_raise(_payload())

        decision = compile_dispatcher_plan(plan, mode=DispatcherMode.DISABLED)

        self.assertFalse(decision.accepted)
        self.assertFalse(decision.execute_actions)
        self.assertEqual(decision.reason_codes, ("dispatcher_disabled",))
        self.assertEqual(decision.actions, ())
        self.assertEqual(decision.fallback.value, "legacy_universal_chat")


if __name__ == "__main__":
    unittest.main()
