"""Append-only ESIP impact telemetry events."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery

from app.config import settings
from app.services import reference_runtime

logger = logging.getLogger(__name__)

TABLE = "esip_reference.esip_impact_events"
SCHEMA_VERSION = "esip_impact_event.v1"

ALLOWED_EVENT_TYPES = {
    "ticket_page_opened",
    "zaf_sidebar_opened",
    "zaf_modal_opened",
    "summary_viewed",
    "summary_generated",
    "customer_risk_viewed",
    "customer_risk_refreshed",
    "similar_tickets_viewed",
    "similar_ticket_opened",
    "source_link_clicked",
    "document_link_clicked",
    "ticket_link_clicked",
    "answer_copied",
    "answer_feedback",
}

ALLOWED_SURFACES = {"web", "zaf_sidebar", "zaf_modal", "universal_chat"}

_table_ready = False


def _get_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=settings.google_cloud_project_id)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _bq_json(value: Any) -> str:
    return json.dumps(_json_safe(value or {}), ensure_ascii=False, default=str, separators=(",", ":"))


def _clean_text(value: Any, limit: int = 500) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    return text[:limit]


def _clean_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def ensure_table(client: bigquery.Client | None = None) -> None:
    """Create the impact event table if needed."""
    global _table_ready
    if _table_ready:
        return
    client = client or _get_client()
    table_id = f"{settings.google_cloud_project_id}.{TABLE}"
    try:
        client.get_table(table_id)
        _table_ready = True
        return
    except Exception:
        pass

    schema = [
        bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("event_date", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("surface", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("workflow", "STRING"),
        bigquery.SchemaField("owner_uid", "STRING"),
        bigquery.SchemaField("owner_email", "STRING"),
        bigquery.SchemaField("owner_role", "STRING"),
        bigquery.SchemaField("ticket_id", "INT64"),
        bigquery.SchemaField("target_ticket_id", "INT64"),
        bigquery.SchemaField("conversation_id", "STRING"),
        bigquery.SchemaField("turn_id", "STRING"),
        bigquery.SchemaField("trace_id", "STRING"),
        bigquery.SchemaField("object_type", "STRING"),
        bigquery.SchemaField("object_id", "STRING"),
        bigquery.SchemaField("object_label", "STRING"),
        bigquery.SchemaField("target_url", "STRING"),
        bigquery.SchemaField("metadata", "JSON"),
        bigquery.SchemaField("schema_version", "STRING", mode="REQUIRED"),
    ]
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="created_at",
    )
    table.clustering_fields = ["owner_email", "ticket_id", "event_type", "surface"]
    client.create_table(table, exists_ok=True)
    _table_ready = True
    logger.info("impact_telemetry: ensured BQ table %s", table_id)


def workflow_for_event(event_type: str, workflow: str | None = None) -> str:
    if workflow:
        return _clean_text(workflow, 80) or "Other"
    if event_type in {"ticket_page_opened"}:
        return "Ticket page"
    if event_type in {"zaf_sidebar_opened", "zaf_modal_opened"}:
        return "Zendesk app"
    if event_type in {"summary_viewed", "summary_generated"}:
        return "Summary"
    if event_type in {"customer_risk_viewed", "customer_risk_refreshed"}:
        return "Customer Risk"
    if event_type in {"similar_tickets_viewed", "similar_ticket_opened"}:
        return "Similar cases"
    if event_type in {"source_link_clicked", "document_link_clicked", "ticket_link_clicked"}:
        return "Source clicks"
    if event_type in {"answer_copied", "answer_feedback"}:
        return "Answer feedback"
    return "Other"


def save_event(*, event: dict[str, Any], user: dict[str, Any]) -> bool:
    """Best-effort event insert. Returns False on validation/write failure."""
    event_type = _clean_text(event.get("event_type"), 80)
    if event_type not in ALLOWED_EVENT_TYPES:
        logger.info("impact_telemetry: rejected unknown event_type=%r", event_type)
        return False
    surface = _clean_text(event.get("surface"), 80) or "web"
    if surface not in ALLOWED_SURFACES:
        surface = "web"

    now = datetime.now(timezone.utc)
    email = str(user.get("email") or "").strip().lower()
    row = {
        "event_id": uuid.uuid4().hex,
        "created_at": now.isoformat(),
        "event_date": now.date().isoformat(),
        "event_type": event_type,
        "surface": surface,
        "workflow": workflow_for_event(event_type, event.get("workflow")),
        "owner_uid": _clean_text(user.get("uid")),
        "owner_email": email or None,
        "owner_role": _clean_text(user.get("effective_role") or user.get("role") or user.get("actual_role")),
        "ticket_id": _clean_int(event.get("ticket_id")),
        "target_ticket_id": _clean_int(event.get("target_ticket_id")),
        "conversation_id": _clean_text(event.get("conversation_id"), 180),
        "turn_id": _clean_text(event.get("turn_id"), 180),
        "trace_id": _clean_text(event.get("trace_id"), 80),
        "object_type": _clean_text(event.get("object_type"), 80),
        "object_id": _clean_text(event.get("object_id"), 240),
        "object_label": _clean_text(event.get("object_label"), 500),
        "target_url": _clean_text(event.get("target_url"), 1200),
        "metadata": _bq_json(event.get("metadata")),
        "schema_version": SCHEMA_VERSION,
    }
    if settings.runtime_profile == "reference":
        reference_runtime.add_impact_event(row)
        return True
    try:
        client = _get_client()
        ensure_table(client)
        table_id = f"{settings.google_cloud_project_id}.{TABLE}"
        errors = client.insert_rows_json(table_id, [row])
        if errors:
            logger.warning("impact_telemetry: BQ insert errors: %s", errors[:2])
            return False
        return True
    except Exception as exc:
        logger.warning("impact_telemetry: event write failed: %s", exc)
        return False
