#!/usr/bin/env python3
"""Tests for Super Admin quality telemetry dashboard summaries.

Run:
scripts/backend_python.sh backend/test_quality_telemetry.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient

from app.services import quality_telemetry
from app.services import firestore


class QualityTelemetryContractTest(unittest.TestCase):
    def test_error_provenance_aggregates_safe_outcomes(self):
        events = [
            {
                "source": "upstream_zendesk",
                "outcome": "upstream_http_error",
                "endpoint_class": "zendesk_ticket",
                "status_code": 503,
                "fallback_used": False,
                "reason_code": "zendesk_http_5xx",
            },
            {
                "source": "esip_model",
                "outcome": "esip_fallback_used",
                "endpoint_class": "customer_risk_model",
                "status_code": None,
                "fallback_used": True,
                "reason_code": "customer_risk_model_invalid_payload",
            },
        ]
        summary = quality_telemetry.summarize_error_provenance(events)

        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["fallback_used_count"], 1)
        self.assertEqual({row["name"]: row["count"] for row in summary["outcomes"]}, {
            "upstream_http_error": 1,
            "esip_fallback_used": 1,
        })
        self.assertFalse(quality_telemetry.contains_raw_text_keys(summary))
        poisoned = quality_telemetry.summarize_error_provenance([{
            "source": "RAW CUSTOMER TEXT",
            "outcome": "Bearer SECRET",
            "endpoint_class": "token=SECRET",
            "reason_code": "RAW PROMPT",
        }])
        self.assertNotIn("SECRET", repr(poisoned))
        self.assertNotIn("RAW", repr(poisoned))

    def test_stage_timing_aggregates_keep_partial_data_honest(self):
        full = {
            "version": "universal_stage_timings.v1",
            "durations_ms": {
                stage: (index + 1) * 100
                for index, stage in enumerate(quality_telemetry.UNIVERSAL_STAGE_NAMES)
            },
            "partial": False,
        }
        summary = quality_telemetry.summarize_stage_timings([
            {"stage_timings": full},
            {"stage_timings": '{"version":"universal_stage_timings.v1","durations_ms":{"interpretation":300,"retrieval":900},"partial":true}'},
            {"stage_timings": {}},
        ])

        by_stage = {row["stage"]: row for row in summary["stages"]}
        self.assertTrue(summary["available"])
        self.assertEqual(summary["measured_turn_count"], 2)
        self.assertEqual(summary["unmeasured_turn_count"], 1)
        self.assertEqual(summary["partial_measured_turn_count"], 1)
        self.assertEqual(by_stage["interpretation"]["count"], 2)
        self.assertEqual(by_stage["interpretation"]["p50_ms"], 100.0)
        self.assertEqual(by_stage["interpretation"]["p90_ms"], 300.0)
        self.assertEqual(by_stage["validator_repair"]["count"], 1)
        self.assertIsNone(by_stage["validator_repair"].get("avg_ms"))
        self.assertIn("need not sum", summary["caveat"])

    def test_empty_payload_has_full_contract_and_caveats(self):
        payload = quality_telemetry.summarize_quality_telemetry(
            quality_events=[],
            turn_rows=[],
            range_info=quality_telemetry.resolve_quality_range(
                "2026-06-01",
                "2026-06-03",
                today=date(2026, 6, 18),
            ),
        )

        self.assertTrue(quality_telemetry.payload_has_all_sections(payload))
        self.assertEqual(payload["summary"]["quality_event_count"], 0)
        self.assertEqual(payload["summary"]["turn_count"], 0)
        self.assertFalse(payload["coverage"]["quality_events_available"])
        self.assertFalse(payload["coverage"]["turn_logs_available"])
        self.assertGreaterEqual(len(payload["caveats"]), 1)
        self.assertFalse(quality_telemetry.contains_raw_text_keys(payload))

    def test_missing_and_unknown_fields_are_bucketed_safely(self):
        payload = quality_telemetry.summarize_quality_telemetry(
            quality_events=[
                {
                    "created_at": "2026-06-10T14:00:00Z",
                    "quality": "mystery_quality",
                    "recommended_action": "reroute",
                    "risk_classes": ["tool_error", "raw_artifact"],
                    "repair_loop_stop_reason": "scope_drift",
                    "model_surface": "answer_validator",
                    "trace_id": "trace-1",
                    "turn_id": "turn-1",
                    "latest_question": "raw question must not be returned",
                    "assistant_answer": "raw answer must not be returned",
                },
                {
                    "created_at": "2026-06-10T15:00:00Z",
                    "validator_quality": "weak",
                    "validator_recommended_action": "rerun_bq",
                    "validator_risk_classes": "false_no_data,stale_data",
                    "repair_loop_stop_reason": "accepted",
                },
            ],
            turn_rows=[],
        )

        quality_counts = {row["name"]: row["count"] for row in payload["quality_distribution"]}
        risk_counts = {row["name"]: row["count"] for row in payload["risk_classes"]}
        stop_counts = {row["name"]: row["count"] for row in payload["repair_stop_reasons"]}

        self.assertEqual(quality_counts["unknown"], 1)
        self.assertEqual(quality_counts["weak"], 1)
        self.assertEqual(risk_counts["tool_error"], 1)
        self.assertEqual(risk_counts["false_no_data"], 1)
        self.assertEqual(stop_counts["scope_drift"], 1)
        self.assertEqual(stop_counts["accepted"], 1)
        self.assertIn("unknown quality", " ".join(payload["caveats"]).lower())
        self.assertFalse(quality_telemetry.contains_raw_text_keys(payload))

    def test_sparse_latency_and_cost_are_summarized_without_crashing(self):
        payload = quality_telemetry.summarize_quality_telemetry(
            quality_events=[
                {
                    "created_at": "2026-06-11T10:00:00-04:00",
                    "validator_quality": "good",
                    "latency_ms": 1000,
                    "estimated_cost_cad": "0.001",
                    "model_surface": "answer_validator",
                },
                {
                    "created_at": "2026-06-11T10:01:00-04:00",
                    "validator_quality": "likely_wrong",
                    "latency_ms": None,
                    "estimated_cost_cad": "",
                    "model_surface": "chat_quality_review",
                },
            ],
            turn_rows=[{"latency_ms": 2500}, {"latency_ms": None}],
        )

        self.assertEqual(payload["latency"]["count"], 1)
        self.assertEqual(payload["latency"]["avg_ms"], 1000.0)
        self.assertEqual(payload["cost"]["count"], 1)
        self.assertEqual(payload["cost"]["estimated_total_cad"], 0.001)
        surfaces = {row["name"]: row["count"] for row in payload["model_surfaces"]}
        self.assertEqual(surfaces["answer_validator"], 1)
        self.assertEqual(surfaces["chat_quality_review"], 1)

    def test_event_and_unique_answer_accounting_separates_duplicates_and_conflicts(self):
        events = [
            {"trace_id": "trace-1", "mode": "auto_validator", "quality": "good"},
            {"trace_id": "trace-1", "quality": "insufficient_evidence"},
            {"trace_id": "trace-2", "mode": "auto_validator", "quality": "good"},
        ]
        payload = quality_telemetry.summarize_quality_telemetry(quality_events=events)

        summary = payload["summary"]
        self.assertEqual(summary["quality_event_count"], 3)
        self.assertEqual(summary["unique_answer_count"], 2)
        self.assertEqual(summary["auto_validator_event_count"], 2)
        self.assertEqual(summary["manual_review_event_count"], 1)
        self.assertEqual(summary["duplicate_event_count"], 1)
        self.assertEqual(summary["conflicting_verdict_count"], 1)
        unique = {row["name"]: row["count"] for row in payload["unique_turn_quality_distribution"]}
        self.assertEqual(unique["insufficient_evidence"], 1)
        self.assertEqual(unique["good"], 1)

    def test_range_is_business_time_aware_bounded_and_swaps_dates(self):
        resolved = quality_telemetry.resolve_quality_range(
            "2026-05-01",
            "2026-06-18",
            today=date(2026, 6, 18),
            max_days=14,
        )
        self.assertEqual(resolved["timezone"], "America/New_York")
        self.assertEqual(resolved["start_date"], "2026-06-05")
        self.assertEqual(resolved["end_date"], "2026-06-18")

        swapped = quality_telemetry.resolve_quality_range(
            "2026-06-18",
            "2026-06-10",
            today=date(2026, 6, 18),
            max_days=31,
        )
        self.assertEqual(swapped["start_date"], "2026-06-10")
        self.assertEqual(swapped["end_date"], "2026-06-18")

    def test_recent_events_are_compact_metadata_only(self):
        payload = quality_telemetry.summarize_quality_telemetry(
            quality_events=[
                {
                    "created_at": "2026-06-12T10:00:00Z",
                    "mode": "auto_validator",
                    "validator_quality": "suspect",
                    "validator_risk_classes": ["false_no_data"],
                    "validator_recommended_action": "zendesk_fallback",
                    "repair_loop_stop_reason": "accepted",
                    "trace_id": "trace-2",
                    "turn_id": "turn-2",
                    "conversation_id": "conv-2",
                    "latest_question": "Should never appear",
                    "assistant_answer": "Should never appear either",
                    "messages": [{"role": "user", "content": "hidden"}],
                }
            ]
        )

        self.assertEqual(len(payload["recent_events"]), 1)
        row = payload["recent_events"][0]
        self.assertEqual(row["trace_id"], "trace-2")
        self.assertEqual(row["turn_id"], "turn-2")
        self.assertEqual(row["quality"], "suspect")
        self.assertNotIn("latest_question", row)
        self.assertNotIn("assistant_answer", row)
        self.assertFalse(quality_telemetry.contains_raw_text_keys(payload))

    def test_filters_apply_to_quality_events(self):
        events = [
            {
                "mode": "auto_validator",
                "validator_quality": "weak",
                "validator_risk_classes": ["false_no_data"],
                "validator_recommended_action": "rerun_bq",
                "repair_action": "rebuild_scoped_evidence",
                "repair_loop_stop_reason": "accepted",
                "model_surface": "answer_validator",
                "requester_email": "morgan.chen@northstar.example.com",
            },
            {
                "mode": "manual_review",
                "validator_quality": "good",
                "validator_risk_classes": [],
                "validator_recommended_action": "none",
                "repair_action": "none",
                "repair_loop_stop_reason": "routine_answer",
                "model_surface": "chat_quality_review",
                "requester_email": "priya.shah@northstar.example.com",
            },
        ]
        filtered = quality_telemetry.apply_quality_filters(
            events,
            event_type="auto_validator",
            quality="weak",
            risk="false_no_data",
            validator_action="rerun_bq",
            repair_action="rebuild_scoped_evidence",
            stop_reason="accepted",
            model_surface="answer_validator",
            requester_email="morgan.chen@northstar.example.com",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["mode"], "auto_validator")

    def test_partial_source_failures_return_warnings(self):
        with (
            mock.patch(
                "app.services.quality_telemetry.fetch_quality_review_events",
                side_effect=RuntimeError("firestore down"),
            ),
            mock.patch(
                "app.services.quality_telemetry.fetch_turn_log_rows",
                return_value=[{"latency_ms": 1200}],
            ),
            mock.patch(
                "app.services.quality_telemetry.fetch_system_provenance_events",
                return_value=[],
            ),
        ):
            payload = quality_telemetry.get_quality_dashboard(
                start_date="2026-06-01",
                end_date="2026-06-02",
            )

        self.assertTrue(payload["warnings"])
        self.assertTrue(payload["coverage"]["partial"])
        self.assertEqual(payload["summary"]["turn_count"], 1)
        self.assertEqual(payload["source_counts"]["quality_events_before_filters"], 0)

    def test_review_reason_truth_table_and_threshold_boundaries(self):
        all_tags = quality_telemetry.classify_review_reason_tags(
            {
                "quality": "weak",
                "problem_types": ["ambiguous_question", "wrong_routing"],
                "risk_classes": ["false_no_data", "wrong_resolution_claim"],
                "review_route_signals": {"ticket_resolution_candidate": True},
            },
            {
                "answer_chars": quality_telemetry.REVIEW_TOO_LONG_ANSWER_CHARS,
                "latency_ms": quality_telemetry.REVIEW_SLOW_LATENCY_MS,
                "sources_count": 0,
                "tickets_count": 0,
            },
        )
        below_thresholds = quality_telemetry.classify_review_reason_tags(
            {"quality": "unknown"},
            {
                "answer_chars": quality_telemetry.REVIEW_TOO_LONG_ANSWER_CHARS - 1,
                "latency_ms": quality_telemetry.REVIEW_SLOW_LATENCY_MS - 1,
            },
        )
        supported_resolution = quality_telemetry.classify_review_reason_tags(
            {
                "quality": "good",
                "review_route_signals": {"ticket_resolution_candidate": True},
            }
        )
        missing_counts = quality_telemetry.classify_review_reason_tags(
            {"quality": "insufficient_evidence"},
            {},
        )

        self.assertEqual(all_tags, quality_telemetry.REVIEW_REASON_TAGS)
        self.assertEqual(below_thresholds, ())
        self.assertEqual(supported_resolution, ())
        self.assertEqual(missing_counts, ())

    def test_review_queue_deduplicates_conflicts_filters_and_stays_raw_free(self):
        events = [
            {
                "created_at": "2026-07-13T12:00:00Z",
                "trace_id": "trace-queue-1",
                "turn_id": "turn-queue-1",
                "mode": "auto_validator",
                "quality": "good",
                "latest_question": "raw question",
            },
            {
                "created_at": "2026-07-13T12:01:00Z",
                "trace_id": "trace-queue-1",
                "mode": "manual_review",
                "quality": "insufficient_evidence",
                "problem_types": ["ignored_negative_constraint"],
                "review_route_signals": {"ticket_resolution_candidate": True},
                "final_answer": "raw answer",
            },
            {
                "created_at": "2026-07-13T12:02:00Z",
                "trace_id": "trace-queue-2",
                "quality": "weak",
            },
        ]
        turns = [
            {
                "trace_id": "trace-queue-1",
                "turn_id": "turn-queue-1",
                "conversation_id": "conv-queue",
                "target_ticket_id": 950050,
                "answer_chars": 6000,
                "latency_ms": 30000,
                "sources_count": 0,
                "tickets_count": 0,
            }
        ]

        with (
            mock.patch("app.services.quality_telemetry.firestore.get_db", side_effect=AssertionError("storage call forbidden")),
            mock.patch("app.services.quality_telemetry.bq_service._get_client", side_effect=AssertionError("BQ call forbidden")),
        ):
            queue = quality_telemetry.build_review_queue(
                quality_events=events,
                turn_rows=turns,
                limit=25,
            )
            filtered = quality_telemetry.build_review_queue(
                quality_events=events,
                turn_rows=turns,
                reason_tag="slow",
                limit=25,
            )

        first = next(row for row in queue["rows"] if row["trace_id"] == "trace-queue-1")
        second = next(row for row in queue["rows"] if row["trace_id"] == "trace-queue-2")
        self.assertEqual(queue["answer_count"], 2)
        self.assertEqual(first["event_count"], 2)
        self.assertTrue(first["conflicting_verdict"])
        self.assertEqual(
            first["reason_tags"],
            ["missed_context", "no_data", "too_long", "slow", "needs_ticket_resolution"],
        )
        self.assertEqual(second["reason_tags"], [])
        self.assertEqual(queue["untagged_answer_count"], 1)
        self.assertEqual(filtered["answer_count"], 1)
        self.assertEqual(filtered["selected_reason_tag"], "slow")
        self.assertFalse(quality_telemetry.contains_raw_text_keys(queue))


class QualityTelemetryRouteTest(unittest.TestCase):
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

    def test_super_admin_can_read_quality_dashboard(self):
        expected = quality_telemetry.summarize_quality_telemetry(
            quality_events=[{"validator_quality": "weak", "trace_id": "t1"}],
            turn_rows=[],
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.admin.quality_telemetry.get_quality_dashboard", return_value=expected) as mocked,
        ):
            response = self.client.get(
                "/api/admin/quality/dashboard?start_date=2026-06-01&end_date=2026-06-02&quality=weak&validator_action=rerun_bq&reason_tag=slow&limit=5",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["quality_event_count"], 1)
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs["quality"], "weak")
        self.assertEqual(mocked.call_args.kwargs["validator_action"], "rerun_bq")
        self.assertEqual(mocked.call_args.kwargs["reason_tag"], "slow")
        self.assertEqual(mocked.call_args.kwargs["limit"], 5)

    def test_regular_admin_cannot_read_quality_dashboard(self):
        with self._auth_patches(role=firestore.ROLE_ADMIN):
            response = self.client.get(
                "/api/admin/quality/dashboard",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 403)

    def test_preview_admin_cannot_read_quality_dashboard(self):
        with self._auth_patches(role=firestore.ROLE_SUPER_ADMIN):
            response = self.client.get(
                "/api/admin/quality/dashboard",
                headers={
                    "Authorization": "Bearer fake-token",
                    "X-ESIP-Preview-Role": "admin",
                },
            )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
