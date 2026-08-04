"""
OCR pipeline: extract text from ticket attachment screenshots via Gemini Vision.

Reads all unprocessed image attachments from BigQuery (ticket_attachments),
downloads each image from content_url, runs OCR via Gemini Vision,
and stores results in the ticket_attachment_ocr table.

Results are stored in a separate table instead of updating ticket_attachments in place —
BQ DML UPDATE on 11k rows would be prohibitively expensive; streaming inserts are instant.

Resume is automatic: unprocessed = images in ticket_attachments but NOT in ticket_attachment_ocr.
Re-run safely after interruption — already-processed images are skipped.

Concurrency: OCR uses ThreadPoolExecutor (default 10 workers) to parallelize
download + Gemini Vision calls. Sequential rate was ~6s/image = 30h for 18k images.
With 10 workers: ~3h.

Usage:
  # Count images to process (no API calls, no writes)
  python scripts/ocr_attachments.py --dry-run

  # Process all unprocessed images
  python scripts/ocr_attachments.py

  # Control concurrency (default: 10)
  python scripts/ocr_attachments.py --workers 15

  # Test with first 20 images
  python scripts/ocr_attachments.py --limit 20

  # Only images from a specific ticket (for debugging)
  python scripts/ocr_attachments.py --ticket-id 950024
"""

import argparse
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import requests
from google.cloud import bigquery as bq_module
from google.oauth2 import service_account

from app.config import settings
from app.services.gemini import ocr_image  # reuses the shared Vertex AI model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

GCP_PROJECT = settings.google_cloud_project_id
BQ_DATASET  = "esip_reference"
OCR_TABLE   = "ticket_attachment_ocr"

MAX_RETRIES      = 3
RETRY_BACKOFF    = 5
MAX_IMAGE_BYTES  = 15 * 1024 * 1024   # 15 MB
WRITE_BATCH_SIZE = 100                 # flush to BQ every N results

# MIME types that Gemini Vision natively supports
SUPPORTED_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}

OCR_TABLE_SCHEMA = [
    bq_module.SchemaField("content_url",       "STRING",    mode="REQUIRED"),
    bq_module.SchemaField("ticket_id",         "INT64"),
    bq_module.SchemaField("ocr_text",          "STRING"),
    bq_module.SchemaField("processing_status", "STRING"),   # success / skipped / failed
    bq_module.SchemaField("skip_reason",       "STRING"),   # None for success
    bq_module.SchemaField("processed_at",      "TIMESTAMP"),
]


def _get_bq_client() -> bq_module.Client:
    # On GCP VMs, credentials file may not exist — fall back to Application Default Credentials.
    try:
        creds = service_account.Credentials.from_service_account_file(
            settings.google_application_credentials,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return bq_module.Client(project=GCP_PROJECT, credentials=creds)
    except Exception:
        return bq_module.Client(project=GCP_PROJECT)


def ensure_ocr_table(client: bq_module.Client) -> None:
    """Create ticket_attachment_ocr table if it does not exist."""
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.{OCR_TABLE}"
    try:
        client.get_table(table_ref)
    except Exception:
        table = bq_module.Table(table_ref, schema=OCR_TABLE_SCHEMA)
        client.create_table(table)
        log.info("Created table %s", OCR_TABLE)


def _fetch_pending_images(
    client: bq_module.Client,
    limit: int | None = None,
    ticket_id: int | None = None,
) -> list[dict]:
    """Return image attachments not yet in ticket_attachment_ocr."""
    ticket_filter = f"AND a.ticket_id = {ticket_id}" if ticket_id else ""
    limit_clause  = f"LIMIT {limit}" if limit else ""

    sql = f"""
    SELECT
        a.ticket_id,
        a.content_url,
        COALESCE(a.content_type, '') AS content_type,
        COALESCE(a.size_bytes, 0)    AS size_bytes
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.{OCR_TABLE}` o
      ON a.content_url = o.content_url
    WHERE a.is_image = TRUE
      AND a.content_url IS NOT NULL
      AND o.content_url IS NULL
      {ticket_filter}
    ORDER BY a.ticket_id, a.content_url
    {limit_clause}
    """
    rows = list(client.query(sql).result())
    return [
        {
            "ticket_id":    r.ticket_id,
            "content_url":  r.content_url,
            "content_type": r.content_type,
            "size_bytes":   r.size_bytes,
        }
        for r in rows
    ]


def _download_image(url: str) -> tuple[bytes | None, str]:
    """Download image bytes. Returns (bytes, "") on success, (None, reason) on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=30, stream=True)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", RETRY_BACKOFF))
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None, f"http_{r.status_code}"
            content = b""
            for chunk in r.iter_content(chunk_size=64 * 1024):
                content += chunk
                if len(content) > MAX_IMAGE_BYTES:
                    return None, "too_large"
            return content, ""
        except Exception as exc:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
            else:
                return None, f"download_error:{exc}"
    return None, "download_failed"


def _process_one(img: dict, now_iso: str) -> dict:
    """
    Worker function: download + OCR one image. Returns a BQ row dict.
    Designed for ThreadPoolExecutor — each call is independent.
    """
    content_url  = img["content_url"]
    content_type = img["content_type"]
    ticket_id    = img["ticket_id"]
    size_bytes   = img["size_bytes"]

    # Skip unsupported MIME types
    if content_type not in SUPPORTED_MIME:
        return {
            "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
            "processing_status": "skipped",
            "skip_reason": f"unsupported_type:{content_type}",
            "processed_at": now_iso,
        }

    # Skip very large files (pre-flight)
    if size_bytes and size_bytes > MAX_IMAGE_BYTES:
        return {
            "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
            "processing_status": "skipped", "skip_reason": "too_large",
            "processed_at": now_iso,
        }

    # Download
    image_bytes, err = _download_image(content_url)
    if err:
        return {
            "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
            "processing_status": "skipped" if err == "too_large" else "failed",
            "skip_reason": err, "processed_at": now_iso,
        }
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return {
            "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
            "processing_status": "skipped", "skip_reason": "too_large",
            "processed_at": now_iso,
        }

    # OCR
    effective_mime = content_type if content_type in SUPPORTED_MIME else "image/jpeg"
    ocr_text, ocr_err = ocr_image(image_bytes, effective_mime)
    if ocr_err:
        return {
            "content_url": content_url, "ticket_id": ticket_id, "ocr_text": None,
            "processing_status": "failed", "skip_reason": ocr_err,
            "processed_at": now_iso,
        }

    return {
        "content_url": content_url, "ticket_id": ticket_id,
        "ocr_text": ocr_text,   # None = image had no text
        "processing_status": "success", "skip_reason": None,
        "processed_at": now_iso,
    }


def _flush(client: bq_module.Client, buffer: list[dict]) -> None:
    if not buffer:
        return
    errors = client.insert_rows_json(f"{GCP_PROJECT}.{BQ_DATASET}.{OCR_TABLE}", buffer)
    if errors:
        log.error("BQ insert errors (%d rows): %s", len(buffer), errors[:2])


def run(
    dry_run:   bool      = False,
    limit:     int | None = None,
    ticket_id: int | None = None,
    workers:   int       = 10,
) -> dict:
    """Main OCR loop with concurrent processing."""
    client = _get_bq_client()
    ensure_ocr_table(client)

    log.info("Fetching pending images...")
    images = _fetch_pending_images(client, limit=limit, ticket_id=ticket_id)
    total = len(images)
    log.info("Images to process: %d (workers=%d)", total, workers)

    if dry_run or total == 0:
        log.info("[%s] — %d images", "dry_run" if dry_run else "nothing_to_do", total)
        return {"total": total, "success": 0, "skipped": 0, "failed": 0}

    now_iso = datetime.now(timezone.utc).isoformat()

    # Thread-safe state
    lock          = threading.Lock()
    buffer:       list[dict] = []
    done_count    = 0
    success_count = 0
    skipped_count = 0
    failed_count  = 0

    def on_result(row: dict) -> None:
        nonlocal buffer, done_count, success_count, skipped_count, failed_count
        with lock:
            buffer.append(row)
            done_count += 1
            status = row["processing_status"]
            if status == "success":
                success_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                failed_count += 1

            if done_count % 100 == 0 or done_count == total:
                log.info("[%d/%d] success=%d skipped=%d failed=%d",
                         done_count, total, success_count, skipped_count, failed_count)

            if len(buffer) >= WRITE_BATCH_SIZE:
                to_flush = buffer[:]
                buffer = []
                return to_flush
        return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_process_one, img, now_iso): img
            for img in images
        }
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                img = futures[future]
                row = {
                    "content_url": img["content_url"], "ticket_id": img["ticket_id"],
                    "ocr_text": None, "processing_status": "failed",
                    "skip_reason": f"worker_exception:{exc}",
                    "processed_at": now_iso,
                }
            to_flush = on_result(row)
            if to_flush:
                _flush(client, to_flush)

    # Final flush of remaining buffer
    with lock:
        if buffer:
            _flush(client, buffer)

    log.info("=" * 60)
    log.info("OCR COMPLETE — %d total | success=%d | skipped=%d | failed=%d",
             total, success_count, skipped_count, failed_count)
    log.info("=" * 60)
    return {
        "total": total, "success": success_count,
        "skipped": skipped_count, "failed": failed_count,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCR ticket attachment images via Gemini Vision")
    parser.add_argument("--dry-run",   action="store_true",     help="Count images only, no API calls")
    parser.add_argument("--limit",     type=int, default=None,  help="Process at most N images")
    parser.add_argument("--ticket-id", type=int, default=None,  help="Only images for this ticket ID")
    parser.add_argument("--workers",   type=int, default=10,    help="Concurrent worker threads (default: 10)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, limit=args.limit, ticket_id=args.ticket_id, workers=args.workers)
