#!/usr/bin/env python3
"""Build a read-only Universal Chat quality test-set seed from durable logs.

The script intentionally does not call Gemini and does not write checkpoints.
Generated output should stay under export/ unless a human redacts and promotes
specific cases into CHAT_REGRESSION_MATRIX.md or a QA script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402
from google.cloud import bigquery  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402


WEAK_RE = re.compile(
    r"no data|cannot find|could not find|not enough|insufficient|unable to|"
    r"automated search did not return|query error|не могу|не найден|недостаточно|нет данных",
    re.IGNORECASE,
)
TOOL_ERROR_RE = re.compile(r"query error|unrecognized name|not found inside|exception|failed", re.IGNORECASE)


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _short(text: str | None, limit: int = 500) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value[: limit - 3] + "..." if len(value) > limit else value


def _tools(phase1_decision: Any) -> list[str]:
    data = _json_loads(phase1_decision)
    if not isinstance(data, dict):
        return []
    tools = data.get("tools") or []
    return [str(t) for t in tools] if isinstance(tools, list) else []


def _bucket(question: str) -> str:
    q = question.lower()
    if re.search(r"(are you sure|where did you find|none of them|not related|wrong|где ты|да ладно|проверь)", q):
        return "challenge_fact_check"
    if re.search(r"(ticket|тикет|#)\s*\d{5,}|\b\d{6}\b", q):
        return "ticket_investigation"
    if re.search(r"(store|restaurant|рест|магазин).*(top|issue|past|month|trend|history|жалоб)", q):
        return "store_history_analytics"
    if re.search(r"(find document|contains|document that contains|which row|документ|строк)", q):
        return "document_exact_lookup"
    if re.search(r"(how to|how do|troubleshoot|procedure|steps|как |что делать)", q):
        return "procedure_troubleshooting"
    if re.search(r"(top|most|trend|how many|count|сколько|самые)", q):
        return "global_analytics"
    if re.search(r"(what is|what does|who handles|who owns|кто |что такое)", q):
        return "definition_owner_lookup"
    return "other_mixed"


def _risk_signals(row: bigquery.table.Row) -> list[str]:
    answer = row.assistant_answer or ""
    evidence = json.dumps(_json_loads(row.evidence_snapshot), ensure_ascii=False, default=str)
    counts = _json_loads(row.counts) or {}
    signals: list[str] = []
    if WEAK_RE.search(answer):
        signals.append("weak_or_no_answer_language")
    if TOOL_ERROR_RE.search(answer) or TOOL_ERROR_RE.search(evidence):
        signals.append("tool_or_query_error")
    try:
        if int(row.latency_ms or 0) >= 20000:
            signals.append("slow_20s_or_more")
    except (TypeError, ValueError):
        pass
    if isinstance(counts, dict):
        evidence_count = sum(int(counts.get(k) or 0) for k in ("kb", "drive", "similar", "bq"))
        emitted_count = int(counts.get("sources") or 0) + int(counts.get("tickets") or 0)
        if evidence_count == 0 or (emitted_count == 0 and _tools(row.phase1_decision)):
            signals.append("low_visible_evidence")
    return signals


def _case_from_row(row: bigquery.table.Row, include_email: bool) -> dict[str, Any]:
    counts = _json_loads(row.counts) or {}
    signals = _risk_signals(row)
    item = {
        "trace_id": row.trace_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "backend_revision": row.backend_revision,
        "bucket": _bucket(row.latest_question or ""),
        "risk_signals": signals,
        "ticket_id": row.ticket_id,
        "target_ticket_id": row.target_ticket_id,
        "question": _short(row.latest_question, 700),
        "answer_excerpt": _short(row.assistant_answer, 700),
        "tools": _tools(row.phase1_decision),
        "counts": counts,
        "latency_ms": int(row.latency_ms or 0) if row.latency_ms is not None else None,
        "gcs_payload_uri": row.gcs_payload_uri,
        "promotion_note": (
            "If this case represents a regression, promote it manually into "
            "CHAT_REGRESSION_MATRIX.md or a targeted QA script with redacted expected criteria."
        ),
    }
    if include_email:
        item["owner_email"] = row.owner_email
    return item


def _query_rows(start: datetime, end: datetime, limit: int) -> list[bigquery.table.Row]:
    client = bigquery.Client(project=settings.google_cloud_project_id)
    sql = f"""
        SELECT
          created_at,
          trace_id,
          conversation_id,
          owner_email,
          ticket_id,
          target_ticket_id,
          latest_question,
          assistant_answer,
          phase1_decision,
          evidence_snapshot,
          counts,
          latency_ms,
          backend_revision,
          gcs_payload_uri
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE created_at >= @start_at
          AND created_at < @end_at
        ORDER BY created_at DESC
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


def build_test_set(rows: list[bigquery.table.Row], include_email: bool, per_bucket: int) -> dict[str, Any]:
    bucket_rows: dict[str, list[bigquery.table.Row]] = defaultdict(list)
    revision_counter: Counter[str] = Counter()
    risk_counter: Counter[str] = Counter()
    latency_values: list[int] = []

    for row in rows:
        bucket_rows[_bucket(row.latest_question or "")].append(row)
        if row.backend_revision:
            revision_counter[str(row.backend_revision)] += 1
        for signal in _risk_signals(row):
            risk_counter[signal] += 1
        if row.latency_ms is not None:
            latency_values.append(int(row.latency_ms or 0))

    cases: list[dict[str, Any]] = []
    for bucket, items in sorted(bucket_rows.items()):
        ranked = sorted(
            items,
            key=lambda r: (
                len(_risk_signals(r)),
                int(r.latency_ms or 0),
                r.created_at.timestamp() if r.created_at else 0,
            ),
            reverse=True,
        )
        cases.extend(_case_from_row(row, include_email=include_email) for row in ranked[:per_bucket])

    latency_values.sort()
    p50 = latency_values[len(latency_values) // 2] if latency_values else None
    p90 = latency_values[int((len(latency_values) - 1) * 0.9)] if latency_values else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "BigQuery esip_reference.universal_chat_turn_logs",
        "notes": [
            "Read-only log-derived seed set; not a deterministic regression suite by itself.",
            "Do not commit generated output if it contains raw user questions or emails.",
            "Manually promote stable, redacted cases into CHAT_REGRESSION_MATRIX.md or QA scripts.",
        ],
        "summary": {
            "total_rows_scanned": len(rows),
            "cases_selected": len(cases),
            "backend_revisions": dict(revision_counter.most_common(20)),
            "risk_signals": dict(risk_counter.most_common()),
            "latency_p50_ms": p50,
            "latency_p90_ms": p90,
        },
        "cases": cases,
    }


def _parse_ts(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    raw = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    now = datetime.now(timezone.utc)
    parser = argparse.ArgumentParser(description="Build a Universal Chat quality test-set seed from logs.")
    parser.add_argument("--start", help="Inclusive UTC timestamp, e.g. 2026-05-01T00:00:00Z")
    parser.add_argument("--end", help="Exclusive UTC timestamp. Defaults to now.")
    parser.add_argument("--days", type=int, default=7, help="Lookback days when --start is omitted.")
    parser.add_argument("--limit", type=int, default=1000, help="Max log rows to scan.")
    parser.add_argument("--per-bucket", type=int, default=6, help="Selected cases per question bucket.")
    parser.add_argument("--include-emails", action="store_true", help="Include owner_email in output.")
    parser.add_argument("--output", type=Path, help="Write JSON output to this path.")
    args = parser.parse_args()

    end = _parse_ts(args.end, now)
    start_default = end - timedelta(days=max(1, args.days))
    start = _parse_ts(args.start, start_default)
    rows = _query_rows(start, end, max(1, args.limit))
    report = build_test_set(rows, include_email=args.include_emails, per_bucket=max(1, args.per_bucket))
    report["window"] = {"start_at": start.isoformat(), "end_at": end.isoformat()}

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)
    print(
        "Summary:",
        f"rows={report['summary']['total_rows_scanned']}",
        f"cases={report['summary']['cases_selected']}",
        f"risks={report['summary']['risk_signals']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
