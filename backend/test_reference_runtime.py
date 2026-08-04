"""End-to-end contracts for the credential-free reference runtime."""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import reference_runtime


class ReferenceRuntimeTest(unittest.TestCase):
    def setUp(self):
        reference_runtime.reset()
        self.client = TestClient(app)
        self.headers = {"Authorization": f"Bearer {reference_runtime.REFERENCE_SESSION_TOKEN}"}

    def tearDown(self):
        reference_runtime.reset()

    def test_reference_session_and_profile_do_not_use_cloud_auth_or_firestore(self):
        forbidden = AssertionError("reference runtime must not contact a cloud identity store")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.dependencies.google_auth.verify_id_token", side_effect=forbidden),
            mock.patch("app.dependencies.firestore.get_user_role", side_effect=forbidden),
            mock.patch("app.routers.auth.firestore.get_or_create_profile", side_effect=forbidden),
            mock.patch("app.routers.profile.firestore.get_or_create_profile", side_effect=forbidden),
        ):
            me = self.client.get("/api/auth/me", headers=self.headers)
            profile = self.client.get("/api/profile", headers=self.headers)

        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "morgan.chen@northstar.example.com")
        self.assertEqual(me.json()["role"], "super_admin")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["uid"], reference_runtime.REFERENCE_UID)

    def test_reference_profile_mutations_round_trip_in_process(self):
        with mock.patch.object(settings, "runtime_profile", "reference"):
            theme = self.client.patch("/api/profile", headers=self.headers, json={"theme": "dark"})
            tab = self.client.post(
                "/api/profile/tabs",
                headers=self.headers,
                json={
                    "ticket_id": 950001,
                    "subject": "Payment terminal cannot complete authorization",
                    "status": "open",
                    "priority": "high",
                },
            )
            history = self.client.post(
                "/api/profile/search-history",
                headers=self.headers,
                json={"query": "store 2001", "type": "store"},
            )
            profile = self.client.get("/api/profile", headers=self.headers)

        self.assertEqual(theme.status_code, 200)
        self.assertEqual(tab.status_code, 200)
        self.assertEqual(history.status_code, 204)
        self.assertEqual(profile.json()["theme"], "dark")
        self.assertEqual(profile.json()["open_tabs"][0]["id"], 950001)
        self.assertEqual(profile.json()["search_history"], [{"query": "store 2001", "type": "store"}])

    def test_reference_ticket_workspace_is_credential_free_and_linked(self):
        forbidden = AssertionError("reference ticket workspace must not contact external providers")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.tickets.zendesk.get_ticket", side_effect=forbidden),
            mock.patch("app.routers.ai.vertex_search.search_kb", side_effect=forbidden),
        ):
            store = self.client.get("/api/tickets/by-store/2001", headers=self.headers)
            ticket = self.client.get("/api/tickets/950001", headers=self.headers)
            history = self.client.get("/api/tickets/950001/history", headers=self.headers)
            similar = self.client.get("/api/tickets/950001/similar", headers=self.headers)
            risk = self.client.get("/api/tickets/950001/customer-risk", headers=self.headers)
            summary = self.client.post(
                "/api/ai/summary",
                headers=self.headers,
                json={"ticket_id": 950001, "mode": "brief", "language": "en"},
            )
            knowledge = self.client.post(
                "/api/ai/kb-suggestions",
                headers=self.headers,
                json={"query": "payment terminal authorization", "page_size": 3},
            )

        self.assertEqual(store.status_code, 200)
        self.assertEqual(store.json()["tickets"][0]["store_id"], "2001")
        self.assertEqual(ticket.status_code, 200)
        self.assertEqual(ticket.json()["store"]["brand"], "Cedar Grill")
        self.assertEqual(len(history.json()), 3)
        self.assertEqual(len(similar.json()["tickets"]), 5)
        self.assertEqual(risk.json()["ticket_id"], 950001)
        self.assertIn("overview", summary.json()["summary"])
        self.assertEqual(len(knowledge.json()["articles"]), 3)

    def test_reference_companion_auth_uses_synthetic_identity_without_firestore(self):
        forbidden = AssertionError("reference companion auth must not contact Firestore")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.zaf_auth.firestore.resolve_zaf_identity_email", side_effect=forbidden),
            mock.patch("app.routers.zaf_auth.firestore.is_user_allowed", side_effect=forbidden),
            mock.patch("app.routers.zaf_auth.firestore.create_zaf_session", side_effect=forbidden),
        ):
            response = self.client.post(
                "/api/zaf/auth",
                json={"email": "alex.morgan@northstar.example.com", "name": "Alex Morgan", "role": "reference"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_token"], reference_runtime.REFERENCE_SESSION_TOKEN)
        self.assertEqual(response.json()["email"], "alex.morgan@northstar.example.com")

    def test_reference_home_feeds_do_not_use_firestore(self):
        forbidden = AssertionError("reference home feeds must not contact Firestore")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.profile.my_unsolved_tickets.get_my_unsolved_tickets", side_effect=forbidden),
            mock.patch("app.routers.feedback.firestore.get_feedback_inbox", side_effect=forbidden),
            mock.patch("app.routers.announcements.announcements.list_active_for_user", side_effect=forbidden),
        ):
            unsolved = self.client.get("/api/profile/my-unsolved-tickets", headers=self.headers)
            inbox = self.client.get("/api/feedback/inbox", headers=self.headers)
            active = self.client.get("/api/announcements/active?surface=web", headers=self.headers)
            all_feedback = self.client.get("/api/feedback/all", headers=self.headers)

        self.assertEqual(unsolved.status_code, 200)
        self.assertGreater(unsolved.json()["count"], 0)
        self.assertEqual(inbox.json(), [])
        self.assertEqual(active.json()[0]["id"], "reference-release-notes")
        self.assertEqual(all_feedback.json(), [])

    def test_reference_universal_chat_streams_answer_and_evidence_without_model_calls(self):
        forbidden = AssertionError("reference chat must not contact model or persistence providers")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.universal.firestore.register_universal_chat_turn_control", side_effect=forbidden),
            mock.patch("app.routers.universal.universal_chat.decide_tools", side_effect=forbidden),
        ):
            response = self.client.post(
                "/api/universal/stream",
                headers=self.headers,
                json={
                    "messages": [{"role": "user", "content": "How many network tickets are in the reference data?"}],
                    "ticket_id": None,
                    "conversation_id": "reference-test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("network-failover tickets", response.text)
        self.assertIn('"done": true', response.text)
        self.assertIn('"tickets":', response.text)

    def test_reference_impact_telemetry_stays_in_process(self):
        forbidden = AssertionError("reference telemetry must not contact BigQuery")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.services.impact_telemetry._get_client", side_effect=forbidden),
        ):
            response = self.client.post(
                "/api/telemetry/impact-event",
                headers=self.headers,
                json={
                    "event_type": "ticket_page_opened",
                    "surface": "web",
                    "ticket_id": 950001,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": True})
        self.assertEqual(reference_runtime.impact_events()[0]["ticket_id"], 950001)

    def test_reference_admin_costs_and_maintenance_do_not_use_cloud_stores(self):
        forbidden = AssertionError("reference administration must not contact cloud stores")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.admin.firestore.is_maintenance_mode", side_effect=forbidden),
            mock.patch("app.routers.admin.firestore.set_maintenance_enabled", side_effect=forbidden),
            mock.patch("app.services.bigquery._get_client", side_effect=forbidden),
        ):
            maintenance = self.client.get("/api/admin/maintenance", headers=self.headers)
            maintenance_update = self.client.patch(
                "/api/admin/maintenance", headers=self.headers, json={"enabled": True}
            )
            snapshot = self.client.get("/api/admin/cost/snapshot", headers=self.headers)
            range_data = self.client.get(
                "/api/admin/cost/range?from_dt=2026-07-25T00:00&to_dt=2026-08-01T00:00",
                headers=self.headers,
            )
            billing = self.client.get(
                "/api/admin/cost/billing?from_dt=2026-07-25&to_dt=2026-08-01",
                headers=self.headers,
            )
            reconciliation = self.client.get(
                "/api/admin/cost/reconciliation?from_dt=2026-07-25&to_dt=2026-08-01",
                headers=self.headers,
            )

        self.assertEqual(maintenance.json(), {"enabled": False})
        self.assertEqual(maintenance_update.json(), {"enabled": True})
        self.assertEqual(snapshot.json()["today"]["calls"], 22)
        self.assertEqual(range_data.json()["total_calls"], 134)
        self.assertEqual(billing.json()["currency"], "CAD")
        self.assertEqual(reconciliation.json()["totals"]["coverage_pct"], 91.0)

    def test_reference_source_operations_are_process_local(self):
        forbidden = AssertionError("reference source administration must not contact providers")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.admin.firestore.get_all_data_sources_raw", side_effect=forbidden),
            mock.patch("app.routers.admin.scheduler_admin.list_schedulers", side_effect=forbidden),
            mock.patch("app.routers.admin.sync_runs.list_runs", side_effect=forbidden),
        ):
            sources = self.client.get("/api/admin/sources", headers=self.headers)
            schedulers = self.client.get("/api/admin/schedulers", headers=self.headers)
            runs = self.client.get("/api/admin/sync-runs", headers=self.headers)
            updated = self.client.patch(
                "/api/admin/sources/reference_support_docs",
                headers=self.headers,
                json={"enabled": False},
            )

        self.assertEqual(sources.status_code, 200)
        self.assertEqual(len(sources.json()), 7)
        self.assertEqual(len(schedulers.json()), 2)
        self.assertEqual(runs.json()[0]["status"], "success")
        self.assertFalse(updated.json()["enabled"])

    def test_reference_architecture_bundle_does_not_use_object_storage(self):
        forbidden = AssertionError("reference architecture must not contact object storage")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.admin_architecture._storage_client", side_effect=forbidden),
        ):
            manifest = self.client.get("/api/admin/architecture/manifest", headers=self.headers)
            overview = self.client.get("/api/admin/architecture/overview", headers=self.headers)
            runbook = self.client.get("/api/admin/architecture/runbook", headers=self.headers)
            diagram = self.client.get("/api/admin/architecture/assets/platform-flow.svg", headers=self.headers)

        self.assertEqual(manifest.json()["diagrams"][0]["id"], "platform-flow")
        self.assertEqual(len(overview.json()["groups"]), 2)
        self.assertEqual(len(runbook.json()["procedures"]), 2)
        self.assertIn("ESIP evidence and operations flow", diagram.text)

    def test_reference_knowledge_gap_analysis_and_draft_are_provider_free(self):
        forbidden = AssertionError("reference gap analysis must not contact providers")
        criteria = {
            "mode": "tickets_vs_all", "depth": "quick", "pattern_limit": 3,
            "period_days": 90, "min_tickets": 25, "impact_metric": "highest_time_cost",
            "sources": [], "gap_types": [], "audiences": ["engineer"],
            "action_types": [], "confidence": [], "scope": {},
        }
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.docs_gap.docs_gap_v2.analyze", side_effect=forbidden),
            mock.patch("app.routers.docs_gap.firestore.list_knowledge_gap_runs", side_effect=forbidden),
        ):
            analysis = self.client.post("/api/docs-gap/v2/analyze", headers=self.headers, json=criteria)
            runs = self.client.get("/api/docs-gap/v2/runs", headers=self.headers)
            draft = self.client.post(
                "/api/docs-gap/v2/draft",
                headers=self.headers,
                json={
                    "finding": analysis.json()["findings"][0],
                    "draft_type": "zendesk_rewrite",
                    "audience": "engineer",
                    "target_source": "zendesk_articles",
                },
            )

        self.assertEqual(len(analysis.json()["findings"]), 3)
        self.assertEqual(runs.json()["runs"][0]["finding_count"], 3)
        self.assertEqual(draft.json()["sections"][1]["basis"], "recommendation")

    def test_reference_analytics_stream_does_not_use_models_or_bigquery(self):
        forbidden = AssertionError("reference analytics must not contact models or BigQuery")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.analytics._decide_sources", side_effect=forbidden),
            mock.patch("app.routers.analytics.bq_service.execute_query", side_effect=forbidden),
        ):
            response = self.client.post(
                "/api/analytics/chat/stream",
                headers=self.headers,
                json={"messages": [{"role": "user", "content": "Which brands open the most tickets?"}], "timeframe_days": 365},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("ticket volumes", response.text)
        self.assertIn('"columns": ["brand", "ticket_count"]', response.text)

    def test_reference_secondary_admin_dashboards_do_not_use_providers(self):
        forbidden = AssertionError("reference administration must not contact external providers")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.admin.firestore.get_db", side_effect=forbidden),
            mock.patch("app.routers.admin.bq_service._get_client", side_effect=forbidden),
            mock.patch("app.routers.admin.usage_insights.get_usage_dashboard", side_effect=forbidden),
            mock.patch("app.routers.admin.impact_metrics.get_impact_dashboard", side_effect=forbidden),
            mock.patch("app.routers.admin.quality_telemetry.get_quality_dashboard", side_effect=forbidden),
            mock.patch("app.routers.admin.announcements_service.list_change_candidates", side_effect=forbidden),
            mock.patch("app.routers.admin.ai_dispatcher_admin.read_config", side_effect=forbidden),
            mock.patch("app.routers.admin.model_transition_capture_admin.read_config", side_effect=forbidden),
        ):
            responses = {
                "notifications": self.client.get("/api/admin/notifications", headers=self.headers),
                "users": self.client.get("/api/admin/users", headers=self.headers),
                "usage": self.client.get("/api/admin/usage/dashboard?start_date=2026-07-28&end_date=2026-08-03", headers=self.headers),
                "impact": self.client.get("/api/admin/impact/dashboard", headers=self.headers),
                "changes": self.client.get("/api/admin/change-candidates", headers=self.headers),
                "announcements": self.client.get("/api/admin/announcements", headers=self.headers),
                "quality": self.client.get("/api/admin/quality/dashboard", headers=self.headers),
                "chat_logs": self.client.get("/api/admin/chat-logs/recent", headers=self.headers),
                "dispatcher": self.client.get("/api/admin/ai-dispatcher/config", headers=self.headers),
                "dispatcher_dashboard": self.client.get("/api/admin/ai-dispatcher/dashboard", headers=self.headers),
                "capture": self.client.get("/api/admin/model-transition-capture/config", headers=self.headers),
                "service_desk_kpi": self.client.get("/api/admin/usage/service-desk-kpi?date=2026-08-03", headers=self.headers),
                "kpi_snapshots": self.client.get("/api/admin/usage/service-desk-kpi/snapshots", headers=self.headers),
                "kpi_delivery": self.client.get("/api/admin/usage/service-desk-kpi/delivery", headers=self.headers),
            }

        self.assertEqual({key: value.status_code for key, value in responses.items()}, {key: 200 for key in responses})
        self.assertEqual(len(responses["users"].json()["users"]), 12)
        self.assertGreater(responses["usage"].json()["summary"]["total_queries_in_range"], 0)
        self.assertEqual(responses["impact"].json()["summary"]["active_users_post"], 11)
        self.assertEqual(responses["quality"].json()["coverage"]["partial"], False)
        self.assertGreater(len(responses["chat_logs"].json()["logs"]), 0)
        self.assertGreater(responses["service_desk_kpi"].json()["summary"]["solved_tickets"], 0)
        self.assertEqual(len(responses["kpi_snapshots"].json()["items"]), 7)
        self.assertEqual(responses["kpi_delivery"].json()["transport"], "reference")

    def test_reference_admin_mutations_and_exports_round_trip_locally(self):
        forbidden = AssertionError("reference mutations must remain local")
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.routers.admin.firestore.get_db", side_effect=forbidden),
            mock.patch("app.services.email_service.send_alert", side_effect=forbidden),
            mock.patch("app.routers.admin.announcements_service.create_change_candidate", side_effect=forbidden),
        ):
            added_recipient = self.client.post(
                "/api/admin/notifications/recipients", headers=self.headers,
                json={"email": "quality.alerts@northstar.example.com"},
            )
            tested_notification = self.client.post("/api/admin/notifications/test", headers=self.headers)
            added_user = self.client.post(
                "/api/admin/users", headers=self.headers,
                json={"email": "devon.hughes@northstar.example.com", "role": "user"},
            )
            updated_user = self.client.patch(
                "/api/admin/users/devon.hughes%40northstar.example.com", headers=self.headers,
                json={"role": "admin", "universal_chat_logging_disabled": True},
            )
            candidate = self.client.post(
                "/api/admin/change-candidates", headers=self.headers,
                json={"title": "New evidence filter", "summary": "Adds a focused evidence filter to the ticket workspace."},
            )
            candidate_id = candidate.json()["id"]
            published = self.client.post(
                f"/api/admin/change-candidates/{candidate_id}/publish", headers=self.headers,
                json={"title": "New evidence filter", "body_markdown": "A focused evidence filter is now available."},
            )
            announcement_id = published.json()["announcement"]["id"]
            stats = self.client.get(f"/api/admin/announcements/{announcement_id}/stats", headers=self.headers)
            dispatcher = self.client.patch(
                "/api/admin/ai-dispatcher/config", headers=self.headers,
                json={"mode": "assistive", "planner_daily_cap_cad": 1.5},
            )
            capture = self.client.put(
                "/api/admin/model-transition-capture/config", headers=self.headers,
                json={"enabled": True, "allowed_emails": ["riley.brooks@northstar.example.com"]},
            )
            usage_export = self.client.get(
                "/api/admin/usage/export?start_date=2026-07-28&end_date=2026-08-03&format=xlsx",
                headers=self.headers,
            )
            impact_export = self.client.get("/api/admin/impact/export?format=pdf", headers=self.headers)
            kpi_export = self.client.get(
                "/api/admin/usage/service-desk-kpi/export?date=2026-08-03&format=xlsx",
                headers=self.headers,
            )
            kpi_delivery = self.client.patch(
                "/api/admin/usage/service-desk-kpi/delivery", headers=self.headers,
                json={"enabled": False},
            )
            kpi_test = self.client.post(
                "/api/admin/usage/service-desk-kpi/delivery/test", headers=self.headers,
                json={"recipient": "morgan.chen@northstar.example.com", "report_date": "2026-08-03"},
            )
            deleted_user = self.client.delete(
                "/api/admin/users/devon.hughes%40northstar.example.com", headers=self.headers
            )

        self.assertEqual(added_recipient.status_code, 200)
        self.assertEqual(tested_notification.json()["transport"], "reference")
        self.assertEqual(added_user.json()["display_name"], "Devon Hughes")
        self.assertEqual(updated_user.json()["role"], "admin")
        self.assertTrue(updated_user.json()["universal_chat_logging_disabled"])
        self.assertEqual(candidate.status_code, 200)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(stats.json()["read_count"], 3)
        self.assertTrue(dispatcher.json()["answer_affecting_allowed"])
        self.assertTrue(capture.json()["enabled"])
        self.assertEqual(usage_export.status_code, 200)
        self.assertGreater(len(usage_export.content), 1000)
        self.assertEqual(impact_export.status_code, 200)
        self.assertTrue(impact_export.content.startswith(b"%PDF"))
        self.assertEqual(kpi_export.status_code, 200)
        self.assertGreater(len(kpi_export.content), 1000)
        self.assertFalse(kpi_delivery.json()["enabled"])
        self.assertTrue(kpi_test.json()["test"]["sent"])
        self.assertEqual(deleted_user.json(), {"deleted": "devon.hughes@northstar.example.com"})

    def test_reference_primary_page_matrix_is_no_network_and_runnable(self):
        forbidden = AssertionError("reference page matrix must stay inside deterministic adapters")
        gap_criteria = {
            "mode": "tickets_vs_all", "depth": "quick", "pattern_limit": 3,
            "period_days": 90, "min_tickets": 25, "impact_metric": "highest_time_cost",
            "sources": [], "gap_types": [], "audiences": ["engineer"],
            "action_types": [], "confidence": [], "scope": {},
        }
        matrix = [
            ("get", "/api/auth/me", None),
            ("get", "/api/profile", None),
            ("get", "/api/profile/my-unsolved-tickets", None),
            ("get", "/api/tickets/by-store/2001", None),
            ("get", "/api/tickets/950001", None),
            ("get", "/api/tickets/950001/customer-risk", None),
            ("get", "/api/tickets/950001/history", None),
            ("get", "/api/tickets/950001/similar", None),
            ("post", "/api/ai/summary", {"ticket_id": 950001, "mode": "brief", "language": "en"}),
            ("post", "/api/ai/kb-suggestions", {"query": "payment terminal", "page_size": 3}),
            ("get", "/api/admin/cost/snapshot", None),
            ("get", "/api/admin/sources", None),
            ("get", "/api/admin/architecture/overview", None),
            ("get", "/api/admin/usage/dashboard", None),
            ("get", "/api/admin/usage/service-desk-kpi", None),
            ("get", "/api/admin/impact/dashboard", None),
            ("get", "/api/admin/quality/dashboard", None),
            ("get", "/api/admin/chat-logs/recent", None),
            ("post", "/api/docs-gap/v2/analyze", gap_criteria),
            ("post", "/api/zaf/auth", {"email": "alex.morgan@northstar.example.com", "name": "Alex Morgan", "role": "reference"}),
        ]
        with (
            mock.patch.object(settings, "runtime_profile", "reference"),
            mock.patch("app.dependencies.google_auth.verify_id_token", side_effect=forbidden),
            mock.patch("app.dependencies.firestore.get_user_role", side_effect=forbidden),
            mock.patch("app.routers.tickets.zendesk.get_ticket", side_effect=forbidden),
            mock.patch("app.routers.ai.vertex_search.search_kb", side_effect=forbidden),
            mock.patch("app.routers.admin.firestore.get_db", side_effect=forbidden),
            mock.patch("app.services.bigquery._get_client", side_effect=forbidden),
            mock.patch("app.routers.admin_architecture._storage_client", side_effect=forbidden),
            mock.patch("app.routers.docs_gap.docs_gap_v2.analyze", side_effect=forbidden),
            mock.patch("app.routers.zaf_auth.firestore.create_zaf_session", side_effect=forbidden),
        ):
            results = []
            for method, path, body in matrix:
                request_headers = {} if path == "/api/zaf/auth" else self.headers
                kwargs = {"headers": request_headers}
                if body is not None:
                    kwargs["json"] = body
                response = getattr(self.client, method)(path, **kwargs)
                results.append((path, response.status_code))

        self.assertEqual(results, [(path, 200) for _, path, _ in matrix])


if __name__ == "__main__":
    unittest.main()
