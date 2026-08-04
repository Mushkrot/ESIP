"""Append-only BigQuery/GCS mirror for sync run audit records."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery, storage

from app.config import settings

logger = logging.getLogger(__name__)

_BQ_TABLE = "esip_reference.sync_run_logs"
_GCS_BUCKET = "esip-reference-data"
_GCS_PREFIX = "sync_logs/runs"
_table_ready = False


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
    return json.dumps(_json_safe(value), ensure_ascii=False, default=str, separators=(",", ":"))


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _get_bq_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=settings.google_cloud_project_id)


def _get_storage_client() -> storage.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return storage.Client(project=settings.google_cloud_project_id)


def _ensure_bq_table(client: bigquery.Client) -> None:
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
        bigquery.SchemaField("mirrored_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("event_date", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("job_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("trigger", "STRING"),
        bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("started_at", "TIMESTAMP"),
        bigquery.SchemaField("finished_at", "TIMESTAMP"),
        bigquery.SchemaField("duration_sec", "INTEGER"),
        bigquery.SchemaField("started_by", "STRING"),
        bigquery.SchemaField("error_count", "INTEGER"),
        bigquery.SchemaField("error_message", "STRING"),
        bigquery.SchemaField("cursor_before", "INTEGER"),
        bigquery.SchemaField("cursor_after", "INTEGER"),
        bigquery.SchemaField("summary", "JSON"),
        bigquery.SchemaField("source_results", "JSON"),
        bigquery.SchemaField("cost_summary", "JSON"),
        bigquery.SchemaField("source_settings", "JSON"),
        bigquery.SchemaField("request", "JSON"),
        bigquery.SchemaField("last_event", "JSON"),
        bigquery.SchemaField("gcs_payload_uri", "STRING"),
        bigquery.SchemaField("schema_version", "STRING", mode="REQUIRED"),
    ]
    table = bigquery.Table(table_id, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="mirrored_at",
    )
    table.clustering_fields = ["job_id", "status", "run_id"]
    client.create_table(table, exists_ok=True)
    _table_ready = True
    logger.info("sync_run_mirror: ensured BQ table %s", table_id)


def _write_gcs_jsonl(payload: dict[str, Any]) -> str | None:
    try:
        started_at = _coerce_dt(payload.get("started_at"))
        day = started_at.strftime("%Y-%m-%d")
        job_id = str(payload.get("job_id") or "unknown")[:80]
        run_id = str(payload.get("run_id") or payload.get("id") or "unknown")[:160]
        blob_name = f"{_GCS_PREFIX}/{day}/{job_id}/{run_id}.jsonl"
        line = json.dumps(_json_safe(payload), ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        _get_storage_client().bucket(_GCS_BUCKET).blob(blob_name).upload_from_string(
            line,
            content_type="application/x-ndjson; charset=utf-8",
        )
        return f"gs://{_GCS_BUCKET}/{blob_name}"
    except Exception as exc:
        logger.warning("sync_run_mirror: GCS write failed: %s", exc)
        return None


def mirror_sync_run(run_id: str, payload: dict[str, Any]) -> None:
    """Best-effort append-only mirror. Never raise into the sync pipeline."""
    try:
        now = datetime.now(timezone.utc)
        safe_payload = _json_safe({**payload, "run_id": run_id})
        safe_payload.setdefault("schema_version", "sync_run.v1")
        safe_payload["mirrored_at"] = now.isoformat()
        safe_payload["event_date"] = _coerce_dt(safe_payload.get("started_at")).date().isoformat()

        gcs_uri = _write_gcs_jsonl(safe_payload)

        row = {
            "mirrored_at": safe_payload["mirrored_at"],
            "event_date": safe_payload["event_date"],
            "run_id": str(safe_payload.get("run_id") or ""),
            "job_id": str(safe_payload.get("job_id") or ""),
            "trigger": safe_payload.get("trigger"),
            "status": str(safe_payload.get("status") or ""),
            "started_at": safe_payload.get("started_at"),
            "finished_at": safe_payload.get("finished_at"),
            "duration_sec": safe_payload.get("duration_sec"),
            "started_by": safe_payload.get("started_by"),
            "error_count": int(safe_payload.get("error_count") or 0),
            "error_message": safe_payload.get("error_message"),
            "cursor_before": safe_payload.get("cursor_before"),
            "cursor_after": safe_payload.get("cursor_after"),
            "summary": _bq_json(safe_payload.get("summary")),
            "source_results": _bq_json(safe_payload.get("source_results")),
            "cost_summary": _bq_json(safe_payload.get("cost_summary")),
            "source_settings": _bq_json(safe_payload.get("source_settings")),
            "request": _bq_json(safe_payload.get("request")),
            "last_event": _bq_json(safe_payload.get("last_event")),
            "gcs_payload_uri": gcs_uri,
            "schema_version": safe_payload["schema_version"],
        }

        client = _get_bq_client()
        _ensure_bq_table(client)
        table_id = f"{settings.google_cloud_project_id}.{_BQ_TABLE}"
        errors = client.insert_rows_json(table_id, [row], row_ids=[run_id])
        if errors:
            logger.warning("sync_run_mirror: BQ insert errors: %s", errors[:2])
    except Exception as exc:
        logger.warning("sync_run_mirror: mirror failed for %s: %s", run_id, exc)
