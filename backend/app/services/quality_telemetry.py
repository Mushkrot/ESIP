"""Read-only Universal Chat quality telemetry summaries.

This module intentionally contains pure summarization helpers first. Live
Firestore/BigQuery fetching is wired later by the admin router, but the payload
contract lives here so frontend and backend cannot drift independently.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import firestore as gcloud_firestore

from app.config import settings
from app.services import bigquery as bq_service
from app.services import error_provenance, firestore

BUSINESS_TZ = ZoneInfo("America/New_York")

DASHBOARD_SECTIONS = (
    "range",
    "summary",
    "coverage",
    "quality_distribution",
    "validator_actions",
    "risk_classes",
    "repair_actions",
    "repair_stop_reasons",
    "model_surfaces",
    "latency",
    "stage_timings",
    "error_provenance",
    "cost",
    "trend",
    "recent_events",
    "review_queue",
    "warnings",
    "caveats",
)

RAW_TEXT_KEYS = {
    "question",
    "latest_question",
    "answer",
    "assistant_answer",
    "messages",
    "trace",
    "trace_json",
    "final_answer",
    "improved_answer",
    "rewrite_from_supplied_evidence",
}

KNOWN_QUALITIES = {
    "good",
    "weak",
    "likely_wrong",
    "insufficient_evidence",
    "suspect",
    "unsafe",
    "unknown",
}

REVIEW_REASON_TAGS = (
    "missed_context",
    "wrong_source",
    "no_data",
    "too_long",
    "slow",
    "needs_ticket_resolution",
)
REVIEW_TOO_LONG_ANSWER_CHARS = 6000
REVIEW_SLOW_LATENCY_MS = 30000
UNIVERSAL_STAGE_NAMES = (
    "interpretation",
    "routing_phase1",
    "retrieval",
    "answer_production",
    "validator_repair",
    "output_critic",
    "persistence",
)

_MISSED_CONTEXT_PROBLEMS = {"ambiguous_question", "ignored_negative_constraint"}
_MISSED_CONTEXT_RISKS = {"context_loss", "followup_scope_loss", "evidence_mismatch", "denominator_drift"}
_WRONG_SOURCE_PROBLEMS = {"wrong_routing", "obsolete_source", "conflicting_sources"}
_WRONG_SOURCE_RISKS = {"source_family_mismatch", "wrong_source", "wrong_routing"}
_NO_DATA_RISKS = {"false_no_data", "tool_error_as_absence", "stale_data", "no_evidence"}
_TICKET_RESOLUTION_RISKS = {"wrong_resolution_claim", "no_documented_resolution"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=BUSINESS_TZ)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=BUSINESS_TZ)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=BUSINESS_TZ)
    return None


def _day_key(value: Any) -> str | None:
    parsed = _as_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(BUSINESS_TZ).date().isoformat()


def resolve_quality_range(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    today: date | None = None,
    max_days: int = 31,
) -> dict[str, Any]:
    """Return a bounded inclusive Eastern date range for dashboard queries."""
    anchor = today or datetime.now(BUSINESS_TZ).date()
    try:
        end_day = date.fromisoformat((end_date or "").strip()) if end_date else anchor
    except ValueError:
        end_day = anchor
    try:
        start_day = date.fromisoformat((start_date or "").strip()) if start_date else end_day - timedelta(days=6)
    except ValueError:
        start_day = end_day - timedelta(days=6)

    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days + 1 > max_days:
        start_day = end_day - timedelta(days=max_days - 1)

    start_at = datetime.combine(start_day, time.min, tzinfo=BUSINESS_TZ)
    end_at = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)
    return {
        "timezone": "America/New_York",
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
        "max_days": max_days,
    }


def _bucket(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": key, "count": count} for key, count in counter.most_common()]


def _normalize_quality(value: Any) -> str:
    quality = str(value or "unknown").strip() or "unknown"
    return quality if quality in KNOWN_QUALITIES else "unknown"


def _list_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _compact_recent_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a compact event row without raw question/answer text."""
    quality = _normalize_quality(
        event.get("validator_quality")
        or event.get("quality")
        or event.get("review_quality")
    )
    compact = {
        "created_at": event.get("created_at"),
        "mode": _event_mode(event),
        "quality": quality,
        "risk_classes": _list_values(event.get("validator_risk_classes") or event.get("risk_classes")),
        "validator_action": event.get("validator_recommended_action") or event.get("recommended_action"),
        "answer_policy": event.get("validator_answer_policy") or event.get("answer_policy"),
        "repair_action": event.get("repair_action") or event.get("validator_recommended_action"),
        "repair_stop_reason": event.get("repair_loop_stop_reason"),
        "model": event.get("model") or event.get("model_id"),
        "model_surface": event.get("model_surface"),
        "latency_ms": event.get("latency_ms") or event.get("duration_ms"),
        "estimated_cost_cad": event.get("estimated_cost_cad") or event.get("cost_cad"),
        "trace_id": event.get("trace_id"),
        "turn_id": event.get("turn_id"),
        "conversation_id": event.get("conversation_id"),
        "requester_email": event.get("requester_email") or event.get("trace_owner_email"),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def _event_mode(event: dict[str, Any]) -> str:
    explicit = str(event.get("mode") or event.get("event_type") or "").strip()
    if explicit:
        return explicit
    # Historical review rows predate the explicit mode field. Validator-specific
    # fields are a reliable automatic marker; other review rows are manual.
    if any(key in event for key in ("validator_answer_policy", "repair_loop_stop_reason", "repair_loop_validation_count")):
        return "auto_validator"
    return "manual_review"


def _event_turn_key(event: dict[str, Any], index: int) -> str:
    for key in ("turn_id", "trace_id"):
        value = str(event.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return f"event:{index}"


def _quality_rank(value: str) -> int:
    return {
        "good": 0,
        "unknown": 1,
        "suspect": 2,
        "weak": 3,
        "insufficient_evidence": 4,
        "likely_wrong": 5,
        "unsafe": 6,
    }.get(value, 1)


def _latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return {
        "count": len(values),
        "avg_ms": round(sum(values) / len(values), 1),
        "p95_ms": round(ordered[p95_index], 1),
        "max_ms": round(ordered[-1], 1),
    }


def _stage_timing_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _stage_latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_ms": None, "p90_ms": None, "max_ms": None}
    ordered = sorted(values)

    def _percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))
        return round(ordered[index], 1)

    return {
        "count": len(ordered),
        "p50_ms": _percentile(0.50),
        "p90_ms": _percentile(0.90),
        "max_ms": round(ordered[-1], 1),
    }


def summarize_stage_timings(turn_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate compact per-turn stage durations without treating missing as zero."""
    values_by_stage: dict[str, list[float]] = defaultdict(list)
    missing_by_stage: Counter[str] = Counter()
    measured_turn_count = 0
    partial_turn_count = 0

    for row in turn_rows:
        payload = _stage_timing_payload(row.get("stage_timings"))
        durations = payload.get("durations_ms") if isinstance(payload.get("durations_ms"), dict) else {}
        valid: dict[str, float] = {}
        for stage in UNIVERSAL_STAGE_NAMES:
            value = _as_float(durations.get(stage))
            if value is not None and value >= 0:
                valid[stage] = value
                values_by_stage[stage].append(value)
            else:
                missing_by_stage[stage] += 1
        if valid:
            measured_turn_count += 1
            if len(valid) < len(UNIVERSAL_STAGE_NAMES):
                partial_turn_count += 1

    return {
        "available": measured_turn_count > 0,
        "turn_count": len(turn_rows),
        "measured_turn_count": measured_turn_count,
        "unmeasured_turn_count": len(turn_rows) - measured_turn_count,
        "partial_measured_turn_count": partial_turn_count,
        "stages": [
            {"stage": stage, **_stage_latency_summary(values_by_stage.get(stage, []))}
            for stage in UNIVERSAL_STAGE_NAMES
        ],
        "missing_by_stage": [
            {"stage": stage, "count": missing_by_stage[stage]}
            for stage in UNIVERSAL_STAGE_NAMES
        ],
        "caveat": (
            "Stage timings are private in-process observations. Streaming and asynchronous stages may overlap, "
            "so stage durations need not sum to total turn latency; missing stages are not reported as zero."
        ),
    }


def summarize_error_provenance(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate only the fixed compact provenance schema."""
    sources: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    endpoint_classes: Counter[str] = Counter()
    reason_codes: Counter[str] = Counter()
    status_codes: Counter[str] = Counter()
    fallback_count = 0
    for event in events:
        source_value = str(event.get("source") or "")
        outcome_value = str(event.get("outcome") or "")
        endpoint_value = str(event.get("endpoint_class") or "")
        reason_value = str(event.get("reason_code") or "")
        source = source_value if source_value in error_provenance.SOURCES else "unknown"
        outcome = outcome_value if outcome_value in error_provenance.OUTCOMES else "unknown"
        endpoint_class = endpoint_value if endpoint_value in error_provenance.ENDPOINT_CLASSES else "unknown"
        reason_code = reason_value if reason_value in error_provenance.REASON_CODES else "unknown"
        sources[source] += 1
        outcomes[outcome] += 1
        endpoint_classes[endpoint_class] += 1
        reason_codes[reason_code] += 1
        status_code = _as_int(event.get("status_code"), 0)
        if status_code:
            status_codes[str(status_code)] += 1
        if bool(event.get("fallback_used")):
            fallback_count += 1
    return {
        "event_count": len(events),
        "fallback_used_count": fallback_count,
        "sources": _bucket(sources),
        "outcomes": _bucket(outcomes),
        "endpoint_classes": _bucket(endpoint_classes),
        "status_codes": _bucket(status_codes),
        "reason_codes": _bucket(reason_codes),
    }


def classify_review_reason_tags(
    event: dict[str, Any],
    turn_row: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return deterministic triage tags from compact evidence only."""
    turn = turn_row or {}
    problems = set(_list_values(event.get("problem_types")))
    risks = set(_list_values(event.get("validator_risk_classes") or event.get("risk_classes")))
    route_signals = event.get("review_route_signals") if isinstance(event.get("review_route_signals"), dict) else {}
    risks.update(_list_values(route_signals.get("risk_classes")))
    quality = _normalize_quality(event.get("validator_quality") or event.get("quality") or event.get("review_quality"))

    answer_chars = _as_int(turn.get("answer_chars"), -1)
    if answer_chars < 0:
        answer_chars = _as_int(event.get("answer_chars"), -1)
    latency_ms = _as_float(turn.get("latency_ms"))
    if latency_ms is None:
        latency_ms = _as_float(event.get("turn_latency_ms"))
    sources_count = _as_int(turn.get("sources_count"), -1)
    if sources_count < 0:
        sources_count = _as_int(event.get("sources_count"), -1)
    tickets_count = _as_int(turn.get("tickets_count"), -1)
    if tickets_count < 0:
        tickets_count = _as_int(event.get("tickets_count"), -1)

    tags: list[str] = []
    if problems & _MISSED_CONTEXT_PROBLEMS or risks & _MISSED_CONTEXT_RISKS:
        tags.append("missed_context")
    if problems & _WRONG_SOURCE_PROBLEMS or risks & _WRONG_SOURCE_RISKS:
        tags.append("wrong_source")
    if risks & _NO_DATA_RISKS or (
        quality == "insufficient_evidence"
        and sources_count == 0
        and tickets_count == 0
    ):
        tags.append("no_data")
    if answer_chars >= REVIEW_TOO_LONG_ANSWER_CHARS:
        tags.append("too_long")
    if latency_ms is not None and latency_ms >= REVIEW_SLOW_LATENCY_MS:
        tags.append("slow")
    resolution_candidate = bool(route_signals.get("ticket_resolution_candidate"))
    if risks & _TICKET_RESOLUTION_RISKS or (
        resolution_candidate
        and quality in {"weak", "likely_wrong", "insufficient_evidence", "suspect", "unsafe"}
    ):
        tags.append("needs_ticket_resolution")
    return tuple(tag for tag in REVIEW_REASON_TAGS if tag in tags)


def build_review_queue(
    *,
    quality_events: list[dict[str, Any]],
    turn_rows: list[dict[str, Any]],
    reason_tag: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Build one compact queue row per answer while retaining review conflicts."""
    safe_reason = str(reason_tag or "").strip()
    if safe_reason and safe_reason not in REVIEW_REASON_TAGS:
        safe_reason = ""

    turns_by_trace = {
        str(row.get("trace_id")): row
        for row in turn_rows
        if str(row.get("trace_id") or "").strip()
    }
    turns_by_turn = {
        str(row.get("turn_id")): row
        for row in turn_rows
        if str(row.get("turn_id") or "").strip()
    }
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for index, event in enumerate(quality_events):
        trace_id = str(event.get("trace_id") or "").strip()
        turn_id = str(event.get("turn_id") or "").strip()
        turn = turns_by_trace.get(trace_id) or turns_by_turn.get(turn_id) or {}
        canonical_trace = str(turn.get("trace_id") or trace_id).strip()
        canonical_turn = str(turn.get("turn_id") or turn_id).strip()
        identity = f"trace:{canonical_trace}" if canonical_trace else (
            f"turn:{canonical_turn}" if canonical_turn else f"event:{index}"
        )
        grouped[identity].append((event, turn))

    rows: list[dict[str, Any]] = []
    for grouped_events in grouped.values():
        events = [event for event, _turn in grouped_events]
        turn = next((row for _event, row in grouped_events if row), {})
        qualities = sorted({
            _normalize_quality(event.get("validator_quality") or event.get("quality") or event.get("review_quality"))
            for event in events
        }, key=_quality_rank)
        observed_tags = {
            tag
            for event, matched_turn in grouped_events
            for tag in classify_review_reason_tags(event, matched_turn)
        }
        tags = tuple(tag for tag in REVIEW_REASON_TAGS if tag in observed_tags)
        conflict = len(qualities) > 1
        if qualities == ["good"] and not tags and not conflict:
            continue
        if safe_reason and safe_reason not in tags:
            continue
        latest_event = max(events, key=lambda event: str(event.get("created_at") or ""))
        trace_id = str(turn.get("trace_id") or latest_event.get("trace_id") or "").strip()
        turn_id = str(turn.get("turn_id") or latest_event.get("turn_id") or "").strip()
        conversation_id = str(turn.get("conversation_id") or latest_event.get("conversation_id") or "").strip()
        answer_chars = _as_int(turn.get("answer_chars"), _as_int(latest_event.get("answer_chars"), 0))
        latency_ms = _as_float(turn.get("latency_ms"))
        if latency_ms is None:
            latency_ms = _as_float(latest_event.get("turn_latency_ms"))
        target_ticket_id = turn.get("target_ticket_id") or latest_event.get("target_ticket_id")
        rows.append({
            "created_at": latest_event.get("created_at"),
            "trace_id": trace_id or None,
            "turn_id": turn_id or None,
            "conversation_id": conversation_id or None,
            "target_ticket_id": target_ticket_id,
            "event_count": len(events),
            "modes": sorted({_event_mode(event) for event in events}),
            "qualities": qualities,
            "conflicting_verdict": conflict,
            "reason_tags": list(tags),
            "latency_ms": latency_ms,
            "answer_chars": answer_chars,
            "sources_count": _as_int(turn.get("sources_count"), _as_int(latest_event.get("sources_count"), 0)),
            "tickets_count": _as_int(turn.get("tickets_count"), _as_int(latest_event.get("tickets_count"), 0)),
        })

    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    reason_counts = Counter(tag for row in rows for tag in row.get("reason_tags") or [])
    safe_limit = max(1, min(int(limit or 25), 100))
    return {
        "available_reason_tags": list(REVIEW_REASON_TAGS),
        "selected_reason_tag": safe_reason or None,
        "thresholds": {
            "too_long_answer_chars": REVIEW_TOO_LONG_ANSWER_CHARS,
            "slow_latency_ms": REVIEW_SLOW_LATENCY_MS,
        },
        "answer_count": len(rows),
        "untagged_answer_count": sum(1 for row in rows if not row.get("reason_tags")),
        "reason_distribution": _bucket(reason_counts),
        "rows": rows[:safe_limit],
    }


def summarize_quality_telemetry(
    *,
    quality_events: list[dict[str, Any]] | None = None,
    turn_rows: list[dict[str, Any]] | None = None,
    range_info: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    recent_limit: int = 25,
    review_reason_tag: str | None = None,
    provenance_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the dashboard payload from compact quality events and turn rows."""
    events = list(quality_events or [])
    turns = list(turn_rows or [])
    safe_range = range_info or resolve_quality_range()

    qualities: Counter[str] = Counter()
    validator_actions: Counter[str] = Counter()
    risk_classes: Counter[str] = Counter()
    repair_actions: Counter[str] = Counter()
    repair_stop_reasons: Counter[str] = Counter()
    model_surfaces: Counter[str] = Counter()
    trend: dict[str, Counter[str]] = defaultdict(Counter)
    latencies: list[float] = []
    costs: list[float] = []
    unknown_field_events = 0
    mode_counts: Counter[str] = Counter()
    events_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for index, event in enumerate(events):
        quality = _normalize_quality(event.get("validator_quality") or event.get("quality") or event.get("review_quality"))
        mode_counts[_event_mode(event)] += 1
        events_by_turn[_event_turn_key(event, index)].append(event)
        qualities[quality] += 1
        if quality == "unknown":
            unknown_field_events += 1
        action = event.get("validator_recommended_action") or event.get("recommended_action")
        validator_actions[str(action or "unknown")] += 1
        for risk in _list_values(event.get("validator_risk_classes") or event.get("risk_classes")):
            risk_classes[risk] += 1
        repair_action = event.get("repair_action") or event.get("validator_recommended_action")
        if repair_action:
            repair_actions[str(repair_action)] += 1
        stop_reason = event.get("repair_loop_stop_reason")
        if stop_reason:
            repair_stop_reasons[str(stop_reason)] += 1
        surface = event.get("model_surface") or event.get("surface") or "unknown"
        model_surfaces[str(surface)] += 1
        latency = _as_float(event.get("latency_ms") or event.get("duration_ms"))
        if latency is not None:
            latencies.append(latency)
        cost = _as_float(event.get("estimated_cost_cad") or event.get("cost_cad"))
        if cost is not None:
            costs.append(cost)
        day = _day_key(event.get("created_at"))
        if day:
            trend[day]["events"] += 1
            trend[day][quality] += 1

    turn_latencies = [_as_float(row.get("latency_ms")) for row in turns]
    turn_latencies = [value for value in turn_latencies if value is not None]
    if not latencies:
        latencies = turn_latencies

    suspect_count = sum(qualities[key] for key in ("weak", "likely_wrong", "insufficient_evidence", "suspect", "unsafe"))
    unique_qualities: Counter[str] = Counter()
    conflict_count = 0
    duplicate_event_count = 0
    for grouped_events in events_by_turn.values():
        event_qualities = [
            _normalize_quality(event.get("validator_quality") or event.get("quality") or event.get("review_quality"))
            for event in grouped_events
        ]
        unique_qualities[max(event_qualities, key=_quality_rank)] += 1
        if len(grouped_events) > 1:
            duplicate_event_count += len(grouped_events) - 1
        if len(set(event_qualities)) > 1:
            conflict_count += 1
    repair_count = sum(repair_actions.values())
    validation_count = sum(_as_int(event.get("repair_loop_validation_count")) for event in events)
    revalidation_count = sum(_as_int(event.get("repair_loop_revalidation_count")) for event in events)

    compact_events = sorted(
        [_compact_recent_event(event) for event in events],
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )[: max(1, min(int(recent_limit or 25), 100))]

    caveats = [
        "Quality telemetry is diagnostic and Super Admin-only.",
        "Counts are based on completed quality events and durable Universal Chat turn logs available for the selected range.",
        "A zero count can mean no matching event was logged; warnings identify partial source failures.",
    ]
    if unknown_field_events:
        caveats.append(f"{unknown_field_events} event(s) had unknown quality labels and were bucketed as unknown.")

    review_queue = build_review_queue(
        quality_events=events,
        turn_rows=turns,
        reason_tag=review_reason_tag,
        limit=recent_limit,
    )

    return {
        "range": safe_range,
        "summary": {
            "quality_event_count": len(events),
            "unique_answer_count": len(events_by_turn),
            "auto_validator_event_count": mode_counts["auto_validator"],
            "manual_review_event_count": mode_counts["manual_review"],
            "duplicate_event_count": duplicate_event_count,
            "conflicting_verdict_count": conflict_count,
            "turn_count": len(turns),
            "suspect_event_count": suspect_count,
            "repair_attempt_count": repair_count,
            "repair_validation_count": validation_count,
            "repair_revalidation_count": revalidation_count,
        },
        "coverage": {
            "quality_events_available": len(events) > 0,
            "turn_logs_available": len(turns) > 0,
            "quality_events_count": len(events),
            "turn_logs_count": len(turns),
            "partial": bool(warnings),
        },
        "quality_distribution": _bucket(qualities),
        "unique_turn_quality_distribution": _bucket(unique_qualities),
        "validator_actions": _bucket(validator_actions),
        "risk_classes": _bucket(risk_classes),
        "repair_actions": _bucket(repair_actions),
        "repair_stop_reasons": _bucket(repair_stop_reasons),
        "model_surfaces": _bucket(model_surfaces),
        "latency": _latency_summary(latencies),
        "stage_timings": summarize_stage_timings(turns),
        "error_provenance": summarize_error_provenance(list(provenance_events or [])),
        "cost": {
            "count": len(costs),
            "estimated_total_cad": round(sum(costs), 6),
            "estimated_avg_cad": round(sum(costs) / len(costs), 6) if costs else None,
        },
        "trend": [
            {"day": day, **dict(counts)}
            for day, counts in sorted(trend.items())
        ],
        "recent_events": compact_events,
        "review_queue": review_queue,
        "warnings": list(warnings or []),
        "caveats": caveats,
    }


def _matches_filter(value: Any, expected: str | None) -> bool:
    if not expected:
        return True
    return str(value or "").strip().lower() == expected.strip().lower()


def _event_matches_filters(
    event: dict[str, Any],
    *,
    event_type: str | None = None,
    quality: str | None = None,
    risk: str | None = None,
    validator_action: str | None = None,
    repair_action: str | None = None,
    stop_reason: str | None = None,
    model_surface: str | None = None,
    requester_email: str | None = None,
) -> bool:
    if not _matches_filter(event.get("mode") or event.get("event_type"), event_type):
        return False
    normalized_quality = _normalize_quality(event.get("validator_quality") or event.get("quality") or event.get("review_quality"))
    if not _matches_filter(normalized_quality, quality):
        return False
    if risk:
        risks = [item.lower() for item in _list_values(event.get("validator_risk_classes") or event.get("risk_classes"))]
        if risk.strip().lower() not in risks:
            return False
    selected_validator = event.get("validator_recommended_action") or event.get("recommended_action")
    if not _matches_filter(selected_validator, validator_action):
        return False
    selected_repair = event.get("repair_action")
    if not _matches_filter(selected_repair, repair_action):
        return False
    if not _matches_filter(event.get("repair_loop_stop_reason"), stop_reason):
        return False
    selected_surface = event.get("model_surface") or event.get("surface")
    if not _matches_filter(selected_surface, model_surface):
        return False
    selected_email = event.get("requester_email") or event.get("trace_owner_email")
    if not _matches_filter(selected_email, requester_email):
        return False
    return True


def apply_quality_filters(
    events: list[dict[str, Any]],
    *,
    event_type: str | None = None,
    quality: str | None = None,
    risk: str | None = None,
    validator_action: str | None = None,
    repair_action: str | None = None,
    stop_reason: str | None = None,
    model_surface: str | None = None,
    requester_email: str | None = None,
) -> list[dict[str, Any]]:
    """Return quality events that match dashboard filters."""
    return [
        event for event in events
        if _event_matches_filters(
            event,
            event_type=event_type,
            quality=quality,
            risk=risk,
            validator_action=validator_action,
            repair_action=repair_action,
            stop_reason=stop_reason,
            model_surface=model_surface,
            requester_email=requester_email,
        )
    ]


def fetch_quality_review_events(
    range_info: dict[str, Any],
    *,
    limit: int = 500,
    db: Any | None = None,
) -> list[dict[str, Any]]:
    """Read bounded quality review events from Firestore."""
    safe_limit = max(1, min(int(limit or 500), 2000))
    client = db or firestore.get_db()
    query = (
        client.collection("chat_quality_reviews")
        .where("created_at", ">=", range_info["start_at"])
        .where("created_at", "<", range_info["end_at"])
        .order_by("created_at", direction=gcloud_firestore.Query.DESCENDING)
        .limit(safe_limit)
    )
    return [
        {"id": getattr(doc, "id", None), **(doc.to_dict() or {})}
        for doc in query.stream()
    ]


def fetch_system_provenance_events(
    range_info: dict[str, Any],
    *,
    limit: int = 1000,
    db: Any | None = None,
) -> list[dict[str, Any]]:
    """Read bounded compact failure/fallback provenance from Firestore."""
    safe_limit = max(1, min(int(limit or 1000), 5000))
    client = db or firestore.get_db()
    query = (
        client.collection("system_provenance_events")
        .where("created_at", ">=", range_info["start_at"])
        .where("created_at", "<", range_info["end_at"])
        .order_by("created_at", direction=gcloud_firestore.Query.DESCENDING)
        .limit(safe_limit)
    )
    return [
        {"id": getattr(doc, "id", None), **(doc.to_dict() or {})}
        for doc in query.stream()
    ]


def fetch_turn_log_rows(
    range_info: dict[str, Any],
    *,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Read bounded denominator/latency rows from durable Universal Chat logs."""
    bq_client = client or bq_service._get_client()
    sql = f"""
        SELECT
          created_at,
          turn_id,
          trace_id,
          conversation_id,
          owner_email,
          owner_role,
          target_ticket_id,
          latency_ms,
          model,
          JSON_VALUE(counts, '$.sources') AS sources_count,
          JSON_VALUE(counts, '$.tickets') AS tickets_count,
          JSON_VALUE(counts, '$.answer_chars') AS answer_chars,
          JSON_QUERY(counts, '$.stage_timings') AS stage_timings_json
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE created_at >= @start_at
          AND created_at < @end_at
        ORDER BY created_at DESC
        LIMIT 5000
    """
    job_config = bq_service.bigquery.QueryJobConfig(
        query_parameters=[
            bq_service.bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", range_info["start_at"]),
            bq_service.bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", range_info["end_at"]),
        ]
    )
    rows: list[dict[str, Any]] = []
    for row in bq_client.query(sql, job_config=job_config).result():
        rows.append({
            "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
            "turn_id": getattr(row, "turn_id", None),
            "trace_id": getattr(row, "trace_id", None),
            "conversation_id": getattr(row, "conversation_id", None),
            "owner_email": getattr(row, "owner_email", None),
            "owner_role": getattr(row, "owner_role", None),
            "target_ticket_id": getattr(row, "target_ticket_id", None),
            "latency_ms": getattr(row, "latency_ms", None),
            "model": getattr(row, "model", None),
            "sources_count": _as_int(getattr(row, "sources_count", 0)),
            "tickets_count": _as_int(getattr(row, "tickets_count", 0)),
            "answer_chars": _as_int(getattr(row, "answer_chars", 0)),
            "stage_timings": _stage_timing_payload(getattr(row, "stage_timings_json", None)),
        })
    return rows


def get_quality_dashboard(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    event_type: str | None = None,
    quality: str | None = None,
    risk: str | None = None,
    validator_action: str | None = None,
    repair_action: str | None = None,
    stop_reason: str | None = None,
    model_surface: str | None = None,
    requester_email: str | None = None,
    reason_tag: str | None = None,
    limit: int = 25,
    firestore_limit: int = 500,
) -> dict[str, Any]:
    """Return a read-only Super Admin quality dashboard payload."""
    safe_limit = max(1, min(int(limit or 25), 100))
    range_info = resolve_quality_range(start_date, end_date)
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    provenance_events: list[dict[str, Any]] = []

    try:
        events = fetch_quality_review_events(range_info, limit=firestore_limit)
    except Exception as exc:
        warnings.append(f"Quality review telemetry is partially unavailable: {exc}")

    try:
        turns = fetch_turn_log_rows(range_info)
    except Exception as exc:
        warnings.append(f"Durable chat turn logs are partially unavailable: {exc}")

    try:
        provenance_events = fetch_system_provenance_events(range_info, limit=firestore_limit)
    except Exception:
        warnings.append("Failure provenance telemetry is partially unavailable.")

    filtered_events = apply_quality_filters(
        events,
        event_type=event_type or None,
        quality=quality or None,
        risk=risk or None,
        validator_action=validator_action or None,
        repair_action=repair_action or None,
        stop_reason=stop_reason or None,
        model_surface=model_surface or None,
        requester_email=requester_email or None,
    )
    payload = summarize_quality_telemetry(
        quality_events=filtered_events,
        turn_rows=turns,
        range_info=range_info,
        warnings=warnings,
        recent_limit=safe_limit,
        review_reason_tag=reason_tag,
        provenance_events=provenance_events,
    )
    payload["filters"] = {
        "event_type": event_type or None,
        "quality": quality or None,
        "risk": risk or None,
        "validator_action": validator_action or None,
        "repair_action": repair_action or None,
        "stop_reason": stop_reason or None,
        "model_surface": model_surface or None,
        "requester_email": requester_email or None,
        "reason_tag": reason_tag if reason_tag in REVIEW_REASON_TAGS else None,
        "limit": safe_limit,
    }
    payload["source_counts"] = {
        "quality_events_before_filters": len(events),
        "quality_events_after_filters": len(filtered_events),
        "turn_logs": len(turns),
        "provenance_events": len(provenance_events),
    }
    return payload


def payload_has_all_sections(payload: dict[str, Any]) -> bool:
    """Return whether a payload contains the public dashboard contract sections."""
    return all(section in payload for section in DASHBOARD_SECTIONS)


def contains_raw_text_keys(value: Any) -> bool:
    """Test helper: detect raw transcript keys in a returned dashboard payload."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in RAW_TEXT_KEYS:
                return True
            if contains_raw_text_keys(nested):
                return True
    if isinstance(value, list):
        return any(contains_raw_text_keys(item) for item in value)
    return False
