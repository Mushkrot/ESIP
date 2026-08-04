#!/usr/bin/env python3
"""Run the next pending-only chunk for the durable manual Zendesk catch-up.

This intentionally ignores known gcs_partial rows so they do not consume the
normal ticket reconciliation batch. Those rows need a scoped GCS refresh.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.manual_ticket_sync import (  # noqa: E402
    BQ_DATASET,
    GCP_PROJECT,
    ITEMS_TABLE,
    _bulk_mark_reconciled,
    _event,
    _gcs_ticket_docs_exist,
    _get_client,
    _process_scoped_embeddings,
    _process_scoped_ocr,
    _refresh_run_counts,
    _set_run_fields,
    _ts,
    _write_one_ticket,
    status,
)


def _update_group(
    client: bigquery.Client,
    run_id: str,
    ticket_ids: list[int],
    sql_set: str,
    extra_params: list[bigquery.ScalarQueryParameter] | None = None,
) -> None:
    if not ticket_ids:
        return
    params: list = [
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        bigquery.ArrayQueryParameter("ticket_ids", "INT64", ticket_ids),
    ]
    if extra_params:
        params.extend(extra_params)
    client.query(
        f"""
        UPDATE `{ITEMS_TABLE}`
        SET {sql_set}, updated_at=CURRENT_TIMESTAMP()
        WHERE run_id = @run_id AND ticket_id IN UNNEST(@ticket_ids)
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()


def run_pending_batch(run_id: str, limit: int) -> dict:
    client = _get_client()
    rows = list(client.query(
        f"""
        SELECT ticket_id, stream_position, status
        FROM `{ITEMS_TABLE}`
        WHERE run_id = @run_id
          AND status IN ('ticket_written', 'ocr_done', 'pending')
        ORDER BY
          CASE WHEN status IN ('ticket_written', 'ocr_done') THEN 0 ELSE 1 END,
          stream_position
        LIMIT @limit
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]),
    ).result())
    ticket_ids = [int(r.ticket_id) for r in rows]
    positions = [int(r.stream_position) for r in rows]
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[str(row.status)] = status_counts.get(str(row.status), 0) + 1
    print(
        f"Selected resumable/pending rows: {len(ticket_ids)} "
        f"positions {min(positions) if positions else None}-{max(positions) if positions else None} "
        f"statuses={status_counts}",
        flush=True,
    )
    if not ticket_ids:
        return status(run_id)

    _set_run_fields(run_id, {
        "status": "pending_reconcile_running",
        "stop_requested": False,
        "heartbeat_at": _ts(),
    })
    _event(run_id, "INFO", "reconcile", "Selected resumable/pending rows", details={
        "limit": limit,
        "min_position": min(positions),
        "max_position": max(positions),
        "status_counts": status_counts,
    })

    ids = ",".join(str(tid) for tid in ticket_ids)
    bq_rows = list(client.query(
        f"""
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
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        ]),
    ).result())
    state = {
        int(r.ticket_id): {
            "ticket_exists": bool(r.ticket_exists),
            "embedding_exists": bool(r.embedding_exists),
            "pending_ocr": int(r.pending_ocr or 0),
        }
        for r in bq_rows
    }
    print(f"BQ state loaded: {len(state)}", flush=True)
    gcs_exists = _gcs_ticket_docs_exist(ticket_ids)
    print(f"GCS docs found: {sum(1 for exists in gcs_exists.values() if exists)}", flush=True)

    done_ids: list[int] = []
    gcs_partial_ids: list[int] = []
    ticket_written_ids: list[int] = []
    left_pending_ids: list[int] = []
    for tid in ticket_ids:
        item = state.get(tid, {})
        if (
            item.get("ticket_exists")
            and item.get("embedding_exists")
            and item.get("pending_ocr") == 0
            and gcs_exists.get(tid)
        ):
            done_ids.append(tid)
        elif item.get("ticket_exists") and item.get("embedding_exists") and item.get("pending_ocr") == 0:
            gcs_partial_ids.append(tid)
        elif item.get("ticket_exists"):
            ticket_written_ids.append(tid)
        else:
            left_pending_ids.append(tid)

    print({
        "done": len(done_ids),
        "gcs_partial": len(gcs_partial_ids),
        "ticket_written": len(ticket_written_ids),
        "left_pending": len(left_pending_ids),
    }, flush=True)
    _bulk_mark_reconciled(
        run_id=run_id,
        done_ids=done_ids,
        gcs_partial_ids=gcs_partial_ids,
        ticket_written_ids=ticket_written_ids,
        timestamp=_ts(),
    )
    _refresh_run_counts(run_id)

    if left_pending_ids:
        org_cache: dict = {}
        user_cache: dict = {}
        newly_written_ids: list[int] = []
        for idx, tid in enumerate(left_pending_ids, start=1):
            try:
                _write_one_ticket(tid, org_cache, user_cache)
                newly_written_ids.append(tid)
                print(f"Tickets: wrote pending {idx}/{len(left_pending_ids)}", flush=True)
                if idx % 10 == 0 or idx == len(left_pending_ids):
                    _set_run_fields(run_id, {"heartbeat_at": _ts()})
            except Exception as exc:
                _update_group(
                    client,
                    run_id,
                    [tid],
                    "status='error_retryable', error_phase='ticket_write', error_message=@error",
                    [bigquery.ScalarQueryParameter("error", "STRING", str(exc)[:1000])],
                )
                _event(run_id, "ERROR", "ticket_write", str(exc), ticket_id=tid)
        if newly_written_ids:
            _update_group(
                client,
                run_id,
                newly_written_ids,
                "status='ticket_written', ticket_written_at=COALESCE(ticket_written_at, CURRENT_TIMESTAMP())",
            )
            ticket_written_ids.extend(newly_written_ids)
            _refresh_run_counts(run_id)

    if ticket_written_ids:
        _set_run_fields(run_id, {"status": "batch_resuming_phases", "heartbeat_at": _ts()})

        def progress(msg: str) -> None:
            print(msg, flush=True)
            _set_run_fields(run_id, {"heartbeat_at": _ts()})

        ocr_result = _process_scoped_ocr(run_id, ticket_written_ids, progress_cb=progress)
        print({"ocr": ocr_result}, flush=True)
        if ocr_result.get("stopped"):
            _set_run_fields(run_id, {"status": "batch_stopped", "heartbeat_at": _ts()})
            return status(run_id)

        _update_group(
            client,
            run_id,
            ticket_written_ids,
            "status='ocr_done', ocr_done_at=COALESCE(ocr_done_at, CURRENT_TIMESTAMP())",
        )

        embed_result = _process_scoped_embeddings(run_id, ticket_written_ids, progress_cb=progress)
        print({
            "embedded": len(embed_result["embedded_ticket_ids"]),
            "gcs_uploaded": len(embed_result["gcs_uploaded_ticket_ids"]),
            "gcs_failed": len(embed_result["gcs_failed_ticket_ids"]),
            "vertex_import_operation": embed_result.get("vertex_import_operation"),
        }, flush=True)

        successful_ids = sorted(set(embed_result["embedded_ticket_ids"]) & set(embed_result["gcs_uploaded_ticket_ids"]))
        gcs_ids = sorted(
            set(embed_result["gcs_failed_ticket_ids"])
            | (set(embed_result["embedded_ticket_ids"]) - set(embed_result["gcs_uploaded_ticket_ids"]))
        )
        error_ids = sorted(set(ticket_written_ids) - set(successful_ids) - set(gcs_ids))
        operation = embed_result.get("vertex_import_operation") or ""

        _update_group(
            client,
            run_id,
            successful_ids,
            (
                "status='done', embedded_at=CURRENT_TIMESTAMP(), "
                "gcs_uploaded_at=CURRENT_TIMESTAMP(), verified_at=CURRENT_TIMESTAMP(), "
                "vertex_import_operation=@operation, error_phase='', error_message=''"
            ),
            [bigquery.ScalarQueryParameter("operation", "STRING", operation)],
        )
        _update_group(
            client,
            run_id,
            gcs_ids,
            (
                "status='gcs_partial', embedded_at=CURRENT_TIMESTAMP(), "
                "error_phase='gcs_upload', "
                "error_message='Embedding exists but GCS upload did not complete'"
            ),
        )
        _update_group(
            client,
            run_id,
            error_ids,
            (
                "status='error_retryable', error_phase='embedding', "
                "error_message='Embedding did not complete for ticket'"
            ),
        )

    _refresh_run_counts(run_id)
    _set_run_fields(run_id, {"status": "batch_complete", "heartbeat_at": _ts()})
    return status(run_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args()
    print(run_pending_batch(args.run_id, args.limit))


if __name__ == "__main__":
    main()
