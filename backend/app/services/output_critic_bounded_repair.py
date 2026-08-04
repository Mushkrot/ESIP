"""Deterministic bounded repair decisions for output critic findings.

This module is intentionally pure: it does not call models, tools, databases,
Zendesk, Drive, Vertex, GCS, or external services. It can only turn an existing
output critic trace plus compact Universal Chat trace metadata into a bounded
correction decision.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.config import settings


CONTRACT_TYPE = "output_critic_bounded_repair"
DEFAULT_MIN_CONFIDENCE = 0.82
MAX_REASON_CHARS = 220

REPAIR_KIND_RAW_ARTIFACT_STRIP = "raw_artifact_strip"
REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR = "false_no_data_or_tool_error"
REPAIR_KIND_FOLLOWUP_SCOPE_LOSS = "followup_scope_loss"
REPAIR_KIND_RESOLUTION_CLAIM_GUARD = "resolution_claim_guard"

DEFAULT_ALLOWED_REPAIR_KINDS = (
    REPAIR_KIND_RAW_ARTIFACT_STRIP,
    REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR,
    REPAIR_KIND_FOLLOWUP_SCOPE_LOSS,
    REPAIR_KIND_RESOLUTION_CLAIM_GUARD,
)

_RISK_TO_REPAIR_KIND = {
    "raw_artifact": REPAIR_KIND_RAW_ARTIFACT_STRIP,
    "false_no_data": REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR,
    "tool_error_as_absence": REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR,
    "followup_scope_loss": REPAIR_KIND_FOLLOWUP_SCOPE_LOSS,
    "wrong_resolution_claim": REPAIR_KIND_RESOLUTION_CLAIM_GUARD,
    "no_documented_resolution": REPAIR_KIND_RESOLUTION_CLAIM_GUARD,
}
_RISK_PRIORITY = (
    "raw_artifact",
    "tool_error_as_absence",
    "false_no_data",
    "followup_scope_loss",
    "wrong_resolution_claim",
    "no_documented_resolution",
)

_NO_DATA_RE = re.compile(
    r"\b("
    r"no\s+(?:data|results?|matching|repeated|tickets?|stores?|issues?|records?)"
    r"|nothing\s+(?:found|matched)"
    r"|0\s+(?:matching\s+)?(?:tickets?|stores?|organizations?|records?|results?)"
    r"|zero\s+(?:tickets?|stores?|records?|results?)"
    r")\b",
    flags=re.IGNORECASE,
)
_ERROR_KEY_RE = re.compile(r"(?:error|failed|failure|timeout|exception|unavailable)", flags=re.IGNORECASE)
_ERROR_TEXT_RE = re.compile(
    r"\b(?:query|sql|tool|lookup|analytics|source|data)\s+(?:error|failed|failure|timeout)\b"
    r"|failed\s+generated\s+sql"
    r"|could\s+not\s+complete\s+the\s+(?:query|lookup|analytics|data)\s+check",
    flags=re.IGNORECASE,
)
_RAW_ARTIFACT_LINE_RE = re.compile(
    r"^\s*(?:DEBUG|TRACE(?:_ID|_JSON)?|SOURCES_JSON|TICKETS_JSON|Tool Call|Tool Output|Data Retrieved)\s*:",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class OutputCriticBoundedRepairDecision:
    """User-visible bounded repair decision plus private trace metadata."""

    emitted: bool
    attempted: bool
    answer_affecting: bool
    repair_applied: bool
    repair_kind: str = ""
    risk_level: str = "none"
    risk_codes: tuple[str, ...] = ()
    confidence: float = 0.0
    reason_codes: tuple[str, ...] = ()
    skipped_reason: str = ""
    stream_delta: str = ""
    repaired_answer: str = ""
    original_answer_sha256: str = ""
    repaired_answer_sha256: str = ""
    original_answer_chars: int = 0
    repaired_answer_chars: int = 0
    contract_type: str = CONTRACT_TYPE

    def to_trace_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_codes"] = list(self.risk_codes)
        payload["reason_codes"] = list(self.reason_codes)
        payload.pop("stream_delta", None)
        payload.pop("repaired_answer", None)
        return {
            key: value
            for key, value in payload.items()
            if value not in ("", [], (), None)
        }


def output_critic_bounded_repair_enabled(enabled: bool | None = None) -> bool:
    if enabled is not None:
        return bool(enabled)
    return bool(getattr(settings, "universal_output_critic_repair_enabled", False))


def allowed_repair_kinds(raw: str | Sequence[str] | None = None) -> tuple[str, ...]:
    if raw is None:
        raw = getattr(settings, "universal_output_critic_repair_allowed_kinds", "")
    if isinstance(raw, str):
        values = [item.strip() for item in re.split(r"[,; ]+", raw) if item.strip()]
    else:
        values = [str(item).strip() for item in raw if str(item).strip()]
    if not values:
        return DEFAULT_ALLOWED_REPAIR_KINDS
    return tuple(item for item in _dedupe(values) if item in DEFAULT_ALLOWED_REPAIR_KINDS)


def min_confidence(value: float | None = None) -> float:
    if value is None:
        value = getattr(settings, "universal_output_critic_repair_min_confidence", DEFAULT_MIN_CONFIDENCE)
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = DEFAULT_MIN_CONFIDENCE
    return max(0.0, min(1.0, number))


def evaluate_output_critic_bounded_repair(
    *,
    latest_question: str,
    visible_answer: str,
    output_critic_trace: Mapping[str, Any] | None,
    trace_context: Mapping[str, Any] | None = None,
    enabled: bool | None = None,
    allowed_kinds: str | Sequence[str] | None = None,
    min_confidence_value: float | None = None,
) -> OutputCriticBoundedRepairDecision:
    """Return a fail-closed deterministic repair decision."""
    del latest_question  # Reserved for future deterministic language selection.
    original = str(visible_answer or "").strip()
    critic = dict(output_critic_trace or {})
    risk_codes = tuple(_dedupe(str(code) for code in (critic.get("risk_codes") or []) if str(code).strip()))
    risk_level = str(critic.get("risk_level") or "none").strip().lower() or "none"
    confidence = _bounded_float(critic.get("confidence"), default=0.0)
    base = _base_decision(
        original_answer=original,
        risk_level=risk_level,
        risk_codes=risk_codes,
        confidence=confidence,
    )

    if not output_critic_bounded_repair_enabled(enabled):
        return _skip(base, "disabled_by_config")
    if not original:
        return _skip(base, "empty_visible_answer")
    if not bool(critic.get("attempted")):
        return _skip(base, "critic_not_attempted")
    if "output_critic_error" in risk_codes or critic.get("error"):
        return _skip(base, "critic_error")
    if risk_level != "high":
        return _skip(base, "risk_level_not_high")
    if confidence < min_confidence(min_confidence_value):
        return _skip(base, "low_confidence")

    repair_kind = _select_repair_kind(risk_codes)
    if not repair_kind:
        return _skip(base, "unsupported_risk")
    if repair_kind not in allowed_repair_kinds(allowed_kinds):
        return _skip(base, "repair_kind_disabled", repair_kind=repair_kind)

    trace = dict(trace_context or {})
    if repair_kind == REPAIR_KIND_RAW_ARTIFACT_STRIP:
        return _raw_artifact_repair(base, original)
    if repair_kind == REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR:
        return _false_no_data_or_tool_error_repair(base, original, trace, risk_codes)
    if repair_kind == REPAIR_KIND_FOLLOWUP_SCOPE_LOSS:
        return _followup_scope_repair(base, original, trace)
    if repair_kind == REPAIR_KIND_RESOLUTION_CLAIM_GUARD:
        return _resolution_claim_guard_repair(base, original, trace)
    return _skip(base, "unsupported_repair_kind", repair_kind=repair_kind)


def compact_trace_metadata(
    decision: OutputCriticBoundedRepairDecision | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(decision, OutputCriticBoundedRepairDecision):
        return decision.to_trace_dict()
    data = dict(decision or {})
    return OutputCriticBoundedRepairDecision(
        emitted=bool(data.get("emitted")),
        attempted=bool(data.get("attempted")),
        answer_affecting=bool(data.get("answer_affecting")),
        repair_applied=bool(data.get("repair_applied")),
        repair_kind=_safe_reason(data.get("repair_kind") or ""),
        risk_level=_safe_reason(data.get("risk_level") or "none"),
        risk_codes=tuple(_dedupe(str(code) for code in data.get("risk_codes") or [])),
        confidence=_bounded_float(data.get("confidence"), default=0.0),
        reason_codes=tuple(_dedupe(str(code) for code in data.get("reason_codes") or [])),
        skipped_reason=_safe_reason(data.get("skipped_reason") or ""),
        original_answer_sha256=_safe_reason(data.get("original_answer_sha256") or ""),
        repaired_answer_sha256=_safe_reason(data.get("repaired_answer_sha256") or ""),
        original_answer_chars=_safe_int(data.get("original_answer_chars")),
        repaired_answer_chars=_safe_int(data.get("repaired_answer_chars")),
    ).to_trace_dict()


def _base_decision(
    *,
    original_answer: str,
    risk_level: str,
    risk_codes: tuple[str, ...],
    confidence: float,
) -> OutputCriticBoundedRepairDecision:
    digest = _sha256(original_answer)
    return OutputCriticBoundedRepairDecision(
        emitted=False,
        attempted=False,
        answer_affecting=False,
        repair_applied=False,
        risk_level=risk_level if risk_level in {"none", "low", "medium", "high"} else "medium",
        risk_codes=risk_codes,
        confidence=confidence,
        original_answer_sha256=digest,
        repaired_answer_sha256=digest,
        original_answer_chars=len(original_answer),
        repaired_answer_chars=len(original_answer),
    )


def _skip(
    base: OutputCriticBoundedRepairDecision,
    reason: str,
    *,
    repair_kind: str = "",
) -> OutputCriticBoundedRepairDecision:
    return OutputCriticBoundedRepairDecision(
        emitted=False,
        attempted=base.attempted or reason != "disabled_by_config",
        answer_affecting=False,
        repair_applied=False,
        repair_kind=repair_kind,
        risk_level=base.risk_level,
        risk_codes=base.risk_codes,
        confidence=base.confidence,
        reason_codes=(_safe_reason(reason),),
        skipped_reason=_safe_reason(reason),
        original_answer_sha256=base.original_answer_sha256,
        repaired_answer_sha256=base.repaired_answer_sha256,
        original_answer_chars=base.original_answer_chars,
        repaired_answer_chars=base.repaired_answer_chars,
    )


def _apply(
    base: OutputCriticBoundedRepairDecision,
    *,
    repair_kind: str,
    reason_codes: Sequence[str],
    stream_delta: str,
    repaired_answer: str,
) -> OutputCriticBoundedRepairDecision:
    cleaned_delta = _normalize_delta(stream_delta)
    cleaned_answer = str(repaired_answer or "").strip()
    if not cleaned_delta or not cleaned_answer:
        return _skip(base, "empty_repair_output", repair_kind=repair_kind)
    return OutputCriticBoundedRepairDecision(
        emitted=True,
        attempted=True,
        answer_affecting=True,
        repair_applied=True,
        repair_kind=repair_kind,
        risk_level=base.risk_level,
        risk_codes=base.risk_codes,
        confidence=base.confidence,
        reason_codes=tuple(_dedupe(reason_codes)),
        stream_delta=cleaned_delta,
        repaired_answer=cleaned_answer,
        original_answer_sha256=base.original_answer_sha256,
        repaired_answer_sha256=_sha256(cleaned_answer),
        original_answer_chars=base.original_answer_chars,
        repaired_answer_chars=len(cleaned_answer),
    )


def _select_repair_kind(risk_codes: Sequence[str]) -> str:
    risk_set = set(risk_codes)
    for risk in _RISK_PRIORITY:
        if risk in risk_set:
            return _RISK_TO_REPAIR_KIND[risk]
    return ""


def _raw_artifact_repair(base: OutputCriticBoundedRepairDecision, answer: str) -> OutputCriticBoundedRepairDecision:
    cleaned = _strip_raw_artifacts(answer)
    if not cleaned or cleaned == answer:
        return _skip(base, "missing_raw_artifact_delta", repair_kind=REPAIR_KIND_RAW_ARTIFACT_STRIP)
    delta = (
        "\n\nCorrection: I removed internal debug/tool-output text from the answer. "
        "Use this cleaned answer instead:\n\n"
        f"{cleaned}"
    )
    return _apply(
        base,
        repair_kind=REPAIR_KIND_RAW_ARTIFACT_STRIP,
        reason_codes=("critic_high_raw_artifact", "deterministic_artifact_strip"),
        stream_delta=delta,
        repaired_answer=f"{answer.rstrip()}{delta}",
    )


def _false_no_data_or_tool_error_repair(
    base: OutputCriticBoundedRepairDecision,
    answer: str,
    trace_context: Mapping[str, Any],
    risk_codes: Sequence[str],
) -> OutputCriticBoundedRepairDecision:
    if not _answer_claims_no_data(answer):
        return _skip(base, "answer_does_not_claim_no_data", repair_kind=REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR)
    if "tool_error_as_absence" not in set(risk_codes) and not _trace_has_error_or_incomplete_data(trace_context):
        return _skip(base, "missing_tool_error_evidence", repair_kind=REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR)
    delta = (
        "\n\nCorrection: I should not treat a failed or incomplete data path as proof that no matching "
        "records exist. Treat the no-data statement above as unverified until the bounded data check "
        "succeeds."
    )
    repaired = f"{answer.rstrip()}{delta}"
    return _apply(
        base,
        repair_kind=REPAIR_KIND_FALSE_NO_DATA_OR_TOOL_ERROR,
        reason_codes=("critic_high_false_no_data_or_tool_error", "deterministic_no_data_correction"),
        stream_delta=delta,
        repaired_answer=repaired,
    )


def _followup_scope_repair(
    base: OutputCriticBoundedRepairDecision,
    answer: str,
    trace_context: Mapping[str, Any],
) -> OutputCriticBoundedRepairDecision:
    assist = _dict(trace_context.get("followup_date_scope_assist"))
    if not (assist.get("accepted") and assist.get("answer_affecting")):
        return _skip(base, "missing_deterministic_followup_answer", repair_kind=REPAIR_KIND_FOLLOWUP_SCOPE_LOSS)
    label = _clean_text(assist.get("display_label"))
    if not label:
        windows = [item for item in assist.get("month_windows") or [] if isinstance(item, Mapping)]
        label = ", ".join(_clean_text(item.get("display_label")) for item in windows if _clean_text(item.get("display_label")))
    if not label:
        return _skip(base, "missing_followup_scope_label", repair_kind=REPAIR_KIND_FOLLOWUP_SCOPE_LOSS)
    delta = (
        "\n\nCorrection: this follow-up should preserve the prior topic and the "
        f"{label} scope. Do not treat the answer above as a narrower date range."
    )
    repaired = f"{answer.rstrip()}{delta}"
    return _apply(
        base,
        repair_kind=REPAIR_KIND_FOLLOWUP_SCOPE_LOSS,
        reason_codes=("critic_high_followup_scope_loss", "deterministic_followup_scope_context_present"),
        stream_delta=delta,
        repaired_answer=repaired,
    )


def _resolution_claim_guard_repair(
    base: OutputCriticBoundedRepairDecision,
    answer: str,
    trace_context: Mapping[str, Any],
) -> OutputCriticBoundedRepairDecision:
    assist = _dict(trace_context.get("ticket_resolution_lookup_assist"))
    summary = _dict(_dict(trace_context.get("policy_flags")).get("ai_dispatcher_ticket_resolution_lookup"))
    if not assist.get("accepted"):
        return _skip(base, "missing_ticket_resolution_assist", repair_kind=REPAIR_KIND_RESOLUTION_CLAIM_GUARD)
    if summary.get("documented_resolution") is not False:
        return _skip(base, "missing_no_documented_resolution_evidence", repair_kind=REPAIR_KIND_RESOLUTION_CLAIM_GUARD)
    delta = (
        "\n\nCorrection: the bounded ticket lookup did not find a useful documented resolution. "
        "Do not treat any suggested fix above as documented evidence for how this ticket was resolved."
    )
    repaired = f"{answer.rstrip()}{delta}"
    return _apply(
        base,
        repair_kind=REPAIR_KIND_RESOLUTION_CLAIM_GUARD,
        reason_codes=("critic_high_resolution_claim", "bounded_lookup_no_documented_resolution"),
        stream_delta=delta,
        repaired_answer=repaired,
    )


def _strip_raw_artifacts(answer: str) -> str:
    kept: list[str] = []
    skipping_tool_block = False
    for line in str(answer or "").splitlines():
        if _RAW_ARTIFACT_LINE_RE.match(line):
            skipping_tool_block = True
            continue
        if skipping_tool_block:
            if not line.strip():
                skipping_tool_block = False
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _trace_has_error_or_incomplete_data(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_ERROR_TEXT_RE.search(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _ERROR_KEY_RE.search(str(key)) and item not in ("", None, False, [], {}):
                return True
            if _trace_has_error_or_incomplete_data(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_trace_has_error_or_incomplete_data(item) for item in value)
    return False


def _answer_claims_no_data(answer: str) -> bool:
    return bool(_NO_DATA_RE.search(answer or ""))


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_delta(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n\n" + text if not text.startswith("\n") else text


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_reason(value: Any) -> str:
    text = _clean_text(value)
    if len(text) <= MAX_REASON_CHARS:
        return text
    return text[: MAX_REASON_CHARS - 1].rstrip() + "..."


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dedupe(values: Sequence[str] | Any) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return seen
