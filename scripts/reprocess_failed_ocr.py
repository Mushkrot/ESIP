"""
Re-OCR script for ticket images that were processed without [IMAGE DESCRIPTION:].

These images have processing_status='success' in ticket_attachment_ocr but no
[IMAGE DESCRIPTION:] tag, commonly because their earlier Zendesk CDN URLs expired.

What this script does:
  1. Queries BQ for all failed images (no [IMAGE DESCRIPTION:])
  2. Looks up fresh attachment URLs from Zendesk API (by ticket_id + file_name)
  3. Downloads each image and re-OCRs with the current prompt
  4. Updates ticket_attachment_ocr rows where text actually changed — IMMEDIATELY
     after each successful OCR (not in batch at end)

Safety guarantees:
  - thinking_budget=0 enforced inside ocr_image() + divergence guard
  - cost_gate checked per image — halts if daily limit reached
  - Only updates rows where OCR text actually changes
  - Precondition: relevant sync schedulers must be paused before a write run
  - --dry-run mode: shows what would happen without making any changes
  - Resume token: scripts/.ocr_reprocess_done.txt tracks completed URLs
    so re-runs never re-OCR already-processed images (no double billing)
  - Repeated failures: scripts/.ocr_reprocess_failures.json counts per URL;
    after --abandon-after consecutive failures (default 3), BQ UPDATE sets
    processing_status='skipped' and skip_reason='reprocess_abandoned:…' so the
    row leaves the cohort (no infinite retry). Use --abandon-after 0 to disable.

Usage:
  python3 scripts/reprocess_failed_ocr.py [--dry-run] [--limit N] [--abandon-after K]

Options:
  --dry-run   Print plan without downloading or writing anything
  --limit N   Process at most N images after excluding resume-file URLs
              (stable ORDER BY ticket_id among remaining rows; default: all)
  --abandon-after K   After K consecutive failures for the same content_url,
              mark row skipped in BQ and stop retrying (0 = disabled; default 3)
  --failure-state-json PATH   Override path for failure counter JSON (default:
              scripts/.ocr_reprocess_failures.json)

Cost planning uses the reviewed pricing profile plus a conservative per-image
upper bound enforced by the cost gate.

Resume safety: completed URLs are tracked in scripts/.ocr_reprocess_done.txt
so re-runs never re-OCR already-processed images (no double billing).
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "backend")
from dotenv import load_dotenv
load_dotenv(override=True)

from app.config import settings
from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET
from app.services.bq_sync import _download_zendesk_attachment, _extract_ticket_attachment_text
from app.services.cost_tracker import (
    CostGateError, CostDivergenceError,
    get_today_spent, get_budget,
    flush_bq_events,
)

import requests as _requests

# ---------------------------------------------------------------------------
# Resume token — persists processed URLs across runs to prevent double billing
# ---------------------------------------------------------------------------

DONE_FILE = Path("scripts/.ocr_reprocess_done.txt")
FAILURE_STATE_DEFAULT = Path("scripts/.ocr_reprocess_failures.json")
_SKIP_REASON_MAX = 900


def load_failure_state(path: Path) -> dict[str, dict]:
    """Per-content_url failure counts between runs. Values: {n, last_status, last_at}."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for url, v in raw.items():
        if not isinstance(url, str) or not isinstance(v, dict):
            continue
        n = v.get("n")
        if not isinstance(n, int) or n < 1:
            continue
        out[url] = {
            "n": n,
            "last_status": str(v.get("last_status", ""))[:200],
            "last_at": str(v.get("last_at", ""))[:80],
        }
    return out


def save_failure_state(path: Path, state: dict[str, dict]) -> None:
    """Atomic replace so concurrent runs do not corrupt JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_reprocess_failure_status(status: str) -> bool:
    """Failures that count toward abandon (not cost halts)."""
    if status == "ok" or status == "?":
        return False
    if status == "no_fresh_url" or status == "too_large":
        return True
    if "url_error" in status or "download_error" in status:
        return True
    if "ocr_error" in status or "ocr_failed" in status:
        return True
    if status.startswith("corrupt_"):
        return True
    return False


def load_done_urls() -> set[str]:
    """Load set of content_urls already successfully OCR'd and written to BQ."""
    if not DONE_FILE.exists():
        return set()
    return set(DONE_FILE.read_text().splitlines())

def mark_done_url(content_url: str) -> None:
    """Append a URL to the done file immediately after BQ write succeeds."""
    with open(DONE_FILE, "a") as f:
        f.write(content_url + "\n")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scripts/reprocess_failed_ocr.log", mode="a"),
    ],
)
for noisy in ["urllib3", "google.auth", "google.api_core", "httpx"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger("reprocess_failed_ocr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_WORKERS      = 6      # concurrent Gemini OCR calls (raise cautiously — Vertex 429 risk)
ZENDESK_RATE_S   = 0.15   # pause between Zendesk API calls (≈400/min max)
MAX_IMAGE_BYTES  = 20 * 1024 * 1024   # 20 MB safety cap

MAX_OCR_RETRIES  = 3      # Gemini retry attempts on transient errors
OCR_RETRY_BASE_S = 2.0    # base backoff seconds (doubles each attempt)

# BQ safety cap when fetching the full failed cohort (no user-facing --limit in SQL).
_SQL_FAILED_COHORT_CAP = 100_000

# ---------------------------------------------------------------------------
# Step 1 — Query BQ for failed images
# ---------------------------------------------------------------------------

def fetch_failed_images(client) -> list[dict]:
    """
    Return rows from ticket_attachment_ocr that have processing_status='success'
    but no [IMAGE DESCRIPTION:] in the OCR text.
    Joined with ticket_attachments to get file_name for URL matching.

    Ordered by ticket_id. Caller applies resume-file filter and optional --limit.
    """
    sql = f"""
        SELECT
            o.ticket_id,
            o.content_url          AS old_content_url,
            o.ocr_text             AS old_ocr_text,
            o.processed_at         AS old_processed_at,
            COALESCE(a.file_name, '') AS file_name,
            COALESCE(a.content_type, 'image/jpeg') AS content_type
        FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o
        LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
            ON a.content_url = o.content_url
        WHERE o.processing_status = 'success'
          AND NOT CONTAINS_SUBSTR(IFNULL(o.ocr_text, ''), '[IMAGE DESCRIPTION:')
        ORDER BY o.ticket_id
        LIMIT {_SQL_FAILED_COHORT_CAP}
    """
    rows = list(client.query(sql).result())
    return [
        {
            "ticket_id":       r.ticket_id,
            "old_content_url": r.old_content_url,
            "old_ocr_text":    r.old_ocr_text or "",
            "old_processed_at": r.old_processed_at,
            "file_name":       r.file_name,
            "content_type":    r.content_type,
        }
        for r in rows
    ]

# ---------------------------------------------------------------------------
# Step 2 — Fetch fresh Zendesk attachment URLs
# ---------------------------------------------------------------------------

def _zendesk_session() -> _requests.Session:
    s = _requests.Session()
    s.auth = settings.zendesk_auth   # (email/token, api_token) tuple
    s.headers.update({"Content-Type": "application/json"})
    return s


def fetch_fresh_urls_for_ticket(session: _requests.Session, ticket_id: int) -> list[dict]:
    """
    Call Zendesk API to get current attachment list for this ticket.
    Returns list of {file_name, content_url, content_type}.
    """
    base = settings.zendesk_base_url
    url = f"{base}/tickets/{ticket_id}/comments.json?include=attachments"
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Zendesk API error for ticket %s: %s", ticket_id, exc)
        return []

    attachments = []
    for comment in data.get("comments", []):
        for att in comment.get("attachments", []):
            attachments.append({
                "file_name":   att.get("file_name", ""),
                "content_url": att.get("content_url", ""),
                "content_type": att.get("content_type", "image/jpeg"),
            })
    return attachments


def match_fresh_url(fresh_attachments: list[dict], file_name: str, old_url: str) -> str | None:
    """
    Find the fresh URL for this image.
    Primary match: file_name (most reliable).
    Fallback: extract filename from old URL query param (?name=...).
    """
    # Try exact file_name match first
    if file_name:
        for att in fresh_attachments:
            if att["file_name"] == file_name:
                return att["content_url"]

    # Fallback: extract name from old URL
    if "?name=" in old_url:
        name_from_url = old_url.split("?name=")[-1].split("&")[0]
        for att in fresh_attachments:
            if att["file_name"] == name_from_url:
                return att["content_url"]

    # Last resort: filename portion of the URL path
    path_name = old_url.rsplit("/", 1)[-1].split("?")[0]
    if path_name:
        for att in fresh_attachments:
            if att["file_name"] == path_name:
                return att["content_url"]

    return None

# ---------------------------------------------------------------------------
# Step 3 — Download + OCR one image (with retry + backoff)
# ---------------------------------------------------------------------------

def process_one(item: dict, fresh_url: str) -> dict:
    """
    Download fresh_url, run OCR with retry, return result dict with _status key.
    Does NOT write to BQ — caller writes immediately after receiving 'ok' result.
    """
    try:
        from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable, InternalServerError as GcpInternalError
    except ImportError:
        DeadlineExceeded = ServiceUnavailable = GcpInternalError = Exception

    ticket_id     = item["ticket_id"]
    old_url       = item["old_content_url"]
    old_text      = item["old_ocr_text"]
    old_ts        = item["old_processed_at"]
    content_type  = item.get("content_type", "image/jpeg")

    content_bytes, downloaded_content_type, download_err = _download_zendesk_attachment(fresh_url)
    if download_err:
        return {**item, "_status": f"url_error:{download_err}", "_fresh_url": fresh_url,
                "_new_text": old_text, "_processed_at": _ts(old_ts)}
    if content_bytes is None:
        return {**item, "_status": "download_error:empty_response", "_fresh_url": fresh_url,
                "_new_text": old_text, "_processed_at": _ts(old_ts)}
    if len(content_bytes) > MAX_IMAGE_BYTES:
        return {**item, "_status": "too_large", "_fresh_url": fresh_url,
                "_new_text": old_text, "_processed_at": _ts(old_ts)}

    # Gemini OCR with retry + exponential backoff on transient errors
    new_text = None
    err = None
    for attempt in range(1, MAX_OCR_RETRIES + 1):
        try:
            declared_mime = (downloaded_content_type or content_type).split(";", 1)[0].strip()
            new_text, err, _effective_mime = _extract_ticket_attachment_text(content_bytes, declared_mime)
            break  # success
        except (CostGateError, CostDivergenceError):
            raise  # always propagate cost errors — halts the script
        except (DeadlineExceeded, ServiceUnavailable, GcpInternalError) as exc:
            if attempt < MAX_OCR_RETRIES:
                wait = OCR_RETRY_BASE_S * (2 ** (attempt - 1))
                logger.warning(
                    "Gemini transient error ticket %s attempt %d/%d, retry in %.1fs: %s",
                    ticket_id, attempt, MAX_OCR_RETRIES, wait, exc,
                )
                time.sleep(wait)
                continue
            return {**item, "_status": f"ocr_error:{exc}", "_fresh_url": fresh_url,
                    "_new_text": old_text, "_processed_at": _ts(old_ts)}
        except Exception as exc:
            return {**item, "_status": f"ocr_error:{exc}", "_fresh_url": fresh_url,
                    "_new_text": old_text, "_processed_at": _ts(old_ts)}

    if err:
        if err.startswith("corrupt_"):
            return {**item, "_status": err, "_fresh_url": fresh_url,
                    "_new_text": old_text, "_processed_at": _ts(old_ts)}
        return {**item, "_status": f"ocr_failed:{err}", "_fresh_url": fresh_url,
                "_new_text": old_text, "_processed_at": _ts(old_ts)}

    now_iso = datetime.now(timezone.utc).isoformat()
    text_changed = (new_text or "") != old_text
    return {
        **item,
        "_status": "ok",
        "_fresh_url": fresh_url,
        "_new_text": new_text or "",
        # Per Critical Rule #3: only bump processed_at when text actually changes
        "_processed_at": now_iso if text_changed else _ts(old_ts),
        "_text_changed": text_changed,
    }


def _ts(ts) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)

# ---------------------------------------------------------------------------
# Step 4 — Write ONE result to BQ immediately after OCR success
# ---------------------------------------------------------------------------

def update_bq_row(client, result: dict) -> bool:
    """
    UPDATE ticket_attachment_ocr for a single row where OCR text changed.
    Uses parameterized query to safely handle arbitrary OCR text.
    Returns True if update succeeded.
    """
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter  # noqa: PLC0415

    if result.get("_status") != "ok" or not result.get("_text_changed"):
        return False

    table = f"`{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`"
    sql = f"""
        UPDATE {table}
        SET ocr_text = @new_text,
            processed_at = TIMESTAMP(@new_ts)
        WHERE content_url = @old_url
    """
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("new_text", "STRING", result["_new_text"] or ""),
            ScalarQueryParameter("new_ts",   "STRING", result["_processed_at"]),
            ScalarQueryParameter("old_url",  "STRING", result["old_content_url"]),
        ]
    )
    try:
        client.query(sql, job_config=job_config).result()
        return True
    except Exception as exc:
        logger.error("BQ UPDATE failed for ticket %s: %s", result["ticket_id"], exc)
        return False


def abandon_row(client, content_url: str, last_status: str) -> bool:
    """
    Mark row as terminal skipped so it drops out of the re-OCR cohort.
    Does not change ocr_text or processed_at (AGENTS: no artificial timestamp bump).
    """
    from google.cloud.bigquery import QueryJobConfig, ScalarQueryParameter  # noqa: PLC0415

    reason = f"reprocess_abandoned:{last_status}"[:_SKIP_REASON_MAX]
    table = f"`{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`"
    sql = f"""
        UPDATE {table}
        SET processing_status = @status,
            skip_reason = @reason
        WHERE content_url = @url
    """
    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("status", "STRING", "skipped"),
            ScalarQueryParameter("reason", "STRING", reason),
            ScalarQueryParameter("url", "STRING", content_url),
        ]
    )
    try:
        job = client.query(sql, job_config=job_config)
        job.result()
        n = getattr(job, "num_dml_affected_rows", None)
        if n is not None and int(n) < 1:
            logger.warning("abandon_row: UPDATE matched 0 rows for url tail …%s", content_url[-48:])
            return False
        return True
    except Exception as exc:
        logger.error("abandon_row BQ UPDATE failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Re-OCR failed ticket attachment images")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without downloading or writing anything")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N images (default: all)")
    parser.add_argument(
        "--abandon-after",
        type=int,
        default=3,
        metavar="K",
        help="After K consecutive failures for the same URL, set BQ skipped (0=off; default 3)",
    )
    parser.add_argument(
        "--failure-state-json",
        type=Path,
        default=None,
        help="Path for per-URL failure counter JSON (default: scripts/.ocr_reprocess_failures.json)",
    )
    args = parser.parse_args()
    failure_state_path = args.failure_state_json or FAILURE_STATE_DEFAULT
    abandon_after = args.abandon_after

    dry_run = args.dry_run
    if dry_run:
        logger.info("=== DRY RUN MODE — no downloads, no BQ writes ===")

    failure_state: dict[str, dict] = {}
    if not dry_run:
        failure_state = load_failure_state(failure_state_path)
        if abandon_after > 0:
            logger.info(
                "Abandon policy: after %d consecutive failures → BQ skipped (%s)",
                abandon_after,
                failure_state_path,
            )
        else:
            logger.info("Abandon policy: disabled (--abandon-after 0)")

    # ── Pre-flight: verify Firestore is reachable + check today's spend ──────
    run_start_spent: float = 0.0   # will be set below for post-run comparison
    if not dry_run:
        try:
            already_spent = get_today_spent(strict=True)
            run_start_spent = already_spent
            budget = get_budget()
            hard_cap = budget["daily_limit_usd"] * budget["hard_cap_pct"] / 100
            logger.info(
                "Pre-flight cost check: already spent today $%.4f / hard cap $%.2f",
                already_spent, hard_cap,
            )
            if already_spent >= hard_cap * 0.9:
                logger.error(
                    "HALTED: already at %.0f%% of hard cap ($%.4f / $%.2f). "
                    "Refusing to start OCR run.",
                    100 * already_spent / hard_cap,
                    already_spent, hard_cap,
                )
                sys.exit(1)
        except CostGateError as exc:
            logger.error("HALTED by pre-flight cost check: %s", exc)
            sys.exit(1)

    # ── Step 1: fetch failed images from BQ ──────────────────────────────────
    logger.info("Querying BQ for images without [IMAGE DESCRIPTION:]...")
    client = _get_client()
    items = fetch_failed_images(client)
    logger.info("Found %d images in BQ cohort (cap %d)", len(items), _SQL_FAILED_COHORT_CAP)

    if not items:
        logger.info("Nothing to do.")
        return

    # ── Filter: skip already-completed URLs (resume token) ───────────────────
    done_urls = load_done_urls()
    if done_urls:
        before = len(items)
        items = [i for i in items if i["old_content_url"] not in done_urls]
        skipped = before - len(items)
        logger.info("Resume: skipping %d already-completed URLs, %d remain", skipped, len(items))
    else:
        logger.info("Resume token empty — %d images in cohort", len(items))

    if not items:
        logger.info("All images already processed. Nothing to do.")
        return

    # ── Apply --limit after resume filter (next N not yet in .ocr_reprocess_done.txt)
    if args.limit is not None:
        before_lim = len(items)
        items = items[: args.limit]
        logger.info(
            "Applying --limit %d after resume: %d -> %d image(s) to process",
            args.limit, before_lim, len(items),
        )

    if not items:
        logger.info("Nothing to do after --limit.")
        return

    # Group by ticket_id
    by_ticket: dict[int, list[dict]] = defaultdict(list)
    for item in items:
        by_ticket[item["ticket_id"]].append(item)

    logger.info("Affecting %d unique tickets", len(by_ticket))

    # Pre-run estimate (printed whether dry-run or not)
    pre_est_total = len(items) * 0.020
    pre_est_worst = len(items) * 0.035
    logger.info("=" * 60)
    logger.info("PRE-RUN COST ESTIMATE")
    logger.info("  Images to process:  %d", len(items))
    logger.info("  Basis:              $0.020/image (conservative gate upper bound; actual ~$0.001-0.005 after max_output_tokens=2048 fix)")
    logger.info("  Typical estimate:   $%.4f", pre_est_total)
    logger.info("  Worst-case (large): $%.4f", pre_est_worst)
    if not dry_run and run_start_spent > 0:
        budget = get_budget()
        hard_cap = budget["daily_limit_usd"] * budget["hard_cap_pct"] / 100
        remaining = hard_cap - run_start_spent
        logger.info("  Spent today so far: $%.4f", run_start_spent)
        logger.info("  Budget remaining:   $%.4f (hard cap $%.2f)", remaining, hard_cap)
    logger.info("=" * 60)

    if dry_run:
        logger.info("--- DRY RUN: ticket breakdown ---")
        for tid, imgs in sorted(by_ticket.items()):
            logger.info("  Ticket #%s — %d image(s): %s",
                        tid,
                        len(imgs),
                        ", ".join(i["file_name"] or i["old_content_url"][-40:] for i in imgs))
        logger.info("=== DRY RUN complete — no changes made ===")
        return

    # ── Step 2: fetch fresh URLs from Zendesk ────────────────────────────────
    logger.info("Fetching fresh attachment URLs from Zendesk API...")
    session = _zendesk_session()
    fresh_by_ticket: dict[int, list[dict]] = {}

    for i, (ticket_id, _) in enumerate(by_ticket.items()):
        fresh_by_ticket[ticket_id] = fetch_fresh_urls_for_ticket(session, ticket_id)
        if i % 50 == 0 and i > 0:
            logger.info("  Zendesk API: %d/%d tickets fetched", i, len(by_ticket))
        time.sleep(ZENDESK_RATE_S)  # respect rate limit

    # ── Step 3+4: OCR concurrently, write to BQ immediately per image ─────────
    logger.info("Starting OCR with %d workers (retry=%d, backoff=%.1fs)...",
                MAX_WORKERS, MAX_OCR_RETRIES, OCR_RETRY_BASE_S)
    stats = {
        "ok": 0,
        "text_changed": 0,
        "bq_written": 0,
        "no_fresh_url": 0,
        "url_error": 0,
        "download_error": 0,
        "ocr_error": 0,
        "too_large": 0,
        "abandoned": 0,
    }
    done = 0
    start_time = time.time()
    halted = False

    def process_item(item: dict):
        ticket_id = item["ticket_id"]
        old_url = item["old_content_url"]

        # Primary: try the stored URL directly — Zendesk CDN tokens are long-lived,
        # and inline images (pasted screenshots) never appear in comment.attachments,
        # so API lookup can't help them anyway.
        result = process_one(item, old_url)
        if result.get("_status") == "ok":
            return result

        # Only on hard download failure (404/expired), try Zendesk API as fallback
        status = result.get("_status", "")
        if status.startswith("url_error") or status.startswith("download_error"):
            fresh_atts = fresh_by_ticket.get(ticket_id, [])
            fresh_url = match_fresh_url(fresh_atts, item["file_name"], old_url)
            if fresh_url and fresh_url != old_url:
                return process_one(item, fresh_url)

        return result

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_item, item): item for item in items}
        for future in as_completed(futures):
            try:
                result = future.result()
            except (CostGateError, CostDivergenceError) as exc:
                logger.error("HALTED by cost protection: %s", exc)
                executor.shutdown(wait=False, cancel_futures=True)
                halted = True
                break

            done += 1
            status = result.get("_status", "?")

            if status == "ok":
                stats["ok"] += 1
                old_url_ok = result["old_content_url"]
                if old_url_ok in failure_state:
                    del failure_state[old_url_ok]
                    save_failure_state(failure_state_path, failure_state)
                if result.get("_text_changed"):
                    stats["text_changed"] += 1
                    # Write to BQ IMMEDIATELY — don't wait until the end
                    if update_bq_row(client, result):
                        stats["bq_written"] += 1
                        mark_done_url(result["old_content_url"])
                    else:
                        logger.warning("BQ write failed for ticket %s — NOT marking done",
                                       result["ticket_id"])
                else:
                    # Text unchanged — still mark as done so we don't re-try on next run
                    mark_done_url(result["old_content_url"])

            elif "url_error" in status:
                stats["url_error"] += 1
            elif "download_error" in status:
                stats["download_error"] += 1
            elif "ocr_error" in status or "ocr_failed" in status:
                stats["ocr_error"] += 1
            elif status == "no_fresh_url":
                stats["no_fresh_url"] += 1
            elif status == "too_large":
                stats["too_large"] += 1

            if status.startswith("corrupt_"):
                if abandon_row(client, result["old_content_url"], status):
                    stats["abandoned"] += 1
                    old_url_corrupt = result["old_content_url"]
                    if old_url_corrupt in failure_state:
                        del failure_state[old_url_corrupt]
                        save_failure_state(failure_state_path, failure_state)
                continue

            if (
                status != "ok"
                and is_reprocess_failure_status(status)
                and abandon_after > 0
            ):
                old_url_f = result["old_content_url"]
                prev = failure_state.get(old_url_f, {})
                nfail = int(prev.get("n", 0)) + 1
                failure_state[old_url_f] = {
                    "n": nfail,
                    "last_status": status[:200],
                    "last_at": datetime.now(timezone.utc).isoformat(),
                }
                save_failure_state(failure_state_path, failure_state)
                if nfail >= abandon_after:
                    if abandon_row(client, old_url_f, status):
                        stats["abandoned"] += 1
                        del failure_state[old_url_f]
                        save_failure_state(failure_state_path, failure_state)
                        logger.info(
                            "Abandoned URL after %d failures (ticket %s): …%s",
                            nfail,
                            result.get("ticket_id"),
                            old_url_f[-56:],
                        )
                    else:
                        logger.error(
                            "Abandon BQ UPDATE failed after %d failures (ticket %s); will retry abandon next run",
                            nfail,
                            result.get("ticket_id"),
                        )

            if done % 50 == 0 or done == len(items):
                elapsed = time.time() - start_time
                rate = done / elapsed if elapsed > 0 else 0
                logger.info(
                    "Progress %d/%d (%.1f/s) | ok=%d changed=%d bq_written=%d no_url=%d err=%d",
                    done, len(items), rate,
                    stats["ok"], stats["text_changed"], stats["bq_written"],
                    stats["no_fresh_url"],
                    stats["url_error"] + stats["download_error"] + stats["ocr_error"],
                )

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    if halted:
        logger.info("HALTED EARLY by cost protection — %d/%d processed", done, len(items))
    else:
        logger.info("DONE")
    logger.info("  Total images:      %d", len(items))
    logger.info("  OCR success:       %d", stats["ok"])
    logger.info("  Text changed:      %d", stats["text_changed"])
    logger.info("  BQ rows written:   %d", stats["bq_written"])
    logger.info("  No fresh URL:      %d (Zendesk URL not found)", stats["no_fresh_url"])
    logger.info("  URL/download err:  %d", stats["url_error"] + stats["download_error"])
    logger.info("  OCR errors:        %d", stats["ocr_error"])
    logger.info("  Too large:         %d", stats["too_large"])
    logger.info("  Abandoned (BQ skipped): %d", stats["abandoned"])
    logger.info("=" * 60)
    logger.info("PRECONDITION: confirm a paused or disabled state for relevant sync jobs before continuing.")
    logger.info("      Run targeted re-embed separately when ready to update search index.")

    # ── Post-run cost comparison ──────────────────────────────────────────────
    # Flush BQ events before reading Firestore (give writes time to land)
    logger.info("Flushing BQ cost events...")
    flush_bq_events(timeout=15.0)

    try:
        end_spent = get_today_spent(strict=False)
        session_actual = end_spent - run_start_spent
        logger.info("=" * 60)
        logger.info("POST-RUN COST REPORT")
        logger.info("  Images processed:   %d / %d", done, len(items))
        logger.info("  Pre-run estimate:   $%.4f (typical) – $%.4f (worst-case)",
                    pre_est_total, pre_est_worst)
        logger.info("  Actual (API-based): $%.4f", session_actual)
        if session_actual > 0:
            error_pct = (pre_est_total - session_actual) / session_actual * 100
            accuracy = "OVERESTIMATE" if error_pct > 0 else "UNDERESTIMATE"
            logger.info("  Estimate error:     %+.1f%% (%s)", error_pct, accuracy)
            if abs(error_pct) > 50:
                logger.warning("  ⚠ ERROR >50%% — review pricing or image sizes before next run")
            elif abs(error_pct) > 30:
                logger.warning("  ⚠ ERROR >30%% — monitor billing before scaling up")
            else:
                logger.info("  ✓ Error within 30%% — acceptable for next run")
        logger.info("  Cost/image actual:  $%.5f", session_actual / done if done > 0 else 0)
        logger.info("  GCP billing lag:    1-24h. Check Admin Panel → Costs tomorrow.")
        logger.info("  BQ cost_events:     ESTIMATE + ACTUAL rows written for each image.")
        logger.info("  Query to verify:")
        logger.info("    SELECT call_type, event_type,")
        logger.info("           AVG(est_cost_usd) est, AVG(act_cost_usd) act,")
        logger.info("           AVG(estimate_error_pct) err_pct")
        logger.info("    FROM `esip-reference-project.esip_reference.cost_events`")
        logger.info("    WHERE DATE(timestamp) = CURRENT_DATE()")
        logger.info("    GROUP BY 1, 2 ORDER BY 1, 2;")
        logger.info("=" * 60)
    except Exception as exc:
        logger.warning("Post-run cost comparison failed (non-critical): %s", exc)


if __name__ == "__main__":
    main()
