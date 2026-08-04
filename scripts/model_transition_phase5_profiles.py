"""Offline, fail-closed candidate profiles for Model Transition Phase 5.

This module is deliberately adapter-neutral. It can describe and serialize a future request,
but it has no provider or network transport and never changes production configuration.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping


GEMINI_2_5 = "gemini-2.5"
GEMINI_3 = "gemini-3"
GEMINI_3_THINKING_LEVELS = frozenset({"MINIMAL", "LOW", "MEDIUM", "HIGH"})
GEMINI_3_FORBIDDEN_SAMPLING_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "candidate_count",
        "frequency_penalty",
        "presence_penalty",
    }
)
EVALUATOR_ONLY_FIELDS = frozenset(
    {
        "assistant_answer",
        "assertion",
        "ambiguous",
        "expected_route",
        "gold_answer",
        "known_bad",
        "known_good",
        "label",
        "quality_events",
        "reference_answer",
        "route_metadata",
        "rubric",
        "verified_good",
        "evaluator_notes",
    }
)
RAW_DATA_BLOB_KEYS = frozenset(
    {"conversation", "data", "evidence", "ticket_context", "tool_results"}
)
KNOWN_MODELS = {
    "gemini-2.5-flash": GEMINI_2_5,
    "gemini-3.5-flash-lite": GEMINI_3,
    "gemini-3.6-flash": GEMINI_3,
}
SDK_PACKAGE = "google-genai"
SDK_VERSION = "1.55.0"
API_VERSION = "v1"
LOCATION = "us-central1"
TRANSPORT_STATUS = "quarantined"
MAX_PROFILE_OUTPUT_TOKENS = 16_384

_SOURCE_GEMINI_36 = "https://ai.google.dev/gemini-api/docs/models/gemini-3.6-flash"
_SOURCE_GEMINI_35_LITE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite"
_SOURCE_PRICING = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
_SOURCE_SDK = "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk"
_SOURCE_THINKING = "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking"
_SOURCE_GEMINI_25 = "https://cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-5-flash"


class ProfileError(ValueError):
    """Raised whenever a profile or request violates a Phase 5 boundary."""


@dataclass(frozen=True)
class CandidateProfile:
    profile_id: str
    model: str
    family: str
    lane: str
    role: str
    eligible_surfaces: tuple[str, ...]
    prompt_variant: str
    prompt_contract_hash: str
    sdk_package: str
    sdk_version: str
    api_version: str
    location: str
    timeout_seconds: int
    max_attempts: int
    retry_enabled: bool
    automatic_function_calling: bool
    built_in_tools: bool
    explicit_cache: bool
    safety_settings: tuple[tuple[str, str], ...]
    max_output_tokens: int
    thinking_budget: int | None
    thinking_level: str | None
    sampling_config: tuple[tuple[str, int | float], ...]
    input_price_usd_per_million: float
    output_price_usd_per_million: float
    cost_cap_usd: float
    transport_status: str
    source_urls: tuple[str, ...]
    notes: str


_SAFETY_SETTINGS = (
    ("HARM_CATEGORY_HARASSMENT", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_HATE_SPEECH", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_SEXUALLY_EXPLICIT", "BLOCK_MEDIUM_AND_ABOVE"),
    ("HARM_CATEGORY_DANGEROUS_CONTENT", "BLOCK_MEDIUM_AND_ABOVE"),
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def prompt_contract_hash(variant: str) -> str:
    if variant not in {"legacy_fidelity", "model_native"}:
        raise ProfileError(f"unknown prompt variant: {variant}")
    descriptor = f"esip-model-transition-phase5-prompt-contract:{variant}:v1"
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


def _profile(
    *,
    profile_id: str,
    model: str,
    lane: str,
    role: str,
    surfaces: tuple[str, ...],
    prompt_variant: str,
    max_output_tokens: int,
    thinking_budget: int | None,
    thinking_level: str | None,
    sampling_config: tuple[tuple[str, int | float], ...],
    input_price: float,
    output_price: float,
    cost_cap: float,
    sources: tuple[str, ...],
    notes: str,
) -> CandidateProfile:
    return CandidateProfile(
        profile_id=profile_id,
        model=model,
        family=KNOWN_MODELS[model],
        lane=lane,
        role=role,
        eligible_surfaces=surfaces,
        prompt_variant=prompt_variant,
        prompt_contract_hash=prompt_contract_hash(prompt_variant),
        sdk_package=SDK_PACKAGE,
        sdk_version=SDK_VERSION,
        api_version=API_VERSION,
        location=LOCATION,
        timeout_seconds=30,
        max_attempts=1,
        retry_enabled=False,
        automatic_function_calling=False,
        built_in_tools=False,
        explicit_cache=False,
        safety_settings=_SAFETY_SETTINGS,
        max_output_tokens=max_output_tokens,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        sampling_config=sampling_config,
        input_price_usd_per_million=input_price,
        output_price_usd_per_million=output_price,
        cost_cap_usd=cost_cap,
        transport_status=TRANSPORT_STATUS,
        source_urls=sources,
        notes=notes,
    )


_CATALOG = (
    _profile(
        profile_id="control-gemini-2.5-flash-lane-d",
        model="gemini-2.5-flash",
        lane="D-control",
        role="control",
        surfaces=("ocr", "universal_chat", "analytics", "docs_gap"),
        prompt_variant="legacy_fidelity",
        max_output_tokens=8_192,
        thinking_budget=1_024,
        thinking_level=None,
        sampling_config=(("temperature", 0.2),),
        input_price=0.30,
        output_price=2.50,
        cost_cap=0.10,
        sources=(_SOURCE_GEMINI_25, _SOURCE_PRICING, _SOURCE_SDK, _SOURCE_THINKING),
        notes="Current production control profile; retained for Lane D until a separate trial.",
    ),
    _profile(
        profile_id="candidate-gemini-3.5-flash-lite-lane-a-bounded",
        model="gemini-3.5-flash-lite",
        lane="A",
        role="candidate",
        surfaces=("classification", "sql", "translation", "validator"),
        prompt_variant="model_native",
        max_output_tokens=1_024,
        thinking_budget=None,
        thinking_level="MINIMAL",
        sampling_config=(),
        input_price=0.33,
        output_price=2.75,
        cost_cap=0.03,
        sources=(_SOURCE_GEMINI_35_LITE, _SOURCE_PRICING, _SOURCE_SDK, _SOURCE_THINKING),
        notes="Bounded/high-throughput candidate lane; no quality ranking implied.",
    ),
    _profile(
        profile_id="candidate-gemini-3.6-flash-lane-b-grounded",
        model="gemini-3.6-flash",
        lane="B",
        role="candidate",
        surfaces=("universal_chat", "analytics", "docs_gap"),
        prompt_variant="model_native",
        max_output_tokens=8_192,
        thinking_budget=None,
        thinking_level="MEDIUM",
        sampling_config=(),
        input_price=1.50,
        output_price=7.50,
        cost_cap=0.20,
        sources=(_SOURCE_GEMINI_36, _SOURCE_PRICING, _SOURCE_SDK, _SOURCE_THINKING),
        notes="Grounded/contextual candidate lane; no quality ranking implied.",
    ),
    _profile(
        profile_id="candidate-gemini-3.6-flash-lane-c-followup",
        model="gemini-3.6-flash",
        lane="C",
        role="candidate",
        surfaces=("follow_up", "output_review"),
        prompt_variant="model_native",
        max_output_tokens=8_192,
        thinking_budget=None,
        thinking_level="MEDIUM",
        sampling_config=(),
        input_price=1.50,
        output_price=7.50,
        cost_cap=0.20,
        sources=(_SOURCE_GEMINI_36, _SOURCE_PRICING, _SOURCE_SDK, _SOURCE_THINKING),
        notes="Follow-up/context candidate lane; no quality ranking implied.",
    ),
)


def catalog() -> tuple[CandidateProfile, ...]:
    return _CATALOG


def profile(profile_id: str) -> CandidateProfile:
    for candidate in _CATALOG:
        if candidate.profile_id == profile_id:
            return candidate
    raise ProfileError(f"unknown profile: {profile_id}")


def _profile_dict(candidate: CandidateProfile) -> dict[str, Any]:
    return asdict(candidate)


def profile_hash(candidate: CandidateProfile) -> str:
    validate_profile(candidate)
    return hashlib.sha256(_canonical(_profile_dict(candidate)).encode("utf-8")).hexdigest()


def _manifest_entry(candidate: CandidateProfile) -> dict[str, Any]:
    validate_profile(candidate)
    return {
        "profile_id": candidate.profile_id,
        "model": candidate.model,
        "family": candidate.family,
        "lane": candidate.lane,
        "role": candidate.role,
        "eligible_surfaces": list(candidate.eligible_surfaces),
        "prompt_variant": candidate.prompt_variant,
        "prompt_contract_hash": candidate.prompt_contract_hash,
        "sdk_package": candidate.sdk_package,
        "sdk_version": candidate.sdk_version,
        "api_version": candidate.api_version,
        "location": candidate.location,
        "timeout_seconds": candidate.timeout_seconds,
        "max_attempts": candidate.max_attempts,
        "max_output_tokens": candidate.max_output_tokens,
        "thinking_budget": candidate.thinking_budget,
        "thinking_level": candidate.thinking_level,
        "sampling_fields": [key for key, _ in candidate.sampling_config],
        "input_price_usd_per_million": candidate.input_price_usd_per_million,
        "output_price_usd_per_million": candidate.output_price_usd_per_million,
        "cost_cap_usd": candidate.cost_cap_usd,
        "transport_status": candidate.transport_status,
        "source_urls": list(candidate.source_urls),
        "notes": candidate.notes,
    }

def catalog_manifest() -> tuple[dict[str, Any], ...]:
    return tuple(_manifest_entry(candidate) for candidate in _CATALOG)


def catalog_hash() -> str:
    return hashlib.sha256(_canonical(catalog_manifest()).encode("utf-8")).hexdigest()


def validate_profile(candidate: CandidateProfile) -> None:
    if not isinstance(candidate, CandidateProfile):
        raise ProfileError("profile must be a CandidateProfile")
    expected_family = KNOWN_MODELS.get(candidate.model)
    if expected_family is None:
        raise ProfileError(f"unknown model: {candidate.model}")
    if candidate.family != expected_family:
        raise ProfileError("model family does not match model")
    if candidate.prompt_variant not in {"legacy_fidelity", "model_native"}:
        raise ProfileError("prompt variant is not allowlisted")
    if candidate.prompt_contract_hash != prompt_contract_hash(candidate.prompt_variant):
        raise ProfileError("prompt contract hash does not match variant")
    if candidate.sdk_package != SDK_PACKAGE or candidate.sdk_version != SDK_VERSION:
        raise ProfileError("SDK package/version is not the reviewed Phase 5 pin")
    if candidate.api_version != API_VERSION or candidate.location != LOCATION:
        raise ProfileError("API version and location must be explicit and reviewed")
    if not isinstance(candidate.timeout_seconds, int) or not 1 <= candidate.timeout_seconds <= 120:
        raise ProfileError("timeout must be a bounded positive integer")
    if candidate.max_attempts != 1 or candidate.retry_enabled:
        raise ProfileError("exactly one attempt and no retry are required")
    if candidate.automatic_function_calling or candidate.built_in_tools or candidate.explicit_cache:
        raise ProfileError("automatic tools, built-in tools and explicit cache are disabled")
    if not candidate.safety_settings:
        raise ProfileError("explicit safety settings are required")
    if candidate.transport_status != TRANSPORT_STATUS:
        raise ProfileError("live transport is not permitted in Phase 5")
    if not 1 <= candidate.max_output_tokens <= MAX_PROFILE_OUTPUT_TOKENS:
        raise ProfileError("max_output_tokens must be explicit and bounded")
    if candidate.cost_cap_usd <= 0:
        raise ProfileError("positive cost cap is required")
    if not candidate.source_urls:
        raise ProfileError("official source references are required")
    sampling_fields = {key for key, _ in candidate.sampling_config}
    if candidate.family == GEMINI_2_5:
        if candidate.thinking_budget is None or candidate.thinking_level is not None:
            raise ProfileError("Gemini 2.5 requires thinking_budget only")
        if not isinstance(candidate.thinking_budget, int) or candidate.thinking_budget < 0:
            raise ProfileError("thinking_budget must be a non-negative integer")
    elif candidate.family == GEMINI_3:
        if candidate.thinking_budget is not None:
            raise ProfileError("Gemini 3 must not receive thinking_budget")
        if candidate.thinking_level not in GEMINI_3_THINKING_LEVELS:
            raise ProfileError("Gemini 3 requires an allowlisted thinking_level")
        if sampling_fields.intersection(GEMINI_3_FORBIDDEN_SAMPLING_FIELDS):
            raise ProfileError("Gemini 3 sampling and penalty fields are forbidden")
    else:
        raise ProfileError(f"unsupported model family: {candidate.family}")


def generation_config(candidate: CandidateProfile) -> dict[str, Any]:
    validate_profile(candidate)
    config: dict[str, Any] = {"max_output_tokens": candidate.max_output_tokens}
    if candidate.family == GEMINI_2_5:
        config["thinking_budget"] = candidate.thinking_budget
    else:
        config["thinking_level"] = candidate.thinking_level
    for key, value in candidate.sampling_config:
        config[key] = value
    return config


def estimate_cost_usd(candidate: CandidateProfile, input_tokens: int, output_tokens: int) -> float:
    validate_profile(candidate)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ProfileError("token reservations must be integers")
    if input_tokens < 0 or output_tokens < 0:
        raise ProfileError("token reservations cannot be negative")
    return round(
        (input_tokens * candidate.input_price_usd_per_million)
        / 1_000_000
        + (output_tokens * candidate.output_price_usd_per_million)
        / 1_000_000,
        8,
    )


def _validate_candidate_input(value: Any, *, inside_raw_blob: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if not inside_raw_blob and normalized in EVALUATOR_ONLY_FIELDS:
                raise ProfileError(f"evaluator-only field leaked into candidate input: {key}")
            if inside_raw_blob:
                continue
            _validate_candidate_input(
                nested,
                inside_raw_blob=normalized in RAW_DATA_BLOB_KEYS,
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_candidate_input(nested, inside_raw_blob=inside_raw_blob)
        return
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ProfileError("candidate input must be JSON-compatible")


def serialize_candidate_request(
    candidate: CandidateProfile,
    candidate_input: Mapping[str, Any],
    *,
    response_schema: Mapping[str, Any] | None = None,
    input_tokens: int = 0,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    validate_profile(candidate)
    if not isinstance(candidate_input, Mapping):
        raise ProfileError("candidate input must be a mapping")
    _validate_candidate_input(candidate_input)
    reserved_output = candidate.max_output_tokens if output_tokens is None else output_tokens
    reserved_cost = estimate_cost_usd(candidate, input_tokens, reserved_output)
    if reserved_cost > candidate.cost_cap_usd:
        raise ProfileError(
            f"reserved cost ${reserved_cost:.8f} exceeds profile cap ${candidate.cost_cap_usd:.8f}"
        )
    contents = copy.deepcopy(dict(candidate_input))
    return {
        "profile_id": candidate.profile_id,
        "model": candidate.model,
        "transport": {
            "sdk_package": candidate.sdk_package,
            "sdk_version": candidate.sdk_version,
            "api_version": candidate.api_version,
            "location": candidate.location,
            "timeout_seconds": candidate.timeout_seconds,
            "max_attempts": candidate.max_attempts,
            "retry_enabled": candidate.retry_enabled,
            "status": candidate.transport_status,
        },
        "generation_config": generation_config(candidate),
        "safety_settings": dict(candidate.safety_settings),
        "tool_policy": {
            "automatic_function_calling": candidate.automatic_function_calling,
            "built_in_tools": candidate.built_in_tools,
            "explicit_cache": candidate.explicit_cache,
        },
        "prompt": {
            "variant": candidate.prompt_variant,
            "contract_hash": candidate.prompt_contract_hash,
        },
        "reserved_cost_usd": reserved_cost,
        "response_schema": copy.deepcopy(dict(response_schema)) if response_schema else None,
        "contents": contents,
    }
