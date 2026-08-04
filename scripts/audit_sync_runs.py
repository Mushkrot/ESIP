#!/usr/bin/env python3
"""
Audit recent sync runs for failures, skipped work, OCR backlog, and problem files.

Run from repo root:
  scripts/backend_python.sh scripts/audit_sync_runs.py --days 7
  scripts/backend_python.sh scripts/audit_sync_runs.py --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(ROOT / "credentials" / "sa_key.json"))

from app.services import sync_runs  # noqa: E402


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _iter_issue_items(source_id: str, source_result: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, severity in (("file_errors", "error"), ("file_warnings", "warning")):
        raw_items = source_result.get(key)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            items.append({
                "severity": severity,
                "source_id": source_id,
                "file_id": item.get("file_id"),
                "file_name": item.get("file_name"),
                "source_path": item.get("source_path"),
                "mime_type": item.get("mime_type"),
                "reason": item.get("reason"),
            })
    skipped_ticket_ids = source_result.get("skipped_ticket_ids")
    if isinstance(skipped_ticket_ids, list):
        for ticket_id in skipped_ticket_ids:
            items.append({
                "severity": "error",
                "source_id": source_id,
                "ticket_id": ticket_id,
                "reason": "comments fetch failed; existing BQ rows preserved",
            })
    return items


def build_audit(days: int, limit: int) -> dict[str, Any]:
    runs = sync_runs.list_runs(days=days, limit=limit)
    detailed_runs = [sync_runs.get_run(run["id"]) or run for run in runs]

    status_counts: Counter[str] = Counter()
    job_counts: Counter[str] = Counter()
    totals = Counter()
    problem_runs: list[dict[str, Any]] = []
    issue_items: list[dict[str, Any]] = []
    source_issue_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    latest_by_job: dict[str, dict[str, Any]] = {}

    for run in detailed_runs:
        status = str(run.get("status") or "unknown")
        job_id = str(run.get("job_id") or "unknown")
        status_counts[status] += 1
        job_counts[job_id] += 1
        latest_by_job.setdefault(job_id, run)

        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        totals["tickets"] += _as_int(summary.get("tickets"))
        totals["comments"] += _as_int(summary.get("comments"))
        totals["attachments"] += _as_int(summary.get("attachments"))
        totals["kb_articles"] += _as_int(summary.get("kb_articles"))
        totals["ocr_processed"] += _as_int(summary.get("ocr_processed"))
        totals["embedded"] += _as_int(summary.get("embedded"))
        totals["skipped_tickets"] += _as_int(summary.get("skipped_tickets"))

        ocr_remaining = _as_int(summary.get("ocr_remaining_after_limit"))
        error_count = _as_int(run.get("error_count"))
        if status != "success" or error_count > 0 or ocr_remaining > 0:
            ocr_deferred = bool(summary.get("ocr_backlog_deferred")) or ocr_remaining > 0
            operator_alert = bool(summary.get("ocr_backlog_operator_alert"))
            if ocr_deferred and not run.get("error_message"):
                reason = summary.get("ocr_backlog_message") or (
                    f"OCR backlog deferred: {ocr_remaining} images remain after the bounded batch"
                )
            else:
                reason = run.get("error_message")
            problem_runs.append({
                "run_id": run.get("id"),
                "job_id": job_id,
                "status": status,
                "started_at": _iso(run.get("started_at")),
                "duration_sec": run.get("duration_sec"),
                "error_count": error_count,
                "error_message": run.get("error_message"),
                "operator_alert": operator_alert,
                "ocr_deferred": ocr_deferred,
                "ocr_remaining": ocr_remaining,
                "reason": reason,
                "summary": summary,
            })

        source_results = run.get("source_results")
        if isinstance(source_results, dict):
            for source_id, value in source_results.items():
                if not isinstance(value, dict):
                    continue
                if _as_int(value.get("errors")) > 0:
                    source_issue_counts[str(source_id)] += _as_int(value.get("errors"))
                if _as_int(value.get("skipped_no_text")) > 0:
                    source_issue_counts[str(source_id)] += _as_int(value.get("skipped_no_text"))
                for item in _iter_issue_items(str(source_id), value):
                    item["run_id"] = run.get("id")
                    item["job_id"] = job_id
                    item["started_at"] = _iso(run.get("started_at"))
                    issue_items.append(item)
                    source_issue_counts[str(source_id)] += 1
                    reason_counts[str(item.get("reason") or "unknown")] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "run_count": len(detailed_runs),
        "status_counts": dict(status_counts),
        "job_counts": dict(job_counts),
        "totals": dict(totals),
        "latest_by_job": {
            job_id: {
                "run_id": run.get("id"),
                "status": run.get("status"),
                "started_at": _iso(run.get("started_at")),
                "duration_sec": run.get("duration_sec"),
                "summary": run.get("summary") or {},
            }
            for job_id, run in latest_by_job.items()
        },
        "problem_runs": problem_runs,
        "issue_items": issue_items,
        "source_issue_counts": dict(source_issue_counts),
        "reason_counts": dict(reason_counts),
    }


def print_markdown(audit: dict[str, Any]) -> None:
    print(f"# Sync Audit Report ({audit['window_days']} days)")
    print()
    print(f"Generated: `{audit['generated_at']}`")
    print(f"Runs analyzed: **{audit['run_count']}**")
    print()
    print("## Status")
    for status, count in sorted(audit["status_counts"].items()):
        print(f"- `{status}`: {count}")
    print()
    print("## Totals")
    for key, value in audit["totals"].items():
        print(f"- `{key}`: {value}")
    print()
    print("## Latest By Job")
    for job_id, run in sorted(audit["latest_by_job"].items()):
        print(
            f"- `{job_id}`: `{run['status']}` · `{run['run_id']}` · "
            f"{run.get('duration_sec') or 0}s"
        )
    print()
    print("## Problem Runs")
    if not audit["problem_runs"]:
        print("- None")
    for run in audit["problem_runs"]:
        marker = "ALERT" if run.get("operator_alert") else "notice"
        print(
            f"- `{marker}` · `{run['run_id']}` · `{run['job_id']}` · `{run['status']}` · "
            f"errors={run['error_count']} · {run.get('reason') or 'no message'}"
        )
    print()
    print("## Issue Items")
    if not audit["issue_items"]:
        print("- None")
    for item in audit["issue_items"][:100]:
        target = item.get("file_name") or item.get("file_id") or item.get("ticket_id") or "unknown"
        print(
            f"- `{item['severity']}` · `{item['source_id']}` · {target} · "
            f"{item.get('reason') or 'unknown'} · run `{item['run_id']}`"
        )
    if len(audit["issue_items"]) > 100:
        print(f"- ... {len(audit['issue_items']) - 100} more issue items omitted")
    print()
    print("## Top Reasons")
    if not audit["reason_counts"]:
        print("- None")
    for reason, count in sorted(audit["reason_counts"].items(), key=lambda kv: (-kv[1], kv[0]))[:20]:
        print(f"- `{reason}`: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit recent ESIP sync runs.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window, max supported by API is 90 days.")
    parser.add_argument("--limit", type=int, default=200, help="Max runs to analyze.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    audit = build_audit(days=max(1, min(args.days, 90)), limit=max(1, min(args.limit, 200)))
    if args.json:
        print(json.dumps(audit, indent=2, ensure_ascii=False, default=str))
    else:
        print_markdown(audit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
