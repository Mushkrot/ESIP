#!/usr/bin/env python3
"""Read-only QA for Simphony POS Type Universal Chat deterministic answers."""

from __future__ import annotations

import sys
from textwrap import indent

from app.routers import universal
from app.services import chat_policy, pos_type_analytics


QUESTIONS = [
    "Summarize all tickets for stores where POS Type = Simphony. Identify repeated stores and repeated problems.",
    "For POS Type Simphony stores, which stores have the most repeated recent issues?",
    "For POS Type Simphony have the most repeated recent issues?",
    "For POS Type Simphony, compare to POS Type Micros 5.5 over the last 7 days.",
    "For POS Type Simphony to POS Type Micros 5.5 over the last 7 days.",
    "Show open or pending tickets for stores where POS Type = Simphony and summarize the likely repeated problems.",
    "Compare repeated issue categories for POS Type Simphony vs POS Type Micros 5.5.",
    "Search tickets that mention the word Simphony.",
]


def _line(value: str = "") -> None:
    sys.stdout.write(f"{value}\n")


def _run_question(question: str) -> None:
    request = chat_policy.extract_pos_type_profile_analytics_request(question)
    _line("=" * 96)
    _line(f"QUESTION: {question}")
    if request is None:
        _line("ROUTING: not POS Type profile analytics")
        _line("EXPECTED: this should stay ordinary text/ticket search if sent through full chat.")
        return

    _line(
        "ROUTING: "
        f"mode={request.mode} "
        f"pos_type={request.pos_type or list(request.pos_types)} "
        f"brand={request.brand} "
        f"start={request.start_date} "
        f"end={request.end_date} "
        f"statuses={list(request.status_filter)}"
    )

    if request.mode == "comparison":
        result = pos_type_analytics.get_pos_type_comparison(
            list(request.pos_types),
            start_date=request.start_date,
            end_date=request.end_date,
            brand=request.brand,
            status_filter=request.status_filter,
        )
        answer = universal._pos_type_comparison_answer(result, request)
    else:
        result = pos_type_analytics.get_pos_type_analytics(
            request.pos_type or "",
            start_date=request.start_date,
            end_date=request.end_date,
            brand=request.brand,
            status_filter=request.status_filter,
        )
        coverage = result.get("coverage") or {}
        _line(
            "COVERAGE: "
            f"orgs={coverage.get('orgs_matched')} "
            f"tickets={coverage.get('tickets_matched')} "
            f"org_join={coverage.get('org_id_join_ticket_count')} "
            f"store_fallback={coverage.get('store_id_fallback_ticket_count')} "
            f"profiles_without_tickets={coverage.get('profile_orgs_without_tickets_count')}"
        )
        _line(
            "ROWS: "
            f"repeated_stores={len(result.get('repeated_stores') or [])} "
            f"repeated_problems={len(result.get('repeated_problems') or [])} "
            f"open_tickets={len(result.get('open_tickets') or [])}"
        )
        answer = universal._pos_type_analytics_answer(result, request)

    _line("ANSWER:")
    _line(indent(answer, "  "))


def main() -> int:
    _line("SIMPHONY_POS_TYPE_QA_READ_ONLY")
    _line("NO_WRITE BigQuery SELECT only; no Zendesk apply; no scheduler/deploy changes.")
    for question in QUESTIONS:
        _run_question(question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
