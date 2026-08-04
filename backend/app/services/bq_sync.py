"""
BQ daily sync service.

Called by POST /api/admin/sync — triggered nightly by Cloud Scheduler.

sync_tickets(start_time)  — incremental Zendesk → BQ tickets/comments/attachments
                            Uses delete+insert for fetched ticket IDs (idempotent).
sync_kb_articles()        — full refresh of kb_articles (truncate+insert, fast ~30s).
vectorize_new_tickets()   — generate Vertex AI embeddings for tickets not yet in
                            ticket_embeddings; used by incremental sync and semantic search.
"""

import io
import logging
import multiprocessing
import os
import queue
import re
import struct
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from google.cloud import bigquery

from app.config import settings
from app.reference_data.fixtures import BRAND_TAG_MAP, brand_from_organization_name, canonical_brand
from app.services.bigquery import _get_client
from app.services.cost_tracker import (
    CallType,
    CostGateError,
    OCR_MEASURED_EXPECTED_CAD,
    calc_cost,
    check_cost_gate,
    get_gate_reserve,
)
from app.services.sla import build_sla_fields
from app.services.zendesk_article_cleanup import (
    cleanup_unpublished_articles,
    is_zendesk_article_published,
)

logger = logging.getLogger(__name__)

GCP_PROJECT = settings.google_cloud_project_id
BQ_DATASET  = "esip_reference"
AUDIT_EVENTS_TABLE = "zendesk_ticket_audit_events"
AUDIT_CHANGE_FIELDS = {"assignee_id", "group_id", "status", "custom_status_id"}

# ---------------------------------------------------------------------------
# Zendesk custom field IDs  (mirrors scripts/export_tickets_bq.py)
# ---------------------------------------------------------------------------

CF = {
    "store_id":          2017,
    "requester_name":    91000001,
    "issue_category":    930100000009,
    "symptoms":          930100000011,
    "workaround":        930100000012,
    "next_steps":        930100000013,
    "resolution":        930100000008,
    "escalated":         930100000006,
    "brand":             91000002,
    "brand_alt":         91000003,
    "source":            91000004,
    "caused_by":         930100000003,
    "product_type":      930100000010,
    "resolution_type":   94000000000001,
    "time_spent_sec":    930100000001,
    "vendor_dispatched": 930100000005,
    "vendor_escalation": 91000005,
}

ROOT_CAUSE_DESC_IDS = {
    91000101, 91000102, 91000103, 91000104, 91000105, 91000106,
    91000107, 91000108, 91000109, 91000110, 91000111, 91000112,
    91000113, 91000114, 91000115, 91000116, 91000117, 91000118,
}

BRAND_TAGS = tuple(BRAND_TAG_MAP)

IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "image/bmp", "image/tiff", "image/svg+xml",
}

MAX_RETRIES   = 3
RETRY_BACKOFF = 5

# ---------------------------------------------------------------------------
# Zendesk helpers
# ---------------------------------------------------------------------------

def _zd_get(url: str, params: dict = None) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(url, params=params, auth=settings.zendesk_auth, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", RETRY_BACKOFF))
            logger.warning("Zendesk rate limit — sleeping %ds", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(RETRY_BACKOFF * attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Zendesk request failed after {MAX_RETRIES} retries: {url}")


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------

def _cf(ticket: dict, field_id: int, default=None):
    for f in ticket.get("custom_fields", []):
        if f["id"] == field_id:
            return f["value"]
    return default


def _bool_cf(ticket: dict, field_id: int) -> bool:
    v = _cf(ticket, field_id)
    return bool(v) if v is not None else False


def _merge_root_cause(ticket: dict) -> str | None:
    parts = [
        str(f["value"]).strip()
        for f in ticket.get("custom_fields", [])
        if f["id"] in ROOT_CAUSE_DESC_IDS and f["value"]
    ]
    return " | ".join(parts) if parts else None


def _parse_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()


def _kb_article_row(article: dict, section: dict, now: datetime) -> dict[str, Any] | None:
    """Build a BigQuery kb_articles row, or None when the article is not published."""
    if not is_zendesk_article_published(article):
        return None
    return {
        "id":          article["id"],
        "title":       article.get("title", ""),
        "url":         article.get("html_url", ""),
        "section":     section.get("name", ""),
        "category":    section.get("category_name", ""),
        "locale":      article.get("locale", ""),
        "created_at":  article.get("created_at"),
        "updated_at":  article.get("updated_at"),
        "exported_at": now.isoformat(),
    }


def _solved_at(ticket: dict) -> str | None:
    if ticket.get("status") in ("solved", "closed"):
        return _parse_ts(ticket.get("updated_at"))
    return None


def _extract_brand(ticket: dict, org_name: str) -> str | None:
    direct = canonical_brand(_cf(ticket, CF["brand"]) or _cf(ticket, CF["brand_alt"]))
    if direct:
        return direct
    for tag in ticket.get("tags", []):
        tagged = canonical_brand(tag)
        if tagged:
            return tagged
    return brand_from_organization_name(org_name)


def _attachments_from_comments(ticket_id: int, comments: list[dict], now: datetime) -> list[dict]:
    rows = []
    for comment in comments:
        comment_id = comment.get("id")
        created_at = _parse_ts(comment.get("created_at"))
        for att in comment.get("attachments", []):
            ct = att.get("content_type", "")
            rows.append({
                "ticket_id":     ticket_id,
                "comment_id":    comment_id,
                "attachment_id": att.get("id"),
                "file_name":     att.get("file_name"),
                "content_type":  ct,
                "size_bytes":    att.get("size", 0),
                "url":           att.get("url"),
                "content_url":   att.get("content_url"),
                "is_image":      ct in IMAGE_MIME_TYPES,
                "created_at":    created_at,
                "exported_at":   now.isoformat(),
            })
        body = comment.get("html_body") or comment.get("body") or ""
        for url in re.findall(
            r'<img[^>]+src=["\']([^"\']+zen(?:desk)?[^"\']*)["\']', body, re.IGNORECASE
        ):
            if not any(r["content_url"] == url for r in rows):
                rows.append({
                    "ticket_id":     ticket_id,
                    "comment_id":    comment_id,
                    "attachment_id": None,
                    "file_name":     url.rsplit("/", 1)[-1].split("?")[0],
                    "content_type":  "image/unknown",
                    "size_bytes":    None,
                    "url":           url,
                    "content_url":   url,
                    "is_image":      True,
                    "created_at":    created_at,
                    "exported_at":   now.isoformat(),
                })
    return rows


def _build_ticket_row(
    ticket: dict,
    comments: list[dict],
    org_cache: dict,
    user_cache: dict,
    group_cache: dict,
    form_cache: dict,
    now: datetime,
) -> dict:
    created_at = _parse_ts(ticket.get("created_at"))
    updated_at = _parse_ts(ticket.get("updated_at"))
    solved_at  = _solved_at(ticket)

    resolution_hours = None
    if created_at and solved_at:
        c = datetime.fromisoformat(created_at)
        s = datetime.fromisoformat(solved_at)
        resolution_hours = (s - c).total_seconds() / 3600

    org_id   = ticket.get("organization_id")
    org_name = _fetch_org_name(org_id, org_cache) if org_id else ""
    assignee_id = ticket.get("assignee_id")
    assignee_user = _fetch_user(assignee_id, user_cache) if assignee_id else None
    assignee_name = (assignee_user or {}).get("name")
    assignee_email = (assignee_user or {}).get("email")
    assignee_group_id = ticket.get("group_id")
    assignee_group_name = _fetch_group_name(assignee_group_id, group_cache)
    ticket_form_name = _fetch_ticket_form_name(ticket.get("ticket_form_id"), form_cache)
    sla_fields = build_sla_fields(ticket, ticket_form_name=ticket_form_name, now=now)

    internal = [c for c in comments if not c.get("public", True)]
    public   = [c for c in comments if c.get("public", True)]

    full_text_parts = [
        ticket.get("subject", ""),
        _cf(ticket, CF["symptoms"]) or "",
        _cf(ticket, CF["resolution"]) or "",
        _merge_root_cause(ticket) or "",
    ]
    for c in internal[-10:]:
        body = c.get("body") or c.get("plain_body") or ""
        full_text_parts.append(body[:500])
    full_text = "\n".join(p for p in full_text_parts if p).strip()

    return {
        "id":                     ticket["id"],
        "subject":                ticket.get("subject"),
        "status":                 ticket.get("status"),
        "priority":               sla_fields["priority"],
        "ticket_type":            sla_fields["ticket_type"],
        "ticket_form_id":         sla_fields["ticket_form_id"],
        "ticket_form_name":       sla_fields["ticket_form_name"],
        "brand":                  _extract_brand(ticket, org_name),
        "store_id":               str(_cf(ticket, CF["store_id"]) or ""),
        "org_id":                 org_id,
        "org_name":               org_name or None,
        "created_at":             created_at,
        "updated_at":             updated_at,
        "solved_at":              solved_at,
        "resolution_hours":       resolution_hours,
        "sla_policy_name":        sla_fields["sla_policy_name"],
        "resolution_sla_target_hours": sla_fields["resolution_sla_target_hours"],
        "resolution_sla_elapsed_hours": sla_fields["resolution_sla_elapsed_hours"],
        "resolution_sla_breached": sla_fields["resolution_sla_breached"],
        "resolution_sla_status":  sla_fields["resolution_sla_status"],
        "time_spent_sec":         _cf(ticket, CF["time_spent_sec"]),
        "via_channel":            ticket.get("via", {}).get("channel"),
        "source":                 _cf(ticket, CF["source"]),
        "assignee_id":            assignee_id,
        "assignee_email":         assignee_email,
        "assignee_name":          assignee_name,
        "assignee_group_id":      assignee_group_id,
        "assignee_group_name":    assignee_group_name,
        "requester_name":         _cf(ticket, CF["requester_name"]),
        "issue_category":         _cf(ticket, CF["issue_category"]),
        "product_type":           _cf(ticket, CF["product_type"]),
        "caused_by":              _cf(ticket, CF["caused_by"]),
        "resolution_type":        _cf(ticket, CF["resolution_type"]),
        "symptoms":               _cf(ticket, CF["symptoms"]),
        "workaround":             _cf(ticket, CF["workaround"]),
        "next_steps":             _cf(ticket, CF["next_steps"]),
        "resolution":             _cf(ticket, CF["resolution"]),
        "root_cause_description": _merge_root_cause(ticket),
        "escalated":              _bool_cf(ticket, CF["escalated"]),
        "vendor_dispatched":      bool(_cf(ticket, CF["vendor_dispatched"])),
        "vendor_escalation":      bool(_cf(ticket, CF["vendor_escalation"])),
        "tags":                   ticket.get("tags", []),
        "comment_count":          len(comments),
        "internal_comment_count": len(internal),
        "public_comment_count":   len(public),
        "attachment_count":       sum(len(c.get("attachments", [])) for c in comments),
        "full_text":              full_text,
        "exported_at":            now.isoformat(),
    }


def _zendesk_user_is_agent(user: dict | None) -> bool | None:
    if not user:
        return None
    role = str(user.get("role") or "").lower()
    if role in {"agent", "admin"}:
        return True
    if role == "end-user":
        return False
    return None


def _comment_author_name(comment: dict, user: dict | None = None) -> str | None:
    return (
        comment.get("author_name")
        or (user or {}).get("name")
        or comment.get("via", {}).get("source", {}).get("from", {}).get("name")
    )


def _build_comment_rows(
    ticket_id: int,
    comments: list[dict],
    now: datetime,
    user_cache: dict,
) -> list[dict]:
    rows: list[dict] = []
    for c in comments:
        author_id = c.get("author_id")
        author = _fetch_user(author_id, user_cache) if author_id else None
        rows.append({
            "ticket_id":       ticket_id,
            "comment_id":      c["id"],
            "created_at":      _parse_ts(c.get("created_at")),
            "is_internal":     not c.get("public", True),
            "author_id":       author_id,
            "author_name":     _comment_author_name(c, author),
            "author_email":    (author or {}).get("email"),
            "author_role":     (author or {}).get("role"),
            "author_is_agent": _zendesk_user_is_agent(author),
            "via_channel":     c.get("via", {}).get("channel"),
            "body":            c.get("body") or c.get("plain_body") or "",
            "exported_at":     now.isoformat(),
        })
    return rows


# ---------------------------------------------------------------------------
# Org / user name caches (process-level, reset per sync run)
# ---------------------------------------------------------------------------

def _fetch_org_name(org_id: int, cache: dict) -> str:
    if org_id in cache:
        return cache[org_id]
    try:
        data = _zd_get(f"{settings.zendesk_base_url}/organizations/{org_id}")
        name = data.get("organization", {}).get("name", "")
    except Exception:
        name = ""
    cache[org_id] = name
    return name


def _fetch_user_name(user_id: int | None, cache: dict) -> str | None:
    user = _fetch_user(user_id, cache)
    return (user or {}).get("name") or None


def _fetch_user(user_id: int | None, cache: dict) -> dict | None:
    if not user_id:
        return None
    if user_id in cache:
        return cache[user_id]
    try:
        data = _zd_get(f"{settings.zendesk_base_url}/users/{user_id}")
        user = data.get("user") or {}
    except Exception:
        user = {}
    cache[user_id] = user or None
    return user or None


def _fetch_group_name(group_id: int | None, cache: dict) -> str | None:
    if not group_id:
        return None
    if group_id in cache:
        return cache[group_id]
    try:
        data = _zd_get(f"{settings.zendesk_base_url}/groups/{group_id}")
        name = data.get("group", {}).get("name", "")
    except Exception as exc:
        logger.warning("Could not resolve Zendesk group_id=%s: %s", group_id, exc)
        name = ""
    cache[group_id] = name or None
    return name or None


def _fetch_ticket_form_name(form_id: int | None, cache: dict) -> str | None:
    if not form_id:
        return None
    if form_id in cache:
        return cache[form_id]
    try:
        data = _zd_get(f"{settings.zendesk_base_url}/ticket_forms/{form_id}")
        form = data.get("ticket_form") or {}
        name = form.get("display_name") or form.get("name") or ""
    except Exception as exc:
        logger.warning("Could not resolve Zendesk ticket_form_id=%s: %s", form_id, exc)
        name = ""
    cache[form_id] = name or None
    return name or None


# ---------------------------------------------------------------------------
# BQ helpers: delete by ticket IDs + load-job insert
# ---------------------------------------------------------------------------

def _bq_delete_tickets(client, ticket_ids: list[int]) -> None:
    """Delete existing rows for these ticket IDs from all 3 tables."""
    if not ticket_ids:
        return
    ids_str = ", ".join(str(i) for i in ticket_ids)
    for table, col in [
        ("tickets",             "id"),
        ("ticket_comments",     "ticket_id"),
        ("ticket_attachments",  "ticket_id"),
    ]:
        sql = (
            f"DELETE FROM `{GCP_PROJECT}.{BQ_DATASET}.{table}` "
            f"WHERE {col} IN ({ids_str})"
        )
        client.query(sql).result()
    logger.info("Deleted rows for %d ticket IDs from BQ", len(ticket_ids))


def _bq_insert(client, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{table}"
    job = client.load_table_from_json(
        rows,
        table_id,
        job_config=bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            ignore_unknown_values=True,
        ),
    )
    job.result(timeout=600)
    logger.info("Loaded %d rows into %s via BigQuery load job", len(rows), table)


def _ensure_ticket_audit_events_table(client) -> None:
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{AUDIT_EVENTS_TABLE}"
    schema = [
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
    try:
        table = client.get_table(table_id)
    except Exception:
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(field="created_at")
        table.clustering_fields = ["ticket_id", "field_name"]
        client.create_table(table)
        logger.info("Created BigQuery table %s", table_id)
        return

    existing = {field.name for field in table.schema}
    missing = {field.name for field in schema} - existing
    if missing:
        raise RuntimeError(f"{table_id} missing columns: {sorted(missing)}")


def _stringify_audit_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    import json

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


def _extract_audit_change_events(ticket_id: int, audits: list[dict]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for audit in audits:
        audit_id = audit.get("id")
        created_at = audit.get("created_at")
        author_id = audit.get("author_id")
        via_channel = ((audit.get("via") or {}).get("channel") or None)
        for event in audit.get("events") or []:
            if event.get("type") != "Change":
                continue
            field_name = str(event.get("field_name") or "")
            if field_name not in AUDIT_CHANGE_FIELDS:
                continue
            events.append({
                "ticket_id": int(ticket_id),
                "audit_id": audit_id,
                "event_id": event.get("id"),
                "created_at": created_at,
                "author_id": author_id,
                "via_channel": via_channel,
                "field_name": field_name,
                "previous_value": event.get("previous_value"),
                "value": event.get("value"),
            })
    return events


def _fetch_ticket_audit_events(ticket_id: int, *, max_pages: int) -> tuple[list[dict[str, Any]], int]:
    audits: list[dict] = []
    url: str | None = f"{settings.zendesk_base_url}/tickets/{int(ticket_id)}/audits.json"
    params: dict | None = {"per_page": 100}
    pages = 0
    while url and pages < max(1, int(max_pages)):
        data = _zd_get(url, params=params)
        audits.extend(data.get("audits") or [])
        url = data.get("next_page")
        params = None
        pages += 1
    return _extract_audit_change_events(ticket_id, audits), pages


def _build_ticket_audit_event_rows(events_by_ticket: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    fetched_at = datetime.now(timezone.utc).isoformat()
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


def _merge_ticket_audit_event_rows(client, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    temp_table = (
        f"{GCP_PROJECT}.{BQ_DATASET}._ticket_audit_events_sync_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    schema = [
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
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"),
    ).result(timeout=600)
    dest = f"{GCP_PROJECT}.{BQ_DATASET}.{AUDIT_EVENTS_TABLE}"
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


def sync_ticket_audit_events(
    ticket_ids: list[int],
    progress_cb: Callable[[str], None] | None = None,
    *,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Persist Zendesk audit assignment/status events for tickets touched by sync."""
    ordered_ids = sorted({int(ticket_id) for ticket_id in ticket_ids})
    if not ordered_ids:
        return {"tickets": 0, "audit_rows": 0, "audit_events": 0, "errors": []}
    if progress_cb:
        progress_cb(f"Fetching Zendesk audit metadata for {len(ordered_ids)} tickets...")

    client = _get_client()
    _ensure_ticket_audit_events_table(client)
    events_by_ticket: dict[int, list[dict[str, Any]]] = {}
    errors: list[str] = []
    request_count = 0
    for index, ticket_id in enumerate(ordered_ids, 1):
        try:
            events, pages = _fetch_ticket_audit_events(ticket_id, max_pages=max_pages)
            events_by_ticket[ticket_id] = events
            request_count += pages
        except Exception as exc:
            errors.append(f"Ticket {ticket_id} audits: {exc}")
        if index % 50 == 0 and progress_cb:
            progress_cb(f"Fetched audit metadata for {index}/{len(ordered_ids)} tickets...")
        time.sleep(0.05)

    rows = _build_ticket_audit_event_rows(events_by_ticket)
    if not errors:
        _merge_ticket_audit_event_rows(client, rows)
    return {
        "tickets": len(ordered_ids),
        "audit_rows": len(rows),
        "audit_events": sum(len(events) for events in events_by_ticket.values()),
        "zendesk_requests": request_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Main sync functions
# ---------------------------------------------------------------------------

def sync_tickets(
    start_time: int,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Incremental ticket sync using the Zendesk Incremental API cursor mechanism.

    start_time  — Unix timestamp; Zendesk returns all tickets updated after this point.
                  Pass the `end_time` from the previous sync response for zero-gap,
                  zero-overlap coverage. On first run, pass (now - 2 days) as a safe default.

    progress_cb — optional callback called with a human-readable progress string.

    Returns:
        {
            "tickets": N, "comments": N, "attachments": N,
            "errors": [...],
            "next_cursor": <int>  # Zendesk end_time — save this and pass as start_time next time.
                                  # None if the stream ended unexpectedly (do NOT save as cursor).
        }

    IMPORTANT: only save "next_cursor" to Firestore AFTER BQ writes complete successfully.
    If this function raises or returns errors, do not update the cursor.
    """
    def _progress(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    since_dt = datetime.fromtimestamp(start_time, tz=timezone.utc)
    logger.info("sync_tickets: fetching tickets updated since %s (cursor=%d)", since_dt.isoformat(), start_time)

    now        = datetime.now(timezone.utc)
    client     = _get_client()
    org_cache: dict  = {}
    user_cache: dict = {}
    group_cache: dict = {}
    form_cache: dict = {}
    errors: list[str] = []

    ticket_rows:     list[dict] = []
    comment_rows:    list[dict] = []
    attachment_rows: list[dict] = []
    ticket_ids:      list[int]  = []
    skipped_ticket_ids: list[int] = []

    next_cursor: int | None = None
    next_url: str | None = None
    page = 0

    while True:
        page += 1
        try:
            if next_url:
                data = _zd_get(next_url)
            else:
                data = _zd_get(
                    f"{settings.zendesk_base_url}/incremental/tickets",
                    params={"start_time": start_time},
                )
        except Exception as exc:
            errors.append(f"Fetch page {page}: {exc}")
            logger.error("sync_tickets: fetch failed at page %d: %s", page, exc)
            break

        # Zendesk returns end_time on every page; the final value (at end_of_stream)
        # is the safe cursor to use for the next run.
        if "end_time" in data:
            next_cursor = data["end_time"]

        tickets = [t for t in data.get("tickets", []) if t.get("status") != "deleted"]

        for ticket in tickets:
            tid = ticket["id"]
            try:
                comments_data = _zd_get(f"{settings.zendesk_base_url}/tickets/{tid}/comments")
                comments = comments_data.get("comments", [])
            except Exception as exc:
                msg = (
                    f"Comments fetch failed for ticket {tid}; skipped ticket to preserve "
                    f"existing BQ comments/attachments: {exc}"
                )
                errors.append(msg)
                skipped_ticket_ids.append(tid)
                logger.warning("sync_tickets: %s", msg)
                time.sleep(0.1)
                continue
            time.sleep(0.1)

            ticket_ids.append(tid)
            ticket_rows.append(_build_ticket_row(ticket, comments, org_cache, user_cache, group_cache, form_cache, now))
            comment_rows.extend(_build_comment_rows(tid, comments, now, user_cache))
            attachment_rows.extend(_attachments_from_comments(tid, comments, now))

            if len(ticket_rows) % 50 == 0:
                _progress(f"Fetching tickets: {len(ticket_rows)} processed...")

        if data.get("end_of_stream"):
            break

        next_url = data.get("after_url") or data.get("next_page")
        if not next_url:
            break
        time.sleep(0.15)

    logger.info(
        "sync_tickets: fetched %d tickets, %d comments, %d attachments; next_cursor=%s",
        len(ticket_rows), len(comment_rows), len(attachment_rows), next_cursor,
    )

    if ticket_ids:
        _progress(f"Saving {len(ticket_rows)} tickets to BigQuery...")
        _bq_delete_tickets(client, ticket_ids)
        _bq_insert(client, "tickets",             ticket_rows)
        _bq_insert(client, "ticket_comments",     comment_rows)
        _bq_insert(client, "ticket_attachments",  attachment_rows)
        audit_result = sync_ticket_audit_events(ticket_ids, progress_cb=progress_cb)
        errors.extend(audit_result.get("errors") or [])
    else:
        audit_result = {"tickets": 0, "audit_rows": 0, "audit_events": 0, "errors": []}

    return {
        "tickets":      len(ticket_rows),
        "comments":     len(comment_rows),
        "attachments":  len(attachment_rows),
        "ticket_audit_events": audit_result.get("audit_events", 0),
        "ticket_audit_rows": audit_result.get("audit_rows", 0),
        "ticket_audit_tickets": audit_result.get("tickets", 0),
        "skipped_tickets": len(skipped_ticket_ids),
        "skipped_ticket_ids": skipped_ticket_ids[:50],
        "errors":       errors,
        "next_cursor":  next_cursor,  # save to Firestore only on full success
    }


def sync_kb_articles() -> dict[str, Any]:
    """
    Full refresh of kb_articles: truncate + insert all published articles.
    Runtime depends on article volume and Zendesk API rate limits.
    """
    now = datetime.now(timezone.utc)
    logger.info("sync_kb_articles: fetching sections...")

    # Sections
    sections: dict[int, dict] = {}
    url = f"{settings.zendesk_base_url}/help_center/sections"
    while url:
        data = _zd_get(url, params={"per_page": 100})
        for s in data.get("sections", []):
            sections[s["id"]] = {"name": s.get("name", ""), "category_id": s.get("category_id")}
        url = data.get("next_page")
        if url:
            time.sleep(0.3)

    # Categories
    categories: dict[int, str] = {}
    cat_url = f"{settings.zendesk_base_url}/help_center/categories"
    while cat_url:
        data = _zd_get(cat_url, params={"per_page": 100})
        for c in data.get("categories", []):
            categories[c["id"]] = c.get("name", "")
        cat_url = data.get("next_page")
        if cat_url:
            time.sleep(0.3)

    for s in sections.values():
        s["category_name"] = categories.get(s.get("category_id"), "")

    # Articles. Zendesk Help Center API returns draft/unpublished articles to
    # authenticated callers; ESIP must not index them as live KB content.
    articles: list[dict] = []
    unpublished_articles: list[dict] = []
    art_url = f"{settings.zendesk_base_url}/help_center/articles"
    while art_url:
        data = _zd_get(art_url, params={"per_page": 100, "sort_by": "created_at"})
        for a in data.get("articles", []):
            section = sections.get(a.get("section_id"), {})
            row = _kb_article_row(a, section, now)
            if row is None:
                unpublished_articles.append({
                    "id": a.get("id"),
                    "title": a.get("title", ""),
                    "html_url": a.get("html_url", ""),
                    "section": section.get("name", ""),
                    "category": section.get("category_name", ""),
                    "locale": a.get("locale", ""),
                    "draft": a.get("draft"),
                    "created_at": a.get("created_at"),
                    "updated_at": a.get("updated_at"),
                })
                continue
            articles.append(row)
        art_url = data.get("next_page")
        if art_url:
            time.sleep(0.3)

    logger.info(
        "sync_kb_articles: fetched %d published articles; skipped %d draft/unpublished articles",
        len(articles),
        len(unpublished_articles),
    )

    client = _get_client()
    client.query(
        f"TRUNCATE TABLE `{GCP_PROJECT}.{BQ_DATASET}.kb_articles`"
    ).result()
    _bq_insert(client, "kb_articles", articles)

    cleanup_result: dict[str, Any] = {}
    errors: list[str] = []
    if unpublished_articles:
        try:
            cleanup_result = cleanup_unpublished_articles(
                unpublished_articles,
                dry_run=False,
                bq_client=client,
            )
            logger.info("sync_kb_articles: unpublished cleanup result=%s", cleanup_result)
        except Exception as exc:
            logger.error("sync_kb_articles: unpublished cleanup failed: %s", exc, exc_info=True)
            errors.append(f"unpublished_cleanup_failed: {exc}")

    return {
        "articles": len(articles),
        "skipped_unpublished_articles": len(unpublished_articles),
        "unpublished_cleanup": cleanup_result,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Embedding sync for semantic ticket search
# ---------------------------------------------------------------------------

_VECTORIZE_BATCH = 100   # texts per Vertex AI embedding call
_VECTORIZE_INSERT_BATCH = 500   # rows per BQ insert call
_DOC_WORKERS = int(os.environ.get("DOC_WORKERS", "20"))   # parallel BQ queries for doc generation
_GCS_WORKERS = int(os.environ.get("GCS_WORKERS", "30"))   # parallel GCS uploads


def vectorize_new_tickets(
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Generate or refresh Vertex AI embeddings for tickets that:
      - have no embedding yet (new tickets), OR
      - were updated in Zendesk since the last embedding (new comments, fields), OR
      - have new OCR results since the last embedding (new screenshots processed).

    Uses full document text (all fields, comments, and OCR) via ticket_document.py.
    Embedding model: gemini-embedding-2-preview (3072 dims).
    Also uploads ticket documents to GCS and triggers Vertex AI Agent Builder INCREMENTAL import.

    Called after each sync run and by the one-time init script.
    Returns {"embedded": N, "gcs_uploaded": N, "errors": [...]}
    """
    from app.services import embeddings as emb_service  # noqa: PLC0415
    from app.services.ticket_document import generate_ticket_document  # noqa: PLC0415

    client = _get_client()

    # Read per-run limit from Firestore system/sync_config {vectorize_limit}.
    # Safety net: caps how many tickets can be re-embedded in one nightly run.
    # Does NOT fix the root cause of cascades — see reprocess_all_ocr.py for that.
    # Default 100; raise via Admin Panel once platform is stable.
    try:
        from app.services.firestore import get_db  # noqa: PLC0415
        _cfg_doc = get_db().collection("system").document("sync_config").get()
        vectorize_limit = int((_cfg_doc.to_dict() or {}).get("vectorize_limit", 100))
    except Exception:
        vectorize_limit = 100
    vectorize_limit = max(1, vectorize_limit)

    # Find tickets needing (re-)embedding:
    #   1. New tickets (te.ticket_id IS NULL) — highest priority, embed first
    #   2. Updated tickets: ticket was modified in Zendesk after the embedding was created
    #      (new comment, new attachment, field change — all change updated_at in Zendesk)
    #
    # NOTE: There is intentionally NO condition based on OCR timestamps.
    # New images always arrive via Zendesk comments → updated_at changes → condition 2 catches it.
    # OCR runs (step 2) always completes before vectorization (step 3) in the same sync run,
    # so OCR data is available when generate_ticket_document() runs.
    # OCR timestamps are deliberately excluded because they can create unnecessary
    # re-embedding cascades without a corresponding Zendesk ticket update.
    #
    # ORDER BY priority: new tickets first, then most recently updated.
    # LIMIT caps how many are processed this run — remainder handled in subsequent nightly runs.
    sql = f"""
    SELECT t.id,
      (te.ticket_id IS NOT NULL) AS is_update
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` t
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` te ON t.id = te.ticket_id
    WHERE te.ticket_id IS NULL
       OR t.updated_at > te.embedded_at
    ORDER BY
      (te.ticket_id IS NULL) DESC,
      t.updated_at DESC
    LIMIT {vectorize_limit}
    """
    # Run a separate COUNT query so we can log how many total candidates exist vs how many
    # will be processed this run — makes it visible in logs when the limit is binding.
    count_sql = f"""
    SELECT COUNT(*) AS total
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` t
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` te ON t.id = te.ticket_id
    WHERE te.ticket_id IS NULL
       OR t.updated_at > te.embedded_at
    """
    try:
        total_candidates = list(client.query(count_sql).result())[0].total
    except Exception:
        total_candidates = None

    rows = list(client.query(sql).result())
    if not rows:
        if progress_cb:
            progress_cb("Embeddings: all tickets up to date, nothing to do.")
        return {"embedded": 0, "gcs_uploaded": 0, "errors": []}

    # Pre-job cost gate: estimate total embedding cost.
    # Average ticket document: ~3 000 tokens (fields + comments + OCR).
    _embed_cost_per_ticket = calc_cost(tokens_in=3000, tokens_out=0, is_embedding=True)
    _embed_projected = _embed_cost_per_ticket * len(rows)
    try:
        check_cost_gate(CallType.EMBED_TICKET, _embed_projected)
    except CostGateError as exc:
        msg = f"Embedding batch blocked by cost gate ({len(rows)} tickets): {exc}"
        logger.error(msg)
        if progress_cb:
            progress_cb(f"Embeddings: BLOCKED — {exc}")
        return {"embedded": 0, "gcs_uploaded": 0, "errors": [msg], "blocked_by_cost_gate": True}

    if total_candidates is not None and total_candidates > len(rows):
        logger.info(
            "vectorize_new_tickets: %d total candidates, processing %d this run (limit=%d). "
            "Remaining %d will be processed in subsequent nightly runs.",
            total_candidates, len(rows), vectorize_limit, total_candidates - len(rows),
        )
    if progress_cb:
        suffix = f" (limit={vectorize_limit}, {total_candidates} total)" if total_candidates else ""
        progress_cb(f"Embeddings: processing {len(rows)} tickets{suffix} (est. cost ${_embed_projected:.4f})...")

    ticket_ids = [r.id for r in rows]

    # Delete stale embeddings for tickets being re-embedded (to avoid duplicates)
    stale_ids = [r.id for r in rows if r.is_update]
    if stale_ids:
        ids_str = ",".join(map(str, stale_ids))
        client.query(
            f"DELETE FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` WHERE ticket_id IN ({ids_str})"
        ).result()

    errors: list[str] = []
    total_embedded = 0
    total_gcs_uploaded = 0
    now = datetime.now(timezone.utc).isoformat()
    total_batches = (len(ticket_ids) + _VECTORIZE_BATCH - 1) // _VECTORIZE_BATCH
    loop_start = time.time()

    for batch_start in range(0, len(ticket_ids), _VECTORIZE_BATCH):
        batch_ids = ticket_ids[batch_start: batch_start + _VECTORIZE_BATCH]
        batch_num = batch_start // _VECTORIZE_BATCH + 1

        # Generate full document text for each ticket in this batch — parallel BQ queries
        batch_texts: list[str] = [f"TICKET #{tid}" for tid in batch_ids]
        with ThreadPoolExecutor(max_workers=_DOC_WORKERS) as executor:
            future_to_idx = {
                executor.submit(generate_ticket_document, tid): i
                for i, tid in enumerate(batch_ids)
            }
            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                try:
                    batch_texts[i] = future.result()
                except Exception as exc:
                    logger.warning("generate_ticket_document failed for ticket %d: %s", batch_ids[i], exc)

        # Embed the full documents
        try:
            vectors = emb_service.embed_texts(batch_texts, task_type="RETRIEVAL_DOCUMENT")
        except Exception as exc:
            err = f"Embedding batch {batch_start}-{batch_start + len(batch_ids)} failed: {exc}"
            logger.error(err)
            errors.append(err)
            continue

        insert_rows = [
            {"ticket_id": tid, "embedding": vec, "embedded_at": now}
            for tid, vec in zip(batch_ids, vectors)
        ]
        try:
            _bq_insert(client, "ticket_embeddings", insert_rows)
            total_embedded += len(insert_rows)
        except Exception as exc:
            err = f"BQ insert batch {batch_start} failed: {exc}"
            logger.error(err)
            errors.append(err)
            continue

        # Upload documents to GCS for Vertex AI Agent Builder indexing — parallel uploads
        with ThreadPoolExecutor(max_workers=_GCS_WORKERS) as executor:
            future_to_tid = {
                executor.submit(upload_ticket_to_gcs, tid, doc_text): tid
                for tid, doc_text in zip(batch_ids, batch_texts)
            }
            for future in as_completed(future_to_tid):
                tid = future_to_tid[future]
                try:
                    future.result()
                    total_gcs_uploaded += 1
                except Exception as exc:
                    logger.warning("GCS upload failed for ticket %d: %s", tid, exc)
                    errors.append(f"GCS upload failed for ticket {tid}: {exc}")

        elapsed = time.time() - loop_start
        rate = total_embedded / elapsed if elapsed > 0 else 0
        remaining = (len(ticket_ids) - total_embedded) / rate if rate > 0 else 0
        logger.info(
            "Batch %d/%d | embedded=%d/%d (%.1f/s) | gcs=%d | errors=%d | ETA: %.0f min",
            batch_num, total_batches, total_embedded, len(ticket_ids),
            rate, total_gcs_uploaded, len(errors), remaining / 60,
        )
        if progress_cb:
            progress_cb(f"Embeddings: {total_embedded}/{len(rows)} done ({rate:.1f}/s, ETA {remaining/60:.0f}min)...")

    # Trigger Vertex AI Agent Builder INCREMENTAL import after all uploads
    if total_gcs_uploaded > 0:
        try:
            op = trigger_vertex_tickets_import()
            logger.info("Vertex AI tickets import triggered: %s", op)
        except Exception as exc:
            logger.warning("Vertex AI tickets import trigger failed: %s", exc)
            errors.append(f"Vertex AI import trigger failed: {exc}")

    if progress_cb:
        progress_cb(f"Embeddings: complete — {total_embedded} tickets embedded, {total_gcs_uploaded} uploaded to GCS.")

    return {"embedded": total_embedded, "gcs_uploaded": total_gcs_uploaded, "errors": errors}


# ---------------------------------------------------------------------------
# GCS upload and Vertex AI ticket data-store import
# ---------------------------------------------------------------------------

_GCS_BUCKET = "esip-reference-data"
_GCS_TICKETS_PREFIX = "tickets"


def _get_gcs_client():
    """Return a GCS client using service account credentials or ADC fallback."""
    from google.cloud import storage as gcs
    from google.oauth2 import service_account as sa
    try:
        creds = sa.Credentials.from_service_account_file(
            settings.google_application_credentials,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return gcs.Client(project=GCP_PROJECT, credentials=creds)
    except Exception:
        return gcs.Client(project=GCP_PROJECT)


def upload_ticket_to_gcs(ticket_id: int, text: str) -> None:
    """Upload a ticket document to gs://esip-reference-data/tickets/{ticket_id}.txt."""
    client = _get_gcs_client()
    bucket = client.bucket(_GCS_BUCKET)
    blob = bucket.blob(f"{_GCS_TICKETS_PREFIX}/{ticket_id}.txt")
    blob.upload_from_string(text.encode("utf-8"), content_type="text/plain; charset=utf-8")
    logger.debug("Uploaded ticket %d to GCS", ticket_id)


def trigger_vertex_tickets_import() -> str:
    """
    Trigger an INCREMENTAL import from gs://esip-reference-data/tickets/ into the
    tickets Vertex AI Agent Builder data store.

    Returns the operation name (long-running operation ID) or raises on error.
    """
    import google.auth
    import google.auth.transport.requests

    _PROJECT  = settings.google_cloud_project_id
    _LOCATION = settings.vertex_ai_location
    _DATA_STORE = settings.vertex_ai_tickets_data_store_id

    # Obtain Bearer token — try service account file first, then ADC
    try:
        from google.oauth2 import service_account as sa
        creds = sa.Credentials.from_service_account_file(
            settings.google_application_credentials,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    url = (
        f"https://discoveryengine.googleapis.com/v1"
        f"/projects/{_PROJECT}/locations/{_LOCATION}"
        f"/collections/default_collection/dataStores/{_DATA_STORE}"
        f"/branches/default_branch/documents:import"
    )
    payload = {
        "gcsSource": {
            "inputUris": [f"gs://{_GCS_BUCKET}/{_GCS_TICKETS_PREFIX}/*"],
            "dataSchema": "content",
        },
        "reconciliationMode": "INCREMENTAL",
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    operation_name = resp.json().get("name", "")
    logger.info("Triggered Vertex AI tickets import: operation=%s", operation_name)
    return operation_name


# ---------------------------------------------------------------------------
# Incremental OCR for newly synced attachments
# ---------------------------------------------------------------------------

_OCR_SLEEP       = 0.4    # seconds between Gemini Vision calls
_DEFAULT_TICKET_OCR_LIMIT = 200
_TICKET_PDF_TEXT_EXTRACT_TIMEOUT_SECONDS = 60

# MIME types supported by Gemini Vision for ticket attachment OCR.
_SUPPORTED_IMAGE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _download_zendesk_attachment(
    content_url: str,
    *,
    timeout: int = 30,
) -> tuple[bytes | None, str, str]:
    """
    Download Zendesk attachment bytes.

    Zendesk private end-user attachment rollout can make content_url require
    API credentials. Try anonymous download first, then authenticated Zendesk
    basic auth on 401/403.
    """
    try:
        response = requests.get(content_url, timeout=timeout)
        if response.status_code in (401, 403):
            response = requests.get(content_url, auth=settings.zendesk_auth, timeout=timeout)
        if response.status_code != 200:
            return None, response.headers.get("Content-Type", ""), f"http_{response.status_code}"
        return response.content, response.headers.get("Content-Type", ""), ""
    except Exception as exc:
        return None, "", f"download_error:{exc}"


def _sniff_attachment_mime(content: bytes, declared_mime: str = "") -> str:
    """Prefer bytes over Zendesk metadata; attachments can be mislabeled."""
    if content.startswith(b"%PDF"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"

    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    if mime == "image/jpg":
        return "image/jpeg"
    return mime


def _ticket_pypdf_extract_worker(content: bytes, result_queue: multiprocessing.Queue) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        result_queue.put(("ok", "\n\n".join(pages).strip()))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def _extract_pdf_text(content: bytes) -> str:
    """Extract vector PDF text with a hard timeout before Gemini PDF OCR fallback."""
    import re as _re

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_ticket_pypdf_extract_worker, args=(content, result_queue), daemon=True)
    proc.start()
    proc.join(_TICKET_PDF_TEXT_EXTRACT_TIMEOUT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        logger.warning(
            "ticket OCR: pypdf timed out after %ss",
            _TICKET_PDF_TEXT_EXTRACT_TIMEOUT_SECONDS,
        )
        return ""

    try:
        status, value = result_queue.get_nowait()
    except queue.Empty:
        logger.warning("ticket OCR: pypdf returned no result")
        return ""
    if status == "error":
        logger.warning("ticket OCR: pypdf extraction failed: %s", value)
        return ""
    return _re.sub(r"[ \t]{2,}", " ", str(value or "").strip())


def _ticket_ocr_limit() -> int:
    """Read nightly ticket OCR cap from Firestore sync_config; default keeps cron bounded."""
    try:
        from app.services.firestore import get_db  # noqa: PLC0415

        cfg_doc = get_db().collection("system").document("sync_config").get()
        raw = (cfg_doc.to_dict() or {}).get("ticket_ocr_limit", _DEFAULT_TICKET_OCR_LIMIT)
        return max(1, int(raw))
    except Exception:
        return _DEFAULT_TICKET_OCR_LIMIT


def _validate_image_bytes(content: bytes, mime_type: str) -> str:
    """Return a terminal skip reason when an image file is structurally corrupt."""
    if mime_type == "image/png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "corrupt_invalid_png"
        pos = 8
        while pos + 8 <= len(content):
            length = struct.unpack(">I", content[pos:pos + 4])[0]
            chunk_type = content[pos + 4:pos + 8]
            data_start = pos + 8
            data_end = data_start + length
            crc_end = data_end + 4
            if crc_end > len(content):
                return "corrupt_truncated_image"
            stored_crc = struct.unpack(">I", content[data_end:crc_end])[0]
            actual_crc = zlib.crc32(chunk_type + content[data_start:data_end]) & 0xFFFFFFFF
            if stored_crc != actual_crc:
                return "corrupt_invalid_png_crc"
            pos = crc_end
            if chunk_type == b"IEND":
                return ""
        return "corrupt_truncated_image"

    if mime_type == "image/jpeg":
        if not content.startswith(b"\xff\xd8\xff"):
            return "corrupt_invalid_jpeg"
        if not content.rstrip().endswith(b"\xff\xd9"):
            return "corrupt_truncated_image"
        return ""

    if mime_type == "image/gif":
        if not content.startswith((b"GIF87a", b"GIF89a")):
            return "corrupt_invalid_gif"
        if not content.rstrip().endswith(b";"):
            return "corrupt_truncated_image"
        return ""

    if mime_type == "image/webp":
        if not (content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
            return "corrupt_invalid_webp"
        if len(content) >= 8:
            declared_size = int.from_bytes(content[4:8], "little") + 8
            if len(content) < declared_size:
                return "corrupt_truncated_image"
        return ""

    return ""


def _extract_ticket_attachment_text(
    content: bytes,
    declared_mime: str,
) -> tuple[str | None, str, str]:
    """
    Return (text, error, effective_mime) for downloaded ticket attachment bytes.

    PDFs are parsed with pypdf first. If they appear scanned or text-poor, use
    Gemini's native PDF OCR path through ocr_image(), preserving the same CIS
    and thinking_budget=0 guard as image OCR.
    """
    from app.services.gemini import ocr_image  # noqa: PLC0415

    effective_mime = _sniff_attachment_mime(content, declared_mime)
    if effective_mime == "application/pdf":
        pdf_text = _extract_pdf_text(content)
        if len(pdf_text.strip()) >= 100:
            return (
                "[IMAGE DESCRIPTION: PDF document attached to the ticket; text was extracted directly from the PDF.]\n\n"
                + pdf_text.strip()
            ), "", effective_mime
        if len(content) > _MAX_IMAGE_BYTES:
            return None, "too_large_pdf", effective_mime
        text, err = ocr_image(content, "application/pdf")
        return text, err, effective_mime

    if effective_mime not in _SUPPORTED_IMAGE_MIME:
        return None, f"unsupported_type:{effective_mime or declared_mime}", effective_mime

    corruption = _validate_image_bytes(content, effective_mime)
    if corruption:
        return None, corruption, effective_mime

    text, err = ocr_image(content, effective_mime)
    return text, err, effective_mime


def ocr_new_attachments(
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    OCR newly synced image attachments that haven't been processed yet.

    Called after each sync run. Processes a bounded batch of pending images so
    one large backlog cannot consume the whole nightly Scheduler window.
    The standalone scripts/ocr_attachments.py handles emergency bulk re-processing.

    Returns {"ocr_processed": N, "errors": [...]}
    """
    client = _get_client()
    now    = datetime.now(timezone.utc)

    # Ensure OCR table exists before querying it
    from app.services.bigquery import ensure_ocr_table  # noqa: PLC0415
    ensure_ocr_table()

    limit = _ticket_ocr_limit()
    count_sql = f"""
    SELECT COUNT(*) AS total
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o
      ON a.content_url = o.content_url
    WHERE a.is_image = TRUE
      AND a.content_url IS NOT NULL
      AND o.content_url IS NULL
    """
    try:
        total_pending = int(list(client.query(count_sql).result())[0].total)
    except Exception:
        total_pending = 0

    sql = f"""
    SELECT
        a.ticket_id,
        a.content_url,
        COALESCE(a.content_type, '') AS content_type,
        COALESCE(a.size_bytes, 0)    AS size_bytes
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o
      ON a.content_url = o.content_url
    WHERE a.is_image = TRUE
      AND a.content_url IS NOT NULL
      AND o.content_url IS NULL
    ORDER BY a.ticket_id
    LIMIT {limit}
    """
    rows = list(client.query(sql).result())
    if not rows:
        if progress_cb:
            progress_cb("OCR: no new images to process.")
        return {
            "ocr_processed": 0,
            "errors": [],
            "warnings": [],
            "pending_total": total_pending,
            "limit": limit,
            "remaining_after_limit": 0,
        }

    # Pre-job cost gate: use CIS reserved CAD per image × batch size (aligned with ocr_image() gate).
    _ocr_reserved_per = get_gate_reserve(CallType.OCR_IMAGE, OCR_MEASURED_EXPECTED_CAD)
    _ocr_cost_per_image = calc_cost(tokens_in=1500, tokens_out=200, call_type=CallType.OCR_IMAGE)
    selected_count = len(rows)
    remaining_after_limit = max(0, total_pending - selected_count)
    _ocr_projected = _ocr_cost_per_image * selected_count
    _ocr_projected_gate = _ocr_reserved_per * len(rows)
    try:
        check_cost_gate(CallType.OCR_IMAGE, _ocr_projected_gate)
    except CostGateError as exc:
        msg = f"OCR batch blocked by cost gate ({selected_count} images): {exc}"
        logger.error(msg)
        if progress_cb:
            progress_cb(f"OCR: BLOCKED — {exc}")
        return {
            "ocr_processed": 0,
            "errors": [msg],
            "warnings": [],
            "blocked_by_cost_gate": True,
            "pending_total": total_pending,
            "limit": limit,
            "remaining_after_limit": total_pending,
        }

    if progress_cb:
        suffix = (
            f"; {remaining_after_limit} pending remain after this bounded batch"
            if remaining_after_limit
            else ""
        )
        progress_cb(
            f"OCR: processing {selected_count}/{total_pending} new images "
            f"(limit={limit}, est. cost ${_ocr_projected:.4f}{suffix})..."
        )

    errors:  list[str] = []
    warnings: list[str] = []
    buffer:  list[dict] = []
    success  = 0

    for img in rows:
        # Per-image gate check (accumulated actual costs may have shifted the budget)
        try:
            check_cost_gate(CallType.OCR_IMAGE, _ocr_reserved_per)
        except CostGateError as exc:
            msg = f"OCR loop aborted by cost gate after {success} images: {exc}"
            logger.error(msg)
            errors.append(msg)
            if buffer:
                _bq_insert(client, "ticket_attachment_ocr", buffer)
                buffer = []
            if progress_cb:
                progress_cb(f"OCR: ABORTED mid-batch — {exc}")
            break
        content_url  = img.content_url
        content_type = img.content_type
        ticket_id    = img.ticket_id
        size_bytes   = img.size_bytes

        image_bytes, downloaded_content_type, download_err = _download_zendesk_attachment(content_url)
        if download_err:
            buffer.append({
                "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
                "processing_status": "failed", "skip_reason": download_err,
                "processed_at": now.isoformat(),
            })
            warnings.append(f"Download failed for {content_url}: {download_err}")
            continue

        declared_mime = downloaded_content_type or content_type
        effective_mime = _sniff_attachment_mime(image_bytes, declared_mime)
        too_large = (size_bytes and size_bytes > _MAX_IMAGE_BYTES) or len(image_bytes) > _MAX_IMAGE_BYTES
        if effective_mime != "application/pdf" and too_large:
            buffer.append({
                "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
                "processing_status": "skipped", "skip_reason": "too_large",
                "processed_at": now.isoformat(),
            })
            continue

        ocr_text, ocr_err, effective_mime = _extract_ticket_attachment_text(image_bytes, declared_mime)
        if ocr_err:
            status = "skipped" if ocr_err.startswith("corrupt_") else "failed"
            buffer.append({
                "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
                "processing_status": status, "skip_reason": ocr_err,
                "processed_at": now.isoformat(),
            })
            warnings.append(f"OCR skipped for {content_url}: {ocr_err}")
        else:
            buffer.append({
                "content_url": content_url, "ticket_id": ticket_id,
                "ocr_text": ocr_text, "processing_status": "success",
                "skip_reason": None, "processed_at": now.isoformat(),
            })
            success += 1

        time.sleep(_OCR_SLEEP)

    if buffer:
        _bq_insert(client, "ticket_attachment_ocr", buffer)

    total = selected_count
    logger.info(
        "ocr_new_attachments: processed=%d success=%d warnings=%d errors=%d",
        total, success, len(warnings), len(errors),
    )
    if progress_cb:
        progress_cb(f"OCR: {success}/{total} images processed.")

    if remaining_after_limit:
        warnings.append(
            f"OCR limit reached: processed {selected_count}/{total_pending}; "
            f"{remaining_after_limit} pending images remain"
        )

    return {
        "ocr_processed": total,
        "errors": errors,
        "warnings": warnings,
        "pending_total": total_pending,
        "limit": limit,
        "remaining_after_limit": remaining_after_limit,
    }
