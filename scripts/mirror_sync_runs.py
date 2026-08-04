#!/usr/bin/env python3
"""Backfill recent Firestore sync_runs into the BigQuery/GCS audit mirror."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(ROOT / "credentials" / "sa_key.json"))

from app.services import sync_runs  # noqa: E402
from app.services.sync_run_mirror import mirror_sync_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror recent sync_runs to BQ/GCS.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    runs = sync_runs.list_runs(days=max(1, min(args.days, 90)), limit=max(1, min(args.limit, 200)))
    mirrored = 0
    for run in runs:
        detail = sync_runs.get_run(run["id"]) or run
        mirror_sync_run(run["id"], detail)
        mirrored += 1
        print(f"mirrored {run['id']}")
    print(f"Done: mirrored {mirrored} sync run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
