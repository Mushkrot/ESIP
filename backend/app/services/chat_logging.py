"""Durable Universal Chat logging for future AI-agent analysis."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery, storage

from app.config import settings

logger = logging.getLogger(__name__)

_BQ_TABLE = "esip_reference.universal_chat_turn_logs"
_GCS_BUCKET = "esip-reference-data"
_GCS_PREFIX = "chat_logs/universal"
_table_ready = False


def _json_safe(value: Any) -> Any:
    """Return a BigQuery/GCS JSON-safe value."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _compact_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[TRUNCATED]"


def _bq_json(value: Any) -> str:
    """BigQuery JSON columns expect a JSON-encoded string via insert_rows_json."""
    return json.dumps(_json_safe(value), ensure_ascii=False, default=str, separators=(",", ":"))


def _get_bq_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=settings.google_cloud_project_id)


def _get_storage_client() -> storage.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return storage.Client(project=settings.google_cloud_project_id)


def _ensure_bq_table(client: bigquery.Client) -> None:
    """Create the Universal Chat log table on first write if it does not exist."""
    global _table_ready
    if _table_ready:
        return

    table_id = f"{settings.google_cloud_project_id}.{_BQ_TABLE}"
    try:
        client.get_table(table_id)
        _table_ready = True
        return
    except Exception:
        pass

    schema = [
        bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("event_date", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("surface", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("conversation_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("trace_id", "STRING"),
        bigquery.SchemaField("owner_uid", "STRING"),
        bigquery.SchemaField("owner_email", "STRING"),
        bigquery.SchemaField("owner_role", "STRING"),
        bigquery.SchemaField("ticket_id", "INTEGER"),
        bigquery.SchemaField("target_ticket_id", "INTEGER"),
        bigquery.SchemaField("latest_question", "STRING"),
        bigquery.SchemaField("assistant_answer", "STRING"),
        bigquery.SchemaField("messages", "JSON"),
        bigquery.SchemaField("phase1_decision", "JSON"),
        bigquery.SchemaField("policy_flags", "JSON"),
        bigquery.SchemaField("evidence_snapshot", "JSON"),
        bigquery.SchemaField("sources", "JSON"),
        bigquery.SchemaField("tickets", "JSON"),
        bigquery.SchemaField("counts", "JSON"),
        bigquery.SchemaField("latency_ms", "INTEGER"),
        bigquery.SchemaField("model", "STRING"),
        bigquery.SchemaField("backend_revision", "STRING"),
        bigquery.SchemaField("gcs_payload_uri", "STRING"),
        bigquery.SchemaField("schema_version", "STRING", mode="REQUIRED"),
    ]
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="created_at",
    )
    table.clustering_fields = ["owner_email", "conversation_id", "surface"]
    client.create_table(table, exists_ok=True)
    _table_ready = True
    logger.info("chat_logging: ensured BQ table %s", table_id)


def _write_gcs_payload(payload: dict) -> str | None:
    try:
        created = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        day = created.strftime("%Y-%m-%d")
        conversation_id = str(payload.get("conversation_id") or "unknown")[:80]
        turn_id = str(payload.get("turn_id") or "unknown")[:80]
        blob_name = f"{_GCS_PREFIX}/{day}/{conversation_id}/{turn_id}.json"
        body = json.dumps(_json_safe(payload), ensure_ascii=False, default=str, separators=(",", ":"))
        client = _get_storage_client()
        client.bucket(_GCS_BUCKET).blob(blob_name).upload_from_string(
            body,
            content_type="application/json; charset=utf-8",
        )
        return f"gs://{_GCS_BUCKET}/{blob_name}"
    except Exception as exc:
        logger.warning("chat_logging: GCS payload write failed: %s", exc)
        return None


def save_universal_chat_turn(payload: dict) -> None:
    """
    Best-effort durable Universal Chat turn logging.

    This must never break or delay the user-visible chat response materially.
    """
    try:
        now = datetime.now(timezone.utc)
        full_payload = _json_safe(dict(payload))
        full_payload.setdefault("schema_version", "universal_chat_turn.v1")
        full_payload.setdefault("surface", "universal_chat")
        full_payload.setdefault("created_at", now.isoformat())
        full_payload.setdefault("event_date", now.date().isoformat())
        full_payload.setdefault("backend_revision", os.environ.get("K_REVISION", "local"))
        full_payload.setdefault("model", settings.gemini_model)

        gcs_uri = _write_gcs_payload(full_payload)

        row = {
            "created_at": full_payload["created_at"],
            "event_date": full_payload["event_date"],
            "surface": full_payload["surface"],
            "conversation_id": str(full_payload.get("conversation_id") or ""),
            "turn_id": str(full_payload.get("turn_id") or ""),
            "trace_id": full_payload.get("trace_id"),
            "owner_uid": full_payload.get("owner_uid"),
            "owner_email": full_payload.get("owner_email"),
            "owner_role": full_payload.get("owner_role"),
            "ticket_id": full_payload.get("ticket_id"),
            "target_ticket_id": full_payload.get("target_ticket_id"),
            "latest_question": _compact_text(full_payload.get("latest_question"), 8000),
            "assistant_answer": _compact_text(full_payload.get("assistant_answer"), 16000),
            "messages": _bq_json(full_payload.get("messages")),
            "phase1_decision": _bq_json(full_payload.get("phase1_decision")),
            "policy_flags": _bq_json(full_payload.get("policy_flags")),
            "evidence_snapshot": _bq_json(full_payload.get("evidence_snapshot")),
            "sources": _bq_json(full_payload.get("sources")),
            "tickets": _bq_json(full_payload.get("tickets")),
            "counts": _bq_json(full_payload.get("counts")),
            "latency_ms": full_payload.get("latency_ms"),
            "model": full_payload.get("model"),
            "backend_revision": full_payload.get("backend_revision"),
            "gcs_payload_uri": gcs_uri,
            "schema_version": full_payload["schema_version"],
        }
        client = _get_bq_client()
        _ensure_bq_table(client)
        table_id = f"{settings.google_cloud_project_id}.{_BQ_TABLE}"
        errors = client.insert_rows_json(table_id, [row])
        if errors:
            logger.warning("chat_logging: BQ insert errors: %s", errors[:2])
    except Exception as exc:
        logger.warning("chat_logging: durable turn logging failed: %s", exc)
