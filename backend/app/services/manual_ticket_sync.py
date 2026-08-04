"""
Durable manual Zendesk ticket catch-up runner.

This service is intentionally separate from the nightly /api/admin/sync path.
It is for controlled backlog catch-up batches where work must survive a
lost terminal, interrupted agent session, Cloud Run revision change, or manual
stop without redoing already completed OCR/embedding work.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery

from app.config import settings
from app.services import embeddings as emb_service
from app.services import firestore
from app.services.bigquery import _get_client
from app.services.bq_sync import (
    _GCS_BUCKET,
    _GCS_TICKETS_PREFIX,
    GCP_PROJECT,
    BQ_DATASET,
    _MAX_IMAGE_BYTES,
    _OCR_SLEEP,
    _get_gcs_client,
    _attachments_from_comments,
    _bq_delete_tickets,
    _bq_insert,
    _build_comment_rows,
    _build_ticket_row,
    _download_zendesk_attachment,
    _extract_ticket_attachment_text,
    _sniff_attachment_mime,
    _zd_get,
    trigger_vertex_tickets_import,
    upload_ticket_to_gcs,
)
from app.services.cost_tracker import (
    CallType,
    CostGateError,
    OCR_MEASURED_EXPECTED_CAD,
    calc_cost,
    check_cost_gate,
    get_gate_reserve,
)
from app.services.ticket_document import generate_ticket_document

logger = logging.getLogger(__name__)

RUNS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.manual_ticket_sync_runs"
ITEMS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.manual_ticket_sync_items"
OCR_ITEMS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.manual_ticket_sync_ocr_items"
EVENTS_TABLE = f"{GCP_PROJECT}.{BQ_DATASET}.manual_ticket_sync_events"

_DOC_WORKERS = 20
_GCS_WORKERS = 30
_EMBED_BATCH = 100
_BQ_DML_RESULT_TIMEOUT_SECONDS = 45
_OCR_ITEM_TIMEOUT_SECONDS = 90


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts() -> str:
    return _utc_now().isoformat()


def _run_id() -> str:
    return "manual_zd_" + _utc_now().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]


def _ids_csv(ticket_ids: list[int]) -> str:
    if not ticket_ids:
        raise ValueError("ticket_ids cannot be empty")
    return ",".join(str(int(tid)) for tid in sorted(set(ticket_ids)))


def _bq_param_type(field: str, value: Any) -> str:
    if field.endswith("_at") or field in {"final_cursor_at"}:
        return "TIMESTAMP"
    if field in {
        "base_cursor",
        "final_cursor",
        "total_discovered",
        "total_done",
        "total_errors",
        "last_stream_position",
        "last_ticket_id",
        "stream_position",
        "ticket_id",
        "retry_count",
        "attempt_count",
    }:
        return "INT64"
    if field in {"stop_requested", "inventory_complete"}:
        return "BOOL"
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    return "STRING"


def _bq_param_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field.endswith("_at") and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def ensure_tables() -> None:
    """Create manual sync tracking tables if they do not already exist."""
    client = _get_client()

    runs = bigquery.Table(
        RUNS_TABLE,
        schema=[
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
            bigquery.SchemaField("heartbeat_at", "TIMESTAMP"),
            bigquery.SchemaField("base_cursor", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("final_cursor", "INT64"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("stop_requested", "BOOL"),
            bigquery.SchemaField("inventory_complete", "BOOL"),
            bigquery.SchemaField("total_discovered", "INT64"),
            bigquery.SchemaField("total_done", "INT64"),
            bigquery.SchemaField("total_errors", "INT64"),
            bigquery.SchemaField("last_stream_position", "INT64"),
            bigquery.SchemaField("last_ticket_id", "INT64"),
            bigquery.SchemaField("note", "STRING"),
        ],
    )
    runs.time_partitioning = bigquery.TimePartitioning(field="created_at")
    runs.clustering_fields = ["run_id", "status"]
    runs.description = "Durable manual Zendesk ticket catch-up run summary."
    client.create_table(runs, exists_ok=True)

    items = bigquery.Table(
        ITEMS_TABLE,
        schema=[
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("stream_position", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("zendesk_updated_at", "TIMESTAMP"),
            bigquery.SchemaField("discovered_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("ticket_written_at", "TIMESTAMP"),
            bigquery.SchemaField("ocr_done_at", "TIMESTAMP"),
            bigquery.SchemaField("embedded_at", "TIMESTAMP"),
            bigquery.SchemaField("gcs_uploaded_at", "TIMESTAMP"),
            bigquery.SchemaField("vertex_import_operation", "STRING"),
            bigquery.SchemaField("verified_at", "TIMESTAMP"),
            bigquery.SchemaField("batch_id", "STRING"),
            bigquery.SchemaField("error_phase", "STRING"),
            bigquery.SchemaField("error_message", "STRING"),
            bigquery.SchemaField("retry_count", "INT64"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
        ],
    )
    items.time_partitioning = bigquery.TimePartitioning(field="discovered_at")
    items.clustering_fields = ["run_id", "status", "ticket_id"]
    items.description = "Per-ticket durable state for manual Zendesk catch-up."
    client.create_table(items, exists_ok=True)

    ocr_items = bigquery.Table(
        OCR_ITEMS_TABLE,
        schema=[
            bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
            bigquery.SchemaField("content_url", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("processing_status", "STRING"),
            bigquery.SchemaField("skip_reason", "STRING"),
            bigquery.SchemaField("attempt_count", "INT64"),
            bigquery.SchemaField("last_error", "STRING"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
            bigquery.SchemaField("processed_at", "TIMESTAMP"),
        ],
    )
    ocr_items.time_partitioning = bigquery.TimePartitioning(field="created_at")
    ocr_items.clustering_fields = ["run_id", "status", "ticket_id"]
    ocr_items.description = "Per-attachment OCR state for manual Zendesk catch-up."
    client.create_table(ocr_items, exists_ok=True)

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
    events.description = "Append-only operational events for manual Zendesk catch-up."
    client.create_table(events, exists_ok=True)


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
    errors = _get_client().insert_rows_json(EVENTS_TABLE, [row])
    if errors:
        logger.warning("manual sync event insert failed: %s", errors[:1])


def _insert_run(run_id: str, start_time: int) -> None:
    _get_client().query(
        f"""
        INSERT INTO `{RUNS_TABLE}` (
          run_id, created_at, updated_at, heartbeat_at, base_cursor, status,
          stop_requested, inventory_complete, total_discovered, total_done,
          total_errors, note
        )
        VALUES (
          @run_id, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), @base_cursor,
          'inventory_running', FALSE, FALSE, 0, 0, 0,
          'Manual Zendesk ticket catch-up inventory; production cursor not advanced.'
        )
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("base_cursor", "INT64", int(start_time)),
        ]),
    ).result()


def _set_run_fields(run_id: str, fields: dict[str, Any]) -> None:
    assignments = ["updated_at = CURRENT_TIMESTAMP()"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
    ]
    for idx, (key, value) in enumerate(fields.items()):
        name = f"v{idx}"
        assignments.append(f"{key} = @{name}")
        params.append(bigquery.ScalarQueryParameter(
            name,
            _bq_param_type(key, value),
            _bq_param_value(key, value),
        ))
    _get_client().query(
        f"UPDATE `{RUNS_TABLE}` SET {', '.join(assignments)} WHERE run_id = @run_id",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()


def _heartbeat(run_id: str, *, status: str | None = None) -> None:
    fields: dict[str, Any] = {"heartbeat_at": _ts()}
    if status:
        fields["status"] = status
    _set_run_fields(run_id, fields)


def _stop_requested(run_id: str) -> bool:
    rows = list(_get_client().query(
        f"SELECT COALESCE(stop_requested, FALSE) AS stop_requested FROM `{RUNS_TABLE}` WHERE run_id = @run_id",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result())
    return bool(rows and rows[0].stop_requested)


def request_stop(run_id: str) -> dict[str, Any]:
    """Ask a running manual batch to stop after its current atomic unit."""
    ensure_tables()
    _set_run_fields(run_id, {"stop_requested": True, "status": "stop_requested"})
    _event(run_id, "INFO", "control", "Stop requested")
    return status(run_id)


def reconcile_existing(
    *,
    run_id: str,
    through_position: int,
    limit: int | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Reconcile manifest rows that were processed by the older manual method.

    A row is marked done only when the ticket exists in BQ, the embedding exists,
    scoped OCR has no remaining pending image URLs, and the Agent Builder GCS
    ticket document exists. Missing GCS docs become gcs_partial so the next batch
    refreshes only that final phase instead of redoing all earlier work.
    """
    ensure_tables()
    client = _get_client()
    rows = list(client.query(
        f"""
        SELECT ticket_id, stream_position
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
          AND stream_position <= @through_position
          AND status IN ('pending', 'processing_ticket', 'ticket_written', 'ocr_done', 'gcs_partial', 'error_retryable')
        ORDER BY stream_position
        {f"LIMIT {int(limit)}" if limit else ""}
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("through_position", "INT64", through_position),
        ]),
    ).result())
    ticket_ids = [int(r.ticket_id) for r in rows]
    if not ticket_ids:
        return status(run_id)
    if progress_cb:
        progress_cb(f"Reconcile: selected {len(ticket_ids)} manifest rows")

    ids = _ids_csv(ticket_ids)
    bq_rows = list(client.query(f"""
    SELECT
      i.ticket_id,
      t.id IS NOT NULL AS ticket_exists,
      te.ticket_id IS NOT NULL AS embedding_exists,
      COUNTIF(a.is_image = TRUE AND a.content_url IS NOT NULL AND o.content_url IS NULL) AS pending_ocr
    FROM `{ITEMS_TABLE}` i
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.tickets` t ON i.ticket_id = t.id
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` te ON i.ticket_id = te.ticket_id
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a ON i.ticket_id = a.ticket_id
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o ON a.content_url = o.content_url
    WHERE i.run_id = @run_id
      AND i.ticket_id IN ({ids})
    GROUP BY i.ticket_id, ticket_exists, embedding_exists
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
    ])).result())
    state = {
        int(r.ticket_id): {
            "ticket_exists": bool(r.ticket_exists),
            "embedding_exists": bool(r.embedding_exists),
            "pending_ocr": int(r.pending_ocr or 0),
        }
        for r in bq_rows
    }
    gcs_exists = _gcs_ticket_docs_exist(ticket_ids)

    done_ids: list[int] = []
    gcs_partial_ids: list[int] = []
    ticket_written_ids: list[int] = []
    left_pending_ids: list[int] = []
    now = _ts()
    for idx, tid in enumerate(ticket_ids, start=1):
        item = state.get(tid, {})
        if item.get("ticket_exists") and item.get("embedding_exists") and item.get("pending_ocr") == 0 and gcs_exists.get(tid):
            done_ids.append(tid)
        elif item.get("ticket_exists") and item.get("embedding_exists") and item.get("pending_ocr") == 0:
            gcs_partial_ids.append(tid)
        elif item.get("ticket_exists"):
            ticket_written_ids.append(tid)
        else:
            left_pending_ids.append(tid)

        if progress_cb and (idx % 100 == 0 or idx == len(ticket_ids)):
            progress_cb(f"Reconcile: {idx}/{len(ticket_ids)} checked")

    _bulk_mark_reconciled(
        run_id=run_id,
        done_ids=done_ids,
        gcs_partial_ids=gcs_partial_ids,
        ticket_written_ids=ticket_written_ids,
        timestamp=now,
    )
    _refresh_run_counts(run_id)
    _event(run_id, "INFO", "reconcile", "Existing work reconciled", details={
        "through_position": through_position,
        "checked": len(ticket_ids),
        "reconciled": len(done_ids),
        "gcs_partial": len(gcs_partial_ids),
        "ticket_written": len(ticket_written_ids),
        "left_pending": len(left_pending_ids),
    })
    return status(run_id)


def _bulk_mark_reconciled(
    *,
    run_id: str,
    done_ids: list[int],
    gcs_partial_ids: list[int],
    ticket_written_ids: list[int],
    timestamp: str,
) -> None:
    updates: list[dict[str, Any]] = []
    for tid in done_ids:
        updates.append({
            "ticket_id": tid,
            "status": "done",
            "ticket_written_at": timestamp,
            "ocr_done_at": timestamp,
            "embedded_at": timestamp,
            "gcs_uploaded_at": timestamp,
            "verified_at": timestamp,
            "error_phase": "",
            "error_message": "",
        })
    for tid in gcs_partial_ids:
        updates.append({
            "ticket_id": tid,
            "status": "gcs_partial",
            "ticket_written_at": timestamp,
            "ocr_done_at": timestamp,
            "embedded_at": timestamp,
            "gcs_uploaded_at": None,
            "verified_at": None,
            "error_phase": "gcs_upload",
            "error_message": "GCS ticket document is missing during reconcile",
        })
    for tid in ticket_written_ids:
        updates.append({
            "ticket_id": tid,
            "status": "ticket_written",
            "ticket_written_at": timestamp,
            "ocr_done_at": None,
            "embedded_at": None,
            "gcs_uploaded_at": None,
            "verified_at": None,
            "error_phase": "",
            "error_message": "",
        })
    if not updates:
        return

    payload = json.dumps(updates, default=str)
    sql = f"""
    UPDATE `{ITEMS_TABLE}` T
    SET
      status = S.status,
      ticket_written_at = S.ticket_written_at,
      ocr_done_at = S.ocr_done_at,
      embedded_at = S.embedded_at,
      gcs_uploaded_at = S.gcs_uploaded_at,
      verified_at = S.verified_at,
      error_phase = S.error_phase,
      error_message = S.error_message,
      updated_at = CURRENT_TIMESTAMP()
    FROM (
      SELECT *
      FROM UNNEST(JSON_QUERY_ARRAY(@payload)) raw,
      UNNEST([STRUCT(
        CAST(JSON_VALUE(raw, '$.ticket_id') AS INT64) AS ticket_id,
        JSON_VALUE(raw, '$.status') AS status,
        SAFE.TIMESTAMP(JSON_VALUE(raw, '$.ticket_written_at')) AS ticket_written_at,
        SAFE.TIMESTAMP(JSON_VALUE(raw, '$.ocr_done_at')) AS ocr_done_at,
        SAFE.TIMESTAMP(JSON_VALUE(raw, '$.embedded_at')) AS embedded_at,
        SAFE.TIMESTAMP(JSON_VALUE(raw, '$.gcs_uploaded_at')) AS gcs_uploaded_at,
        SAFE.TIMESTAMP(JSON_VALUE(raw, '$.verified_at')) AS verified_at,
        JSON_VALUE(raw, '$.error_phase') AS error_phase,
        JSON_VALUE(raw, '$.error_message') AS error_message
      )])
    ) S
    WHERE T.run_id = @run_id AND T.ticket_id = S.ticket_id
    """
    _get_client().query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("payload", "STRING", payload),
        ]),
    ).result()


def _gcs_ticket_docs_exist(ticket_ids: list[int]) -> dict[int, bool]:
    client = _get_gcs_client()
    bucket = client.bucket(_GCS_BUCKET)
    wanted = {f"{_GCS_TICKETS_PREFIX}/{tid}.txt": tid for tid in ticket_ids}
    found: set[int] = set()

    # Bulk-listing avoids one network request per ticket. Stop early once all
    # requested ticket docs are found.
    for blob in client.list_blobs(bucket, prefix=f"{_GCS_TICKETS_PREFIX}/", page_size=1000):
        ticket_id = wanted.get(blob.name)
        if ticket_id is not None:
            found.add(ticket_id)
            if len(found) == len(wanted):
                break

    return {tid: tid in found for tid in ticket_ids}


def create_inventory(
    *,
    start_time: int | None = None,
    run_id: str | None = None,
    limit: int | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Materialize the Zendesk incremental stream into a durable manifest.

    This only fetches Zendesk stream metadata and writes manifest rows. It does
    not write ticket data, OCR images, embeddings, GCS docs, or production cursor.
    """
    ensure_tables()
    client = _get_client()
    if start_time is None:
        start_time = firestore.get_sync_cursor()
    if start_time is None:
        raise ValueError("No start_time provided and Firestore production sync cursor is empty")
    rid = run_id or _run_id()
    now = _ts()

    _insert_run(rid, int(start_time))
    _event(rid, "INFO", "inventory", "Inventory started", details={"base_cursor": start_time, "limit": limit})

    next_url: str | None = None
    stream_position = 0
    discovered = 0
    last_ticket_id = 0
    final_cursor: int | None = None
    reached_end_of_stream = False
    buffer: list[dict[str, Any]] = []

    while True:
        if _stop_requested(rid):
            _set_run_fields(rid, {"status": "inventory_stopped", "inventory_complete": False})
            break

        if next_url:
            data = _zd_get(next_url)
        else:
            data = _zd_get(
                f"{settings.zendesk_base_url}/incremental/tickets",
                params={"start_time": int(start_time)},
            )

        if "end_time" in data:
            final_cursor = int(data["end_time"])

        for ticket in data.get("tickets", []):
            if ticket.get("status") == "deleted":
                continue
            stream_position += 1
            discovered += 1
            last_ticket_id = int(ticket["id"])
            buffer.append({
                "run_id": rid,
                "stream_position": stream_position,
                "ticket_id": last_ticket_id,
                "zendesk_updated_at": ticket.get("updated_at"),
                "discovered_at": now,
                "status": "pending",
                "retry_count": 0,
                "updated_at": now,
            })
            if len(buffer) >= 500:
                _upsert_items(buffer)
                buffer = []
                _heartbeat(rid)
            if limit and discovered >= limit:
                break

        if progress_cb and discovered % 500 == 0:
            progress_cb(f"Inventory: {discovered} tickets discovered")

        _set_run_fields(rid, {
            "heartbeat_at": _ts(),
            "total_discovered": discovered,
            "last_stream_position": stream_position,
            "last_ticket_id": last_ticket_id,
        })

        if limit and discovered >= limit:
            break
        if data.get("end_of_stream"):
            reached_end_of_stream = True
            break
        next_url = data.get("after_url") or data.get("next_page")
        if not next_url:
            break
        time.sleep(0.15)

    if buffer:
        _upsert_items(buffer)

    complete = not _stop_requested(rid) and reached_end_of_stream and limit is None
    _set_run_fields(rid, {
        "status": "inventory_ready" if complete else ("inventory_stopped" if _stop_requested(rid) else "inventory_partial"),
        "inventory_complete": complete,
        "final_cursor": final_cursor or 0,
        "total_discovered": discovered,
        "last_stream_position": stream_position,
    })
    _event(rid, "INFO", "inventory", "Inventory finished", details={
        "discovered": discovered,
        "final_cursor": final_cursor,
        "complete": complete,
        "reached_end_of_stream": reached_end_of_stream,
        "limit": limit,
    })
    return status(rid)


def _upsert_items(rows: list[dict[str, Any]]) -> None:
    """Upsert manifest rows by (run_id, ticket_id)."""
    if not rows:
        return
    payload = json.dumps(rows, default=str)
    sql = f"""
    MERGE `{ITEMS_TABLE}` T
    USING (
      SELECT *
      FROM UNNEST(JSON_QUERY_ARRAY(@payload)) raw,
      UNNEST([STRUCT(
        JSON_VALUE(raw, '$.run_id') AS run_id,
        CAST(JSON_VALUE(raw, '$.stream_position') AS INT64) AS stream_position,
        CAST(JSON_VALUE(raw, '$.ticket_id') AS INT64) AS ticket_id,
        TIMESTAMP(JSON_VALUE(raw, '$.zendesk_updated_at')) AS zendesk_updated_at,
        TIMESTAMP(JSON_VALUE(raw, '$.discovered_at')) AS discovered_at,
        JSON_VALUE(raw, '$.status') AS status,
        CAST(JSON_VALUE(raw, '$.retry_count') AS INT64) AS retry_count,
        TIMESTAMP(JSON_VALUE(raw, '$.updated_at')) AS updated_at
      )])
    ) S
    ON T.run_id = S.run_id AND T.ticket_id = S.ticket_id
    WHEN NOT MATCHED THEN
      INSERT (
        run_id, stream_position, ticket_id, zendesk_updated_at, discovered_at,
        status, retry_count, updated_at
      )
      VALUES (
        S.run_id, S.stream_position, S.ticket_id, S.zendesk_updated_at, S.discovered_at,
        S.status, S.retry_count, S.updated_at
      )
    """
    _get_client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("payload", "STRING", payload)]
        ),
    ).result()


def _mark_item(run_id: str, ticket_id: int, fields: dict[str, Any]) -> None:
    assignments = ["updated_at = CURRENT_TIMESTAMP()"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        bigquery.ScalarQueryParameter("ticket_id", "INT64", ticket_id),
    ]
    for idx, (key, value) in enumerate(fields.items()):
        name = f"v{idx}"
        assignments.append(f"{key} = @{name}")
        params.append(bigquery.ScalarQueryParameter(
            name,
            _bq_param_type(key, value),
            _bq_param_value(key, value),
        ))
    _get_client().query(
        f"""
        UPDATE `{ITEMS_TABLE}`
        SET {', '.join(assignments)}
        WHERE run_id = @run_id AND ticket_id = @ticket_id
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()


def _pending_ticket_ids(run_id: str, limit: int) -> list[int]:
    rows = list(_get_client().query(
        f"""
        SELECT ticket_id
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
          AND status IN (
            'pending',
            'processing_ticket',
            'ticket_written',
            'ocr_done',
            'embedding_partial',
            'gcs_partial',
            'error_retryable'
          )
        ORDER BY stream_position
        LIMIT @limit
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]),
    ).result())
    return [int(r.ticket_id) for r in rows]


def run_batch(
    *,
    run_id: str,
    limit: int,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Process the next manifest tickets with per-phase durable checkpoints."""
    ensure_tables()
    batch_id = "batch_" + uuid.uuid4().hex[:10]
    ticket_ids = _pending_ticket_ids(run_id, limit)
    if not ticket_ids:
        return status(run_id)

    _set_run_fields(run_id, {"status": "batch_running", "stop_requested": False, "heartbeat_at": _ts()})
    _event(run_id, "INFO", "batch", "Batch started", details={"batch_id": batch_id, "limit": limit, "tickets": ticket_ids})

    org_cache: dict[Any, Any] = {}
    user_cache: dict[Any, Any] = {}
    written: list[int] = []

    for tid in ticket_ids:
        if _stop_requested(run_id):
            _event(run_id, "INFO", "batch", "Stop honored before ticket write", ticket_id=tid)
            break
        try:
            _mark_item(run_id, tid, {"status": "processing_ticket", "batch_id": batch_id})
            _write_one_ticket(tid, org_cache, user_cache)
            _mark_item(run_id, tid, {"status": "ticket_written", "ticket_written_at": _ts()})
            written.append(tid)
            _heartbeat(run_id)
            if progress_cb:
                progress_cb(f"Tickets: wrote {len(written)}/{len(ticket_ids)}")
        except Exception as exc:
            logger.exception("manual sync ticket write failed for %s", tid)
            _mark_item(run_id, tid, {
                "status": "error_retryable",
                "error_phase": "ticket_write",
                "error_message": str(exc)[:1000],
                "retry_count": 1,
            })
            _event(run_id, "ERROR", "ticket_write", str(exc), ticket_id=tid)

    if not written:
        _set_run_fields(run_id, {"status": "batch_stopped" if _stop_requested(run_id) else "batch_no_work"})
        return status(run_id)

    try:
        ocr_result = _process_scoped_ocr(run_id, written, progress_cb=progress_cb)
    except CostGateError as exc:
        _set_run_fields(run_id, {"status": "blocked_by_cost_gate", "heartbeat_at": _ts()})
        _event(run_id, "ERROR", "ocr", f"Batch blocked by OCR cost gate: {exc}")
        return status(run_id)
    except Exception as exc:
        _set_run_fields(run_id, {"status": "batch_error", "heartbeat_at": _ts()})
        _event(run_id, "ERROR", "ocr", f"Scoped OCR failed: {exc}")
        return status(run_id)
    if ocr_result.get("stopped"):
        _set_run_fields(run_id, {"status": "batch_stopped", "heartbeat_at": _ts()})
        _event(run_id, "INFO", "batch", "Stop honored during OCR", details={"batch_id": batch_id})
        return status(run_id)
    for tid in written:
        _mark_item(run_id, tid, {"status": "ocr_done", "ocr_done_at": _ts()})

    if _stop_requested(run_id):
        _set_run_fields(run_id, {"status": "batch_stopped"})
        _event(run_id, "INFO", "batch", "Stop honored after OCR", details={"batch_id": batch_id})
        return status(run_id)

    try:
        embed_result = _process_scoped_embeddings(run_id, written, progress_cb=progress_cb)
    except CostGateError as exc:
        _set_run_fields(run_id, {"status": "blocked_by_cost_gate", "heartbeat_at": _ts()})
        _event(run_id, "ERROR", "embedding", f"Batch blocked by embedding cost gate: {exc}")
        return status(run_id)
    except Exception as exc:
        _set_run_fields(run_id, {"status": "batch_error", "heartbeat_at": _ts()})
        _event(run_id, "ERROR", "embedding", f"Scoped embedding failed: {exc}")
        return status(run_id)
    successful_ids = set(embed_result["embedded_ticket_ids"]) & set(embed_result["gcs_uploaded_ticket_ids"])
    failed_gcs_ids = set(embed_result["gcs_failed_ticket_ids"])
    embedded_only_ids = set(embed_result["embedded_ticket_ids"]) - set(embed_result["gcs_uploaded_ticket_ids"])

    for tid in written:
        if tid in successful_ids:
            _mark_item(run_id, tid, {
                "status": "done",
                "embedded_at": _ts(),
                "gcs_uploaded_at": _ts(),
                "verified_at": _ts(),
                "vertex_import_operation": embed_result.get("vertex_import_operation") or "",
                "error_phase": "",
                "error_message": "",
            })
        elif tid in failed_gcs_ids or tid in embedded_only_ids:
            _mark_item(run_id, tid, {
                "status": "gcs_partial",
                "embedded_at": _ts(),
                "error_phase": "gcs_upload",
                "error_message": "Embedding exists but GCS upload did not complete",
            })
        else:
            _mark_item(run_id, tid, {
                "status": "error_retryable",
                "error_phase": "embedding",
                "error_message": "Embedding did not complete for ticket",
            })

    _refresh_run_counts(run_id)
    _set_run_fields(run_id, {"status": "batch_complete", "heartbeat_at": _ts()})
    _event(run_id, "INFO", "batch", "Batch complete", details={
        "batch_id": batch_id,
        "written": len(written),
        "ocr": ocr_result,
        "embedded": len(embed_result["embedded_ticket_ids"]),
        "gcs_uploaded": len(embed_result["gcs_uploaded_ticket_ids"]),
        "gcs_failed": len(embed_result["gcs_failed_ticket_ids"]),
    })
    return status(run_id)


def _write_one_ticket(ticket_id: int, org_cache: dict[Any, Any], user_cache: dict[Any, Any]) -> None:
    ticket = _zd_get(f"{settings.zendesk_base_url}/tickets/{ticket_id}").get("ticket")
    if not ticket:
        raise RuntimeError(f"Zendesk ticket {ticket_id} not found")
    comments = _zd_get(f"{settings.zendesk_base_url}/tickets/{ticket_id}/comments").get("comments", [])
    now = _utc_now()
    client = _get_client()
    _bq_delete_tickets(client, [ticket_id])
    _bq_insert(client, "tickets", [_build_ticket_row(ticket, comments, org_cache, user_cache, now)])
    _bq_insert(client, "ticket_comments", _build_comment_rows(ticket_id, comments, now, user_cache))
    _bq_insert(client, "ticket_attachments", _attachments_from_comments(ticket_id, comments, now))


def _process_scoped_ocr(
    run_id: str,
    ticket_ids: list[int],
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    client = _get_client()
    ids = _ids_csv(ticket_ids)
    sql = f"""
    SELECT
      a.ticket_id,
      a.content_url,
      COALESCE(a.content_type, '') AS content_type,
      COALESCE(a.size_bytes, 0) AS size_bytes
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o
      ON a.content_url = o.content_url
    WHERE a.ticket_id IN ({ids})
      AND a.is_image = TRUE
      AND a.content_url IS NOT NULL
      AND o.content_url IS NULL
    ORDER BY a.ticket_id, a.content_url
    """
    rows = list(client.query(sql).result())
    now = _ts()
    _upsert_ocr_items(run_id, rows)

    if not rows:
        if progress_cb:
            progress_cb("OCR: no scoped candidates")
        return {"candidates": 0, "success": 0, "errors": 0}

    reserved_per = get_gate_reserve(CallType.OCR_IMAGE, OCR_MEASURED_EXPECTED_CAD)
    projected = calc_cost(tokens_in=1500, tokens_out=200, call_type=CallType.OCR_IMAGE) * len(rows)
    try:
        check_cost_gate(CallType.OCR_IMAGE, reserved_per * len(rows))
    except CostGateError as exc:
        _event(run_id, "ERROR", "ocr", f"OCR blocked by cost gate: {exc}", details={"candidates": len(rows)})
        raise

    if progress_cb:
        progress_cb(f"OCR: processing {len(rows)} scoped images (est. C${projected:.4f})")

    buffer: list[dict[str, Any]] = []
    success = 0
    errors = 0
    stopped = False

    for idx, img in enumerate(rows, start=1):
        if _stop_requested(run_id):
            stopped = True
            break
        content_url = img.content_url
        ticket_id = int(img.ticket_id)
        try:
            check_cost_gate(CallType.OCR_IMAGE, reserved_per)
            image_bytes, downloaded_content_type, download_err = _download_zendesk_attachment(content_url)
            if download_err:
                status = "failed"
                skip_reason = download_err
                ocr_text = None
                errors += 1
            else:
                declared_mime = downloaded_content_type or img.content_type
                effective_mime = _sniff_attachment_mime(image_bytes, declared_mime)
                too_large = (img.size_bytes and img.size_bytes > _MAX_IMAGE_BYTES) or len(image_bytes) > _MAX_IMAGE_BYTES
                if effective_mime != "application/pdf" and too_large:
                    status = "skipped"
                    skip_reason = "too_large"
                    ocr_text = None
                else:
                    with _manual_ocr_timeout(_OCR_ITEM_TIMEOUT_SECONDS):
                        ocr_text, ocr_err, _ = _extract_ticket_attachment_text(image_bytes, declared_mime)
                    if ocr_err:
                        status = "skipped" if ocr_err.startswith("corrupt_") else "failed"
                        skip_reason = ocr_err
                        errors += 1
                    else:
                        status = "success"
                        skip_reason = None
                        success += 1
            buffer.append({
                "content_url": content_url,
                "ticket_id": ticket_id,
                "ocr_text": ocr_text,
                "processing_status": status,
                "skip_reason": skip_reason,
                "processed_at": now,
            })
            _safe_mark_ocr_item(run_id, ticket_id, content_url, {
                "status": "done",
                "processing_status": status,
                "skip_reason": skip_reason or "",
                "processed_at": now,
                "updated_at": now,
            })
        except Exception as exc:
            errors += 1
            buffer.append({
                "content_url": content_url,
                "ticket_id": ticket_id,
                "ocr_text": None,
                "processing_status": "failed",
                "skip_reason": str(exc)[:500],
                "processed_at": now,
            })
            _safe_mark_ocr_item(run_id, ticket_id, content_url, {
                "status": "error_retryable",
                "last_error": str(exc)[:1000],
                "updated_at": now,
            })
            _event(run_id, "ERROR", "ocr", str(exc), ticket_id=ticket_id, details={"content_url": content_url})

        if len(buffer) >= 10:
            _bq_insert(client, "ticket_attachment_ocr", buffer)
            buffer = []
        if progress_cb and (idx % 10 == 0 or idx == len(rows)):
            progress_cb(f"OCR: {idx}/{len(rows)} candidates checked")
        time.sleep(_OCR_SLEEP)

    if buffer:
        _bq_insert(client, "ticket_attachment_ocr", buffer)

    return {"candidates": len(rows), "success": success, "errors": errors, "stopped": stopped}


@contextmanager
def _manual_ocr_timeout(seconds: int):
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"manual OCR item exceeded {seconds}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _upsert_ocr_items(run_id: str, rows: list[Any]) -> None:
    if not rows:
        return
    now = _ts()
    payload = json.dumps([
        {
            "run_id": run_id,
            "ticket_id": int(r.ticket_id),
            "content_url": r.content_url,
            "status": "pending",
            "attempt_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        for r in rows
    ], default=str)
    sql = f"""
    MERGE `{OCR_ITEMS_TABLE}` T
    USING (
      SELECT *
      FROM UNNEST(JSON_QUERY_ARRAY(@payload)) raw,
      UNNEST([STRUCT(
        JSON_VALUE(raw, '$.run_id') AS run_id,
        CAST(JSON_VALUE(raw, '$.ticket_id') AS INT64) AS ticket_id,
        JSON_VALUE(raw, '$.content_url') AS content_url,
        JSON_VALUE(raw, '$.status') AS status,
        CAST(JSON_VALUE(raw, '$.attempt_count') AS INT64) AS attempt_count,
        TIMESTAMP(JSON_VALUE(raw, '$.created_at')) AS created_at,
        TIMESTAMP(JSON_VALUE(raw, '$.updated_at')) AS updated_at
      )])
    ) S
    ON T.run_id = S.run_id AND T.content_url = S.content_url
    WHEN NOT MATCHED THEN
      INSERT (run_id, ticket_id, content_url, status, attempt_count, created_at, updated_at)
      VALUES (S.run_id, S.ticket_id, S.content_url, S.status, S.attempt_count, S.created_at, S.updated_at)
    """
    _get_client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("payload", "STRING", payload)]
        ),
    ).result()


def _mark_ocr_item(run_id: str, ticket_id: int, content_url: str, fields: dict[str, Any]) -> None:
    assignments = ["attempt_count = COALESCE(attempt_count, 0) + 1"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        bigquery.ScalarQueryParameter("content_url", "STRING", content_url),
    ]
    for idx, (key, value) in enumerate(fields.items()):
        name = f"v{idx}"
        assignments.append(f"{key} = @{name}")
        params.append(bigquery.ScalarQueryParameter(
            name,
            _bq_param_type(key, value),
            _bq_param_value(key, value),
        ))
    job = _get_client().query(
        f"""
        UPDATE `{OCR_ITEMS_TABLE}`
        SET {', '.join(assignments)}
        WHERE run_id = @run_id AND content_url = @content_url
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
        timeout=30,
    )
    job.result(timeout=_BQ_DML_RESULT_TIMEOUT_SECONDS)


def _safe_mark_ocr_item(run_id: str, ticket_id: int, content_url: str, fields: dict[str, Any]) -> None:
    # Non-critical tracking table only. The durable source of truth for OCR
    # resume is ticket_attachment_ocr; blocking here can stall the whole batch.
    return


def _process_scoped_embeddings(
    run_id: str,
    ticket_ids: list[int],
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    client = _get_client()
    ids = _ids_csv(ticket_ids)
    rows = list(client.query(f"""
    SELECT t.id
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` t
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` te ON t.id = te.ticket_id
    WHERE t.id IN ({ids})
      AND (te.ticket_id IS NULL OR t.updated_at > te.embedded_at)
    ORDER BY t.id
    """).result())
    ids_to_embed = {int(r.id) for r in rows}

    embedding_ready: list[int] = []
    gcs_uploaded: list[int] = []
    gcs_failed: list[int] = []
    now = _ts()

    for batch_start in range(0, len(ticket_ids), _EMBED_BATCH):
        batch_ids = ticket_ids[batch_start:batch_start + _EMBED_BATCH]
        batch_texts = [f"TICKET #{tid}" for tid in batch_ids]
        with ThreadPoolExecutor(max_workers=_DOC_WORKERS) as executor:
            future_to_idx = {executor.submit(generate_ticket_document, tid): i for i, tid in enumerate(batch_ids)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    batch_texts[idx] = future.result()
                except Exception as exc:
                    logger.warning("manual sync document generation failed for ticket %s: %s", batch_ids[idx], exc)

        embed_pairs = [(tid, text) for tid, text in zip(batch_ids, batch_texts) if tid in ids_to_embed]
        if embed_pairs:
            embed_ids = [tid for tid, _ in embed_pairs]
            embed_texts = [text for _, text in embed_pairs]
            stale_ids = _ids_csv(embed_ids)
            client.query(
                f"DELETE FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` WHERE ticket_id IN ({stale_ids})"
            ).result()
            vectors = emb_service.embed_texts(embed_texts, task_type="RETRIEVAL_DOCUMENT")
            _bq_insert(client, "ticket_embeddings", [
                {"ticket_id": tid, "embedding": vec, "embedded_at": now}
                for tid, vec in zip(embed_ids, vectors)
            ])
            embedding_ready.extend(embed_ids)

        # Tickets without a stale/missing embedding are already embedding-ready,
        # but their GCS documents are still refreshed so Agent Builder has the
        # same ticket text as BQ.
        embedding_ready.extend([tid for tid in batch_ids if tid not in ids_to_embed])

        with ThreadPoolExecutor(max_workers=_GCS_WORKERS) as executor:
            future_to_tid = {
                executor.submit(upload_ticket_to_gcs, tid, text): tid
                for tid, text in zip(batch_ids, batch_texts)
            }
            for future in as_completed(future_to_tid):
                tid = future_to_tid[future]
                try:
                    future.result()
                    gcs_uploaded.append(tid)
                except Exception as exc:
                    gcs_failed.append(tid)
                    _event(run_id, "ERROR", "gcs_upload", str(exc), ticket_id=tid)

        if progress_cb:
            progress_cb(f"Embeddings/GCS: {len(embedding_ready)}/{len(ticket_ids)} embedding-ready")

    operation = ""
    if gcs_uploaded:
        try:
            operation = trigger_vertex_tickets_import()
        except Exception as exc:
            _event(run_id, "ERROR", "vertex_import", str(exc), details={"uploaded": len(gcs_uploaded)})

    return {
        "embedded_ticket_ids": sorted(set(embedding_ready)),
        "gcs_uploaded_ticket_ids": gcs_uploaded,
        "gcs_failed_ticket_ids": gcs_failed,
        "vertex_import_operation": operation,
    }


def _refresh_run_counts(run_id: str) -> None:
    rows = list(_get_client().query(
        f"""
        SELECT
          COUNT(*) AS total,
          COUNTIF(status = 'done') AS done,
          COUNTIF(STARTS_WITH(status, 'error') OR status IN ('gcs_partial', 'embedding_partial')) AS errors
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result())
    if rows:
        _set_run_fields(run_id, {
            "total_discovered": int(rows[0].total or 0),
            "total_done": int(rows[0].done or 0),
            "total_errors": int(rows[0].errors or 0),
        })


def status(run_id: str | None = None) -> dict[str, Any]:
    ensure_tables()
    client = _get_client()
    if run_id is None:
        rows = list(client.query(
            f"SELECT run_id FROM `{RUNS_TABLE}` ORDER BY created_at DESC LIMIT 1"
        ).result())
        if not rows:
            return {"run": None, "counts": []}
        run_id = rows[0].run_id

    run_rows = list(client.query(
        f"SELECT * FROM `{RUNS_TABLE}` WHERE run_id = @run_id LIMIT 1",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result())
    count_rows = list(client.query(
        f"""
        SELECT status, COUNT(*) AS count
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
        GROUP BY status
        ORDER BY status
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result())
    latest_errors = list(client.query(
        f"""
        SELECT created_at, level, phase, ticket_id, message
        FROM `{EVENTS_TABLE}`
        WHERE run_id = @run_id AND level IN ('ERROR', 'WARN')
        ORDER BY created_at DESC
        LIMIT 10
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        ),
    ).result())

    return {
        "run": dict(run_rows[0].items()) if run_rows else None,
        "counts": [dict(r.items()) for r in count_rows],
        "latest_errors": [dict(r.items()) for r in latest_errors],
    }


def finalize_run(run_id: str, *, advance_cursor: bool = False) -> dict[str, Any]:
    """
    Mark a run final only if all manifest items are done.

    Production cursor advancement is explicit and must be used only after human
    verification. This function never resumes schedulers.
    """
    current = status(run_id)
    counts = {row["status"]: row["count"] for row in current.get("counts", [])}
    not_done = sum(count for state, count in counts.items() if state != "done")
    if not_done:
        raise RuntimeError(f"Cannot finalize run {run_id}: {not_done} manifest items are not done")

    run = current["run"]
    if not run or not run.get("inventory_complete"):
        raise RuntimeError(f"Cannot finalize run {run_id}: inventory is not complete")
    final_cursor = int(run.get("final_cursor") or 0)
    if advance_cursor:
        if not final_cursor:
            raise RuntimeError(f"Cannot advance cursor for {run_id}: final_cursor is empty")
        firestore.set_sync_cursor(final_cursor)
        _event(run_id, "INFO", "finalize", "Production sync cursor advanced", details={"final_cursor": final_cursor})

    _set_run_fields(run_id, {"status": "finalized", "heartbeat_at": _ts()})
    return status(run_id)
