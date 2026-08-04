#!/usr/bin/env python3
"""QA for deterministic ticket_semantic_search contract execution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.routers import universal  # noqa: E402
from app.services import bigquery  # noqa: E402


PASS = "PASS"
FAIL = "FAIL"


def _check(condition: bool, case_id: str, message: str) -> bool:
    print(f"{PASS if condition else FAIL} {case_id}: {message}")
    return condition


def _answer_has_no_debug_terms(answer: str) -> bool:
    lowered = answer.lower()
    forbidden = [
        "semantic contract",
        "structured zendesk ticket search",
        "обязательные признаки",
        "квалификаторы",
        "сильные кандидаты",
        "связанные, но неполные кандидаты",
        "совпало",
        "не хватает",
        "| тикет |",
        "| ticket |",
        "required concepts",
        "qualifiers",
        "strong candidates",
        "related but incomplete candidates",
        "matched | missing",
    ]
    return not any(term in lowered for term in forbidden)


def _base_contract(filters: dict, *, time_range: dict | None = None) -> dict:
    return {
        "version": "2.0-shadow",
        "mode": "shadow",
        "intent": "ticket_retrieval",
        "data_domain": "zendesk_tickets",
        "scope": "global",
        "executor": "ticket_semantic_search",
        "output": "ticket_examples",
        "time_range": time_range,
        "filters": filters,
        "confidence": "high",
    }


def case_tss_001_cogeco_modem() -> bool:
    contract = _base_contract({
        "search_goal": "find tickets matching a described scenario",
        "required_concepts": ["sent technician", "connect new modem", "to network"],
        "vendors": ["FiberLink"],
        "devices": ["modem"],
        "actions": ["connect"],
    })
    result = bigquery.search_tickets_semantic_contract(contract, limit=10, candidate_limit=700)
    rows = result.get("rows") or []
    answer = universal._ticket_semantic_search_answer(contract, result, "Russian")
    strong_vendor = [
        row for row in rows
        if row.get("semantic_match_type") == "strong_candidate"
        and "FiberLink" in (row.get("matched_vendors") or [])
        and not row.get("missing_required_concepts")
    ]
    top = rows[0] if rows else {}
    ok = (
        result.get("summary", {}).get("candidate_rows", 0) > 0
        and bool(strong_vendor)
        and top.get("semantic_match_type") == "strong_candidate"
        and "event_match_type" not in top
        and "пожар" not in answer.lower()
        and "#950011" in answer
        and _answer_has_no_debug_terms(answer)
    )
    return _check(
        ok,
        "TSS-001",
        (
            "FiberLink/new-modem scenario yields a strong vendor-qualified ticket before generic related rows; "
            f"summary={result.get('summary')}; top=#{top.get('ticket_id')} {top.get('semantic_match_type')} "
            f"matched={top.get('matched_concepts')} vendors={top.get('matched_vendors')} missing={top.get('missing_required_concepts')}"
        ),
    )


def case_tss_002_telecom_partner_fiber_range() -> bool:
    contract = _base_contract(
        {
            "semantic_issue_or_category": "network or connection issues",
            "search_goal": "find tickets mentioning a Telecom Partner fiber-line upgrade",
            "required_concepts": ["network", "connection", "fiber line upgrade"],
            "vendors": ["Telecom Partner"],
            "actions": ["upgrade"],
        },
        time_range={"date_phrase": "from 2023 to today", "source": "semantic_frame"},
    )
    result = bigquery.search_tickets_semantic_contract(contract, limit=10, candidate_limit=500)
    rows = result.get("rows") or []
    summary = result.get("summary") or {}
    answer = universal._ticket_semantic_search_answer(contract, result, "Russian")
    strong = [
        row for row in rows
        if row.get("semantic_match_type") == "strong_candidate"
        and "Telecom Partner" in (row.get("matched_vendors") or [])
        and "upgrade" in (row.get("matched_actions") or [])
        and "fiber line upgrade" in (row.get("matched_concepts") or [])
        and not row.get("missing_required_concepts")
    ]
    ok = (
        summary.get("start_date") == "2023-01-01"
        and bool(summary.get("end_date"))
        and bool(strong)
        and all("event_match_type" not in row for row in rows)
        and "пожар" not in answer.lower()
        and "2023-01-01" in answer
        and _answer_has_no_debug_terms(answer)
    )
    top = rows[0] if rows else {}
    return _check(
        ok,
        "TSS-002",
        (
            "Telecom Partner/fiber range preserves from-2023 date scope and yields strong concept coverage; "
            f"summary={summary}; top=#{top.get('ticket_id')} {top.get('semantic_match_type')} "
            f"matched={top.get('matched_concepts')} vendors={top.get('matched_vendors')} actions={top.get('matched_actions')}"
        ),
    )


def case_tss_003_empty_contract() -> bool:
    result = bigquery.search_tickets_semantic_contract(_base_contract({"required_concepts": []}))
    ok = result.get("rows") == [] and result.get("summary", {}).get("reason") == "empty_recall_terms"
    return _check(ok, "TSS-003", f"empty contract returns no rows without broad scan; summary={result.get('summary')}")


def case_tss_004_partial_answer_is_human_facing() -> bool:
    contract = _base_contract({
        "search_goal": "find tickets mentioning internet connection and Fiber for store 2010",
        "required_concepts": ["internet connection", "Fiber"],
    })
    result = {
        "summary": {"start_date": None, "end_date": None},
        "rows": [
            {
                "ticket_id": 950001,
                "id": 950001,
                "subject": "Internet issues- WAN stuck on connecting",
                "created_at": "2024-04-09",
                "org_name": "Store 2010",
                "store_id": "2010",
                "semantic_match_type": "related_incomplete",
                "matched_concepts": ["internet connection"],
                "missing_required_concepts": ["Fiber"],
                "evidence_snippet": "WAN stuck on connecting. Store reports internet connection issue.",
            }
        ],
    }
    answer = universal._ticket_semantic_search_answer(contract, result, "Russian")
    ok = (
        "Нашел связанные тикеты" in answer
        and "#950001" in answer
        and "связанные совпадения" in answer
        and "Точного подтверждения" not in answer
        and _answer_has_no_debug_terms(answer)
    )
    return _check(ok, "TSS-004", f"partial/no-strong answer is human-facing; answer={answer!r}")


def case_tss_005_multi_store_answer_keeps_requested_store_scope() -> bool:
    contract = _base_contract({
        "search_goal": "find internet/speed/connection interruption related tickets",
        "required_concepts": ["internet", "speed", "connection interruption"],
        "store_ids": ["2012", "2011", "2002", "2003", "2004", "2006"],
    })
    result = {
        "summary": {
            "store_ids": ["2012", "2011", "2002", "2003", "2004", "2006"],
            "start_date": "2025-07-01",
            "end_date": "2026-07-01",
        },
        "rows": [
            {
                "ticket_id": 950053,
                "id": 950053,
                "subject": "Internet connection interrupted",
                "created_at": "2026-06-12",
                "org_name": "Store 2003",
                "store_id": "2002",
                "semantic_match_type": "related_incomplete",
                "matched_concepts": ["internet"],
                "missing_required_concepts": ["speed", "connection interruption"],
                "evidence_snippet": "Store reports intermittent internet connectivity.",
            },
            {
                "ticket_id": 950027,
                "id": 950027,
                "subject": "Network slow",
                "created_at": "2026-05-10",
                "org_name": "Store 2004",
                "store_id": "2003",
                "semantic_match_type": "related_incomplete",
                "matched_concepts": ["speed"],
                "missing_required_concepts": ["internet", "connection interruption"],
                "evidence_snippet": "Network is slow and service drops.",
            },
        ],
    }
    answer = universal._ticket_semantic_search_answer(contract, result, "English")
    ok = (
        "I checked 6 requested stores" in answer
        and "**Store 2012: no indexed matches" in answer
        and "**Store 2003: related tickets to review" in answer
        and "#950053" in answer
        and "**Store 2004: related tickets to review" in answer
        and "#950027" in answer
        and "I did not find a clear confirmation" not in answer
        and _answer_has_no_debug_terms(answer)
    )
    return _check(ok, "TSS-005", f"multi-store answer preserves per-store scope; answer={answer!r}")


def main() -> int:
    cases = [
        case_tss_001_cogeco_modem,
        case_tss_002_telecom_partner_fiber_range,
        case_tss_003_empty_contract,
        case_tss_004_partial_answer_is_human_facing,
        case_tss_005_multi_store_answer_keeps_requested_store_scope,
    ]
    results = [case() for case in cases]
    passed = sum(1 for ok in results if ok)
    print(f"RESULTS: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
