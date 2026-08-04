"""AI dispatcher Super Admin config/dashboard tests.

Run from project root:
scripts/backend_python.sh -m pytest backend/test_ai_dispatcher_admin.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from fastapi.testclient import TestClient

from app.services import ai_dispatcher_admin, firestore, model_transition_capture_admin


class _FakeSnapshot:
    def __init__(self, data=None, doc_id: str = ""):
        self._data = data
        self.id = doc_id
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store: dict, key: str):
        self.store = store
        self.key = key

    def get(self):
        return _FakeSnapshot(self.store.get(self.key), self.key)

    def set(self, data: dict, merge: bool = False):
        if merge and isinstance(self.store.get(self.key), dict):
            self.store[self.key] = {**self.store[self.key], **dict(data)}
        else:
            self.store[self.key] = dict(data)


class _FakeQuery:
    def __init__(self, store: dict):
        self.store = store
        self._limit = 50
        self._filters: list[tuple[str, str, str]] = []

    def where(self, field: str, op: str, value: str):
        self._filters.append((field, op, value))
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, limit: int):
        self._limit = limit
        return self

    def stream(self):
        rows = list(self.store.items())
        for field, op, expected in self._filters:
            if op == ">=":
                rows = [item for item in rows if str((item[1] or {}).get(field) or "") >= str(expected)]
            elif op == "<":
                rows = [item for item in rows if str((item[1] or {}).get(field) or "") < str(expected)]
        rows = sorted(
            rows,
            key=lambda item: str((item[1] or {}).get("created_at") or ""),
            reverse=True,
        )
        return [_FakeSnapshot(data, key) for key, data in rows[: self._limit]]


class _FakeCollection:
    def __init__(self, store: dict):
        self.store = store

    def document(self, key: str):
        return _FakeDocRef(self.store, key)

    def order_by(self, *args, **kwargs):
        return _FakeQuery(self.store).order_by(*args, **kwargs)

    def where(self, *args, **kwargs):
        return _FakeQuery(self.store).where(*args, **kwargs)


class _FakeDb:
    def __init__(self):
        self.collections: dict[str, dict] = {
            "system": {},
            "chat_answer_traces": {},
            "chat_quality_reviews": {},
        }

    def collection(self, name: str):
        return _FakeCollection(self.collections.setdefault(name, {}))


def _seed_trace(fake_db: _FakeDb):
    fake_db.collections["chat_answer_traces"]["trace-1"] = {
        "created_at": "2026-07-12T12:00:00+00:00",
        "trace_id": "trace-1",
        "turn_id": "turn-1",
        "conversation_id": "conv-1",
        "messages": [{"role": "user", "content": "raw text must not leak"}],
        "final_answer": "raw answer must not leak",
        "ai_dispatcher": {
            "mode": "shadow",
            "eligible": True,
            "planner_attempted": True,
            "planner_calls": 1,
            "accepted": False,
            "answer_affecting": False,
            "candidate_classes": ["prior_result_reference", "bulk_ticket_ids"],
            "candidate_reason_codes": {
                "prior_result_reference": ["prior_result_reference"],
                "bulk_ticket_ids": ["bulk_ticket_ids"],
            },
            "fallback_reason": "planner_policy_rejected",
            "reason_codes": ["planner_validation_failed"],
            "latency_ms": 1200,
            "estimated_cost_cad": 0.002,
            "actual_cost_cad": 0.001,
            "model_surface": "universal_dispatcher_planner",
        },
        "output_critic_shadow": {
            "emitted": True,
            "attempted": True,
            "answer_affecting": False,
            "risk_level": "high",
            "risk_codes": ["followup_scope_loss"],
            "confidence": 0.91,
            "reason_short": "Raw model prose must not reach diagnostics.",
            "latency_ms": 321,
            "estimated_cost_cad": 0.0003,
            "reserved_cost_cad": 0.0006,
            "actual_cost_cad": 0.0002,
            "raw_model_response": "RAW MODEL RESPONSE",
        },
        "ai_dispatcher_result_set_memory_saved": {"memory_id": "mem-1"},
    }


class DispatcherAdminServiceTest(unittest.TestCase):
    def test_default_config_is_disabled_and_conservative(self):
        config = ai_dispatcher_admin.default_config_payload()

        self.assertEqual(config["mode"], "disabled")
        self.assertFalse(config["enabled"])
        self.assertFalse(config["answer_affecting_allowed"])
        self.assertEqual(config["assistive_task_classes"], [])
        self.assertEqual(config["shadow_task_classes"], [])
        self.assertEqual(config["planner_daily_cap_cad"], 2.0)
        self.assertEqual(config["max_planner_calls_per_turn"], 1)
        self.assertEqual(config["max_explicit_ticket_ids"], 75)

    def test_patch_validation_accepts_shadow_and_rejects_unsafe_values(self):
        safe = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "shadow",
                "planner_daily_cap_cad": 1.5,
                "max_tool_actions": 3,
                "eligible_task_classes": ["bulk_ticket_ids", "prior_result_reference"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(safe["mode"], "shadow")

        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires one approved"):
            ai_dispatcher_admin.validate_config_patch({"mode": "assistive"}, actor_email="morgan.chen@northstar.example.com")
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "planner_daily_cap_cad"):
            ai_dispatcher_admin.validate_config_patch({"planner_daily_cap_cad": 20}, actor_email="morgan.chen@northstar.example.com")
        for field in ai_dispatcher_admin.FORBIDDEN_SAFETY_PATCH_FIELDS:
            with self.subTest(field=field), self.assertRaisesRegex(
                ai_dispatcher_admin.DispatcherAdminValidationError,
                "Safety-locked config field",
            ):
                ai_dispatcher_admin.validate_config_patch({field: True}, actor_email="morgan.chen@northstar.example.com")

    def test_refinement_shadow_config_is_separate_allowlisted_and_not_answer_affecting(self):
        safe = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "shadow",
                "shadow_task_classes": [
                    "followup_result_set_refinement",
                    "followup_ticket_reference",
                ],
            },
            actor_email="morgan.chen@northstar.example.com",
        )
        public = ai_dispatcher_admin._public_config(safe)

        self.assertEqual(
            safe["shadow_task_classes"],
            ["followup_result_set_refinement", "followup_ticket_reference"],
        )
        self.assertEqual(
            public["allowed_shadow_task_classes"],
            ["followup_result_set_refinement", "followup_ticket_reference"],
        )
        self.assertFalse(public["answer_affecting_allowed"])
        self.assertEqual(public["assistive_task_classes"], [])

        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "shadow_task_classes"):
            ai_dispatcher_admin.validate_config_patch(
                {"shadow_task_classes": ["ticket_resolution_lookup"]},
                actor_email="morgan.chen@northstar.example.com",
            )

    def test_legacy_rollout_fields_are_ignored(self):
        safe = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "shadow",
                "cohort": ["super_admin"],
                "pilot_user_emails": ["riley.brooks@northstar.example.com"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(safe["mode"], "shadow")
        self.assertNotIn("cohort", safe)
        self.assertNotIn("pilot_user_emails", safe)

    def test_patch_validation_allows_staged_phase_5_assistive_classes(self):
        safe = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["prior_result_reference"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(safe["mode"], "assistive")
        self.assertEqual(safe["assistive_task_classes"], ["prior_result_reference"])

        phase5b = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["prior_result_reference", "bulk_ticket_ids"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(phase5b["assistive_task_classes"], ["prior_result_reference", "bulk_ticket_ids"])

        phase5c = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(
            phase5c["assistive_task_classes"],
            ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
        )

        phase6a = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": [
                    "prior_result_reference",
                    "bulk_ticket_ids",
                    "challenge_fact_check",
                    "source_family_mismatch",
                ],
                "eligible_task_classes": [
                    "prior_result_reference",
                    "bulk_ticket_ids",
                    "challenge_fact_check",
                    "source_family_mismatch",
                ],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(
            phase6a["assistive_task_classes"],
            ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check", "source_family_mismatch"],
        )

        phase6d = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["followup_date_scope"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(phase6d["assistive_task_classes"], ["followup_date_scope"])

        phase6e = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["ticket_resolution_lookup"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(phase6e["assistive_task_classes"], ["ticket_resolution_lookup"])

        phase6h_refinement = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["followup_result_set_refinement"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(phase6h_refinement["assistive_task_classes"], ["followup_result_set_refinement"])

        phase6h_reference = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "assistive",
                "assistive_task_classes": ["followup_ticket_reference"],
                "eligible_task_classes": ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(phase6h_reference["assistive_task_classes"], ["followup_ticket_reference"])

        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires prior_result_reference"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "assistive",
                    "assistive_task_classes": ["challenge_fact_check"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires prior_result_reference and bulk_ticket_ids"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "assistive",
                    "assistive_task_classes": ["prior_result_reference", "challenge_fact_check"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires prior_result_reference"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "assistive",
                    "assistive_task_classes": ["bulk_ticket_ids"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires Phase 5A"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "assistive",
                    "assistive_task_classes": ["source_family_mismatch"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires Phase 5A"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "assistive",
                    "assistive_task_classes": ["prior_result_reference", "bulk_ticket_ids", "source_family_mismatch"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
    def test_patch_validation_allows_primary_for_proven_classes_only(self):
        proven_answer_classes = [
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
            "followup_date_scope",
        ]
        proven_planner_classes = [
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
        ]
        safe = ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "primary_for_eligible",
                "eligible_task_classes": proven_planner_classes,
                "assistive_task_classes": proven_answer_classes,
                "shadow_task_classes": [
                    "followup_result_set_refinement",
                    "followup_ticket_reference",
                ],
                "planner_daily_cap_cad": 2.0,
                "max_planner_calls_per_turn": 1,
                "max_tool_actions": 4,
                "max_explicit_ticket_ids": 75,
                "max_action_rows": 100,
                "max_output_tokens": 2048,
            },
            actor_email="morgan.chen@northstar.example.com",
        )

        self.assertEqual(safe["mode"], "primary_for_eligible")
        self.assertEqual(safe["eligible_task_classes"], proven_planner_classes)
        self.assertEqual(safe["assistive_task_classes"], proven_answer_classes)
        self.assertEqual(
            safe["shadow_task_classes"],
            ["followup_result_set_refinement", "followup_ticket_reference"],
        )

        public = ai_dispatcher_admin._public_config(safe)
        self.assertIn("primary_for_eligible", public["allowed_modes"])
        self.assertNotIn("primary_for_eligible", public["blocked_modes"])
        self.assertTrue(public["answer_affecting_allowed"])
        self.assertEqual(set(public["primary_eligible_task_classes"]), set(proven_planner_classes))
        self.assertEqual(set(public["primary_answer_affecting_task_classes"]), set(proven_answer_classes))

    def test_patch_validation_rejects_unsafe_primary_config(self):
        safe_eligible = ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check", "source_family_mismatch"]
        safe_assistive = ["prior_result_reference", "bulk_ticket_ids", "challenge_fact_check", "source_family_mismatch"]

        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "requires one proven"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": [],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "unproven eligible"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible + ["multi_store_ticket_search"],
                    "assistive_task_classes": safe_assistive,
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "unproven answer-affecting"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": safe_assistive + ["ticket_resolution_lookup"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "unproven answer-affecting"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": safe_assistive + ["followup_ticket_reference"],
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "max_tool_actions"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": safe_assistive,
                    "max_tool_actions": 5,
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "max_explicit_ticket_ids"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": safe_assistive,
                    "max_explicit_ticket_ids": 76,
                },
                actor_email="morgan.chen@northstar.example.com",
            )
        with self.assertRaisesRegex(ai_dispatcher_admin.DispatcherAdminValidationError, "max_output_tokens"):
            ai_dispatcher_admin.validate_config_patch(
                {
                    "mode": "primary_for_eligible",
                    "eligible_task_classes": safe_eligible,
                    "assistive_task_classes": safe_assistive,
                    "max_output_tokens": 2049,
                },
                actor_email="morgan.chen@northstar.example.com",
            )

    def test_dashboard_is_compact_and_summarizes_dispatcher_telemetry(self):
        payload = ai_dispatcher_admin.summarize_dashboard(
            config={"mode": "shadow"},
            traces=[
                {
                    "created_at": "2026-07-01T12:00:00+00:00",
                    "trace_id": "trace-1",
                    "turn_id": "turn-1",
                    "conversation_id": "conv-1",
                    "mode": "shadow",
                    "eligible": True,
                    "planner_attempted": True,
                    "accepted": False,
                    "answer_affecting": False,
                    "candidate_classes": ["prior_result_reference", "bulk_ticket_ids"],
                    "candidate_reason_codes": {
                        "prior_result_reference": ["prior_result_reference"],
                        "bulk_ticket_ids": ["bulk_ticket_ids"],
                    },
                    "fallback_reason": "planner_policy_rejected",
                    "reason_codes": ["planner_validation_failed"],
                    "latency_ms": 1200,
                    "estimated_cost_cad": 0.002,
                    "actual_cost_cad": 0.001,
                    "output_critic": {
                        "emitted": True,
                        "attempted": True,
                        "answer_affecting": False,
                        "risk_level": "high",
                        "risk_codes": ["followup_scope_loss"],
                        "latency_ms": 800,
                        "estimated_cost_cad": 0.0003,
                        "actual_cost_cad": 0.0002,
                    },
                    "messages": "raw must not be present in real compact rows",
                }
            ],
            quality_events=[
                {"trace_id": "trace-1", "quality": "suspect"},
                {"trace_id": "trace-1", "quality": "suspect"},
                {"trace_id": "trace-1", "review_quality": "good"},
            ],
            turn_rows=[{"trace_id": "trace-1", "latency_ms": 6400}],
            range_info={
                "timezone": "America/New_York",
                "start_date": "2026-07-01",
                "end_date": "2026-07-07",
                "max_days": 31,
            },
        )

        self.assertEqual(payload["summary"]["eligible_turns"], 1)
        self.assertEqual(payload["summary"]["vetoed_or_rejected_plans"], 1)
        self.assertEqual(payload["cost"]["estimated_total_cad"], 0.002)
        prior_row = next(row for row in payload["class_exposure"] if row["task_class"] == "prior_result_reference")
        self.assertEqual(prior_row["candidate_turns"], 1)
        self.assertEqual(prior_row["planner_attempted_turns"], 1)
        self.assertEqual(prior_row["rejected_plans"], 1)
        self.assertEqual(prior_row["quality_turns"], 1)
        self.assertEqual(prior_row["turn_latency"]["p50_ms"], 6400.0)
        self.assertEqual(payload["quality_join"]["quality_event_count"], 3)
        self.assertEqual(payload["quality_join"]["unique_quality_answer_traces"], 1)
        self.assertEqual(payload["quality_join"]["duplicate_quality_events"], 2)
        self.assertEqual(payload["output_critic"]["attempted_turns"], 1)
        self.assertEqual(payload["output_critic"]["answer_affecting_turns"], 0)
        self.assertEqual(payload["output_critic"]["risk_codes"], [{"name": "followup_scope_loss", "count": 1}])
        self.assertEqual(payload["output_critic"]["latency"]["p50_ms"], 800.0)
        self.assertEqual(payload["output_critic"]["cost"]["actual_total_cad"], 0.0002)
        self.assertFalse(ai_dispatcher_admin.payload_contains_raw_text_keys(payload))

    def test_firestore_fetch_compacts_traces(self):
        fake_db = _FakeDb()
        _seed_trace(fake_db)

        rows = ai_dispatcher_admin.fetch_recent_dispatcher_traces(limit=10, db=fake_db)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trace_id"], "trace-1")
        self.assertTrue(rows[0]["result_set_saved"])
        self.assertEqual(rows[0]["candidate_classes"], ["prior_result_reference", "bulk_ticket_ids"])
        self.assertEqual(rows[0]["output_critic"]["risk_codes"], ["followup_scope_loss"])
        self.assertEqual(rows[0]["output_critic"]["latency_ms"], 321.0)
        self.assertNotIn("reason_short", rows[0]["output_critic"])
        self.assertNotIn("raw_model_response", rows[0]["output_critic"])
        self.assertNotIn("messages", rows[0])
        self.assertNotIn("final_answer", rows[0])

    def test_critic_exposure_aggregates_skips_errors_and_cost_without_raw_details(self):
        base = {
            "mode": "shadow",
            "eligible": False,
            "planner_attempted": False,
            "accepted": False,
            "answer_affecting": False,
        }
        payload = ai_dispatcher_admin.summarize_dashboard(
            config={"mode": "shadow"},
            traces=[
                {
                    **base,
                    "trace_id": "critic-good",
                    "output_critic": {
                        "emitted": True,
                        "attempted": True,
                        "answer_affecting": False,
                        "risk_level": "none",
                        "latency_ms": 100,
                        "actual_cost_cad": 0.0001,
                    },
                },
                {
                    **base,
                    "trace_id": "critic-skipped",
                    "output_critic": {
                        "emitted": False,
                        "attempted": False,
                        "answer_affecting": False,
                        "risk_level": "none",
                        "skipped_reason": "disabled_by_config",
                    },
                },
                {
                    **base,
                    "trace_id": "critic-error",
                    "output_critic": {
                        "emitted": True,
                        "attempted": True,
                        "answer_affecting": False,
                        "risk_level": "medium",
                        "risk_codes": ["output_critic_error"],
                        "error_code": "timeout",
                        "latency_ms": 2500,
                        "estimated_cost_cad": 0.0003,
                    },
                },
            ],
        )

        critic = payload["output_critic"]
        self.assertEqual(critic["observed_turns"], 3)
        self.assertEqual(critic["attempted_turns"], 2)
        self.assertEqual(critic["skipped_turns"], 1)
        self.assertEqual(critic["error_turns"], 1)
        self.assertEqual(critic["answer_affecting_turns"], 0)
        self.assertEqual(critic["skip_reasons"], [{"name": "disabled_by_config", "count": 1}])
        self.assertEqual(critic["error_codes"], [{"name": "timeout", "count": 1}])
        self.assertFalse(ai_dispatcher_admin.payload_contains_raw_text_keys(payload))

    def test_critic_compaction_buckets_error_and_unknown_skip_without_leaking_details(self):
        compact = ai_dispatcher_admin._compact_output_critic(
            {
                "emitted": True,
                "attempted": True,
                "answer_affecting": False,
                "risk_level": "medium",
                "risk_codes": ["output_critic_error", "not_allowlisted"],
                "skipped_reason": "internal raw skip detail",
                "error": "provider timeout SECRET RESPONSE BODY",
                "reason_short": "RAW MODEL PROSE",
                "raw_model_response": "RAW MODEL RESPONSE",
                "latency_ms": -1,
            }
        )

        rendered = repr(compact)
        self.assertEqual(compact["risk_codes"], ["output_critic_error"])
        self.assertEqual(compact["skipped_reason"], "other")
        self.assertEqual(compact["error_code"], "timeout")
        self.assertNotIn("latency_ms", compact)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("RAW MODEL", rendered)

    def test_bounded_date_fetch_and_raw_key_detector(self):
        fake_db = _FakeDb()
        _seed_trace(fake_db)
        fake_db.collections["chat_answer_traces"]["trace-before-Eastern-day"] = {
            **fake_db.collections["chat_answer_traces"]["trace-1"],
            "created_at": "2026-07-12T02:00:00+00:00",
            "trace_id": "trace-before-Eastern-day",
        }
        range_info = {
            "start_at": "2026-07-12T04:00:00+00:00",
            "end_at": "2026-07-14T04:00:00+00:00",
        }

        rows = ai_dispatcher_admin.fetch_dispatcher_traces(range_info, limit=9999, db=fake_db)

        self.assertEqual([row["trace_id"] for row in rows], ["trace-1"])
        self.assertTrue(ai_dispatcher_admin.payload_contains_raw_text_keys({"raw_prompt": "secret"}))
        self.assertTrue(ai_dispatcher_admin.payload_contains_raw_text_keys({"nested": {"full_text": "secret"}}))

    def test_accepted_without_answer_is_suppressed_and_duplicate_trace_is_deduplicated(self):
        refinement_trace = {
            "trace_id": "trace-locked",
            "mode": "shadow",
            "eligible": True,
            "planner_attempted": True,
            "accepted": True,
            "answer_affecting": False,
            "candidate_classes": ["followup_result_set_refinement"],
            "candidate_reason_codes": {"followup_result_set_refinement": ["explicit_result_set_filter"]},
            "latency_ms": 500,
        }
        ticket_reference_trace = {
            **refinement_trace,
            "trace_id": "trace-ticket-reference",
            "candidate_classes": ["followup_ticket_reference"],
            "candidate_reason_codes": {"followup_ticket_reference": ["ticket_reference_followup"]},
        }

        payload = ai_dispatcher_admin.summarize_dashboard(
            config={"mode": "shadow"},
            traces=[refinement_trace, dict(refinement_trace), ticket_reference_trace],
        )

        refinement_row = next(
            item for item in payload["class_exposure"]
            if item["task_class"] == "followup_result_set_refinement"
        )
        ticket_reference_row = next(
            item for item in payload["class_exposure"]
            if item["task_class"] == "followup_ticket_reference"
        )
        self.assertEqual(payload["summary"]["trace_count"], 2)
        for row in (refinement_row, ticket_reference_row):
            self.assertEqual(row["candidate_turns"], 1)
            self.assertEqual(row["accepted_plans"], 1)
            self.assertEqual(row["suppressed_or_locked_turns"], 1)
            self.assertEqual(row["answer_affecting_turns"], 0)
        self.assertEqual(
            refinement_row["reason_codes"],
            [{"name": "explicit_result_set_filter", "count": 1}],
        )
        self.assertEqual(
            ticket_reference_row["reason_codes"],
            [{"name": "ticket_reference_followup", "count": 1}],
        )

    def test_get_dashboard_clamps_range_and_reports_partial_sources(self):
        fake_db = _FakeDb()
        with (
            mock.patch.object(ai_dispatcher_admin, "fetch_dispatcher_traces", return_value=[]),
            mock.patch(
                "app.services.quality_telemetry.fetch_quality_review_events",
                side_effect=RuntimeError("quality unavailable"),
            ),
            mock.patch(
                "app.services.quality_telemetry.fetch_turn_log_rows",
                side_effect=RuntimeError("turn logs unavailable"),
            ),
        ):
            payload = ai_dispatcher_admin.get_dashboard(
                start_date="2026-01-01",
                end_date="2026-07-13",
                db=fake_db,
            )

        self.assertEqual(payload["range"]["start_date"], "2026-06-13")
        self.assertEqual(payload["range"]["end_date"], "2026-07-13")
        self.assertEqual(payload["range"]["max_days"], 31)
        self.assertTrue(payload["summary"]["partial"])
        self.assertEqual(len(payload["warnings"]), 2)
        self.assertIn("No dispatcher traces matched", payload["caveats"][-1])


class DispatcherAdminRouteTest(unittest.TestCase):
    def tearDown(self):
        model_transition_capture_admin.reset_runtime_cache()

    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self, role: str = firestore.ROLE_SUPER_ADMIN):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": "u1",
                    "email": "morgan.chen@northstar.example.com",
                    "name": "Boss",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=role),
        ):
            yield

    def test_super_admin_can_read_update_disable_and_view_dashboard(self):
        fake_db = _FakeDb()
        _seed_trace(fake_db)
        with (
            self._auth_patches(),
            mock.patch("app.services.ai_dispatcher_admin.firestore.get_db", return_value=fake_db),
            mock.patch("app.services.quality_telemetry.fetch_turn_log_rows", return_value=[]),
        ):
            read = self.client.get("/api/admin/ai-dispatcher/config", headers={"Authorization": "Bearer fake-token"})
            patch = self.client.patch(
                "/api/admin/ai-dispatcher/config",
                headers={"Authorization": "Bearer fake-token"},
                json={"mode": "shadow", "planner_daily_cap_cad": 1.0},
            )
            dashboard = self.client.get(
                "/api/admin/ai-dispatcher/dashboard?start_date=2026-07-01&end_date=2026-07-31",
                headers={"Authorization": "Bearer fake-token"},
            )
            disabled = self.client.post("/api/admin/ai-dispatcher/emergency-disable", headers={"Authorization": "Bearer fake-token"})

        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["mode"], "disabled")
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.json()["mode"], "shadow")
        self.assertFalse(patch.json()["answer_affecting_allowed"])
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["summary"]["trace_count"], 1)
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(disabled.json()["mode"], "disabled")
        self.assertTrue(disabled.json()["emergency_disabled"])

    def test_non_super_admin_and_preview_admin_are_denied(self):
        fake_db = _FakeDb()
        with (
            self._auth_patches(role=firestore.ROLE_ADMIN),
            mock.patch("app.services.ai_dispatcher_admin.firestore.get_db", return_value=fake_db),
        ):
            admin_read = self.client.get("/api/admin/ai-dispatcher/config", headers={"Authorization": "Bearer fake-token"})

        with (
            self._auth_patches(role=firestore.ROLE_SUPER_ADMIN),
            mock.patch("app.services.ai_dispatcher_admin.firestore.get_db", return_value=fake_db),
        ):
            preview_read = self.client.get(
                "/api/admin/ai-dispatcher/config",
                headers={"Authorization": "Bearer fake-token", "X-ESIP-Preview-Role": "admin"},
            )

        self.assertEqual(admin_read.status_code, 403)
        self.assertEqual(preview_read.status_code, 403)

    def test_super_admin_controls_default_off_capture_scope(self):
        fake_db = _FakeDb()
        with (
            self._auth_patches(),
            mock.patch("app.services.model_transition_capture_admin.firestore.get_db", return_value=fake_db),
        ):
            initial = self.client.get(
                "/api/admin/model-transition-capture/config",
                headers={"Authorization": "Bearer fake-token"},
            )
            invalid = self.client.put(
                "/api/admin/model-transition-capture/config",
                headers={"Authorization": "Bearer fake-token"},
                json={"enabled": True, "allowed_emails": []},
            )
            enabled = self.client.put(
                "/api/admin/model-transition-capture/config",
                headers={"Authorization": "Bearer fake-token"},
                json={"enabled": True, "allowed_emails": ["riley.brooks@northstar.example.com"]},
            )

        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["enabled"])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["enabled"])
        self.assertEqual(enabled.json()["allowed_emails"], ["riley.brooks@northstar.example.com"])

    def test_capture_control_denies_admin_and_preview_writes(self):
        fake_db = _FakeDb()
        with (
            self._auth_patches(role=firestore.ROLE_ADMIN),
            mock.patch("app.services.model_transition_capture_admin.firestore.get_db", return_value=fake_db),
        ):
            admin_read = self.client.get(
                "/api/admin/model-transition-capture/config",
                headers={"Authorization": "Bearer fake-token"},
            )
        with (
            self._auth_patches(role=firestore.ROLE_SUPER_ADMIN),
            mock.patch("app.services.model_transition_capture_admin.firestore.get_db", return_value=fake_db),
        ):
            preview_write = self.client.put(
                "/api/admin/model-transition-capture/config",
                headers={"Authorization": "Bearer fake-token", "X-ESIP-Preview-Role": "admin"},
                json={"enabled": False, "allowed_emails": []},
            )

        self.assertEqual(admin_read.status_code, 403)
        self.assertEqual(preview_write.status_code, 403)

    def test_patch_rejects_invalid_config(self):
        fake_db = _FakeDb()
        with (
            self._auth_patches(),
            mock.patch("app.services.ai_dispatcher_admin.firestore.get_db", return_value=fake_db),
        ):
            response = self.client.patch(
                "/api/admin/ai-dispatcher/config",
                headers={"Authorization": "Bearer fake-token"},
                json={"mode": "primary_for_eligible"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("requires one proven", response.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
