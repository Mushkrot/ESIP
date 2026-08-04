#!/usr/bin/env python3
"""Read-only Zendesk Ticket Audits probe for Service Desk KPI validation.

Examples:
  scripts/backend_python.sh scripts/probe_zendesk_ticket_audits.py --ticket-id 950043 --resolve-users
  scripts/backend_python.sh scripts/probe_zendesk_ticket_audits.py --date 2026-05-27 --limit 10

This probe reads a small sample only. It does not write to Zendesk, BigQuery,
OCR tables, embeddings, Vertex, Firestore, or schedulers.
"""

from __future__ import annotations

import argparse
import json

from app.services import zendesk_ticket_audit_sample


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket-id", type=int, action="append", default=[], help="Ticket id to inspect")
    parser.add_argument("--date", help="Eastern report date used to select a small BQ ticket sample")
    parser.add_argument("--limit", type=int, default=10, help="Sample size when --date is used")
    parser.add_argument("--max-pages", type=int, default=3, help="Max Zendesk audit pages per ticket")
    parser.add_argument("--resolve-users", action="store_true", help="Resolve assignee ids to Zendesk users")
    parser.add_argument("--sleep", type=float, default=0.15, help="Pause between ticket audit reads")
    parser.add_argument("--json", action="store_true", help="Print full JSON payload")
    args = parser.parse_args()

    ticket_ids = list(dict.fromkeys(args.ticket_id))
    if not ticket_ids:
        if not args.date:
            parser.error("Provide --ticket-id or --date")
        ticket_ids = zendesk_ticket_audit_sample.sample_ticket_ids_for_day(
            report_date=args.date,
            limit=args.limit,
        )
    payload = zendesk_ticket_audit_sample.probe_ticket_audits(
        ticket_ids,
        max_pages=args.max_pages,
        resolve_users=args.resolve_users,
        sleep_seconds=args.sleep,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Zendesk Ticket Audits probe: {payload['ticket_count']} tickets, {payload['event_count']} events")
        print("READ ONLY: no writes to Zendesk, BigQuery, OCR, embeddings, Vertex, Firestore, or schedulers.")
        print(f"Field counts: {payload['field_counts']}")
        for ticket in payload["tickets"]:
            print(
                f"- Ticket {ticket['ticket_id']}: {ticket['audit_count']} audits, "
                f"{ticket['event_count']} relevant changes, {ticket['field_counts']}"
            )
            for event in ticket["events"][:8]:
                value = event.get("value_user") or event.get("value")
                previous = event.get("previous_user") or event.get("previous_value")
                print(
                    f"    {event['created_at']} {event['field_name']}: "
                    f"{previous!r} -> {value!r} via {event.get('via_channel')}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
