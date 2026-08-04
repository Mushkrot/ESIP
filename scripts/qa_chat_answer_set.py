#!/usr/bin/env python3
"""Run a fixed Universal Chat answer-quality set and save comparable snapshots.

Outputs go under gitignored export/ by default. Keep cases redacted and
synthetic; do not add raw user log questions here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = ROOT / "export" / "chat_answer_sets"
DEFAULT_BASE_URL = os.getenv("ESIP_BASE_URL", "https://api.esip.example.com")


CASES: list[dict[str, Any]] = [
    {
        "id": "CHAT-032A",
        "question": "what are the top issues on May 8",
        "messages": [{"role": "user", "content": "what are the top issues on May 8"}],
        "ticket_id": None,
        "expect": {
            "must_contain_any": ["May 8", "2026-05-08", "105", "ticket"],
            "must_not_contain": [
                "no tickets were created",
                "did not return any results",
                "no results",
                "Accounts Payable FAQ",
                "Barcode Scanners Setup Guide",
            ],
            "max_sources": 0,
        },
    },
    {
        "id": "CHAT-032B",
        "question": "top stores that reported an issue on May 8",
        "messages": [{"role": "user", "content": "top stores that reported an issue on May 8"}],
        "ticket_id": None,
        "expect": {
            "must_contain_any": ["May 8", "2026-05-08", "store", "ticket"],
            "must_not_contain": ["Accounts Payable FAQ", "Barcode Scanners Setup Guide"],
            "max_sources": 0,
        },
    },
    {
        "id": "CHAT-032C-FOLLOWUP-AFTER-DOCS",
        "question": "what are the top issues on May 8",
        "messages": [
            {"role": "user", "content": "Find document that contains: Oracle POS and Cash Drawers"},
            {
                "role": "assistant",
                "content": "The automated Drive search found matching documentation including Accounts Payable FAQ and Accounts Payable Login and Navigation.",
            },
            {"role": "user", "content": "what are the top issues on May 8"},
        ],
        "ticket_id": None,
        "expect": {
            "must_contain_any": ["May 8", "2026-05-08", "105", "ticket"],
            "must_not_contain": [
                "no tickets were created",
                "did not return any results",
                "Accounts Payable FAQ",
                "Accounts Payable Login and Navigation",
                "Barcode Scanners Setup Guide",
            ],
            "max_sources": 0,
        },
    },
    {
        "id": "CHAT-034A-TOP-ISSUES-GROUPED-MAY6",
        "question": "what are the top issues on May 6?",
        "messages": [{"role": "user", "content": "what are the top issues on May 6?"}],
        "ticket_id": None,
        "expect": {
            "must_contain_all": ["May 6", "2026", "ticket"],
            "must_contain_all_after_markdown": ["**Applications --> Store Operations --> End Of Day**"],
            "min_bullet_lines": 4,
            "must_contain_any": ["Uncategorized", "Applications --> Store Operations --> End Of Day"],
            "must_not_contain": [
                "__",
                "Subject-derived",
                "TICKETS_JSON",
                "TOOL CALL",
                "DATA RETRIEVED",
                "tool_code",
                "Accounts Payable FAQ",
                "Barcode Scanners Setup Guide",
            ],
            "max_sources": 0,
        },
    },
    {
        "id": "CHAT-034B-TOP-ISSUES-GROUPED-MAY8",
        "question": "what are the top issues on May 8?",
        "messages": [{"role": "user", "content": "what are the top issues on May 8?"}],
        "ticket_id": None,
        "expect": {
            "must_contain_all": ["May 8", "2026", "ticket"],
            "must_contain_all_after_markdown": ["**Applications --> Order Portal --> Reset Requested**"],
            "min_bullet_lines": 5,
            "must_contain_any": ["Applications --> Order Portal --> Reset Requested", "Payment Device --> Wired Payment Terminal --> Pinpad Not Ready"],
            "must_not_contain": [
                "__",
                "Subject-derived",
                "TICKETS_JSON",
                "TOOL CALL",
                "DATA RETRIEVED",
                "tool_code",
                "Accounts Payable FAQ",
                "Barcode Scanners Setup Guide",
            ],
            "max_sources": 0,
        },
    },
    {
        "id": "CHAT-037A-TOP-ISSUES-MONTH-DAY-YEAR-MAY10",
        "question": "what are the top issues on May 10?",
        "messages": [{"role": "user", "content": "what are the top issues on May 10?"}],
        "ticket_id": None,
        "expect": {
            "must_contain_all": ["May 10", "2026", "ticket"],
            "must_contain_all_after_markdown": ["**Server --> Database**"],
            "min_bullet_lines": 5,
            "must_contain_any": ["Server --> Database", "Aggregators --> Delivery Partner"],
            "must_not_contain": [
                "__",
                "Subject-derived",
                "TICKETS_JSON",
                "TOOL CALL",
                "DATA RETRIEVED",
                "tool_code",
                "May 10, the top reported",
            ],
            "max_sources": 0,
        },
    },
    {
        "id": "GOOD-STORE-ISSUES",
        "question": "Can you give me the details and top issues from store 2011 from the past 3 months?",
        "messages": [{"role": "user", "content": "Can you give me the details and top issues from store 2011 from the past 3 months?"}],
        "ticket_id": None,
        "expect": {
            "must_contain_any": ["6411"],
            "must_not_contain": ["not filtered by store", "cannot provide top issues"],
        },
    },
    {
        "id": "GOOD-DRIVE-EXACT",
        "question": 'Find document that contains: "Oracle POS and Cash Drawers"',
        "messages": [{"role": "user", "content": 'Find document that contains: "Oracle POS and Cash Drawers"'}],
        "ticket_id": None,
        "expect": {
            "must_contain_any": ["Foundry Burger", "Oracle POS and Cash Drawers"],
            "must_not_contain": ["did not find", "cannot find"],
        },
    },
]


def _get_token(base_url: str) -> str:
    resp = requests.post(
        f"{base_url}/api/zaf/auth",
        json={"email": "morgan.chen@northstar.example.com", "name": "QA Bot", "role": "agent"},
        timeout=10,
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
        timeout=60,
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
    checks = _evaluate(answer, sources, case.get("expect") or {})
    return {
        "id": case["id"],
        "question": case["question"],
        "ticket_id": case.get("ticket_id"),
        "elapsed_ms": elapsed_ms,
        "answer": answer,
        "sources": sources,
        "tickets": tickets,
        "trace_id": done_payload.get("trace_id"),
        "checks": checks,
        "passed": all(item["passed"] for item in checks),
    }


def _evaluate(answer: str, sources: list[Any], expect: dict[str, Any]) -> list[dict[str, Any]]:
    answer_lower = answer.lower()
    checks: list[dict[str, Any]] = []
    must_any = [str(item) for item in expect.get("must_contain_any") or []]
    if must_any:
        checks.append({
            "name": "must_contain_any",
            "passed": any(item.lower() in answer_lower for item in must_any),
            "expected": must_any,
        })
    for item in expect.get("must_contain_all") or []:
        item_s = str(item)
        checks.append({
            "name": f"must_contain_all:{item_s}",
            "passed": item_s.lower() in answer_lower,
            "expected": item_s,
        })
    for item in expect.get("must_contain_all_after_markdown") or []:
        item_s = str(item)
        checks.append({
            "name": f"must_contain_all_after_markdown:{item_s}",
            "passed": item_s in answer,
            "expected": item_s,
        })
    for item in expect.get("must_not_contain") or []:
        item_s = str(item)
        checks.append({
            "name": f"must_not_contain:{item_s}",
            "passed": item_s.lower() not in answer_lower,
            "expected": item_s,
        })
    if "max_sources" in expect:
        max_sources = int(expect["max_sources"])
        checks.append({
            "name": "max_sources",
            "passed": len(sources) <= max_sources,
            "expected": max_sources,
            "actual": len(sources),
        })
    if "min_bullet_lines" in expect:
        min_bullets = int(expect["min_bullet_lines"])
        bullet_lines = [
            line for line in answer.splitlines()
            if line.lstrip().startswith(("- ", "* ", "• "))
        ]
        checks.append({
            "name": "min_bullet_lines",
            "passed": len(bullet_lines) >= min_bullets,
            "expected": min_bullets,
            "actual": len(bullet_lines),
        })
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed Universal Chat answer-quality cases.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--tag", default="run")
    parser.add_argument("--case", choices=[case["id"] for case in CASES])
    parser.add_argument("--out-dir", default=str(EXPORT_DIR))
    args = parser.parse_args()

    selected = [case for case in CASES if not args.case or case["id"] == args.case]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    token = _get_token(args.base_url.rstrip("/"))
    results = [_run_case(args.base_url.rstrip("/"), token, case) for case in selected]
    passed = sum(1 for result in results if result["passed"])
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "tag": args.tag,
        "passed": passed,
        "total": len(results),
        "results": results,
    }
    out_path = out_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{args.tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {out_path}")
    for result in results:
        print(f"{'PASS' if result['passed'] else 'FAIL'} {result['id']} {result['elapsed_ms']}ms sources={len(result['sources'])}")
    print(f"RESULTS: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
