#!/usr/bin/env python3
"""
CLI for controlled, resumable Zendesk ticket catch-up.

Run through scripts/backend_python.sh from the repo root, for example:
  scripts/backend_python.sh scripts/manual_ticket_sync.py inventory --from-production-cursor
  scripts/backend_python.sh scripts/manual_ticket_sync.py run-batch --run-id RUN --limit 100
  scripts/backend_python.sh scripts/manual_ticket_sync.py status --run-id RUN
  scripts/backend_python.sh scripts/manual_ticket_sync.py stop --run-id RUN
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import firestore  # noqa: E402
from app.services.manual_ticket_sync import (  # noqa: E402
    create_inventory,
    ensure_tables,
    finalize_run,
    reconcile_existing,
    request_stop,
    run_batch,
    status,
)


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=_json_default, sort_keys=True))


def _parse_start_time(args: argparse.Namespace) -> int | None:
    if args.start_time:
        return int(args.start_time)
    if args.start_datetime:
        dt = datetime.fromisoformat(args.start_datetime.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if args.from_production_cursor:
        cursor = firestore.get_sync_cursor()
        if cursor is None:
            raise SystemExit("Production Firestore sync cursor is empty.")
        return int(cursor)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled manual Zendesk ticket sync runner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ensure-tables", help="Create manual sync BQ tables if missing")

    inv = sub.add_parser("inventory", help="Create a durable Zendesk incremental stream manifest")
    inv.add_argument("--run-id", help="Optional explicit run id")
    inv.add_argument("--from-production-cursor", action="store_true", help="Use system/bq_sync.sync_cursor")
    inv.add_argument("--start-time", type=int, help="Unix start_time cursor")
    inv.add_argument("--start-datetime", help="ISO datetime, e.g. 2026-03-27T06:26:00Z")
    inv.add_argument("--limit", type=int, help="Optional discovery limit for a dry/small inventory")

    st = sub.add_parser("status", help="Show run status")
    st.add_argument("--run-id", help="Run id; defaults to newest run")

    batch = sub.add_parser("run-batch", help="Process the next manifest tickets")
    batch.add_argument("--run-id", required=True)
    batch.add_argument("--limit", type=int, required=True)

    rec = sub.add_parser("reconcile-existing", help="Mark already-completed manifest rows after verification")
    rec.add_argument("--run-id", required=True)
    rec.add_argument(
        "--through-position",
        type=int,
        required=True,
        help="Highest manifest stream_position to reconcile, e.g. 2220 for old batches #1-#5",
    )
    rec.add_argument("--limit", type=int, help="Maximum non-done manifest rows to reconcile this call")

    stop = sub.add_parser("stop", help="Request graceful stop for a run")
    stop.add_argument("--run-id", required=True)

    fin = sub.add_parser("finalize", help="Finalize a fully completed run")
    fin.add_argument("--run-id", required=True)
    fin.add_argument(
        "--advance-cursor",
        action="store_true",
        help="Explicitly advance system/bq_sync.sync_cursor to the run final_cursor",
    )

    args = parser.parse_args()

    if args.command == "ensure-tables":
        ensure_tables()
        _print_json({"status": "ok"})
    elif args.command == "inventory":
        start_time = _parse_start_time(args)
        result = create_inventory(
            start_time=start_time,
            run_id=args.run_id,
            limit=args.limit,
            progress_cb=lambda msg: print(msg, file=sys.stderr),
        )
        _print_json(result)
    elif args.command == "status":
        _print_json(status(args.run_id))
    elif args.command == "run-batch":
        result = run_batch(
            run_id=args.run_id,
            limit=args.limit,
            progress_cb=lambda msg: print(msg, file=sys.stderr),
        )
        _print_json(result)
    elif args.command == "reconcile-existing":
        result = reconcile_existing(
            run_id=args.run_id,
            through_position=args.through_position,
            limit=args.limit,
            progress_cb=lambda msg: print(msg, file=sys.stderr),
        )
        _print_json(result)
    elif args.command == "stop":
        _print_json(request_stop(args.run_id))
    elif args.command == "finalize":
        _print_json(finalize_run(args.run_id, advance_cursor=args.advance_cursor))


if __name__ == "__main__":
    main()
