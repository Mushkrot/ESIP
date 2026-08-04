"""Super Admin configuration and dashboard helpers for AI dispatcher rollout."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from google.cloud import firestore as gcloud_firestore

from app.services import (
    ai_dispatcher_planner,
    ai_dispatcher_runtime,
    firestore,
    output_critic_shadow,
    quality_telemetry,
)

ALLOWED_PATCH_MODES = {
    ai_dispatcher_planner.DispatcherMode.DISABLED.value,
    ai_dispatcher_planner.DispatcherMode.SHADOW.value,
    ai_dispatcher_planner.DispatcherMode.ASSISTIVE.value,
    ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE.value,
}
BLOCKED_RUNTIME_MODES: set[str] = set()
ALLOWED_TASK_CLASSES = set(ai_dispatcher_runtime.DispatcherRuntimeConfig.eligible_task_classes)
ALLOWED_ASSISTIVE_TASK_CLASSES = set(ai_dispatcher_runtime.ALLOWED_ASSISTIVE_TASK_CLASSES)
ALLOWED_SHADOW_TASK_CLASSES = set(ai_dispatcher_runtime.ALLOWED_SHADOW_TASK_CLASSES)
PRIMARY_PLANNER_TASK_CLASSES = {
    ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
    ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS,
    ai_dispatcher_runtime.ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
    ai_dispatcher_runtime.ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH,
}
PRIMARY_ANSWER_AFFECTING_TASK_CLASSES = set(ai_dispatcher_runtime.PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
DEFAULT_RECENT_LIMIT = 50
MAX_RECENT_LIMIT = 200
MAX_DASHBOARD_DAYS = 31
FORBIDDEN_SAFETY_PATCH_FIELDS = {
    "output_critic_bounded_repair",
    "universal_output_critic_repair_allowed_kinds",
    "universal_output_critic_repair_enabled",
    "universal_output_critic_repair_min_confidence",
}
ALLOWED_CRITIC_SKIP_REASONS = {
    "disabled_by_config",
    "empty_visible_answer",
    "timeout_before_start",
}
ALLOWED_CRITIC_ERROR_CODES = {
    "contract_error",
    "cost_gate",
    "provider_or_runtime_error",
    "timeout",
}


class DispatcherAdminValidationError(ValueError):
    """Raised when an admin config patch is unsafe or unsupported."""


def default_config_payload() -> dict[str, Any]:
    cfg = ai_dispatcher_runtime.DispatcherRuntimeConfig()
    return {
        **cfg.to_trace_dict(),
        "enabled": cfg.mode is not ai_dispatcher_planner.DispatcherMode.DISABLED,
        "answer_affecting_allowed": _answer_affecting_allowed(cfg),
        "config_document": f"{ai_dispatcher_runtime.CONFIG_COLLECTION}/{ai_dispatcher_runtime.CONFIG_DOCUMENT}",
        "safe_defaults": True,
    }


def read_config(*, db: Any | None = None) -> dict[str, Any]:
    client = db or firestore.get_db()
    try:
        doc = client.collection(ai_dispatcher_runtime.CONFIG_COLLECTION).document(ai_dispatcher_runtime.CONFIG_DOCUMENT).get()
        raw = doc.to_dict() or {} if doc.exists else {}
    except Exception:
        raw = {}
    return _public_config(raw)


def update_config(patch: Mapping[str, Any], *, actor_email: str, db: Any | None = None) -> dict[str, Any]:
    client = db or firestore.get_db()
    current = read_config(db=client)
    update = validate_config_patch(patch, current=current, actor_email=actor_email)
    client.collection(ai_dispatcher_runtime.CONFIG_COLLECTION).document(ai_dispatcher_runtime.CONFIG_DOCUMENT).set(update, merge=True)
    return read_config(db=client)


def emergency_disable(*, actor_email: str, db: Any | None = None) -> dict[str, Any]:
    client = db or firestore.get_db()
    current = read_config(db=client)
    now = _now_iso()
    update = {
        "mode": ai_dispatcher_planner.DispatcherMode.DISABLED.value,
        "emergency_disabled": True,
        "emergency_disabled_at": now,
        "emergency_disabled_by": _clean_email(actor_email),
        "previous_mode": current.get("mode") or ai_dispatcher_planner.DispatcherMode.DISABLED.value,
        "updated_at": now,
        "updated_by": _clean_email(actor_email),
    }
    client.collection(ai_dispatcher_runtime.CONFIG_COLLECTION).document(ai_dispatcher_runtime.CONFIG_DOCUMENT).set(update, merge=True)
    return read_config(db=client)


def validate_config_patch(
    patch: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None = None,
    actor_email: str = "",
) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise DispatcherAdminValidationError("Config patch must be an object.")
    forbidden_fields = sorted(set(patch) & FORBIDDEN_SAFETY_PATCH_FIELDS)
    if forbidden_fields:
        raise DispatcherAdminValidationError(
            f"Safety-locked config field is not writable here: {', '.join(forbidden_fields)}"
        )
    current_config = dict(current or default_config_payload())
    out: dict[str, Any] = {}

    if bool(patch.get("emergency_disable")):
        out["mode"] = ai_dispatcher_planner.DispatcherMode.DISABLED.value
        out["emergency_disabled"] = True
        out["emergency_disabled_at"] = _now_iso()
        out["emergency_disabled_by"] = _clean_email(actor_email)

    if "mode" in patch:
        mode = str(patch.get("mode") or "").strip()
        if mode in BLOCKED_RUNTIME_MODES:
            raise DispatcherAdminValidationError(f"Mode {mode} is reserved for later rollout phases.")
        if mode not in ALLOWED_PATCH_MODES:
            raise DispatcherAdminValidationError("Unsupported dispatcher mode.")
        out["mode"] = mode
        if mode != ai_dispatcher_planner.DispatcherMode.DISABLED.value:
            out["emergency_disabled"] = False

    out["planner_daily_cap_cad"] = _optional_float(
        patch,
        "planner_daily_cap_cad",
        minimum=0.0,
        maximum=ai_dispatcher_runtime.MAX_PLANNER_DAILY_CAP_CAD,
        default=current_config.get("planner_daily_cap_cad", ai_dispatcher_runtime.DEFAULT_PLANNER_DAILY_CAP_CAD),
    )
    out["max_planner_calls_per_turn"] = _optional_int(
        patch,
        "max_planner_calls_per_turn",
        minimum=1,
        maximum=1,
        default=current_config.get("max_planner_calls_per_turn", 1),
    )
    out["max_tool_actions"] = _optional_int(
        patch,
        "max_tool_actions",
        minimum=1,
        maximum=4,
        default=current_config.get("max_tool_actions", 4),
    )
    out["max_explicit_ticket_ids"] = _optional_int(
        patch,
        "max_explicit_ticket_ids",
        minimum=1,
        maximum=75,
        default=current_config.get("max_explicit_ticket_ids", 75),
    )
    out["max_action_rows"] = _optional_int(
        patch,
        "max_action_rows",
        minimum=1,
        maximum=100,
        default=current_config.get("max_action_rows", 100),
    )
    out["max_output_tokens"] = _optional_int(
        patch,
        "max_output_tokens",
        minimum=256,
        maximum=2048,
        default=current_config.get("max_output_tokens", ai_dispatcher_runtime.DEFAULT_MAX_OUTPUT_TOKENS),
    )

    if "eligible_task_classes" in patch:
        classes = _string_list(patch.get("eligible_task_classes"), allowed=ALLOWED_TASK_CLASSES, field="eligible_task_classes")
        if not classes:
            raise DispatcherAdminValidationError("At least one eligible task class is required.")
        out["eligible_task_classes"] = classes

    if "assistive_task_classes" in patch:
        classes = _string_list(
            patch.get("assistive_task_classes"),
            allowed=ALLOWED_ASSISTIVE_TASK_CLASSES,
            field="assistive_task_classes",
        )
        out["assistive_task_classes"] = classes

    if "shadow_task_classes" in patch:
        out["shadow_task_classes"] = _string_list(
            patch.get("shadow_task_classes"),
            allowed=ALLOWED_SHADOW_TASK_CLASSES,
            field="shadow_task_classes",
        )

    effective_mode = out.get("mode", current_config.get("mode"))
    effective_eligible_classes = out.get("eligible_task_classes", current_config.get("eligible_task_classes") or [])
    effective_assistive_classes = out.get("assistive_task_classes", current_config.get("assistive_task_classes") or [])
    if effective_mode == ai_dispatcher_planner.DispatcherMode.ASSISTIVE.value and not effective_assistive_classes:
        raise DispatcherAdminValidationError("Assistive mode requires one approved assistive task class.")
    if effective_mode == ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE.value:
        eligible_set = set(effective_eligible_classes)
        assistive_set = set(effective_assistive_classes)
        if not assistive_set:
            raise DispatcherAdminValidationError("Primary mode requires one proven answer-affecting task class.")
        unsafe_eligible = sorted(eligible_set - PRIMARY_PLANNER_TASK_CLASSES)
        if unsafe_eligible:
            joined = ", ".join(unsafe_eligible)
            raise DispatcherAdminValidationError(f"Primary mode does not allow unproven eligible task classes: {joined}")
        unsafe_assistive = sorted(assistive_set - PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
        if unsafe_assistive:
            joined = ", ".join(unsafe_assistive)
            raise DispatcherAdminValidationError(f"Primary mode does not allow unproven answer-affecting task classes: {joined}")
    if (
        ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS in set(effective_assistive_classes)
        and ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE not in set(effective_assistive_classes)
    ):
        raise DispatcherAdminValidationError("Phase 5B bulk_ticket_ids requires prior_result_reference to stay enabled.")
    if ai_dispatcher_runtime.ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK in set(effective_assistive_classes):
        required = {
            ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
            ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS,
        }
        if not required.issubset(set(effective_assistive_classes)):
            raise DispatcherAdminValidationError(
                "Phase 5C challenge_fact_check requires prior_result_reference and bulk_ticket_ids to stay enabled."
            )
    if ai_dispatcher_runtime.ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH in set(effective_assistive_classes):
        required = {
            ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
            ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS,
            ai_dispatcher_runtime.ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
        }
        if not required.issubset(set(effective_assistive_classes)):
            raise DispatcherAdminValidationError(
                "Phase 6A source_family_mismatch requires Phase 5A, 5B, and 5C classes to stay enabled."
            )

    out["updated_at"] = _now_iso()
    out["updated_by"] = _clean_email(actor_email)
    return out


def get_dashboard(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = DEFAULT_RECENT_LIMIT,
    db: Any | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    range_info = quality_telemetry.resolve_quality_range(
        start_date,
        end_date,
        max_days=MAX_DASHBOARD_DAYS,
    )
    try:
        config = read_config(db=db)
    except Exception as exc:
        config = default_config_payload()
        warnings.append(f"Dispatcher config unavailable; using safe defaults: {exc}")

    traces: list[dict[str, Any]] = []
    try:
        traces = fetch_dispatcher_traces(range_info, limit=limit, db=db)
    except Exception as exc:
        warnings.append(f"Dispatcher trace telemetry is partially unavailable: {exc}")

    quality_events: list[dict[str, Any]] = []
    try:
        quality_events = quality_telemetry.fetch_quality_review_events(range_info, db=db)
    except Exception as exc:
        warnings.append(f"Quality review telemetry is partially unavailable: {exc}")

    turn_rows: list[dict[str, Any]] = []
    try:
        turn_rows = quality_telemetry.fetch_turn_log_rows(range_info)
    except Exception as exc:
        warnings.append(f"Durable turn latency telemetry is partially unavailable: {exc}")

    return summarize_dashboard(
        config=config,
        traces=traces,
        quality_events=quality_events,
        turn_rows=turn_rows,
        range_info=range_info,
        warnings=warnings,
        limit=limit,
    )


def fetch_dispatcher_traces(
    range_info: Mapping[str, Any],
    *,
    limit: int = MAX_RECENT_LIMIT,
    db: Any | None = None,
) -> list[dict[str, Any]]:
    """Read a bounded inclusive-Eastern dispatcher trace window."""
    safe_limit = max(1, min(int(limit or MAX_RECENT_LIMIT), MAX_RECENT_LIMIT))
    start_at = _utc_iso_boundary(range_info["start_at"])
    end_at = _utc_iso_boundary(range_info["end_at"])
    client = db or firestore.get_db()
    query = (
        client.collection("chat_answer_traces")
        .where("created_at", ">=", start_at)
        .where("created_at", "<", end_at)
        .order_by("created_at", direction=gcloud_firestore.Query.DESCENDING)
        .limit(safe_limit)
    )
    rows: list[dict[str, Any]] = []
    for doc in query.stream():
        compact = _compact_trace(doc.to_dict() or {})
        if compact:
            rows.append(compact)
    return rows


def fetch_recent_dispatcher_traces(*, limit: int = DEFAULT_RECENT_LIMIT, db: Any | None = None) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit or DEFAULT_RECENT_LIMIT), MAX_RECENT_LIMIT))
    client = db or firestore.get_db()
    query = (
        client.collection("chat_answer_traces")
        .order_by("created_at", direction=gcloud_firestore.Query.DESCENDING)
        .limit(safe_limit)
    )
    rows: list[dict[str, Any]] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        compact = _compact_trace(data)
        if compact:
            rows.append(compact)
    return rows


def summarize_dashboard(
    *,
    config: Mapping[str, Any],
    traces: list[dict[str, Any]],
    quality_events: list[dict[str, Any]] | None = None,
    turn_rows: list[dict[str, Any]] | None = None,
    range_info: Mapping[str, Any] | None = None,
    warnings: list[str] | None = None,
    limit: int = DEFAULT_RECENT_LIMIT,
) -> dict[str, Any]:
    safe_traces = _unique_safe_traces(traces)
    mode_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    latencies: list[float] = []
    estimated_costs: list[float] = []
    actual_costs: list[float] = []
    eligible = 0
    attempted = 0
    accepted = 0
    vetoed = 0
    answer_affecting = 0

    for row in safe_traces:
        mode = str(row.get("mode") or "unknown")
        mode_counts[mode] += 1
        if row.get("eligible"):
            eligible += 1
        if row.get("planner_attempted"):
            attempted += 1
        if row.get("accepted"):
            accepted += 1
        elif row.get("planner_attempted"):
            vetoed += 1
        if row.get("answer_affecting"):
            answer_affecting += 1
        fallback = str(row.get("fallback_reason") or "none")
        fallback_counts[fallback] += 1
        for reason in row.get("reason_codes") or []:
            reason_counts[str(reason)] += 1
        latency = _float(row.get("latency_ms"))
        if latency is not None:
            latencies.append(latency)
        est = _float(row.get("estimated_cost_cad"))
        if est is not None:
            estimated_costs.append(est)
        actual = _float(row.get("actual_cost_cad"))
        if actual is not None:
            actual_costs.append(actual)

    caveats = [
        "Dispatcher dashboard is Super Admin-only.",
        "Recent rows are compact telemetry only and do not include raw user messages or assistant answers.",
        "A zero count can mean no eligible dispatcher trace was logged in the recent trace window.",
    ]
    if not safe_traces:
        caveats.append("No dispatcher traces matched the recent window.")

    class_exposure, quality_join = _class_exposure_rows(
        safe_traces,
        quality_events=list(quality_events or []),
        turn_rows=list(turn_rows or []),
    )
    critic_exposure = _critic_exposure(safe_traces)
    turn_latencies = [
        latency
        for row in list(turn_rows or [])
        if (latency := _float(row.get("latency_ms"))) is not None
    ]

    return {
        "config": _public_config(config),
        "range": dict(range_info or {}),
        "summary": {
            "trace_count": len(safe_traces),
            "eligible_turns": eligible,
            "planner_attempted_turns": attempted,
            "accepted_plans": accepted,
            "vetoed_or_rejected_plans": vetoed,
            "answer_affecting_turns": answer_affecting,
            "partial": bool(warnings),
        },
        "mode_distribution": _bucket(mode_counts),
        "fallback_reasons": _bucket(fallback_counts),
        "reason_codes": _bucket(reason_counts),
        "latency": _percentiles(latencies),
        "turn_latency": _percentiles(turn_latencies),
        "class_exposure": class_exposure,
        "output_critic": critic_exposure,
        "quality_join": quality_join,
        "cost": {
            "estimated_total_cad": round(sum(estimated_costs), 6),
            "actual_total_cad": round(sum(actual_costs), 6),
            "estimated_count": len(estimated_costs),
            "actual_count": len(actual_costs),
        },
        "recent_traces": safe_traces[: max(1, min(int(limit or DEFAULT_RECENT_LIMIT), MAX_RECENT_LIMIT))],
        "warnings": list(warnings or []),
        "caveats": caveats,
    }


def payload_contains_raw_text_keys(value: Any) -> bool:
    raw_keys = {
        "messages",
        "latest_question",
        "routing_question",
        "assistant_answer",
        "final_answer",
        "answer",
        "full_text",
        "html_body",
        "prompt",
        "raw_prompt",
        "raw_response",
        "model_response",
        "raw_model_response",
    }
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in raw_keys:
                return True
            if payload_contains_raw_text_keys(nested):
                return True
    if isinstance(value, list):
        return any(payload_contains_raw_text_keys(item) for item in value)
    return False


def _public_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = ai_dispatcher_runtime.dispatcher_config_from_mapping(raw or {})
    data = default_config_payload()
    data.update(cfg.to_trace_dict())
    if raw:
        for key in ("updated_at", "updated_by", "emergency_disabled", "emergency_disabled_at", "emergency_disabled_by", "previous_mode"):
            if key in raw:
                data[key] = raw[key]
    data["enabled"] = data["mode"] != ai_dispatcher_planner.DispatcherMode.DISABLED.value
    data["answer_affecting_allowed"] = _answer_affecting_allowed(cfg)
    data["allowed_modes"] = sorted(ALLOWED_PATCH_MODES)
    data["blocked_modes"] = sorted(BLOCKED_RUNTIME_MODES)
    data["allowed_task_classes"] = sorted(ALLOWED_TASK_CLASSES)
    data["allowed_assistive_task_classes"] = sorted(ALLOWED_ASSISTIVE_TASK_CLASSES)
    data["allowed_shadow_task_classes"] = sorted(ALLOWED_SHADOW_TASK_CLASSES)
    data["primary_eligible_task_classes"] = sorted(PRIMARY_PLANNER_TASK_CLASSES)
    data["primary_answer_affecting_task_classes"] = sorted(PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
    return data


def _compact_trace(data: Mapping[str, Any]) -> dict[str, Any] | None:
    trace = data.get("ai_dispatcher")
    if not isinstance(trace, Mapping):
        return None
    critic = _compact_output_critic(data.get("output_critic_shadow"))
    return {
        "created_at": data.get("created_at"),
        "trace_id": data.get("trace_id"),
        "turn_id": data.get("turn_id"),
        "conversation_id": data.get("conversation_id"),
        "mode": trace.get("mode") or ((trace.get("config") or {}).get("mode") if isinstance(trace.get("config"), Mapping) else None),
        "eligible": bool(trace.get("eligible")),
        "planner_attempted": bool(trace.get("planner_attempted")),
        "planner_calls": int(trace.get("planner_calls") or 0),
        "accepted": bool(trace.get("accepted")),
        "answer_affecting": bool(trace.get("answer_affecting")),
        "assistive_task_class": trace.get("assistive_task_class") or "",
        "candidate_classes": [
            str(item)
            for item in trace.get("candidate_classes") or []
            if str(item) in ai_dispatcher_runtime.ALL_KNOWN_TASK_CLASSES
        ],
        "candidate_reason_codes": _compact_candidate_reasons(trace.get("candidate_reason_codes")),
        "fallback_reason": trace.get("fallback_reason") or "",
        "reason_codes": [str(item) for item in trace.get("reason_codes") or []],
        "latency_ms": trace.get("latency_ms"),
        "estimated_cost_cad": trace.get("estimated_cost_cad"),
        "actual_cost_cad": trace.get("actual_cost_cad"),
        "model_surface": trace.get("model_surface"),
        "output_critic": critic,
        "result_set_saved": bool(data.get("ai_dispatcher_result_set_memory_saved")),
        "result_set_comparison": bool(data.get("ai_dispatcher_result_set_comparison")),
    }


def _safe_recent_trace(row: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "created_at",
        "trace_id",
        "turn_id",
        "conversation_id",
        "mode",
        "eligible",
        "planner_attempted",
        "planner_calls",
        "accepted",
        "answer_affecting",
        "assistive_task_class",
        "candidate_classes",
        "candidate_reason_codes",
        "fallback_reason",
        "reason_codes",
        "latency_ms",
        "estimated_cost_cad",
        "actual_cost_cad",
        "model_surface",
        "output_critic",
        "result_set_saved",
        "result_set_comparison",
    }
    safe = {key: row.get(key) for key in allowed if key in row}
    if "output_critic" in safe:
        safe["output_critic"] = _compact_output_critic(safe["output_critic"])
    return safe


def _unique_safe_traces(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, trace in enumerate(traces):
        safe = _safe_recent_trace(trace)
        identity = str(safe.get("trace_id") or f"row:{index}")
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(safe)
    return rows


def _compact_output_critic(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    risk_level = str(value.get("risk_level") or "none").strip().lower()
    if risk_level not in output_critic_shadow.ALLOWED_RISK_LEVELS:
        risk_level = "unknown"
    risk_codes = [
        str(code)
        for code in value.get("risk_codes") or []
        if str(code) in output_critic_shadow.ALLOWED_RISK_CODES
    ]
    skipped_reason = str(value.get("skipped_reason") or "").strip().lower().replace(" ", "_")
    if skipped_reason and skipped_reason not in ALLOWED_CRITIC_SKIP_REASONS:
        skipped_reason = "other"
    error_code = str(value.get("error_code") or "").strip()
    if error_code not in ALLOWED_CRITIC_ERROR_CODES:
        error_code = _critic_error_code(value.get("error"), risk_codes)
    compact = {
        "emitted": bool(value.get("emitted")),
        "attempted": bool(value.get("attempted")),
        "answer_affecting": bool(value.get("answer_affecting")),
        "risk_level": risk_level,
        "risk_codes": list(dict.fromkeys(risk_codes)),
        "skipped_reason": skipped_reason,
        "error_code": error_code,
        "latency_ms": _nonnegative_float(value.get("latency_ms")),
        "estimated_cost_cad": _nonnegative_float(value.get("estimated_cost_cad")),
        "reserved_cost_cad": _nonnegative_float(value.get("reserved_cost_cad")),
        "actual_cost_cad": _nonnegative_float(value.get("actual_cost_cad")),
    }
    return {key: item for key, item in compact.items() if item not in (None, "", [])}


def _critic_error_code(value: Any, risk_codes: list[str]) -> str:
    text = str(value or "").strip().casefold()
    if not text and "output_critic_error" not in risk_codes:
        return ""
    if "timeout" in text:
        return "timeout"
    if any(term in text for term in ("cost", "budget", "reserve")):
        return "cost_gate"
    if any(term in text for term in ("json", "schema", "response was not")):
        return "contract_error"
    return "provider_or_runtime_error"


def _critic_exposure(traces: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = 0
    skipped = 0
    emitted = 0
    errors = 0
    answer_affecting = 0
    risk_levels: Counter[str] = Counter()
    risk_codes: Counter[str] = Counter()
    skip_reasons: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    latencies: list[float] = []
    costs: dict[str, list[float]] = {
        "estimated_cost_cad": [],
        "reserved_cost_cad": [],
        "actual_cost_cad": [],
    }

    observed = 0
    for row in traces:
        critic = row.get("output_critic")
        if not isinstance(critic, Mapping) or not critic:
            continue
        observed += 1
        emitted += int(bool(critic.get("emitted")))
        if critic.get("attempted"):
            attempted += 1
        else:
            skipped += 1
        answer_affecting += int(bool(critic.get("answer_affecting")))
        risk_levels[str(critic.get("risk_level") or "unknown")] += 1
        for code in critic.get("risk_codes") or []:
            risk_codes[str(code)] += 1
        if critic.get("skipped_reason"):
            skip_reasons[str(critic.get("skipped_reason"))] += 1
        if critic.get("error_code"):
            errors += 1
            error_codes[str(critic.get("error_code"))] += 1
        latency = _nonnegative_float(critic.get("latency_ms"))
        if latency is not None:
            latencies.append(latency)
        for field in costs:
            cost = _nonnegative_float(critic.get(field))
            if cost is not None:
                costs[field].append(cost)

    return {
        "observed_turns": observed,
        "emitted_turns": emitted,
        "attempted_turns": attempted,
        "skipped_turns": skipped,
        "error_turns": errors,
        "answer_affecting_turns": answer_affecting,
        "risk_levels": _bucket(risk_levels),
        "risk_codes": _bucket(risk_codes),
        "skip_reasons": _bucket(skip_reasons),
        "error_codes": _bucket(error_codes),
        "latency": _percentiles(latencies),
        "cost": {
            "estimated_total_cad": round(sum(costs["estimated_cost_cad"]), 6),
            "reserved_total_cad": round(sum(costs["reserved_cost_cad"]), 6),
            "actual_total_cad": round(sum(costs["actual_cost_cad"]), 6),
            "estimated_count": len(costs["estimated_cost_cad"]),
            "reserved_count": len(costs["reserved_cost_cad"]),
            "actual_count": len(costs["actual_cost_cad"]),
        },
    }


def _compact_candidate_reasons(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        task_class: [str(reason)[:80] for reason in reasons[:12] if str(reason).strip()]
        for raw_class, raw_reasons in value.items()
        if (task_class := str(raw_class)) in ai_dispatcher_runtime.ALL_KNOWN_TASK_CLASSES
        for reasons in [list(raw_reasons) if isinstance(raw_reasons, (list, tuple)) else []]
    }


def _class_exposure_rows(
    traces: list[dict[str, Any]],
    *,
    quality_events: list[dict[str, Any]],
    turn_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    quality_by_trace: dict[str, set[str]] = {}
    quality_event_count = 0
    for event in quality_events:
        trace_id = str(event.get("trace_id") or "").strip()
        if not trace_id:
            continue
        quality_event_count += 1
        quality = str(
            event.get("validator_quality")
            or event.get("quality")
            or event.get("review_quality")
            or "unknown"
        ).strip() or "unknown"
        quality_by_trace.setdefault(trace_id, set()).add(quality)

    turn_latency_by_trace: dict[str, float] = {}
    for row in turn_rows:
        trace_id = str(row.get("trace_id") or "").strip()
        latency = _float(row.get("latency_ms"))
        if trace_id and latency is not None and trace_id not in turn_latency_by_trace:
            turn_latency_by_trace[trace_id] = latency

    stats: dict[str, dict[str, Any]] = {
        task_class: {
            "task_class": task_class,
            "candidate_turns": 0,
            "planner_attempted_turns": 0,
            "accepted_plans": 0,
            "rejected_plans": 0,
            "suppressed_or_locked_turns": 0,
            "answer_affecting_turns": 0,
            "quality_trace_ids": set(),
            "quality_counts": Counter(),
            "reason_counts": Counter(),
            "planner_latencies": [],
            "turn_latencies": [],
        }
        for task_class in ai_dispatcher_runtime.ALL_KNOWN_TASK_CLASSES
    }

    dispatcher_trace_ids: set[str] = set()
    for row in traces:
        trace_id = str(row.get("trace_id") or "").strip()
        if trace_id:
            dispatcher_trace_ids.add(trace_id)
        candidate_classes = [
            str(item)
            for item in row.get("candidate_classes") or []
            if str(item) in stats
        ]
        if not candidate_classes:
            inferred = [
                str(reason)
                for reason in row.get("reason_codes") or []
                if str(reason) in stats
            ]
            selected = str(row.get("assistive_task_class") or "")
            candidate_classes = list(dict.fromkeys(inferred + ([selected] if selected in stats else [])))

        planner_latency = _float(row.get("latency_ms"))
        candidate_reasons = row.get("candidate_reason_codes") if isinstance(row.get("candidate_reason_codes"), Mapping) else {}
        for task_class in candidate_classes:
            item = stats[task_class]
            item["candidate_turns"] += 1
            if row.get("planner_attempted"):
                item["planner_attempted_turns"] += 1
            if row.get("accepted"):
                item["accepted_plans"] += 1
            elif row.get("planner_attempted"):
                item["rejected_plans"] += 1
            if row.get("accepted") and not (
                row.get("answer_affecting") and row.get("assistive_task_class") == task_class
            ):
                item["suppressed_or_locked_turns"] += 1
            if row.get("answer_affecting") and row.get("assistive_task_class") == task_class:
                item["answer_affecting_turns"] += 1
            for reason in list(candidate_reasons.get(task_class) or []) + list(row.get("reason_codes") or []):
                item["reason_counts"][str(reason)] += 1
            if row.get("planner_attempted") and planner_latency is not None:
                item["planner_latencies"].append(planner_latency)
            if trace_id in turn_latency_by_trace:
                item["turn_latencies"].append(turn_latency_by_trace[trace_id])
            if trace_id in quality_by_trace:
                item["quality_trace_ids"].add(trace_id)
                for quality in quality_by_trace[trace_id]:
                    item["quality_counts"][quality] += 1

    rows: list[dict[str, Any]] = []
    for task_class in ai_dispatcher_runtime.ALL_KNOWN_TASK_CLASSES:
        item = stats[task_class]
        rows.append({
            "task_class": task_class,
            "candidate_turns": item["candidate_turns"],
            "planner_attempted_turns": item["planner_attempted_turns"],
            "accepted_plans": item["accepted_plans"],
            "rejected_plans": item["rejected_plans"],
            "suppressed_or_locked_turns": item["suppressed_or_locked_turns"],
            "answer_affecting_turns": item["answer_affecting_turns"],
            "quality_turns": len(item["quality_trace_ids"]),
            "quality_distribution": _bucket(item["quality_counts"]),
            "reason_codes": _bucket(item["reason_counts"]),
            "planner_latency": _percentiles(item["planner_latencies"]),
            "turn_latency": _percentiles(item["turn_latencies"]),
        })

    quality_trace_ids = set(quality_by_trace)
    return rows, {
        "quality_event_count": quality_event_count,
        "unique_quality_answer_traces": len(quality_trace_ids),
        "duplicate_quality_events": max(0, quality_event_count - len(quality_trace_ids)),
        "matched_dispatcher_traces": len(dispatcher_trace_ids & quality_trace_ids),
    }


def _answer_affecting_allowed(cfg: ai_dispatcher_runtime.DispatcherRuntimeConfig) -> bool:
    return (
        cfg.mode
        in {
            ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
            ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE,
        }
        and bool(cfg.assistive_task_classes)
    )


def _optional_int(patch: Mapping[str, Any], key: str, *, minimum: int, maximum: int, default: Any) -> int:
    if key not in patch:
        try:
            return int(default)
        except (TypeError, ValueError):
            return minimum
    try:
        value = int(patch.get(key))
    except (TypeError, ValueError):
        raise DispatcherAdminValidationError(f"{key} must be an integer.") from None
    if value < minimum or value > maximum:
        raise DispatcherAdminValidationError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _optional_float(patch: Mapping[str, Any], key: str, *, minimum: float, maximum: float, default: Any) -> float:
    if key not in patch:
        try:
            return float(default)
        except (TypeError, ValueError):
            return minimum
    try:
        value = float(patch.get(key))
    except (TypeError, ValueError):
        raise DispatcherAdminValidationError(f"{key} must be numeric.") from None
    if value < minimum or value > maximum:
        raise DispatcherAdminValidationError(f"{key} must be between {minimum} and {maximum}.")
    return round(value, 4)


def _string_list(raw: Any, *, allowed: set[str], field: str) -> list[str]:
    if not isinstance(raw, list):
        raise DispatcherAdminValidationError(f"{field} must be a list.")
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        if value not in allowed:
            raise DispatcherAdminValidationError(f"Unsupported {field} value: {value}")
        seen.add(value)
        values.append(value)
    return values


def _bucket(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": key, "count": count} for key, count in counter.most_common()]


def _percentiles(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_ms": None, "p90_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": round(_percentile(ordered, 0.5), 1),
        "p90_ms": round(_percentile(ordered, 0.9), 1),
        "max_ms": round(ordered[-1], 1),
    }


def _percentile(ordered: list[float], pct: float) -> float:
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[index]


def _float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_float(value: Any) -> float | None:
    number = _float(value)
    if number is None or number < 0:
        return None
    return number


def _clean_email(value: str) -> str:
    return str(value or "").strip().lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_iso_boundary(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


__all__ = [
    "DispatcherAdminValidationError",
    "default_config_payload",
    "emergency_disable",
    "get_dashboard",
    "fetch_dispatcher_traces",
    "payload_contains_raw_text_keys",
    "read_config",
    "summarize_dashboard",
    "update_config",
    "validate_config_patch",
]
