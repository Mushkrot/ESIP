"""
Zendesk → BigQuery ticket export pipeline.

Exports all Zendesk tickets (and comments/attachments) into BigQuery dataset
`esip_reference` in project esip-reference-project.

The Zendesk search query uses `updated>=DATE`, which captures:
  - Tickets created in the period
  - Tickets closed/solved in the period (even if opened earlier)
  - Any ticket with activity (comments, status changes) in the period

Tables created automatically if they don't exist:
  - tickets          — one row per ticket, all structured fields
  - ticket_comments  — one row per comment (public + internal notes)
  - ticket_attachments — one row per attachment URL (images, files in comments)

Usage:
  # Full historical export with explicit start date — run once, takes 1-2 hours
  python export_tickets_bq.py --mode=full --since-date=2025-01-01

  # Resume after interruption — continues from last completed page, no duplicates
  python export_tickets_bq.py --mode=full --since-date=2025-01-01 --resume

  # Incremental update (last 2 days) — run daily via Cloud Scheduler
  python export_tickets_bq.py --mode=incremental

  # Custom date range in days
  python export_tickets_bq.py --mode=full --days=30

  # Dry run — fetch tickets but don't write to BQ (for testing)
  python export_tickets_bq.py --mode=full --days=3 --dry-run
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.sla import build_sla_fields

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "example")
ZENDESK_EMAIL     = os.getenv("ZENDESK_EMAIL")
ZENDESK_TOKEN     = os.getenv("ZENDESK_API_TOKEN")
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZENDESK_AUTH      = (f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)

GCP_PROJECT       = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "esip-reference-project")
CREDENTIALS_FILE  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
BQ_DATASET        = "esip_reference"
# Keep this as "US" for the existing dataset. Google is realigning the display
# wording for US/EU locations in 2026, but current BigQuery APIs still use "US".
BQ_LOCATION       = "US"

# Zendesk custom field IDs
CF = {
    "store_id":              2017,
    "requester_name":        91000001,
    "issue_category":        930100000009,
    "symptoms":              930100000011,
    "workaround":            930100000012,
    "next_steps":            930100000013,
    "resolution":            930100000008,
    "escalated":             930100000006,
    "next_follow_up":        930100000007,
    "brand":                 91000002,
    "brand_alt":             91000003,
    "source":                91000004,
    "caused_by":             930100000003,
    "product_type":          930100000010,
    "resolution_type":       94000000000001,
    "time_spent_sec":        930100000001,
    "time_spent_last":       930100000002,
    "vendor_dispatched":     930100000005,
    "vendor_escalation":     91000005,
}

# Root cause description textareas (per device type) — merge into one field
ROOT_CAUSE_DESC_FIELD_IDS = {
    91000101, 91000102, 91000103, 91000104, 91000105, 91000106,
    91000107, 91000108, 91000109, 91000110, 91000111, 91000112,
    91000113, 91000114, 91000115, 91000116, 91000117, 91000118,
}

# Rate limiting
ZENDESK_PAGE_SIZE   = 100      # tickets per page
COMMENTS_BATCH_SIZE = 10       # parallel comment fetches (stay well under rate limit)
SLEEP_BETWEEN_PAGES = 0.15     # seconds between ticket list pages
SLEEP_BETWEEN_COMMENTS = 0.1   # seconds between comment batches
MAX_RETRIES         = 3
RETRY_BACKOFF       = 5        # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CHECKPOINT_FILE = os.path.join(os.path.dirname(__file__), ".export_checkpoint.json")


# ---------------------------------------------------------------------------
# BigQuery schema definitions
# ---------------------------------------------------------------------------

TICKETS_SCHEMA = [
    bigquery.SchemaField("id",                      "INT64",   mode="REQUIRED"),
    bigquery.SchemaField("subject",                 "STRING"),
    bigquery.SchemaField("status",                  "STRING"),
    bigquery.SchemaField("priority",                "STRING"),
    bigquery.SchemaField("ticket_type",             "STRING"),
    bigquery.SchemaField("ticket_form_id",          "INT64"),
    bigquery.SchemaField("ticket_form_name",        "STRING"),
    bigquery.SchemaField("brand",                   "STRING"),
    bigquery.SchemaField("store_id",                "STRING"),
    bigquery.SchemaField("org_id",                  "INT64"),
    bigquery.SchemaField("org_name",                "STRING"),
    bigquery.SchemaField("created_at",              "TIMESTAMP"),
    bigquery.SchemaField("updated_at",              "TIMESTAMP"),
    bigquery.SchemaField("solved_at",               "TIMESTAMP"),
    bigquery.SchemaField("resolution_hours",        "FLOAT64"),
    bigquery.SchemaField("sla_policy_name",         "STRING"),
    bigquery.SchemaField("resolution_sla_target_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_elapsed_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_breached", "BOOL"),
    bigquery.SchemaField("resolution_sla_status",   "STRING"),
    bigquery.SchemaField("time_spent_sec",          "INT64"),
    bigquery.SchemaField("via_channel",             "STRING"),
    bigquery.SchemaField("source",                  "STRING"),
    bigquery.SchemaField("assignee_id",             "INT64"),
    bigquery.SchemaField("assignee_email",          "STRING"),
    bigquery.SchemaField("assignee_name",           "STRING"),
    bigquery.SchemaField("assignee_group_id",       "INT64"),
    bigquery.SchemaField("assignee_group_name",     "STRING"),
    bigquery.SchemaField("requester_name",          "STRING"),
    bigquery.SchemaField("issue_category",          "STRING"),
    bigquery.SchemaField("product_type",            "STRING"),
    bigquery.SchemaField("caused_by",               "STRING"),
    bigquery.SchemaField("resolution_type",         "STRING"),
    bigquery.SchemaField("symptoms",                "STRING"),
    bigquery.SchemaField("workaround",              "STRING"),
    bigquery.SchemaField("next_steps",              "STRING"),
    bigquery.SchemaField("resolution",              "STRING"),
    bigquery.SchemaField("root_cause_description",  "STRING"),
    bigquery.SchemaField("escalated",               "BOOL"),
    bigquery.SchemaField("vendor_dispatched",       "BOOL"),
    bigquery.SchemaField("vendor_escalation",       "BOOL"),
    bigquery.SchemaField("tags",                    "STRING",  mode="REPEATED"),
    bigquery.SchemaField("comment_count",           "INT64"),
    bigquery.SchemaField("internal_comment_count",  "INT64"),
    bigquery.SchemaField("public_comment_count",    "INT64"),
    bigquery.SchemaField("attachment_count",        "INT64"),
    bigquery.SchemaField("full_text",               "STRING"),
    bigquery.SchemaField("exported_at",             "TIMESTAMP"),
]

COMMENTS_SCHEMA = [
    bigquery.SchemaField("ticket_id",       "INT64",     mode="REQUIRED"),
    bigquery.SchemaField("comment_id",      "INT64",     mode="REQUIRED"),
    bigquery.SchemaField("created_at",      "TIMESTAMP"),
    bigquery.SchemaField("is_internal",     "BOOL"),
    bigquery.SchemaField("author_id",       "INT64"),
    bigquery.SchemaField("author_name",     "STRING"),
    bigquery.SchemaField("author_email",    "STRING"),
    bigquery.SchemaField("author_role",     "STRING"),
    bigquery.SchemaField("author_is_agent", "BOOL"),
    bigquery.SchemaField("via_channel",     "STRING"),
    bigquery.SchemaField("body",            "STRING"),
    bigquery.SchemaField("exported_at",     "TIMESTAMP"),
]

ATTACHMENTS_SCHEMA = [
    bigquery.SchemaField("ticket_id",        "INT64",   mode="REQUIRED"),
    bigquery.SchemaField("comment_id",       "INT64"),
    bigquery.SchemaField("attachment_id",    "INT64"),
    bigquery.SchemaField("file_name",        "STRING"),
    bigquery.SchemaField("content_type",     "STRING"),   # image/png, image/jpeg, etc.
    bigquery.SchemaField("size_bytes",       "INT64"),
    bigquery.SchemaField("url",              "STRING"),   # direct download URL
    bigquery.SchemaField("content_url",      "STRING"),   # Zendesk CDN URL
    bigquery.SchemaField("is_image",         "BOOL"),     # True for image/* content types
    bigquery.SchemaField("created_at",       "TIMESTAMP"),
    bigquery.SchemaField("exported_at",      "TIMESTAMP"),
]


# ---------------------------------------------------------------------------
# BigQuery client setup
# ---------------------------------------------------------------------------

def get_bq_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


def ensure_dataset_and_tables(client: bigquery.Client) -> None:
    """Create dataset and tables if they don't exist."""
    dataset_ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET)
    try:
        client.get_dataset(dataset_ref)
        log.info("Dataset %s already exists", BQ_DATASET)
    except Exception:
        ds = bigquery.Dataset(dataset_ref)
        ds.location = BQ_LOCATION
        ds.description = "ESIP — Zendesk ticket analytics"
        client.create_dataset(ds)
        log.info("Created dataset %s", BQ_DATASET)

    table_defs = {
        "tickets":              TICKETS_SCHEMA,
        "ticket_comments":      COMMENTS_SCHEMA,
        "ticket_attachments":   ATTACHMENTS_SCHEMA,
    }
    for table_name, schema in table_defs.items():
        table_ref = dataset_ref.table(table_name)
        try:
            table = client.get_table(table_ref)
            log.info("Table %s already exists", table_name)
            existing = {field.name for field in table.schema}
            additions = [field for field in schema if field.name not in existing]
            if additions:
                table.schema = list(table.schema) + additions
                client.update_table(table, ["schema"])
                log.info("Added missing columns to %s: %s", table_name, ", ".join(field.name for field in additions))
        except Exception:
            table = bigquery.Table(table_ref, schema=schema)
            client.create_table(table)
            log.info("Created table %s", table_name)


# ---------------------------------------------------------------------------
# Zendesk helpers
# ---------------------------------------------------------------------------

def _zendesk_get(url: str, params: dict = None) -> dict:
    """GET with retry on rate limit / transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(url, params=params, auth=ZENDESK_AUTH, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", RETRY_BACKOFF))
            log.warning("Rate limited — sleeping %ds", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            log.warning("Server error %d on attempt %d", r.status_code, attempt)
            time.sleep(RETRY_BACKOFF * attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}")


def fetch_incremental_page(start_time: int, cursor_url: str = None) -> dict:
    """
    Fetch one page from the Zendesk Incremental Tickets API.

    Uses cursor-based pagination — no 1000-result limit (unlike Search API).
    Returns ALL tickets updated since start_time Unix timestamp.

    Response keys: tickets[], next_page (URL), end_of_stream (bool), after_url
    """
    if cursor_url:
        # Follow the cursor URL directly (includes all params)
        return _zendesk_get(cursor_url)
    return _zendesk_get(
        f"{ZENDESK_BASE}/incremental/tickets",
        params={
            "start_time":  start_time,
            "include":     "comment_count",
        },
    )


def fetch_comments(ticket_id: int) -> list[dict]:
    """Fetch all comments for a ticket."""
    data = _zendesk_get(f"{ZENDESK_BASE}/tickets/{ticket_id}/comments")
    return data.get("comments", [])


def fetch_org_name(org_id: int, cache: dict) -> str:
    """Fetch org name with in-process cache."""
    if org_id in cache:
        return cache[org_id]
    try:
        data = _zendesk_get(f"{ZENDESK_BASE}/organizations/{org_id}")
        name = data.get("organization", {}).get("name", "")
        cache[org_id] = name
        return name
    except Exception:
        cache[org_id] = ""
        return ""


def fetch_user_name(user_id: int, cache: dict) -> str:
    """Fetch user display name with in-process cache."""
    user = fetch_user(user_id, cache)
    return (user or {}).get("name") or ""


def fetch_user(user_id: int, cache: dict) -> dict | None:
    """Fetch Zendesk user metadata with in-process cache."""
    if user_id in cache:
        return cache[user_id]
    try:
        data = _zendesk_get(f"{ZENDESK_BASE}/users/{user_id}")
        user = data.get("user") or {}
    except Exception:
        user = {}
    cache[user_id] = user or None
    return user or None


def fetch_group_name(group_id: int, cache: dict) -> str:
    """Fetch Zendesk group name with in-process cache."""
    if group_id in cache:
        return cache[group_id]
    try:
        data = _zendesk_get(f"{ZENDESK_BASE}/groups/{group_id}")
        name = data.get("group", {}).get("name", "")
        cache[group_id] = name
        return name
    except Exception:
        cache[group_id] = ""
        return ""


def fetch_ticket_form_name(form_id: int | None, cache: dict) -> str | None:
    """Fetch Zendesk ticket form display name with in-process cache."""
    if not form_id:
        return None
    if form_id in cache:
        return cache[form_id]
    try:
        data = _zendesk_get(f"{ZENDESK_BASE}/ticket_forms/{form_id}")
        form = data.get("ticket_form") or {}
        name = form.get("display_name") or form.get("name") or ""
    except Exception:
        name = ""
    cache[form_id] = name or None
    return name or None


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def _cf(ticket: dict, field_id: int, default=None):
    """Extract value of a custom field by ID."""
    for f in ticket.get("custom_fields", []):
        if f["id"] == field_id:
            return f["value"]
    return default


def _bool_cf(ticket: dict, field_id: int) -> bool:
    v = _cf(ticket, field_id)
    return bool(v) if v is not None else False


def _merge_root_cause_descriptions(ticket: dict) -> str:
    """Combine all root cause description textarea fields into one string."""
    parts = []
    for f in ticket.get("custom_fields", []):
        if f["id"] in ROOT_CAUSE_DESC_FIELD_IDS and f["value"]:
            parts.append(str(f["value"]).strip())
    return " | ".join(parts) if parts else None


def _parse_solved_at(ticket: dict) -> str | None:
    """For solved/closed tickets, use updated_at as proxy for solved_at."""
    status = ticket.get("status", "")
    if status in ("solved", "closed"):
        ts = ticket.get("updated_at")
        if ts:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    return None


def _parse_ts(ts_str: str | None) -> str | None:
    """Parse ISO timestamp and return as string (BigQuery insert_rows_json requires strings)."""
    if not ts_str:
        return None
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return dt.isoformat()


def _extract_attachments_from_comments(
    ticket_id: int,
    comments: list[dict],
    now: datetime,
) -> list[dict]:
    """
    Extract all attachment metadata from comments.

    Zendesk stores attachments in two ways:
      1. comment['attachments'] — files directly attached to the comment
      2. Inline images embedded in comment body HTML (img src URLs)

    We capture both. For inline images, we extract the URL from the HTML.
    This is critical: engineers frequently paste screenshots directly into
    internal notes — these show up as inline images, not as attachments.
    """
    rows = []
    image_mime_types = {"image/png", "image/jpeg", "image/gif", "image/webp",
                        "image/bmp", "image/tiff", "image/svg+xml"}

    for comment in comments:
        comment_id = comment.get("id")
        created_at = _parse_ts(comment.get("created_at"))

        # 1. Direct attachments
        for att in comment.get("attachments", []):
            content_type = att.get("content_type", "")
            rows.append({
                "ticket_id":     ticket_id,
                "comment_id":    comment_id,
                "attachment_id": att.get("id"),
                "file_name":     att.get("file_name"),
                "content_type":  content_type,
                "size_bytes":    att.get("size", 0),
                "url":           att.get("url"),           # API endpoint URL
                "content_url":   att.get("content_url"),   # CDN direct URL
                "is_image":      content_type in image_mime_types,
                "created_at":    created_at,
                "exported_at":   now.isoformat(),
            })

        # 2. Inline images from comment body HTML
        # IMPORTANT: use html_body (not body) — inline <img> tags only appear in html_body.
        # comment.get("body") returns plain text which never contains <img> tags.
        html_body = comment.get("html_body") or comment.get("body") or ""
        # Match <img src="..."> patterns (Zendesk CDN URLs for inline screenshots)
        inline_urls = re.findall(
            r'<img[^>]+src=["\']([^"\']+zen(?:desk)?[^"\']*)["\']',
            html_body,
            re.IGNORECASE,
        )
        for url in inline_urls:
            # Avoid duplicating what's already in direct attachments
            already_captured = any(r["content_url"] == url for r in rows)
            if not already_captured:
                file_name = url.rsplit("/", 1)[-1].split("?")[0]
                rows.append({
                    "ticket_id":     ticket_id,
                    "comment_id":    comment_id,
                    "attachment_id": None,       # inline — no Zendesk attachment ID
                    "file_name":     file_name,
                    "content_type":  "image/unknown",
                    "size_bytes":    None,
                    "url":           url,
                    "content_url":   url,
                    "is_image":      True,
                    "created_at":    created_at,
                    "exported_at":   now.isoformat(),
                })

    return rows


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def build_ticket_row(
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
    solved_at  = _parse_solved_at(ticket)

    resolution_hours = None
    if created_at and solved_at:
        from datetime import datetime
        c = datetime.fromisoformat(created_at)
        s = datetime.fromisoformat(solved_at)
        resolution_hours = (s - c).total_seconds() / 3600

    org_id   = ticket.get("organization_id")
    org_name = fetch_org_name(org_id, org_cache) if org_id else None

    assignee_id   = ticket.get("assignee_id")
    assignee_user = fetch_user(assignee_id, user_cache) if assignee_id else None
    assignee_name = (assignee_user or {}).get("name")
    assignee_email = (assignee_user or {}).get("email")
    assignee_group_id = ticket.get("group_id")
    assignee_group_name = fetch_group_name(assignee_group_id, group_cache) if assignee_group_id else None
    ticket_form_name = fetch_ticket_form_name(ticket.get("ticket_form_id"), form_cache)
    sla_fields = build_sla_fields(ticket, ticket_form_name=ticket_form_name, now=now)

    internal = [c for c in comments if c.get("via", {}).get("channel") == "private" or
                c.get("public") is False or c.get("type") == "Comment" and not c.get("public", True)]
    # Zendesk: public=False → internal note
    internal = [c for c in comments if not c.get("public", True)]
    public   = [c for c in comments if c.get("public", True)]

    # full_text for semantic search: key fields + last 10 internal notes
    full_text_parts = [
        ticket.get("subject", ""),
        _cf(ticket, CF["symptoms"]) or "",
        _cf(ticket, CF["resolution"]) or "",
        _merge_root_cause_descriptions(ticket) or "",
    ]
    for c in internal[-10:]:
        body = c.get("body") or c.get("plain_body") or ""
        full_text_parts.append(body[:500])
    full_text = "\n".join(p for p in full_text_parts if p).strip()

    # Brand: prefer dedicated custom field, fall back to tag-based detection,
    # then fall back to org_name prefix matching.
    # Slug values must match keys in bigquery.py _BRAND_DISPLAY for display conversion.
    brand = _cf(ticket, CF["brand"]) or _cf(ticket, CF["brand_alt"])
    if not brand:
        tags = ticket.get("tags", [])
        for tag in tags:
            if tag in (
                "Cedar Grill", "Metro Burger", "Frontier Kitchen", "Lakeside Tavern",
                "fresh_market", "foundry_burger", "garden_italian",
                "riverstone_hospitality", "Garden Table", "City Fries", "maple_roastery",
                "Piazza Kitchen", "Piazza Kitchen_",
            ):
                brand = tag.rstrip("_")  # normalize "Piazza Kitchen_" → "Piazza Kitchen"
                break
    if not brand and org_name:
        org_lower = org_name.lower()
        if "piazza kitchen" in org_lower:
            brand = "Piazza Kitchen"
        elif "burger" in org_lower and "priest" in org_lower:
            brand = "foundry_burger"

    # Vendor flags
    vendor_dispatched = bool(_cf(ticket, CF["vendor_dispatched"]))
    vendor_escalation = bool(_cf(ticket, CF["vendor_escalation"]))

    return {
        "id":                     ticket["id"],
        "subject":                ticket.get("subject"),
        "status":                 ticket.get("status"),
        "priority":               sla_fields["priority"],
        "ticket_type":            sla_fields["ticket_type"],
        "ticket_form_id":         sla_fields["ticket_form_id"],
        "ticket_form_name":       sla_fields["ticket_form_name"],
        "brand":                  brand,
        "store_id":               str(_cf(ticket, CF["store_id"]) or ""),
        "org_id":                 org_id,
        "org_name":               org_name,
        "created_at":             created_at,   # ISO string
        "updated_at":             updated_at,   # ISO string
        "solved_at":              solved_at,    # ISO string or None
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
        "root_cause_description": _merge_root_cause_descriptions(ticket),
        "escalated":              _bool_cf(ticket, CF["escalated"]),
        "vendor_dispatched":      vendor_dispatched,
        "vendor_escalation":      vendor_escalation,
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


def build_comment_rows(
    ticket_id: int,
    comments: list[dict],
    now: datetime,
    user_cache: dict,
) -> list[dict]:
    rows = []
    for c in comments:
        author_id = c.get("author_id")
        author = fetch_user(author_id, user_cache) if author_id else None
        body = c.get("body") or c.get("plain_body") or ""
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
            "body":            body,
            "exported_at":     now.isoformat(),
        })
    return rows


# ---------------------------------------------------------------------------
# BigQuery insert helpers
# ---------------------------------------------------------------------------

def truncate_tables(client: bigquery.Client) -> None:
    """Truncate all 3 tables for a clean fresh export."""
    for table in ("tickets", "ticket_comments", "ticket_attachments"):
        table_id = f"`{GCP_PROJECT}.{BQ_DATASET}.{table}`"
        log.info("Truncating %s...", table_id)
        client.query(f"TRUNCATE TABLE {table_id}").result()
    log.info("All tables truncated — clean slate for full export")


def upsert_tickets(client: bigquery.Client, rows: list[dict], dry_run: bool) -> None:
    if not rows:
        return
    if dry_run:
        log.info("[DRY RUN] Would insert %d ticket rows", len(rows))
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.tickets"
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        log.error("BQ insert errors (tickets): %s", errors[:3])
    else:
        log.info("Inserted %d tickets", len(rows))


def upsert_comments(client: bigquery.Client, rows: list[dict], dry_run: bool) -> None:
    if not rows:
        return
    if dry_run:
        log.info("[DRY RUN] Would insert %d comment rows", len(rows))
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.ticket_comments"
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        log.error("BQ insert errors (comments): %s", errors[:3])


def upsert_attachments(client: bigquery.Client, rows: list[dict], dry_run: bool) -> None:
    if not rows:
        return
    if dry_run:
        log.info("[DRY RUN] Would insert %d attachment rows", len(rows))
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments"
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        log.error("BQ insert errors (attachments): %s", errors[:3])


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {}


def _save_checkpoint(data: dict) -> None:
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def run(mode: str, days: int, dry_run: bool, resume: bool = False,
        since_date: str = None) -> None:
    now = datetime.now(timezone.utc)

    # Determine start date
    if since_date:
        since = datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc)
    else:
        since = now - timedelta(days=days)

    # Resume from checkpoint if requested
    checkpoint = _load_checkpoint() if resume else {}
    if checkpoint and resume:
        saved_since = checkpoint.get("since")
        if saved_since:
            since = datetime.fromisoformat(saved_since)
        start_page     = checkpoint.get("next_page", 1)
        total_tickets  = checkpoint.get("total_tickets", 0)
        total_comments = checkpoint.get("total_comments", 0)
        total_attachments = checkpoint.get("total_attachments", 0)
        log.info("RESUMING from checkpoint: next_page=%d, tickets_so_far=%d",
                 start_page, total_tickets)
    else:
        start_page        = 1
        total_tickets     = 0
        total_comments    = 0
        total_attachments = 0

    if mode == "incremental":
        log.info("INCREMENTAL mode — fetching tickets updated since %s", since.date())
    else:
        log.info("FULL mode — fetching tickets updated since %s", since.date())

    # BigQuery setup
    if not dry_run:
        log.info("Connecting to BigQuery...")
        client = get_bq_client()
        ensure_dataset_and_tables(client)
        # Truncate tables only on fresh (non-resume) full export
        if mode == "full" and not resume and start_page == 1:
            log.info("Fresh full export — truncating existing data...")
            truncate_tables(client)
    else:
        client = None
        log.info("[DRY RUN] Skipping BigQuery connection")

    org_cache:  dict = {}
    user_cache: dict = {}
    group_cache: dict = {}
    form_cache: dict = {}

    ticket_rows     = []
    comment_rows    = []
    attachment_rows = []

    # Incremental API uses Unix timestamp + cursor URL for pagination
    start_time_unix = int(since.timestamp())
    cursor_url      = checkpoint.get("next_cursor_url") if (checkpoint and resume) else None
    page_num        = checkpoint.get("page_num", 0) if (checkpoint and resume) else 0

    while True:
        page_num += 1
        log.info("Fetching batch %d (cursor: %s)...", page_num,
                 "checkpoint" if cursor_url else f"start={since.date()}")
        try:
            data = fetch_incremental_page(start_time_unix, cursor_url)
        except Exception as exc:
            log.error("Failed to fetch batch %d: %s — stopping", page_num, exc)
            break

        tickets = data.get("tickets", [])
        if not tickets:
            log.info("No tickets in batch %d — done", page_num)
            break

        # Skip deleted tickets (incremental API includes them)
        tickets = [t for t in tickets if t.get("status") != "deleted"]

        for ticket in tickets:
            ticket_id = ticket["id"]

            try:
                comments = fetch_comments(ticket_id)
            except Exception as exc:
                log.warning("Failed to fetch comments for ticket %d: %s", ticket_id, exc)
                comments = []
            time.sleep(SLEEP_BETWEEN_COMMENTS)

            t_row   = build_ticket_row(ticket, comments, org_cache, user_cache, group_cache, form_cache, now)
            c_rows  = build_comment_rows(ticket_id, comments, now, user_cache)
            at_rows = _extract_attachments_from_comments(ticket_id, comments, now)

            ticket_rows.append(t_row)
            comment_rows.extend(c_rows)
            attachment_rows.extend(at_rows)

            total_tickets     += 1
            total_comments    += len(c_rows)
            total_attachments += len(at_rows)

        # Flush entire batch to BQ, then checkpoint.
        # On resume we use next_cursor_url, so no batch is ever processed twice.
        upsert_tickets(client, ticket_rows, dry_run)
        upsert_comments(client, comment_rows, dry_run)
        upsert_attachments(client, attachment_rows, dry_run)
        ticket_rows     = []
        comment_rows    = []
        attachment_rows = []

        next_cursor_url = data.get("after_url") or data.get("next_page")
        end_of_stream   = data.get("end_of_stream", False)

        log.info("Progress: %d tickets, %d comments, %d attachments (batch %d done)",
                 total_tickets, total_comments, total_attachments, page_num)

        if not dry_run:
            _save_checkpoint({
                "since":            since.isoformat(),
                "next_cursor_url":  next_cursor_url,
                "page_num":         page_num,
                "total_tickets":    total_tickets,
                "total_comments":   total_comments,
                "total_attachments": total_attachments,
                "last_updated":     now.isoformat(),
            })

        if end_of_stream or not next_cursor_url:
            log.info("End of stream — export complete")
            break

        cursor_url = next_cursor_url
        time.sleep(SLEEP_BETWEEN_PAGES)

    log.info("=" * 60)
    log.info("DONE")
    log.info("  Tickets:     %d", total_tickets)
    log.info("  Comments:    %d", total_comments)
    log.info("  Attachments: %d", total_attachments)
    log.info("  Since:       %s", since.date())
    log.info("  Mode:        %s", mode)
    log.info("  Dry run:     %s", dry_run)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zendesk → BigQuery export pipeline")
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        default="incremental",
        help="full = historical export; incremental = recent updates only",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Override date range in days (default: 365 for full, 2 for incremental)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data but do not write to BigQuery",
    )
    parser.add_argument(
        "--since-date",
        type=str,
        default=None,
        help="Explicit start date YYYY-MM-DD (overrides --days). Fetches all tickets updated on or after this date.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint — continues from the next unprocessed page, no duplicates.",
    )
    args = parser.parse_args()

    if args.days is None:
        args.days = 365 if args.mode == "full" else 2

    run(
        mode=args.mode,
        days=args.days,
        dry_run=args.dry_run,
        resume=args.resume,
        since_date=args.since_date,
    )
