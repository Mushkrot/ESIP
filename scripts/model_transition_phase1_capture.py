#!/usr/bin/env python3
"""Capture a bounded, private production baseline for Model Transition Phase 1.

The collector reads durable Universal Chat rows only. It never calls a model and
never writes to Google Cloud. Raw rows are written only below the explicitly
private export directory; the console/report summary contains metadata only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PRIVATE_ROOT = ROOT / "export" / "model_replacement_test"
TABLE_SUFFIX = "esip_reference.universal_chat_turn_logs"
LANES = (
    "A_bounded_structured",
    "B_grounded_answer",
    "C_contextual_tool_workflow",
    "D_multimodal",
)

_FOLLOWUP_RE = re.compile(
    r"\b(also|that one|those|same|only|just|what about|and for|previous|earlier|again)\b|"
    r"\b(тот|это|только|еще|ещё|предыдущ|а для|снова)\b",
    re.IGNORECASE,
)
_MULTIMODAL_RE = re.compile(r"\b(image|photo|screenshot|pdf|ocr|attachment|скрин|фото|изображ)", re.IGNORECASE)
_STRUCTURED_RE = re.compile(r"\b(json|sql|count|how many|list|top|ticket\s+ids?|store\s+ids?)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"\b(error|exception|failed|failure|unrecognized name|query error|500|502)\b", re.IGNORECASE)
_WEAK_RE = re.compile(
    r"no data|cannot find|could not find|not enough|insufficient|unable to|"
    r"no results|not found|не могу|не найден|нет данных|недостаточно",
    re.IGNORECASE,
)

RAW_FIELDS = (
    "created_at",
    "conversation_id",
    "turn_id",
    "trace_id",
    "owner_uid",
    "owner_email",
    "owner_role",
    "ticket_id",
    "target_ticket_id",
    "latest_question",
    "assistant_answer",
    "messages",
    "phase1_decision",
    "policy_flags",
    "evidence_snapshot",
    "sources",
    "tickets",
    "counts",
    "latency_ms",
    "model",
    "backend_revision",
    "gcs_payload_uri",
    "schema_version",
)


def json_value(value: Any) -> Any:
    """Convert BigQuery Row values and JSON columns into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, dict):
            return {str(key): json_value(item) for key, item in value.items()}
        return [json_value(item) for item in value]
    if hasattr(value, "items"):
        return {str(key): json_value(item) for key, item in value.items()}
    return str(value)


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def compact_text(value: Any, limit: int = 16000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def row_dict(row: Any) -> dict[str, Any]:
    """Extract the declared log schema without silently inventing fields."""
    output: dict[str, Any] = {}
    for field in RAW_FIELDS:
        value = getattr(row, field, None)
        if field in {"messages", "phase1_decision", "policy_flags", "evidence_snapshot", "sources", "tickets", "counts"}:
            parsed = parse_json(value)
            output[field] = json_value(parsed if parsed is not None else value)
        elif field in {"latest_question", "assistant_answer"}:
            output[field] = compact_text(value)
        else:
            output[field] = json_value(value)
    return output


def _tools(row: dict[str, Any]) -> list[str]:
    decision = row.get("phase1_decision")
    values = decision.get("tools") if isinstance(decision, dict) else []
    return [str(value) for value in values if str(value).strip()] if isinstance(values, list) else []


def _counts(row: dict[str, Any]) -> dict[str, int]:
    raw = row.get("counts")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def lane_for_row(row: dict[str, Any]) -> str:
    """Choose one conservative QA lane from explicit route/evidence signals."""
    question = str(row.get("latest_question") or "")
    answer = str(row.get("assistant_answer") or "")
    counts = _counts(row)
    decision = row.get("phase1_decision") if isinstance(row.get("phase1_decision"), dict) else {}
    policy = row.get("policy_flags") if isinstance(row.get("policy_flags"), dict) else {}
    # Do not scan raw evidence or ticket metadata for this lane. Ticket rows
    # commonly contain attachment/image words even when the user asked a
    # normal text question; only the request and explicit OCR policy signals
    # establish a multimodal case.
    explicit_multimodal_policy = any(
        key in policy for key in ("ocr", "image", "pdf", "attachment", "media_resolution")
    )
    if _MULTIMODAL_RE.search(question) or explicit_multimodal_policy:
        return "D_multimodal"
    messages = row.get("messages")
    if isinstance(messages, list) and len(messages) > 2 or _FOLLOWUP_RE.search(question):
        return "C_contextual_tool_workflow"
    if _STRUCTURED_RE.search(question) or any(key in decision for key in ("selected_executor", "sql", "query")):
        return "A_bounded_structured"
    evidence_total = sum(counts.get(key, 0) for key in ("kb", "drive", "similar", "bq", "sources", "tickets"))
    if evidence_total or _tools(row) or _WEAK_RE.search(answer):
        return "B_grounded_answer"
    return "B_grounded_answer"


def risk_signals(row: dict[str, Any]) -> list[str]:
    answer = str(row.get("assistant_answer") or "")
    evidence = row.get("evidence_snapshot")
    counts = _counts(row)
    signals: list[str] = []
    if _WEAK_RE.search(answer):
        signals.append("weak_or_no_answer_language")
    # Evidence often contains legitimate ticket text such as "error". It is
    # not a tool failure unless the answer or compact policy signal says so.
    policy = row.get("policy_flags") if isinstance(row.get("policy_flags"), dict) else {}
    if _ERROR_RE.search(answer) or any(
        key in policy for key in ("query_error", "tool_error", "retrieval_error", "error")
    ):
        signals.append("tool_or_query_error_language")
    try:
        if int(row.get("latency_ms") or 0) >= 20000:
            signals.append("slow_20s_or_more")
    except (TypeError, ValueError):
        pass
    if sum(counts.get(key, 0) for key in ("kb", "drive", "similar", "bq", "sources", "tickets")) == 0 and _tools(row):
        signals.append("low_visible_evidence")
    if _FOLLOWUP_RE.search(str(row.get("latest_question") or "")):
        signals.append("possible_followup")
    return signals


def _case_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    signals = risk_signals(row)
    try:
        latency = int(row.get("latency_ms") or 0)
    except (TypeError, ValueError):
        latency = 0
    return (len(signals), latency, str(row.get("created_at") or ""))


def select_cases(rows: Iterable[dict[str, Any]], per_lane: int = 12) -> list[dict[str, Any]]:
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_lane[lane_for_row(row)].append(row)
    selected: list[dict[str, Any]] = []
    for lane in LANES:
        candidates = sorted(by_lane.get(lane, []), key=_case_sort_key, reverse=True)
        selected.extend(candidates[: max(1, int(per_lane))])
    return selected


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def summarize(rows: list[dict[str, Any]], selected: list[dict[str, Any]], *, start: str, end: str, limit: int, per_lane: int) -> dict[str, Any]:
    selected_by_lane = Counter(lane_for_row(row) for row in selected)
    source_rows_missing: Counter[str] = Counter()
    for row in selected:
        for field in RAW_FIELDS:
            value = row.get(field)
            if value in (None, "", [], {}):
                source_rows_missing[field] += 1
    risk_counter = Counter(signal for row in selected for signal in risk_signals(row))
    revisions = Counter(str(row.get("backend_revision")) for row in selected if row.get("backend_revision"))
    models = Counter(str(row.get("model")) for row in selected if row.get("model"))
    latencies = sorted(int(row["latency_ms"]) for row in selected if str(row.get("latency_ms") or "").isdigit())
    return {
        "window": {"start_at": start, "end_at": end, "timezone": "UTC"},
        "query": {"source": TABLE_SUFFIX, "rows_scanned": len(rows), "limit": limit, "per_lane": per_lane, "read_only": True},
        "selection": {"cases_selected": len(selected), "by_lane": dict(selected_by_lane), "required_lanes": list(LANES)},
        "models": dict(models),
        "backend_revisions": dict(revisions),
        "risk_signals": dict(risk_counter),
        "missing_fields_in_selected": dict(source_rows_missing),
        "latency_ms": {
            "count": len(latencies),
            "min": latencies[0] if latencies else None,
            "median": latencies[len(latencies) // 2] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
        "quality_events": {
            "count": 0,
            "available": False,
            "source": "Firestore chat_quality_reviews (optional companion read)",
        },
        "limitations": [
            "This snapshot is a production-behavior reference, not a correctness judgment.",
            "Only Universal Chat durable turn logs are captured; non-Universal-Chat surfaces remain inventory-only unless a separate source is added.",
            "Missing telemetry is counted, never inferred.",
        ],
    }


def query_rows(start: datetime, end: datetime, limit: int) -> list[dict[str, Any]]:
    from dotenv import load_dotenv
    from google.cloud import bigquery
    from app.config import settings

    load_dotenv(ROOT / ".env", override=True)
    client = bigquery.Client(project=settings.google_cloud_project_id)
    table = f"{settings.google_cloud_project_id}.{TABLE_SUFFIX}"
    sql = f"""
        SELECT {', '.join(RAW_FIELDS)}
        FROM `{table}`
        WHERE created_at >= @start_at AND created_at < @end_at
        ORDER BY created_at DESC
        LIMIT @limit
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start),
            bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ],
        maximum_bytes_billed=500_000_000,
        use_query_cache=True,
    )
    return [row_dict(row) for row in client.query(sql, job_config=config).result()]


def fetch_quality_events(start: datetime, end: datetime, limit: int = 2000) -> tuple[list[dict[str, Any]], str | None]:
    """Read compact review metadata only; return a gap instead of guessing on failure."""
    try:
        from google.cloud import firestore as gcloud_firestore
        from app.services.firestore import get_db

        query = (
            get_db().collection("chat_quality_reviews")
            .where("created_at", ">=", start.isoformat())
            .where("created_at", "<", end.isoformat())
            .order_by("created_at", direction=gcloud_firestore.Query.DESCENDING)
            .limit(max(1, min(int(limit), 2000)))
        )
        allowed = {
            "created_at", "trace_id", "turn_id", "conversation_id", "quality", "review_quality",
            "validator_quality", "risk_classes", "validator_risk_classes", "problem_types",
            "validator_recommended_action", "recommended_action", "repair_action",
            "repair_loop_stop_reason", "repair_loop_validation_count", "repair_loop_revalidation_count",
            "model", "model_surface", "latency_ms", "turn_latency_ms", "estimated_cost_cad",
            "actual_cost_cad", "sources_count", "tickets_count", "target_ticket_id",
            "review_route_signals", "mode",
        }
        events: list[dict[str, Any]] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            events.append({key: json_value(data.get(key)) for key in allowed if data.get(key) not in (None, "", [], {})})
        return events, None
    except Exception as exc:  # pragma: no cover - exercised only with live credentials
        return [], type(exc).__name__


def write_snapshot(
    output: Path,
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    summary: dict[str, Any],
    quality_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    selected_path = output / "baseline_cases.jsonl"
    all_rows_path = output / "scanned_rows.jsonl"
    with selected_path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(selected, start=1):
            packet = dict(row)
            packet["case_id"] = f"P1-{index:04d}"
            packet["lane"] = lane_for_row(row)
            packet["risk_signals"] = risk_signals(row)
            handle.write(json.dumps(packet, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    with all_rows_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    quality_events = list(quality_events or [])
    quality_path = output / "quality_events.jsonl"
    with quality_path.open("w", encoding="utf-8") as handle:
        for event in quality_events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    summary["quality_events"] = {
        "count": len(quality_events),
        "available": bool(quality_events),
        "source": "Firestore chat_quality_reviews (optional companion read)",
    }
    manifest = {
        "schema": "esip.model_transition.phase1.private_snapshot.v1",
        "summary": summary,
        "files": {
            "baseline_cases.jsonl": {"sha256": hashlib.sha256(selected_path.read_bytes()).hexdigest(), "rows": len(selected)},
            "scanned_rows.jsonl": {"sha256": hashlib.sha256(all_rows_path.read_bytes()).hexdigest(), "rows": len(rows)},
            "quality_events.jsonl": {"sha256": hashlib.sha256(quality_path.read_bytes()).hexdigest(), "rows": len(quality_events)},
        },
        "manifest_sha256": None,
    }
    manifest["manifest_sha256"] = canonical_hash({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="Inclusive UTC timestamp")
    parser.add_argument("--end", help="Exclusive UTC timestamp")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--per-lane", type=int, default=12)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    end = datetime.fromisoformat((args.end or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00")).astimezone(timezone.utc)
    start = datetime.fromisoformat((args.start or (end - timedelta(days=max(1, args.days))).isoformat()).replace("Z", "+00:00")).astimezone(timezone.utc)
    if start >= end:
        raise SystemExit("start must be before end")
    limit = max(1, min(int(args.limit), 10000))
    per_lane = max(1, min(int(args.per_lane), 50))
    rows = query_rows(start, end, limit)
    selected = select_cases(rows, per_lane=per_lane)
    summary = summarize(rows, selected, start=start.isoformat(), end=end.isoformat(), limit=limit, per_lane=per_lane)
    quality_events, quality_gap = fetch_quality_events(start, end)
    if quality_gap:
        summary["quality_events"]["gap"] = quality_gap
    output = args.output or PRIVATE_ROOT / f"phase1-{start.date().isoformat()}-{end.date().isoformat()}"
    manifest = write_snapshot(output, rows, selected, summary, quality_events=quality_events)
    print(json.dumps({"output": str(output), "manifest_sha256": manifest["manifest_sha256"], **summary}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
