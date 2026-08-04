"""Read-only Zendesk ticket audit sampling for KPI definition validation."""

from __future__ import annotations

import time
from collections import Counter
from datetime import date
from typing import Any

import requests
from google.cloud import bigquery

from app.config import settings
from app.services import service_desk_kpi

INTERESTING_CHANGE_FIELDS = {"assignee_id", "group_id", "status", "custom_status_id"}


def sample_ticket_ids_for_day(*, report_date: str, limit: int = 10) -> list[int]:
    """Read a small candidate ticket sample from BigQuery without writing anything."""
    day = date.fromisoformat(report_date[:10])
    start_at, end_at = service_desk_kpi._day_bounds_utc(day)
    ticket_table = f"`{settings.google_cloud_project_id}.esip_reference.tickets`"
    email_ok = service_desk_kpi._email_domain_condition("assignee_email")
    sql = f"""
    SELECT id
    FROM {ticket_table}
    WHERE (
        (created_at >= @start_at AND created_at < @end_at)
        OR (solved_at >= @start_at AND solved_at < @end_at)
        OR LOWER(COALESCE(status, '')) IN ('open', 'pending', 'hold')
      )
      AND ({email_ok} OR NULLIF(TRIM(IFNULL(assignee_name, '')), '') IS NOT NULL)
    ORDER BY updated_at DESC
    LIMIT @limit
    """
    params = service_desk_kpi._query_params(start_at, end_at)
    params.append(bigquery.ScalarQueryParameter("limit", "INT64", max(1, int(limit))))
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    client = service_desk_kpi._get_client()
    return [int(row["id"]) for row in client.query(sql, job_config=job_config).result()]


def probe_ticket_audits(
    ticket_ids: list[int],
    *,
    max_pages: int = 3,
    resolve_users: bool = False,
    sleep_seconds: float = 0.15,
) -> dict[str, Any]:
    """Fetch a small audit sample from Zendesk and return parsed assignment/status events."""
    session = requests.Session()
    session.auth = settings.zendesk_auth
    user_cache: dict[str, dict[str, Any]] = {}
    tickets: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    for ticket_id in ticket_ids:
        audits = fetch_ticket_audits(session, ticket_id, max_pages=max_pages)
        events = extract_change_events(ticket_id, audits)
        if resolve_users:
            _resolve_event_users(session, events, user_cache=user_cache)
        tickets.append(
            {
                "ticket_id": ticket_id,
                "audit_count": len(audits),
                "event_count": len(events),
                "field_counts": dict(Counter(event["field_name"] for event in events)),
                "events": events,
            }
        )
        all_events.extend(events)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return {
        "read_only": True,
        "ticket_count": len(ticket_ids),
        "event_count": len(all_events),
        "field_counts": dict(Counter(event["field_name"] for event in all_events)),
        "tickets": tickets,
    }


def fetch_ticket_audits(session: requests.Session, ticket_id: int, *, max_pages: int = 3) -> list[dict[str, Any]]:
    """Read Zendesk audits for one ticket. Caller controls small-page limits."""
    url = f"{settings.zendesk_base_url}/tickets/{int(ticket_id)}/audits.json"
    audits: list[dict[str, Any]] = []
    pages = 0
    while url and pages < max(1, int(max_pages)):
        response = session.get(url, params={"per_page": 100} if pages == 0 else None, timeout=30)
        response.raise_for_status()
        data = response.json()
        audits.extend(data.get("audits") or [])
        url = data.get("next_page")
        pages += 1
    return audits


def extract_change_events(ticket_id: int, audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract KPI-relevant Change events from Zendesk audit payloads."""
    out: list[dict[str, Any]] = []
    for audit in audits:
        audit_id = audit.get("id")
        created_at = audit.get("created_at")
        author_id = audit.get("author_id")
        via_channel = ((audit.get("via") or {}).get("channel") or None)
        for event in audit.get("events") or []:
            if event.get("type") != "Change":
                continue
            field_name = str(event.get("field_name") or "")
            if field_name not in INTERESTING_CHANGE_FIELDS:
                continue
            out.append(
                {
                    "ticket_id": int(ticket_id),
                    "audit_id": audit_id,
                    "event_id": event.get("id"),
                    "created_at": created_at,
                    "author_id": author_id,
                    "via_channel": via_channel,
                    "field_name": field_name,
                    "previous_value": event.get("previous_value"),
                    "value": event.get("value"),
                }
            )
    return out


def _resolve_event_users(
    session: requests.Session,
    events: list[dict[str, Any]],
    *,
    user_cache: dict[str, dict[str, Any]],
) -> None:
    for event in events:
        if event["field_name"] != "assignee_id":
            continue
        event["previous_user"] = _zendesk_user_label(session, event.get("previous_value"), user_cache)
        event["value_user"] = _zendesk_user_label(session, event.get("value"), user_cache)


def _zendesk_user_label(
    session: requests.Session,
    user_id: Any,
    user_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if user_id in (None, ""):
        return None
    key = str(user_id)
    if key not in user_cache:
        response = session.get(f"{settings.zendesk_base_url}/users/{key}.json", timeout=30)
        response.raise_for_status()
        user_cache[key] = response.json().get("user") or {}
    user = user_cache.get(key) or {}
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
    }
