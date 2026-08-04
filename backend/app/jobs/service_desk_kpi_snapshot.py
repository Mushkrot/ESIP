"""CLI entrypoint for the cloud-side Service Desk KPI Excel snapshot job."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.services import service_desk_kpi_snapshot as snapshot
from app.services import service_desk_kpi_delivery as delivery


def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)


def _stdout(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _stderr(line: str) -> None:
    sys.stderr.write(f"{line}\n")
    sys.stderr.flush()


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(delivery.DEFAULT_TIMEZONE))
    return parsed


def _print_email_outcome(outcome: delivery.DeliveryOutcome) -> None:
    if outcome.status == "sent":
        _stdout(f"EMAIL_SENT {outcome.recipient_count} {outcome.gcs_uri}")
    else:
        _stdout(f"EMAIL_SKIP {outcome.message or outcome.status}")
    _write_json(asdict(outcome))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and store a daily Service Desk KPI Excel snapshot.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Build one Excel snapshot and store it in GCS.")
    run.add_argument("--date", help="Eastern report date, YYYY-MM-DD. Defaults to yesterday.")
    run.add_argument("--bucket", help="Destination GCS bucket. Defaults to configured reports bucket.")
    run.add_argument("--prefix", help="Destination GCS prefix. Defaults to configured reports prefix.")
    run.add_argument("--overwrite", action="store_true", help="Overwrite an existing snapshot for the same date.")
    run.add_argument(
        "--allow-stale-test-output",
        action="store_true",
        help="Allow non-final CXone data only when writing to a non-production test prefix.",
    )

    deliver_due = sub.add_parser("deliver-due", help="Send due Service Desk KPI reports for configured delivery slots.")
    deliver_due.add_argument("--date", help="Eastern report date, YYYY-MM-DD. Defaults to yesterday.")
    deliver_due.add_argument("--now", help="Override current dispatcher time as ISO datetime for deterministic tests.")

    send_test = sub.add_parser("send-test", help="Send one KPI report to a safe test recipient only.")
    send_test.add_argument("--date", help="Eastern report date, YYYY-MM-DD. Defaults to yesterday.")
    send_test.add_argument("--recipient", required=True, help="Safe test recipient email.")

    args = parser.parse_args()

    if args.command == "deliver-due":
        now = _parse_now(args.now)
        config = delivery.get_delivery_config()
        if not config.enabled:
            _stdout("EMAIL_SKIP disabled")
            return 0
        slots = delivery.due_slots(config, now)
        if not slots:
            _stdout("EMAIL_SKIP no due slots")
            return 0
        try:
            for slot in slots:
                outcome = delivery.deliver_slot(config, slot, report_date=args.date, now=now)
                _print_email_outcome(outcome)
            return 0
        except Exception as exc:
            _stderr(f"EMAIL_FAIL {type(exc).__name__}: {exc}")
            return 1

    if args.command == "send-test":
        try:
            outcome = delivery.send_test_delivery(recipient=args.recipient, report_date=args.date)
            _print_email_outcome(outcome)
            return 0
        except Exception as exc:
            _stderr(f"EMAIL_FAIL {type(exc).__name__}: {exc}")
            return 1

    if args.command != "run":
        raise SystemExit(f"Unknown command: {args.command}")

    result = snapshot.write_daily_snapshot(
        report_date=args.date,
        bucket_name=args.bucket,
        prefix=args.prefix,
        overwrite=args.overwrite,
        allow_stale_test_output=args.allow_stale_test_output,
    )
    if result.status == "skip":
        _stdout(f"SKIP fresh existing report: {result.gcs_uri}")
    elif result.status == "refreshed":
        _stdout(f"REFRESHED stale report: {result.gcs_uri}")
    else:
        _stdout(f"WROTE {result.gcs_uri}")
    _write_json(asdict(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _stderr(f"FAIL {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
