"""
Full BigQuery rebuild pipeline with clean re-export and OCR.

Runs sequentially:
  1. Truncate BQ tables (tickets, ticket_comments, ticket_attachments,
                          ticket_embeddings, kb_articles)
     NOTE: ticket_attachment_ocr is NOT cleared — resume-safe.
  2. Delete export checkpoint (force fresh export)
  3. Full ticket export from Zendesk for the configured retention window
  4. KB articles sync
  5. OCR all image attachments via Gemini Vision
  6. Vectorize all tickets (with OCR-enriched text)
  7. Reset Firestore sync cursor to now (so daily sync picks up from here)

Runtime depends on the configured dataset size, provider rate limits, attachment
volume, and embedding batch size. Use the dry-run inventory before scheduling a
rebuild and keep the resumable checkpoints enabled.

Resume after interruption:
  - Export:      use --resume flag on export_tickets_bq.py (checkpoint file)
  - OCR:         re-run rebuild_pipeline.py --steps=ocr+ (already-processed images skipped)
  - Vectorize:   already-embedded tickets are skipped via LEFT JOIN

Usage:
  # Full run (start from scratch)
  python scripts/rebuild_pipeline.py

  # Start from a specific step (skip earlier steps)
  python scripts/rebuild_pipeline.py --start-at=ocr
  python scripts/rebuild_pipeline.py --start-at=vectorize
  python scripts/rebuild_pipeline.py --start-at=cursor
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from google.cloud import bigquery as bq_module
from google.oauth2 import service_account

from app.config import settings
from app.services import firestore as fs_service
from app.services.bq_sync import sync_kb_articles, vectorize_new_tickets, ocr_new_attachments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

GCP_PROJECT      = settings.google_cloud_project_id
BQ_DATASET       = "esip_reference"
SCRIPTS_DIR      = Path(__file__).parent
CHECKPOINT_FILE  = SCRIPTS_DIR / ".export_checkpoint.json"
EXPORT_SINCE     = "2024-01-01"   # full historical export start date

STEPS = ["truncate", "export", "kb", "ocr", "vectorize", "cursor"]


def _get_bq_client() -> bq_module.Client:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return bq_module.Client(project=GCP_PROJECT, credentials=creds)


def step_truncate(client: bq_module.Client) -> None:
    log.info("=" * 60)
    log.info("STEP 1/6: Truncating BQ tables...")
    tables = ["tickets", "ticket_comments", "ticket_attachments",
              "ticket_embeddings", "kb_articles"]
    for t in tables:
        fqn = f"`{GCP_PROJECT}.{BQ_DATASET}.{t}`"
        log.info("  Truncating %s...", t)
        client.query(f"TRUNCATE TABLE {fqn}").result()
    log.info("Tables truncated — clean slate.")


def step_delete_checkpoint() -> None:
    log.info("Deleting export checkpoint (if any)...")
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        log.info("Checkpoint deleted: %s", CHECKPOINT_FILE)
    else:
        log.info("No checkpoint file found.")


def _run_subprocess(cmd: list[str], step_name: str) -> None:
    """Run a Python script as unbuffered subprocess, streaming output to our logger."""
    import os
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    log.info("Running: %s", " ".join(cmd))
    start = time.time()
    result = subprocess.run(cmd, cwd=str(SCRIPTS_DIR.parent), env=env)
    elapsed = time.time() - start
    if result.returncode != 0:
        raise RuntimeError(f"{step_name} failed with exit code {result.returncode}")
    log.info("%s complete in %.0f minutes", step_name, elapsed / 60)


def step_export() -> None:
    log.info("=" * 60)
    log.info("STEP 2/6: Full Zendesk export (since %s)...", EXPORT_SINCE)
    log.info("Expected: 3-4 hours (41k+ tickets)")
    cmd = [
        sys.executable, "-u",
        str(SCRIPTS_DIR / "export_tickets_bq.py"),
        "--mode=full",
        f"--since-date={EXPORT_SINCE}",
    ]
    _run_subprocess(cmd, "Export")


def step_kb() -> None:
    log.info("=" * 60)
    log.info("STEP 3/6: Syncing KB articles...")
    result = sync_kb_articles()
    log.info("KB articles synced: %d articles, errors: %s",
             result["articles"], result["errors"])


def step_ocr() -> None:
    log.info("=" * 60)
    log.info("STEP 4/6: OCR all image attachments...")
    log.info("Runtime depends on attachment volume and OCR provider limits")

    # Run OCR as unbuffered subprocess for real-time log output (20 concurrent workers)
    cmd = [sys.executable, "-u", str(SCRIPTS_DIR / "ocr_attachments.py"), "--workers", "20"]
    _run_subprocess(cmd, "OCR")


def step_vectorize() -> None:
    log.info("=" * 60)
    log.info("STEP 5/6: Vectorizing tickets...")
    log.info("Runtime depends on the incremental ticket backlog")
    start = time.time()

    def progress(msg: str) -> None:
        log.info("[vectorize] %s", msg)

    result = vectorize_new_tickets(progress_cb=progress)
    elapsed = time.time() - start
    log.info("Vectorization complete in %.0f minutes: embedded=%d errors=%d",
             elapsed / 60, result["embedded"], len(result["errors"]))
    if result["errors"]:
        log.warning("Vectorization errors: %s", result["errors"][:3])


def step_cursor() -> None:
    log.info("=" * 60)
    log.info("STEP 6/6: Resetting Firestore sync cursor to now...")
    now_unix = int(datetime.now(timezone.utc).timestamp())
    try:
        fs_service.set_sync_cursor(now_unix)
        log.info("Sync cursor reset to: %d (%s)", now_unix,
                 datetime.fromtimestamp(now_unix, tz=timezone.utc).isoformat())
    except Exception as exc:
        log.warning("Could not reset sync cursor: %s (non-fatal)", exc)


def run(start_at: str = "truncate") -> None:
    start_index = STEPS.index(start_at)
    active_steps = STEPS[start_index:]

    total_start = time.time()
    log.info("=" * 60)
    log.info("REBUILD PIPELINE STARTING")
    log.info("Steps to run: %s", active_steps)
    log.info("=" * 60)

    client = _get_bq_client() if "truncate" in active_steps else None

    for step in active_steps:
        step_start = time.time()
        try:
            if step == "truncate":
                step_truncate(client)
                step_delete_checkpoint()
            elif step == "export":
                step_export()
            elif step == "kb":
                step_kb()
            elif step == "ocr":
                step_ocr()
            elif step == "vectorize":
                step_vectorize()
            elif step == "cursor":
                step_cursor()

            elapsed = time.time() - step_start
            log.info("Step '%s' completed in %.1f minutes", step, elapsed / 60)

        except Exception as exc:
            log.error("Step '%s' FAILED: %s", step, exc)
            log.error("To resume, run:  python rebuild_pipeline.py --start-at=%s", step)
            raise

    total_elapsed = time.time() - total_start
    log.info("=" * 60)
    log.info("REBUILD COMPLETE in %.1f minutes (%.1f hours)", total_elapsed / 60, total_elapsed / 3600)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Full BQ rebuild pipeline with OCR")
    parser.add_argument(
        "--start-at",
        choices=STEPS,
        default="truncate",
        help="Start from this step (useful for resuming after failure)",
    )
    args = parser.parse_args()
    run(start_at=args.start_at)
