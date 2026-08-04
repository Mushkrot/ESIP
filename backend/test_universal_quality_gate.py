"""Universal Chat deterministic answer quality gate tests.

Run from project root:
scripts/backend_python.sh backend/test_universal_quality_gate.py
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.routers import universal
from app.services import firestore
from app.services import universal_answer_now


class UniversalStageTimingStreamTest(unittest.IsolatedAsyncioTestCase):
    async def _run_cadence_stream(self, save_side_effect):
        saved: list[dict] = []

        def _save(payload):
            saved.append(payload)
            if save_side_effect:
                raise save_side_effect

        req = universal.UniversalChatRequest(
            messages=[{"role": "user", "content": "how soon will your db update?"}],
            conversation_id="conv-stage-timing",
        )
        with (
            mock.patch.object(universal, "_platform_update_cadence_answer", return_value="Cadence answer."),
            mock.patch.object(universal.firestore, "get_user_role", return_value="user"),
            mock.patch.object(universal.firestore, "is_universal_chat_logging_disabled", return_value=False),
            mock.patch.object(universal.firestore, "register_universal_chat_turn_control"),
            mock.patch.object(universal.firestore, "mark_universal_chat_turn_control_completed"),
            mock.patch.object(universal.chat_logging, "save_universal_chat_turn", side_effect=_save),
        ):
            response = await universal.universal_stream(
                req,
                {"uid": "uid-stage", "email": "morgan.chen@northstar.example.com"},
            )
            chunks = [chunk async for chunk in response.body_iterator]
        return "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks), saved

    async def test_cadence_fast_path_persists_truthful_partial_timings_without_new_sse(self):
        body, saved = await self._run_cadence_stream(None)

        self.assertEqual(body.count('"status"'), 1)
        self.assertEqual(body.count('"done"'), 1)
        timings = saved[0]["counts"]["stage_timings"]
        self.assertIn("interpretation", timings["durations_ms"])
        self.assertIn("answer_production", timings["durations_ms"])
        self.assertNotIn("retrieval", timings["durations_ms"])
        self.assertTrue(timings["partial"])

    async def test_cadence_persistence_failure_remains_fail_open(self):
        body, saved = await self._run_cadence_stream(RuntimeError("storage unavailable"))

        self.assertEqual(len(saved), 1)
        self.assertIn('"done": true', body)
        self.assertNotIn('"error"', body)

    async def test_default_off_capture_can_be_enabled_without_changing_fast_answer(self):
        with mock.patch.object(
            universal.model_transition_capture_admin,
            "get_runtime_config",
            return_value={"enabled": True, "allowed_emails": ["morgan.chen@northstar.example.com"]},
        ):
            body, saved = await self._run_cadence_stream(None)

        self.assertIn('"done": true', body)
        # This deterministic path never reaches a Gemini boundary, so it must
        # remain capture-free even when the default-off feature is enabled.
        self.assertNotIn("model_transition_request_capture", saved[0]["policy_flags"])
        self.assertNotIn("model_transition_request_captures", saved[0])


class UniversalQualityGateTest(unittest.TestCase):
    def test_stage_timings_are_allowlisted_monotonic_and_call_free(self):
        clock = mock.Mock(side_effect=[10.0, 10.125, 20.0, 19.5])
        timings = universal.UniversalStageTimings(clock=clock)

        timings.start("interpretation")
        timings.start("raw_question")
        timings.finish("interpretation")
        timings.start("retrieval")
        timings.finish("retrieval")
        timings.finish("retrieval")
        snapshot = timings.snapshot()

        self.assertEqual(
            snapshot["durations_ms"],
            {"interpretation": 125, "retrieval": 0},
        )
        self.assertNotIn("raw_question", repr(snapshot))
        self.assertTrue(snapshot["partial"])
        self.assertEqual(clock.call_count, 4)

    def test_review_route_signals_are_compact_and_raw_free(self):
        metadata = universal._compact_review_trace_metadata({
            "turn_id": "turn-review",
            "conversation_id": "conv-review",
            "target_ticket_id": 950050,
            "messages": [{"role": "user", "content": "RAW QUESTION"}],
            "final_answer": "RAW ANSWER",
            "counts": {"answer_chars": "6200", "sources": "0", "tickets": "1"},
            "ai_dispatcher": {
                "candidate_classes": ["ticket_resolution_lookup", "not_allowed"],
                "assistive_task_class": "",
                "raw_response": "RAW DISPATCHER",
            },
            "output_critic_shadow": {
                "risk_codes": ["wrong_resolution_claim"],
                "reason_short": "RAW CRITIC REASON",
            },
        })

        self.assertEqual(metadata["turn_id"], "turn-review")
        self.assertEqual(metadata["answer_chars"], 6200)
        self.assertEqual(metadata["tickets_count"], 1)
        self.assertTrue(metadata["review_route_signals"]["ticket_resolution_candidate"])
        self.assertEqual(metadata["review_route_signals"]["candidate_classes"], ["ticket_resolution_lookup"])
        self.assertEqual(metadata["review_route_signals"]["risk_classes"], ["wrong_resolution_claim"])
        rendered = repr(metadata)
        self.assertNotIn("RAW QUESTION", rendered)
        self.assertNotIn("RAW ANSWER", rendered)
        self.assertNotIn("RAW DISPATCHER", rendered)
        self.assertNotIn("RAW CRITIC REASON", rendered)

    def test_query_error_does_not_become_confident_no_data(self):
        result = universal._apply_universal_answer_quality_gate(
            "No matching tickets were found.",
            tools=["run_bq_query"],
            bq_rows="Query error: Syntax error near WHERE",
        )

        self.assertTrue(result.flags["changed"])
        self.assertEqual(result.flags["action"], "replaced_query_error_no_data")
        self.assertIn("query failed", result.visible_answer)
        self.assertIn("did not treat that failure as proof", result.visible_answer)
        self.assertIn("Checked: BigQuery ticket analytics.", result.visible_answer)
        self.assertNotIn("No matching tickets were found", result.visible_answer)
        self.assertNotIn("Syntax error near WHERE", result.visible_answer)

    def test_raw_machine_artifacts_are_stripped_from_visible_answer(self):
        candidate = """Here is the answer.

Tool Call:
```json
{"name": "run_bq_query"}
```

Tool Output:
rows

DEBUG: internal note
TRACE_ID: abc123
TRACE_JSON: {"trace": "hidden"}
SOURCES_JSON: {"sources": []}
TICKETS_JSON: [{"id": 950005}]
"""

        result = universal._apply_universal_answer_quality_gate(candidate)

        self.assertTrue(result.flags["stripped_artifacts"])
        self.assertEqual(result.visible_answer, "Here is the answer.")
        self.assertNotIn("Tool Call", result.visible_answer)
        self.assertNotIn("Tool Output", result.visible_answer)
        self.assertNotIn("SOURCES_JSON", result.visible_answer)
        self.assertNotIn("TICKETS_JSON", result.visible_answer)
        self.assertNotIn("TRACE_JSON", result.visible_answer)
        self.assertNotIn("TRACE_ID", result.visible_answer)

    def test_true_no_data_answer_says_what_was_checked(self):
        result = universal._apply_universal_answer_quality_gate(
            "No repeated stores were found for this filter.",
            tools=["run_bq_query", "search_similar_tickets"],
            bq_rows=[],
            similar_tickets=[],
        )

        self.assertTrue(result.flags["checked_paths_added"])
        self.assertIn("No repeated stores were found", result.visible_answer)
        self.assertIn("Checked: BigQuery ticket analytics, similar ticket history.", result.visible_answer)

    def test_stale_empty_result_gets_limitation_caveat(self):
        result = universal._apply_universal_answer_quality_gate(
            "No tickets were found for today.",
            tools=["run_bq_query"],
            bq_rows=[],
            stale_or_incomplete_data=True,
        )

        self.assertTrue(result.flags["stale_or_incomplete_caveat_added"])
        self.assertIn("Data limitation:", result.visible_answer)
        self.assertIn("bounded check rather than proof", result.visible_answer)
        self.assertIn("Checked: BigQuery ticket analytics.", result.visible_answer)

    def test_existing_fallback_note_is_not_duplicated(self):
        answer = (
            "No tickets were found for the requested window.\n\n"
            "BigQuery returned no usable rows for this requested window, so Zendesk was checked as the ticket source of truth."
        )

        result = universal._apply_universal_answer_quality_gate(
            answer,
            tools=["run_bq_query"],
            bq_rows=[],
            zendesk_temporal_fallback_tickets=[],
            stale_or_incomplete_data=True,
        )

        self.assertFalse(result.flags["stale_or_incomplete_caveat_added"])
        self.assertFalse(result.flags["checked_paths_added"])
        self.assertEqual(result.visible_answer, answer)

    def test_supported_answer_is_left_unchanged_before_ai_validator(self):
        answer = "The top issue was **Server --> Database** with 3 tickets."

        result = universal._apply_universal_answer_quality_gate(
            answer,
            tools=["run_bq_query"],
            bq_rows=[{"issue_category": "Server --> Database", "ticket_count": 3}],
        )

        self.assertFalse(result.flags["changed"])
        self.assertEqual(result.flags["action"], "unchanged")
        self.assertEqual(result.visible_answer, answer)

    def test_unsupported_strong_claim_is_currently_ai_validator_scope(self):
        answer = "Ticket #950033 breached SLA because no public agent reply was sent within target."

        result = universal._apply_universal_answer_quality_gate(
            answer,
            tools=["run_bq_query"],
            bq_rows=[{"id": 950033, "status": "open"}],
        )

        self.assertFalse(result.flags["changed"])
        self.assertEqual(result.flags["action"], "unchanged")
        self.assertEqual(result.visible_answer, answer)

    def test_unsupported_prior_scope_claim_is_rewritten(self):
        answer = (
            "You are correct. My previous response did not include ticket #950053 because "
            "the previous query was for a different list of store IDs (2012, 2011, 2003, 2004, 2006), "
            "and store 2003 was not in that list.\n\n"
            "Ticket #950053 is for store 2003 and mentions internet connectivity."
        )

        result = universal._apply_universal_answer_quality_gate(answer)

        self.assertTrue(result.flags["changed"])
        self.assertTrue(result.flags["unsupported_prior_scope_claim"])
        self.assertEqual(result.flags["action"], "rewrote_unsupported_prior_scope_claim")
        self.assertIn("scope/retrieval miss", result.visible_answer)
        self.assertIn("Ticket #950053", result.visible_answer)
        self.assertNotIn("2002 was not in that list", result.visible_answer)
        self.assertNotIn("different list of store IDs", result.visible_answer)


class UniversalAnswerNowContractTest(unittest.TestCase):
    def test_answer_now_is_partial_finalization_not_client_abort(self):
        result = universal_answer_now.answer_now_candidate_policy(
            "Current safe candidate.",
            answer_now_requested=True,
            changed_finalization_policy=True,
        )

        self.assertIn("Current safe candidate.", result.visible_answer)
        self.assertIn("because you asked to answer now", result.visible_answer)
        self.assertTrue(result.flags["partial_caveat_added"])
        self.assertFalse(result.flags["client_aborted"])
        self.assertEqual(result.flags["action"], "answer_now_partial_caveat")

    def test_answer_now_partial_caveat_only_when_policy_changes(self):
        unchanged = universal_answer_now.answer_now_candidate_policy(
            "Final answer already accepted.",
            answer_now_requested=True,
            changed_finalization_policy=False,
        )
        not_requested = universal_answer_now.answer_now_candidate_policy(
            "Normal final answer.",
            answer_now_requested=False,
            changed_finalization_policy=True,
        )

        self.assertFalse(unchanged.flags["partial_caveat_added"])
        self.assertEqual(unchanged.visible_answer, "Final answer already accepted.")
        self.assertFalse(not_requested.flags["partial_caveat_added"])
        self.assertEqual(not_requested.visible_answer, "Normal final answer.")

    def test_answer_now_without_candidate_waits_for_safe_candidate(self):
        result = universal_answer_now.answer_now_candidate_policy(
            "",
            answer_now_requested=True,
            changed_finalization_policy=True,
        )

        self.assertEqual(result.visible_answer, "")
        self.assertTrue(result.flags["wait_for_safe_candidate"])
        self.assertEqual(result.flags["action"], "wait_for_safe_candidate")

    def test_turn_control_requires_owner_and_live_matching_turn(self):
        now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
        payload = universal_answer_now.build_turn_control_payload(
            turn_id="turn-1",
            conversation_id="conv-1",
            owner_uid="uid-1",
            owner_email="olivia.parker@northstar.example.com",
            now=now,
        )

        active = universal_answer_now.evaluate_turn_control_document(
            payload,
            owner_uid="uid-1",
            owner_email="olivia.parker@northstar.example.com",
            conversation_id="conv-1",
            now=now + timedelta(seconds=10),
        )
        wrong_user = universal_answer_now.evaluate_turn_control_document(
            payload,
            owner_uid="uid-2",
            owner_email="priya.shah@northstar.example.com",
            conversation_id="conv-1",
            now=now + timedelta(seconds=10),
        )
        expired = universal_answer_now.evaluate_turn_control_document(
            payload,
            owner_uid="uid-1",
            owner_email="olivia.parker@northstar.example.com",
            conversation_id="conv-1",
            now=now + timedelta(minutes=10),
        )
        unsupported = universal_answer_now.evaluate_turn_control_document(
            {**payload, "action": "stop_response"},
            owner_uid="uid-1",
            owner_email="olivia.parker@northstar.example.com",
            conversation_id="conv-1",
            now=now + timedelta(seconds=10),
        )

        self.assertTrue(active.available)
        self.assertEqual(active.status, "active")
        self.assertEqual(wrong_user.status, "forbidden")
        self.assertFalse(wrong_user.available)
        self.assertEqual(expired.status, "expired")
        self.assertFalse(expired.available)
        self.assertEqual(unsupported.status, "unsupported_action")
        self.assertFalse(unsupported.available)

    def test_turn_registration_payload_contains_no_raw_transcript_fields(self):
        payload = universal_answer_now.build_turn_registration_payload(
            turn_id="turn-1",
            conversation_id="conv-1",
            owner_uid="uid-1",
            owner_email="olivia.parker@northstar.example.com",
        )

        self.assertEqual(payload["status"], "active")
        self.assertFalse(payload["answer_now_requested"])
        for raw_key in ("question", "answer", "messages", "transcript", "content", "prompt"):
            self.assertNotIn(raw_key, payload)


class UniversalTurnControlEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.main import app

        cls.client = TestClient(app)

    @contextmanager
    def _auth_patches(self):
        with (
            mock.patch(
                "app.dependencies.google_auth.verify_id_token",
                return_value={
                    "uid": "uid-1",
                    "email": "alex.morgan@northstar.example.com",
                    "name": "Owner",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=firestore.ROLE_USER),
            mock.patch("app.dependencies.firestore.validate_zaf_session", return_value=None),
        ):
            yield

    def test_turn_control_endpoint_accepts_owned_answer_now_request(self):
        with (
            self._auth_patches(),
            mock.patch(
                "app.routers.universal.firestore.request_universal_chat_answer_now",
                return_value={
                    "status": "accepted",
                    "available": True,
                    "action": "answer_now",
                    "turn_id": "turn-1",
                    "conversation_id": "conv-1",
                },
            ) as mocked,
        ):
            response = self.client.post(
                "/api/universal/turns/turn-1/control",
                json={"action": "answer_now", "conversation_id": "conv-1"},
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["action"], "answer_now")
        self.assertTrue(body["answer_now_requested"])
        self.assertIn("Preparing the best available answer", body["message"])
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["turn_id"], "turn-1")
        self.assertEqual(kwargs["conversation_id"], "conv-1")
        self.assertEqual(kwargs["owner_uid"], "uid-1")
        self.assertEqual(kwargs["owner_email"], "alex.morgan@northstar.example.com")
        self.assertNotIn("question", kwargs)
        self.assertNotIn("answer", kwargs)
        self.assertNotIn("messages", kwargs)

    def test_turn_control_endpoint_rejects_unsupported_action_before_firestore_write(self):
        with (
            self._auth_patches(),
            mock.patch("app.routers.universal.firestore.request_universal_chat_answer_now") as mocked,
        ):
            response = self.client.post(
                "/api/universal/turns/turn-1/control",
                json={"action": "stop_response", "conversation_id": "conv-1"},
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 422)
        mocked.assert_not_called()

    def test_turn_control_endpoint_requires_authentication(self):
        with mock.patch("app.routers.universal.firestore.request_universal_chat_answer_now") as mocked:
            response = self.client.post(
                "/api/universal/turns/turn-1/control",
                json={"action": "answer_now", "conversation_id": "conv-1"},
            )

        self.assertIn(response.status_code, {401, 403})
        mocked.assert_not_called()

    def test_turn_control_endpoint_returns_controlled_errors_for_unavailable_turn(self):
        for status in ("missing", "expired", "conversation_mismatch"):
            with (
                self._auth_patches(),
                mock.patch(
                    "app.routers.universal.firestore.request_universal_chat_answer_now",
                    return_value={
                        "status": status,
                        "available": False,
                        "action": "answer_now",
                        "reason": f"{status} reason",
                    },
                ),
            ):
                response = self.client.post(
                    "/api/universal/turns/turn-1/control",
                    json={"action": "answer_now", "conversation_id": "conv-1"},
                    headers={"Authorization": "Bearer fake-token"},
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["status"], status)

    def test_turn_control_endpoint_rejects_cross_user_turn(self):
        with (
            self._auth_patches(),
            mock.patch(
                "app.routers.universal.firestore.request_universal_chat_answer_now",
                return_value={
                    "status": "forbidden",
                    "available": False,
                    "action": "answer_now",
                    "reason": "turn belongs to another user",
                },
            ),
        ):
            response = self.client.post(
                "/api/universal/turns/turn-1/control",
                json={"action": "answer_now", "conversation_id": "conv-1"},
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
