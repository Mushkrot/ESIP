"""Evaluate answer risks in shadow mode without changing the visible answer.

Includes deterministic checks and an optional cost-bounded model review."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.services.genai_transport import GenerationConfig, BoundModel

from app.config import settings
from app.services import model_routing
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


CONTRACT_TYPE = "output_critic_shadow"
MODEL_SURFACE = model_routing.ModelSurface.OUTPUT_CRITIC_SHADOW.value
MAX_SAFE_REASON_CHARS = 220
MAX_EXCERPT_CHARS = 1200
ESTIMATED_OUTPUT_TOKENS = 400

logger = logging.getLogger(__name__)

ALLOWED_RISK_LEVELS = frozenset({"none", "low", "medium", "high"})
ALLOWED_RISK_CODES = frozenset(
    {
        "false_no_data",
        "tool_error_as_absence",
        "stale_data",
        "source_family_mismatch",
        "evidence_mismatch",
        "context_loss",
        "followup_scope_loss",
        "unsupported_strong_claim",
        "weak_evidence",
        "denominator_drift",
        "raw_artifact",
        "wrong_resolution_claim",
        "no_documented_resolution",
        "output_critic_error",
    }
)

_RAW_KEY_MARKERS = (
    "full_text",
    "html_body",
    "body",
    "raw_prompt",
    "prompt",
    "raw_model_response",
    "model_response",
    "raw_response",
    "trace_json",
    "context_json",
    "private_note",
)
_SAFE_INPUT_KEYS = {
    "latest_question",
    "routing_question",
    "semantic_frame",
    "phase1_decision",
    "policy_flags",
    "quality_gate_flags",
    "answer_validation",
    "counts",
    "sources",
    "tickets",
    "ai_dispatcher",
    "followup_understanding_shadow",
    "followup_date_scope_assist",
    "ticket_resolution_lookup_assist",
    "conversation_entities_loaded",
    "conversation_entities_saved",
}

OUTPUT_CRITIC_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "risk_codes": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ALLOWED_RISK_CODES)},
        },
        "confidence": {"type": "number"},
        "reason_short": {"type": "string"},
    },
    "required": ["risk_level", "risk_codes", "confidence", "reason_short"],
}

_CRITIC_PROMPT = """\
You are the private shadow output critic for ESIP Universal Chat.

Review only the compact bundle below. Do not use outside knowledge. Do not fetch
data. Do not call tools. Do not rewrite the answer. Do not include final factual
content. Return JSON only.

Flag risks only when the visible answer appears unsupported, contradicts the
compact trace, loses follow-up/date scope, presents no-data as proof after a
tool/source problem, uses the wrong source family, makes unsupported strong
claims, or exposes raw internal artifacts. Good supported answers should return
risk_level=none with no risk codes.

Compact bundle:
{context_json}
"""

_model: BoundModel | None = None


@dataclass(frozen=True)
class OutputCriticShadowResult:
    """Compact private telemetry for one output critic observation."""

    emitted: bool
    attempted: bool
    answer_affecting: bool
    risk_level: str
    risk_codes: tuple[str, ...]
    confidence: float
    reason_short: str
    skipped_reason: str = ""
    error: str = ""
    latency_ms: int | None = None
    estimated_cost_cad: float | None = None
    reserved_cost_cad: float | None = None
    actual_cost_cad: float | None = None
    correlation_id: str = ""
    model_surface: str = MODEL_SURFACE
    contract_type: str = CONTRACT_TYPE

    def to_trace_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_codes"] = list(self.risk_codes)
        payload["answer_affecting"] = False
        return {
            key: value
            for key, value in payload.items()
            if value not in ("", [], (), None)
        }


def normalize_output_critic_payload(raw: Mapping[str, Any] | None) -> OutputCriticShadowResult:
    """Normalize model/contract-like data into a fail-safe shadow result."""
    data = dict(raw or {})
    risk_codes = tuple(_dedupe(str(code) for code in data.get("risk_codes") or [] if str(code) in ALLOWED_RISK_CODES))
    risk_level = str(data.get("risk_level") or ("medium" if risk_codes else "none")).strip().lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        risk_level = "medium" if risk_codes else "none"
    confidence = _bounded_float(data.get("confidence"), default=0.0)
    reason_short = _safe_reason(data.get("reason_short") or data.get("reason") or "")
    attempted = bool(data.get("attempted", True))
    emitted = bool(data.get("emitted", attempted))
    return OutputCriticShadowResult(
        emitted=emitted,
        attempted=attempted,
        answer_affecting=False,
        risk_level=risk_level,
        risk_codes=risk_codes,
        confidence=confidence,
        reason_short=reason_short,
        skipped_reason=_safe_reason(data.get("skipped_reason") or ""),
        error=_safe_reason(data.get("error") or ""),
        latency_ms=_optional_int(data.get("latency_ms")),
        estimated_cost_cad=_optional_float(data.get("estimated_cost_cad")),
        reserved_cost_cad=_optional_float(data.get("reserved_cost_cad")),
        actual_cost_cad=_optional_float(data.get("actual_cost_cad")),
        correlation_id=str(data.get("correlation_id") or "")[:80],
    )


def skipped_result(reason: str) -> OutputCriticShadowResult:
    return OutputCriticShadowResult(
        emitted=False,
        attempted=False,
        answer_affecting=False,
        risk_level="none",
        risk_codes=(),
        confidence=0.0,
        reason_short="",
        skipped_reason=_safe_reason(reason),
    )


def error_result(reason: str, *, latency_ms: int | None = None) -> OutputCriticShadowResult:
    return OutputCriticShadowResult(
        emitted=True,
        attempted=True,
        answer_affecting=False,
        risk_level="medium",
        risk_codes=("output_critic_error",),
        confidence=0.0,
        reason_short="Output critic failed open.",
        error=_safe_reason(reason),
        latency_ms=latency_ms,
    )


def build_critic_input_bundle(
    *,
    latest_question: str,
    visible_answer: str,
    trace_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact prompt-ready bundle without raw ticket text or prompts."""
    trace_context = trace_context or {}
    compact_trace = {
        key: _sanitize_value(value)
        for key, value in trace_context.items()
        if key in _SAFE_INPUT_KEYS
    }
    return {
        "latest_question": _excerpt(latest_question, MAX_EXCERPT_CHARS),
        "visible_answer_excerpt": _excerpt(visible_answer, MAX_EXCERPT_CHARS),
        "trace": compact_trace,
    }


def compact_trace_metadata(result: OutputCriticShadowResult | Mapping[str, Any] | None) -> dict[str, Any]:
    """Return metadata safe to persist in trace/log payloads."""
    if isinstance(result, OutputCriticShadowResult):
        payload = result.to_trace_dict()
    else:
        payload = normalize_output_critic_payload(result).to_trace_dict()
    return _sanitize_value(payload)


def output_critic_shadow_enabled(enabled: bool | None = None) -> bool:
    if enabled is not None:
        return bool(enabled)
    return bool(getattr(settings, "universal_output_critic_shadow_enabled", False))


def run_output_critic_shadow(
    *,
    latest_question: str,
    visible_answer: str,
    trace_context: Mapping[str, Any] | None = None,
    enabled: bool | None = None,
    skip_reason: str = "",
) -> OutputCriticShadowResult:
    """Run one fail-open shadow critic call for a finalized visible answer."""
    if skip_reason:
        return skipped_result(skip_reason)
    if not output_critic_shadow_enabled(enabled):
        return skipped_result("disabled_by_config")
    if not str(visible_answer or "").strip():
        return skipped_result("empty_visible_answer")

    started = time.monotonic()
    bundle = build_critic_input_bundle(
        latest_question=latest_question,
        visible_answer=visible_answer,
        trace_context=trace_context,
    )
    prompt = _CRITIC_PROMPT.replace(
        "{context_json}",
        json.dumps(bundle, sort_keys=True, default=str),
    )
    try:
        profile = model_routing.resolve_surface_model_profile(
            settings,
            model_routing.ModelSurface.OUTPUT_CRITIC_SHADOW,
        )
        generation_payload = model_routing.generation_config_payload(profile)
        generation_payload["response_mime_type"] = "application/json"
        generation_payload["response_schema"] = OUTPUT_CRITIC_RESPONSE_SCHEMA
        generation_config = GenerationConfig.from_dict(generation_payload)
        estimated_tokens = estimate_tokens_from_text(prompt)
        estimated_cost = calc_cost(
            estimated_tokens,
            ESTIMATED_OUTPUT_TOKENS,
            tokens_thinking=0,
            call_type=CallType.UNIVERSAL_OUTPUT_CRITIC,
        )
        reserved = get_gate_reserve(CallType.UNIVERSAL_OUTPUT_CRITIC, estimated_cost)
        correlation_id = new_correlation_id()
        check_cost_gate(CallType.UNIVERSAL_OUTPUT_CRITIC, reserved, fail_closed=False)
        record_estimate(
            CallType.UNIVERSAL_OUTPUT_CRITIC,
            estimated_cost,
            correlation_id,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=ESTIMATED_OUTPUT_TOKENS,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
            notes="Universal Chat output critic shadow call",
        )
        model = _get_model(profile.model_id)
        response = model.generate_content(prompt, generation_config=generation_config)
        actual_cost = _record_actual_usage(response, correlation_id, estimated_cost)
        parsed = _parse_response_json(getattr(response, "text", "") or "")
        result = normalize_output_critic_payload(
            {
                **parsed,
                "attempted": True,
                "emitted": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "estimated_cost_cad": estimated_cost,
                "reserved_cost_cad": reserved,
                "actual_cost_cad": actual_cost,
                "correlation_id": correlation_id,
            }
        )
        return result
    except (CostGateError, CostDivergenceError) as exc:
        return error_result(str(exc), latency_ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:
        logger.warning("output_critic_shadow failed open: %s", exc)
        return error_result(str(exc), latency_ms=int((time.monotonic() - started) * 1000))


def _parse_response_json(raw_text: str) -> dict[str, Any]:
    parsed = json.loads(raw_text or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("output critic response was not a JSON object")
    return parsed


def _get_model(model_id: str) -> BoundModel:
    global _model
    if _model is not None and model_id == settings.gemini_model:
        return _model
    model = BoundModel(model_id, surface="output_critic_shadow")
    if model_id == settings.gemini_model:
        _model = model
    return model


def _record_actual_usage(response: Any, correlation_id: str, estimated_cost: float) -> float | None:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    tokens_thinking = getattr(usage, "thoughts_token_count", 0) or 0
    actual_cost = record_usage(
        CallType.UNIVERSAL_OUTPUT_CRITIC,
        tokens_in,
        tokens_out,
        tokens_thinking,
        correlation_id=correlation_id,
        est_cost_usd=estimated_cost,
        expected_cost_cad=estimated_cost,
        notes="Universal Chat output critic shadow call",
    )
    check_divergence(
        estimated_cost,
        actual_cost,
        CallType.UNIVERSAL_OUTPUT_CRITIC,
        tokens_thinking,
        thinking_budget_was_zero=True,
    )
    return actual_cost


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_raw_key(key_str):
                continue
            clean[key_str] = _sanitize_value(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value[:20]]
    if isinstance(value, str):
        return _excerpt(value, MAX_EXCERPT_CHARS)
    return value


def _is_raw_key(key: str) -> bool:
    normalized = key.lower()
    return any(marker == normalized or marker in normalized for marker in _RAW_KEY_MARKERS)


def _excerpt(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _safe_reason(value: Any) -> str:
    return _excerpt(value, MAX_SAFE_REASON_CHARS)


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: Any) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return seen
