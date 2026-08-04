"""
Backfill script: extract inline images from Zendesk comment HTML bodies
and insert them into the ticket_attachments BQ table.

Background: the export_tickets_bq.py script had a bug where it searched
the plain-text `body` for <img> tags (never present) instead of `html_body`.
This means ALL inline images are missing from ticket_attachments in BQ.

This script:
  1. Reads all ticket IDs from BQ
  2. For each ticket, fetches comments from Zendesk API (to get html_body)
  3. Extracts inline <img> URLs from html_body
  4. Inserts new rows into ticket_attachments (deduplication via existing URL set)

Resumable: tracks progress in .backfill_inline_checkpoint.json
  - After interruption, re-run and it will skip already-processed tickets.

Usage:
  python scripts/backfill_inline_images.py
  python scripts/backfill_inline_images.py --workers 15
  python scripts/backfill_inline_images.py --dry-run      # count only, no writes
  python scripts/backfill_inline_images.py --ticket-id 950024  # single ticket
  python scripts/backfill_inline_images.py --since-date 2026-01-01  # recent only
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import requests
from google.cloud import bigquery
from google.oauth2 import service_account

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

GCP_PROJECT      = settings.google_cloud_project_id
BQ_DATASET       = "esip_reference"
SCRIPTS_DIR      = Path(__file__).parent
CHECKPOINT_FILE  = SCRIPTS_DIR / ".backfill_inline_checkpoint.json"

ZENDESK_BASE  = f"https://{settings.zendesk_subdomain}.zendesk.com/api/v2"
ZENDESK_AUTH  = (f"{settings.zendesk_email}/token", settings.zendesk_api_token)

MAX_RETRIES     = 3
RETRY_BACKOFF   = 5
WRITE_BATCH     = 200   # flush BQ every N image rows
SLEEP_ON_LIMIT  = 0.5   # seconds between Zendesk requests per worker
                        # 5 workers × 2 req/s = 10 req/s = 600 req/min → stays under Zendesk 700 req/min limit

# Only Zendesk-hosted inline images (not external CDN images like logos)
INLINE_URL_PATTERN = re.compile(
    r'<img[^>]+src=["\']([^"\']*(?:zendesk\.com|zen\.desk)[^"\']*)["\']',
    re.IGNORECASE,
)


def _get_bq_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


def _zendesk_get(url: str, params: dict = None) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(url, params=params, auth=ZENDESK_AUTH, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", RETRY_BACKOFF))
            log.warning("Rate limited — sleeping %ds", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(RETRY_BACKOFF * attempt)
            continue
        if r.status_code == 404:
            return {}  # ticket deleted or inaccessible
        r.raise_for_status()
        return r.json()
    return {}


def _fetch_ticket_ids(
    client: bigquery.Client,
    since_date: str | None,
    ticket_id: int | None,
) -> list[int]:
    if ticket_id:
        return [ticket_id]
    where = f"WHERE created_at >= '{since_date}'" if since_date else ""
    sql = f"""
    SELECT id FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    {where}
    ORDER BY id
    """
    return [r.id for r in client.query(sql).result()]


def _fetch_existing_urls(client: bigquery.Client) -> set[str]:
    """Load all content_urls already in ticket_attachments to skip duplicates."""
    sql = f"""
    SELECT DISTINCT content_url
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments`
    WHERE content_url IS NOT NULL
    """
    return {r.content_url for r in client.query(sql).result()}


def _process_ticket(ticket_id: int, existing_urls: set[str], now_iso: str) -> list[dict]:
    """Fetch comments for one ticket and extract inline image rows."""
    data = _zendesk_get(f"{ZENDESK_BASE}/tickets/{ticket_id}/comments")
    comments = data.get("comments", [])
    rows = []
    for c in comments:
        html_body = c.get("html_body") or ""
        if not html_body:
            continue
        created_at = c.get("created_at", now_iso)
        comment_id = c.get("id")
        for url in INLINE_URL_PATTERN.findall(html_body):
            if url in existing_urls:
                continue
            # Derive a file name from the URL
            file_name = url.rsplit("/", 1)[-1].split("?")[0] or "image.png"
            rows.append({
                "ticket_id":     ticket_id,
                "comment_id":    comment_id,
                "attachment_id": None,
                "file_name":     file_name,
                "content_type":  "image/png",   # Zendesk inline images are always PNG
                "size_bytes":    None,
                "url":           url,
                "content_url":   url,
                "is_image":      True,
                "created_at":    created_at,
                "exported_at":   now_iso,
            })
    time.sleep(SLEEP_ON_LIMIT)
    return rows


def _flush(client: bigquery.Client, buffer: list[dict], dry_run: bool) -> None:
    if not buffer or dry_run:
        return
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments"
    errors = client.insert_rows_json(table_id, buffer)
    if errors:
        log.error("BQ insert errors (%d rows): %s", len(buffer), errors[:2])


def _load_checkpoint() -> set[int]:
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        return set(data.get("done", []))
    return set()


def _save_checkpoint(done: set[int]) -> None:
    CHECKPOINT_FILE.write_text(json.dumps({"done": sorted(done)}))


def run(
    dry_run:   bool       = False,
    workers:   int        = 5,
    ticket_id: int | None = None,
    since_date: str | None = None,
) -> None:
    client = _get_bq_client()

    log.info("Loading existing attachment URLs from BQ...")
    existing_urls = _fetch_existing_urls(client)
    log.info("Existing attachment URLs: %d", len(existing_urls))

    log.info("Loading ticket IDs from BQ...")
    ticket_ids = _fetch_ticket_ids(client, since_date, ticket_id)
    log.info("Tickets to scan: %d", len(ticket_ids))

    # Resume support
    done_ids = _load_checkpoint() if not ticket_id else set()
    remaining = [t for t in ticket_ids if t not in done_ids]
    log.info("Already done (checkpoint): %d — scanning %d remaining",
             len(done_ids), len(remaining))

    if not remaining:
        log.info("Nothing to do.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()

    lock           = threading.Lock()
    buffer:        list[dict] = []
    total_images   = 0
    total_tickets  = 0
    total_done     = 0
    local_done     = set()

    def on_result(ticket: int, rows: list[dict]) -> list[dict] | None:
        nonlocal buffer, total_images, total_tickets, total_done
        with lock:
            if rows:
                # Add new URLs to existing_urls to avoid duplicates within this run
                for r in rows:
                    existing_urls.add(r["content_url"])
                buffer.extend(rows)
                total_images  += len(rows)
                total_tickets += 1
            done_ids.add(ticket)
            local_done.add(ticket)
            total_done += 1

            if total_done % 1000 == 0 or total_done == len(remaining):
                log.info("[%d/%d] inline_images_found=%d tickets_with_images=%d",
                         total_done, len(remaining), total_images, total_tickets)
                _save_checkpoint(done_ids)

            if len(buffer) >= WRITE_BATCH:
                to_flush = buffer[:]
                buffer.clear()
                return to_flush
        return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_process_ticket, t, existing_urls, now_iso): t
            for t in remaining
        }
        for future in as_completed(futures):
            tid = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                log.warning("Error processing ticket %d: %s", tid, exc)
                rows = []
            to_flush = on_result(tid, rows)
            if to_flush:
                _flush(client, to_flush, dry_run)

    # Final flush
    with lock:
        if buffer:
            _flush(client, buffer, dry_run)
        _save_checkpoint(done_ids)

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE — scanned=%d new_inline_images=%d tickets_with_images=%d%s",
             len(remaining), total_images, total_tickets,
             " [DRY RUN — no writes]" if dry_run else "")
    log.info("=" * 60)

    if not dry_run and CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        log.info("Checkpoint file removed (run complete).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill inline images into ticket_attachments")
    parser.add_argument("--dry-run",    action="store_true",    help="Count only, no BQ writes")
    parser.add_argument("--workers",    type=int, default=5,    help="Concurrent Zendesk fetches (default: 5, stay under rate limit)")
    parser.add_argument("--ticket-id",  type=int, default=None, help="Process a single ticket ID")
    parser.add_argument("--since-date", type=str, default=None, help="Only tickets created after YYYY-MM-DD")
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        workers=args.workers,
        ticket_id=args.ticket_id,
        since_date=args.since_date,
    )
