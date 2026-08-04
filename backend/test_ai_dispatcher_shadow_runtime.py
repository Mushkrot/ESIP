"""AI dispatcher shadow runtime tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_shadow_runtime.py
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services import ai_dispatcher_runtime
from app.services.ai_dispatcher_planner import DispatcherMode


def _shadow_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    data = {"mode": DispatcherMode.SHADOW, **overrides}
    return ai_dispatcher_runtime.DispatcherRuntimeConfig(**data)


def _assistive_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    data = {
        "mode": DispatcherMode.ASSISTIVE,
        "assistive_task_classes": ("prior_result_reference",),
        **overrides,
    }
    return ai_dispatcher_runtime.DispatcherRuntimeConfig(**data)


def _phase5b_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    return _assistive_config(assistive_task_classes=("prior_result_reference", "bulk_ticket_ids"), **overrides)


def _phase5c_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    return _assistive_config(
        assistive_task_classes=("prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"),
        **overrides,
    )


def _phase6a_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    return _assistive_config(
        assistive_task_classes=(
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
        ),
        **overrides,
    )


def _primary_config(**overrides) -> ai_dispatcher_runtime.DispatcherRuntimeConfig:
    data = {
        "mode": DispatcherMode.PRIMARY_FOR_ELIGIBLE,
        "assistive_task_classes": (
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
            "followup_date_scope",
        ),
        "eligible_task_classes": (
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
            "followup_date_scope",
        ),
        **overrides,
    }
    return ai_dispatcher_runtime.DispatcherRuntimeConfig(**data)


def _runtime_kwargs(**overrides) -> dict:
    data = {
        "latest_question": "Compare ticket 950012 and 950027 with your previous result.",
        "routing_question": "Compare ticket 950012 and 950027 with your previous result.",
        "messages": [{"role": "user", "content": "Compare ticket 950012 and 950027"}],
        "semantic_frame": {"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        "conversation_id": "conv-test",
        "turn_id": "turn-test",
        "trace_id": "trace-test",
        "owner_role": "super_admin",
        "explicit_store_ids": ["2012", "2011"],
        "explicit_ticket_id": None,
        "previous_assistant_text": "Previous tickets: 950053, 950027",
    }
    data.update(overrides)
    return data


def _planner_payload() -> dict:
    return {
        "task_type": "ticket_set_compare",
        "answer_language": "user",
        "confidence": 0.9,
        "requires_dispatcher": True,
        "scope": {
            "store_ids": ["2012", "2011"],
            "ticket_ids": [950053, 950027],
            "source_family": "tickets",
        },
        "prior_result_refs": [
            {
                "turn_id": "turn-previous",
                "use_full_result_set": True,
                "reason": "Compare against the prior result set.",
            }
        ],
        "actions": [
            {
                "name": "load_prior_result_set",
                "inputs": {"turn_id": "turn-previous"},
                "max_rows": 100,
                "required": True,
            },
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            },
            {
                "name": "compare_ticket_sets",
                "inputs": {"criteria": ["internet", "speed"]},
                "max_rows": 50,
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


def _source_family_ticket_search_payload(**overrides) -> dict:
    payload = {
        "task_type": "ticket_set_search",
        "answer_language": "user",
        "confidence": 0.82,
        "requires_dispatcher": True,
        "scope": {
            "store_ids": ["2012"],
            "ticket_ids": [],
            "source_family": "tickets",
        },
        "prior_result_refs": [],
        "actions": [
            {
                "name": "ticket_set_search",
                "inputs": {
                    "store_ids": ["2012"],
                    "criteria": ["Telecom Partner", "degraded line", "opened vendor ticket"],
                    "query": "Telecom Partner degraded line opened vendor ticket",
                },
                "max_rows": 50,
                "required": True,
            }
        ],
        "comparison_contract": {"left_label": "", "right_label": "", "criteria": [], "must_report": []},
        "answer_contract": {
            "must_include": ["checked_scope", "criteria", "evidence_limits"],
            "must_not_claim": ["searched_drive_instead_of_tickets"],
        },
        "risk_flags": ["source_family_mismatch"],
        "cost_plan": {"max_planner_calls": 1, "max_tool_actions": 1, "max_ticket_ids": 75, "max_rows": 50},
        "fallback": "legacy_universal_chat",
    }
    payload.update(overrides)
    return payload


class _FakeUsage:
    prompt_token_count = 100
    candidates_token_count = 20
    thoughts_token_count = 0


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = _FakeUsage()


class _FakeModel:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[dict] = []

    def generate_content(self, prompt: str, generation_config=None):
        self.calls.append({"prompt": prompt, "generation_config": generation_config})
        return _FakeResponse(self.response_text)


class DispatcherShadowRuntimeTest(unittest.TestCase):
    def test_disabled_mode_performs_zero_planner_calls(self):
        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(),
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
            )

        self.assertFalse(result.eligible)
        self.assertFalse(result.planner_attempted)
        self.assertEqual(result.planner_calls, 0)
        self.assertEqual(result.fallback_reason, "dispatcher_disabled")
        self.assertFalse(result.answer_affecting)
        self.assertIn("prior_result_reference", result.candidate_classes)
        self.assertIn("bulk_ticket_ids", result.candidate_classes)
        self.assertIn("multi_store_ticket_search", result.candidate_classes)
        self.assertIn("challenge_fact_check", result.candidate_classes)
        trace = result.to_trace_dict()
        self.assertNotIn("latest_question", trace)
        self.assertNotIn("messages", trace)

    def test_multi_store_candidate_preserves_scope_and_stays_non_answer_affecting(self):
        store_ids = ["2012", "2011", "2002", "2003", "2004", "2006"]
        question = (
            "For every store 2012 2011 2002 2003 2004 2006 find all "
            "internet speed connection tickets for the last 365 days"
        )
        captured_store_ids: list[tuple[str, ...]] = []
        original_detector = ai_dispatcher_runtime.detect_dispatcher_candidates

        def capture_candidate(**kwargs):
            captured_store_ids.append(kwargs["deterministic_entities"].store_ids)
            return original_detector(**kwargs)

        with (
            patch.object(ai_dispatcher_runtime, "detect_dispatcher_candidates", side_effect=capture_candidate),
            patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question=question,
                    routing_question=question,
                    messages=[{"role": "user", "content": question}],
                    semantic_frame={"intent": "ticket_retrieval", "target_context": "global"},
                    explicit_store_ids=store_ids,
                    explicit_ticket_id=None,
                    previous_assistant_text="",
                ),
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
            )

        self.assertEqual(captured_store_ids, [tuple(store_ids)])
        self.assertIn("multi_store_ticket_search", result.candidate_classes)
        self.assertEqual(result.planner_calls, 0)
        self.assertFalse(result.answer_affecting)
        self.assertNotIn("multi_store_ticket_search", ai_dispatcher_runtime.PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
        self.assertEqual(result.config.max_tool_actions, 4)
        self.assertEqual(result.config.max_explicit_ticket_ids, 75)
        self.assertEqual(result.config.max_action_rows, 100)

    def test_multi_store_candidate_rejects_ticket_and_non_ticket_controls(self):
        controls = [
            {
                "question": "For store 2008 find internet tickets",
                "stores": ["2008"],
                "ticket_id": None,
            },
            {
                "question": "Show ticket #1048",
                "stores": [],
                "ticket_id": 1048,
            },
            {
                "question": "Compare tickets 950012 and 950027",
                "stores": [],
                "ticket_id": 950053,
            },
            {
                "question": "How do I reset routers at stores 2012 and 2011?",
                "stores": ["2012", "2011"],
                "ticket_id": None,
            },
        ]

        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            for control in controls:
                with self.subTest(question=control["question"]):
                    result = ai_dispatcher_runtime.run_dispatcher_shadow(
                        **_runtime_kwargs(
                            latest_question=control["question"],
                            routing_question=control["question"],
                            messages=[{"role": "user", "content": control["question"]}],
                            semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
                            explicit_store_ids=control["stores"],
                            explicit_ticket_id=control["ticket_id"],
                            previous_assistant_text="",
                        ),
                        config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
                    )
                    self.assertNotIn("multi_store_ticket_search", result.candidate_classes)
                    self.assertFalse(result.answer_affecting)

    def test_candidate_observation_detects_disabled_followups_without_external_calls(self):
        forbidden = AssertionError("candidate observation must not call planner or tools")
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", side_effect=forbidden),
            patch("app.services.bigquery.bulk_lookup_ticket_ids", side_effect=forbidden),
            patch("app.services.firestore.get_db", side_effect=forbidden),
        ):
            date_result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="i just want the one for July and Jun",
                    routing_question="i just want the one for July and Jun",
                    messages=[
                        {"role": "assistant", "content": "Earlier internet ticket results."},
                        {"role": "user", "content": "i just want the one for July and Jun"},
                    ],
                    semantic_frame={"intent": "ticket_retrieval", "target_context": "prior_turn"},
                    explicit_store_ids=[],
                    explicit_ticket_id=None,
                    previous_assistant_text="Earlier internet ticket results.",
                ),
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
            )
            refinement_result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="only the open ones for store 2003",
                    routing_question="only the open ones for store 2003",
                    messages=[
                        {"role": "assistant", "content": "Earlier ticket result set."},
                        {"role": "user", "content": "only the open ones for store 2003"},
                    ],
                    semantic_frame={"intent": "ticket_retrieval", "target_context": "prior_turn"},
                    explicit_store_ids=["2002"],
                    explicit_ticket_id=None,
                    previous_assistant_text="Earlier ticket result set.",
                ),
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
            )
            ticket_reference_result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="show me the second one",
                    routing_question="show me the second one",
                    messages=[
                        {"role": "assistant", "content": "Earlier ticket result set."},
                        {"role": "user", "content": "show me the second one"},
                    ],
                    semantic_frame={"intent": "ticket_retrieval", "target_context": "prior_turn"},
                    explicit_store_ids=[],
                    explicit_ticket_id=None,
                    previous_assistant_text="Earlier ticket result set.",
                ),
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(mode=DispatcherMode.DISABLED),
            )

        self.assertEqual(date_result.planner_calls, 0)
        self.assertIn("followup_date_scope", date_result.candidate_classes)
        self.assertEqual(refinement_result.planner_calls, 0)
        self.assertIn("followup_result_set_refinement", refinement_result.candidate_classes)
        self.assertNotIn("followup_ticket_reference", refinement_result.candidate_classes)
        self.assertEqual(ticket_reference_result.planner_calls, 0)
        self.assertEqual(ticket_reference_result.candidate_classes, ("followup_ticket_reference",))
        self.assertFalse(date_result.answer_affecting)
        self.assertFalse(refinement_result.answer_affecting)
        self.assertFalse(ticket_reference_result.answer_affecting)

    def test_shadow_mode_skips_ineligible_good_route_without_planner_call(self):
        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="How do I reset an internet router?",
                    routing_question="How do I reset an internet router?",
                    messages=[{"role": "user", "content": "How do I reset an internet router?"}],
                    semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
                    explicit_store_ids=[],
                    explicit_ticket_id=None,
                    previous_assistant_text="",
                ),
                config=_shadow_config(),
            )

        self.assertFalse(result.eligible)
        self.assertFalse(result.planner_attempted)
        self.assertEqual(result.planner_calls, 0)
        self.assertEqual(result.fallback_reason, "not_eligible")
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.candidate_classes, ())

    def test_phase6c_followup_shadow_is_not_an_assistive_runtime_class(self):
        self.assertEqual(
            ai_dispatcher_runtime.ALLOWED_ASSISTIVE_TASK_CLASSES,
            (
                "prior_result_reference",
                "bulk_ticket_ids",
                "challenge_fact_check",
                "source_family_mismatch",
                "followup_date_scope",
                "ticket_resolution_lookup",
                "followup_result_set_refinement",
                "followup_ticket_reference",
            ),
        )
        self.assertNotIn("followup_understanding_shadow", ai_dispatcher_runtime.ALLOWED_ASSISTIVE_TASK_CLASSES)
        config = ai_dispatcher_runtime.dispatcher_config_from_mapping(
            {
                "mode": "assistive",
                "assistive_task_classes": ["prior_result_reference", "followup_understanding_shadow"],
            }
        )
        self.assertEqual(config.assistive_task_classes, ("prior_result_reference",))

        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="i just want the one for July and Jun",
                    routing_question="i just want the one for July and Jun",
                    messages=[
                        {"role": "assistant", "content": "Earlier result for internet speed."},
                        {"role": "user", "content": "i just want the one for July and Jun"},
                    ],
                    semantic_frame={"intent": "store_analytics", "target_context": "prior_turn"},
                    explicit_store_ids=[],
                    explicit_ticket_id=None,
                    previous_assistant_text="Earlier result for internet speed.",
                ),
                config=config,
            )

        self.assertFalse(result.eligible)
        self.assertFalse(result.planner_attempted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.fallback_reason, "not_eligible")

    def test_result_set_refinement_shadow_is_separate_default_off_and_allowlisted(self):
        default = ai_dispatcher_runtime.DispatcherRuntimeConfig()
        configured = ai_dispatcher_runtime.dispatcher_config_from_mapping(
            {
                "mode": "primary_for_eligible",
                "shadow_task_classes": [
                    "followup_result_set_refinement",
                    "followup_ticket_reference",
                    "unknown_class",
                    "followup_result_set_refinement",
                ],
            }
        )

        self.assertEqual(default.shadow_task_classes, ())
        self.assertEqual(
            ai_dispatcher_runtime.ALLOWED_SHADOW_TASK_CLASSES,
            ("followup_result_set_refinement", "followup_ticket_reference"),
        )
        self.assertEqual(
            configured.shadow_task_classes,
            ("followup_result_set_refinement", "followup_ticket_reference"),
        )
        self.assertTrue(ai_dispatcher_runtime.followup_result_set_refinement_shadow_enabled(configured))
        self.assertTrue(ai_dispatcher_runtime.followup_ticket_reference_shadow_enabled(configured))
        self.assertFalse(ai_dispatcher_runtime.followup_result_set_refinement_enabled(configured))
        self.assertFalse(ai_dispatcher_runtime.followup_ticket_reference_enabled(configured))
        self.assertNotIn(
            "followup_result_set_refinement",
            ai_dispatcher_runtime.PRIMARY_ANSWER_AFFECTING_TASK_CLASSES,
        )
        self.assertFalse(
            ai_dispatcher_runtime.followup_result_set_refinement_shadow_enabled(
                ai_dispatcher_runtime.DispatcherRuntimeConfig(
                    mode=DispatcherMode.DISABLED,
                    shadow_task_classes=("followup_result_set_refinement",),
                )
            )
        )
        self.assertFalse(
            ai_dispatcher_runtime.followup_ticket_reference_shadow_enabled(
                ai_dispatcher_runtime.DispatcherRuntimeConfig(
                    mode=DispatcherMode.DISABLED,
                    shadow_task_classes=("followup_ticket_reference",),
                )
            )
        )

    def test_phase6d_followup_date_scope_config_is_runtime_gated_without_planner(self):
        enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.ASSISTIVE,
            assistive_task_classes=("followup_date_scope",),
        )
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.SHADOW,
            assistive_task_classes=("followup_date_scope",),
        )

        self.assertTrue(ai_dispatcher_runtime.followup_date_scope_assist_enabled(enabled))
        self.assertFalse(ai_dispatcher_runtime.followup_date_scope_assist_enabled(disabled))

        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="i just want the one for July and Jun",
                    routing_question="i just want the one for July and Jun",
                    messages=[
                        {"role": "assistant", "content": "Earlier result for internet speed."},
                        {"role": "user", "content": "i just want the one for July and Jun"},
                    ],
                    semantic_frame={"intent": "store_analytics", "target_context": "prior_turn"},
                    explicit_store_ids=[],
                    explicit_ticket_id=None,
                    previous_assistant_text="Earlier result for internet speed.",
                ),
                config=enabled,
            )

        self.assertFalse(result.planner_attempted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.fallback_reason, "not_eligible")

    def test_phase6e_ticket_resolution_lookup_config_is_runtime_gated_without_planner(self):
        enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.ASSISTIVE,
            assistive_task_classes=("ticket_resolution_lookup",),
        )
        disabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=DispatcherMode.SHADOW,
            assistive_task_classes=("ticket_resolution_lookup",),
        )

        self.assertTrue(ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(enabled))
        self.assertFalse(ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(disabled))

        with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="How to resolved this?",
                    routing_question="How to resolved this?",
                    messages=[{"role": "user", "content": "How to resolved this?"}],
                    semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
                    explicit_store_ids=[],
                    explicit_ticket_id=950050,
                    previous_assistant_text="",
                ),
                config=enabled,
            )

        self.assertFalse(result.planner_attempted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.fallback_reason, "not_eligible")

    def test_shadow_mode_allows_non_super_admin_when_enabled(self):
        fake_model = _FakeModel(json.dumps(_planner_payload()))

        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(owner_role="user"),
                config=_shadow_config(),
            )

        self.assertTrue(result.eligible)
        self.assertTrue(result.planner_attempted)
        self.assertEqual(result.planner_calls, 1)
        trace = result.to_trace_dict()
        self.assertNotIn("cohort", trace["config"])
        self.assertNotIn("pilot_user_emails", trace["config"])

    def test_shadow_mode_calls_planner_once_with_cost_and_budget_controls(self):
        generation_configs: list[dict] = []
        fake_model = _FakeModel(json.dumps(_planner_payload()))

        def capture_generation_config(data: dict):
            generation_configs.append(data)
            return data

        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=capture_generation_config),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate") as check_cost_gate,
            patch.object(ai_dispatcher_runtime, "record_estimate") as record_estimate,
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001) as record_usage,
            patch.object(ai_dispatcher_runtime, "check_divergence") as check_divergence,
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(),
                config=_shadow_config(),
            )

        self.assertTrue(result.eligible)
        self.assertTrue(result.planner_attempted)
        self.assertEqual(result.planner_calls, 1)
        self.assertFalse(result.answer_affecting)
        self.assertTrue(result.accepted)
        self.assertEqual(result.output_tokens_limit, 1024)
        self.assertEqual(result.thinking_budget, 0)
        self.assertGreater(result.reserved_cost_cad or 0.0, 0.0)
        self.assertEqual(generation_configs[0]["max_output_tokens"], 1024)
        self.assertEqual(generation_configs[0]["thinking_config"], {"thinking_budget": 0})
        self.assertEqual(generation_configs[0]["response_mime_type"], "application/json")
        self.assertEqual(
            generation_configs[0]["response_schema"],
            ai_dispatcher_runtime.ai_dispatcher_planner.DISPATCHER_PLAN_RESPONSE_SCHEMA,
        )
        check_cost_gate.assert_called_once()
        self.assertGreater(check_cost_gate.call_args.args[1], 0.0)
        record_estimate.assert_called_once()
        record_usage.assert_called_once()
        check_divergence.assert_called_once()
        self.assertEqual(fake_model.calls[0]["generation_config"]["thinking_config"], {"thinking_budget": 0})
        self.assertIn("confidence must be a number", fake_model.calls[0]["prompt"])
        self.assertIn("scope must always be an object", fake_model.calls[0]["prompt"])
        compile_decision = result.compile_decision
        self.assertEqual(compile_decision["mode"], "shadow")
        self.assertFalse(compile_decision["execute_actions"])

    def test_invalid_planner_json_falls_back_without_answer_impact(self):
        fake_model = _FakeModel("{not json")
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(),
                config=_shadow_config(),
            )

        self.assertTrue(result.eligible)
        self.assertTrue(result.planner_attempted)
        self.assertEqual(result.planner_calls, 1)
        self.assertFalse(result.accepted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.fallback_reason, "planner_policy_rejected")
        self.assertIn("planner_validation_failed", result.reason_codes)

    def test_assistive_mode_marks_only_prior_result_comparison_answer_affecting(self):
        fake_model = _FakeModel(json.dumps(_planner_payload()))
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(),
                config=_assistive_config(),
            )

        self.assertTrue(result.accepted)
        self.assertTrue(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "prior_result_reference")
        self.assertIn("assistive_prior_result_comparison", result.reason_codes)
        self.assertEqual(result.compile_decision["mode"], "shadow")
        self.assertFalse(result.compile_decision["execute_actions"])

    def test_phase5a_assistive_keeps_bulk_and_fact_check_classes_locked(self):
        bulk_payload = _planner_payload()
        bulk_payload["task_type"] = "bulk_ticket_lookup"
        bulk_payload["prior_result_refs"] = []
        bulk_payload["actions"] = [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 100,
                "required": True,
            }
        ]
        fact_check_payload = _planner_payload()
        fact_check_payload["task_type"] = "fact_check_prior_answer"
        fact_check_payload["actions"] = [
            {
                "name": "load_prior_result_set",
                "inputs": {"turn_id": "turn-prior-claim"},
                "max_rows": 100,
                "required": True,
            },
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            }
        ]

        def run(payload: dict):
            fake_model = _FakeModel(json.dumps(payload))
            with (
                patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
                patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
                patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
                patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
                patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
                patch.object(ai_dispatcher_runtime, "check_cost_gate"),
                patch.object(ai_dispatcher_runtime, "record_estimate"),
                patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
                patch.object(ai_dispatcher_runtime, "check_divergence"),
            ):
                return ai_dispatcher_runtime.run_dispatcher_shadow(
                    **_runtime_kwargs(),
                    config=_assistive_config(),
                )

        bulk_result = run(bulk_payload)
        fact_check_result = run(fact_check_payload)

        self.assertTrue(bulk_result.accepted)
        self.assertFalse(bulk_result.answer_affecting)
        self.assertEqual(bulk_result.assistive_task_class, "")
        self.assertIn("assistive_class_locked", bulk_result.reason_codes)
        self.assertTrue(fact_check_result.accepted)
        self.assertFalse(fact_check_result.answer_affecting)
        self.assertEqual(fact_check_result.assistive_task_class, "")
        self.assertIn("assistive_class_locked", fact_check_result.reason_codes)

    def test_phase5b_assistive_marks_bulk_ticket_lookup_only_with_prior_context(self):
        bulk_payload = _planner_payload()
        bulk_payload["task_type"] = "bulk_ticket_lookup"
        bulk_payload["prior_result_refs"] = []
        bulk_payload["actions"] = [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            }
        ]

        def run(*, previous_assistant_text: str, semantic_frame: dict):
            fake_model = _FakeModel(json.dumps(bulk_payload))
            with (
                patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
                patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
                patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
                patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
                patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
                patch.object(ai_dispatcher_runtime, "check_cost_gate"),
                patch.object(ai_dispatcher_runtime, "record_estimate"),
                patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
                patch.object(ai_dispatcher_runtime, "check_divergence"),
            ):
                return ai_dispatcher_runtime.run_dispatcher_shadow(
                    **_runtime_kwargs(
                        latest_question="950053 950027",
                        routing_question="950053 950027",
                        messages=[{"role": "user", "content": "950053 950027"}],
                        semantic_frame=semantic_frame,
                        previous_assistant_text=previous_assistant_text,
                        explicit_store_ids=[],
                    ),
                    config=_phase5b_config(),
                )

        prior_context = run(
            previous_assistant_text="Earlier result set discussed internet tickets.",
            semantic_frame={"intent": "retrieve_context", "target_context": "prior_turn"},
        )
        no_prior_context = run(previous_assistant_text="", semantic_frame={"intent": "retrieve_context"})

        self.assertTrue(prior_context.accepted)
        self.assertTrue(prior_context.answer_affecting)
        self.assertEqual(prior_context.assistive_task_class, "bulk_ticket_ids")
        self.assertIn("assistive_bulk_ticket_lookup", prior_context.reason_codes)
        self.assertTrue(no_prior_context.accepted)
        self.assertFalse(no_prior_context.answer_affecting)
        self.assertEqual(no_prior_context.assistive_task_class, "")
        self.assertIn("assistive_class_locked", no_prior_context.reason_codes)

    def test_phase5b_assistive_keeps_fact_check_locked(self):
        fact_check_payload = _planner_payload()
        fact_check_payload["task_type"] = "fact_check_prior_answer"
        fact_check_payload["actions"] = [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            }
        ]
        fake_model = _FakeModel(json.dumps(fact_check_payload))
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="Where did you find that claim? Verify 950053 950027.",
                    routing_question="Where did you find that claim? Verify 950053 950027.",
                ),
                config=_phase5b_config(),
            )

        self.assertTrue(result.accepted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "")
        self.assertIn("assistive_class_locked", result.reason_codes)

    def test_phase5c_assistive_marks_bounded_fact_check_answer_affecting(self):
        fact_check_payload = _planner_payload()
        fact_check_payload["task_type"] = "fact_check_prior_answer"
        fact_check_payload["actions"] = [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            }
        ]
        fake_model = _FakeModel(json.dumps(fact_check_payload))
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="Where did you find that claim? Verify 950053 and 950027.",
                    routing_question="Where did you find that claim? Verify 950053 and 950027.",
                    semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
                ),
                config=_phase5c_config(),
            )

        self.assertTrue(result.accepted)
        self.assertTrue(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "challenge_fact_check")
        self.assertIn("assistive_challenge_fact_check", result.reason_codes)
        self.assertEqual(result.compile_decision["task_type"], "fact_check_prior_answer")

    def test_phase5c_assistive_rejects_unbounded_fact_check_actions(self):
        fact_check_payload = _planner_payload()
        fact_check_payload["task_type"] = "fact_check_prior_answer"
        fact_check_payload["actions"] = [
            {
                "name": "run_bq_query",
                "inputs": {"query": "SELECT * FROM tickets"},
                "max_rows": 100,
                "required": True,
            }
        ]
        fake_model = _FakeModel(json.dumps(fact_check_payload))
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question="Where did you find that claim? Verify 950053 and 950027.",
                    routing_question="Where did you find that claim? Verify 950053 and 950027.",
                    semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
                ),
                config=_phase5c_config(),
            )

        self.assertTrue(result.accepted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "")
        self.assertIn("assistive_class_locked", result.reason_codes)

    def test_phase6a_assistive_marks_scoped_ticket_source_family_mismatch_answer_affecting(self):
        fake_model = _FakeModel(json.dumps(_source_family_ticket_search_payload()))
        question = "Find examples of tickets for store 2012 where an engineer opened a Telecom Partner ticket for a degraded line."
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question=question,
                    routing_question=question,
                    messages=[{"role": "user", "content": question}],
                    semantic_frame={"intent": "general", "target_context": "global"},
                    explicit_store_ids=["2012"],
                    previous_assistant_text="",
                ),
                config=_phase6a_config(),
            )

        self.assertTrue(result.accepted)
        self.assertTrue(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "source_family_mismatch")
        self.assertIn("assistive_source_family_mismatch", result.reason_codes)
        self.assertEqual(result.compile_decision["task_type"], "ticket_set_search")
        self.assertEqual(result.compile_decision["effective_scope"]["source_family"], "tickets")

    def test_phase6a_assistive_rejects_contact_table_and_wrong_source_family(self):
        contact_question = "What is the Tempo Systems vendor contact phone number?"
        self.assertFalse(
            ai_dispatcher_runtime.source_family_mismatch_candidate(
                latest_question=contact_question,
                routing_question=contact_question,
                semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
            )
        )

        drive_payload = _source_family_ticket_search_payload(
            scope={"store_ids": ["2012"], "ticket_ids": [], "source_family": "drive"}
        )
        fake_model = _FakeModel(json.dumps(drive_payload))
        question = "Find examples of tickets for store 2012 where an engineer opened a Telecom Partner ticket for a degraded line."
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(
                    latest_question=question,
                    routing_question=question,
                    messages=[{"role": "user", "content": question}],
                    semantic_frame={"intent": "general", "target_context": "global"},
                    explicit_store_ids=["2012"],
                    previous_assistant_text="",
                ),
                config=_phase6a_config(),
            )

        self.assertTrue(result.accepted)
        self.assertFalse(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "")
        self.assertIn("assistive_class_locked", result.reason_codes)

    def test_primary_mode_marks_proven_prior_result_comparison_answer_affecting(self):
        fake_model = _FakeModel(json.dumps(_planner_payload()))
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=fake_model),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            result = ai_dispatcher_runtime.run_dispatcher_shadow(
                **_runtime_kwargs(),
                config=_primary_config(assistive_task_classes=("prior_result_reference",)),
            )

        self.assertTrue(result.accepted)
        self.assertTrue(result.answer_affecting)
        self.assertEqual(result.assistive_task_class, "prior_result_reference")
        self.assertIn("assistive_prior_result_comparison", result.reason_codes)
        self.assertEqual(result.compile_decision["mode"], "primary_for_eligible")
        self.assertTrue(result.compile_decision["execute_actions"])
        self.assertEqual(result.planner_calls, 1)
        self.assertEqual(result.thinking_budget, 0)
        self.assertLessEqual(result.output_tokens_limit, 2048)
        self.assertEqual(fake_model.calls[0]["generation_config"]["thinking_config"]["thinking_budget"], 0)
        self.assertEqual(fake_model.calls[0]["generation_config"]["max_output_tokens"], result.output_tokens_limit)

    def test_primary_mode_excludes_unproven_helpers_and_allows_followup_date_scope(self):
        proven = set(ai_dispatcher_runtime.PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
        self.assertEqual(
            proven,
            {
                "prior_result_reference",
                "bulk_ticket_ids",
                "challenge_fact_check",
                "source_family_mismatch",
                "followup_date_scope",
            },
        )
        self.assertNotIn("ticket_resolution_lookup", proven)
        self.assertNotIn("followup_result_set_refinement", proven)
        self.assertNotIn("followup_ticket_reference", proven)
        self.assertNotIn("multi_store_ticket_search", proven)

        primary_followup = _primary_config(assistive_task_classes=("followup_date_scope",))
        primary_unproven = _primary_config(
            assistive_task_classes=(
                "ticket_resolution_lookup",
                "followup_result_set_refinement",
                "followup_ticket_reference",
            )
        )

        self.assertTrue(ai_dispatcher_runtime.followup_date_scope_assist_enabled(primary_followup))
        self.assertFalse(ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(primary_unproven))
        self.assertFalse(ai_dispatcher_runtime.followup_result_set_refinement_enabled(primary_unproven))
        self.assertFalse(ai_dispatcher_runtime.followup_ticket_reference_enabled(primary_unproven))

    def test_config_coercion_fails_closed_and_clamps_caps(self):
        cfg = ai_dispatcher_runtime.dispatcher_config_from_mapping(
            {
                "mode": "unknown",
                "planner_daily_cap_cad": 999,
                "max_planner_calls_per_turn": 4,
                "max_tool_actions": 99,
                "max_explicit_ticket_ids": 999,
                "max_action_rows": 999,
                "max_output_tokens": 99999,
                "eligible_task_classes": ["bulk_ticket_ids", "", "bulk_ticket_ids"],
                "cohort": ["super_admin", "", "super_admin"],
                "pilot_user_emails": [
                    "jamie.rivera@northstar.example.com",
                    "invalid",
                    "alex.morgan@northstar.example.com",
                ],
            }
        )

        self.assertEqual(cfg.mode, DispatcherMode.DISABLED)
        self.assertEqual(cfg.planner_daily_cap_cad, 2.0)
        self.assertEqual(cfg.max_planner_calls_per_turn, 1)
        self.assertEqual(cfg.max_tool_actions, 4)
        self.assertEqual(cfg.max_explicit_ticket_ids, 75)
        self.assertEqual(cfg.max_action_rows, 100)
        self.assertEqual(cfg.max_output_tokens, 2048)
        self.assertEqual(cfg.eligible_task_classes, ("bulk_ticket_ids",))
        self.assertNotIn("cohort", cfg.to_trace_dict())
        self.assertNotIn("pilot_user_emails", cfg.to_trace_dict())


if __name__ == "__main__":
    unittest.main()
