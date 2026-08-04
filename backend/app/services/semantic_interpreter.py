"""Pre-routing semantic interpretation for Universal Chat."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import vertexai
from google.oauth2 import service_account
from vertexai.generative_models import GenerationConfig, GenerativeModel

from app.config import settings
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

logger = logging.getLogger(__name__)

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

_ALLOWED_TARGET_CONTEXT = {"open_ticket", "explicit_ticket", "store", "global", "prior_turn", "unclear"}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}

_PROMPT = """\
You are the pre-routing semantic interpreter for ESIP, the Northstar Support Operations assistant.
You do not answer the user. You normalize the latest operator question so backend routing can
understand it even when the user writes in another language, with typos, missing words, or informal grammar.

Return one JSON object only. No markdown.

TODAY: {today} (America/New_York)

Rules:
- Always write normalized_question_en in English.
- normalized_question_en must be a complete, ideal version of the operator's latest question.
- Preserve the operator's intent. Do not add facts that are not in the latest question, conversation, or UI state.
- If the operator says "this ticket", "current ticket", "this issue", "this restaurant", or the same idea in any language,
  and there is an open ticket, set target_context to "open_ticket".
- If the operator asks for a specific store, date range, counts, top issues, or analytics, set target_context to "store" or "global"
  as appropriate; do not let the open ticket pollute independent analytics questions.
- Set target_context to "store" only when the latest question or UI/conversation context names a concrete store,
  current/open restaurant, or same-store scope. Generic phrases like "restaurant log", "a restaurant", or "any ticket"
  without a store id/name are global ticket searches.
- For short follow-ups with pronouns such as it, this, that, those, top store, or same issue, inherit the relevant metric,
  issue/category, store, and time window from recent conversation. Set target_context to "prior_turn" unless the open ticket
  itself is needed.
- When inheriting issue/category names from history, rewrite them as natural English words. For example,
  "Server --> Hard Drive" becomes "Server Hard Drive issues" in normalized_question_en; do not make it look like an exact
  database value unless the operator literally typed that value.
- If the operator asks whether something happened at this/current restaurant, classify it as an evidence-bound fact check.
  Do not hardcode or special-case incident words such as fire, flood, outage, or restoration.
- If the latest turn asks to count or group items by a dimension, fill task. Use task_type "count_by_dimension".
  Use dimension_type "semantic_labels" when the requested buckets are descriptive labels from the current question or prior
  answer, such as degraded speed, lagging Wi-Fi, dropping connections, or outage labels. Use dimension_type
  "issue_category" only when the user asks for official Zendesk issue_category values.
- For follow-ups like "each listed category", "those categories", or the same idea in another language, infer labels from
  recent conversation when they are visible and set dimension_source to "prior_turn".
- If the operator asks to find, search, check, or list tickets matching a described scenario, set intent to
  "ticket_retrieval" and fill retrieval. Preserve vendor, device, and action concepts as first-class fields.
  Examples: find a ticket where FiberLink sent a technician to connect a new modem; check tickets since 2023
  mentioning a Telecom Partner fiber-line upgrade; find old tickets about a device replacement.
- In retrieval.required_concepts, include concepts that must be present for a strong match. Do not put generic
  command words like "find", "check", "ticket", "log", or "restaurant" there unless they are part of the actual scenario.
- In retrieval.optional_concepts, include broader recall terms that can help find related tickets but are not enough by
  themselves. Keep vendors, devices, and actions in their own arrays too.
- If the user did not literally type a ticket id, do not add the open ticket id into normalized_question_en. Say "the currently open ticket".
- Preserve answer_language as the language the final answer should use. Use "English", "Russian", "French", "Spanish",
  "Chinese", "Hindi", or another clear English language name.
- entities must contain only safe routing hints. Use null for unknown values. If an id only comes from UI state, put it in
  open_ticket_id, not explicit_ticket_id.
- If the latest question names multiple concrete store/location IDs, put all of them in entities.store_ids.
  Use entities.store_id only when there is exactly one concrete store.
- needs_clarification is true only when routing cannot reasonably infer what the operator means.

Allowed intent values:
- current_ticket_context
- ticket_details
- event_fact_check
- next_steps
- store_analytics
- temporal_analytics
- procedure_lookup
- similar_cases
- ticket_retrieval
- row_provenance
- prior_answer_challenge
- general
- clarification

Conversation history before latest turn:
{history}

UI state:
{ui_state}

Latest operator question:
{question}

Return JSON with exactly these keys:
{{
  "normalized_question_en": "complete English question",
  "answer_language": "English language name",
  "intent": "one allowed intent value",
  "target_context": "open_ticket | explicit_ticket | store | global | prior_turn | unclear",
  "entities": {{
    "explicit_ticket_id": null,
    "open_ticket_id": null,
    "store_id": null,
    "store_ids": [],
    "date_phrase": null,
    "issue_or_category": null
  }},
  "task": {{
    "task_type": "general_answer | count_by_dimension | retrieve_context | compare_periods | fact_check",
    "dimension_type": null,
    "dimension_source": null,
    "labels": [],
    "overlap_allowed": null
  }},
  "retrieval": {{
    "search_goal": null,
    "required_concepts": [],
    "optional_concepts": [],
    "vendors": [],
    "devices": [],
    "actions": [],
    "output": "ticket_examples | ticket_ids | evidence_summary | null"
  }},
  "introduced_entities": {{
    "semantic_labels": []
  }},
  "confidence": "high | medium | low",
  "needs_clarification": false,
  "reasoning_short": "one short sentence"
}}
"""


@dataclass(frozen=True)
class SemanticInterpretation:
    normalized_question_en: str
    answer_language: str = "English"
    intent: str = "general"
    target_context: str = "global"
    entities: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    retrieval: dict[str, Any] = field(default_factory=dict)
    introduced_entities: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"
    needs_clarification: bool = False
    reasoning_short: str = ""
    status: str = "ok"
    fallback_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def interpret_latest_turn(
    messages: list[dict],
    *,
    open_ticket_id: int | None = None,
    open_ticket_metadata: dict[str, Any] | None = None,
) -> SemanticInterpretation:
    """Normalize the latest user turn before deterministic Universal Chat routing."""
    raw_question = _latest_question(messages)
    if not raw_question:
        return _fallback("", reason="empty_question")

    prompt = _PROMPT.format(
        today=_today_business_time_label(),
        history=_history_block(messages),
        ui_state=_ui_state_block(open_ticket_id, open_ticket_metadata),
        question=raw_question,
    )

    try:
        estimated_tokens = estimate_tokens_from_text(prompt)
        estimated_output_tokens = 512
        estimated_cost = calc_cost(
            estimated_tokens,
            estimated_output_tokens,
            call_type=CallType.UNIVERSAL_SEMANTIC,
        )
        reserved = get_gate_reserve(CallType.UNIVERSAL_SEMANTIC, estimated_cost)
        corr = new_correlation_id()
        check_cost_gate(CallType.UNIVERSAL_SEMANTIC, reserved, fail_closed=False)
        record_estimate(
            CallType.UNIVERSAL_SEMANTIC,
            estimated_cost,
            corr,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=estimated_output_tokens,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
        )

        response = _model.generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict(
                {
                    "temperature": 0.0,
                    "max_output_tokens": 1024,
                    "response_mime_type": "application/json",
                    "thinking_config": {"thinking_budget": 0},
                }
            ),
        )
        usage = getattr(response, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual_cost = record_usage(
                CallType.UNIVERSAL_SEMANTIC,
                t_in,
                t_out,
                t_think,
                correlation_id=corr,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )
            check_divergence(
                estimated_cost,
                actual_cost,
                CallType.UNIVERSAL_SEMANTIC,
                t_think,
                thinking_budget_was_zero=True,
            )

        data = json.loads(response.text)
        return _validate(data, raw_question, open_ticket_id=open_ticket_id)
    except Exception as exc:
        logger.warning("semantic_interpreter failed, falling back to raw routing: %s", exc)
        return _fallback(raw_question, reason=str(exc))


def build_routing_messages(messages: list[dict], interpretation: SemanticInterpretation) -> list[dict]:
    """Return a copy of messages with the latest user turn replaced by normalized English."""
    if not messages:
        return []
    routing_messages = [dict(m) for m in messages]
    normalized = (interpretation.normalized_question_en or "").strip()
    if normalized:
        routing_messages[-1]["content"] = normalized
    return routing_messages


def _validate(data: dict[str, Any], raw_question: str, *, open_ticket_id: int | None) -> SemanticInterpretation:
    normalized = _clean_text(data.get("normalized_question_en"), max_len=900) or raw_question
    answer_language = _clean_text(data.get("answer_language"), max_len=40) or _fallback_language(raw_question)
    intent = _clean_text(data.get("intent"), max_len=80) or "general"
    target_context = _clean_text(data.get("target_context"), max_len=40) or "global"
    confidence = _clean_text(data.get("confidence"), max_len=20) or "low"
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    store_ids = _safe_store_id_list(entities.get("store_ids"), max_items=20)
    legacy_store_ids = _safe_store_id_list(entities.get("store_id"), max_items=20)
    for store_id in legacy_store_ids:
        if store_id not in store_ids:
            store_ids.append(store_id)
    single_store_id = store_ids[0] if len(store_ids) == 1 else None
    safe_entities = {
        "explicit_ticket_id": _safe_int_or_none(entities.get("explicit_ticket_id")),
        "open_ticket_id": _safe_int_or_none(entities.get("open_ticket_id")) or open_ticket_id,
        "store_id": single_store_id,
        "store_ids": store_ids,
        "date_phrase": _clean_text(entities.get("date_phrase"), max_len=120) or None,
        "issue_or_category": _clean_text(entities.get("issue_or_category"), max_len=180) or None,
    }
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    safe_task = {
        "task_type": _clean_text(task.get("task_type"), max_len=80) or "general_answer",
        "dimension_type": _clean_text(task.get("dimension_type"), max_len=80) or None,
        "dimension_source": _clean_text(task.get("dimension_source"), max_len=80) or None,
        "labels": _safe_string_list(task.get("labels"), max_items=10, max_len=120),
        "overlap_allowed": task.get("overlap_allowed") if isinstance(task.get("overlap_allowed"), bool) else None,
    }
    retrieval = data.get("retrieval") if isinstance(data.get("retrieval"), dict) else {}
    safe_retrieval = {
        "search_goal": _clean_text(retrieval.get("search_goal"), max_len=240) or None,
        "required_concepts": _safe_string_list(retrieval.get("required_concepts"), max_items=12, max_len=120),
        "optional_concepts": _safe_string_list(retrieval.get("optional_concepts"), max_items=16, max_len=120),
        "vendors": _safe_string_list(retrieval.get("vendors"), max_items=8, max_len=80),
        "devices": _safe_string_list(retrieval.get("devices"), max_items=8, max_len=80),
        "actions": _safe_string_list(retrieval.get("actions"), max_items=8, max_len=120),
        "output": _clean_text(retrieval.get("output"), max_len=80) or None,
    }
    introduced = data.get("introduced_entities") if isinstance(data.get("introduced_entities"), dict) else {}
    safe_introduced = {
        "semantic_labels": _safe_string_list(introduced.get("semantic_labels"), max_items=10, max_len=120),
    }
    if target_context not in _ALLOWED_TARGET_CONTEXT:
        target_context = "unclear"
    if confidence not in _ALLOWED_CONFIDENCE:
        confidence = "low"
    return SemanticInterpretation(
        normalized_question_en=normalized,
        answer_language=answer_language,
        intent=intent,
        target_context=target_context,
        entities=safe_entities,
        task=safe_task,
        retrieval=safe_retrieval,
        introduced_entities=safe_introduced,
        confidence=confidence,
        needs_clarification=bool(data.get("needs_clarification")),
        reasoning_short=_clean_text(data.get("reasoning_short"), max_len=240),
    )


def _fallback(raw_question: str, *, reason: str) -> SemanticInterpretation:
    return SemanticInterpretation(
        normalized_question_en=raw_question,
        answer_language=_fallback_language(raw_question),
        intent="general",
        target_context="global",
        confidence="low",
        status="fallback",
        fallback_reason=reason[:500],
    )


def _latest_question(messages: list[dict]) -> str:
    if not messages:
        return ""
    return str((messages[-1] or {}).get("content") or "")


def _history_block(messages: list[dict]) -> str:
    recent = messages[-6:] if len(messages) > 6 else messages
    lines: list[str] = []
    for msg in recent[:-1]:
        role = "User" if msg.get("role") == "user" else "Assistant"
        content = str(msg.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}: {content[:500]}")
    return "\n".join(lines) if lines else "(first message)"


def _ui_state_block(open_ticket_id: int | None, metadata: dict[str, Any] | None) -> str:
    if not open_ticket_id:
        return "No open ticket in the left panel."
    lines = [f"open_ticket_id: {open_ticket_id}"]
    if metadata:
        for key in ("subject", "store_id", "org_name", "brand", "issue_category", "status", "created_at"):
            value = metadata.get(key)
            if value not in (None, ""):
                lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _today_business_time_label() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %d, %Y")


def _clean_text(value: Any, *, max_len: int) -> str:
    text = str(value or "").strip()
    return text[:max_len]


def _safe_int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_string_list(value: Any, *, max_items: int, max_len: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, max_len=max_len)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _safe_store_id_list(value: Any, *, max_items: int) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.findall(r"\b\d{3,6}\b", str(value or ""))

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        match = re.fullmatch(r"\d{3,6}", text)
        if not match or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _fallback_language(text: str) -> str:
    return "Russian" if any("\u0400" <= ch <= "\u04ff" for ch in text or "") else "English"
