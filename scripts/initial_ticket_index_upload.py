"""
Initial ticket-index upload: build full-content documents, upload to GCS, and embed tickets.

Steps:
  1. TRUNCATE ticket_embeddings (only on first run — skipped on resume)
  2. Fetch all ticket IDs from BQ
  3. For each chunk of 500 tickets:
     a. Bulk-fetch fields, comments, OCR (3 queries per chunk, not per ticket)
     b. Assemble document strings in Python
     c. Upload to GCS in parallel (20 workers)
     d. Embed in parallel batches (8 workers, 5 texts each)
     e. Insert rows into ticket_embeddings (batches of 500)
     f. Save checkpoint
  4. Trigger Vertex AI INCREMENTAL import
  5. Print summary

Resume safety:
  Checkpoint: scripts/.initial_ticket_index_checkpoint.json
  On resume: completed ticket IDs are loaded; those chunks are skipped.
  TRUNCATE is only done on the very first run (when checkpoint is absent or empty).

Usage (on VM):
  source venv/bin/activate
  python3 scripts/initial_ticket_index_upload.py
"""

import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from google.cloud import bigquery as bq_module
from google.cloud import storage as gcs_module

from app.config import settings
from app.services.embeddings import embed_texts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GCP_PROJECT  = settings.google_cloud_project_id
BQ_DATASET   = "esip_reference"
GCS_BUCKET   = "esip-reference-data"
GCS_PREFIX   = "tickets"
CHUNK_SIZE   = 500
GCS_WORKERS  = 20
EMB_WORKERS  = 2    # Keep low to avoid 429 quota on text-embedding-large-001 (~1500 req/min limit)
EMB_BATCH    = 5    # texts per embed_texts() call
EMB_SLEEP    = 0.5  # seconds between embedding batches to stay under quota
EMB_MAX_RETRIES = 4 # retry on 429 with exponential backoff
BQ_INSERT_BATCH = 50   # ~2.25MB per insert (500 rows × 3072 floats × 4 bytes × 3072 = too large for 500)

CHECKPOINT_FILE = Path(__file__).parent / ".initial_ticket_index_checkpoint.json"

# ---------------------------------------------------------------------------
# BQ / GCS clients (ADC on VM, service account file locally)
# ---------------------------------------------------------------------------

def _get_bq_client():
    """Return BQ client — service account credentials if available, else ADC."""
    try:
        from google.oauth2 import service_account as sa
        if settings.google_application_credentials:
            creds = sa.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return bq_module.Client(project=GCP_PROJECT, credentials=creds)
    except Exception:
        pass
    return bq_module.Client(project=GCP_PROJECT)


def _get_gcs_client():
    """Return GCS client — service account credentials if available, else ADC."""
    try:
        from google.oauth2 import service_account as sa
        if settings.google_application_credentials:
            creds = sa.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            return gcs_module.Client(project=GCP_PROJECT, credentials=creds)
    except Exception:
        pass
    return gcs_module.Client(project=GCP_PROJECT)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def load_checkpoint() -> set[int]:
    """Return set of ticket IDs already processed. Empty set = first run."""
    if not CHECKPOINT_FILE.exists():
        return set()
    try:
        data = json.loads(CHECKPOINT_FILE.read_text())
        return set(data.get("completed_ids", []))
    except Exception as exc:
        logger.warning("Checkpoint read failed (%s) — starting fresh", exc)
        return set()


def save_checkpoint(completed_ids: set[int], total: int) -> None:
    data = {
        "completed_ids": sorted(completed_ids),
        "total": total,
        "done": len(completed_ids),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

def _fmt_date(dt) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d")
    s = str(dt)
    return s[:10] if len(s) >= 10 else s


def _val(v) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return "—"
    return str(v)


def assemble_document(ticket: dict, comments: list[dict], ocr_by_comment: dict) -> str:
    """
    Build plain-text document for one ticket from pre-fetched Python dicts.

    ticket        — dict with ticket fields
    comments      — list of dicts sorted by created_at ASC
    ocr_by_comment — dict mapping comment_id (int|None) → list of (file_name, ocr_text)
    """
    lines = []

    if ticket.get("subject"):
        lines.append(f"SUBJECT: {ticket['subject']}")

    lines += [
        f"TICKET #{ticket['id']} | STATUS: {_val(ticket.get('status'))} | "
        f"CREATED: {_fmt_date(ticket.get('created_at'))} | SOLVED: {_fmt_date(ticket.get('solved_at'))}",
        f"STORE: {_val(ticket.get('store_id'))} | ORG: {_val(ticket.get('org_name'))} | "
        f"BRAND: {_val(ticket.get('brand'))} | CHANNEL: {_val(ticket.get('via_channel'))}",
        f"CATEGORY: {_val(ticket.get('issue_category'))} | PRODUCT: {_val(ticket.get('product_type'))} | "
        f"CAUSED-BY: {_val(ticket.get('caused_by'))}",
        f"RESOLUTION-TYPE: {_val(ticket.get('resolution_type'))} | "
        f"ESCALATED: {'yes' if ticket.get('escalated') else 'no'} | "
        f"VENDOR: {'yes' if ticket.get('vendor_dispatched') else 'no'}",
        f"SYMPTOMS: {_val(ticket.get('symptoms'))}",
        f"RESOLUTION: {_val(ticket.get('resolution'))}",
        "",
        "COMMENTS:",
        "",
    ]

    for c in comments:
        vis = "internal" if c.get("is_internal") else "public"
        created = c.get("created_at")
        dt_str = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else str(created)[:16]
        author = c.get("author_name") or "Unknown"
        lines.append(f"[{dt_str} | {author} ({vis})]")
        body = c.get("body") or ""
        if body.strip():
            lines.append(body.strip())

        comment_id = c.get("comment_id")
        for fname, ocr_text in ocr_by_comment.get(comment_id, []):
            lines.append(f"[IMAGE: {fname}]")
            if ocr_text and ocr_text.strip():
                lines.append(ocr_text.strip())

        lines.append("")

    # Images not attached to any comment
    for fname, ocr_text in ocr_by_comment.get(None, []):
        lines.append(f"[IMAGE: {fname}]")
        if ocr_text and ocr_text.strip():
            lines.append(ocr_text.strip())
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk BQ fetching
# ---------------------------------------------------------------------------

def fetch_chunk(client, ticket_ids: list[int]) -> tuple[list[dict], list[dict], dict]:
    """
    Fetch ticket fields, comments, and OCR for a list of ticket IDs.
    Returns (tickets_list, comments_list, ocr_by_ticket_and_comment)

    ocr_by_ticket_and_comment: dict[ticket_id] -> dict[comment_id|None -> list[(fname, ocr_text)]]
    """
    ids_param = [bq_module.ArrayQueryParameter("ticket_ids", "INT64", ticket_ids)]
    job_cfg = bq_module.QueryJobConfig(query_parameters=ids_param)

    # ---- Tickets ----
    tickets_sql = f"""
    SELECT
        id, subject, status, brand, store_id, org_id, org_name,
        created_at, updated_at, solved_at,
        via_channel, issue_category, product_type, caused_by,
        resolution_type, escalated, vendor_dispatched, vendor_escalation,
        symptoms, resolution
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id IN UNNEST(@ticket_ids)
    ORDER BY id
    """
    ticket_rows = list(client.query(tickets_sql, job_config=job_cfg).result())
    tickets = [dict(r.items()) for r in ticket_rows]

    # ---- Comments ----
    comments_sql = f"""
    SELECT
        ticket_id, comment_id, created_at, is_internal, author_name, body
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments`
    WHERE ticket_id IN UNNEST(@ticket_ids)
    ORDER BY ticket_id, created_at ASC
    """
    comment_rows = list(client.query(comments_sql, job_config=job_cfg).result())
    comments = [dict(r.items()) for r in comment_rows]

    # ---- OCR ----
    ocr_sql = f"""
    SELECT
        ta.ticket_id,
        ta.comment_id,
        COALESCE(ta.file_name, '') AS file_name,
        ocr.ocr_text
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` ta
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` ocr
        ON ta.content_url = ocr.content_url
    WHERE ta.ticket_id IN UNNEST(@ticket_ids)
      AND ta.is_image = TRUE
      AND (ocr.processing_status IS NULL OR ocr.processing_status = 'success')
    ORDER BY ta.ticket_id, ta.created_at ASC
    """
    ocr_rows = list(client.query(ocr_sql, job_config=job_cfg).result())

    # Group OCR by (ticket_id, comment_id)
    ocr_by_ticket: dict[int, dict] = {}
    for row in ocr_rows:
        tid = row.ticket_id
        cid = row.comment_id  # may be None
        if tid not in ocr_by_ticket:
            ocr_by_ticket[tid] = {}
        if cid not in ocr_by_ticket[tid]:
            ocr_by_ticket[tid][cid] = []
        ocr_by_ticket[tid][cid].append((row.file_name or "image", row.ocr_text))

    return tickets, comments, ocr_by_ticket


# ---------------------------------------------------------------------------
# GCS upload
# ---------------------------------------------------------------------------

def upload_to_gcs(gcs_client, ticket_id: int, text: str) -> None:
    bucket = gcs_client.bucket(GCS_BUCKET)
    blob = bucket.blob(f"{GCS_PREFIX}/{ticket_id}.txt")
    blob.upload_from_string(text.encode("utf-8"), content_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Vertex AI import trigger
# ---------------------------------------------------------------------------

def trigger_vertex_import() -> str:
    import google.auth
    import google.auth.transport.requests
    import requests as req_lib

    try:
        from google.oauth2 import service_account as sa
        if settings.google_application_credentials:
            creds = sa.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        else:
            raise ValueError("no credentials file")
    except Exception:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    _LOC = settings.vertex_ai_location
    _DS  = settings.vertex_ai_tickets_data_store_id

    url = (
        f"https://discoveryengine.googleapis.com/v1"
        f"/projects/{GCP_PROJECT}/locations/{_LOC}"
        f"/collections/default_collection/dataStores/{_DS}"
        f"/branches/default_branch/documents:import"
    )
    payload = {
        "gcsSource": {
            "inputUris": [f"gs://{GCS_BUCKET}/{GCS_PREFIX}/*"],
            "dataSchema": "content",
        },
        "reconciliationMode": "INCREMENTAL",
    }
    resp = req_lib.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    op = resp.json().get("name", "")
    logger.info("Vertex AI import triggered: %s", op)
    return op


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_time = time.time()
    logger.info("=== Initial ticket-index upload starting ===")

    bq_client  = _get_bq_client()
    gcs_client = _get_gcs_client()

    # Load checkpoint
    completed_ids = load_checkpoint()
    is_resume = bool(completed_ids)
    if is_resume:
        logger.info("Resuming from checkpoint — %d tickets already done", len(completed_ids))
    else:
        logger.info("Fresh start — will TRUNCATE ticket_embeddings first")

    # Step 1: TRUNCATE only on first run
    if not is_resume:
        logger.info("TRUNCATING ticket_embeddings...")
        bq_client.query(
            f"TRUNCATE TABLE `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings`"
        ).result()
        logger.info("TRUNCATE done")

    # Step 2: Fetch all ticket IDs
    logger.info("Fetching all ticket IDs from BQ...")
    id_rows = list(bq_client.query(
        f"SELECT id FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` ORDER BY id"
    ).result())
    all_ids = [r.id for r in id_rows]
    total = len(all_ids)
    logger.info("Total tickets: %d", total)

    # Filter out already-done IDs
    pending_ids = [tid for tid in all_ids if tid not in completed_ids]
    logger.info("Pending: %d tickets", len(pending_ids))

    if not pending_ids:
        logger.info("Nothing to do — all tickets already processed!")
    else:
        # Step 3: Process chunks
        total_embedded  = 0
        total_gcs       = 0
        total_errors    = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for chunk_start in range(0, len(pending_ids), CHUNK_SIZE):
            chunk_ids = pending_ids[chunk_start: chunk_start + CHUNK_SIZE]
            chunk_num = chunk_start // CHUNK_SIZE + 1
            total_chunks = (len(pending_ids) + CHUNK_SIZE - 1) // CHUNK_SIZE
            elapsed = time.time() - start_time
            logger.info(
                "Chunk %d/%d | IDs %d–%d | done=%d/%d | elapsed=%.0fs",
                chunk_num, total_chunks,
                chunk_ids[0], chunk_ids[-1],
                len(completed_ids), total, elapsed,
            )

            # 3a: Bulk-fetch
            try:
                tickets, comments, ocr_by_ticket = fetch_chunk(bq_client, chunk_ids)
            except Exception as exc:
                logger.error("fetch_chunk failed for chunk %d: %s", chunk_num, exc)
                total_errors += len(chunk_ids)
                continue

            # Index comments by ticket_id
            comments_by_ticket: dict[int, list[dict]] = {}
            for c in comments:
                tid = c["ticket_id"]
                if tid not in comments_by_ticket:
                    comments_by_ticket[tid] = []
                comments_by_ticket[tid].append(c)

            # 3b: Assemble documents
            ticket_map = {t["id"]: t for t in tickets}
            docs: list[tuple[int, str]] = []  # (ticket_id, document_text)
            for tid in chunk_ids:
                t = ticket_map.get(tid)
                if t is None:
                    logger.warning("Ticket %d not found in BQ — skipping", tid)
                    continue
                try:
                    doc = assemble_document(
                        t,
                        comments_by_ticket.get(tid, []),
                        ocr_by_ticket.get(tid, {}),
                    )
                    docs.append((tid, doc))
                except Exception as exc:
                    logger.warning("assemble_document failed for %d: %s", tid, exc)
                    docs.append((tid, f"TICKET #{tid}"))

            if not docs:
                logger.warning("No documents assembled for chunk %d", chunk_num)
                continue

            doc_ids   = [d[0] for d in docs]
            doc_texts = [d[1] for d in docs]

            # 3c: Upload to GCS in parallel
            gcs_errors = 0

            def _upload(args):
                tid, text = args
                try:
                    upload_to_gcs(gcs_client, tid, text)
                    return tid, None
                except Exception as exc:
                    return tid, str(exc)

            with ThreadPoolExecutor(max_workers=GCS_WORKERS) as executor:
                futures = {executor.submit(_upload, (tid, text)): tid
                           for tid, text in zip(doc_ids, doc_texts)}
                for future in as_completed(futures):
                    tid, err = future.result()
                    if err:
                        logger.warning("GCS upload failed for %d: %s", tid, err)
                        gcs_errors += 1

            chunk_gcs_uploaded = len(doc_ids) - gcs_errors
            total_gcs += chunk_gcs_uploaded
            logger.info("GCS: %d uploaded, %d errors", chunk_gcs_uploaded, gcs_errors)

            # 3d: Embed in parallel batches
            # Split texts into batches of EMB_BATCH, call embed_texts per batch
            all_batches = [
                (doc_ids[i:i + EMB_BATCH], doc_texts[i:i + EMB_BATCH])
                for i in range(0, len(docs), EMB_BATCH)
            ]

            vectors_by_id: dict[int, list[float]] = {}
            embed_errors = 0

            def _embed_batch(batch_args):
                b_ids, b_texts = batch_args
                for attempt in range(EMB_MAX_RETRIES):
                    try:
                        vecs = embed_texts(b_texts, task_type="RETRIEVAL_DOCUMENT")
                        time.sleep(EMB_SLEEP)
                        return list(zip(b_ids, vecs)), None
                    except Exception as exc:
                        err_str = str(exc)
                        if "429" in err_str and attempt < EMB_MAX_RETRIES - 1:
                            wait = 2 ** (attempt + 2)  # 4, 8, 16 seconds
                            time.sleep(wait)
                            continue
                        return [], err_str
                return [], "max retries exceeded"

            with ThreadPoolExecutor(max_workers=EMB_WORKERS) as executor:
                futures = {executor.submit(_embed_batch, batch): batch
                           for batch in all_batches}
                for future in as_completed(futures):
                    pairs, err = future.result()
                    if err:
                        logger.warning("Embedding batch failed: %s", err)
                        embed_errors += len(futures[future][0])
                    else:
                        for tid, vec in pairs:
                            vectors_by_id[tid] = vec

            logger.info("Embeddings: %d done, %d errors", len(vectors_by_id), embed_errors)
            total_errors += gcs_errors + embed_errors

            # 3e: Insert to ticket_embeddings
            embed_rows = [
                {"ticket_id": tid, "embedding": vec, "embedded_at": now_iso}
                for tid, vec in vectors_by_id.items()
            ]
            if embed_rows:
                for ins_start in range(0, len(embed_rows), BQ_INSERT_BATCH):
                    batch = embed_rows[ins_start: ins_start + BQ_INSERT_BATCH]
                    errors = bq_client.insert_rows_json(
                        f"{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings", batch
                    )
                    if errors:
                        logger.error("BQ insert errors: %s", errors[:2])
                        total_errors += len(batch)
                    else:
                        total_embedded += len(batch)

            # 3f: Save checkpoint — mark all chunk IDs as done (even if some GCS/embed failed,
            # since BQ embeddings are what matters for re-running)
            completed_ids.update(doc_ids)
            save_checkpoint(completed_ids, total)

            done_count = len(completed_ids)
            pct = done_count / total * 100 if total else 0
            elapsed = time.time() - start_time
            eta = (elapsed / done_count * (total - done_count)) if done_count > 0 else 0
            logger.info(
                "Progress: %d/%d (%.1f%%) | embedded=%d gcs=%d | ETA=%.0fm",
                done_count, total, pct, total_embedded, total_gcs, eta / 60,
            )

    # Step 4: Trigger Vertex AI import
    logger.info("Triggering Vertex AI INCREMENTAL import...")
    try:
        op = trigger_vertex_import()
        logger.info("Import operation: %s", op)
    except Exception as exc:
        logger.error("Vertex AI import trigger failed: %s", exc)

    # Step 5: Summary
    elapsed = time.time() - start_time
    logger.info("=== Initial ticket-index upload complete ===")
    logger.info("Total tickets processed: %d / %d", len(completed_ids), total)
    logger.info("Elapsed: %.0f minutes", elapsed / 60)

    # Verification queries
    logger.info("--- Verification ---")
    try:
        row = list(bq_client.query(
            f"SELECT COUNT(*) AS cnt, MAX(ARRAY_LENGTH(embedding)) AS dims "
            f"FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings`"
        ).result())[0]
        logger.info("ticket_embeddings: count=%d, dims=%s", row.cnt, row.dims)
    except Exception as exc:
        logger.warning("Verification query failed: %s", exc)

    try:
        bucket = gcs_client.bucket(GCS_BUCKET)
        blobs = list(bucket.list_blobs(prefix=f"{GCS_PREFIX}/"))
        logger.info("GCS tickets/: %d files", len(blobs))
    except Exception as exc:
        logger.warning("GCS verification failed: %s", exc)

    logger.info("ALL DONE")


if __name__ == "__main__":
    main()
