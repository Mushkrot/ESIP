"""Runtime planner for the bounded Universal Chat AI dispatcher.

The runtime boundary is intentionally conservative. In this phase it can call
the planner for private telemetry, compile the proposal through deterministic
policy, and return compact trace metadata. Answer-affecting paths stay gated by
deterministic class validators; planner output never directly writes the final
Universal Chat answer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import vertexai
from google.oauth2 import service_account
from vertexai.generative_models import GenerationConfig, GenerativeModel

from app.config import settings
from app.services import ai_dispatcher_planner
from app.services.cost_tracker import (
    CallType,
    CostDivergenceError,
    CostGateError,
    calc_cost,
    check_cost_gate,
    check_divergence,
    estimate_tokens_from_text,
    get_gate_reserve,
    new_correlation_id,
    record_estimate,
    record_usage,
)

logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo("America/New_York")
CONFIG_COLLECTION = "system"
CONFIG_DOCUMENT = "ai_dispatcher_config"
DEFAULT_PLANNER_DAILY_CAP_CAD = 2.0
MAX_PLANNER_DAILY_CAP_CAD = 2.0
DEFAULT_MAX_OUTPUT_TOKENS = 1024
ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE = "prior_result_reference"
ASSISTIVE_CLASS_BULK_TICKET_IDS = "bulk_ticket_ids"
ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK = "challenge_fact_check"
ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH = "source_family_mismatch"
ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE = "followup_date_scope"
ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP = "ticket_resolution_lookup"
ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT = "followup_result_set_refinement"
ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE = "followup_ticket_reference"
DISPATCHER_CLASS_MULTI_STORE_TICKET_SEARCH = "multi_store_ticket_search"
ALLOWED_ASSISTIVE_TASK_CLASSES = (
    ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
    ASSISTIVE_CLASS_BULK_TICKET_IDS,
    ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
    ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH,
    ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE,
    ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP,
    ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT,
    ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE,
)
ALLOWED_SHADOW_TASK_CLASSES = (
    ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT,
    ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE,
)
ALL_KNOWN_TASK_CLASSES = (
    ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
    ASSISTIVE_CLASS_BULK_TICKET_IDS,
    ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
    DISPATCHER_CLASS_MULTI_STORE_TICKET_SEARCH,
    ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH,
    ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE,
    ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP,
    ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT,
    ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE,
)
PRIMARY_ANSWER_AFFECTING_TASK_CLASSES = (
    ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
    ASSISTIVE_CLASS_BULK_TICKET_IDS,
    ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
    ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH,
    ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE,
)

_model: GenerativeModel | None = None


@dataclass(frozen=True)
class DispatcherRuntimeConfig:
    mode: ai_dispatcher_planner.DispatcherMode = ai_dispatcher_planner.DispatcherMode.DISABLED
    planner_daily_cap_cad: float = DEFAULT_PLANNER_DAILY_CAP_CAD
    max_planner_calls_per_turn: int = 1
    max_tool_actions: int = 4
    max_explicit_ticket_ids: int = 75
    max_action_rows: int = 100
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    eligible_task_classes: tuple[str, ...] = (
        "prior_result_reference",
        "bulk_ticket_ids",
        "challenge_fact_check",
        "multi_store_ticket_search",
        "source_family_mismatch",
    )
    assistive_task_classes: tuple[str, ...] = ()
    shadow_task_classes: tuple[str, ...] = ()

    def limits(self) -> ai_dispatcher_planner.DispatcherPolicyLimits:
        return ai_dispatcher_planner.DispatcherPolicyLimits(
            max_planner_calls=max(1, int(self.max_planner_calls_per_turn or 1)),
            max_tool_actions=max(1, int(self.max_tool_actions or 1)),
            max_explicit_ticket_ids=max(1, int(self.max_explicit_ticket_ids or 1)),
            max_action_rows=max(1, int(self.max_action_rows or 1)),
        )

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "planner_daily_cap_cad": self.planner_daily_cap_cad,
            "max_planner_calls_per_turn": self.max_planner_calls_per_turn,
            "max_tool_actions": self.max_tool_actions,
            "max_explicit_ticket_ids": self.max_explicit_ticket_ids,
            "max_action_rows": self.max_action_rows,
            "max_output_tokens": self.max_output_tokens,
            "eligible_task_classes": list(self.eligible_task_classes),
            "assistive_task_classes": list(self.assistive_task_classes),
            "shadow_task_classes": list(self.shadow_task_classes),
        }


@dataclass(frozen=True)
class DispatcherRuntimeResult:
    config: DispatcherRuntimeConfig
    eligible: bool = False
    planner_attempted: bool = False
    planner_calls: int = 0
    answer_affecting: bool = False
    assistive_task_class: str = ""
    accepted: bool = False
    fallback_reason: str = ""
    reason_codes: tuple[str, ...] = ()
    estimated_cost_cad: float | None = None
    reserved_cost_cad: float | None = None
    actual_cost_cad: float | None = None
    latency_ms: int | None = None
    prompt_tokens_estimate: int | None = None
    output_tokens_limit: int = DEFAULT_MAX_OUTPUT_TOKENS
    thinking_budget: int = 0
    correlation_id: str = ""
    compile_decision: Mapping[str, Any] = field(default_factory=dict)
    issues: tuple[Mapping[str, Any], ...] = ()
    cap_details: tuple[Mapping[str, Any], ...] = ()
    candidate_classes: tuple[str, ...] = ()
    candidate_reason_codes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode.value,
            "config": self.config.to_trace_dict(),
            "eligible": self.eligible,
            "planner_attempted": self.planner_attempted,
            "planner_calls": self.planner_calls,
            "answer_affecting": self.answer_affecting,
            "assistive_task_class": self.assistive_task_class,
            "accepted": self.accepted,
            "fallback_reason": self.fallback_reason,
            "reason_codes": list(self.reason_codes),
            "estimated_cost_cad": self.estimated_cost_cad,
            "reserved_cost_cad": self.reserved_cost_cad,
            "actual_cost_cad": self.actual_cost_cad,
            "latency_ms": self.latency_ms,
            "prompt_tokens_estimate": self.prompt_tokens_estimate,
            "output_tokens_limit": self.output_tokens_limit,
            "thinking_budget": self.thinking_budget,
            "correlation_id": self.correlation_id,
            "compile_decision": dict(self.compile_decision),
            "issues": [dict(issue) for issue in self.issues],
            "cap_details": [dict(detail) for detail in self.cap_details],
            "candidate_classes": list(self.candidate_classes),
            "candidate_reason_codes": {
                str(task_class): list(reason_codes)
                for task_class, reason_codes in self.candidate_reason_codes.items()
                if str(task_class) in ALL_KNOWN_TASK_CLASSES
            },
            "model_surface": CallType.UNIVERSAL_DISPATCHER_PLANNER.value,
        }


@dataclass(frozen=True)
class DispatcherCandidateObservation:
    """Raw-text-free deterministic class opportunities for private telemetry."""

    classes: tuple[str, ...] = ()
    reason_codes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_trace_dict(self) -> dict[str, Any]:
        return {
            "candidate_classes": list(self.classes),
            "candidate_reason_codes": {
                task_class: list(self.reason_codes.get(task_class) or ())
                for task_class in self.classes
            },
        }


def get_dispatcher_config() -> DispatcherRuntimeConfig:
    """Read dispatcher config from Firestore, failing closed to disabled."""
    try:
        from app.services import firestore  # noqa: PLC0415

        doc = firestore.get_db().collection(CONFIG_COLLECTION).document(CONFIG_DOCUMENT).get()
        data = doc.to_dict() or {} if doc.exists else {}
    except Exception as exc:
        logger.warning("ai_dispatcher: config read failed; using disabled default: %s", exc)
        return DispatcherRuntimeConfig()
    return dispatcher_config_from_mapping(data)


def dispatcher_config_from_mapping(raw: Mapping[str, Any] | None) -> DispatcherRuntimeConfig:
    data = raw or {}
    mode = _coerce_mode(data.get("mode"))
    return DispatcherRuntimeConfig(
        mode=mode,
        planner_daily_cap_cad=_float_between(
            data.get("planner_daily_cap_cad"),
            0.0,
            MAX_PLANNER_DAILY_CAP_CAD,
            DEFAULT_PLANNER_DAILY_CAP_CAD,
        ),
        max_planner_calls_per_turn=_int_between(data.get("max_planner_calls_per_turn"), 1, 1, 1),
        max_tool_actions=_int_between(data.get("max_tool_actions"), 1, 4, 4),
        max_explicit_ticket_ids=_int_between(data.get("max_explicit_ticket_ids"), 1, 75, 75),
        max_action_rows=_int_between(data.get("max_action_rows"), 1, 100, 100),
        max_output_tokens=_int_between(data.get("max_output_tokens"), 256, 2048, DEFAULT_MAX_OUTPUT_TOKENS),
        eligible_task_classes=_clean_str_tuple(data.get("eligible_task_classes"))
        or DispatcherRuntimeConfig.eligible_task_classes,
        assistive_task_classes=_clean_assistive_task_classes(data.get("assistive_task_classes")),
        shadow_task_classes=_clean_shadow_task_classes(data.get("shadow_task_classes")),
    )


def followup_date_scope_assist_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether the deterministic Phase 6D assistive path may affect answers."""
    cfg = config or get_dispatcher_config()
    return _answer_affecting_class_enabled(cfg, ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE)


def ticket_resolution_lookup_assist_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether the deterministic Phase 6E assistive path may affect answers."""
    cfg = config or get_dispatcher_config()
    return (
        cfg.mode is ai_dispatcher_planner.DispatcherMode.ASSISTIVE
        and ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP in set(cfg.assistive_task_classes)
    )


def followup_result_set_refinement_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether the deterministic Phase 6H result-set refinement path may affect answers."""
    cfg = config or get_dispatcher_config()
    return (
        cfg.mode is ai_dispatcher_planner.DispatcherMode.ASSISTIVE
        and ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT in set(cfg.assistive_task_classes)
    )


def followup_result_set_refinement_shadow_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether saved result-set refinement may emit private shadow metadata."""
    cfg = config or get_dispatcher_config()
    return (
        cfg.mode is not ai_dispatcher_planner.DispatcherMode.DISABLED
        and ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT in set(cfg.shadow_task_classes)
    )


def followup_ticket_reference_shadow_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether saved-set ticket references may emit private shadow metadata."""
    cfg = config or get_dispatcher_config()
    return (
        cfg.mode is not ai_dispatcher_planner.DispatcherMode.DISABLED
        and ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE in set(cfg.shadow_task_classes)
    )


def followup_ticket_reference_enabled(config: DispatcherRuntimeConfig | None = None) -> bool:
    """Return whether the deterministic Phase 6H ticket-reference path may affect answers."""
    cfg = config or get_dispatcher_config()
    return (
        cfg.mode is ai_dispatcher_planner.DispatcherMode.ASSISTIVE
        and ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE in set(cfg.assistive_task_classes)
    )


def run_dispatcher_shadow(
    *,
    latest_question: str,
    routing_question: str,
    messages: list[Mapping[str, Any]],
    semantic_frame: Mapping[str, Any],
    conversation_id: str,
    turn_id: str,
    trace_id: str,
    owner_role: str,
    explicit_store_ids: list[str] | tuple[str, ...] = (),
    explicit_ticket_id: int | None = None,
    previous_assistant_text: str = "",
    config: DispatcherRuntimeConfig | None = None,
) -> DispatcherRuntimeResult:
    """Run the dispatcher planner when eligible.

    Shadow mode is metadata only. Assistive mode remains class-gated: Phase 5A
    can mark prior result-set comparison as answer-affecting; Phase 5B can mark
    bounded bulk ticket-ID lookup as answer-affecting only in prior/comparison
    context. The planner still does not directly execute tools.
    """
    deterministic = ai_dispatcher_planner.DispatcherDeterministicEntities(
        store_ids=tuple(dict.fromkeys(str(item).strip() for item in explicit_store_ids if str(item).strip())),
        ticket_ids=_deterministic_ticket_ids(latest_question, routing_question, explicit_ticket_id),
        reason="explicit_entities_from_universal_chat",
    )
    try:
        candidates = detect_dispatcher_candidates(
            latest_question=latest_question,
            routing_question=routing_question,
            messages=messages,
            semantic_frame=semantic_frame,
            deterministic_entities=deterministic,
            explicit_ticket_id=explicit_ticket_id,
            previous_assistant_text=previous_assistant_text,
        )
    except Exception as exc:
        logger.warning("ai_dispatcher: candidate observation failed open: %s", type(exc).__name__)
        candidates = DispatcherCandidateObservation()

    cfg = config or get_dispatcher_config()
    if cfg.mode is ai_dispatcher_planner.DispatcherMode.DISABLED:
        return DispatcherRuntimeResult(
            config=cfg,
            eligible=False,
            fallback_reason="dispatcher_disabled",
            reason_codes=("dispatcher_disabled",),
            candidate_classes=candidates.classes,
            candidate_reason_codes=candidates.reason_codes,
        )
    eligible, eligibility_reasons = dispatcher_eligible(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        deterministic_entities=deterministic,
        previous_assistant_text=previous_assistant_text,
        enabled_classes=cfg.eligible_task_classes,
    )
    if not eligible:
        return DispatcherRuntimeResult(
            config=cfg,
            eligible=False,
            fallback_reason="not_eligible",
            reason_codes=("not_eligible",),
            candidate_classes=candidates.classes,
            candidate_reason_codes=candidates.reason_codes,
        )

    started = time.monotonic()
    prompt = build_planner_prompt(
        latest_question=latest_question,
        routing_question=routing_question,
        messages=messages,
        semantic_frame=semantic_frame,
        conversation_id=conversation_id,
        turn_id=turn_id,
        trace_id=trace_id,
        owner_role=owner_role,
        deterministic_entities=deterministic,
        eligibility_reasons=eligibility_reasons,
    )
    estimated_tokens = estimate_tokens_from_text(prompt)
    est_output_tokens = cfg.max_output_tokens
    estimated_cost = calc_cost(
        estimated_tokens,
        est_output_tokens,
        tokens_thinking=0,
        call_type=CallType.UNIVERSAL_DISPATCHER_PLANNER,
    )
    reserved = get_gate_reserve(CallType.UNIVERSAL_DISPATCHER_PLANNER, estimated_cost)
    correlation_id = new_correlation_id()

    try:
        if _today_dispatcher_spent_cad() + reserved > cfg.planner_daily_cap_cad:
            return DispatcherRuntimeResult(
                config=cfg,
                eligible=True,
                fallback_reason="dispatcher_daily_cap",
                reason_codes=("dispatcher_daily_cap",),
                estimated_cost_cad=estimated_cost,
                reserved_cost_cad=reserved,
                prompt_tokens_estimate=estimated_tokens,
                output_tokens_limit=cfg.max_output_tokens,
                correlation_id=correlation_id,
                candidate_classes=candidates.classes,
                candidate_reason_codes=candidates.reason_codes,
            )
        check_cost_gate(CallType.UNIVERSAL_DISPATCHER_PLANNER, reserved, fail_closed=False)
        record_estimate(
            CallType.UNIVERSAL_DISPATCHER_PLANNER,
            estimated_cost,
            correlation_id,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=est_output_tokens,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
            notes="Universal Chat AI dispatcher planner shadow call",
        )
        response = _get_model().generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict(
                {
                    "temperature": 0.0,
                    "max_output_tokens": cfg.max_output_tokens,
                    "response_mime_type": "application/json",
                    "response_schema": ai_dispatcher_planner.DISPATCHER_PLAN_RESPONSE_SCHEMA,
                    "thinking_config": {"thinking_budget": 0},
                }
            ),
        )
        actual_cost = _record_actual_usage(response, correlation_id, estimated_cost)
        decision = ai_dispatcher_planner.compile_dispatcher_payload(
            getattr(response, "text", "") or "",
            mode=_compile_mode_for_runtime(cfg.mode),
            limits=cfg.limits(),
            deterministic_entities=deterministic,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        assistive_task_class = _select_assistive_answer_class(
            cfg=cfg,
            decision=decision,
            eligibility_reasons=eligibility_reasons,
            latest_question=latest_question,
            routing_question=routing_question,
            semantic_frame=semantic_frame,
            previous_assistant_text=previous_assistant_text,
        )
        assistive_reason_codes: tuple[str, ...] = ()
        if assistive_task_class == ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE:
            assistive_reason_codes = ("assistive_prior_result_comparison",)
        elif assistive_task_class == ASSISTIVE_CLASS_BULK_TICKET_IDS:
            assistive_reason_codes = ("assistive_bulk_ticket_lookup",)
        elif assistive_task_class == ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK:
            assistive_reason_codes = ("assistive_challenge_fact_check",)
        elif assistive_task_class == ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH:
            assistive_reason_codes = ("assistive_source_family_mismatch",)
        elif cfg.mode is ai_dispatcher_planner.DispatcherMode.ASSISTIVE and decision.accepted:
            assistive_reason_codes = ("assistive_class_locked",)
        elif cfg.mode is ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE and decision.accepted:
            assistive_reason_codes = ("primary_class_locked",)
        reason_codes = tuple(dict.fromkeys(tuple(decision.reason_codes) + eligibility_reasons + assistive_reason_codes))
        return DispatcherRuntimeResult(
            config=cfg,
            eligible=True,
            planner_attempted=True,
            planner_calls=1,
            answer_affecting=bool(assistive_task_class),
            assistive_task_class=assistive_task_class,
            accepted=decision.accepted,
            fallback_reason="" if decision.accepted else "planner_policy_rejected",
            reason_codes=reason_codes,
            estimated_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
            actual_cost_cad=actual_cost,
            latency_ms=latency_ms,
            prompt_tokens_estimate=estimated_tokens,
            output_tokens_limit=cfg.max_output_tokens,
            thinking_budget=0,
            correlation_id=correlation_id,
            compile_decision=decision.to_dict(),
            issues=tuple(issue.to_dict() for issue in decision.issues),
            cap_details=tuple(detail.to_dict() for detail in decision.cap_details),
            candidate_classes=candidates.classes,
            candidate_reason_codes=candidates.reason_codes,
        )
    except (CostGateError, CostDivergenceError) as exc:
        return _fallback_result(
            cfg,
            "cost_blocked",
            exc,
            estimated_cost,
            reserved,
            estimated_tokens,
            correlation_id,
            started,
            candidates,
        )
    except Exception as exc:
        logger.warning("ai_dispatcher: shadow planner failed trace=%s: %s", trace_id, exc)
        return _fallback_result(
            cfg,
            "planner_error",
            exc,
            estimated_cost,
            reserved,
            estimated_tokens,
            correlation_id,
            started,
            candidates,
        )


def detect_dispatcher_candidates(
    *,
    latest_question: str,
    routing_question: str,
    messages: list[Mapping[str, Any]],
    semantic_frame: Mapping[str, Any],
    deterministic_entities: ai_dispatcher_planner.DispatcherDeterministicEntities,
    explicit_ticket_id: int | None = None,
    previous_assistant_text: str = "",
) -> DispatcherCandidateObservation:
    """Detect every known class opportunity without model, storage, or tool access."""
    reasons_by_class: dict[str, tuple[str, ...]] = {}
    _, base_reasons = dispatcher_eligible(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        deterministic_entities=deterministic_entities,
        previous_assistant_text=previous_assistant_text,
        enabled_classes=(
            ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE,
            ASSISTIVE_CLASS_BULK_TICKET_IDS,
            ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK,
            DISPATCHER_CLASS_MULTI_STORE_TICKET_SEARCH,
            ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH,
        ),
    )
    for reason in base_reasons:
        if reason in ALL_KNOWN_TASK_CLASSES:
            reasons_by_class[reason] = (reason,)

    from app.services import (  # noqa: PLC0415
        followup_result_set_assist,
        followup_understanding_shadow,
        ticket_resolution_lookup_assist,
    )

    followup = followup_understanding_shadow.build_followup_understanding_shadow(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=dict(semantic_frame or {}),
        previous_assistant_text=previous_assistant_text,
        messages=[dict(message) for message in messages],
    )
    if followup.emitted and followup.month_windows:
        reasons_by_class[ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE] = tuple(followup.reason_codes)

    resolution = ticket_resolution_lookup_assist.build_ticket_resolution_lookup_assist(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=explicit_ticket_id,
        open_ticket_id=explicit_ticket_id,
    )
    if resolution.accepted:
        reasons_by_class[ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP] = tuple(resolution.reason_codes)

    result_set_candidate = followup_result_set_assist.detect_followup_result_set_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
    )
    if result_set_candidate.is_candidate:
        result_reasons = tuple(result_set_candidate.reason_codes)
        if any(
            reason in {
                "ticket_reference_followup",
                "ambiguous_ticket_reference",
                "missing_target_ticket",
                "ordinal_out_of_range",
            }
            for reason in result_reasons
        ):
            reasons_by_class[ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE] = result_reasons
        else:
            reasons_by_class[ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT] = result_reasons

    classes = tuple(task_class for task_class in ALL_KNOWN_TASK_CLASSES if task_class in reasons_by_class)
    return DispatcherCandidateObservation(classes=classes, reason_codes=reasons_by_class)


def dispatcher_eligible(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any],
    deterministic_entities: ai_dispatcher_planner.DispatcherDeterministicEntities,
    previous_assistant_text: str = "",
    enabled_classes: tuple[str, ...] = DispatcherRuntimeConfig.eligible_task_classes,
) -> tuple[bool, tuple[str, ...]]:
    text = f"{latest_question}\n{routing_question}".casefold()
    enabled = set(enabled_classes)
    reasons: list[str] = []

    if "prior_result_reference" in enabled and re.search(r"\b(previous|prior|last|earlier|your list|your previous|compare)\b", text):
        reasons.append("prior_result_reference")
    if "bulk_ticket_ids" in enabled and len(deterministic_entities.ticket_ids) >= 2:
        reasons.append("bulk_ticket_ids")
    if "challenge_fact_check" in enabled and (
        "where did you" in text
        or "prove" in text
        or "confirm" in text
        or "verify" in text
        or "подтверд" in text
        or "где ты" in text
    ):
        reasons.append("challenge_fact_check")
    if (
        "multi_store_ticket_search" in enabled
        and len(deterministic_entities.store_ids) >= 2
        and re.search(r"\b(ticket|tickets|тикет|internet|speed|connection|lookup|find|search)\b", text)
    ):
        reasons.append("multi_store_ticket_search")
    if "source_family_mismatch" in enabled and source_family_mismatch_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
    ):
        reasons.append("source_family_mismatch")

    semantic_intent = str((semantic_frame or {}).get("intent") or "").casefold()
    if "challenge_fact_check" in enabled and semantic_intent in {"event_fact_check", "fact_check_prior_answer"}:
        reasons.append("challenge_fact_check")
    if previous_assistant_text and "prior_result_reference" in enabled and "compare" in text:
        reasons.append("prior_result_reference")

    unique = tuple(dict.fromkeys(reasons))
    return bool(unique), unique


def build_planner_prompt(
    *,
    latest_question: str,
    routing_question: str,
    messages: list[Mapping[str, Any]],
    semantic_frame: Mapping[str, Any],
    conversation_id: str,
    turn_id: str,
    trace_id: str,
    owner_role: str,
    deterministic_entities: ai_dispatcher_planner.DispatcherDeterministicEntities,
    eligibility_reasons: tuple[str, ...],
) -> str:
    recent_messages = [
        {
            "role": str(item.get("role") or "")[:20],
            "content": str(item.get("content") or "")[:700],
        }
        for item in messages[-6:]
    ]
    payload = {
        "latest_question": latest_question[:1200],
        "routing_question": routing_question[:1200],
        "semantic_frame": semantic_frame,
        "recent_messages": recent_messages,
        "conversation": {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "trace_id": trace_id,
            "owner_role": owner_role,
        },
        "deterministic_entities": deterministic_entities.to_dict(),
        "eligibility_reasons": list(eligibility_reasons),
        "allowed_task_types": [item.value for item in ai_dispatcher_planner.DispatcherTaskType],
        "allowed_actions": [item.value for item in ai_dispatcher_planner.DispatcherActionName],
        "allowed_source_families": [item.value for item in ai_dispatcher_planner.DispatcherSourceFamily],
        "allowed_fallbacks": [item.value for item in ai_dispatcher_planner.DispatcherFallback],
    }
    return (
        "You are the bounded AI task planner for ESIP Universal Chat.\n"
        "Return one JSON object only. Do not answer the user. Do not use markdown.\n"
        "The JSON must match the configured response schema and backend dispatcher contract exactly.\n"
        "Use only enum strings from Context JSON. Never invent task_type, action names, tools, credentials, "
        "ticket facts, or data access.\n"
        "Preserve deterministic_entities exactly; they are authoritative and should appear in scope/actions.\n"
        "confidence must be a number from 0 to 1, not a label. Use 0.8 for high, 0.6 for medium, 0.4 for low.\n"
        "scope must always be an object with store_ids array, ticket_ids array, and source_family string.\n"
        "actions must always be an array of objects with name, inputs, max_rows, and required.\n"
        "comparison_contract and answer_contract must always be objects; use empty strings/arrays when not applicable.\n"
        "fallback must be exactly legacy_universal_chat unless the right safe fallback is ask_clarification or safe_limitation.\n"
        "cost_plan must keep max_planner_calls=1 and stay within the supplied caps.\n"
        "For prior-result comparison: task_type=ticket_set_compare, include load_prior_result_set, "
        "bulk_ticket_id_lookup, and compare_ticket_sets when relevant.\n"
        "For explicit ticket ID details: task_type=bulk_ticket_lookup and action bulk_ticket_id_lookup.\n"
        "For challenging a previous claim: task_type=fact_check_prior_answer and prefer ticket evidence actions.\n"
        "For multi-store ticket search: task_type=ticket_set_search and action ticket_set_search.\n\n"
        "For source-family mismatch before retrieval: if the user asks for ticket examples or ticket scenarios "
        "and the correct source family is Zendesk tickets, use task_type=ticket_set_search, "
        "scope.source_family=tickets, and action ticket_set_search. Do not use this for contact/table/procedure asks.\n\n"
        "Context JSON:\n"
        f"{json.dumps(payload, sort_keys=True, default=str)}"
    )


def _get_model() -> GenerativeModel:
    global _model
    if _model is None:
        try:
            creds = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            vertexai.init(
                project=settings.google_cloud_project_id,
                location=settings.gemini_location,
                credentials=creds,
            )
        except Exception:
            vertexai.init(
                project=settings.google_cloud_project_id,
                location=settings.gemini_location,
            )
        _model = GenerativeModel(settings.gemini_model)
    return _model


def _record_actual_usage(response: Any, correlation_id: str, estimated_cost: float) -> float | None:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    tokens_thinking = getattr(usage, "thoughts_token_count", 0) or 0
    actual_cost = record_usage(
        CallType.UNIVERSAL_DISPATCHER_PLANNER,
        tokens_in,
        tokens_out,
        tokens_thinking,
        correlation_id=correlation_id,
        est_cost_usd=estimated_cost,
        expected_cost_cad=estimated_cost,
        notes="Universal Chat AI dispatcher planner shadow call",
    )
    check_divergence(
        estimated_cost,
        actual_cost,
        CallType.UNIVERSAL_DISPATCHER_PLANNER,
        tokens_thinking,
        thinking_budget_was_zero=True,
    )
    return actual_cost


def _fallback_result(
    cfg: DispatcherRuntimeConfig,
    reason: str,
    exc: Exception,
    estimated_cost: float,
    reserved: float,
    estimated_tokens: int,
    correlation_id: str,
    started: float,
    candidates: DispatcherCandidateObservation,
) -> DispatcherRuntimeResult:
    return DispatcherRuntimeResult(
        config=cfg,
        eligible=True,
        planner_attempted=False,
        planner_calls=0,
        fallback_reason=reason,
        reason_codes=(reason,),
        estimated_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
        latency_ms=int((time.monotonic() - started) * 1000),
        prompt_tokens_estimate=estimated_tokens,
        output_tokens_limit=cfg.max_output_tokens,
        correlation_id=correlation_id,
        issues=({"field": "planner", "code": reason, "message": type(exc).__name__},),
        candidate_classes=candidates.classes,
        candidate_reason_codes=candidates.reason_codes,
    )


def _today_dispatcher_spent_cad() -> float:
    try:
        from app.services import firestore  # noqa: PLC0415

        key = datetime.now(BUSINESS_TZ).strftime("%Y-%m-%d")
        doc = firestore.get_db().document(f"cost_counters/{key}").get()
        data = doc.to_dict() or {} if doc.exists else {}
        return float(
            (((data.get("by_type") or {}).get(CallType.UNIVERSAL_DISPATCHER_PLANNER.value) or {}).get("cost_usd"))
            or 0.0
        )
    except Exception as exc:
        logger.warning("ai_dispatcher: could not read dispatcher daily cost: %s", exc)
        return 0.0


def _deterministic_ticket_ids(latest_question: str, routing_question: str, explicit_ticket_id: int | None) -> tuple[int, ...]:
    values: list[int] = []
    if explicit_ticket_id:
        values.append(int(explicit_ticket_id))
    for text in (latest_question, routing_question):
        for match in re.findall(r"(?<!\d)(\d{5,10})(?!\d)", text or ""):
            try:
                values.append(int(match))
            except ValueError:
                continue
    return tuple(dict.fromkeys(value for value in values if value > 0))


def _compile_mode_for_runtime(
    mode: ai_dispatcher_planner.DispatcherMode,
) -> ai_dispatcher_planner.DispatcherMode:
    if mode is ai_dispatcher_planner.DispatcherMode.DISABLED:
        return ai_dispatcher_planner.DispatcherMode.DISABLED
    if mode is ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE:
        return ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE
    return ai_dispatcher_planner.DispatcherMode.SHADOW


def _answer_affecting_class_enabled(cfg: DispatcherRuntimeConfig, task_class: str) -> bool:
    enabled = set(cfg.assistive_task_classes)
    if cfg.mode is ai_dispatcher_planner.DispatcherMode.ASSISTIVE:
        return task_class in enabled
    if cfg.mode is ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE:
        return task_class in set(PRIMARY_ANSWER_AFFECTING_TASK_CLASSES) and task_class in enabled
    return False


def _select_assistive_answer_class(
    *,
    cfg: DispatcherRuntimeConfig,
    decision: ai_dispatcher_planner.DispatcherCompileDecision,
    eligibility_reasons: tuple[str, ...],
    latest_question: str = "",
    routing_question: str = "",
    semantic_frame: Mapping[str, Any] | None = None,
    previous_assistant_text: str = "",
) -> str:
    if cfg.mode not in {
        ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
        ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE,
    }:
        return ""
    if not decision.accepted:
        return ""
    enabled = set(cfg.assistive_task_classes)
    if cfg.mode is ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE:
        enabled &= set(PRIMARY_ANSWER_AFFECTING_TASK_CLASSES)
    action_names = {action.name for action in decision.actions}
    if ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE in enabled and ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE in set(eligibility_reasons):
        required_actions = {
            ai_dispatcher_planner.DispatcherActionName.LOAD_PRIOR_RESULT_SET,
            ai_dispatcher_planner.DispatcherActionName.BULK_TICKET_ID_LOOKUP,
            ai_dispatcher_planner.DispatcherActionName.COMPARE_TICKET_SETS,
        }
        if required_actions.issubset(action_names):
            return ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE
    if (
        ASSISTIVE_CLASS_BULK_TICKET_IDS in enabled
        and ASSISTIVE_CLASS_BULK_TICKET_IDS in set(eligibility_reasons)
        and _is_phase5b_bulk_ticket_lookup(
            decision=decision,
            eligibility_reasons=eligibility_reasons,
            latest_question=latest_question,
            routing_question=routing_question,
            semantic_frame=semantic_frame or {},
            previous_assistant_text=previous_assistant_text,
        )
    ):
        return ASSISTIVE_CLASS_BULK_TICKET_IDS
    if (
        ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK in enabled
        and ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK in set(eligibility_reasons)
        and _is_phase5c_challenge_fact_check(decision=decision)
    ):
        return ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK
    if (
        ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH in enabled
        and ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH in set(eligibility_reasons)
        and _is_phase6a_source_family_mismatch(decision=decision)
    ):
        return ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH
    return ""


def _is_phase5c_challenge_fact_check(
    *,
    decision: ai_dispatcher_planner.DispatcherCompileDecision,
) -> bool:
    if decision.task_type is not ai_dispatcher_planner.DispatcherTaskType.FACT_CHECK_PRIOR_ANSWER:
        return False
    if not 1 <= len(decision.effective_scope.ticket_ids) <= 75:
        return False
    action_names = {action.name for action in decision.actions}
    allowed_actions = {
        ai_dispatcher_planner.DispatcherActionName.LOAD_PRIOR_RESULT_SET,
        ai_dispatcher_planner.DispatcherActionName.BULK_TICKET_ID_LOOKUP,
    }
    if action_names - allowed_actions:
        return False
    if ai_dispatcher_planner.DispatcherActionName.BULK_TICKET_ID_LOOKUP not in action_names:
        return False
    for action in decision.actions:
        if (
            action.name
            in {
                ai_dispatcher_planner.DispatcherActionName.BULK_TICKET_ID_LOOKUP,
                ai_dispatcher_planner.DispatcherActionName.LOAD_PRIOR_RESULT_SET,
            }
            and action.max_rows > 100
        ):
            return False
    return True


def _is_phase5b_bulk_ticket_lookup(
    *,
    decision: ai_dispatcher_planner.DispatcherCompileDecision,
    eligibility_reasons: tuple[str, ...],
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any],
    previous_assistant_text: str,
) -> bool:
    reasons = set(eligibility_reasons)
    if "challenge_fact_check" in reasons:
        return False
    if len(decision.effective_scope.ticket_ids) < 2:
        return False
    action_names = {action.name for action in decision.actions}
    if action_names != {ai_dispatcher_planner.DispatcherActionName.BULK_TICKET_ID_LOOKUP}:
        return False
    if any(action.max_rows > 75 for action in decision.actions):
        return False
    return _has_prior_bulk_ticket_context(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
    )


def _has_prior_bulk_ticket_context(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any],
    previous_assistant_text: str,
) -> bool:
    if not str(previous_assistant_text or "").strip():
        return False
    text = f"{latest_question}\n{routing_question}".casefold()
    if any(
        term in text
        for term in (
            "previous",
            "prior",
            "last answer",
            "your list",
            "your previous",
            "compare",
            "comparison",
            "these",
            "those",
            "предыдущ",
            "прошл",
            "сравн",
            "спис",
            "эти",
        )
    ):
        return True
    target_context = str((semantic_frame or {}).get("target_context") or "").casefold()
    if target_context in {"prior_turn", "conversation"}:
        return True
    return _looks_like_bare_ticket_id_followup(latest_question) or _looks_like_bare_ticket_id_followup(routing_question)


def _is_phase6a_source_family_mismatch(
    *,
    decision: ai_dispatcher_planner.DispatcherCompileDecision,
) -> bool:
    if decision.task_type is not ai_dispatcher_planner.DispatcherTaskType.TICKET_SET_SEARCH:
        return False
    if decision.effective_scope.source_family is not ai_dispatcher_planner.DispatcherSourceFamily.TICKETS:
        return False
    if not decision.effective_scope.store_ids:
        return False
    action_names = {action.name for action in decision.actions}
    if action_names != {ai_dispatcher_planner.DispatcherActionName.TICKET_SET_SEARCH}:
        return False
    if any(action.max_rows > 100 for action in decision.actions):
        return False
    if len(decision.effective_scope.ticket_ids) > 75:
        return False
    return True


def source_family_mismatch_candidate(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a turn is narrow enough for Phase 6A source-family planning."""
    text = f"{latest_question}\n{routing_question}".casefold()
    if not text.strip():
        return False
    asks_for_ticket_examples = bool(
        re.search(r"\b(find|show|list|get|give)\b.{0,80}\b(examples?|tickets?|cases?|incidents?)\b", text)
        or re.search(r"\b(examples?|tickets?|cases?|incidents?)\b.{0,80}\b(where|when|that|with)\b", text)
    )
    if not asks_for_ticket_examples:
        return False
    if re.search(r"\b(phone|contact|number|table|spreadsheet|sheet|job aid|document|article|kb)\b", text):
        return False
    intent = str((semantic_frame or {}).get("intent") or "").casefold()
    if intent in {"procedure_lookup", "current_ticket_context", "ticket_details"}:
        return False
    return True


def _looks_like_bare_ticket_id_followup(text: str) -> bool:
    if len(_deterministic_ticket_ids(text, text, None)) < 2:
        return False
    without_ids = re.sub(r"(?<!\d)\d{5,10}(?!\d)", " ", text or "")
    words = re.findall(r"[a-zа-яё]+", without_ids.casefold())
    allowed = {
        "ticket",
        "tickets",
        "id",
        "ids",
        "these",
        "those",
        "check",
        "lookup",
        "look",
        "up",
        "show",
        "details",
        "тикет",
        "тикеты",
        "тикетов",
        "эти",
        "проверь",
        "посмотри",
    }
    return len(words) <= 6 and all(word in allowed for word in words)


def _coerce_mode(raw: Any) -> ai_dispatcher_planner.DispatcherMode:
    try:
        return ai_dispatcher_planner.DispatcherMode(str(raw or "").strip())
    except ValueError:
        return ai_dispatcher_planner.DispatcherMode.DISABLED


def _int_between(raw: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _float_between(raw: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)


def _clean_str_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values = [str(item).strip() for item in raw if str(item).strip()]
    return tuple(dict.fromkeys(values))


def _clean_assistive_task_classes(raw: Any) -> tuple[str, ...]:
    allowed = set(ALLOWED_ASSISTIVE_TASK_CLASSES)
    values = [item for item in _clean_str_tuple(raw) if item in allowed]
    return tuple(dict.fromkeys(values))


def _clean_shadow_task_classes(raw: Any) -> tuple[str, ...]:
    allowed = set(ALLOWED_SHADOW_TASK_CLASSES)
    values = [item for item in _clean_str_tuple(raw) if item in allowed]
    return tuple(dict.fromkeys(values))


__all__ = [
    "DispatcherRuntimeConfig",
    "DispatcherRuntimeResult",
    "DispatcherCandidateObservation",
    "ALL_KNOWN_TASK_CLASSES",
    "ALLOWED_ASSISTIVE_TASK_CLASSES",
    "ALLOWED_SHADOW_TASK_CLASSES",
    "PRIMARY_ANSWER_AFFECTING_TASK_CLASSES",
    "ASSISTIVE_CLASS_BULK_TICKET_IDS",
    "ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK",
    "ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE",
    "ASSISTIVE_CLASS_FOLLOWUP_RESULT_SET_REFINEMENT",
    "ASSISTIVE_CLASS_FOLLOWUP_TICKET_REFERENCE",
    "ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE",
    "ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH",
    "ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP",
    "DISPATCHER_CLASS_MULTI_STORE_TICKET_SEARCH",
    "build_planner_prompt",
    "dispatcher_config_from_mapping",
    "dispatcher_eligible",
    "detect_dispatcher_candidates",
    "followup_date_scope_assist_enabled",
    "followup_result_set_refinement_enabled",
    "followup_result_set_refinement_shadow_enabled",
    "followup_ticket_reference_enabled",
    "followup_ticket_reference_shadow_enabled",
    "get_dispatcher_config",
    "run_dispatcher_shadow",
    "source_family_mismatch_candidate",
    "ticket_resolution_lookup_assist_enabled",
]
