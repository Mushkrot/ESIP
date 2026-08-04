"""Durable metadata-only Zendesk backfill for historical KPI/SLA gaps.

This module is intentionally separate from the normal Zendesk sync.  It only
updates metadata fields that were missed by earlier ingestion passes:

- derived SLA/form metadata on existing tickets;
- author/via metadata on existing ticket comments;
- Zendesk Ticket Audit assignment/status events for KPI history.

It never calls Gemini, OCR, embeddings, Vertex import, GCS upload, or sync
cursor writes.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from google.cloud import bigquery

from app.config import settings
from app.services import bigquery as bq_service
from app.services import bq_sync
from app.services.sla import build_sla_fields

logger = logging.getLogger(__name__)

GCP_PROJECT = settings.google_cloud_project_id
BQ_DATASET = "esip_reference"

RUNS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.zendesk_metadata_backfill_runs"
ITEMS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.zendesk_metadata_backfill_items"
EVENTS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.zendesk_metadata_backfill_events"

SLA_AUTHOR_PHASE = "sla_author"
AUDITS_PHASE = "audits"
ALL_PHASES = {SLA_AUTHOR_PHASE, AUDITS_PHASE}
PENDING_STATUSES = {"pending", "error_retryable"}

SHOW_MANY_USERS_BATCH = 100
DEFAULT_MAX_ATTEMPTS = 3

COMMENT_AUTHOR_COLUMNS = {
    "author_id",
    "author_email",
    "author_role",
    "author_is_agent",
    "via_channel",
}
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


@dataclass(frozen=True)
class BackfillSelection:
    ticket_phases: dict[int, set[str]]
    selected_tickets: int
    selected_phases: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _utc_now().isoformat()


def _client() -> bigquery.Client:
    return bq_service._get_client()


def _event(
    run_id: str,
    level: str,
    phase: str,
    message: str,
    *,
    ticket_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    row = {
        "event_id": uuid.uuid4().hex,
        "run_id": run_id,
        "created_at": _ts(),
        "level": level,
        "phase": phase,
        "ticket_id": ticket_id,
        "message": message,
        "details_json": json.dumps(details or {}, default=str),
    }
    errors = _client().insert_rows_json(EVENTS_TABLE, [row])
    if errors:
        logger.warning("metadata backfill event insert failed: %s", errors[:1])


def ensure_tables() -> None:
    """Create durable metadata-backfill tracking tables and audit table."""
    client = _client()

    runs = bigquery.Table(
        RUNS_TABLE,
        schema=[
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
            bigquery.SchemaField("heartbeat_at", "TIMESTAMP"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("active", "BOOL"),
            bigquery.SchemaField("include_sla_author", "BOOL"),
            bigquery.SchemaField("include_audits", "BOOL"),
            bigquery.SchemaField("sla_scope", "STRING"),
            bigquery.SchemaField("note", "STRING"),
        ],
    )
    runs.time_partitioning = bigquery.TimePartitioning(field="created_at")
    runs.clustering_fields = ["run_id", "status"]
    runs.description = "Durable run summary for temporary Zendesk metadata-only backfill."
    client.create_table(runs, exists_ok=True)

    items = bigquery.Table(
        ITEMS_TABLE,
        schema=[
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("phase", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("candidate_updated_at", "TIMESTAMP"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
            bigquery.SchemaField("started_at", "TIMESTAMP"),
            bigquery.SchemaField("completed_at", "TIMESTAMP"),
            bigquery.SchemaField("attempt_count", "INT64"),
            bigquery.SchemaField("last_error", "STRING"),
            bigquery.SchemaField("last_batch_id", "STRING"),
        ],
    )
    items.time_partitioning = bigquery.TimePartitioning(field="created_at")
    items.clustering_fields = ["run_id", "status", "phase", "ticket_id"]
    items.description = "Per-ticket, per-phase state for Zendesk metadata-only backfill."
    client.create_table(items, exists_ok=True)

    events = bigquery.Table(
        EVENTS_TABLE,
        schema=[
            bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("level", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("phase", "STRING"),
            bigquery.SchemaField("ticket_id", "INT64"),
            bigquery.SchemaField("message", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("details_json", "STRING"),
        ],
    )
    events.time_partitioning = bigquery.TimePartitioning(field="created_at")
    events.clustering_fields = ["run_id", "phase", "level"]
    events.description = "Append-only operational events for Zendesk metadata-only backfill."
    client.create_table(events, exists_ok=True)

    bq_sync._ensure_ticket_audit_events_table(client)


def _table_columns(table_name: str) -> set[str]:
    table = _client().get_table(f"{GCP_PROJECT}.{BQ_DATASET}.{table_name}")
    return {field.name for field in table.schema}


def require_destination_columns() -> None:
    missing_comment = COMMENT_AUTHOR_COLUMNS - _table_columns("ticket_comments")
    missing_sla = SLA_COLUMNS - _table_columns("tickets")
    missing = []
    if missing_comment:
        missing.append(f"ticket_comments missing {sorted(missing_comment)}")
    if missing_sla:
        missing.append(f"tickets missing {sorted(missing_sla)}")
    if missing:
        raise RuntimeError("; ".join(missing))


def completed_ticket_ids_from_checkpoint(path: Path, phase: str) -> set[int]:
    """Read a local phase-aware checkpoint and return completed ticket ids."""
    if phase not in ALL_PHASES:
        raise ValueError(f"Unknown phase: {phase}")
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    completed: set[int] = set()
    if phase == SLA_AUTHOR_PHASE:
        completed.update(int(ticket_id) for ticket_id in data.get("completed_ticket_ids", []))
    for raw_ticket_id, phases in (data.get("completed_ticket_phases") or {}).items():
        if phase in {str(value) for value in phases}:
            completed.add(int(raw_ticket_id))
    return completed


def _limit_sql(limit: int | None) -> str:
    return f"LIMIT {int(limit)}" if limit else ""


def _sla_status_filter(scope: str) -> str:
    if scope == "closed":
        return "LOWER(COALESCE(t.status, '')) IN ('solved', 'closed')"
    if scope == "unresolved":
        return "LOWER(COALESCE(t.status, '')) NOT IN ('solved', 'closed')"
    if scope == "all":
        return "TRUE"
    raise ValueError(f"Unsupported SLA scope: {scope}")


def _upsert_run(
    *,
    run_id: str,
    include_sla_author: bool,
    include_audits: bool,
    sla_scope: str,
    note: str,
) -> None:
    _client().query(
        f"""
        MERGE `{RUNS_TABLE}` T
        USING (
          SELECT
            @run_id AS run_id,
            CURRENT_TIMESTAMP() AS now_ts,
            @include_sla_author AS include_sla_author,
            @include_audits AS include_audits,
            @sla_scope AS sla_scope,
            @note AS note
        ) S
        ON T.run_id = S.run_id
        WHEN MATCHED THEN UPDATE SET
          updated_at = S.now_ts,
          heartbeat_at = S.now_ts,
          status = IF(T.status = 'completed', T.status, 'ready'),
          active = TRUE,
          include_sla_author = S.include_sla_author,
          include_audits = S.include_audits,
          sla_scope = S.sla_scope,
          note = S.note
        WHEN NOT MATCHED THEN INSERT (
          run_id, created_at, updated_at, heartbeat_at, status, active,
          include_sla_author, include_audits, sla_scope, note
        ) VALUES (
          S.run_id, S.now_ts, S.now_ts, S.now_ts, 'ready', TRUE,
          S.include_sla_author, S.include_audits, S.sla_scope, S.note
        )
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("include_sla_author", "BOOL", include_sla_author),
            bigquery.ScalarQueryParameter("include_audits", "BOOL", include_audits),
            bigquery.ScalarQueryParameter("sla_scope", "STRING", sla_scope),
            bigquery.ScalarQueryParameter("note", "STRING", note),
        ]),
    ).result(timeout=600)


def create_manifest(
    *,
    run_id: str,
    include_sla_author: bool = True,
    include_audits: bool = True,
    sla_scope: str = "closed",
    sla_limit: int | None = None,
    audit_limit: int | None = None,
    completed_audit_ticket_ids: set[int] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Create or update the durable manifest for the historical metadata job."""
    ensure_tables()
    require_destination_columns()
    _upsert_run(
        run_id=run_id,
        include_sla_author=include_sla_author,
        include_audits=include_audits,
        sla_scope=sla_scope,
        note=note,
    )
    client = _client()

    if include_sla_author:
        status_filter = _sla_status_filter(sla_scope)
        client.query(
            f"""
            MERGE `{ITEMS_TABLE}` T
            USING (
              SELECT
                @run_id AS run_id,
                t.id AS ticket_id,
                @phase AS phase,
                COALESCE(MAX(t.updated_at), MAX(t.solved_at), MAX(t.created_at)) AS candidate_updated_at,
                CURRENT_TIMESTAMP() AS now_ts
              FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` AS t
              LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments` AS c
                ON c.ticket_id = t.id
              WHERE {status_filter}
                AND (
                  t.resolution_sla_status IS NULL
                  OR (c.author_id IS NULL AND c.author_role IS NULL AND c.via_channel IS NULL)
                )
              GROUP BY t.id
              ORDER BY candidate_updated_at DESC, t.id DESC
              {_limit_sql(sla_limit)}
            ) S
            ON T.run_id = S.run_id AND T.ticket_id = S.ticket_id AND T.phase = S.phase
            WHEN NOT MATCHED THEN INSERT (
              run_id, ticket_id, phase, status, candidate_updated_at,
              created_at, updated_at, attempt_count
            ) VALUES (
              S.run_id, S.ticket_id, S.phase, 'pending', S.candidate_updated_at,
              S.now_ts, S.now_ts, 0
            )
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter("phase", "STRING", SLA_AUTHOR_PHASE),
            ]),
        ).result(timeout=600)

    if include_audits:
        completed_ids = sorted(completed_audit_ticket_ids or set())
        client.query(
            f"""
            MERGE `{ITEMS_TABLE}` T
            USING (
              SELECT
                @run_id AS run_id,
                t.id AS ticket_id,
                @phase AS phase,
                MAX(COALESCE(t.updated_at, t.solved_at, t.created_at)) AS candidate_updated_at,
                CURRENT_TIMESTAMP() AS now_ts,
                t.id IN UNNEST(@completed_ids) AS already_done
              FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` AS t
              WHERE t.id IS NOT NULL
              GROUP BY t.id
              ORDER BY candidate_updated_at DESC, t.id DESC
              {_limit_sql(audit_limit)}
            ) S
            ON T.run_id = S.run_id AND T.ticket_id = S.ticket_id AND T.phase = S.phase
            WHEN NOT MATCHED THEN INSERT (
              run_id, ticket_id, phase, status, candidate_updated_at,
              created_at, updated_at, completed_at, attempt_count
            ) VALUES (
              S.run_id, S.ticket_id, S.phase,
              IF(S.already_done, 'done', 'pending'),
              S.candidate_updated_at, S.now_ts, S.now_ts,
              IF(S.already_done, S.now_ts, NULL), 0
            )
            WHEN MATCHED AND T.status != 'done' AND S.already_done THEN UPDATE SET
              status = 'done',
              completed_at = S.now_ts,
              updated_at = S.now_ts,
              last_error = NULL
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter("phase", "STRING", AUDITS_PHASE),
                bigquery.ArrayQueryParameter("completed_ids", "INT64", completed_ids),
            ]),
        ).result(timeout=600)

    _event(run_id, "INFO", "manifest", "Manifest created or refreshed", details={
        "include_sla_author": include_sla_author,
        "include_audits": include_audits,
        "sla_scope": sla_scope,
        "sla_limit": sla_limit,
        "audit_limit": audit_limit,
        "imported_completed_audit_ids": len(completed_audit_ticket_ids or set()),
    })
    return status(run_id)


def _set_run_status(run_id: str, status_value: str) -> None:
    _client().query(
        f"""
        UPDATE `{RUNS_TABLE}`
        SET status = @status, updated_at = CURRENT_TIMESTAMP(), heartbeat_at = CURRENT_TIMESTAMP()
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("status", "STRING", status_value),
        ]),
    ).result(timeout=600)


def recover_stale_running(run_id: str, *, stale_hours: int = 8) -> int:
    """Move old interrupted running items back to retryable state."""
    job = _client().query(
        f"""
        UPDATE `{ITEMS_TABLE}`
        SET
          status = 'error_retryable',
          updated_at = CURRENT_TIMESTAMP(),
          last_error = 'Recovered stale running item before next batch'
        WHERE run_id = @run_id
          AND status = 'running'
          AND updated_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @stale_hours HOUR)
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("stale_hours", "INT64", int(stale_hours)),
        ]),
    )
    job.result(timeout=600)
    return int(job.num_dml_affected_rows or 0)


def _select_next_pending(run_id: str, *, limit: int) -> BackfillSelection:
    rows = list(_client().query(
        f"""
        SELECT ticket_id, ARRAY_AGG(phase ORDER BY phase) AS phases
        FROM (
          SELECT ticket_id, phase, MAX(candidate_updated_at) OVER (PARTITION BY ticket_id) AS sort_at
          FROM `{ITEMS_TABLE}`
          WHERE run_id = @run_id
            AND status IN ('pending', 'error_retryable')
            AND COALESCE(attempt_count, 0) < @max_attempts
        )
        GROUP BY ticket_id, sort_at
        ORDER BY sort_at DESC, ticket_id DESC
        LIMIT @limit
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
            bigquery.ScalarQueryParameter("max_attempts", "INT64", DEFAULT_MAX_ATTEMPTS),
        ]),
    ).result())
    ticket_phases = {
        int(row.ticket_id): {str(phase) for phase in row.phases if str(phase) in ALL_PHASES}
        for row in rows
    }
    return BackfillSelection(
        ticket_phases=ticket_phases,
        selected_tickets=len(ticket_phases),
        selected_phases=sum(len(phases) for phases in ticket_phases.values()),
    )


def _phase_payload(ticket_phases: dict[int, set[str]]) -> str:
    rows = [
        {"ticket_id": int(ticket_id), "phase": phase}
        for ticket_id, phases in sorted(ticket_phases.items())
        for phase in sorted(phases)
    ]
    return json.dumps(rows, default=str)


def _update_item_status(
    run_id: str,
    ticket_phases: dict[int, set[str]],
    *,
    status_value: str,
    batch_id: str,
    last_error: str | None = None,
) -> None:
    if not ticket_phases:
        return
    payload = _phase_payload(ticket_phases)
    if status_value not in {"running", "done", "error"}:
        raise ValueError(f"Unsupported item status update: {status_value}")
    status_expr = (
        "IF(COALESCE(T.attempt_count, 0) >= @max_attempts, 'error_terminal', 'error_retryable')"
        if status_value == "error"
        else "@status_value"
    )
    _client().query(
        f"""
        UPDATE `{ITEMS_TABLE}` T
        SET
          status = {status_expr},
          updated_at = CURRENT_TIMESTAMP(),
          started_at = IF(@status_value = 'running', CURRENT_TIMESTAMP(), T.started_at),
          completed_at = IF(@status_value = 'done', CURRENT_TIMESTAMP(), T.completed_at),
          attempt_count = IF(@status_value = 'running', COALESCE(T.attempt_count, 0) + 1, T.attempt_count),
          last_batch_id = @batch_id,
          last_error = IF(@status_value = 'done', NULL, @last_error)
        FROM (
          SELECT
            CAST(JSON_VALUE(raw, '$.ticket_id') AS INT64) AS ticket_id,
            JSON_VALUE(raw, '$.phase') AS phase
          FROM UNNEST(JSON_QUERY_ARRAY(@payload)) AS raw
        ) S
        WHERE T.run_id = @run_id
          AND T.ticket_id = S.ticket_id
          AND T.phase = S.phase
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("payload", "STRING", payload),
            bigquery.ScalarQueryParameter("status_value", "STRING", status_value if status_value != "error" else "error"),
            bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id),
            bigquery.ScalarQueryParameter("last_error", "STRING", (last_error or "")[:1000]),
            bigquery.ScalarQueryParameter("max_attempts", "INT64", DEFAULT_MAX_ATTEMPTS),
        ]),
    ).result(timeout=600)


def _zd_get(path_or_url: str, params: dict | None = None) -> dict[str, Any]:
    url = path_or_url if path_or_url.startswith("http") else f"{settings.zendesk_base_url}{path_or_url}"
    return bq_sync._zd_get(url, params=params)


def fetch_ticket(ticket_id: int) -> dict[str, Any]:
    return _zd_get(f"/tickets/{int(ticket_id)}.json").get("ticket") or {}


def fetch_ticket_comments(ticket_id: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    url: str | None = f"/tickets/{int(ticket_id)}/comments.json"
    params: dict | None = {"include": "attachments"}
    while url:
        data = _zd_get(url, params=params)
        comments.extend(data.get("comments") or [])
        url = data.get("next_page")
        params = None
    return comments


def fetch_ticket_form_name(form_id: int | None, cache: dict[int, str | None]) -> str | None:
    if not form_id:
        return None
    if form_id in cache:
        return cache[form_id]
    try:
        form = _zd_get(f"/ticket_forms/{int(form_id)}.json").get("ticket_form") or {}
        cache[int(form_id)] = form.get("display_name") or form.get("name")
    except Exception:
        cache[int(form_id)] = None
    return cache[int(form_id)]


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


def build_author_rows(
    ticket_comments: dict[int, list[dict[str, Any]]],
    users: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    fetched_at = _ts()
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


def _fetch_ticket_bundle(
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
                form_name = fetch_ticket_form_name(int(form_id), form_cache)
        else:
            form_name = None
        fields = build_sla_fields(ticket, ticket_form_name=form_name)
        sla_row = {
            "ticket_id": int(ticket["id"]),
            **fields,
            "fetched_at": _ts(),
        }
        comments = fetch_ticket_comments(ticket_id)
        zendesk_requests += 1

    if AUDITS_PHASE in phases:
        audit_events, audit_requests = bq_sync._fetch_ticket_audit_events(ticket_id, max_pages=audit_max_pages)
        zendesk_requests += audit_requests

    if sleep_sec > 0:
        time.sleep(sleep_sec)

    return {
        "ticket_id": int(ticket_id),
        "phases": sorted(phases),
        "sla_row": sla_row,
        "comments": comments,
        "audit_events": audit_events,
        "zendesk_requests": zendesk_requests,
    }


def _temp_name(prefix: str) -> str:
    return f"{GCP_PROJECT}.{BQ_DATASET}.{prefix}_{_utc_now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def apply_author_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    client = _client()
    temp_table = _temp_name("_metadata_author_backfill")
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=AUTHOR_TEMP_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    try:
        client.query(f"""
          MERGE `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments` T
          USING `{temp_table}` S
          ON T.ticket_id = S.ticket_id AND T.comment_id = S.comment_id
          WHEN MATCHED THEN UPDATE SET
            author_id = S.author_id,
            author_name = COALESCE(S.author_name, T.author_name),
            author_email = S.author_email,
            author_role = S.author_role,
            author_is_agent = S.author_is_agent,
            via_channel = S.via_channel
        """).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def apply_sla_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    client = _client()
    temp_table = _temp_name("_metadata_sla_backfill")
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=SLA_TEMP_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    try:
        client.query(f"""
          MERGE `{GCP_PROJECT}.{BQ_DATASET}.tickets` T
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
        """).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def _mark_terminal_if_complete(run_id: str) -> None:
    rows = list(_client().query(
        f"""
        SELECT
          COUNTIF(status IN ('pending', 'running', 'error_retryable')) AS open_items,
          COUNTIF(status = 'error_terminal') AS terminal_errors
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result())
    if not rows:
        return
    if int(rows[0].open_items or 0) == 0:
        _set_run_status(run_id, "completed_with_errors" if int(rows[0].terminal_errors or 0) else "completed")


def run_next_batch(
    *,
    run_id: str,
    ticket_limit: int,
    max_runtime_seconds: int,
    chunk_size: int = 25,
    workers: int = 2,
    sleep_sec: float = 0.1,
    audit_max_pages: int = 3,
    apply: bool = True,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Process pending manifest items until the ticket or runtime limit is hit."""
    ensure_tables()
    require_destination_columns()
    current = status(run_id).get("run")
    if not current:
        raise RuntimeError(f"Unknown metadata backfill run_id: {run_id}")
    if not current.get("active", False):
        return {"run_id": run_id, "skipped": True, "reason": "run is inactive"}
    if current.get("status") in {"completed", "completed_with_errors"}:
        return {"run_id": run_id, "skipped": True, "reason": f"run is {current.get('status')}"}
    recovered = recover_stale_running(run_id)
    if recovered:
        _event(run_id, "WARNING", "recover", "Recovered stale running items", details={"items": recovered})

    batch_id = "metadata_" + uuid.uuid4().hex[:10]
    _set_run_status(run_id, "batch_running" if apply else "dry_run_running")
    _event(run_id, "INFO", "batch", "Batch started", details={
        "batch_id": batch_id,
        "ticket_limit": ticket_limit,
        "max_runtime_seconds": max_runtime_seconds,
        "chunk_size": chunk_size,
        "workers": workers,
        "apply": apply,
        "audit_max_pages": audit_max_pages,
    })

    started = time.monotonic()
    processed_tickets = 0
    total_sla_rows = 0
    total_author_rows = 0
    total_audit_rows = 0
    total_audit_events = 0
    total_zendesk_requests = 0
    total_errors = 0
    form_cache: dict[int, str | None] = {}
    form_cache_lock = threading.Lock()
    workers = max(1, min(int(workers or 1), 4))
    chunk_size = max(1, int(chunk_size or 1))

    while processed_tickets < int(ticket_limit):
        elapsed = time.monotonic() - started
        if elapsed >= int(max_runtime_seconds):
            break
        remaining = int(ticket_limit) - processed_tickets
        selection = _select_next_pending(run_id, limit=min(chunk_size, remaining))
        if not selection.ticket_phases:
            break

        chunk_started = time.monotonic()
        _update_item_status(run_id, selection.ticket_phases, status_value="running", batch_id=batch_id)

        ticket_comments: dict[int, list[dict[str, Any]]] = {}
        ticket_audit_events: dict[int, list[dict[str, Any]]] = {}
        author_ids: set[int] = set()
        sla_rows: list[dict[str, Any]] = []
        chunk_success: dict[int, set[str]] = {}
        chunk_error_items: dict[int, set[str]] = {}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_ticket = {
                executor.submit(
                    _fetch_ticket_bundle,
                    ticket_id,
                    phases,
                    form_cache,
                    form_cache_lock,
                    max(0.0, sleep_sec),
                    audit_max_pages,
                ): ticket_id
                for ticket_id, phases in selection.ticket_phases.items()
            }
            bundles: list[dict[str, Any]] = []
            for future in as_completed(future_to_ticket):
                ticket_id = future_to_ticket[future]
                try:
                    bundles.append(future.result())
                except Exception as exc:
                    logger.exception("metadata backfill fetch failed for ticket %s", ticket_id)
                    chunk_error_items[ticket_id] = set(selection.ticket_phases[ticket_id])
                    total_errors += 1
                    _event(
                        run_id,
                        "ERROR",
                        "fetch",
                        str(exc)[:1000],
                        ticket_id=ticket_id,
                        details={"phases": sorted(selection.ticket_phases[ticket_id])},
                    )

        for bundle in sorted(bundles, key=lambda item: int(item["ticket_id"])):
            ticket_id = int(bundle["ticket_id"])
            phases = set(selection.ticket_phases[ticket_id])
            total_zendesk_requests += int(bundle["zendesk_requests"])
            if bundle["sla_row"]:
                sla_rows.append(bundle["sla_row"])
            if SLA_AUTHOR_PHASE in phases:
                comments = bundle["comments"]
                ticket_comments[ticket_id] = comments
                for comment in comments:
                    author_id = comment.get("author_id")
                    if author_id:
                        author_ids.add(int(author_id))
            if AUDITS_PHASE in phases:
                ticket_audit_events[ticket_id] = bundle["audit_events"]
            chunk_success[ticket_id] = phases

        try:
            users = fetch_users(author_ids) if author_ids else {}
            if author_ids:
                total_zendesk_requests += (len(author_ids) + SHOW_MANY_USERS_BATCH - 1) // SHOW_MANY_USERS_BATCH
            author_rows = build_author_rows(ticket_comments, users) if ticket_comments else []
            audit_rows = bq_sync._build_ticket_audit_event_rows(ticket_audit_events) if ticket_audit_events else []
        except Exception as exc:
            logger.exception("metadata backfill row build failed")
            total_errors += max(1, len(chunk_success))
            _update_item_status(
                run_id,
                chunk_success,
                status_value="error",
                batch_id=batch_id,
                last_error=str(exc)[:1000],
            )
            _event(run_id, "ERROR", "build_rows", str(exc)[:1000], details={"batch_id": batch_id})
            if chunk_error_items:
                _update_item_status(
                    run_id,
                    chunk_error_items,
                    status_value="error",
                    batch_id=batch_id,
                    last_error="Ticket fetch failed; see events table for details",
                )
            continue

        try:
            if apply:
                apply_sla_rows(sla_rows)
                apply_author_rows(author_rows)
                bq_sync._merge_ticket_audit_event_rows(_client(), audit_rows)
                _update_item_status(run_id, chunk_success, status_value="done", batch_id=batch_id)
            else:
                _update_item_status(
                    run_id,
                    chunk_success,
                    status_value="error",
                    batch_id=batch_id,
                    last_error="Dry run completed without writes",
                )
        except Exception as exc:
            logger.exception("metadata backfill BQ apply failed")
            total_errors += max(1, len(chunk_success))
            _update_item_status(
                run_id,
                chunk_success,
                status_value="error",
                batch_id=batch_id,
                last_error=str(exc)[:1000],
            )
            _event(run_id, "ERROR", "apply", str(exc)[:1000], details={"batch_id": batch_id})
            raise

        if chunk_error_items:
            _update_item_status(
                run_id,
                chunk_error_items,
                status_value="error",
                batch_id=batch_id,
                last_error="Ticket fetch failed; see events table for details",
            )

        processed_tickets += len(selection.ticket_phases)
        total_sla_rows += len(sla_rows)
        total_author_rows += len(author_rows)
        total_audit_rows += len(audit_rows)
        total_audit_events += sum(len(events) for events in ticket_audit_events.values())
        chunk_payload = {
            "phase": "chunk",
            "batch_id": batch_id,
            "tickets": len(selection.ticket_phases),
            "sla_rows": len(sla_rows),
            "author_rows": len(author_rows),
            "audit_rows": len(audit_rows),
            "audit_events": sum(len(events) for events in ticket_audit_events.values()),
            "errors": len(chunk_error_items),
            "elapsed_sec": round(time.monotonic() - chunk_started, 2),
            "apply": apply,
        }
        _event(run_id, "INFO", "chunk", "Chunk completed", details=chunk_payload)
        if progress_cb:
            progress_cb(chunk_payload)

    elapsed_total = time.monotonic() - started
    result = {
        "run_id": run_id,
        "batch_id": batch_id,
        "apply": apply,
        "processed_tickets": processed_tickets,
        "sla_rows": total_sla_rows,
        "author_rows": total_author_rows,
        "audit_rows": total_audit_rows,
        "audit_events": total_audit_events,
        "zendesk_requests_estimate": total_zendesk_requests,
        "errors": total_errors,
        "elapsed_sec": round(elapsed_total, 2),
        "tickets_per_min": round(processed_tickets / max(elapsed_total / 60, 0.001), 2),
        "workers": workers,
        "chunk_size": chunk_size,
        "max_runtime_reached": elapsed_total >= int(max_runtime_seconds),
    }
    _event(run_id, "INFO", "batch", "Batch finished", details=result)
    _mark_terminal_if_complete(run_id)
    if status(run_id).get("run", {}).get("status") not in {"completed", "completed_with_errors"}:
        _set_run_status(run_id, "ready")
    return result


def status(run_id: str) -> dict[str, Any]:
    ensure_tables()
    client = _client()
    run_rows = list(client.query(
        f"""
        SELECT run_id, created_at, updated_at, heartbeat_at, status, active,
               include_sla_author, include_audits, sla_scope, note
        FROM `{RUNS_TABLE}`
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result())
    item_rows = list(client.query(
        f"""
        SELECT phase, status, COUNT(*) AS items, COUNT(DISTINCT ticket_id) AS tickets
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
        GROUP BY phase, status
        ORDER BY phase, status
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result())
    pending_rows = list(client.query(
        f"""
        SELECT
          COUNT(DISTINCT IF(status IN ('pending', 'error_retryable', 'running'), ticket_id, NULL)) AS open_tickets,
          COUNTIF(status IN ('pending', 'error_retryable', 'running')) AS open_items,
          COUNTIF(status = 'done') AS done_items,
          COUNTIF(status = 'error_terminal') AS terminal_error_items
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result())
    run = None
    if run_rows:
        row = run_rows[0]
        run = {
            "run_id": row.run_id,
            "created_at": str(row.created_at),
            "updated_at": str(row.updated_at),
            "heartbeat_at": str(row.heartbeat_at),
            "status": row.status,
            "active": bool(row.active),
            "include_sla_author": bool(row.include_sla_author),
            "include_audits": bool(row.include_audits),
            "sla_scope": row.sla_scope,
            "note": row.note,
        }
    summary = pending_rows[0] if pending_rows else None
    return {
        "run": run,
        "items_by_phase_status": [
            {
                "phase": row["phase"],
                "status": row["status"],
                "items": int(row["items"]),
                "tickets": int(row["tickets"]),
            }
            for row in item_rows
        ],
        "totals": {
            "open_tickets": int(summary.open_tickets or 0) if summary else 0,
            "open_items": int(summary.open_items or 0) if summary else 0,
            "done_items": int(summary.done_items or 0) if summary else 0,
            "terminal_error_items": int(summary.terminal_error_items or 0) if summary else 0,
        },
    }


def request_stop(run_id: str) -> dict[str, Any]:
    """Stop future scheduler work by making the run inactive."""
    ensure_tables()
    _client().query(
        f"""
        UPDATE `{RUNS_TABLE}`
        SET active = FALSE, status = 'stopped', updated_at = CURRENT_TIMESTAMP(), heartbeat_at = CURRENT_TIMESTAMP()
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result(timeout=600)
    _event(run_id, "INFO", "control", "Run stopped")
    return status(run_id)


def delete_run_state(run_id: str, *, confirm_run_id: str) -> dict[str, Any]:
    """Delete only temporary manifest/event/run rows for a single run id."""
    if confirm_run_id != run_id:
        raise ValueError("confirm_run_id must match run_id")
    ensure_tables()
    deleted: dict[str, Any] = {}
    for label, table_id in (
        ("items", ITEMS_TABLE),
        ("runs", RUNS_TABLE),
        ("events", EVENTS_TABLE),
    ):
        try:
            job = _client().query(
                f"DELETE FROM `{table_id}` WHERE run_id = @run_id",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                ]),
            )
            job.result(timeout=600)
            deleted[label] = int(job.num_dml_affected_rows or 0)
        except Exception as exc:
            if "streaming buffer" not in str(exc).lower():
                raise
            deleted[label] = 0
            deleted[f"{label}_warning"] = "Skipped fresh streaming-buffer rows; safe to retry later."
    return {"run_id": run_id, "deleted": deleted}
