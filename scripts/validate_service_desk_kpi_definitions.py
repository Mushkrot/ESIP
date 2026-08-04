#!/usr/bin/env python3
"""Read-only Service Desk KPI definition validator.

Run from repo root:
  scripts/backend_python.sh scripts/validate_service_desk_kpi_definitions.py --date 2026-05-27

This script reads the existing KPI report only. It does not write to Zendesk,
BigQuery, OCR tables, embeddings, Vertex, Firestore, or schedulers.
"""

from __future__ import annotations

import argparse
import json

from app.services import service_desk_kpi, service_desk_kpi_validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Eastern report date, YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=12, help="Number of per-owner rows to show")
    parser.add_argument("--manual-assigned", type=int, default=None, help="Optional manual Assigned total")
    parser.add_argument("--manual-solved", type=int, default=None, help="Optional manual Solved total")
    parser.add_argument("--manual-outstanding", type=int, default=None, help="Optional manual Outstanding total")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    report = service_desk_kpi.get_daily_report(report_date=args.date)
    manual = {
        key: value
        for key, value in {
            "assigned": args.manual_assigned,
            "solved": args.manual_solved,
            "outstanding": args.manual_outstanding,
        }.items()
        if value is not None
    }
    payload = service_desk_kpi_validation.build_validation_payload(
        report,
        manual_totals=manual,
        top_n=args.top,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(service_desk_kpi_validation.format_validation_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
