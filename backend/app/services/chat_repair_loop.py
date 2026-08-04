"""Pure policy helpers for bounded automatic Universal Chat answer repair."""

from __future__ import annotations

import json
import re
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import settings
from app.models.chat_quality import (
    ChatAnswerValidationResult,
    ChatRepairLoopDecision,
    ChatRepairLoopIteration,
    ChatRepairLoopState,
    ChatRepairScopeFingerprint,
    ChatValidatorAction,
    ChatValidatorRiskClass,
)
from app.services import universal_answer_now


_REPAIR_ALLOWED_ACTIONS = frozenset({
    "rerun_bq",
    "zendesk_fallback",
    "drive_exact_retry",
    "rebuild_scoped_evidence",
})
_WEAK_REVIEW_QUALITIES = frozenset({"weak", "likely_wrong", "insufficient_evidence"})


def default_loop_state(
    *,
    scope_fingerprint: ChatRepairScopeFingerprint | None = None,
) -> ChatRepairLoopState:
    """Build loop state from runtime settings without touching live clients."""
    return ChatRepairLoopState(
        enabled=bool(getattr(settings, "universal_ai_repair_loop_enabled", True)),
        max_repairs=max(0, int(getattr(settings, "universal_ai_repair_loop_max_repairs", 2))),
        max_validations=max(0, int(getattr(settings, "universal_ai_repair_loop_max_validations", 3))),
        scope_fingerprint=scope_fingerprint,
    )


def validation_decision(state: ChatRepairLoopState) -> ChatRepairLoopDecision:
    """Return whether another validator check is allowed."""
    if not state.enabled:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="loop_disabled",
            reason="automatic repair loop disabled",
        )
    if state.validation_count >= state.max_validations:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="max_validations",
            reason="validation cap reached",
        )
    return ChatRepairLoopDecision(allowed=True)


def repair_decision(
    state: ChatRepairLoopState,
    *,
    action: ChatValidatorAction,
    repair_signature: str,
) -> ChatRepairLoopDecision:
    """Return whether another repair attempt is allowed."""
    if not state.enabled:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="loop_disabled",
            reason="automatic repair loop disabled",
        )
    if action not in _REPAIR_ALLOWED_ACTIONS:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="repair_not_allowed",
            reason=f"validator action {action} is not repair-loop allowlisted",
        )
    if state.repair_count >= state.max_repairs:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="max_repairs",
            reason="repair cap reached",
        )
    if repair_signature in state.repair_signatures:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="repeated_action",
            reason="same repair signature already attempted",
        )
    return ChatRepairLoopDecision(allowed=True)


def escalation_decision(
    *,
    validation_result: ChatAnswerValidationResult | None = None,
    review_quality: str | None = None,
    should_validate: bool = True,
    escalation_count: int = 0,
) -> ChatRepairLoopDecision:
    """Return whether stronger-model escalation is allowed for a repair/review path."""
    if not bool(getattr(settings, "universal_model_escalation_enabled", False)):
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="escalation_disabled",
            reason="stronger-model escalation disabled",
        )
    if not should_validate:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="routine_answer",
            reason="routine supported answer must not escalate",
        )
    max_per_turn = max(0, int(getattr(settings, "universal_model_escalation_max_per_turn", 1)))
    if escalation_count >= max_per_turn:
        return ChatRepairLoopDecision(
            allowed=False,
            stop_reason="escalation_cap",
            reason="per-turn escalation cap reached",
        )

    allowed_actions = _settings_escalation_actions()
    action = str(validation_result.recommended_action) if validation_result else ""
    weak_review = _norm_text(review_quality) in _WEAK_REVIEW_QUALITIES
    if action in allowed_actions or weak_review:
        return ChatRepairLoopDecision(allowed=True)

    return ChatRepairLoopDecision(
        allowed=False,
        stop_reason="escalation_not_allowlisted",
        reason="validator/review outcome is not escalation allowlisted",
    )


def record_validation(state: ChatRepairLoopState) -> ChatRepairLoopState:
    """Return state after one validator check."""
    return state.model_copy(update={"validation_count": state.validation_count + 1})


def record_repair(
    state: ChatRepairLoopState,
    *,
    repair_signature: str,
) -> ChatRepairLoopState:
    """Return state after one repair attempt."""
    signatures = list(state.repair_signatures)
    if repair_signature not in signatures:
        signatures.append(repair_signature)
    return state.model_copy(update={
        "repair_count": state.repair_count + 1,
        "repair_signatures": signatures,
    })


def build_scope_fingerprint(
    *,
    latest_question: str | None = None,
    target_ticket_id: Any = None,
    store_id: Any = None,
    pos_type: str | None = None,
    status_filter: list[str] | tuple[str, ...] | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    source_family: str | None = None,
    denominator_key: str | None = None,
) -> ChatRepairScopeFingerprint:
    """Normalize request scope into a comparable fingerprint."""
    statuses = tuple(sorted({
        _norm_text(status)
        for status in (status_filter or [])
        if _norm_text(status)
    }))
    return ChatRepairScopeFingerprint(
        latest_question=_norm_text(latest_question),
        target_ticket_id=_norm_id(target_ticket_id),
        store_id=_norm_id(store_id),
        pos_type=_norm_text(pos_type),
        status_filter=statuses,
        date_start=_norm_text(date_start),
        date_end=_norm_text(date_end),
        source_family=_norm_text(source_family),
        denominator_key=_norm_text(denominator_key),
    )


def scope_drifted(
    original: ChatRepairScopeFingerprint | None,
    candidate: ChatRepairScopeFingerprint | None,
) -> bool:
    """Return true when a repair candidate changed the original request scope."""
    if original is None or candidate is None:
        return False
    return _scope_key(original) != _scope_key(candidate)


def build_repair_signature(
    *,
    action: ChatValidatorAction,
    scope_fingerprint: ChatRepairScopeFingerprint | None = None,
    risk_classes: list[ChatValidatorRiskClass] | tuple[ChatValidatorRiskClass, ...] | None = None,
) -> str:
    """Build a stable signature for repeated-action detection."""
    payload = {
        "action": action,
        "risks": sorted(str(risk) for risk in (risk_classes or [])),
        "scope": _scope_key(scope_fingerprint) if scope_fingerprint else None,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def no_progress(
    previous_answer: str | None,
    repaired_answer: str | None,
    *,
    min_answer_chars: int = 20,
    min_delta_chars: int = 8,
) -> bool:
    """Return true when a repair produced no useful visible-answer change."""
    before = _norm_answer(previous_answer)
    after = _norm_answer(repaired_answer)
    if len(after) < min_answer_chars:
        return True
    if after == before:
        return True
    return abs(len(after) - len(before)) < min_delta_chars and after in {before, before.strip()}


def compact_loop_telemetry(
    state: ChatRepairLoopState,
    *,
    iterations: list[ChatRepairLoopIteration] | None = None,
) -> dict[str, Any]:
    """Return compact private telemetry without prompts, raw tool dumps, or answers."""
    return {
        "enabled": state.enabled,
        "validation_count": state.validation_count,
        "repair_count": state.repair_count,
        "max_validations": state.max_validations,
        "max_repairs": state.max_repairs,
        "stop_reason": state.stop_reason,
        "answer_now_requested": state.answer_now_requested,
        "answer_now_checkpoint": state.answer_now_checkpoint,
        "answer_now_final_policy": state.answer_now_final_policy,
        "repair_signatures_count": len(state.repair_signatures),
        "iterations": [
            {
                "iteration": item.iteration,
                "validation_count": item.validation_count,
                "repair_count": item.repair_count,
                "validator_quality": item.validator_quality,
                "validator_action": item.validator_action,
                "repair_attempted": item.repair_attempted,
                "repair_applied": item.repair_applied,
                "stop_reason": item.stop_reason,
                "has_repair_signature": bool(item.repair_signature),
            }
            for item in (iterations or [])
        ],
    }


def request_answer_now(
    state: ChatRepairLoopState,
    *,
    checkpoint: str,
    final_policy: str | None = None,
) -> ChatRepairLoopState:
    """Mark the pure repair-loop state as Answer Now requested."""
    return state.model_copy(update={
        "answer_now_requested": True,
        "answer_now_checkpoint": str(checkpoint or "").strip() or "unknown",
        "answer_now_final_policy": final_policy,
    })


def answer_now_checkpoint_decision(
    state: ChatRepairLoopState,
    *,
    current_answer: str | None,
    checkpoint: str,
) -> ChatRepairLoopDecision:
    """Return whether optional work should continue at an Answer Now checkpoint."""
    if not state.answer_now_requested:
        return ChatRepairLoopDecision(allowed=True)

    candidate = (current_answer or "").strip()
    if not candidate:
        return ChatRepairLoopDecision(
            allowed=True,
            stop_reason="answer_now_waiting_for_candidate",
            reason="Answer Now requested before a safe answer candidate exists",
        )

    return ChatRepairLoopDecision(
        allowed=False,
        stop_reason="answer_now",
        reason=f"Answer Now requested at {str(checkpoint or '').strip() or 'unknown'} checkpoint",
    )


def build_answer_now_partial_result(answer: str) -> tuple[str, dict[str, Any]]:
    """Return a user-visible partial answer and compact structured flags."""
    result = universal_answer_now.answer_now_candidate_policy(
        answer,
        answer_now_requested=True,
        changed_finalization_policy=True,
    )
    return result.visible_answer, {
        **result.flags,
        "stop_reason": "answer_now",
        "answer_policy": "partial",
    }


def _settings_escalation_actions() -> set[str]:
    raw = str(getattr(settings, "universal_model_escalation_allowed_actions", "") or "")
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip()
    }


async def run_one_pass_repair(
    state: ChatRepairLoopState,
    *,
    validation_result: ChatAnswerValidationResult,
    latest_question: str,
    repair_seed_builder: Callable[[ChatAnswerValidationResult, str], Any],
    refresh_trace_builder: Callable[[Any], Awaitable[dict]],
    review_runner: Callable[[dict], Awaitable[tuple[Any, dict[str, Any]]]],
    answer_gate: Callable[[str], tuple[str, dict[str, Any]]],
) -> tuple[ChatRepairLoopState, dict[str, Any], str | None, dict[str, Any] | None]:
    """Run the current one-pass automatic repair through policy-controlled callbacks.

    Phase 3 intentionally preserves current behavior: one initial validator result
    can cause at most one read-only repair/review pass, and repaired text is not
    revalidated until the later repeated-validation phase.
    """
    if validation_result.validator_enabled:
        state = record_validation(state)
    signature = build_repair_signature(
        action=validation_result.recommended_action,
        scope_fingerprint=state.scope_fingerprint,
        risk_classes=validation_result.risk_classes,
    )
    repair_state: dict[str, Any] = {
        "attempted": False,
        "applied": False,
        "max_passes": 1,
        "loop": compact_loop_telemetry(state),
    }
    decision = repair_decision(
        state,
        action=validation_result.recommended_action,
        repair_signature=signature,
    )
    if not decision.allowed:
        next_state = state.model_copy(update={"stop_reason": decision.stop_reason})
        repair_state["skip_reason"] = decision.stop_reason
        repair_state["loop"] = compact_loop_telemetry(next_state)
        return next_state, repair_state, None, None

    next_state = record_repair(state, repair_signature=signature)
    repair_state.update({
        "attempted": True,
        "requested_action": validation_result.recommended_action,
        "risk_classes": list(validation_result.risk_classes),
        "repair_signature": signature,
    })
    try:
        repair_seed = repair_seed_builder(validation_result, latest_question)
        refresh_trace = await refresh_trace_builder(repair_seed)
        refresh_block = refresh_trace.get("review_refresh") if isinstance(refresh_trace, dict) else None
        repair_state["refresh"] = {
            "used": bool(refresh_block),
            "query": (refresh_block or {}).get("query"),
            "counts": (refresh_block or {}).get("counts", {}),
            "tools": ((refresh_block or {}).get("phase1_decision") or {}).get("tools", []),
        }
        if not refresh_block:
            next_state = next_state.model_copy(update={"stop_reason": "repair_failed"})
            repair_state["skip_reason"] = "no_refresh_evidence"
            repair_state["loop"] = compact_loop_telemetry(next_state)
            return next_state, repair_state, None, None

        repair_review, repair_telemetry = await review_runner(refresh_trace)
        repair_state["review"] = {
            "quality": repair_review.quality,
            "confidence": repair_review.confidence,
            "problem_types": list(repair_review.problem_types),
            "recommended_action": repair_review.recommended_action,
            "has_improved_answer": bool(repair_review.improved_answer),
            "telemetry": repair_telemetry,
        }
        if repair_review.improved_answer:
            repaired_answer, gate_flags = answer_gate(repair_review.improved_answer)
            repair_state["applied"] = True
            repair_state["final_quality_gate_flags"] = gate_flags
            next_state = next_state.model_copy(update={"stop_reason": "accepted"})
            repair_state["loop"] = compact_loop_telemetry(next_state)
            return next_state, repair_state, repaired_answer, gate_flags

        next_state = next_state.model_copy(update={"stop_reason": "repair_failed"})
        repair_state["skip_reason"] = "no_improved_answer"
        repair_state["loop"] = compact_loop_telemetry(next_state)
        return next_state, repair_state, None, None
    except Exception as exc:
        next_state = next_state.model_copy(update={"stop_reason": "repair_failed"})
        repair_state["error"] = str(exc)
        repair_state["loop"] = compact_loop_telemetry(next_state)
        return next_state, repair_state, None, None


async def run_revalidation_repair_loop(
    state: ChatRepairLoopState,
    *,
    initial_validation_result: ChatAnswerValidationResult,
    initial_answer: str,
    latest_question: str,
    repair_seed_builder: Callable[[ChatAnswerValidationResult, str], Any],
    refresh_trace_builder: Callable[[Any], Awaitable[dict]],
    review_runner: Callable[[dict], Awaitable[tuple[Any, dict[str, Any]]]],
    answer_gate: Callable[[str], tuple[str, dict[str, Any]]],
    validator_runner: Callable[[str], Awaitable[tuple[ChatAnswerValidationResult, dict[str, Any]]]],
    validation_result_applier: Callable[[str, ChatAnswerValidationResult], tuple[str, dict[str, Any]]],
    caveat_builder: Callable[[str, str], tuple[str, dict[str, Any]]],
    candidate_scope_builder: Callable[[], ChatRepairScopeFingerprint | None] | None = None,
    answer_now_checker: Callable[[str], bool | Awaitable[bool]] | None = None,
) -> tuple[ChatRepairLoopState, dict[str, Any], str | None, dict[str, Any] | None]:
    """Run bounded repair with repeated validation until accepted or safely stopped."""
    if initial_validation_result.validator_enabled:
        state = record_validation(state)

    current_result = initial_validation_result
    current_answer = initial_answer
    iterations: list[ChatRepairLoopIteration] = []
    repair_state: dict[str, Any] = {
        "attempted": False,
        "applied": False,
        "max_passes": state.max_repairs,
        "loop": compact_loop_telemetry(state, iterations=iterations),
        "revalidations": [],
        "answer_now": {
            "requested": False,
            "checkpoint": None,
            "wait_for_safe_candidate": False,
            "final_policy": None,
        },
    }

    while True:
        checkpoint_state, answer_now_result = await _maybe_finalize_for_answer_now(
            state,
            current_answer=current_answer,
            checkpoint="before_optional_repair",
            answer_now_checker=answer_now_checker,
        )
        state = checkpoint_state
        if answer_now_result is not None:
            final_answer, final_flags = answer_now_result
            repair_state["answer_now"] = {
                "requested": True,
                "checkpoint": state.answer_now_checkpoint,
                "wait_for_safe_candidate": False,
                "final_policy": state.answer_now_final_policy,
            }
            repair_state["skip_reason"] = "answer_now"
            repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
            return state, repair_state, final_answer, final_flags

        action = current_result.recommended_action
        if action not in _REPAIR_ALLOWED_ACTIONS:
            final_answer, final_flags = validation_result_applier(current_answer, current_result)
            state = state.model_copy(update={"stop_reason": "accepted"})
            repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
            return state, repair_state, final_answer, final_flags

        signature = build_repair_signature(
            action=action,
            scope_fingerprint=state.scope_fingerprint,
            risk_classes=current_result.risk_classes,
        )
        decision = repair_decision(state, action=action, repair_signature=signature)
        if not decision.allowed:
            state = state.model_copy(update={"stop_reason": decision.stop_reason})
            final_answer, final_flags = caveat_builder(current_answer, decision.stop_reason)
            repair_state["skip_reason"] = decision.stop_reason
            repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
            return state, repair_state, final_answer, final_flags

        state = record_repair(state, repair_signature=signature)
        repair_state.update({
            "attempted": True,
            "requested_action": action,
            "risk_classes": list(current_result.risk_classes),
            "repair_signature": signature,
        })
        iteration = ChatRepairLoopIteration(
            iteration=len(iterations) + 1,
            validation_count=state.validation_count,
            repair_count=state.repair_count,
            validator_quality=current_result.quality,
            validator_action=action,
            repair_signature=signature,
            repair_attempted=True,
        )

        try:
            repair_seed = repair_seed_builder(current_result, latest_question)
            refresh_trace = await refresh_trace_builder(repair_seed)
            refresh_block = refresh_trace.get("review_refresh") if isinstance(refresh_trace, dict) else None
            repair_state["refresh"] = {
                "used": bool(refresh_block),
                "query": (refresh_block or {}).get("query"),
                "counts": (refresh_block or {}).get("counts", {}),
                "tools": ((refresh_block or {}).get("phase1_decision") or {}).get("tools", []),
            }
            if not refresh_block:
                state = state.model_copy(update={"stop_reason": "repair_failed"})
                final_answer, final_flags = caveat_builder(current_answer, "repair_failed")
                repair_state["skip_reason"] = "no_refresh_evidence"
                iterations.append(iteration.model_copy(update={"stop_reason": "repair_failed"}))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            repair_review, repair_telemetry = await review_runner(refresh_trace)
            repair_state["review"] = {
                "quality": repair_review.quality,
                "confidence": repair_review.confidence,
                "problem_types": list(repair_review.problem_types),
                "recommended_action": repair_review.recommended_action,
                "has_improved_answer": bool(repair_review.improved_answer),
                "telemetry": repair_telemetry,
            }
            if not repair_review.improved_answer:
                state = state.model_copy(update={"stop_reason": "repair_failed"})
                final_answer, final_flags = caveat_builder(current_answer, "repair_failed")
                repair_state["skip_reason"] = "no_improved_answer"
                iterations.append(iteration.model_copy(update={"stop_reason": "repair_failed"}))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            repaired_answer, gate_flags = answer_gate(repair_review.improved_answer)
            repair_state["applied"] = True
            repair_state["final_quality_gate_flags"] = gate_flags
            if no_progress(current_answer, repaired_answer):
                state = state.model_copy(update={"stop_reason": "no_progress"})
                final_answer, final_flags = caveat_builder(current_answer, "no_progress")
                repair_state["applied"] = current_answer != initial_answer
                repair_state["skip_reason"] = "no_progress"
                iterations.append(iteration.model_copy(
                    update={"repair_applied": False, "stop_reason": "no_progress"}
                ))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            candidate_scope = candidate_scope_builder() if candidate_scope_builder else state.scope_fingerprint
            if scope_drifted(state.scope_fingerprint, candidate_scope):
                state = state.model_copy(update={"stop_reason": "scope_drift"})
                final_answer, final_flags = caveat_builder(current_answer, "scope_drift")
                repair_state["applied"] = current_answer != initial_answer
                repair_state["skip_reason"] = "scope_drift"
                iterations.append(iteration.model_copy(
                    update={"repair_applied": False, "stop_reason": "scope_drift"}
                ))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            checkpoint_state, answer_now_result = await _maybe_finalize_for_answer_now(
                state,
                current_answer=repaired_answer,
                checkpoint="before_revalidation",
                answer_now_checker=answer_now_checker,
            )
            state = checkpoint_state
            if answer_now_result is not None:
                final_answer, final_flags = answer_now_result
                repair_state["answer_now"] = {
                    "requested": True,
                    "checkpoint": state.answer_now_checkpoint,
                    "wait_for_safe_candidate": False,
                    "final_policy": state.answer_now_final_policy,
                }
                repair_state["skip_reason"] = "answer_now"
                iterations.append(iteration.model_copy(update={"repair_applied": True, "stop_reason": "answer_now"}))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            validation_gate = validation_decision(state)
            if not validation_gate.allowed:
                state = state.model_copy(update={"stop_reason": validation_gate.stop_reason})
                final_answer, final_flags = caveat_builder(repaired_answer, validation_gate.stop_reason)
                repair_state["skip_reason"] = validation_gate.stop_reason
                iterations.append(iteration.model_copy(
                    update={"repair_applied": True, "stop_reason": validation_gate.stop_reason}
                ))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            try:
                current_result, validator_telemetry = await validator_runner(repaired_answer)
            except Exception as exc:
                state = state.model_copy(update={"stop_reason": "validator_failure"})
                final_answer, final_flags = caveat_builder(repaired_answer, "validator_failure")
                repair_state["error"] = str(exc)
                iterations.append(iteration.model_copy(
                    update={"repair_applied": True, "stop_reason": "validator_failure"}
                ))
                repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
                return state, repair_state, final_answer, final_flags

            state = record_validation(state)
            repair_state["revalidations"].append({
                "quality": current_result.quality,
                "risk_classes": list(current_result.risk_classes),
                "recommended_action": current_result.recommended_action,
                "answer_policy": current_result.answer_policy,
                "telemetry": validator_telemetry,
            })
            iterations.append(iteration.model_copy(update={"repair_applied": True}))
            current_answer = repaired_answer
        except Exception as exc:
            state = state.model_copy(update={"stop_reason": "repair_failed"})
            final_answer, final_flags = caveat_builder(current_answer, "repair_failed")
            repair_state["error"] = str(exc)
            iterations.append(iteration.model_copy(update={"stop_reason": "repair_failed"}))
            repair_state["loop"] = compact_loop_telemetry(state, iterations=iterations)
            return state, repair_state, final_answer, final_flags


async def _maybe_finalize_for_answer_now(
    state: ChatRepairLoopState,
    *,
    current_answer: str | None,
    checkpoint: str,
    answer_now_checker: Callable[[str], bool | Awaitable[bool]] | None,
) -> tuple[ChatRepairLoopState, tuple[str, dict[str, Any]] | None]:
    if answer_now_checker is None:
        return state, None
    requested = answer_now_checker(checkpoint)
    if inspect.isawaitable(requested):
        requested = await requested
    if not requested:
        return state, None

    requested_state = request_answer_now(state, checkpoint=checkpoint)
    decision = answer_now_checkpoint_decision(
        requested_state,
        current_answer=current_answer,
        checkpoint=checkpoint,
    )
    if decision.allowed:
        return requested_state, None

    final_state = requested_state.model_copy(update={
        "stop_reason": "answer_now",
        "answer_now_final_policy": "partial",
    })
    return final_state, build_answer_now_partial_result(current_answer or "")


def _scope_key(scope: ChatRepairScopeFingerprint) -> tuple[Any, ...]:
    return (
        scope.latest_question,
        scope.target_ticket_id,
        scope.store_id,
        scope.pos_type,
        scope.status_filter,
        scope.date_start,
        scope.date_end,
        scope.source_family,
        scope.denominator_key,
    )


def _norm_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip().lower())
    return text or None


def _norm_answer(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())
