#!/usr/bin/env python3
"""Manual Universal Chat durable-log audit.

Read-only by default. Use --record-run to store a lightweight checkpoint in
Firestore chat_audit_runs after reviewing the generated report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from google.cloud import bigquery  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.firestore import get_db  # noqa: E402


WEAK_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(no|not enough|insufficient)\s+(specific\s+)?(data|information|details|evidence)\b",
        r"\b(could not|can't|cannot|unable to)\s+(find|locate|determine)\b",
        r"\bautomated search (did not|didn't) (return|find)\b",
        r"\bno (results|matching tickets|relevant tickets|documents?)\b",
        r"\bquery error\b",
        r"\bunrecognized name\b",
        r"\bnot found inside\b",
    ]
]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _short(text: Any, limit: int = 260) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[:limit] + "..."


def _contains_weak_language(answer: str) -> bool:
    return any(pattern.search(answer or "") for pattern in WEAK_PATTERNS)


def _tools(decision: Any) -> list[str]:
    data = _json_loads(decision) or {}
    tools = data.get("tools") if isinstance(data, dict) else []
    return [str(t) for t in tools] if isinstance(tools, list) else []


def _counts(value: Any) -> dict:
    data = _json_loads(value)
    return data if isinstance(data, dict) else {}


def _positive_int(data: dict, key: str) -> int:
    try:
        return int(data.get(key) or 0)
    except Exception:
        return 0


def _tool_latency(data: dict) -> dict[str, int]:
    raw = data.get("tool_latency_ms")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)
        except Exception:
            continue
    return out


def _latest_recorded_audit() -> dict | None:
    docs = (
        get_db()
        .collection("chat_audit_runs")
        .order_by("end_at", direction="DESCENDING")
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        return data
    return None


def _query_rows(start: datetime, end: datetime, limit: int) -> list[bigquery.table.Row]:
    client = bigquery.Client(project=settings.google_cloud_project_id)
    sql = f"""
        SELECT
          created_at,
          conversation_id,
          turn_id,
          trace_id,
          owner_email,
          owner_role,
          ticket_id,
          target_ticket_id,
          latest_question,
          assistant_answer,
          phase1_decision,
          policy_flags,
          evidence_snapshot,
          counts,
          latency_ms,
          backend_revision,
          gcs_payload_uri
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE created_at >= @start_at
          AND created_at < @end_at
        ORDER BY created_at ASC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start),
            bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    return list(client.query(sql, job_config=job_config).result())


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return int(ordered[idx])


def build_report(start: datetime, end: datetime, rows: list[bigquery.table.Row]) -> dict:
    weak_examples: list[dict] = []
    error_examples: list[dict] = []
    low_evidence_examples: list[dict] = []
    completeness_examples: list[dict] = []
    slow_examples: list[dict] = []
    tool_counter: Counter[str] = Counter()
    revision_counter: Counter[str] = Counter()
    user_counter: Counter[str] = Counter()
    missing_counter: Counter[str] = Counter()
    latency_values: list[int] = []
    per_tool_latency: dict[str, list[int]] = {}

    weak_count = 0
    tool_error_count = 0
    low_evidence_count = 0
    slow_count = 0

    for row in rows:
        answer = row.assistant_answer or ""
        question = row.latest_question or ""
        counts = _counts(row.counts)
        tools = _tools(row.phase1_decision)
        for tool in tools:
            tool_counter[tool] += 1
        if row.backend_revision:
            revision_counter[str(row.backend_revision)] += 1
        if row.owner_email:
            user_counter[str(row.owner_email)] += 1

        latency_ms = int(row.latency_ms or 0)
        if latency_ms:
            latency_values.append(latency_ms)
        for tool, elapsed in _tool_latency(counts).items():
            per_tool_latency.setdefault(tool, []).append(elapsed)

        evidence_count = (
            _positive_int(counts, "kb")
            + _positive_int(counts, "drive")
            + _positive_int(counts, "similar")
            + _positive_int(counts, "bq")
        )
        sources_count = _positive_int(counts, "sources")
        tickets_count = _positive_int(counts, "tickets")

        missing = []
        for field in ["trace_id", "conversation_id", "turn_id", "latest_question", "assistant_answer"]:
            if not getattr(row, field, None):
                missing.append(field)
                missing_counter[field] += 1
        if row.gcs_payload_uri is None:
            missing.append("gcs_payload_uri")
            missing_counter["gcs_payload_uri"] += 1
        if not isinstance(counts, dict) or not counts:
            missing.append("counts")
            missing_counter["counts"] += 1

        base = {
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "trace_id": row.trace_id,
            "conversation_id": row.conversation_id,
            "turn_id": row.turn_id,
            "owner_email": row.owner_email,
            "question": _short(question),
            "answer_excerpt": _short(answer),
            "tools": tools,
            "counts": counts,
            "latency_ms": latency_ms or None,
            "backend_revision": row.backend_revision,
            "gcs_payload_uri": row.gcs_payload_uri,
        }

        if _contains_weak_language(answer):
            weak_count += 1
            if len(weak_examples) < 12:
                weak_examples.append(base)

        error_text = " ".join([answer, json.dumps(_json_loads(row.evidence_snapshot), default=str)])
        if re.search(r"query error|unrecognized name|not found inside|exception|failed", error_text, re.I):
            tool_error_count += 1
            if len(error_examples) < 12:
                error_examples.append(base)

        if evidence_count == 0 or (sources_count == 0 and tickets_count == 0 and tools):
            low_evidence_count += 1
            if len(low_evidence_examples) < 12:
                low_evidence_examples.append(base)

        if latency_ms >= 20000:
            slow_count += 1
            if len(slow_examples) < 12:
                slow_examples.append(base)

        if missing:
            if len(completeness_examples) < 12:
                item = dict(base)
                item["missing_fields"] = missing
                completeness_examples.append(item)

    tool_latency_summary = {}
    for tool, values in sorted(per_tool_latency.items()):
        tool_latency_summary[tool] = {
            "count": len(values),
            "p50_ms": _percentile(values, 0.50),
            "p90_ms": _percentile(values, 0.90),
            "max_ms": max(values) if values else None,
        }

    return {
        "window": {
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        },
        "total_turns": len(rows),
        "unique_users": len(user_counter),
        "counts": {
            "weak_language": weak_count,
            "tool_or_query_errors": tool_error_count,
            "low_evidence": low_evidence_count,
            "slow_20s_or_more": slow_count,
            "missing_trace_fields": sum(missing_counter.values()),
        },
        "latency": {
            "p50_ms": _percentile(latency_values, 0.50),
            "p90_ms": _percentile(latency_values, 0.90),
            "max_ms": max(latency_values) if latency_values else None,
            "by_tool": tool_latency_summary,
        },
        "top_tools": dict(tool_counter.most_common(20)),
        "backend_revisions": dict(revision_counter.most_common(20)),
        "missing_fields": dict(missing_counter.most_common()),
        "examples": {
            "weak_language": weak_examples,
            "tool_or_query_errors": error_examples,
            "low_evidence": low_evidence_examples,
            "slow": slow_examples,
            "trace_completeness": completeness_examples,
        },
    }


def _print_markdown(report: dict, previous: dict | None) -> None:
    window = report["window"]
    counts = report["counts"]
    latency = report["latency"]
    print("# Universal Chat Audit")
    print()
    print(f"- Window: `{window['start_at']}` to `{window['end_at']}`")
    if previous:
        print(f"- Previous checkpoint: `{previous.get('id')}` ended at `{previous.get('end_at')}`")
    print(f"- Total turns: {report['total_turns']}")
    print(f"- Unique users: {report['unique_users']}")
    print(f"- Weak/no-answer language: {counts['weak_language']}")
    print(f"- Tool/query errors: {counts['tool_or_query_errors']}")
    print(f"- Low evidence turns: {counts['low_evidence']}")
    print(f"- Slow turns >=20s: {counts['slow_20s_or_more']}")
    print(f"- Missing trace fields: {counts['missing_trace_fields']}")
    print(f"- Latency p50/p90/max ms: {latency['p50_ms']} / {latency['p90_ms']} / {latency['max_ms']}")
    if latency["by_tool"]:
        print()
        print("## Tool Latency")
        for tool, item in latency["by_tool"].items():
            print(f"- `{tool}`: count={item['count']} p50={item['p50_ms']} p90={item['p90_ms']} max={item['max_ms']}")
    for group, items in report["examples"].items():
        if not items:
            continue
        print()
        print(f"## Examples: {group}")
        for item in items[:8]:
            print(f"- `{item.get('trace_id')}` {item.get('created_at')} latency={item.get('latency_ms')} tools={item.get('tools')}")
            print(f"  Q: {item.get('question')}")
            print(f"  A: {item.get('answer_excerpt')}")


def _record_run(report: dict) -> str:
    start = report["window"]["start_at"]
    end = report["window"]["end_at"]
    doc_id = f"chat_audit_{start[:10]}_{end[:10]}_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    get_db().collection("chat_audit_runs").document(doc_id).set({
        "start_at": start,
        "end_at": end,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_turns": report["total_turns"],
        "counts": report["counts"],
        "latency": report["latency"],
        "backend_revisions": report["backend_revisions"],
    })
    return doc_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Universal Chat durable logs in a bounded window.")
    parser.add_argument("--start", help="Inclusive UTC timestamp, e.g. 2026-05-03T00:00:00Z")
    parser.add_argument("--end", help="Exclusive UTC timestamp. Defaults to now.")
    parser.add_argument("--days", type=int, default=None, help="Use the last N days when --start is omitted.")
    parser.add_argument("--since-last-audit", action="store_true", help="Start from the previous recorded audit end_at.")
    parser.add_argument("--limit", type=int, default=1000, help="Maximum rows to inspect.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--record-run", action="store_true", help="Write a Firestore chat_audit_runs checkpoint.")
    args = parser.parse_args()

    previous = _latest_recorded_audit() if args.since_last_audit or args.record_run else None
    end = _parse_ts(args.end) or datetime.now(timezone.utc)
    if args.start:
        start = _parse_ts(args.start)
    elif args.since_last_audit and previous and previous.get("end_at"):
        start = _parse_ts(str(previous["end_at"]))
    else:
        days = max(1, min(int(args.days or 7), 90))
        start = end - timedelta(days=days)
    if start is None or start >= end:
        raise SystemExit("Invalid audit window: start must be before end")

    rows = _query_rows(start, end, max(1, min(int(args.limit or 1000), 10000)))
    report = build_report(start, end, rows)
    if args.record_run:
        report["recorded_audit_id"] = _record_run(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_markdown(report, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
