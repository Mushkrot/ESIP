"""Tests for the hybrid My Unsolved Tickets sidebar service."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app.models.profile import MyUnsolvedTicketsResponse
from app.models.ticket import TicketListItem
from app.services import firestore
from app.services import my_unsolved_tickets as service


def _ticket(ticket_id: int = 950035) -> TicketListItem:
    return TicketListItem(
        id=ticket_id,
        subject="Slow internet",
        status="open",
        priority="normal",
        updated_at=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
        assignee_name="Samir Patel",
        issue_category="network",
        store_id="2008",
        org_name="Piazza Kitchen 2008",
    )


def _cache(*, expires_delta: timedelta) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "tickets": [_ticket().model_dump(mode="python")],
        "fetched_at": now - timedelta(minutes=5),
        "expires_at": now + expires_delta,
    }


class _FakeRow:
    id = 950039
    subject = "Store Operations Portal"
    status = "pending"
    priority = "high"
    updated_at = datetime(2026, 6, 9, 13, 0, tzinfo=timezone.utc)
    assignee_name = "Samir Patel"
    issue_category = "pos_terminal"
    store_id = "2002"
    org_name = "Metro Burger 2002"


class _FakeJob:
    def result(self, timeout=None):
        return [_FakeRow()]


class _FakeBqClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None, **kwargs):
        self.calls.append((sql, job_config, kwargs))
        return _FakeJob()


class _FakeFirestoreNode:
    def __init__(self, path=()):
        self.path = path

    def collection(self, name):
        return _FakeFirestoreNode((*self.path, "collection", name))

    def document(self, name):
        return _FakeFirestoreNode((*self.path, "document", name))


class MyUnsolvedTicketsServiceTest(unittest.TestCase):
    def test_fresh_cache_returns_live_cache_without_bq_or_refresh(self):
        enqueue = mock.Mock()
        with (
            mock.patch.object(service.firestore, "get_my_unsolved_tickets_cache", return_value=_cache(expires_delta=timedelta(minutes=20))),
            mock.patch.object(service, "query_my_unsolved_tickets_from_bq") as query_bq,
            mock.patch.object(service.firestore, "try_acquire_my_unsolved_tickets_refresh_lock") as lock,
        ):
            resp = service.get_my_unsolved_tickets(
                uid="u1",
                email="Samir.Patel@northstar.example.com",
                enqueue_refresh=enqueue,
            )

        self.assertEqual(resp.source, "zendesk_live_cache")
        self.assertEqual(resp.refresh_status, "fresh")
        self.assertEqual(resp.count, 1)
        query_bq.assert_not_called()
        lock.assert_not_called()
        enqueue.assert_not_called()

    def test_stale_cache_returns_bq_snapshot_and_queues_refresh(self):
        enqueue = mock.Mock()
        with (
            mock.patch.object(service.firestore, "get_my_unsolved_tickets_cache", return_value=_cache(expires_delta=timedelta(minutes=-1))),
            mock.patch.object(service, "query_my_unsolved_tickets_from_bq", return_value=[_ticket(950039)]) as query_bq,
            mock.patch.object(service.firestore, "try_acquire_my_unsolved_tickets_refresh_lock", return_value=True),
        ):
            resp = service.get_my_unsolved_tickets(
                uid="u1",
                email="Samir.Patel@northstar.example.com",
                enqueue_refresh=enqueue,
            )

        self.assertEqual(resp.source, "bq_snapshot")
        self.assertEqual(resp.refresh_status, "queued")
        self.assertEqual(resp.items[0].id, 950039)
        query_bq.assert_called_once_with("samir.patel@northstar.example.com", limit=20)
        enqueue.assert_called_once_with("u1", "samir.patel@northstar.example.com", 20)

    def test_refresh_lock_reports_refreshing_without_queueing_duplicate(self):
        enqueue = mock.Mock()
        with (
            mock.patch.object(service.firestore, "get_my_unsolved_tickets_cache", return_value=None),
            mock.patch.object(service, "query_my_unsolved_tickets_from_bq", return_value=[]),
            mock.patch.object(service.firestore, "try_acquire_my_unsolved_tickets_refresh_lock", return_value=False),
        ):
            resp = service.get_my_unsolved_tickets(
                uid="u1",
                email="samir.patel@northstar.example.com",
                enqueue_refresh=enqueue,
            )

        self.assertEqual(resp.refresh_status, "refreshing")
        enqueue.assert_not_called()

    def test_manual_refresh_success_writes_live_cache(self):
        live_ticket = _ticket(950040)
        cache = {
            "tickets": [live_ticket.model_dump(mode="python")],
            "fetched_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        }
        with (
            mock.patch.object(service.zendesk, "get_unsolved_tickets_assigned_to_email", return_value=[live_ticket]) as live,
            mock.patch.object(service.firestore, "set_my_unsolved_tickets_cache", return_value=cache) as set_cache,
        ):
            resp = service.refresh_my_unsolved_tickets(
                uid="u1",
                email="Samir.Patel@northstar.example.com",
            )

        self.assertEqual(resp.source, "zendesk_live_cache")
        self.assertEqual(resp.refresh_status, "fresh")
        live.assert_called_once_with("samir.patel@northstar.example.com", limit=20)
        set_cache.assert_called_once()

    def test_manual_refresh_failure_falls_back_to_bq_snapshot(self):
        with (
            mock.patch.object(service, "refresh_my_unsolved_tickets_cache", side_effect=RuntimeError("zendesk down")),
            mock.patch.object(service.firestore, "get_my_unsolved_tickets_cache", return_value=None),
            mock.patch.object(service, "query_my_unsolved_tickets_from_bq", return_value=[_ticket(950041)]),
        ):
            resp = service.refresh_my_unsolved_tickets(
                uid="u1",
                email="Samir.Patel@northstar.example.com",
            )

        self.assertEqual(resp.source, "bq_snapshot")
        self.assertEqual(resp.refresh_status, "failed")
        self.assertEqual(resp.items[0].id, 950041)

    def test_bq_query_filters_by_current_user_and_unsolved_statuses(self):
        client = _FakeBqClient()
        with mock.patch.object(service, "_get_client", return_value=client):
            items = service.query_my_unsolved_tickets_from_bq("Samir.Patel@northstar.example.com", limit=7)

        self.assertEqual(items[0].id, 950039)
        sql, job_config, kwargs = client.calls[0]
        self.assertIn("LOWER(TRIM(COALESCE(assignee_email, ''))) = @email", sql)
        self.assertIn("IN UNNEST(@statuses)", sql)
        params = {p.name: p for p in job_config.query_parameters}
        self.assertEqual(params["email"].value, "samir.patel@northstar.example.com")
        self.assertEqual(params["statuses"].values, list(service.UNSOLVED_STATUSES))
        self.assertEqual(params["limit"].value, 7)
        self.assertIsNone(kwargs["retry"])
        self.assertIsNone(kwargs["job_retry"])

    def test_firestore_cache_key_is_scoped_by_uid(self):
        with mock.patch.object(firestore, "get_db", return_value=_FakeFirestoreNode()):
            first_path = firestore._my_unsolved_tickets_doc_ref("uid-one").path
            second_path = firestore._my_unsolved_tickets_doc_ref("uid-two").path

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(
            first_path,
            (
                "collection",
                "cache",
                "document",
                "my_unsolved_tickets",
                "collection",
                "uid-one",
                "document",
                "data",
            ),
        )
        self.assertIn("uid-two", second_path)


class MyUnsolvedTicketsRouterTest(unittest.TestCase):
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
                    "uid": "uid-current",
                    "email": "alex.morgan@northstar.example.com",
                    "name": "Current User",
                },
            ),
            mock.patch("app.dependencies.firestore.is_maintenance_blocked", return_value=False),
            mock.patch("app.dependencies.firestore.is_user_allowed", return_value=True),
            mock.patch("app.dependencies.firestore.get_user_role", return_value=firestore.ROLE_USER),
        ):
            yield

    def test_route_scopes_to_authenticated_user(self):
        payload = MyUnsolvedTicketsResponse(
            items=[],
            count=0,
            source="bq_snapshot",
            fetched_at=datetime.now(timezone.utc),
            expires_at=None,
            refresh_status="queued",
        )
        with (
            self._auth_patches(),
            mock.patch("app.routers.profile.my_unsolved_tickets.get_my_unsolved_tickets", return_value=payload) as get_items,
        ):
            resp = self.client.get(
                "/api/profile/my-unsolved-tickets?limit=12",
                headers={"Authorization": "Bearer fake-token"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["refresh_status"], "queued")
        kwargs = get_items.call_args.kwargs
        self.assertEqual(kwargs["uid"], "uid-current")
        self.assertEqual(kwargs["email"], "alex.morgan@northstar.example.com")
        self.assertEqual(kwargs["limit"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
