"""
Optional second-pass quality review for Universal Chat answers.

This layer reviews an already generated answer using the saved answer trace.
It does not change Universal Chat routing or run new retrieval by itself.
"""

import json
import logging
import re
from typing import Any

from app.services.genai_transport import GenerationConfig, BoundModel

from app.config import settings
from app.models.chat_quality import ChatQualityReviewResult
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

_model = BoundModel(settings.gemini_model, surface="chat_quality")
_model_factory = BoundModel
DEFAULT_ESCALATION_MODEL = "gemini-3.5-flash"

_REVIEW_PROMPT = """\
You are a senior QA reviewer for ESIP Universal Chat answers.
The engineer clicked "Improve answer"; they need a corrected answer, not just a quality rating.

Your task:
1. Review whether the assistant answer is well-supported by the saved trace.
2. Identify routing, retrieval, source, or reasoning problems.
3. Improve the answer when it is weak, unsupported, wrong, incomplete, or too generic.
4. If the evidence is insufficient, write a safer corrected answer that says what cannot be
   supported and what should be rerun or clarified.

Do not invent facts. Do not use outside knowledge. Do not claim a source says something unless it is present in the trace.
If the first answer is good, improved_answer may repeat the supported final answer or add a very small clarification.
If the first answer is not good, a quality label alone is not enough. In that case, improved_answer
MUST contain a complete engineer-facing replacement answer using ONLY saved trace evidence. If the
trace evidence is insufficient, improved_answer MUST still contain a safer replacement that retracts
unsupported claims and states the evidence gap. Use the same language as the user's latest message.

Return JSON only with this exact shape:
{
  "quality": "good | weak | likely_wrong | insufficient_evidence",
  "confidence": 0.0,
  "problem_types": [
    "wrong_routing",
    "weak_sources",
    "obsolete_source",
    "conflicting_sources",
    "ambiguous_question",
    "ignored_negative_constraint",
    "answer_too_generic"
  ],
  "recommended_action": "keep | ask_clarification | reroute | run_semantic_plus_exact | rewrite_with_existing_sources",
  "reformulated_query": "optional better retrieval query or null",
  "explanation": "internal concise explanation of what is wrong or why it is supported",
  "user_visible_note": "short plain-language note for the engineer",
  "improved_answer": "better answer for the engineer, or null"
}

Saved trace:
{trace_json}

Engineer note:
{user_note}
"""


def _compact_for_prompt(value: Any, limit: int = 18000) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "\n... [trace truncated]"


def _parse_review_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("chat_quality review JSON parse failed: %r", raw[:500])
        return {
            "quality": "weak",
            "confidence": 0.2,
            "problem_types": ["answer_too_generic"],
            "recommended_action": "ask_clarification",
            "reformulated_query": None,
            "explanation": "The review model did not return valid JSON.",
            "user_visible_note": "I could not complete a structured review of this answer.",
            "improved_answer": None,
        }


def _normalize_review_data(data: dict) -> dict:
    allowed_quality = {"good", "weak", "likely_wrong", "insufficient_evidence"}
    allowed_problems = {
        "wrong_routing",
        "weak_sources",
        "obsolete_source",
        "conflicting_sources",
        "ambiguous_question",
        "ignored_negative_constraint",
        "answer_too_generic",
    }
    allowed_actions = {
        "keep",
        "ask_clarification",
        "reroute",
        "run_semantic_plus_exact",
        "rewrite_with_existing_sources",
    }
    quality = str(data.get("quality") or "weak").strip()
    action = str(data.get("recommended_action") or "ask_clarification").strip()
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    problems = data.get("problem_types") or []
    if not isinstance(problems, list):
        problems = []

    return {
        "quality": quality if quality in allowed_quality else "weak",
        "confidence": min(1.0, max(0.0, confidence)),
        "problem_types": [str(p) for p in problems if str(p) in allowed_problems],
        "recommended_action": action if action in allowed_actions else "ask_clarification",
        "reformulated_query": data.get("reformulated_query") or None,
        "explanation": str(data.get("explanation") or "No explanation returned."),
        "user_visible_note": str(data.get("user_visible_note") or "The answer was reviewed."),
        "improved_answer": data.get("improved_answer") or None,
    }


def _latest_user_text(trace: dict) -> str:
    messages = trace.get("messages") or []
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or "")
    return ""


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _strip_answer_payloads(text: str) -> str:
    """Remove hidden JSON payloads if a saved trace ever contains them."""
    clean = str(text or "")
    for marker in ("\n\nSOURCES_JSON", "\n\nTICKETS_JSON", "\n\nTRACE_JSON"):
        if marker in clean:
            clean = clean.split(marker, 1)[0]
    return clean.strip()


def _original_visible_answer(trace: dict) -> str:
    return _strip_answer_payloads(trace.get("final_answer") or trace.get("answer") or "")


def _fallback_improved_answer(trace: dict, result: ChatQualityReviewResult) -> str:
    """Return a safe replacement when the review model flags a problem but omits a rewrite."""
    latest = _latest_user_text(trace)
    note = (result.user_visible_note or result.explanation or "").strip()
    if _has_cyrillic(latest):
        base = (
            "Я перепроверил исходный ответ по сохраненной трассе и не могу подтвердить его "
            "как надежный из доступных данных."
        )
        if note:
            base += f" {note}"
        if result.recommended_action in {"reroute", "run_semantic_plus_exact"}:
            base += (
                " Нужен повторный поиск/маршрутизация по источникам, а не уверенный вывод "
                "из прежнего ответа."
            )
        elif result.recommended_action == "ask_clarification":
            base += " Нужен уточняющий вопрос или дополнительная проверка источников."
        return base

    base = (
        "I rechecked the original answer against the saved trace and cannot treat it as reliably "
        "supported by the available evidence."
    )
    if note:
        base += f" {note}"
    if result.recommended_action in {"reroute", "run_semantic_plus_exact"}:
        base += (
            " The safer next step is to rerun retrieval against the relevant sources instead "
            "of keeping the original claim."
        )
    elif result.recommended_action == "ask_clarification":
        base += " The safer next step is to ask a clarification or check additional sources."
    return base


def _confirmed_original_answer(trace: dict, result: ChatQualityReviewResult) -> str:
    """Return a user-facing answer for good reviews so the UI never shows stats only."""
    original = _original_visible_answer(trace)
    if original:
        return original
    latest = _latest_user_text(trace)
    note = (result.user_visible_note or result.explanation or "").strip()
    if _has_cyrillic(latest):
        return f"Я перепроверил ответ по сохраненной трассе. {note or 'Ответ выглядит подтвержденным доступными данными.'}".strip()
    return f"I rechecked the answer against the saved trace. {note or 'The answer is supported by the available evidence.'}".strip()


def _ticket_reference(trace: dict, *texts: str) -> str:
    ticket_id = trace.get("target_ticket_id") or trace.get("ticket_id")
    if ticket_id:
        return f"#{ticket_id}"
    for text in texts:
        match = re.search(r"#?(\d{5,8})", str(text or ""))
        if match:
            return f"#{match.group(1)}"
    return "this ticket"


def _looks_like_sla_gap(trace: dict, result: ChatQualityReviewResult) -> bool:
    latest = _latest_user_text(trace)
    original = _original_visible_answer(trace)
    combined = f"{latest}\n{original}\n{result.improved_answer or ''}".lower()
    if not re.search(r"\bsla\b|service level agreement", combined):
        return False
    gap_terms = (
        "does not include",
        "do not include",
        "not include",
        "not available",
        "no sla",
        "sla information",
        "sla performance",
        "sla targets",
    )
    return any(term in combined for term in gap_terms)


def _sla_gap_answer(trace: dict) -> str:
    latest = _latest_user_text(trace)
    ticket_ref = _ticket_reference(trace, latest, _original_visible_answer(trace))
    if _has_cyrillic(latest):
        return (
            f"В доступных ESIP данных по {ticket_ref} я не вижу официальных Zendesk SLA "
            "target/breach полей. ESIP может проверить timing-сигналы, которые уже есть в "
            "тикете: created time, текущий status, public update/reply timing и resolution time "
            "когда он доступен. Но это не то же самое, что официальный SLA. Чтобы отвечать про "
            "SLA target или breach status надежно, нам нужно дополнительно забирать Zendesk SLA "
            "или ticket metric fields."
        )
    return (
        f"I do not see official Zendesk SLA target or breach data for {ticket_ref} in the "
        "available ESIP ticket details. ESIP can check timing signals we do have, such as "
        "created time, current status, public update/reply timing, and resolution time when "
        "available. That is not the same as an official SLA. To answer SLA target or breach "
        "status reliably, we need to ingest the Zendesk SLA or ticket metric fields."
    )


def _improved_answer_looks_worse_for_live_zendesk(result: ChatQualityReviewResult) -> bool:
    answer = (result.improved_answer or "").lower()
    if not answer:
        return False
    degraded_phrases = (
        "couldn't retrieve from analytics database",
        "could not retrieve from analytics database",
        "couldn't retrieve from the analytics database",
        "could not retrieve from the analytics database",
    )
    return "__" in answer or any(phrase in answer for phrase in degraded_phrases)


def _looks_like_live_zendesk_answer(trace: dict) -> bool:
    final_answer = _original_visible_answer(trace).lower()
    if "live zendesk" in final_answer:
        return True
    counts = trace.get("counts") or {}
    return bool(counts.get("zendesk_live_recent") or trace.get("live_zendesk_recent_tickets"))


def _apply_review_overrides(
    trace: dict,
    result: ChatQualityReviewResult,
) -> tuple[ChatQualityReviewResult, bool]:
    """Apply deterministic product-safety overrides around the review model."""
    if _looks_like_sla_gap(trace, result):
        return _copy_review_result(
            result,
            quality="weak",
            confidence=min(result.confidence or 0.75, 0.75),
            problem_types=["weak_sources"],
            recommended_action="rewrite_with_existing_sources",
            reformulated_query=None,
            explanation=(
                "The saved trace does not contain official Zendesk SLA target or breach fields. "
                "A plain no-data answer is technically true but not useful enough for the engineer."
            ),
            user_visible_note="ESIP has timing signals, but not official SLA target/breach fields in this trace.",
            improved_answer=_sla_gap_answer(trace),
        ), True

    if _looks_like_live_zendesk_answer(trace) and (
        result.quality == "good" or _improved_answer_looks_worse_for_live_zendesk(result)
    ):
        return _copy_review_result(
            result,
            quality="good",
            confidence=max(result.confidence or 0.0, 0.85),
            problem_types=[],
            recommended_action="keep",
            reformulated_query=None,
            explanation="The saved trace includes a live Zendesk answer; preserve the deterministic answer.",
            user_visible_note="I rechecked the saved trace; the live Zendesk answer is supported.",
            improved_answer=_confirmed_original_answer(trace, result),
        ), True

    return result, False


def _copy_review_result(result: ChatQualityReviewResult, **updates) -> ChatQualityReviewResult:
    if hasattr(result, "model_copy"):
        return result.model_copy(update=updates)
    return result.copy(update=updates)


def _ensure_improved_answer(
    trace: dict,
    result: ChatQualityReviewResult,
) -> tuple[ChatQualityReviewResult, bool]:
    result, used_override = _apply_review_overrides(trace, result)
    if result.improved_answer:
        return result, used_override
    if result.quality == "good":
        return _copy_review_result(result, improved_answer=_confirmed_original_answer(trace, result)), True
    return _copy_review_result(result, improved_answer=_fallback_improved_answer(trace, result)), True


def _resolve_review_profile(*, use_escalation: bool) -> tuple[model_routing.ModelProfile, str]:
    if use_escalation and bool(getattr(settings, "universal_model_escalation_enabled", False)):
        escalation_model = (
            getattr(settings, "universal_model_escalation_model", "") or DEFAULT_ESCALATION_MODEL
        )
        return (
            model_routing.resolve_surface_model_profile(
                settings,
                model_routing.ModelSurface.REPAIR_ESCALATION,
                override_model_id=escalation_model,
            ),
            "escalation",
        )
    return (
        model_routing.resolve_surface_model_profile(
            settings,
            model_routing.ModelSurface.CHAT_QUALITY_REVIEW,
        ),
        "baseline",
    )


def review_answer_trace_with_telemetry(
    trace: dict,
    user_note: str | None = None,
    *,
    use_escalation: bool = False,
    escalation_reason: str | None = None,
) -> tuple[ChatQualityReviewResult, dict[str, Any]]:
    """Run a manual quality review and return private runtime telemetry."""
    trace_json = _compact_for_prompt(trace)
    prompt = (
        _REVIEW_PROMPT
        .replace("{trace_json}", trace_json)
        .replace("{user_note}", (user_note or "No additional note provided.").strip())
    )

    estimated_tokens = estimate_tokens_from_text(prompt)
    profile, candidate_mode = _resolve_review_profile(use_escalation=use_escalation)
    try:
        generation_payload = model_routing.generation_config_payload(profile)
        generation_payload["response_mime_type"] = "application/json"
        generation_config = GenerationConfig.from_dict(generation_payload)
    except Exception as exc:
        if use_escalation:
            result, fallback_telemetry = review_answer_trace_with_telemetry(
                trace,
                user_note,
                use_escalation=False,
                escalation_reason=escalation_reason,
            )
            fallback_telemetry["fallback_reason"] = "escalation_model_config_error"
            fallback_telemetry["escalation_error"] = str(exc)
            fallback_telemetry["escalation_attempted"] = True
            return result, fallback_telemetry
        raise
    estimated_cost = calc_cost(estimated_tokens, 700, call_type=CallType.UNIVERSAL_REVIEW)
    reserved = get_gate_reserve(CallType.UNIVERSAL_REVIEW, estimated_cost)
    corr = new_correlation_id()
    telemetry: dict[str, Any] = {
        "model": profile.model_id,
        **model_routing.telemetry_payload(
            profile,
            candidate_mode=candidate_mode,
            escalation_reason=escalation_reason,
        ),
        "escalation_attempted": bool(use_escalation and candidate_mode == "escalation"),
        "correlation_id": corr,
        "estimated_input_tokens": estimated_tokens,
        "estimated_output_tokens": 700,
        "estimated_cost_cad": estimated_cost,
        "reserved_cost_cad": reserved,
        "thinking_budget": profile.thinking_budget,
        "thinking_level": profile.thinking_level,
        "max_output_tokens": 4096,
    }
    try:
        check_cost_gate(CallType.UNIVERSAL_REVIEW, reserved, fail_closed=True)
    except Exception as exc:
        if use_escalation:
            result, fallback_telemetry = review_answer_trace_with_telemetry(
                trace,
                user_note,
                use_escalation=False,
                escalation_reason=escalation_reason,
            )
            fallback_telemetry["fallback_reason"] = "escalation_cost_gate"
            fallback_telemetry["escalation_error"] = str(exc)
            fallback_telemetry["escalation_attempted"] = True
            return result, fallback_telemetry
        raise
    record_estimate(
        CallType.UNIVERSAL_REVIEW,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=700,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    try:
        model = _model if profile.model_id == settings.gemini_model else _model_factory(profile.model_id)
        response = model.generate_content(
            prompt,
            generation_config=generation_config,
        )
    except Exception as exc:
        if use_escalation:
            result, fallback_telemetry = review_answer_trace_with_telemetry(
                trace,
                user_note,
                use_escalation=False,
                escalation_reason=escalation_reason,
            )
            fallback_telemetry["fallback_reason"] = "escalation_provider_error"
            fallback_telemetry["escalation_error"] = str(exc)
            fallback_telemetry["escalation_attempted"] = True
            return result, fallback_telemetry
        raise

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
    result = ChatQualityReviewResult(**_normalize_review_data(_parse_review_json(raw_text)))
    result, used_fallback = _ensure_improved_answer(trace, result)
    if used_fallback:
        telemetry["fallback_improved_answer"] = True
    return result, telemetry


def review_answer_trace(trace: dict, user_note: str | None = None) -> ChatQualityReviewResult:
    """Run a manual quality review for one saved Universal Chat answer trace."""
    result, _telemetry = review_answer_trace_with_telemetry(trace, user_note)
    return result
