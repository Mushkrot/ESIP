"""
Automatic second-level validator for Universal Chat answer candidates.

This service reviews a structured answer-candidate bundle only when the
deterministic risk detector asks for it. It does not fetch data and it does not
replace manual Improve Answer; stream integration is added in later phases.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import vertexai
from google.oauth2 import service_account
from vertexai.generative_models import GenerationConfig, GenerativeModel

from app.config import settings
from app.models.chat_quality import (
    ChatAnswerRiskDetectionResult,
    ChatAnswerValidationResult,
    ChatQualityReviewResult,
)
from app.services.cost_tracker import (
    CallType,
    calc_cost,
    check_cost_gate,
    check_divergence,
    estimate_tokens_from_text,
    get_gate_reserve,
    new_correlation_id,
    record_estimate,
    record_usage,
)
from app.services import model_routing

logger = logging.getLogger(__name__)

MAX_VALIDATOR_OUTPUT_TOKENS = 2048
ESTIMATED_VALIDATOR_OUTPUT_TOKENS = 700

try:
    _creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    vertexai.init(
        project=settings.google_cloud_project_id,
        location=settings.gemini_location,
        credentials=_creds,
    )
except Exception:
    vertexai.init(
        project=settings.google_cloud_project_id,
        location=settings.gemini_location,
    )

_model = GenerativeModel(settings.gemini_model)
_model_factory = GenerativeModel

_VALIDATOR_PROMPT = """\
You are the private second-level answer validator for ESIP Universal Chat.

Review the supplied answer-candidate bundle only. Do not use outside knowledge.
Do not fetch data. Do not invent facts. Do not treat previous assistant prose as
evidence. The backend will execute data access and final answer policy; your job
is only to identify risk and recommend one allowlisted action.

Return JSON only with this exact shape:
{
  "quality": "good | suspect | unsafe",
  "confidence": 0.0,
  "risk_classes": [
    "false_no_data",
    "tool_error_as_absence",
    "stale_data",
    "source_family_mismatch",
    "route_task_mismatch",
    "evidence_mismatch",
    "context_loss",
    "unsupported_strong_claim",
    "weak_evidence",
    "denominator_drift",
    "raw_artifact"
  ],
  "recommended_action": "none | rerun_bq | zendesk_fallback | drive_exact_retry | rebuild_scoped_evidence | rewrite_with_existing_sources | final_with_caveat",
  "answer_policy": "normal | partial | no_data_checked | caveated",
  "reason_short": "short private reason",
  "user_status": "optional short operational status or null",
  "rewrite_from_supplied_evidence": "optional final-answer rewrite using only supplied evidence, or null"
}

Rules:
- If the answer is well supported, use quality=good, recommended_action=none, answer_policy=normal.
- If evidence is missing, stale, mismatched, or weak, recommend a caveat or an allowlisted rerun/fallback.
- Only include rewrite_from_supplied_evidence when it can be written entirely from fields in the bundle.
- Never include tool JSON, debug text, SQL internals, trace ids, or chain-of-thought.

Answer-candidate bundle:
{context_json}
"""

_ALLOWED_QUALITY = {"good", "suspect", "unsafe"}
_ALLOWED_RISKS = {
    "false_no_data",
    "tool_error_as_absence",
    "stale_data",
    "source_family_mismatch",
    "route_task_mismatch",
    "evidence_mismatch",
    "context_loss",
    "unsupported_strong_claim",
    "weak_evidence",
    "denominator_drift",
    "raw_artifact",
    "validator_error",
}
_ALLOWED_ACTIONS = {
    "none",
    "rerun_bq",
    "zendesk_fallback",
    "drive_exact_retry",
    "rebuild_scoped_evidence",
    "rewrite_with_existing_sources",
    "final_with_caveat",
}
_ALLOWED_POLICIES = {"normal", "partial", "no_data_checked", "caveated"}
_REWRITE_ALLOWED_ACTIONS = {"rewrite_with_existing_sources", "final_with_caveat"}
_BOUNDED_REPAIR_ACTIONS = {
    "rerun_bq",
    "zendesk_fallback",
    "drive_exact_retry",
    "rebuild_scoped_evidence",
}
_REPAIR_TO_REVIEW_ACTION = {
    "rerun_bq": "reroute",
    "zendesk_fallback": "reroute",
    "drive_exact_retry": "run_semantic_plus_exact",
    "rebuild_scoped_evidence": "run_semantic_plus_exact",
}
_REPAIR_RISK_TO_PROBLEM = {
    "false_no_data": "weak_sources",
    "tool_error_as_absence": "weak_sources",
    "stale_data": "obsolete_source",
    "source_family_mismatch": "wrong_routing",
    "evidence_mismatch": "weak_sources",
    "context_loss": "weak_sources",
    "unsupported_strong_claim": "weak_sources",
    "weak_evidence": "weak_sources",
    "denominator_drift": "conflicting_sources",
    "raw_artifact": "answer_too_generic",
    "validator_error": "weak_sources",
}
_RAW_LINE_RE = re.compile(r"^\s*(?:DEBUG|TRACE(?:_ID)?|SOURCES_JSON|TICKETS_JSON|TRACE_JSON)\s*:", re.IGNORECASE | re.MULTILINE)
_TOOL_BLOCK_RE = re.compile(r"^\s*(?:Tool Call|Tool Output|Data Retrieved)\s*:", re.IGNORECASE | re.MULTILINE)
_NO_DATA_RE = re.compile(
    r"\b("
    r"no\s+(?:data|results?|matching|repeated|tickets?|stores?|issues?|records?)"
    r"|nothing\s+(?:found|matched)"
    r"|0\s+(?:matching\s+)?(?:tickets?|stores?|organizations?|records?|results?)"
    r"|zero\s+(?:tickets?|stores?|records?|results?)"
    r")\b",
    flags=re.IGNORECASE,
)
_QUERY_ERROR_RE = re.compile(
    r"\b(?:query|sql|tool|bq|bigquery|zendesk|drive)\s+error\b"
    r"|failed\s+generated\s+sql"
    r"|analytics\s+helper\s+failed"
    r"|timeout"
    r"|exception",
    flags=re.IGNORECASE,
)
_CURRENT_WINDOW_RE = re.compile(r"\b(today|yesterday|current|open|recent|last\s+\d+\s+days?)\b", re.IGNORECASE)
_CONTEXT_FOLLOWUP_RE = re.compile(
    r"\b(this|those|these|same issue|same store|listed categories|none are related)\b"
    r"|перечислен|эт(от|и|а)|так(ой|ие)\s+же",
    re.IGNORECASE,
)
_UNSUPPORTED_STRONG_CLAIM_RE = re.compile(
    r"\b(root cause|caused by|caused|sla|breach(?:ed)?|public reply|agent reply|customer risk|sentiment|tone|frustrat(?:ed|ion)"
    r"|happened on|occurred on|took place on|started on|began on|event date|fire date|confirmed (?:that )?date)\b",
    re.IGNORECASE,
)
_ANALYTICS_QUESTION_RE = re.compile(
    r"\b(count|how many|top|trend|compare|average|group by|analytics|tickets? logged|repeated stores?|repeated issues?)\b",
    re.IGNORECASE,
)
_TICKET_EXAMPLE_RE = re.compile(
    r"\b(ticket examples?|examples?\s+of\s+tickets?|similar tickets?|find tickets?|scenario|тикет)\b",
    re.IGNORECASE,
)
_CONTACT_TABLE_RE = re.compile(r"\b(contact|phone|email|table|spreadsheet|sheet|matrix|lookup|row)\b", re.IGNORECASE)


def automatic_validator_enabled() -> bool:
    """Return whether automatic validation is enabled for chat stream use."""
    return bool(getattr(settings, "universal_ai_validator_enabled", True))


def _contains_query_or_tool_error(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_QUERY_ERROR_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_query_or_tool_error(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_query_or_tool_error(v) for v in value)
    return False


def _count_rows(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        return 1 if value.strip() else 0
    if value:
        return 1
    return 0


def _source_families(context: dict[str, Any]) -> list[str]:
    families: list[str] = []
    tools = {str(tool) for tool in context.get("tools") or []}
    counts = context.get("counts") or {}

    if "run_bq_query" in tools or counts.get("bq", 0) > 0 or context.get("bq_rows") is not None:
        families.append("analytics")
    if any(tool in tools for tool in ("search_knowledge_base", "kb_search")) or counts.get("kb", 0) > 0:
        families.append("kb")
    if any(tool in tools for tool in ("search_drive_exact", "drive_search")) or counts.get("drive", 0) > 0:
        families.append("drive")
    if "search_similar_tickets" in tools or counts.get("similar", 0) > 0 or counts.get("direct_ticket_context", 0) > 0:
        families.append("tickets")
    if counts.get("zendesk_live_recent", 0) > 0 or counts.get("zendesk_temporal_tail", 0) > 0 or counts.get("zendesk_temporal_fallback", 0) > 0:
        families.append("zendesk_live")
    if counts.get("pos_type_profile", 0) > 0:
        families.append("profile_analytics")

    deduped: list[str] = []
    for family in families:
        if family not in deduped:
            deduped.append(family)
    return deduped


def build_answer_validation_context(
    *,
    candidate_answer: str,
    latest_question: str = "",
    routing_question: str = "",
    semantic_frame: dict[str, Any] | None = None,
    tools: list | tuple | None = None,
    phase1_decision: dict[str, Any] | None = None,
    policy_flags: dict[str, Any] | None = None,
    quality_gate_flags: dict[str, Any] | None = None,
    bq_sql: str = "",
    bq_rows: Any = None,
    kb_results: Any = None,
    drive_chunks: Any = None,
    similar_tickets: Any = None,
    live_zendesk_recent_tickets: Any = None,
    zendesk_temporal_tail_tickets: Any = None,
    zendesk_temporal_fallback_tickets: Any = None,
    direct_ticket_context: bool | int = False,
    sources: list[dict[str, Any]] | None = None,
    tickets: list[dict[str, Any]] | None = None,
    conversation_entities: dict[str, Any] | None = None,
    expected_source_family: str | None = None,
    available_fields: list[str] | tuple[str, ...] | None = None,
    risk_hints: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a compact, PII-minimized context bundle for risk detection/validation."""
    counts = {
        "bq": _count_rows(bq_rows),
        "kb": _count_rows(kb_results),
        "drive": _count_rows(drive_chunks),
        "similar": _count_rows(similar_tickets),
        "zendesk_live_recent": _count_rows(live_zendesk_recent_tickets),
        "zendesk_temporal_tail": _count_rows(zendesk_temporal_tail_tickets),
        "zendesk_temporal_fallback": _count_rows(zendesk_temporal_fallback_tickets),
        "direct_ticket_context": 1 if bool(direct_ticket_context) else 0,
        "sources": len(sources or []),
        "tickets": len(tickets or []),
    }
    if isinstance(quality_gate_flags, dict):
        for key in ("customer_risk", "ticket_semantic_search", "pos_type_profile"):
            value = quality_gate_flags.get(f"{key}_count")
            if isinstance(value, int):
                counts[key] = value

    context = {
        "candidate_answer": str(candidate_answer or ""),
        "latest_question": str(latest_question or ""),
        "routing_question": str(routing_question or latest_question or ""),
        "semantic_frame": semantic_frame or {},
        "tools": [str(tool) for tool in (tools or [])],
        "phase1_decision": phase1_decision or {},
        "policy_flags": policy_flags or {},
        "quality_gate_flags": quality_gate_flags or {},
        "bq_sql_present": bool(str(bq_sql or "").strip()),
        "bq_rows": bq_rows if isinstance(bq_rows, str) else None,
        "counts": counts,
        "source_families": [],
        "conversation_entities_present": bool(conversation_entities),
        "expected_source_family": (expected_source_family or "").strip(),
        "available_fields": [str(field) for field in (available_fields or [])],
        "risk_hints": [str(hint) for hint in (risk_hints or [])],
    }
    context["source_families"] = _source_families(context)
    context["signals"] = {
        "claims_no_data": bool(_NO_DATA_RE.search(context["candidate_answer"])),
        "query_or_tool_error": _contains_query_or_tool_error(bq_rows) or _contains_query_or_tool_error(candidate_answer),
        "raw_artifacts": bool(_RAW_LINE_RE.search(candidate_answer) or _TOOL_BLOCK_RE.search(candidate_answer)),
        "current_window_language": bool(_CURRENT_WINDOW_RE.search(f"{latest_question}\n{candidate_answer}")),
        "context_followup_language": bool(_CONTEXT_FOLLOWUP_RE.search(latest_question or routing_question or "")),
        "unsupported_strong_claim_language": bool(_UNSUPPORTED_STRONG_CLAIM_RE.search(candidate_answer)),
    }
    return context


def _add_risk(
    risks: list[str],
    reasons: dict[str, str],
    risk: str,
    reason: str,
) -> None:
    if risk not in _ALLOWED_RISKS:
        return
    if risk not in risks:
        risks.append(risk)
    reasons.setdefault(risk, reason)


def _quality_gate_resolved_risks(context: dict[str, Any]) -> set[str]:
    """Return risks already made safe by the deterministic quality gate."""
    flags = context.get("quality_gate_flags") or {}
    if not isinstance(flags, dict) or not flags.get("changed"):
        return set()

    action = str(flags.get("action") or "")
    if action == "replaced_query_error_no_data":
        return {"tool_error_as_absence", "false_no_data"}
    if action == "added_stale_or_incomplete_caveat":
        return {"stale_data"}
    return set()


def detect_answer_risks(context: dict[str, Any]) -> ChatAnswerRiskDetectionResult:
    """Pure deterministic risk detector. It never calls Gemini or performs I/O."""
    answer = str(context.get("candidate_answer") or "")
    question = str(context.get("latest_question") or context.get("routing_question") or "")
    counts = context.get("counts") or {}
    signals = context.get("signals") or {}
    source_families = list(context.get("source_families") or _source_families(context))
    risks: list[str] = []
    reasons: dict[str, str] = {}
    risk_hints = {str(hint) for hint in (context.get("risk_hints") or [])}

    if signals.get("raw_artifacts"):
        _add_risk(risks, reasons, "raw_artifact", "Candidate answer contains internal/tool/debug artifacts.")

    if signals.get("query_or_tool_error"):
        _add_risk(risks, reasons, "tool_error_as_absence", "Trace or answer contains a tool/query error signal.")

    positive_evidence = any(
        int(counts.get(key) or 0) > 0
        for key in ("bq", "kb", "drive", "similar", "zendesk_live_recent", "zendesk_temporal_tail", "zendesk_temporal_fallback", "direct_ticket_context", "sources", "tickets", "pos_type_profile")
    )
    if signals.get("claims_no_data") and (signals.get("query_or_tool_error") or positive_evidence):
        _add_risk(risks, reasons, "false_no_data", "No-data language conflicts with error or positive evidence signals.")

    stale_flags = context.get("policy_flags") or {}
    if (
        signals.get("claims_no_data")
        and signals.get("current_window_language")
        and (
            stale_flags.get("zendesk_live_recent_overlay_error")
            or stale_flags.get("zendesk_temporal_tail_error")
            or stale_flags.get("zendesk_temporal_fallback_error")
            or stale_flags.get("bq_stale")
            or "stale_data" in risk_hints
        )
    ):
        _add_risk(risks, reasons, "stale_data", "Fresh/current-window no-data answer has stale or incomplete source signals.")

    expected = str(context.get("expected_source_family") or "").strip()
    if expected and expected not in source_families:
        _add_risk(risks, reasons, "source_family_mismatch", f"Expected source family `{expected}` is absent from retrieved evidence.")
    elif _ANALYTICS_QUESTION_RE.search(question) and not any(f in source_families for f in ("analytics", "profile_analytics", "zendesk_live")) and source_families:
        _add_risk(risks, reasons, "source_family_mismatch", "Analytics-style question lacks analytics/profile/live ticket evidence.")
    elif _TICKET_EXAMPLE_RE.search(question) and not any(f in source_families for f in ("tickets", "zendesk_live", "analytics")) and source_families:
        _add_risk(risks, reasons, "source_family_mismatch", "Ticket-example question lacks ticket evidence.")
    elif _CONTACT_TABLE_RE.search(question) and "drive" not in source_families and source_families:
        _add_risk(risks, reasons, "source_family_mismatch", "Table/contact lookup lacks exact Drive/table evidence.")

    semantic = context.get("semantic_frame") if isinstance(context.get("semantic_frame"), dict) else {}
    task = semantic.get("task") if isinstance(semantic.get("task"), dict) else {}
    selected_executor = str((context.get("policy_flags") or {}).get("selected_executor") or "")
    if (
        ("profile_analytics" in source_families or selected_executor == "pos_type_profile_analytics")
        and str(semantic.get("intent") or "") == "general"
        and str(task.get("task_type") or "general_answer") == "general_answer"
    ):
        _add_risk(
            risks,
            reasons,
            "route_task_mismatch",
            "General troubleshooting was paired with profile analytics rather than a cohort/profile task.",
        )

    for hinted in ("evidence_mismatch", "denominator_drift"):
        if hinted in risk_hints:
            _add_risk(risks, reasons, hinted, f"Risk hint `{hinted}` was carried from deterministic trace policy.")
    if "weak_evidence" in risk_hints and not positive_evidence:
        _add_risk(risks, reasons, "weak_evidence", "Risk hint `weak_evidence` was carried from deterministic trace policy.")

    if (
        signals.get("context_followup_language")
        and not context.get("conversation_entities_present")
        and int(counts.get("direct_ticket_context") or 0) <= 0
    ):
        _add_risk(risks, reasons, "context_loss", "Follow-up wording depends on prior entities, but none are present.")

    if signals.get("unsupported_strong_claim_language"):
        fields = {str(field).lower() for field in (context.get("available_fields") or [])}
        strong_support = {
            "resolution_sla_status",
            "resolution_sla_breached",
            "last_public_comment_author_type",
            "customer_followups_after_last_agent",
            "customer_risk",
            "root_cause",
        }
        if not fields.intersection(strong_support):
            _add_risk(risks, reasons, "unsupported_strong_claim", "Strong SLA/public-comment/risk/root-cause language lacks supporting fields.")

    if answer and not positive_evidence and not signals.get("claims_no_data") and ("weak_evidence" in risk_hints or len(answer) > 160):
        _add_risk(risks, reasons, "weak_evidence", "Substantive answer has no positive evidence counts.")

    resolved_risks = _quality_gate_resolved_risks(context)
    if resolved_risks:
        risks = [risk for risk in risks if risk not in resolved_risks]
        reasons = {risk: reason for risk, reason in reasons.items() if risk not in resolved_risks}

    return ChatAnswerRiskDetectionResult(
        should_validate=bool(risks),
        risk_classes=risks,
        risk_reasons=reasons,
        source_families=source_families,
    )


def _compact_for_prompt(value: Any, limit: int = 18000) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "\n... [validator context truncated]"


def _parse_validator_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("chat_answer_validator JSON parse failed: %r", raw[:500])
        return {}


def _sanitize_rewrite(text: Any) -> str | None:
    clean = str(text or "").strip()
    if not clean:
        return None

    for marker in ("SOURCES_JSON:", "TICKETS_JSON:", "TRACE_JSON:"):
        idx = clean.find(marker)
        if idx != -1:
            clean = clean[:idx].strip()

    kept: list[str] = []
    skipping_tool_block = False
    for line in clean.splitlines():
        if _TOOL_BLOCK_RE.match(line):
            skipping_tool_block = True
            continue
        if skipping_tool_block:
            if not line.strip():
                skipping_tool_block = False
            continue
        if _RAW_LINE_RE.match(line):
            continue
        kept.append(line)

    clean = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return clean or None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_validator_data(data: dict, *, enabled: bool = True) -> ChatAnswerValidationResult:
    quality = str(data.get("quality") or "suspect").strip()
    action = str(data.get("recommended_action") or "final_with_caveat").strip()
    policy = str(data.get("answer_policy") or "caveated").strip()
    risks = data.get("risk_classes") or []
    if not isinstance(risks, list):
        risks = []

    normalized_action = action if action in _ALLOWED_ACTIONS else "final_with_caveat"
    rewrite = _sanitize_rewrite(data.get("rewrite_from_supplied_evidence"))
    if normalized_action not in _REWRITE_ALLOWED_ACTIONS:
        rewrite = None

    normalized_risks = [str(risk) for risk in risks if str(risk) in _ALLOWED_RISKS]
    if not normalized_risks and not data:
        normalized_risks = ["validator_error"]

    return ChatAnswerValidationResult(
        quality=quality if quality in _ALLOWED_QUALITY else "suspect",
        confidence=_safe_float(data.get("confidence"), 0.0),
        risk_classes=normalized_risks,
        recommended_action=normalized_action,
        answer_policy=policy if policy in _ALLOWED_POLICIES else "caveated",
        reason_short=str(data.get("reason_short") or "Validator returned no usable reason.").strip(),
        user_status=(str(data.get("user_status")).strip() if data.get("user_status") else None),
        rewrite_from_supplied_evidence=rewrite,
        validator_enabled=enabled,
    )


def _disabled_result() -> ChatAnswerValidationResult:
    return ChatAnswerValidationResult(
        quality="good",
        confidence=1.0,
        risk_classes=[],
        recommended_action="none",
        answer_policy="normal",
        reason_short="Automatic validator disabled by runtime config.",
        user_status=None,
        rewrite_from_supplied_evidence=None,
        validator_enabled=False,
    )


def validator_error_result(reason: str) -> ChatAnswerValidationResult:
    """Return a safe result when automatic validation itself fails."""
    return ChatAnswerValidationResult(
        quality="suspect",
        confidence=0.0,
        risk_classes=["validator_error"],
        recommended_action="final_with_caveat",
        answer_policy="caveated",
        reason_short=reason or "Automatic validator failed.",
        user_status=None,
        rewrite_from_supplied_evidence=None,
        validator_enabled=True,
    )


def _append_validation_caveat(answer: str) -> str:
    lower = (answer or "").lower()
    if "data limitation:" in lower or "bounded answer" in lower or "bounded check" in lower:
        return (answer or "").strip()
    caveat = (
        "Data limitation: this is a bounded answer from the checks completed so far; "
        "treat it as incomplete if the missing source/filter matters."
    )
    return f"{(answer or '').rstrip()}\n\n{caveat}".strip()


def apply_validation_result_to_answer(
    visible_answer: str,
    result: ChatAnswerValidationResult,
) -> tuple[str, dict[str, Any]]:
    """Apply safe immediate validator actions without running repair."""
    flags: dict[str, Any] = {
        "validator_enabled": result.validator_enabled,
        "validator_quality": result.quality,
        "validator_risk_classes": list(result.risk_classes),
        "validator_recommended_action": result.recommended_action,
        "validator_answer_policy": result.answer_policy,
        "validator_changed_answer": False,
    }
    if not result.validator_enabled or result.recommended_action == "none":
        return visible_answer, flags

    if (
        result.recommended_action in _REWRITE_ALLOWED_ACTIONS
        and result.rewrite_from_supplied_evidence
    ):
        flags["validator_changed_answer"] = result.rewrite_from_supplied_evidence.strip() != (visible_answer or "").strip()
        flags["validator_applied"] = "rewrite_from_supplied_evidence"
        return result.rewrite_from_supplied_evidence.strip(), flags

    if result.recommended_action == "final_with_caveat":
        updated = _append_validation_caveat(visible_answer)
        flags["validator_changed_answer"] = updated.strip() != (visible_answer or "").strip()
        flags["validator_applied"] = "final_with_caveat"
        return updated, flags

    flags["validator_applied"] = "deferred_repair"
    updated = _append_validation_caveat(visible_answer)
    flags["validator_changed_answer"] = updated.strip() != (visible_answer or "").strip()
    return updated, flags


def should_attempt_bounded_repair(result: ChatAnswerValidationResult) -> bool:
    """Return whether the automatic stream may attempt one read-only repair pass."""
    return bool(result.validator_enabled and result.recommended_action in _BOUNDED_REPAIR_ACTIONS)


def bounded_repair_review_seed(
    result: ChatAnswerValidationResult,
    *,
    latest_question: str = "",
) -> ChatQualityReviewResult:
    """Map a validator result into the existing Improve Answer refresh contract."""
    problems: list[str] = []
    for risk in result.risk_classes:
        problem = _REPAIR_RISK_TO_PROBLEM.get(str(risk))
        if problem and problem not in problems:
            problems.append(problem)
    if not problems:
        problems = ["weak_sources"]

    return ChatQualityReviewResult(
        quality="insufficient_evidence" if result.quality != "unsafe" else "likely_wrong",
        confidence=max(0.0, min(1.0, result.confidence)),
        problem_types=problems,
        recommended_action=_REPAIR_TO_REVIEW_ACTION.get(result.recommended_action, "reroute"),
        reformulated_query=(latest_question or "").strip() or None,
        explanation=f"Automatic validator requested bounded repair: {result.reason_short}",
        user_visible_note=(
            "The first answer needed a bounded source refresh before it could be shown safely."
        ),
        improved_answer=None,
    )


def validate_answer_candidate_with_telemetry(
    validation_context: dict[str, Any],
    *,
    enabled: bool | None = None,
) -> tuple[ChatAnswerValidationResult, dict[str, Any]]:
    """Run one bounded automatic validator call for a risky answer candidate."""
    if enabled is None:
        enabled = automatic_validator_enabled()
    if not enabled:
        return _disabled_result(), {
            "validator_enabled": False,
            "skipped_reason": "disabled_by_config",
            "thinking_budget": 0,
            "max_output_tokens": MAX_VALIDATOR_OUTPUT_TOKENS,
        }

    context_json = _compact_for_prompt(validation_context)
    prompt = _VALIDATOR_PROMPT.replace("{context_json}", context_json)
    profile = model_routing.resolve_surface_model_profile(
        settings,
        model_routing.ModelSurface.ANSWER_VALIDATOR,
    )
    generation_payload = model_routing.generation_config_payload(profile)
    generation_payload["response_mime_type"] = "application/json"
    estimated_tokens = estimate_tokens_from_text(prompt)
    estimated_cost = calc_cost(
        estimated_tokens,
        ESTIMATED_VALIDATOR_OUTPUT_TOKENS,
        call_type=CallType.UNIVERSAL_REVIEW,
    )
    reserved = get_gate_reserve(CallType.UNIVERSAL_REVIEW, estimated_cost)
    corr = new_correlation_id()
    telemetry: dict[str, Any] = {
        "validator_enabled": True,
        "model": profile.model_id,
        **model_routing.telemetry_payload(profile, candidate_mode="baseline"),
        "correlation_id": corr,
        "estimated_input_tokens": estimated_tokens,
        "estimated_output_tokens": ESTIMATED_VALIDATOR_OUTPUT_TOKENS,
        "estimated_cost_cad": estimated_cost,
        "reserved_cost_cad": reserved,
        "thinking_budget": profile.thinking_budget,
        "thinking_level": profile.thinking_level,
        "max_output_tokens": MAX_VALIDATOR_OUTPUT_TOKENS,
    }
    check_cost_gate(CallType.UNIVERSAL_REVIEW, reserved, fail_closed=True)
    record_estimate(
        CallType.UNIVERSAL_REVIEW,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=ESTIMATED_VALIDATOR_OUTPUT_TOKENS,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    model = _model if profile.model_id == settings.gemini_model else _model_factory(profile.model_id)
    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig.from_dict(generation_payload),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        t_in = getattr(usage, "prompt_token_count", 0) or 0
        t_out = getattr(usage, "candidates_token_count", 0) or 0
        t_think = getattr(usage, "thoughts_token_count", 0) or 0
        actual_cost = record_usage(
            CallType.UNIVERSAL_REVIEW,
            t_in,
            t_out,
            t_think,
            correlation_id=corr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        telemetry.update({
            "actual_input_tokens": t_in,
            "actual_output_tokens": t_out,
            "actual_thinking_tokens": t_think,
            "actual_cost_cad": actual_cost,
        })
        check_divergence(
            estimated_cost,
            actual_cost,
            CallType.UNIVERSAL_REVIEW,
            t_think,
            thinking_budget_was_zero=True,
        )

    raw_text = response.text or ""
    telemetry["response_chars"] = len(raw_text)
    result = _normalize_validator_data(_parse_validator_json(raw_text), enabled=True)
    telemetry.update({
        "quality": result.quality,
        "risk_classes": result.risk_classes,
        "recommended_action": result.recommended_action,
        "answer_policy": result.answer_policy,
        "has_rewrite": bool(result.rewrite_from_supplied_evidence),
    })
    return result, telemetry


def validate_answer_candidate(validation_context: dict[str, Any]) -> ChatAnswerValidationResult:
    """Return only the validator result for callers that do not need telemetry."""
    result, _telemetry = validate_answer_candidate_with_telemetry(validation_context)
    return result
