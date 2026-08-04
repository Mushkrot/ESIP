#!/usr/bin/env python3
"""Combined maintenance backfill for Zendesk ticket metadata.

Default is dry-run: fetches Zendesk ticket/comment metadata and reports rows
that would be updated, but does not write to BigQuery. Use --apply only for an
approved maintenance window.

This is metadata-only:
- tickets: priority/type/form + derived resolution SLA fields
- ticket_comments: author_id/email/role/is_agent/via_channel
- optional ticket audit events: assignee/status/group changes for KPI history

It does not touch ticket text, OCR, embeddings, Vertex indexes, GCS, or sync
cursor state.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402
from app.services import bigquery as bq_service  # noqa: E402
from app.services.sla import build_sla_fields  # noqa: E402
from app.services import zendesk_ticket_audit_sample  # noqa: E402


MAX_RETRIES = 3
SHOW_MANY_USERS_BATCH = 100
SLA_AUTHOR_PHASE = "sla_author"
AUDITS_PHASE = "audits"
ALL_PHASES = {SLA_AUTHOR_PHASE, AUDITS_PHASE}

COMMENT_AUTHOR_COLUMNS = {"author_id", "author_email", "author_role", "author_is_agent", "via_channel"}
SLA_COLUMNS = {
    "priority",
    "ticket_type",
    "ticket_form_id",
    "ticket_form_name",
    "sla_policy_name",
    "resolution_sla_target_hours",
    "resolution_sla_elapsed_hours",
    "resolution_sla_breached",
    "resolution_sla_status",
}

AUTHOR_TEMP_SCHEMA = [
    bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("comment_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("author_id", "INT64"),
    bigquery.SchemaField("author_name", "STRING"),
    bigquery.SchemaField("author_email", "STRING"),
    bigquery.SchemaField("author_role", "STRING"),
    bigquery.SchemaField("author_is_agent", "BOOL"),
    bigquery.SchemaField("via_channel", "STRING"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP"),
]

SLA_TEMP_SCHEMA = [
    bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("priority", "STRING"),
    bigquery.SchemaField("ticket_type", "STRING"),
    bigquery.SchemaField("ticket_form_id", "INT64"),
    bigquery.SchemaField("ticket_form_name", "STRING"),
    bigquery.SchemaField("sla_policy_name", "STRING"),
    bigquery.SchemaField("resolution_sla_target_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_elapsed_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_breached", "BOOL"),
    bigquery.SchemaField("resolution_sla_status", "STRING"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP"),
]

AUDIT_EVENTS_TABLE = "zendesk_ticket_audit_events"
AUDIT_EVENTS_SCHEMA = [
    bigquery.SchemaField("event_key", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("audit_id", "INT64"),
    bigquery.SchemaField("event_id", "INT64"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("author_id", "INT64"),
    bigquery.SchemaField("via_channel", "STRING"),
    bigquery.SchemaField("field_name", "STRING"),
    bigquery.SchemaField("previous_value", "STRING"),
    bigquery.SchemaField("value", "STRING"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP"),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _zd_get(path_or_url: str, params: dict | None = None) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else f"{settings.zendesk_base_url}{path_or_url}"
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(url, params=params, auth=settings.zendesk_auth, timeout=30)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            print(json.dumps({"phase": "rate_limit", "wait_sec": wait, "url": url}), flush=True)
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Zendesk request failed after {MAX_RETRIES} retries: {url}")


def _table_columns(table_name: str) -> set[str]:
    client = bq_service._get_client()
    table = client.get_table(f"{settings.google_cloud_project_id}.esip_reference.{table_name}")
    return {field.name for field in table.schema}


def ensure_audit_table(*, create_if_missing: bool) -> None:
    client = bq_service._get_client()
    table_id = f"{settings.google_cloud_project_id}.esip_reference.{AUDIT_EVENTS_TABLE}"
    try:
        table = client.get_table(table_id)
    except Exception as exc:
        if not create_if_missing:
            raise SystemExit(
                f"Missing BQ table {table_id}. Re-run with --create-audit-table "
                "before applying audit-event writes."
            ) from exc
        table = bigquery.Table(table_id, schema=AUDIT_EVENTS_SCHEMA)
        table.time_partitioning = bigquery.TimePartitioning(field="created_at")
        table.clustering_fields = ["ticket_id", "field_name"]
        client.create_table(table)
        print(json.dumps({"phase": "created_audit_table", "table": table_id}), flush=True)
        return

    existing = {field.name for field in table.schema}
    required = {field.name for field in AUDIT_EVENTS_SCHEMA}
    missing = required - existing
    if missing:
        raise SystemExit(f"{table_id} missing columns: {sorted(missing)}")


def require_columns(*, include_audits: bool, apply: bool, create_audit_table: bool) -> None:
    missing_comment = COMMENT_AUTHOR_COLUMNS - _table_columns("ticket_comments")
    missing_sla = SLA_COLUMNS - _table_columns("tickets")
    missing = []
    if missing_comment:
        missing.append(f"ticket_comments missing {sorted(missing_comment)}")
    if missing_sla:
        missing.append(f"tickets missing {sorted(missing_sla)}")
    if missing:
        raise SystemExit("; ".join(missing))
    if include_audits and (apply or create_audit_table):
        ensure_audit_table(create_if_missing=create_audit_table)


def _ids_from_csv(value: str | None) -> list[int]:
    if not value:
        return []
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def fetch_sla_author_candidate_ticket_ids(
    *,
    scope: str,
    limit: int | None,
    since_days: int | None,
    updated_start: str | None,
    updated_end: str | None,
) -> tuple[list[int], int]:
    client = bq_service._get_client()
    status_filter = (
        "LOWER(COALESCE(t.status, '')) IN ('solved', 'closed')"
        if scope == "closed"
        else "LOWER(COALESCE(t.status, '')) NOT IN ('solved', 'closed')"
        if scope == "unresolved"
        else "TRUE"
    )
    date_filters = []
    params: list[bigquery.ScalarQueryParameter] = []
    if since_days:
        since = _utc_now() - timedelta(days=since_days)
        date_filters.append("t.updated_at >= @since")
        params.append(bigquery.ScalarQueryParameter("since", "TIMESTAMP", since))
    if updated_start:
        date_filters.append("DATE(t.updated_at, 'America/New_York') >= @updated_start")
        params.append(bigquery.ScalarQueryParameter("updated_start", "DATE", updated_start))
    if updated_end:
        date_filters.append("DATE(t.updated_at, 'America/New_York') < @updated_end")
        params.append(bigquery.ScalarQueryParameter("updated_end", "DATE", updated_end))
    date_sql = " AND ".join(date_filters) if date_filters else "TRUE"
    base_sql = f"""
      FROM `{settings.google_cloud_project_id}.esip_reference.tickets` AS t
      LEFT JOIN `{settings.google_cloud_project_id}.esip_reference.ticket_comments` AS c
        ON c.ticket_id = t.id
      WHERE {status_filter}
        AND {date_sql}
        AND (
          t.resolution_sla_status IS NULL
          OR (c.author_id IS NULL AND c.author_role IS NULL AND c.via_channel IS NULL)
        )
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
    total = int(next(iter(client.query(f"SELECT COUNT(DISTINCT t.id) AS n {base_sql}", job_config=job_config).result())).n or 0)
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    ids_sql = f"""
      SELECT t.id, MAX(t.updated_at) AS last_updated_at
      {base_sql}
      GROUP BY t.id
      ORDER BY last_updated_at DESC, t.id DESC
      {limit_sql}
    """
    ids = [int(row.id) for row in client.query(ids_sql, job_config=job_config).result()]
    return ids, total


def _date_bound_param(name: str, value: str | None) -> bigquery.ScalarQueryParameter | None:
    if not value:
        return None
    return bigquery.ScalarQueryParameter(name, "DATE", value)


def fetch_audit_candidate_ticket_ids(
    *,
    audit_start: str | None,
    audit_end: str | None,
    audit_since_days: int | None,
    limit: int | None,
) -> tuple[list[int], int]:
    """Select tickets whose movement can affect KPI history for the audit window."""
    client = bq_service._get_client()
    filters = []
    params: list[bigquery.ScalarQueryParameter] = []
    if audit_since_days:
        since = _utc_now() - timedelta(days=audit_since_days)
        filters.append("(t.created_at >= @since OR t.updated_at >= @since OR t.solved_at >= @since)")
        params.append(bigquery.ScalarQueryParameter("since", "TIMESTAMP", since))
    start_param = _date_bound_param("audit_start", audit_start)
    end_param = _date_bound_param("audit_end", audit_end)
    if start_param:
        params.append(start_param)
    if end_param:
        params.append(end_param)
    if start_param or end_param:
        field_windows = []
        for field in ("created_at", "updated_at", "solved_at"):
            parts = []
            if start_param:
                parts.append(f"DATE(t.{field}, 'America/New_York') >= @audit_start")
            if end_param:
                parts.append(f"DATE(t.{field}, 'America/New_York') < @audit_end")
            field_windows.append("(" + " AND ".join(parts) + ")")
        filters.append("(" + " OR ".join(field_windows) + ")")
    if not filters:
        raise SystemExit("Audit phase requires --audit-start/--audit-end or --audit-since-days.")
    where = " AND ".join(filters)
    base_sql = f"""
      FROM `{settings.google_cloud_project_id}.esip_reference.tickets` AS t
      WHERE {where}
        AND (
          t.created_at IS NOT NULL
          OR t.updated_at IS NOT NULL
          OR t.solved_at IS NOT NULL
        )
    """
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    total = int(next(iter(client.query(f"SELECT COUNT(DISTINCT t.id) AS n {base_sql}", job_config=job_config).result())).n or 0)
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    ids_sql = f"""
      SELECT t.id, MAX(t.updated_at) AS last_updated_at
      {base_sql}
      GROUP BY t.id
      ORDER BY last_updated_at DESC, t.id DESC
      {limit_sql}
    """
    ids = [int(row.id) for row in client.query(ids_sql, job_config=job_config).result()]
    return ids, total


def build_phase_candidates(
    *,
    include_sla_author: bool,
    include_audits: bool,
    scope: str,
    sla_author_limit: int | None,
    audit_limit: int | None,
    since_days: int | None,
    updated_start: str | None,
    updated_end: str | None,
    audit_start: str | None,
    audit_end: str | None,
    audit_since_days: int | None,
    explicit_ids: list[int],
) -> tuple[dict[int, set[str]], dict[str, int]]:
    candidates: dict[int, set[str]] = {}
    totals = {SLA_AUTHOR_PHASE: 0, AUDITS_PHASE: 0}

    def add(ids: list[int], phase: str) -> None:
        for ticket_id in ids:
            candidates.setdefault(int(ticket_id), set()).add(phase)

    if explicit_ids:
        phases = set()
        if include_sla_author:
            phases.add(SLA_AUTHOR_PHASE)
            totals[SLA_AUTHOR_PHASE] = len(explicit_ids)
        if include_audits:
            phases.add(AUDITS_PHASE)
            totals[AUDITS_PHASE] = len(explicit_ids)
        for ticket_id in explicit_ids:
            candidates[int(ticket_id)] = set(phases)
        return candidates, totals

    if include_sla_author:
        ids, total = fetch_sla_author_candidate_ticket_ids(
            scope=scope,
            limit=sla_author_limit,
            since_days=since_days,
            updated_start=updated_start,
            updated_end=updated_end,
        )
        totals[SLA_AUTHOR_PHASE] = total
        add(ids, SLA_AUTHOR_PHASE)
    if include_audits:
        ids, total = fetch_audit_candidate_ticket_ids(
            audit_start=audit_start,
            audit_end=audit_end,
            audit_since_days=audit_since_days,
            limit=audit_limit,
        )
        totals[AUDITS_PHASE] = total
        add(ids, AUDITS_PHASE)
    return candidates, totals


def fetch_ticket(ticket_id: int) -> dict[str, Any]:
    return _zd_get(f"/tickets/{ticket_id}.json").get("ticket") or {}


def fetch_ticket_comments(ticket_id: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    url: str | None = f"/tickets/{ticket_id}/comments.json"
    params: dict | None = {"include": "attachments"}
    while url:
        data = _zd_get(url, params=params)
        comments.extend(data.get("comments", []))
        url = data.get("next_page")
        params = None
    return comments


def fetch_ticket_audit_events(ticket_id: int, *, max_pages: int) -> tuple[list[dict[str, Any]], int]:
    audits: list[dict[str, Any]] = []
    url: str | None = f"/tickets/{ticket_id}/audits.json"
    params: dict | None = {"per_page": 100}
    pages = 0
    while url and pages < max(1, int(max_pages)):
        data = _zd_get(url, params=params)
        audits.extend(data.get("audits") or [])
        url = data.get("next_page")
        params = None
        pages += 1
    return zendesk_ticket_audit_sample.extract_change_events(ticket_id, audits), pages


def fetch_ticket_form_name(form_id: int | None, cache: dict[int, str | None]) -> str | None:
    if not form_id:
        return None
    if form_id in cache:
        return cache[form_id]
    try:
        form = _zd_get(f"/ticket_forms/{form_id}.json").get("ticket_form") or {}
        cache[form_id] = form.get("display_name") or form.get("name")
    except Exception:
        cache[form_id] = None
    return cache[form_id]


def fetch_users(author_ids: set[int]) -> dict[int, dict[str, Any]]:
    users: dict[int, dict[str, Any]] = {}
    ordered = sorted(author_ids)
    for index in range(0, len(ordered), SHOW_MANY_USERS_BATCH):
        batch = ordered[index:index + SHOW_MANY_USERS_BATCH]
        if not batch:
            continue
        data = _zd_get("/users/show_many.json", params={"ids": ",".join(str(value) for value in batch)})
        for user in data.get("users", []):
            user_id = user.get("id")
            if user_id is not None:
                users[int(user_id)] = user
        time.sleep(0.1)
    return users


def _zendesk_user_is_agent(user: dict[str, Any] | None) -> bool | None:
    if not user:
        return None
    role = str(user.get("role") or "").lower()
    if role in {"agent", "admin"}:
        return True
    if role == "end-user":
        return False
    return None


def _comment_author_name(comment: dict[str, Any], user: dict[str, Any] | None = None) -> str | None:
    return (
        comment.get("author_name")
        or (user or {}).get("name")
        or comment.get("via", {}).get("source", {}).get("from", {}).get("name")
    )


def build_author_rows(ticket_comments: dict[int, list[dict[str, Any]]], users: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    fetched_at = _utc_now().isoformat()
    rows: list[dict[str, Any]] = []
    for ticket_id, comments in ticket_comments.items():
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id is None:
                continue
            author_id = comment.get("author_id")
            author = users.get(int(author_id)) if author_id else None
            rows.append({
                "ticket_id": int(ticket_id),
                "comment_id": int(comment_id),
                "author_id": int(author_id) if author_id else None,
                "author_name": _comment_author_name(comment, author),
                "author_email": (author or {}).get("email"),
                "author_role": (author or {}).get("role"),
                "author_is_agent": _zendesk_user_is_agent(author),
                "via_channel": comment.get("via", {}).get("channel"),
                "fetched_at": fetched_at,
            })
    return rows


def build_sla_row(ticket: dict[str, Any], form_cache: dict[int, str | None]) -> dict[str, Any]:
    ticket_id = int(ticket["id"])
    form_name = fetch_ticket_form_name(ticket.get("ticket_form_id"), form_cache)
    fields = build_sla_fields(ticket, ticket_form_name=form_name)
    return {
        "ticket_id": ticket_id,
        **fields,
        "fetched_at": _utc_now().isoformat(),
    }


def fetch_ticket_bundle(
    ticket_id: int,
    phases: set[str],
    form_cache: dict[int, str | None],
    form_cache_lock: threading.Lock,
    sleep_sec: float,
    audit_max_pages: int,
) -> dict[str, Any]:
    zendesk_requests = 0
    sla_row = None
    comments: list[dict[str, Any]] = []
    audit_events: list[dict[str, Any]] = []
    if SLA_AUTHOR_PHASE in phases:
        ticket = fetch_ticket(ticket_id)
        zendesk_requests += 1
        form_id = ticket.get("ticket_form_id")
        if form_id:
            with form_cache_lock:
                form_name = fetch_ticket_form_name(form_id, form_cache)
        else:
            form_name = None
        fields = build_sla_fields(ticket, ticket_form_name=form_name)
        sla_row = {
            "ticket_id": int(ticket["id"]),
            **fields,
            "fetched_at": _utc_now().isoformat(),
        }
        comments = fetch_ticket_comments(ticket_id)
        zendesk_requests += 1
    if AUDITS_PHASE in phases:
        audit_events, audit_requests = fetch_ticket_audit_events(ticket_id, max_pages=audit_max_pages)
        zendesk_requests += audit_requests
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    return {
        "ticket_id": ticket_id,
        "sla_row": sla_row,
        "comments": comments,
        "audit_events": audit_events,
        "phases": sorted(phases),
        "zendesk_requests": zendesk_requests,
    }


def apply_author_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    client = bq_service._get_client()
    temp_table = (
        f"{settings.google_cloud_project_id}.esip_reference._combined_author_backfill_"
        f"{_utc_now().strftime('%Y%m%d%H%M%S')}"
    )
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=AUTHOR_TEMP_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    merge_sql = f"""
      MERGE `{settings.google_cloud_project_id}.esip_reference.ticket_comments` T
      USING `{temp_table}` S
      ON T.ticket_id = S.ticket_id AND T.comment_id = S.comment_id
      WHEN MATCHED THEN UPDATE SET
        author_id = S.author_id,
        author_name = COALESCE(S.author_name, T.author_name),
        author_email = S.author_email,
        author_role = S.author_role,
        author_is_agent = S.author_is_agent,
        via_channel = S.via_channel
    """
    try:
        client.query(merge_sql).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def apply_sla_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    client = bq_service._get_client()
    temp_table = (
        f"{settings.google_cloud_project_id}.esip_reference._combined_sla_backfill_"
        f"{_utc_now().strftime('%Y%m%d%H%M%S')}"
    )
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=SLA_TEMP_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    merge_sql = f"""
      MERGE `{settings.google_cloud_project_id}.esip_reference.tickets` T
      USING `{temp_table}` S
      ON T.id = S.ticket_id
      WHEN MATCHED THEN UPDATE SET
        priority = S.priority,
        ticket_type = S.ticket_type,
        ticket_form_id = S.ticket_form_id,
        ticket_form_name = S.ticket_form_name,
        sla_policy_name = S.sla_policy_name,
        resolution_sla_target_hours = S.resolution_sla_target_hours,
        resolution_sla_elapsed_hours = S.resolution_sla_elapsed_hours,
        resolution_sla_breached = S.resolution_sla_breached,
        resolution_sla_status = S.resolution_sla_status
    """
    try:
        client.query(merge_sql).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def _stringify_audit_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _audit_event_key(event: dict[str, Any]) -> str:
    raw = [
        event.get("ticket_id"),
        event.get("audit_id"),
        event.get("event_id"),
        event.get("field_name"),
        event.get("created_at"),
        _stringify_audit_value(event.get("previous_value")),
        _stringify_audit_value(event.get("value")),
    ]
    return "|".join("" if value is None else str(value) for value in raw)


def build_audit_rows(events_by_ticket: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    fetched_at = _utc_now().isoformat()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for events in events_by_ticket.values():
        for event in events:
            key = _audit_event_key(event)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "event_key": key,
                "ticket_id": int(event["ticket_id"]),
                "audit_id": int(event["audit_id"]) if event.get("audit_id") is not None else None,
                "event_id": int(event["event_id"]) if event.get("event_id") is not None else None,
                "created_at": event.get("created_at"),
                "author_id": int(event["author_id"]) if event.get("author_id") is not None else None,
                "via_channel": event.get("via_channel"),
                "field_name": event.get("field_name"),
                "previous_value": _stringify_audit_value(event.get("previous_value")),
                "value": _stringify_audit_value(event.get("value")),
                "fetched_at": fetched_at,
            })
    return rows


def apply_audit_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    client = bq_service._get_client()
    temp_table = (
        f"{settings.google_cloud_project_id}.esip_reference._ticket_audit_events_"
        f"{_utc_now().strftime('%Y%m%d%H%M%S')}"
    )
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=AUDIT_EVENTS_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    dest = f"{settings.google_cloud_project_id}.esip_reference.{AUDIT_EVENTS_TABLE}"
    merge_sql = f"""
      MERGE `{dest}` T
      USING `{temp_table}` S
      ON T.event_key = S.event_key
      WHEN MATCHED THEN UPDATE SET
        fetched_at = S.fetched_at
      WHEN NOT MATCHED THEN INSERT (
        event_key, ticket_id, audit_id, event_id, created_at, author_id,
        via_channel, field_name, previous_value, value, fetched_at
      ) VALUES (
        S.event_key, S.ticket_id, S.audit_id, S.event_id, S.created_at, S.author_id,
        S.via_channel, S.field_name, S.previous_value, S.value, S.fetched_at
      )
    """
    try:
        client.query(merge_sql).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def _load_checkpoint(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {"completed_ticket_ids": [], "errors": []}
    return json.loads(path.read_text())


def _save_checkpoint(path: Path | None, checkpoint: dict[str, Any]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))


def _completed_phase_map(checkpoint: dict[str, Any]) -> dict[int, set[str]]:
    phase_map: dict[int, set[str]] = {}
    for ticket_id in checkpoint.get("completed_ticket_ids", []):
        phase_map.setdefault(int(ticket_id), set()).add(SLA_AUTHOR_PHASE)
    for raw_ticket_id, phases in (checkpoint.get("completed_ticket_phases") or {}).items():
        valid = {str(phase) for phase in phases if str(phase) in ALL_PHASES}
        if valid:
            phase_map.setdefault(int(raw_ticket_id), set()).update(valid)
    return phase_map


def _pending_phase_items(
    candidates: dict[int, set[str]],
    completed_phases: dict[int, set[str]],
) -> dict[int, set[str]]:
    pending: dict[int, set[str]] = {}
    for ticket_id, phases in candidates.items():
        missing = set(phases) - completed_phases.get(ticket_id, set())
        if missing:
            pending[int(ticket_id)] = missing
    return pending


def _record_completed_phases(
    checkpoint: dict[str, Any],
    ticket_phases: dict[int, set[str]],
) -> None:
    phase_map = _completed_phase_map(checkpoint)
    for ticket_id, phases in ticket_phases.items():
        phase_map.setdefault(int(ticket_id), set()).update(set(phases))
    checkpoint["completed_ticket_phases"] = {
        str(ticket_id): sorted(phases)
        for ticket_id, phases in sorted(phase_map.items())
    }
    checkpoint["completed_ticket_ids"] = sorted(
        ticket_id
        for ticket_id, phases in phase_map.items()
        if SLA_AUTHOR_PHASE in phases
    )


def _chunks(values: list[int], size: int) -> list[list[int]]:
    size = max(1, int(size or 1))
    return [values[index:index + size] for index in range(0, len(values), size)]


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "rows": len(rows),
        "with_author_id": sum(1 for row in rows if row.get("author_id") is not None),
        "with_author_role": sum(1 for row in rows if row.get("author_role")),
        "with_via_channel": sum(1 for row in rows if row.get("via_channel")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined Zendesk ticket metadata enrichment.")
    parser.add_argument("--scope", choices=["closed", "unresolved", "all"], default="closed")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sla-author-limit", type=int, help="Limit for SLA/comment-author cohort. Defaults to --limit.")
    parser.add_argument("--ticket-ids", help="Comma-separated explicit ticket IDs for a targeted pilot.")
    parser.add_argument("--since-days", type=int)
    parser.add_argument("--updated-start", help="Eastern DATE lower bound, inclusive: YYYY-MM-DD")
    parser.add_argument("--updated-end", help="Eastern DATE upper bound, exclusive: YYYY-MM-DD")
    parser.add_argument("--skip-sla-author", action="store_true", help="Skip SLA/comment-author phase.")
    parser.add_argument("--include-audits", action="store_true", help="Also fetch Zendesk Ticket Audit assignment/status events for KPI history.")
    parser.add_argument("--audit-start", help="Eastern DATE lower bound for KPI audit candidate tickets, inclusive: YYYY-MM-DD")
    parser.add_argument("--audit-end", help="Eastern DATE upper bound for KPI audit candidate tickets, exclusive: YYYY-MM-DD")
    parser.add_argument("--audit-since-days", type=int)
    parser.add_argument("--audit-limit", type=int, help="Limit for KPI audit cohort. Defaults to --limit.")
    parser.add_argument("--audit-max-pages", type=int, default=3, help="Max Zendesk audit pages per ticket.")
    parser.add_argument("--create-audit-table", action="store_true", help="Create the BQ audit events table if missing.")
    parser.add_argument("--chunk-size", type=int, default=25)
    parser.add_argument("--checkpoint-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--workers", type=int, default=1, help="Parallel Zendesk ticket/comment fetch workers per chunk.")
    parser.add_argument("--stop-after-chunks", type=int)
    args = parser.parse_args()

    include_sla_author = not args.skip_sla_author
    include_audits = bool(args.include_audits)
    if not include_sla_author and not include_audits:
        raise SystemExit("Nothing to do: enable SLA/comment-author or --include-audits.")
    require_columns(
        include_audits=include_audits,
        apply=args.apply,
        create_audit_table=args.create_audit_table,
    )
    explicit_ids = _ids_from_csv(args.ticket_ids)
    if explicit_ids and args.limit:
        explicit_ids = explicit_ids[: args.limit]
    candidates, total_candidates_by_phase = build_phase_candidates(
        include_sla_author=include_sla_author,
        include_audits=include_audits,
        scope=args.scope,
        sla_author_limit=args.sla_author_limit if args.sla_author_limit is not None else args.limit,
        audit_limit=args.audit_limit if args.audit_limit is not None else args.limit,
        since_days=args.since_days,
        updated_start=args.updated_start,
        updated_end=args.updated_end,
        audit_start=args.audit_start,
        audit_end=args.audit_end,
        audit_since_days=args.audit_since_days,
        explicit_ids=explicit_ids,
    )
    selected_ids = sorted(candidates)

    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_phases = _completed_phase_map(checkpoint)
    pending_items = _pending_phase_items(candidates, completed_phases)
    pending_ids = sorted(pending_items)
    selected_by_phase = {
        SLA_AUTHOR_PHASE: sum(1 for phases in candidates.values() if SLA_AUTHOR_PHASE in phases),
        AUDITS_PHASE: sum(1 for phases in candidates.values() if AUDITS_PHASE in phases),
    }
    pending_by_phase = {
        SLA_AUTHOR_PHASE: sum(1 for phases in pending_items.values() if SLA_AUTHOR_PHASE in phases),
        AUDITS_PHASE: sum(1 for phases in pending_items.values() if AUDITS_PHASE in phases),
    }
    print(json.dumps({
        "phase": "selected",
        "apply": args.apply,
        "scope": args.scope,
        "explicit_ticket_ids": bool(explicit_ids),
        "total_candidates_by_phase": total_candidates_by_phase,
        "selected_by_phase": selected_by_phase,
        "selected": len(selected_ids),
        "already_completed_ticket_phases": sum(len(phases) for phases in completed_phases.values()),
        "pending_by_phase": pending_by_phase,
        "pending": len(pending_ids),
        "checkpoint_phase_aware": True,
    }), flush=True)
    if not pending_ids:
        return 0

    started = time.monotonic()
    processed_tickets = 0
    total_zendesk_requests = 0
    total_comment_rows = 0
    total_sla_rows = 0
    total_audit_rows = 0
    total_audit_events = 0
    sample_sla_rows: list[dict[str, Any]] = []
    sample_author_rows: list[dict[str, Any]] = []
    sample_audit_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = list(checkpoint.get("errors", []))
    form_cache: dict[int, str | None] = {}
    form_cache_lock = threading.Lock()
    workers = max(1, min(int(args.workers or 1), 8))

    for chunk_index, ticket_ids in enumerate(_chunks(pending_ids, args.chunk_size), 1):
        if args.stop_after_chunks and chunk_index > args.stop_after_chunks:
            break
        chunk_started = time.monotonic()
        ticket_comments: dict[int, list[dict[str, Any]]] = {}
        ticket_audit_events: dict[int, list[dict[str, Any]]] = {}
        author_ids: set[int] = set()
        sla_rows: list[dict[str, Any]] = []
        chunk_errors: list[dict[str, Any]] = []
        completed_this_chunk: dict[int, set[str]] = {}

        if workers == 1:
            for ticket_id in ticket_ids:
                try:
                    bundle = fetch_ticket_bundle(
                        ticket_id,
                        pending_items[ticket_id],
                        form_cache,
                        form_cache_lock,
                        max(0.0, args.sleep),
                        args.audit_max_pages,
                    )
                    total_zendesk_requests += int(bundle["zendesk_requests"])
                    if bundle["sla_row"]:
                        sla_rows.append(bundle["sla_row"])
                    comments = bundle["comments"]
                    audit_events = bundle["audit_events"]
                except Exception as exc:
                    error = {"ticket_id": ticket_id, "error": str(exc)}
                    chunk_errors.append(error)
                    errors.append(error)
                    continue
                if SLA_AUTHOR_PHASE in pending_items[ticket_id]:
                    ticket_comments[ticket_id] = comments
                if AUDITS_PHASE in pending_items[ticket_id]:
                    ticket_audit_events[ticket_id] = audit_events
                completed_this_chunk[ticket_id] = set(pending_items[ticket_id])
                for comment in comments:
                    author_id = comment.get("author_id")
                    if author_id:
                        author_ids.add(int(author_id))
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_ticket = {
                    executor.submit(
                        fetch_ticket_bundle,
                        ticket_id,
                        pending_items[ticket_id],
                        form_cache,
                        form_cache_lock,
                        max(0.0, args.sleep),
                        args.audit_max_pages,
                    ): ticket_id
                    for ticket_id in ticket_ids
                }
                bundles: list[dict[str, Any]] = []
                for future in as_completed(future_to_ticket):
                    ticket_id = future_to_ticket[future]
                    try:
                        bundles.append(future.result())
                    except Exception as exc:
                        error = {"ticket_id": ticket_id, "error": str(exc)}
                        chunk_errors.append(error)
                        errors.append(error)
                for bundle in sorted(bundles, key=lambda item: int(item["ticket_id"])):
                    total_zendesk_requests += int(bundle["zendesk_requests"])
                    if bundle["sla_row"]:
                        sla_rows.append(bundle["sla_row"])
                    comments = bundle["comments"]
                    ticket_id = int(bundle["ticket_id"])
                    if SLA_AUTHOR_PHASE in pending_items[ticket_id]:
                        ticket_comments[ticket_id] = comments
                    if AUDITS_PHASE in pending_items[ticket_id]:
                        ticket_audit_events[ticket_id] = bundle["audit_events"]
                    completed_this_chunk[ticket_id] = set(pending_items[ticket_id])
                    for comment in comments:
                        author_id = comment.get("author_id")
                        if author_id:
                            author_ids.add(int(author_id))

        users = fetch_users(author_ids) if author_ids else {}
        if author_ids:
            total_zendesk_requests += (len(author_ids) + SHOW_MANY_USERS_BATCH - 1) // SHOW_MANY_USERS_BATCH
        author_rows = build_author_rows(ticket_comments, users) if ticket_comments else []
        audit_rows = build_audit_rows(ticket_audit_events) if ticket_audit_events else []

        if not sample_sla_rows:
            sample_sla_rows = sla_rows[:5]
        if not sample_author_rows:
            sample_author_rows = author_rows[:5]
        if not sample_audit_rows:
            sample_audit_rows = audit_rows[:5]
        if args.apply:
            apply_sla_rows(sla_rows)
            apply_author_rows(author_rows)
            if include_audits:
                apply_audit_rows(audit_rows)

        processed_tickets += len(completed_this_chunk)
        total_sla_rows += len(sla_rows)
        total_comment_rows += len(author_rows)
        total_audit_rows += len(audit_rows)
        total_audit_events += sum(len(events) for events in ticket_audit_events.values())
        _record_completed_phases(checkpoint, completed_this_chunk)
        checkpoint.update({
            "errors": errors,
            "last_chunk": chunk_index,
            "last_updated": _utc_now().isoformat(),
            "apply": args.apply,
            "include_sla_author": include_sla_author,
            "include_audits": include_audits,
        })
        _save_checkpoint(args.checkpoint_file, checkpoint)
        print(json.dumps({
            "phase": "chunk",
            "chunk": chunk_index,
            "tickets": len(completed_this_chunk),
            "sla_author_tickets": len(ticket_comments),
            "audit_tickets": len(ticket_audit_events),
            "sla_rows": len(sla_rows),
            "author_rows": len(author_rows),
            "audit_rows": len(audit_rows),
            "audit_events": sum(len(events) for events in ticket_audit_events.values()),
            "unique_authors": len(author_ids),
            "errors": len(chunk_errors),
            "elapsed_sec": round(time.monotonic() - chunk_started, 2),
            "apply": args.apply,
            "workers": workers,
        }), flush=True)

    elapsed = time.monotonic() - started
    print(json.dumps({
        "phase": "summary",
        "apply": args.apply,
        "processed_tickets": processed_tickets,
        "sla_rows": total_sla_rows,
        "author_rows": total_comment_rows,
        "audit_rows": total_audit_rows,
        "audit_events": total_audit_events,
        "author_summary": _summary(sample_author_rows) if sample_author_rows else {},
        "zendesk_requests_estimate": total_zendesk_requests,
        "errors": len(errors),
        "elapsed_sec": round(elapsed, 2),
        "tickets_per_min": round(processed_tickets / max(elapsed / 60, 0.001), 2),
        "workers": workers,
        "sample_sla_rows": sample_sla_rows,
        "sample_author_rows": sample_author_rows,
        "sample_audit_rows": sample_audit_rows,
    }, default=str), flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
