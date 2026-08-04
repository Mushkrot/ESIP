#!/usr/bin/env python3
"""
One-time and incremental vectorization of tickets for semantic search.

Reads tickets not yet present in `ticket_embeddings`, generates Vertex AI
embeddings (text-embedding-004, 768 dims), and inserts them into BQ.

Usage:
  # Initial run — vectorize all un-embedded tickets (41k, ~5-10 min)
  python scripts/vectorize_tickets_bq.py

  # Dry-run — show how many tickets need embedding, don't write anything
  python scripts/vectorize_tickets_bq.py --dry-run

  # Limit to first N tickets (useful for testing)
  python scripts/vectorize_tickets_bq.py --limit 500
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from app.services.bq_sync import vectorize_new_tickets  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Vectorize un-embedded BQ tickets")
    parser.add_argument("--dry-run", action="store_true", help="Count tickets only, no writes")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N tickets")
    args = parser.parse_args()

    if args.dry_run or args.limit:
        # For dry-run/limit we query BQ directly
        from app.services.bigquery import _get_client
        from app.config import settings

        client = _get_client()
        limit_clause = f"LIMIT {args.limit}" if args.limit else ""
        sql = f"""
        SELECT COUNT(*) AS cnt
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets` t
        LEFT JOIN `{settings.google_cloud_project_id}.esip_reference.ticket_embeddings` te
          ON t.id = te.ticket_id
        WHERE te.ticket_id IS NULL
        {limit_clause}
        """
        rows = list(client.query(sql).result())
        count = rows[0].cnt
        print(f"Tickets needing embeddings: {count}")
        if args.dry_run:
            return

    start = time.time()
    progress_messages: list[str] = []

    def progress(msg: str) -> None:
        elapsed = time.time() - start
        line = f"[{elapsed:6.1f}s] {msg}"
        print(line, flush=True)
        progress_messages.append(line)

    result = vectorize_new_tickets(progress_cb=progress)

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Embedded: {result['embedded']}")
    if result["errors"]:
        print(f"  Errors ({len(result['errors'])}):")
        for e in result["errors"]:
            print(f"    {e}")
    else:
        print("  Errors: none")


if __name__ == "__main__":
    main()
