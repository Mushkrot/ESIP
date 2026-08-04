"""Full OCR reprocessing pipeline.

The runner rebuilds OCR text for eligible ticket attachments, preserves the
previous text and timestamp when an attachment is unreachable or its text is
unchanged, and writes the rebuilt table through a bounded replacement step.
Ticket documents are then refreshed through the normal incremental
vectorization rules. Eligible Drive images can be refreshed in the same
controlled run.

Run from the project root only after reviewing the target environment, cost
limits, and rollback path::

    python3 scripts/reprocess_all_ocr.py

Runtime depends on attachment volume and provider limits.
"""

import io
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, "backend")
from dotenv import load_dotenv
load_dotenv(override=True)

from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET
from app.services.gemini import ocr_image
from google.cloud import bigquery

import requests as _requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scripts/reprocess_ocr.log", mode="w"),
    ],
)
# suppress noisy libs
for name in ["urllib3", "google.auth", "google.api_core", "httpx"]:
    logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("reprocess_ocr")

_SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "15"))
_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_DRIVE_IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}

OCR_TABLE_SCHEMA = [
    bigquery.SchemaField("content_url",        "STRING"),
    bigquery.SchemaField("ticket_id",          "INTEGER"),
    bigquery.SchemaField("ocr_text",           "STRING"),
    bigquery.SchemaField("processing_status",  "STRING"),
    bigquery.SchemaField("skip_reason",        "STRING"),
    bigquery.SchemaField("processed_at",       "TIMESTAMP"),
]


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def phase1_reocr_ticket_images() -> dict:
    """
    Re-OCR all ticket images. Returns counts dict.
    Uses WRITE_TRUNCATE to replace ticket_attachment_ocr entirely.
    """
    logger.info("=" * 60)
    logger.info("PHASE 1: Re-OCR ticket images")
    logger.info("=" * 60)

    client = _get_client()
    now = datetime.now(timezone.utc)
    now_iso = _ts(now)

    # Fetch all rows
    logger.info("Fetching all rows from ticket_attachment_ocr...")
    all_rows = list(client.query(f"""
        SELECT content_url, ticket_id, ocr_text, processing_status, skip_reason, processed_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`
        ORDER BY ticket_id
    """).result())
    logger.info("Total rows: %d", len(all_rows))

    # Separate: re-OCR candidates vs keep-as-is
    to_reocr = [r for r in all_rows if r.processing_status == "success"]
    keep_as_is = [r for r in all_rows if r.processing_status != "success"]

    logger.info("To re-OCR: %d | Keep as-is (skipped/failed): %d", len(to_reocr), len(keep_as_is))

    reocr_success = 0
    reocr_url_expired = 0
    reocr_ocr_failed = 0
    new_rows: list[dict] = []

    def process_one(row) -> dict:
        content_url = row.content_url
        ticket_id = row.ticket_id
        old_ocr_text = row.ocr_text
        # Preserve old timestamp when text doesn't change — prevents mass re-embedding cascade.
        # processed_at is only bumped when OCR text actually changes (different content).
        old_processed_at = _ts(row.processed_at) if row.processed_at else now_iso

        try:
            r = _requests.get(content_url, timeout=30)
            if r.status_code != 200:
                # URL expired — text unchanged, preserve old processed_at (no re-embedding)
                return {
                    "content_url": content_url, "ticket_id": ticket_id,
                    "ocr_text": old_ocr_text, "processing_status": "success",
                    "skip_reason": None, "processed_at": old_processed_at,
                    "_status": "url_expired",
                }
            image_bytes = r.content
            if len(image_bytes) > _MAX_IMAGE_BYTES:
                # Too large — text unchanged, preserve old processed_at
                return {
                    "content_url": content_url, "ticket_id": ticket_id,
                    "ocr_text": old_ocr_text, "processing_status": "success",
                    "skip_reason": None, "processed_at": old_processed_at,
                    "_status": "too_large_kept",
                }
        except Exception as exc:
            # Download error — text unchanged, preserve old processed_at
            return {
                "content_url": content_url, "ticket_id": ticket_id,
                "ocr_text": old_ocr_text, "processing_status": "success",
                "skip_reason": None, "processed_at": old_processed_at,
                "_status": f"download_error:{exc}",
            }

        # Determine MIME type from content type header
        ct = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if ct not in _SUPPORTED_MIME:
            ct = "image/jpeg"

        new_text, err = ocr_image(image_bytes, ct)
        if err:
            # OCR failed — text unchanged, preserve old processed_at
            return {
                "content_url": content_url, "ticket_id": ticket_id,
                "ocr_text": old_ocr_text, "processing_status": "success",
                "skip_reason": None, "processed_at": old_processed_at,
                "_status": f"ocr_error:{err}",
            }

        # Only bump processed_at when OCR text actually changed — this is the re-embedding trigger.
        # If text is identical (prompt unchanged, image unchanged), preserve old timestamp.
        effective_processed_at = now_iso if new_text != old_ocr_text else old_processed_at
        return {
            "content_url": content_url, "ticket_id": ticket_id,
            "ocr_text": new_text, "processing_status": "success",
            "skip_reason": None, "processed_at": effective_processed_at,
            "_status": "ok",
        }

    # Process concurrently
    logger.info("Starting concurrent re-OCR with %d workers...", _MAX_WORKERS)
    done_count = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(process_one, row): row for row in to_reocr}
        for future in as_completed(futures):
            result = future.result()
            status = result.pop("_status", "ok")
            new_rows.append(result)
            done_count += 1

            if status == "ok":
                reocr_success += 1
            elif status == "url_expired":
                reocr_url_expired += 1
            else:
                reocr_ocr_failed += 1

            if done_count % 500 == 0:
                elapsed = time.time() - start_time
                rate = done_count / elapsed
                remaining = (len(to_reocr) - done_count) / rate if rate > 0 else 0
                logger.info(
                    "Progress: %d/%d (%.1f/s) | ok=%d expired=%d failed=%d | ETA: %.0f min",
                    done_count, len(to_reocr), rate,
                    reocr_success, reocr_url_expired, reocr_ocr_failed,
                    remaining / 60,
                )

    # Add kept rows
    for row in keep_as_is:
        new_rows.append({
            "content_url": row.content_url, "ticket_id": row.ticket_id,
            "ocr_text": row.ocr_text, "processing_status": row.processing_status,
            "skip_reason": row.skip_reason,
            "processed_at": now_iso,  # bump timestamp so vectorize detects them
        })

    logger.info(
        "Re-OCR complete: ok=%d url_expired=%d failed=%d | Total rows to write: %d",
        reocr_success, reocr_url_expired, reocr_ocr_failed, len(new_rows),
    )

    # WRITE_TRUNCATE — replace entire table
    logger.info("Writing %d rows to ticket_attachment_ocr (WRITE_TRUNCATE)...", len(new_rows))
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr"
    ndjson = "\n".join(json.dumps(r) for r in new_rows).encode("utf-8")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=OCR_TABLE_SCHEMA,
    )
    load_job = client.load_table_from_file(io.BytesIO(ndjson), table_ref, job_config=job_config)
    load_job.result()
    if load_job.errors:
        logger.error("BQ load errors: %s", load_job.errors)
    else:
        logger.info("ticket_attachment_ocr rebuilt successfully (%d rows)", len(new_rows))

    return {
        "total_processed": len(to_reocr),
        "reocr_success": reocr_success,
        "reocr_url_expired": reocr_url_expired,
        "reocr_failed": reocr_ocr_failed,
        "total_rows_written": len(new_rows),
    }


def phase2_reembed_tickets() -> dict:
    """
    Re-embed all tickets with images using vectorize_new_tickets().
    Since processed_at was bumped to now in phase 1, all 45k tickets will be detected.
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: Re-embed tickets + rebuild GCS + Vertex AI")
    logger.info("=" * 60)

    from app.services.bq_sync import vectorize_new_tickets

    def progress(msg: str) -> None:
        logger.info("[phase2] %s", msg)

    result = vectorize_new_tickets(progress_cb=progress)
    logger.info("Phase 2 complete: %s", result)
    return result


def phase3_reprocess_drive_images() -> dict:
    """
    Force re-process Drive image files with new OCR prompt.
    Clears image file entries from drive_chunks so sync treats them as new.
    """
    logger.info("=" * 60)
    logger.info("PHASE 3: Re-process Drive images")
    logger.info("=" * 60)

    from app.services.drive_sync import (
        _get_drive_creds, _get_gcp_token, list_all_drive_files,
        download_file, extract_text, upload_to_gcs, upsert_vertex_doc,
        chunk_text, SOURCE_ID, DRIVE_FOLDER_ID,
    )
    from app.services.bigquery import (
        get_indexed_drive_files, get_drive_chunks_for_files,
        build_drive_chunk_rows, bulk_write_drive_chunks,
    )

    drive_creds = _get_drive_creds()
    gcp_token = _get_gcp_token()

    # List all Drive files
    drive_files = list_all_drive_files(drive_creds)
    image_files = [f for f in drive_files if f["mimeType"] in _DRIVE_IMAGE_MIMETYPES]
    non_image_files = [f for f in drive_files if f["mimeType"] not in _DRIVE_IMAGE_MIMETYPES]

    logger.info("Drive: %d total, %d images to re-process, %d non-images to keep",
                len(drive_files), len(image_files), len(non_image_files))

    # Get current indexed state
    indexed = get_indexed_drive_files(SOURCE_ID)
    non_image_ids = {f["id"] for f in non_image_files}

    all_bq_rows: list[dict] = []
    processed = 0
    errors = 0

    # Re-process all image files
    for f in image_files:
        file_id   = f["id"]
        file_name = f["name"]
        mime_type = f["mimeType"]
        modified  = f.get("modifiedTime", "")
        drive_url = f"https://drive.google.com/file/d/{file_id}/view"

        logger.info("Re-processing image: %s", file_name)
        try:
            content = download_file(file_id, mime_type, drive_creds)
            text = extract_text(content, mime_type, file_name)

            full_text = f"FILE: {file_name}\nSOURCE: SD Documentation (Google Drive)\n\n{text}"
            gcs_uri = upload_to_gcs(file_id, full_text, gcp_token)
            upsert_vertex_doc(file_id, file_name, gcs_uri, drive_url)

            chunks = chunk_text(full_text)
            rows = build_drive_chunk_rows(
                file_id=file_id,
                file_name=file_name,
                source_id=SOURCE_ID,
                source_path="",
                source_path_slug="root",
                drive_url=drive_url,
                modified_at=modified,
                chunks=chunks,
            )
            all_bq_rows.extend(rows)

            if text.strip():
                logger.info("  -> %d chunks, text: %s...", len(rows), text[:80])
            else:
                logger.info("  -> %d chunks (description only)", len(rows))
            processed += 1
        except Exception as exc:
            logger.error("  -> ERROR: %s", exc)
            errors += 1

    # Fetch existing BQ rows for non-image files (unchanged)
    logger.info("Fetching existing BQ chunks for %d non-image files...", len(non_image_ids))
    existing_rows = get_drive_chunks_for_files(non_image_ids)
    all_bq_rows.extend(existing_rows)
    logger.info("Total BQ rows to write: %d", len(all_bq_rows))

    # Write all with WRITE_TRUNCATE
    bulk_write_drive_chunks(all_bq_rows)
    logger.info("Drive phase complete: processed=%d errors=%d total_chunks=%d",
                processed, errors, len(all_bq_rows))

    return {"images_processed": processed, "errors": errors, "total_chunks": len(all_bq_rows)}


def main():
    logger.info("Starting full OCR re-processing pipeline")
    logger.info("Time: %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    start = time.time()

    results = {}

    # Phase 1: Re-OCR ticket images
    if os.environ.get("SKIP_PHASE1", "false").lower() == "true":
        logger.info("PHASE 1: Skipped (SKIP_PHASE1=true)")
        results["phase1"] = {"skipped": True}
    else:
        try:
            results["phase1"] = phase1_reocr_ticket_images()
        except Exception as exc:
            logger.error("PHASE 1 FAILED: %s", exc, exc_info=True)
            results["phase1"] = {"error": str(exc)}

    # Phase 2: Re-embed tickets
    try:
        results["phase2"] = phase2_reembed_tickets()
    except Exception as exc:
        logger.error("PHASE 2 FAILED: %s", exc, exc_info=True)
        results["phase2"] = {"error": str(exc)}

    # Phase 3: Re-process Drive images (requires JSON key for DWD — skip on GCP)
    if os.environ.get("SKIP_PHASE3", "false").lower() == "true":
        logger.info("PHASE 3: Skipped (SKIP_PHASE3=true)")
        results["phase3"] = {"skipped": True}
    else:
        try:
            results["phase3"] = phase3_reprocess_drive_images()
        except Exception as exc:
            logger.error("PHASE 3 FAILED: %s", exc, exc_info=True)
            results["phase3"] = {"error": str(exc)}

    elapsed = time.time() - start

    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE — %.1f minutes", elapsed / 60)
    logger.info("=" * 60)
    logger.info("Phase 1 (OCR):     %s", results.get("phase1"))
    logger.info("Phase 2 (Embed):   %s", results.get("phase2"))
    logger.info("Phase 3 (Drive):   %s", results.get("phase3"))


if __name__ == "__main__":
    main()
