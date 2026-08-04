"""CXone KPI ingestion tests.
Run from project root: scripts/backend_python.sh backend/test_cxone_kpi.py
"""

from __future__ import annotations

import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from google.api_core import exceptions as google_exceptions

from app.services import cxone_kpi


class CxoneKpiNormalizeTest(unittest.TestCase):
    def test_normalize_completed_contact_keeps_kpi_fields(self):
        row = cxone_kpi._normalize_contact(
            {
                "contactId": 123,
                "masterContactId": 100,
                "contactStartDate": "2026-05-28T12:00:00Z",
                "lastUpdateTime": "2026-05-28T12:03:00Z",
                "mediaTypeId": 4,
                "mediaTypeName": "Phone Call",
                "isOutbound": False,
                "agentId": 42,
                "firstName": "Casey",
                "lastName": "Nguyen",
                "teamId": 7,
                "teamName": "Service Desk",
                "skillId": 9,
                "skillName": "Inbound",
                "campaignId": 11,
                "campaignName": "Support",
                "pointOfContactId": 13,
                "pointOfContactName": "Helpdesk",
                "totalDurationSeconds": 125.5,
                "agentSeconds": 100,
                "inQueueSeconds": 15,
                "holdSeconds": 5,
                "acwSeconds": 10,
                "abandonSeconds": 0,
                "abandoned": False,
                "isRefused": False,
                "serviceLevelFlag": 1,
                "primaryDispositionId": None,
                "transferIndicatorId": 2,
                "transferIndicatorName": "None",
            },
            report_date=date(2026, 5, 28),
            fetched_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        )

        self.assertEqual(row["report_date"], "2026-05-28")
        self.assertEqual(row["contact_id"], 123)
        self.assertEqual(row["agent_id"], 42)
        self.assertEqual(row["media_type_name"], "Phone Call")
        self.assertFalse(row["is_outbound"])
        self.assertEqual(row["total_duration_seconds"], 125.5)

    def test_normalize_agent_prefers_email_and_display_name(self):
        row = cxone_kpi._normalize_agent(
            {
                "agentId": 42,
                "userId": "u1",
                "emailAddress": "Casey.Nguyen@northstar.example.com",
                "firstName": "Casey",
                "lastName": "Nguyen",
                "teamId": 7,
                "teamName": "Service Desk",
                "profileId": 2,
                "profileName": "Admin",
                "isActive": True,
                "lastUpdated": "2026-05-28T12:00:00Z",
            },
            fetched_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        )

        self.assertEqual(row["agent_id"], 42)
        self.assertEqual(row["email"], "casey.nguyen@northstar.example.com")
        self.assertEqual(row["display_name"], "Casey Nguyen")


class CxoneKpiSessionTest(unittest.TestCase):
    def test_session_uses_access_key_auth_and_discovery(self):
        access_key_value = "placeholder"
        fake_session = mock.Mock()
        fake_session.post.return_value.json.return_value = {"access_token": "token"}
        fake_session.post.return_value.raise_for_status.return_value = None
        fake_session.get.return_value.json.return_value = {"api_endpoint": "https://api-na1.niceincontact.com"}
        fake_session.get.return_value.raise_for_status.return_value = None

        with (
            mock.patch("app.services.cxone_kpi.requests.Session", return_value=fake_session),
            mock.patch("app.services.cxone_kpi.settings.cxone_region", "na1"),
            mock.patch("app.services.cxone_kpi.settings.cxone_access_key_id", "key"),
            mock.patch("app.services.cxone_kpi.settings.cxone_access_key_secret", access_key_value),
        ):
            session = cxone_kpi._CxoneSession()

        self.assertEqual(session.api_endpoint, "https://api-na1.niceincontact.com")
        fake_session.post.assert_called_once()
        auth_kwargs = fake_session.post.call_args.kwargs
        self.assertEqual(auth_kwargs["json"], {"accessKeyId": "key", "accessKeySecret": access_key_value})
        self.assertNotIn("secret", str(session.__dict__))


class CxoneKpiTableTest(unittest.TestCase):
    def test_cached_status_checks_bigquery_without_live_credentials(self):
        client = mock.Mock()
        fresh = {
            "run_id": "run-1",
            "started_at": "2026-06-06T12:20:00+00:00",
            "completed_at": "2026-06-06T12:21:00+00:00",
            "fresh_after": "2026-06-06T12:00:00+00:00",
            "contact_count": 17,
            "agent_count": 5,
            "api_total_records": 17,
            "is_fresh": True,
        }

        with (
            mock.patch("app.services.cxone_kpi._configured", return_value=False),
            mock.patch("app.services.cxone_kpi._latest_successful_run_status", return_value=fresh) as latest_run,
        ):
            status = cxone_kpi.get_cached_cxone_day_status(client, report_date=date(2026, 6, 5))

        self.assertEqual(status["status"], "cached")
        self.assertTrue(status["available"])
        self.assertTrue(status["is_fresh"])
        self.assertEqual(status["contact_count"], 17)
        latest_run.assert_called_once_with(client, date(2026, 6, 5))

    def test_freshness_cutoff_is_next_day_8am_business_time(self):
        cutoff = cxone_kpi._fresh_cache_cutoff_utc(date(2026, 6, 5))

        self.assertEqual(cutoff.isoformat(), "2026-06-06T12:00:00+00:00")
        self.assertFalse(
            cxone_kpi._completed_after_fresh_cutoff(
                date(2026, 6, 5),
                cutoff - timedelta(seconds=1),
            )
        )
        self.assertTrue(cxone_kpi._completed_after_fresh_cutoff(date(2026, 6, 5), cutoff))

    def test_cached_only_returns_stale_warning_without_live_fetch(self):
        client = mock.Mock()
        stale = {
            "run_id": "run-stale",
            "started_at": "2026-06-07T01:27:32+00:00",
            "completed_at": "2026-06-07T01:27:45+00:00",
            "fresh_after": "2026-06-07T12:00:00+00:00",
            "contact_count": 177,
            "agent_count": 20,
            "api_total_records": 177,
            "is_fresh": False,
        }

        with mock.patch("app.services.cxone_kpi._latest_successful_run_status", return_value=stale):
            status = cxone_kpi.get_cached_cxone_day_status(client, report_date=date(2026, 6, 6))

        self.assertEqual(status["status"], "stale")
        self.assertFalse(status["available"])
        self.assertFalse(status["is_fresh"])
        self.assertIn("before the final daily cutoff", status["warning"])

    def test_ensure_loaded_uses_fresh_cache_without_fetch(self):
        client = mock.Mock()
        fresh = {
            "run_id": "run-fresh",
            "started_at": "2026-06-06T12:20:00+00:00",
            "completed_at": "2026-06-06T12:21:00+00:00",
            "fresh_after": "2026-06-06T12:00:00+00:00",
            "contact_count": 278,
            "agent_count": 20,
            "api_total_records": 278,
            "is_fresh": True,
        }

        with (
            mock.patch("app.services.cxone_kpi._configured", return_value=True),
            mock.patch("app.services.cxone_kpi.ensure_tables") as ensure_tables,
            mock.patch("app.services.cxone_kpi._latest_successful_run_status", return_value=fresh),
            mock.patch("app.services.cxone_kpi._CxoneSession") as session_cls,
        ):
            status = cxone_kpi.ensure_cxone_day_loaded(
                client,
                report_date=date(2026, 6, 5),
                start_at=datetime(2026, 6, 5, 4, tzinfo=timezone.utc),
                end_at=datetime(2026, 6, 6, 4, tzinfo=timezone.utc),
            )

        self.assertEqual(status["status"], "cached")
        self.assertTrue(status["is_fresh"])
        ensure_tables.assert_called_once_with(client)
        session_cls.assert_not_called()

    def test_ensure_loaded_refreshes_stale_successful_run(self):
        client = mock.Mock()
        stale = {
            "run_id": "run-stale",
            "started_at": "2026-06-07T01:27:32+00:00",
            "completed_at": "2026-06-07T01:27:45+00:00",
            "fresh_after": "2026-06-07T12:00:00+00:00",
            "contact_count": 177,
            "agent_count": 20,
            "api_total_records": 177,
            "is_fresh": False,
        }
        session = mock.Mock()
        session.fetch_completed_contacts.return_value = {
            "contacts": [{"contactId": 1, "agentId": 42, "mediaTypeId": 4, "agentSeconds": 100}],
            "total_records": 1,
        }
        session.fetch_agents.return_value = [{"agentId": 42, "emailAddress": "casey.nguyen@northstar.example.com"}]

        with (
            mock.patch("app.services.cxone_kpi._configured", return_value=True),
            mock.patch("app.services.cxone_kpi.ensure_tables"),
            mock.patch("app.services.cxone_kpi._latest_successful_run_status", return_value=stale),
            mock.patch("app.services.cxone_kpi._CxoneSession", return_value=session),
            mock.patch("app.services.cxone_kpi._replace_contacts") as replace_contacts,
            mock.patch("app.services.cxone_kpi._replace_agents") as replace_agents,
            mock.patch("app.services.cxone_kpi._record_run") as record_run,
        ):
            status = cxone_kpi.ensure_cxone_day_loaded(
                client,
                report_date=date(2026, 6, 6),
                start_at=datetime(2026, 6, 6, 4, tzinfo=timezone.utc),
                end_at=datetime(2026, 6, 7, 4, tzinfo=timezone.utc),
            )

        self.assertEqual(status["status"], "loaded")
        self.assertTrue(status["available"])
        replace_contacts.assert_called_once()
        replace_agents.assert_called_once()
        record_run.assert_called_once()

    def test_ensure_tables_tolerates_create_conflict(self):
        existing_table = mock.Mock()
        existing_table.schema = list(cxone_kpi.CONTACTS_SCHEMA)
        created_table = mock.Mock()
        created_table.schema = list(cxone_kpi.AGENTS_SCHEMA)
        runs_table = mock.Mock()
        runs_table.schema = list(cxone_kpi.RUNS_SCHEMA)

        client = mock.Mock()
        client.get_table.side_effect = [
            google_exceptions.NotFound("missing"),
            existing_table,
            google_exceptions.NotFound("missing"),
            runs_table,
        ]
        client.create_table.side_effect = [
            google_exceptions.Conflict("already exists"),
            created_table,
        ]

        cxone_kpi.ensure_tables(client)

        self.assertEqual(client.create_table.call_count, 2)
        self.assertEqual(client.update_table.call_count, 0)

    def test_query_rows_raises_clear_timeout_error(self):
        client = mock.Mock()
        job = mock.Mock()
        job.result.side_effect = FuturesTimeoutError()
        client.query.return_value = job

        with self.assertRaises(cxone_kpi.CxoneKpiError) as ctx:
            cxone_kpi._query_rows(client, "SELECT 1", context="checking cached CXone KPI status")

        self.assertIn("BigQuery timed out", str(ctx.exception))
        self.assertIn("checking cached CXone KPI status", str(ctx.exception))
        client.query.assert_called_once()
        self.assertEqual(client.query.call_args.kwargs["retry"], None)
        self.assertEqual(client.query.call_args.kwargs["job_retry"], None)
        self.assertEqual(
            client.query.call_args.kwargs["timeout"],
            cxone_kpi.BIGQUERY_JOB_TIMEOUT_SECONDS,
        )
        job.result.assert_called_once_with(timeout=cxone_kpi.BIGQUERY_JOB_TIMEOUT_SECONDS)

    def test_query_rows_wraps_query_start_failure(self):
        client = mock.Mock()
        client.query.side_effect = RuntimeError("network blocked")

        with self.assertRaises(cxone_kpi.CxoneKpiError) as ctx:
            cxone_kpi._query_rows(client, "SELECT 1", context="checking cached CXone KPI status")

        self.assertIn("BigQuery request failed", str(ctx.exception))
        self.assertIn("RuntimeError: network blocked", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
