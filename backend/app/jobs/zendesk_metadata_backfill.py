"""CLI entrypoint for the temporary Zendesk metadata-only backfill job.

Run locally through:
  scripts/backend_python.sh -m app.jobs.zendesk_metadata_backfill status --run-id RUN

Cloud Run Job uses the same module with the deployed backend image.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.services import zendesk_metadata_backfill as backfill


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zendesk metadata-only historical backfill.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-manifest", help="Create or refresh a durable BigQuery manifest.")
    init.add_argument("--run-id", required=True)
    init.add_argument("--skip-sla-author", action="store_true")
    init.add_argument("--skip-audits", action="store_true")
    init.add_argument("--sla-scope", choices=["closed", "unresolved", "all"], default="closed")
    init.add_argument("--sla-limit", type=int)
    init.add_argument("--audit-limit", type=int)
    init.add_argument("--import-audit-checkpoint", type=Path)
    init.add_argument("--note", default="Temporary metadata-only historical Zendesk backfill.")

    run = sub.add_parser("run-batch", help="Process the next pending manifest chunk.")
    run.add_argument("--run-id", required=True)
    run.add_argument("--ticket-limit", type=int, default=20000)
    run.add_argument("--max-runtime-seconds", type=int, default=10500)
    run.add_argument("--chunk-size", type=int, default=25)
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--sleep", type=float, default=0.1)
    run.add_argument("--audit-max-pages", type=int, default=3)
    run.add_argument("--apply", action="store_true")

    status = sub.add_parser("status", help="Show current durable manifest status.")
    status.add_argument("--run-id", required=True)

    stop = sub.add_parser("request-stop", help="Mark the durable run inactive.")
    stop.add_argument("--run-id", required=True)

    delete = sub.add_parser("delete-run-state", help="Delete temporary manifest/event rows for one run.")
    delete.add_argument("--run-id", required=True)
    delete.add_argument("--confirm-run-id", required=True)

    recover = sub.add_parser("recover-stale", help="Move old running items back to retryable.")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--stale-hours", type=int, default=8)

    args = parser.parse_args()

    if args.command == "init-manifest":
        completed_audit_ids: set[int] = set()
        if args.import_audit_checkpoint:
            completed_audit_ids = backfill.completed_ticket_ids_from_checkpoint(
                args.import_audit_checkpoint,
                backfill.AUDITS_PHASE,
            )
        payload = backfill.create_manifest(
            run_id=args.run_id,
            include_sla_author=not args.skip_sla_author,
            include_audits=not args.skip_audits,
            sla_scope=args.sla_scope,
            sla_limit=args.sla_limit,
            audit_limit=args.audit_limit,
            completed_audit_ticket_ids=completed_audit_ids,
            note=args.note,
        )
        payload["imported_completed_audit_ids"] = len(completed_audit_ids)
        _print(payload)
        return 0

    if args.command == "run-batch":
        if not args.apply:
            raise SystemExit("Refusing to run without --apply. Use tests/status for dry checks.")

        def progress(payload: dict[str, Any]) -> None:
            _print(payload)

        _print(backfill.run_next_batch(
            run_id=args.run_id,
            ticket_limit=args.ticket_limit,
            max_runtime_seconds=args.max_runtime_seconds,
            chunk_size=args.chunk_size,
            workers=args.workers,
            sleep_sec=args.sleep,
            audit_max_pages=args.audit_max_pages,
            apply=True,
            progress_cb=progress,
        ))
        return 0

    if args.command == "status":
        _print(backfill.status(args.run_id))
        return 0

    if args.command == "request-stop":
        _print(backfill.request_stop(args.run_id))
        return 0

    if args.command == "delete-run-state":
        _print(backfill.delete_run_state(args.run_id, confirm_run_id=args.confirm_run_id))
        return 0

    if args.command == "recover-stale":
        _print({
            "run_id": args.run_id,
            "recovered_items": backfill.recover_stale_running(args.run_id, stale_hours=args.stale_hours),
        })
        return 0

    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
