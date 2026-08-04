#!/usr/bin/env python3
"""Adversarial read-only QA for POS Type / Simphony analytics routing.

This is intentionally stricter than the smoke script:
- validates routing expectations;
- runs live BigQuery SELECTs for representative cases;
- asserts nonzero/zero coverage expectations where the current data supports it;
- performs no writes to Zendesk, BigQuery, GCS, schedulers, or tickets.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services import chat_policy, pos_type_analytics


BUSINESS_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class MatrixCase:
    name: str
    question: str
    expect_route: bool
    expect_mode: str | None = None
    expect_pos_type: str | None = None
    expect_pos_types: tuple[str, ...] | None = None
    expect_status_filter: tuple[str, ...] = ()
    expect_brand: str | None = None
    expect_date_window: str | None = None
    min_tickets: int | None = None
    max_tickets: int | None = None


def _recent_window(days: int) -> tuple[str, str]:
    end = datetime.now(BUSINESS_TIMEZONE).date()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _fail(message: str) -> None:
    raise AssertionError(message)


def _coverage_for(request: chat_policy.PosTypeAnalyticsRequest) -> dict[str, Any]:
    if request.mode == "comparison":
        result = pos_type_analytics.get_pos_type_comparison(
            list(request.pos_types),
            start_date=request.start_date,
            end_date=request.end_date,
            brand=request.brand,
            status_filter=request.status_filter,
        )
        counts = {
            key: sum(int(row.get("ticket_count") or 0) for row in rows)
            for key, rows in (result.get("problems_by_pos_type") or {}).items()
        }
        return {"comparison_problem_ticket_counts": counts}

    result = pos_type_analytics.get_pos_type_analytics(
        request.pos_type or "",
        start_date=request.start_date,
        end_date=request.end_date,
        brand=request.brand,
        status_filter=request.status_filter,
    )
    return result.get("coverage") or {}


def _assert_case(case: MatrixCase) -> str:
    request = chat_policy.extract_pos_type_profile_analytics_request(case.question)
    if case.expect_route and request is None:
        _fail(f"{case.name}: expected POS Type route, got ordinary chat/search")
    if not case.expect_route:
        if request is not None:
            _fail(f"{case.name}: expected no POS Type route, got {request}")
        return "PASS no-route"

    assert request is not None
    if case.expect_mode and request.mode != case.expect_mode:
        _fail(f"{case.name}: expected mode={case.expect_mode}, got {request.mode}")
    if case.expect_pos_type and request.pos_type != case.expect_pos_type:
        _fail(f"{case.name}: expected pos_type={case.expect_pos_type}, got {request.pos_type}")
    if case.expect_pos_types and request.pos_types != case.expect_pos_types:
        _fail(f"{case.name}: expected pos_types={case.expect_pos_types}, got {request.pos_types}")
    if request.status_filter != case.expect_status_filter:
        _fail(f"{case.name}: expected statuses={case.expect_status_filter}, got {request.status_filter}")
    if request.brand != case.expect_brand:
        _fail(f"{case.name}: expected brand={case.expect_brand}, got {request.brand}")

    if case.expect_date_window == "recent_30":
        expected_start, expected_end = _recent_window(30)
        if (request.start_date, request.end_date) != (expected_start, expected_end):
            _fail(
                f"{case.name}: expected recent 30 window "
                f"{expected_start}..{expected_end}, got {request.start_date}..{request.end_date}"
            )
    elif case.expect_date_window == "last_7":
        expected_start, expected_end = _recent_window(7)
        if (request.start_date, request.end_date) != (expected_start, expected_end):
            _fail(
                f"{case.name}: expected last 7 window "
                f"{expected_start}..{expected_end}, got {request.start_date}..{request.end_date}"
            )
    elif case.expect_date_window is None:
        if request.start_date or request.end_date:
            _fail(f"{case.name}: expected no date window, got {request.start_date}..{request.end_date}")

    coverage = _coverage_for(request)
    if request.mode == "comparison":
        counts = coverage.get("comparison_problem_ticket_counts") or {}
        if case.min_tickets is not None and not all(value >= case.min_tickets for value in counts.values()):
            _fail(f"{case.name}: expected every comparison side >= {case.min_tickets}, got {counts}")
        return f"PASS comparison counts={counts}"

    tickets = int(coverage.get("tickets_matched") or 0)
    orgs = int(coverage.get("orgs_matched") or 0)
    if case.min_tickets is not None and tickets < case.min_tickets:
        _fail(f"{case.name}: expected tickets >= {case.min_tickets}, got {tickets}; coverage={coverage}")
    if case.max_tickets is not None and tickets > case.max_tickets:
        _fail(f"{case.name}: expected tickets <= {case.max_tickets}, got {tickets}; coverage={coverage}")
    return f"PASS orgs={orgs} tickets={tickets} statuses={list(request.status_filter)}"


CASES = [
    MatrixCase(
        name="golden_all_history",
        question="Summarize all tickets for stores where POS Type = Simphony. Identify repeated stores and repeated problems.",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        min_tickets=10_000,
    ),
    MatrixCase(
        name="explicit_simphony_stores_recent",
        question="For POS Type Simphony stores, which stores have the most repeated recent issues?",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_date_window="recent_30",
        min_tickets=100,
    ),
    MatrixCase(
        name="open_pending_status_filter",
        question="Show open or pending tickets for stores where POS Type = Simphony and summarize likely repeated problems.",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_status_filter=("open", "pending"),
        min_tickets=1,
        max_tickets=100,
    ),
    MatrixCase(
        name="unresolved_status_filter",
        question="Which Simphony stores have unresolved tickets?",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_status_filter=("new", "open", "pending", "hold"),
        min_tickets=1,
        max_tickets=100,
    ),
    MatrixCase(
        name="not_closed_should_mean_unresolved",
        question="Which Simphony stores have tickets that are not closed?",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_status_filter=("new", "open", "pending", "hold"),
        min_tickets=1,
        max_tickets=100,
    ),
    MatrixCase(
        name="simphony_2_maps_to_canonical_simphony",
        question="Summarize tickets for stores where POS Type = Simphony 2.",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        min_tickets=10_000,
    ),
    MatrixCase(
        name="simphony2_shorthand_maps_to_canonical_simphony",
        question="Which Simphony2 stores have the most repeated issues?",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        min_tickets=10_000,
    ),
    MatrixCase(
        name="brand_filter_Metro Burger",
        question="For Metro Burger stores where POS Type = Simphony, summarize repeated problems.",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_brand="Metro Burger",
        min_tickets=1_000,
    ),
    MatrixCase(
        name="last_7_days",
        question="For POS Type Simphony stores, show repeated issues in the last 7 days.",
        expect_route=True,
        expect_mode="single",
        expect_pos_type="Simphony",
        expect_date_window="last_7",
        min_tickets=1,
    ),
    MatrixCase(
        name="comparison_simphony_micros",
        question="Compare repeated issue categories for POS Type Simphony vs POS Type Micros 5.5.",
        expect_route=True,
        expect_mode="comparison",
        expect_pos_types=("Simphony", "Micros 5.5"),
        min_tickets=1,
    ),
    MatrixCase(
        name="comparison_simphony2_micros",
        question="Compare repeated issue categories for POS Type Simphony 2 vs POS Type Micros 5.5.",
        expect_route=True,
        expect_mode="comparison",
        expect_pos_types=("Simphony", "Micros 5.5"),
        min_tickets=1,
    ),
    MatrixCase(
        name="text_search_negative",
        question="Search tickets that mention the word Simphony.",
        expect_route=False,
    ),
    MatrixCase(
        name="simphony_install_text_negative",
        question="Search ticket text for Simphony install issues.",
        expect_route=False,
    ),
]


def main() -> int:
    print("SIMPHONY_POS_TYPE_MATRIX_QA_READ_ONLY")
    print("NO_WRITE BigQuery SELECT only; no Zendesk/BigQuery writes; no scheduler/deploy changes.")
    failures: list[str] = []
    for case in CASES:
        try:
            outcome = _assert_case(case)
            print(f"PASS {case.name}: {outcome}")
        except Exception as exc:
            failures.append(f"{case.name}: {exc}")
            print(f"FAIL {case.name}: {exc}")
    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
