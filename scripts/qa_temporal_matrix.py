#!/usr/bin/env python3
"""Run the temporal Universal Chat QA matrix and save answers for analysis."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = ROOT / "export" / "temporal_chat_matrix"
DEFAULT_BASE_URL = os.getenv("ESIP_BASE_URL", "https://api.esip.example.com")

BUSINESS_TODAY = datetime.now(ZoneInfo("America/New_York")).date()
MAY_2026_END_DAY = BUSINESS_TODAY.day if BUSINESS_TODAY.year == 2026 and BUSINESS_TODAY.month == 5 else 31
MAY_2026_VISIBLE_RANGE = f"May 1-May {MAY_2026_END_DAY}, 2026"


CASES: list[dict[str, Any]] = [
    {"id": "TQA-001", "category": "ticket_details", "question": "Can you give me the details about ticket 950006?"},
    {"id": "TQA-002", "category": "store_analytics", "question": "Can you give me the details and top issues from store 2011 from the past 3 months?"},
    {"id": "TQA-003", "category": "store_content_analytics", "question": "Can you give me the details and top issues from store 2011 from the past 3 months by looking at the contents of the tickets or ticket description and not the issue_category?"},
    {"id": "TQA-004", "category": "procedure", "question": "who do I escalate DispatchFlow issues to?", "allow_sources": True},
    {"id": "TQA-010", "category": "top_issues", "question": "What are the top issues reported today?", "expect_live_for_fresh": True},
    {"id": "TQA-011", "category": "top_issues", "question": "What are the top issues reported yesterday?", "expect_date_visible": True},
    {"id": "TQA-012", "category": "top_issues", "question": "What are the top issues reported 2 days ago?", "expect_date_visible": True},
    {"id": "TQA-013", "category": "top_issues", "question": "What are the top issues reported within last 5 days?", "expect_range_visible": True},
    {"id": "TQA-014", "category": "top_issues", "question": "What are the top issues reported within 1 week?", "expect_range_visible": True},
    {"id": "TQA-015", "category": "top_issues", "question": "What are the top issues on May 8?", "expect_year": "2026"},
    {"id": "TQA-016", "category": "top_issues", "question": "What are the top issues on April 10?", "expect_year": "2026"},
    {"id": "TQA-017", "category": "top_issues", "question": "What are the top issues on May 10 2025?", "expect_year": "2025"},
    {"id": "TQA-018", "category": "top_issues", "question": "What are the top issues in April?", "expect_year": "2026"},
    {"id": "TQA-019", "category": "top_issues", "question": "What are the top issues last month?", "expect_range_visible": True},
    {"id": "TQA-020", "category": "top_issues", "question": "What are the top issues this week?", "expect_range_visible": True},
    {"id": "TQA-021", "category": "top_issues", "question": "What are the top issues last week?", "expect_range_visible": True},
    {"id": "TQA-022", "category": "top_issues", "question": "What are the top issues last Friday?", "expect_date_visible": True},
    {"id": "TQA-030", "category": "ticket_count", "question": "How many tickets are created today?", "expect_live_for_fresh": True, "expect_no_ticket_cards": True},
    {"id": "TQA-031", "category": "ticket_count", "question": "How many tickets are created yesterday?", "expect_date_visible": True, "expect_no_ticket_cards": True},
    {"id": "TQA-032", "category": "ticket_count", "question": "How many tickets are created 2 days ago?", "expect_date_visible": True, "expect_no_ticket_cards": True},
    {"id": "TQA-033", "category": "ticket_count", "question": "How many tickets are created within last 5 days?", "expect_range_visible": True, "expect_no_ticket_cards": True},
    {"id": "TQA-034", "category": "ticket_count", "question": "How many tickets are created within 1 week?", "expect_range_visible": True, "expect_no_ticket_cards": True},
    {"id": "TQA-035", "category": "ticket_count", "question": "How many tickets are created on May 8?", "expect_year": "2026", "expect_no_ticket_cards": True},
    {"id": "TQA-036", "category": "ticket_count", "question": "How many tickets were created on May 8 2021?", "expect_year": "2021", "expect_zendesk_if_no_bq": True, "expect_no_ticket_cards": True},
    {"id": "TQA-037", "category": "ticket_count", "question": "How n)may tickets were opened withon last 5 days?", "expect_range_visible": True, "expect_count_answer": True, "expect_no_ticket_cards": True},
    {"id": "TQA-038", "category": "ticket_count", "question": "How many tickets were updated within last 5 days?", "expect_range_visible": True, "expect_count_answer": True, "expect_word": "updated", "expect_no_ticket_cards": True},
    {"id": "TQA-039", "category": "ticket_count", "question": "How many tickets were created in May 2026?", "expect_range_visible": True, "expect_count_answer": True, "expect_any": [MAY_2026_VISIBLE_RANGE], "expect_no_ticket_cards": True},
    {"id": "TQA-040", "category": "top_stores", "question": "Stores reported most tickets today?", "expect_live_for_fresh": True},
    {"id": "TQA-041", "category": "top_stores", "question": "Stores reported most tickets yesterday?", "expect_date_visible": True},
    {"id": "TQA-042", "category": "top_stores", "question": "Stores reported most tickets 2 days ago?", "expect_date_visible": True},
    {"id": "TQA-043", "category": "top_stores", "question": "Stores reported most tickets within last 5 days?", "expect_range_visible": True},
    {"id": "TQA-044", "category": "top_stores", "question": "Stores reported most tickets on May 8?", "expect_year": "2026"},
    {
        "id": "TQA-050",
        "category": "followup_date",
        "question": "How about yesterday?",
        "messages": [
            {"role": "user", "content": "What are the top issues on May 10?"},
            {"role": "assistant", "content": "On May 10, 2026, the top reported issues were Server --> Database and Aggregators --> Delivery Partner."},
            {"role": "user", "content": "How about yesterday?"},
        ],
        "expect_date_visible": True,
    },
    {
        "id": "TQA-051",
        "category": "followup_year",
        "question": "May 10 - what year?",
        "messages": [
            {"role": "user", "content": "What are the top issues on May 10?"},
            {"role": "assistant", "content": "On May 10, 2026, the top reported issues were Server --> Database and Aggregators --> Delivery Partner."},
            {"role": "user", "content": "May 10 - what year?"},
        ],
        "expect_year": "2026",
    },
    {"id": "TQA-052", "category": "ticket_context", "question": "How many times has this category of issue been reported?", "ticket_id": 950034},
    {"id": "TQA-053", "category": "ticket_context", "question": "How can this be avoided?", "ticket_id": 950036, "allow_sources": True},
    {
        "id": "TQA-060",
        "category": "context_chain",
        "question": "Which stores reported the most tickets?",
        "messages": [
            {"role": "user", "content": "What are the top issues on May 8?"},
            {"role": "assistant", "content": "On May 8, 2026, the top reported issues included Applications --> Order Portal --> Reset Requested."},
            {"role": "user", "content": "How many tickets were created that day?"},
            {"role": "assistant", "content": "There were 105 tickets created on May 8, 2026."},
            {"role": "user", "content": "Which stores reported the most tickets?"},
        ],
        "expect_year": "2026",
        "expect_any": ["May 8", "1763", "store"],
    },
    {
        "id": "TQA-061",
        "category": "context_chain",
        "question": "How many tickets were created that day?",
        "messages": [
            {"role": "user", "content": "What are the top issues on May 10?"},
            {"role": "assistant", "content": "On May 10, 2026, the top reported issues included Server --> Database."},
            {"role": "user", "content": "How about yesterday?"},
            {"role": "assistant", "content": "Yesterday (May 12, 2026, Eastern time), the top reported issues included Applications --> Store Operations --> Items Missing."},
            {"role": "user", "content": "How many tickets were created that day?"},
        ],
        "expect_year": "2026",
        "expect_any": ["May 12", "yesterday", "80"],
    },
    {
        "id": "TQA-062",
        "category": "context_chain",
        "question": "Show examples from the top store.",
        "messages": [
            {"role": "user", "content": "What are the top issues within last 5 days?"},
            {"role": "assistant", "content": "Within the last 5 days, top issues included Order Portal reset requests and Store Operations end of day issues."},
            {"role": "user", "content": "Which stores are driving those?"},
            {"role": "assistant", "content": "Within the last 5 days, Store 2004 reported the most tickets."},
            {"role": "user", "content": "Show examples from the top store."},
        ],
        "expect_range_visible": True,
        "expect_any": ["1763", "ticket"],
    },
    {
        "id": "TQA-063",
        "category": "context_chain",
        "question": "How many were Store Operations Depletion?",
        "messages": [
            {"role": "user", "content": "Can you give me the details and top issues from store 2011 from the past 3 months?"},
            {"role": "assistant", "content": "For store 2011 over the past three months, top issues included Store Operations Depletion with 15 tickets."},
            {"role": "user", "content": "Now do that by ticket contents, not issue_category"},
            {"role": "assistant", "content": "For store 2011 over the past 3 months, based on ticket content, Store Operations Depletion appeared with 15 tickets."},
            {"role": "user", "content": "How many were Store Operations Depletion?"},
        ],
        "expect_any": ["6411", "15", "Store Operations Depletion"],
    },
    {
        "id": "TQA-064",
        "category": "context_chain",
        "question": "Which stores had it most?",
        "ticket_id": 950034,
        "messages": [
            {"role": "user", "content": "How many times has this category of issue been reported?"},
            {"role": "assistant", "content": "The issue category Server --> Hard Drive has been reported 476 times in total."},
            {"role": "user", "content": "What about in the last 90 days?"},
            {"role": "assistant", "content": "In the last 90 days, Server --> Hard Drive has been reported 216 times."},
            {"role": "user", "content": "Which stores had it most?"},
        ],
        "expect_any": ["Server", "Hard Drive", "90", "store"],
    },
    {
        "id": "TQA-065",
        "category": "context_chain",
        "question": "Which stores reported most?",
        "messages": [
            {"role": "user", "content": "How many tickets were created on May 8 2021?"},
            {"role": "assistant", "content": "On May 8, 2021, Zendesk was checked for created tickets."},
            {"role": "user", "content": "What were the top issues that day?"},
            {"role": "assistant", "content": "On May 8, 2021, the top issues were checked in Zendesk."},
            {"role": "user", "content": "Which stores reported most?"},
        ],
        "expect_year": "2021",
        "expect_any": ["May 8", "Zendesk", "store"],
    },
    {
        "id": "TQA-066",
        "category": "context_chain",
        "question": "How about this month?",
        "messages": [
            {"role": "user", "content": "who do I escalate DispatchFlow issues to?"},
            {"role": "assistant", "content": "Escalate DispatchFlow issues to the BA team."},
            {"role": "user", "content": "How many DispatchFlow tickets did we get last month?"},
            {"role": "assistant", "content": "Last month, DispatchFlow ticket volume was checked from ticket analytics."},
            {"role": "user", "content": "How about this month?"},
        ],
        "expect_any": ["DispatchFlow", "DispatchFlow", "this month", "May", "2026"],
    },
    {
        "id": "TQA-067",
        "category": "context_chain",
        "question": "How many tickets were created that day?",
        "messages": [
            {"role": "user", "content": "What are the top issues on April 10?"},
            {"role": "assistant", "content": "On April 10, 2026, the top reported issues included POS Terminal --> Dining Room --> System Closed Error."},
            {"role": "user", "content": "What are the top issues on May 8?"},
            {"role": "assistant", "content": "On May 8, 2026, the top reported issues included Applications --> Order Portal --> Reset Requested."},
            {"role": "user", "content": "How many tickets were created that day?"},
        ],
        "expect_year": "2026",
        "expect_any": ["May 8", "105"],
    },
]


def _get_token(base_url: str) -> str:
    resp = requests.post(
        f"{base_url}/api/zaf/auth",
        json={"email": "morgan.chen@northstar.example.com", "name": "Temporal QA Bot", "role": "agent"},
        timeout=15,
    )
    resp.raise_for_status()
    return str(resp.json()["session_token"])


def _run_case(base_url: str, token: str, case: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    payload = {
        "messages": case.get("messages") or [{"role": "user", "content": case["question"]}],
        "ticket_id": case.get("ticket_id"),
    }
    started = time.monotonic()
    resp = requests.post(
        f"{base_url}/api/universal/stream",
        headers=headers,
        json=payload,
        stream=True,
        timeout=int(case.get("timeout", 120)),
    )
    resp.raise_for_status()
    answer = ""
    done_payload: dict[str, Any] = {}
    for line in resp.iter_lines():
        if not line:
            continue
        text = line.decode("utf-8") if isinstance(line, bytes) else str(line)
        if not text.startswith("data: "):
            continue
        try:
            data = json.loads(text[6:])
        except Exception:
            continue
        if "delta" in data:
            answer += str(data["delta"])
        if data.get("done"):
            done_payload = data
            break
    elapsed_ms = int((time.monotonic() - started) * 1000)
    sources = done_payload.get("sources") or []
    tickets = done_payload.get("tickets") or []
    flags = _analyze_answer(case, answer, sources, tickets)
    return {
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "ticket_id": case.get("ticket_id"),
        "elapsed_ms": elapsed_ms,
        "answer": answer,
        "sources": sources,
        "tickets": tickets,
        "trace_id": done_payload.get("trace_id"),
        "flags": flags,
        "likely_problem": bool(flags),
    }


def _analyze_answer(case: dict[str, Any], answer: str, sources: list[Any], tickets: list[Any]) -> list[str]:
    text = answer.lower()
    flags: list[str] = []
    analytics = case["category"] in {
        "top_issues",
        "ticket_count",
        "top_stores",
        "store_analytics",
        "store_content_analytics",
        "followup_date",
        "ticket_context",
    }

    weak_phrases = [
        "i do not have specific data",
        "i don't have specific data",
        "without a specific date context",
        "automated search did not return",
        "did not return any results",
        "no specific data",
        "no data available",
        "no ticket data",
    ]
    if any(phrase in text for phrase in weak_phrases):
        flags.append("weak_or_no_data_language")

    # SOURCES_JSON/TICKETS_JSON are backend control footers parsed out by the
    # frontend; keep them in saved raw snapshots but do not flag them as
    # user-visible leaks here.
    forbidden = ["tool call:", "tool output:", "data retrieved:", "tool_code"]
    leaked = [item for item in forbidden if item in text]
    if leaked:
        flags.append(f"visible_internal_artifact:{','.join(leaked)}")

    if analytics and sources and not case.get("allow_sources"):
        flags.append(f"unexpected_sources:{len(sources)}")

    if analytics and any(term in text for term in ["accounts payable faq", "barcode scanners setup guide", "oracle pos and cash drawers"]):
        flags.append("unrelated_drive_or_kb_context")

    expected_year = case.get("expect_year")
    if expected_year and expected_year not in answer:
        flags.append(f"missing_expected_year:{expected_year}")

    expect_any = [str(item) for item in case.get("expect_any") or []]
    if expect_any and not any(item.lower() in text for item in expect_any):
        flags.append(f"missing_expected_any:{'|'.join(expect_any)}")

    if case.get("expect_live_for_fresh"):
        if "live zendesk" not in text:
            flags.append("fresh_window_missing_live_zendesk")
        if "Eastern" not in text:
            flags.append("fresh_window_missing_business_time")
        if not tickets and not case.get("expect_no_ticket_cards"):
            flags.append("fresh_window_missing_ticket_cards")

    if case.get("expect_date_visible") and not re.search(r"\b(202\d-\d\d-\d\d|[a-z]+ \d{1,2}, 202\d|yesterday|today|Eastern)\b", text):
        flags.append("date_window_not_visible")

    if case.get("expect_range_visible") and not re.search(r"\b(202\d-\d\d-\d\d|[a-z]+ \d{1,2}-[a-z]+ \d{1,2}, 202\d|from|between|through|last \d|past|week|month|Eastern)\b", text):
        flags.append("range_not_visible")

    if case.get("expect_count_answer"):
        if re.search(r"\btop issues?\b", text) or re.search(r"\buncategorized\b.*\bhad\b", text):
            flags.append("count_question_answered_as_issue_breakdown")
        if not re.search(r"\b\d+\b|\*\*\d+\*\*", text):
            flags.append("count_answer_missing_number")

    expected_word = case.get("expect_word")
    if expected_word and expected_word.lower() not in text:
        flags.append(f"missing_expected_word:{expected_word}")

    if case.get("expect_no_ticket_cards") and tickets:
        flags.append(f"misleading_ticket_cards_for_count:{len(tickets)}")

    if case.get("expect_zendesk_if_no_bq") and "zendesk" not in text:
        zero_claim = bool(
            re.search(r"\b0\s+tickets?\b", text)
            or re.search(r"\*\*0\*\*\s+tickets?", text)
            or "no tickets" in text
            or "no stores" in text
        )
        if zero_claim:
            flags.append("old_or_missing_bq_data_without_zendesk_fallback")

    if case["id"] == "TQA-052" and "which category" in text:
        flags.append("contextual_category_clarification_loop")

    if case["id"] == "TQA-053" and ("what issue" in text or "what this refers" in text):
        flags.append("contextual_prevention_clarification_loop")

    return flags


def _write_markdown(payload: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# Temporal Chat QA Run",
        "",
        f"- Created: `{payload['created_at']}`",
        f"- Base URL: `{payload['base_url']}`",
        f"- Eastern runtime date: `{payload['business_time_runtime_date']}`",
        f"- Likely problems: `{payload['problem_count']}/{payload['total']}`",
        "",
        "## Results",
        "",
        "| ID | Category | Flags | Trace | Answer excerpt |",
        "|---|---|---|---|---|",
    ]
    for result in payload["results"]:
        flags = ", ".join(result["flags"]) if result["flags"] else "OK"
        excerpt = " ".join(result["answer"].split())[:260].replace("|", "\\|")
        trace = result.get("trace_id") or ""
        lines.append(f"| {result['id']} | {result['category']} | {flags} | `{trace}` | {excerpt} |")

    lines.extend(["", "## Full Answers", ""])
    for result in payload["results"]:
        lines.extend([
            f"### {result['id']} - {result['question']}",
            "",
            f"- Category: `{result['category']}`",
            f"- Trace: `{result.get('trace_id') or ''}`",
            f"- Elapsed: `{result['elapsed_ms']}ms`",
            f"- Sources: `{len(result['sources'])}`",
            f"- Ticket cards: `{len(result['tickets'])}`",
            f"- Flags: `{', '.join(result['flags']) if result['flags'] else 'OK'}`",
            "",
            result["answer"].strip() or "(empty answer)",
            "",
        ])
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run temporal Universal Chat QA matrix.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out-dir", default=str(EXPORT_DIR))
    parser.add_argument("--tag", default="temporal_matrix")
    parser.add_argument("--case", choices=[case["id"] for case in CASES])
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    selected = [case for case in CASES if not args.case or case["id"] == args.case]
    if args.limit:
        selected = selected[: args.limit]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip("/")
    token = _get_token(base_url)

    results = []
    for case in selected:
        print(f"RUN {case['id']} {case['question']}", flush=True)
        try:
            results.append(_run_case(base_url, token, case))
        except Exception as exc:
            results.append({
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "ticket_id": case.get("ticket_id"),
                "elapsed_ms": 0,
                "answer": "",
                "sources": [],
                "tickets": [],
                "trace_id": None,
                "flags": [f"runner_error:{type(exc).__name__}:{exc}"],
                "likely_problem": True,
            })

    now = datetime.now(timezone.utc)
    payload = {
        "created_at": now.isoformat(),
        "base_url": base_url,
        "business_time_runtime_date": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "total": len(results),
        "problem_count": sum(1 for result in results if result["likely_problem"]),
        "results": results,
    }
    safe_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.tag).strip("_") or "temporal_matrix"
    stem = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{safe_tag}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(payload, md_path)

    print(f"Saved {json_path}")
    print(f"Saved {md_path}")
    for result in results:
        status = "FLAG" if result["likely_problem"] else "OK"
        print(f"{status} {result['id']} {result['elapsed_ms']}ms flags={','.join(result['flags']) or '-'}")
    print(f"RESULTS: {payload['problem_count']}/{payload['total']} likely problems")
    return 1 if payload["problem_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
