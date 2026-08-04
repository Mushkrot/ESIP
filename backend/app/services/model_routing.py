"""Pure model-routing helpers for ESIP Gemini surfaces.

This module intentionally avoids live clients and router imports. It only turns
settings-like values into model profiles and GenerationConfig payload dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ModelRoutingError(ValueError):
    """Raised when a model profile or generation config is unsafe."""


class ModelFamily(str, Enum):
    GEMINI_2_5 = "gemini_2_5"
    GEMINI_3 = "gemini_3"
    UNKNOWN = "unknown"


class ModelSurface(str, Enum):
    ANSWER_VALIDATOR = "answer_validator"
    CHAT_QUALITY_REVIEW = "chat_quality_review"
    OUTPUT_CRITIC_SHADOW = "output_critic_shadow"
    REPAIR_ESCALATION = "repair_escalation"
    UNIVERSAL_PHASE1_ROUTING = "universal_phase1_routing"
    UNIVERSAL_PHASE2_FINAL = "universal_phase2_final"
    UNIVERSAL_TRANSLATION = "universal_translation"
    SEMANTIC_INTERPRETER = "semantic_interpreter"
    ANALYTICS_SQL = "analytics_sql"
    ANALYTICS_FINAL = "analytics_final"
    DOCS_GAP_REPORT = "docs_gap_report"
    OCR = "ocr"
    AI_SUMMARY = "ai_summary"
    SUMMARY_TRANSLATION = "summary_translation"


@dataclass(frozen=True)
class SurfaceDefaults:
    max_output_tokens: int
    temperature: float
    thinking_budget: int
    cost_tier_label: str = "current"


@dataclass(frozen=True)
class ModelProfile:
    surface: ModelSurface
    model_id: str
    family: ModelFamily
    max_output_tokens: int
    temperature: float
    thinking_budget: int | None = None
    thinking_level: str | None = None
    cost_tier_label: str = "current"
    fallback_reason: str | None = None


SURFACE_DEFAULTS: Mapping[ModelSurface, SurfaceDefaults] = {
    ModelSurface.ANSWER_VALIDATOR: SurfaceDefaults(max_output_tokens=2048, temperature=0.0, thinking_budget=0),
    ModelSurface.CHAT_QUALITY_REVIEW: SurfaceDefaults(max_output_tokens=4096, temperature=0.0, thinking_budget=0),
    ModelSurface.OUTPUT_CRITIC_SHADOW: SurfaceDefaults(max_output_tokens=1024, temperature=0.0, thinking_budget=0),
    ModelSurface.REPAIR_ESCALATION: SurfaceDefaults(max_output_tokens=4096, temperature=0.0, thinking_budget=0, cost_tier_label="stronger_candidate"),
    ModelSurface.UNIVERSAL_PHASE1_ROUTING: SurfaceDefaults(max_output_tokens=1024, temperature=0.0, thinking_budget=0),
    ModelSurface.UNIVERSAL_PHASE2_FINAL: SurfaceDefaults(max_output_tokens=8192, temperature=0.2, thinking_budget=1024),
    ModelSurface.UNIVERSAL_TRANSLATION: SurfaceDefaults(max_output_tokens=4096, temperature=0.0, thinking_budget=0),
    ModelSurface.SEMANTIC_INTERPRETER: SurfaceDefaults(max_output_tokens=1024, temperature=0.0, thinking_budget=0),
    ModelSurface.ANALYTICS_SQL: SurfaceDefaults(max_output_tokens=1024, temperature=0.0, thinking_budget=0),
    ModelSurface.ANALYTICS_FINAL: SurfaceDefaults(max_output_tokens=8192, temperature=0.2, thinking_budget=1024),
    ModelSurface.DOCS_GAP_REPORT: SurfaceDefaults(max_output_tokens=4096, temperature=0.2, thinking_budget=1024),
    ModelSurface.OCR: SurfaceDefaults(max_output_tokens=2048, temperature=0.0, thinking_budget=0),
    ModelSurface.AI_SUMMARY: SurfaceDefaults(max_output_tokens=16384, temperature=0.2, thinking_budget=0),
    ModelSurface.SUMMARY_TRANSLATION: SurfaceDefaults(max_output_tokens=16384, temperature=0.2, thinking_budget=0),
}


_SURFACE_OVERRIDE_ATTR: Mapping[ModelSurface, str] = {
    ModelSurface.ANSWER_VALIDATOR: "universal_validator_model",
    ModelSurface.CHAT_QUALITY_REVIEW: "universal_review_model",
    ModelSurface.OUTPUT_CRITIC_SHADOW: "universal_output_critic_model",
    ModelSurface.REPAIR_ESCALATION: "universal_model_escalation_model",
}


def normalize_model_id(model_id: str | None) -> str:
    return (model_id or "").strip()


def detect_model_family(model_id: str) -> ModelFamily:
    model = normalize_model_id(model_id).lower()
    if model.startswith("gemini-2.5-"):
        return ModelFamily.GEMINI_2_5
    if model.startswith("gemini-3."):
        return ModelFamily.GEMINI_3
    return ModelFamily.UNKNOWN


def _default_thinking_level(thinking_budget: int) -> str:
    if thinking_budget <= 0:
        return "none"
    if thinking_budget <= 1024:
        return "low"
    return "medium"


def _cost_tier_for_model(model_id: str, default_label: str) -> str:
    model = normalize_model_id(model_id).lower()
    if model == "gemini-3.1-flash-lite":
        return "candidate_low"
    if model == "gemini-3.5-flash":
        return "stronger_candidate"
    return default_label


def build_model_profile(
    *,
    surface: ModelSurface | str,
    model_id: str,
    max_output_tokens: int,
    temperature: float = 0.0,
    thinking_budget: int = 0,
    cost_tier_label: str = "current",
    fallback_reason: str | None = None,
) -> ModelProfile:
    resolved_surface = ModelSurface(surface)
    resolved_model = normalize_model_id(model_id)
    if not resolved_model:
        raise ModelRoutingError("model_id is required")
    if max_output_tokens <= 0:
        raise ModelRoutingError("max_output_tokens must be positive")

    family = detect_model_family(resolved_model)
    if family is ModelFamily.UNKNOWN:
        raise ModelRoutingError(f"unknown model family for {resolved_model}")

    if family is ModelFamily.GEMINI_3:
        return ModelProfile(
            surface=resolved_surface,
            model_id=resolved_model,
            family=family,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            thinking_level=_default_thinking_level(thinking_budget),
            cost_tier_label=_cost_tier_for_model(resolved_model, cost_tier_label),
            fallback_reason=fallback_reason,
        )

    return ModelProfile(
        surface=resolved_surface,
        model_id=resolved_model,
        family=family,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        thinking_budget=thinking_budget,
        cost_tier_label=_cost_tier_for_model(resolved_model, cost_tier_label),
        fallback_reason=fallback_reason,
    )


def generation_config_payload(profile: ModelProfile) -> dict[str, Any]:
    """Return a safe payload for `GenerationConfig.from_dict(...)`."""
    if profile.thinking_budget is not None and profile.thinking_level is not None:
        raise ModelRoutingError("thinking_budget and thinking_level cannot be set together")
    if profile.max_output_tokens <= 0:
        raise ModelRoutingError("max_output_tokens must be positive")

    thinking_config: dict[str, Any]
    if profile.family is ModelFamily.GEMINI_3:
        if not profile.thinking_level:
            raise ModelRoutingError("Gemini 3 profile requires thinking_level")
        thinking_config = {"thinking_level": profile.thinking_level}
    elif profile.family is ModelFamily.GEMINI_2_5:
        if profile.thinking_budget is None:
            raise ModelRoutingError("Gemini 2.5 profile requires thinking_budget")
        thinking_config = {"thinking_budget": profile.thinking_budget}
    else:
        raise ModelRoutingError(f"unsupported model family: {profile.family}")

    return {
        "temperature": profile.temperature,
        "max_output_tokens": profile.max_output_tokens,
        "thinking_config": thinking_config,
    }


def resolve_surface_model_profile(
    settings_obj: Any,
    surface: ModelSurface | str,
    *,
    override_model_id: str | None = None,
) -> ModelProfile:
    """Resolve a surface profile while preserving current defaults.

    Empty or unknown overrides fall back to `settings_obj.gemini_model`; unknown
    fallback models fail closed because the app would not have a known-safe
    generation-config family.
    """
    resolved_surface = ModelSurface(surface)
    defaults = SURFACE_DEFAULTS[resolved_surface]
    fallback_model = normalize_model_id(getattr(settings_obj, "gemini_model", ""))
    configured_override = normalize_model_id(override_model_id)
    if not configured_override:
        attr_name = _SURFACE_OVERRIDE_ATTR.get(resolved_surface)
        configured_override = normalize_model_id(getattr(settings_obj, attr_name, "")) if attr_name else ""

    selected_model = configured_override or fallback_model
    fallback_reason: str | None = None
    if configured_override and detect_model_family(configured_override) is ModelFamily.UNKNOWN:
        selected_model = fallback_model
        fallback_reason = "unknown_override_model_family"

    return build_model_profile(
        surface=resolved_surface,
        model_id=selected_model,
        max_output_tokens=defaults.max_output_tokens,
        temperature=defaults.temperature,
        thinking_budget=defaults.thinking_budget,
        cost_tier_label=defaults.cost_tier_label,
        fallback_reason=fallback_reason,
    )


def telemetry_payload(profile: ModelProfile, *, candidate_mode: str = "baseline", escalation_reason: str | None = None) -> dict[str, Any]:
    return {
        "model_id": profile.model_id,
        "model_family": profile.family.value,
        "model_surface": profile.surface.value,
        "candidate_mode": candidate_mode,
        "cost_tier_label": profile.cost_tier_label,
        "escalation_reason": escalation_reason,
        "fallback_reason": profile.fallback_reason,
    }
