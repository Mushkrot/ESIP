"""
Universal Chat router.

POST /api/universal/stream
  — Single streaming chat endpoint that handles ticket lookup, KB search,
    Drive document summarization, and analytics queries in one mode.
  — Uses JSON mode Phase 1 (deterministic routing) instead of AUTO function-calling.
  — Returns SSE events identical to other chat endpoints.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.dependencies import AnyUser
from app.models.chat_quality import ChatAnswerRiskDetectionResult, ChatQualityReviewRequest, ChatQualityReviewResult
from app.services import (
    ai_dispatcher_result_sets,
    ai_dispatcher_runtime,
    bigquery,
    chat_answer_validator,
    chat_logging,
    model_transition_request_capture,
    model_transition_capture_admin,
    chat_policy,
    chat_quality,
    chat_repair_loop,
    customer_risk_analytics,
    error_provenance,
    followup_date_scope_assist,
    followup_result_set_assist,
    followup_understanding_shadow,
    firestore,
    output_critic_bounded_repair,
    output_critic_shadow,
    pos_type_analytics,
    reference_runtime,
    semantic_contract_v2,
    semantic_interpreter,
    semantic_label_counts,
    semantic_task_contracts,
    temporal_resolver,
    ticket_resolution_lookup_assist,
    universal_answer_now,
    universal_chat,
    vertex_search,
)
from app.services.kb_retrieval_context import log_kb_status

logger = logging.getLogger(__name__)
router = APIRouter()
LIVE_ZENDESK_RECENT_LIMIT = 500
STATUS_CHECKING_TICKET_CONTEXT = "Checking ticket context..."
STATUS_SEARCHING_PROCEDURES = "Searching indexed procedures..."
STATUS_CHECKING_ANALYTICS = "Checking analytics..."
STATUS_CHECKING_ANOTHER_SOURCE = "The first result looks incomplete, checking another source..."
STATUS_PREPARING_ANSWER = "Preparing the best available answer..."
_DRIVE_HINT_BLOCKED_EXECUTORS = frozenset({
    "customer_risk_analytics",
    "direct_ticket_context",
    "event_discovery",
    "pos_type_profile_analytics",
    "public_update_analytics",
    "schema_limited_analytics",
    "semantic_label_count",
    "ticket_analytics",
    "ticket_semantic_search",
})
UNIVERSAL_STAGE_NAMES = (
    "interpretation",
    "routing_phase1",
    "retrieval",
    "answer_production",
    "validator_repair",
    "output_critic",
    "persistence",
)


class UniversalStageTimings:
    """Record allowlisted monotonic stage durations without doing I/O."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started: dict[str, float] = {}
        self._durations_ms: dict[str, int] = {}

    def start(self, stage: str) -> None:
        if stage in UNIVERSAL_STAGE_NAMES and stage not in self._started:
            self._started[stage] = self._clock()

    def finish(self, stage: str) -> None:
        started = self._started.pop(stage, None)
        if stage not in UNIVERSAL_STAGE_NAMES or started is None:
            return
        self._durations_ms[stage] = max(0, int(round((self._clock() - started) * 1000)))

    def snapshot(self) -> dict[str, Any]:
        durations = {
            stage: self._durations_ms[stage]
            for stage in UNIVERSAL_STAGE_NAMES
            if stage in self._durations_ms
        }
        missing = [stage for stage in UNIVERSAL_STAGE_NAMES if stage not in durations]
        return {
            "version": "universal_stage_timings.v1",
            "durations_ms": durations,
            "missing_stages": missing,
            "partial": bool(missing),
        }


def _sse(payload: dict) -> str:
    """Format an additive SSE payload for Universal Chat clients."""
    return f"data: {json.dumps(payload)}\n\n"


def _status_payload(status: str, *, trace_id: str, turn_id: str, conversation_id: str) -> dict:
    return {
        "status": status,
        "trace_id": trace_id,
        "turn_id": turn_id,
        "conversation_id": conversation_id,
    }


def _model_to_plain_dict(value) -> dict:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value) if isinstance(value, dict) else {}


def _drive_hint_policy(
    *,
    any_drive_search_enabled: bool,
    force_drive_tabular: bool,
    selected_executor: str,
    tools: list[str] | tuple[str, ...] | None = None,
    has_explicit_drive_lookup: bool = False,
) -> tuple[bool, str | None]:
    """Decide whether the fallback Drive hint lookup may run."""
    if not any_drive_search_enabled:
        return False, "drive_disabled"
    if not force_drive_tabular:
        return False, "not_drive_tabular"
    tool_set = {str(tool) for tool in (tools or [])}
    if "search_drive_exact" in tool_set and has_explicit_drive_lookup:
        return False, "explicit_drive_lookup_present"
    executor = str(selected_executor or "").strip()
    if executor in _DRIVE_HINT_BLOCKED_EXECUTORS:
        return False, executor
    return True, None


def _build_followup_understanding_shadow_payload(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any],
    previous_assistant_text: str,
    conversation_entities: dict | None,
    messages: list[dict],
) -> dict[str, Any]:
    contract = followup_understanding_shadow.build_followup_understanding_shadow(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
        conversation_entities=conversation_entities,
        messages=messages,
    )
    return contract.to_dict()


def _build_followup_date_scope_assist_payload(
    *,
    shadow_payload: dict[str, Any],
    latest_question: str,
    routing_question: str,
    previous_assistant_text: str,
    conversation_entities: dict | None,
    messages: list[dict],
) -> dict[str, Any]:
    contract = followup_date_scope_assist.build_followup_date_scope_assist(
        shadow_payload=shadow_payload,
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text=previous_assistant_text,
        conversation_entities=conversation_entities,
        messages=messages,
    )
    return contract.to_dict()


def _build_ticket_resolution_lookup_assist_payload(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any],
    explicit_ticket_id: int | None,
    target_ticket_id: int | None,
    open_ticket_id: int | None,
) -> dict[str, Any]:
    contract = ticket_resolution_lookup_assist.build_ticket_resolution_lookup_assist(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=target_ticket_id,
        open_ticket_id=open_ticket_id,
    )
    return contract.to_dict()


def _build_ticket_resolution_lookup_answer(
    *,
    assist_payload: dict[str, Any],
    lookup_result: dict | None,
    answer_language: str,
) -> str | None:
    if not bool(assist_payload.get("accepted")):
        return None
    return ticket_resolution_lookup_assist.build_ticket_resolution_lookup_answer(
        lookup_result or {},
        target_ticket_id=assist_payload.get("target_ticket_id"),
        answer_language=answer_language,
    )


def _build_followup_result_set_assist_payload(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any],
    previous_assistant_text: str,
    result_set: dict | None,
) -> dict[str, Any]:
    contract = followup_result_set_assist.build_followup_result_set_assist(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
        result_set=result_set,
    )
    return contract.to_dict()


def _build_followup_result_set_refinement_shadow_payload(
    *,
    latest_question: str = "",
    routing_question: str = "",
    semantic_frame: dict[str, Any] | None = None,
    previous_assistant_text: str = "",
    result_set: dict | None = None,
    rejection_reason: str = "",
) -> dict[str, Any]:
    if rejection_reason:
        assist_payload: dict[str, Any] = {
            "accepted": False,
            "reason_codes": [rejection_reason],
        }
    else:
        assist_payload = _build_followup_result_set_assist_payload(
            latest_question=latest_question,
            routing_question=routing_question,
            semantic_frame=semantic_frame or {},
            previous_assistant_text=previous_assistant_text,
            result_set=result_set,
        )
    return followup_result_set_assist.build_result_set_refinement_shadow_observation(assist_payload)


def _build_followup_ticket_reference_shadow_payload(
    *,
    latest_question: str = "",
    routing_question: str = "",
    semantic_frame: dict[str, Any] | None = None,
    previous_assistant_text: str = "",
    result_set: dict | None = None,
    rejection_reason: str = "",
) -> dict[str, Any]:
    if rejection_reason:
        assist_payload: dict[str, Any] = {
            "accepted": False,
            "reason_codes": [rejection_reason],
        }
    else:
        assist_payload = _build_followup_result_set_assist_payload(
            latest_question=latest_question,
            routing_question=routing_question,
            semantic_frame=semantic_frame or {},
            previous_assistant_text=previous_assistant_text,
            result_set=result_set,
        )
    return followup_result_set_assist.build_ticket_reference_shadow_observation(assist_payload)


def _build_followup_result_set_refinement_answer(
    *,
    assist_payload: dict[str, Any],
    result_set: dict | None,
    answer_language: str,
) -> str | None:
    if not bool(assist_payload.get("accepted")):
        return None
    if assist_payload.get("request_kind") not in {"result_set_refinement", "omitted_result_set"}:
        return None
    return followup_result_set_assist.build_result_set_refinement_answer(
        assist_payload,
        result_set or {},
        answer_language=answer_language,
    )


def _build_followup_ticket_reference_answer(
    *,
    assist_payload: dict[str, Any],
    lookup_result: dict | None,
    result_set: dict | None,
    answer_language: str,
) -> str | None:
    if not bool(assist_payload.get("accepted")):
        return None
    if assist_payload.get("request_kind") != "ticket_reference_lookup":
        return None
    return followup_result_set_assist.build_ticket_reference_lookup_answer(
        assist_payload,
        lookup_result or {},
        result_set or {},
        answer_language=answer_language,
    )


def _summarize_followup_ticket_reference_lookup(lookup_result: dict | None) -> dict[str, Any]:
    data = dict(lookup_result or {})
    return {
        "requested_ids": [int(item) for item in data.get("requested_ids") or [] if str(item).isdigit()],
        "found_ids": [int(item) for item in data.get("found_ids") or [] if str(item).isdigit()],
        "missing_ids": [int(item) for item in data.get("missing_ids") or [] if str(item).isdigit()],
        "row_count": len(data.get("rows") or []) if isinstance(data.get("rows"), list) else 0,
        "cap": int(data.get("cap") or 1),
        "truncated": bool(data.get("truncated")),
        "bounded_ticket_lookup": True,
    }


def _build_output_critic_shadow_context(
    *,
    routing_question: str,
    semantic_frame: dict[str, Any],
    phase1_decision: dict[str, Any],
    policy_flags: dict[str, Any],
    quality_gate_flags: dict[str, Any],
    answer_validation: dict[str, Any],
    counts: dict[str, Any],
    sources: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    ai_dispatcher_trace: dict[str, Any] | None,
    followup_shadow_payload: dict[str, Any],
    followup_date_scope_payload: dict[str, Any],
    ticket_resolution_lookup_payload: dict[str, Any],
    conversation_entities_loaded: bool,
    conversation_entities_saved: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build compact context for private output critique only."""
    return output_critic_shadow.build_critic_input_bundle(
        latest_question=routing_question,
        visible_answer="",
        trace_context={
            "routing_question": routing_question,
            "semantic_frame": semantic_frame,
            "phase1_decision": phase1_decision,
            "policy_flags": policy_flags,
            "quality_gate_flags": quality_gate_flags,
            "answer_validation": answer_validation,
            "counts": counts,
            "sources": sources,
            "tickets": tickets,
            "ai_dispatcher": ai_dispatcher_trace or {},
            "followup_understanding_shadow": followup_shadow_payload,
            "followup_date_scope_assist": followup_date_scope_payload,
            "ticket_resolution_lookup_assist": ticket_resolution_lookup_payload,
            "conversation_entities_loaded": conversation_entities_loaded,
            "conversation_entities_saved": conversation_entities_saved or {},
        },
    )["trace"]


async def _resolve_output_critic_shadow_payload(
    *,
    loop: asyncio.AbstractEventLoop,
    latest_question: str,
    visible_answer: str,
    trace_context: dict[str, Any],
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Run the output critic after answer selection without mutating the answer."""
    if not output_critic_shadow.output_critic_shadow_enabled():
        return output_critic_shadow.skipped_result("disabled_by_config").to_trace_dict()
    bounded_timeout_ms = max(250, min(10000, int(timeout_ms or 2500)))
    future = loop.run_in_executor(
        None,
        lambda: output_critic_shadow.run_output_critic_shadow(
            latest_question=latest_question,
            visible_answer=visible_answer,
            trace_context=trace_context,
            enabled=True,
        ),
    )
    try:
        result = await asyncio.wait_for(future, timeout=bounded_timeout_ms / 1000)
    except asyncio.TimeoutError:
        result = output_critic_shadow.error_result("output_critic_timeout", latency_ms=bounded_timeout_ms)
    except Exception as exc:
        result = output_critic_shadow.error_result(str(exc))
    return output_critic_shadow.compact_trace_metadata(result)


def _resolve_output_critic_bounded_repair_payload(
    *,
    latest_question: str,
    visible_answer: str,
    output_critic_trace: dict[str, Any],
    trace_context: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Apply one deterministic post-critic repair decision without tool/model calls."""
    decision = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question=latest_question,
        visible_answer=visible_answer,
        output_critic_trace=output_critic_trace,
        trace_context=trace_context,
    )
    metadata = output_critic_bounded_repair.compact_trace_metadata(decision)
    if decision.repair_applied and decision.repaired_answer:
        return decision.repaired_answer, decision.stream_delta, metadata
    return visible_answer, "", metadata


def _followup_date_scope_assist_bq_sql(assist_payload: dict[str, Any]) -> str | None:
    if not bool(assist_payload.get("accepted")):
        return None
    windows = assist_payload.get("month_windows") if isinstance(assist_payload.get("month_windows"), list) else []
    topic = assist_payload.get("prior_topic") if isinstance(assist_payload.get("prior_topic"), dict) else {}
    date_clauses: list[str] = []
    for window in windows[:3]:
        if not isinstance(window, dict):
            continue
        start = str(window.get("start_date") or "")
        end = str(window.get("end_date_exclusive") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            continue
        date_clauses.append(
            f"(DATE(created_at, 'America/New_York') >= DATE '{start}' "
            f"AND DATE(created_at, 'America/New_York') < DATE '{end}')"
        )
    if not date_clauses:
        return None

    filters = ["(" + " OR ".join(date_clauses) + ")"]
    store_id = str(topic.get("store_id") or "").strip()
    if store_id.isdigit():
        filters.append(f"store_id = '{store_id}'")

    terms = _followup_date_scope_terms(topic)
    if terms:
        text_expr = (
            "LOWER(CONCAT(COALESCE(subject, ''), ' ', COALESCE(issue_category, ''), ' ', "
            "COALESCE(symptoms, ''), ' ', COALESCE(full_text, '')))"
        )
        term_clauses = [f"{text_expr} LIKE '%{_sql_like_literal(term)}%'" for term in terms[:6]]
        filters.append("(" + " OR ".join(term_clauses) + ")")

    where_sql = "\n  AND ".join(filters)
    return f"""
SELECT
  id,
  subject,
  store_id,
  org_name,
  brand,
  status,
  issue_category,
  created_at,
  updated_at,
  LEFT(COALESCE(full_text, ''), 1200) AS full_text
FROM `esip-reference-project.esip_reference.tickets`
WHERE {where_sql}
ORDER BY created_at DESC
LIMIT 75
""".strip()


def _build_followup_date_scope_answer(
    *,
    assist_payload: dict[str, Any],
    rows: list[dict] | str | None,
) -> str | None:
    if not bool(assist_payload.get("accepted")):
        return None
    if isinstance(rows, str):
        return (
            "I understood the follow-up month scope, but the bounded ticket analytics lookup failed, "
            f"so I cannot honestly list the matching tickets yet. Lookup result: {rows[:300]}\n\n"
            'SOURCES_JSON: {"sources": []}'
        )
    safe_rows = [row for row in (rows or []) if isinstance(row, dict)]
    windows = [item for item in (assist_payload.get("month_windows") or []) if isinstance(item, dict)]
    topic = assist_payload.get("prior_topic") if isinstance(assist_payload.get("prior_topic"), dict) else {}
    scope_bits = []
    if topic.get("store_id"):
        scope_bits.append(f"store {topic['store_id']}")
    labels = [str(item) for item in topic.get("semantic_labels") or [] if str(item).strip()]
    if labels:
        scope_bits.append("topic labels: " + ", ".join(labels[:4]))
    elif topic.get("issue_or_category"):
        scope_bits.append(f"topic: {topic['issue_or_category']}")
    scope = "; ".join(scope_bits) if scope_bits else "the prior topic"
    month_label = str(assist_payload.get("display_label") or "the requested month windows")

    lines = [
        f"I kept the prior context ({scope}) and narrowed it to {month_label}.",
        "I treated those as full month windows, not as July 1-2 only.",
    ]
    if bool(assist_payload.get("current_month_to_date")):
        lines.append("Current-month windows are month-to-date based on available ESIP analytics.")

    if not safe_rows:
        lines.append("I did not find matching tickets in those month windows.")
    else:
        lines.append(f"I found {len(safe_rows)} matching ticket(s) across the requested windows.")
        for window in windows:
            label = str(window.get("display_label") or "Requested window")
            start = str(window.get("start_date") or "")
            end = str(window.get("end_date_exclusive") or "")
            window_rows = [row for row in safe_rows if _row_date_in_window(row, start, end)]
            lines.append(f"\n{label}: {len(window_rows)} ticket(s)")
            for row in window_rows[:8]:
                tid = row.get("id") or row.get("ticket_id")
                subject = str(row.get("subject") or "No subject").strip()
                status = str(row.get("status") or "unknown").strip()
                created = _row_created_date(row) or "unknown date"
                store = str(row.get("store_id") or "").strip()
                store_text = f" store {store}," if store else ""
                lines.append(f"- #{tid}:{store_text} {created}, {status}, {subject}")
            if len(window_rows) > 8:
                lines.append(f"- ... {len(window_rows) - 8} more in this window.")

    tickets = []
    for row in safe_rows[:20]:
        card = _normalize_ticket_card(row)
        if card:
            tickets.append(card)
    lines.append("\nTICKETS_JSON: " + json.dumps(tickets))
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _followup_date_scope_terms(topic: dict[str, Any]) -> list[str]:
    values: list[str] = []
    issue = str(topic.get("issue_or_category") or "").strip()
    if issue and issue.lower() not in {"ticket_lookup", "ticket_list", "ticket_count"}:
        values.append(issue)
    for label in topic.get("semantic_labels") or []:
        text = str(label or "").strip()
        if text:
            values.append(text)
    return list(dict.fromkeys(values))


def _sql_like_literal(value: str) -> str:
    return str(value or "").strip().lower().replace("'", "''")


def _row_created_date(row: dict[str, Any]) -> str:
    value = row.get("created_at")
    if isinstance(value, datetime):
        return value.astimezone(BUSINESS_TZ).date().isoformat()
    text = str(value or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else ""


def _row_date_in_window(row: dict[str, Any], start: str, end: str) -> bool:
    created = _row_created_date(row)
    return bool(created and start <= created < end)


def _looks_like_platform_update_cadence_question(question: str) -> bool:
    text = re.sub(r"\s+", " ", (question or "").lower()).strip()
    if not text:
        return False
    subject_hit = bool(re.search(r"\b(db|database|data|analytics|sync|esip|system|platform)\b", text))
    cadence_hit = bool(re.search(r"\b(update|updated|refresh|refreshed|sync|synced|fresh|current|cadence|schedule)\b", text))
    direct_phrase_hit = bool(re.search(r"\b(your|esip|the)\s+(db|database|data|analytics)\s+(update|sync|refresh)", text))
    how_soon_hit = bool(re.search(r"\bhow soon\b.*\b(db|database|data|analytics)\b.*\b(update|sync|refresh)", text))
    when_hit = bool(re.search(r"\bwhen\b.*\b(db|database|data|analytics)\b.*\b(update|sync|refresh)", text))
    return bool((subject_hit and cadence_hit and (direct_phrase_hit or how_soon_hit or when_hit)) or "sync schedule" in text)


def _platform_update_cadence_answer(question: str) -> str | None:
    if not _looks_like_platform_update_cadence_question(question):
        return None
    cursor_dt = _bq_sync_cursor_dt()
    if cursor_dt:
        freshness = _format_sync_freshness_note()
    else:
        freshness = "I could not read the current BQ freshness cursor from this chat turn."
    return (
        "ESIP data is updated on a scheduled cadence, not continuously:\n"
        "* Zendesk ticket analytics / BigQuery sync runs on its configured scheduler.\n"
        "* Google Drive documentation sync runs on its own configured scheduler.\n\n"
        f"Current freshness check: {freshness}\n\n"
        "For exact times and operational status, admins can check the Admin sync/status and Sources sync history screens. "
        "This chat answer does not start a sync job or change any scheduler state."
    )


def _ai_dispatcher_source_family_ticket_contract(
    dispatcher_trace: dict,
    *,
    latest_question: str,
    routing_question: str,
    base_contract: semantic_contract_v2.SemanticContractV2,
) -> dict | None:
    """Build a ticket-search contract from an accepted source-family correction."""
    if not isinstance(dispatcher_trace, dict):
        return None
    if dispatcher_trace.get("assistive_task_class") != ai_dispatcher_runtime.ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH:
        return None
    decision = dispatcher_trace.get("compile_decision")
    if not isinstance(decision, dict):
        return None
    if decision.get("task_type") != "ticket_set_search":
        return None
    scope = decision.get("effective_scope")
    if not isinstance(scope, dict) or scope.get("source_family") != "tickets":
        return None
    actions = decision.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        return None
    action = actions[0]
    if not isinstance(action, dict) or action.get("name") != "ticket_set_search":
        return None
    inputs = action.get("inputs")
    if not isinstance(inputs, dict):
        inputs = {}
    store_ids = _clean_dispatcher_store_ids(scope.get("store_ids") or inputs.get("store_ids"))
    if not store_ids:
        return None
    criteria = _clean_dispatcher_string_list(inputs.get("criteria"))
    query = str(inputs.get("query") or "").strip()
    search_goal = query or " ".join(criteria).strip() or routing_question.strip() or latest_question.strip()
    required_concepts = criteria or ([search_goal] if search_goal else [])
    if not search_goal or not required_concepts:
        return None
    base_payload = base_contract.to_dict()
    return {
        "version": "2.0-dispatcher-source-family",
        "mode": "assistive",
        "intent": "ticket_retrieval",
        "data_domain": "zendesk_tickets",
        "scope": "store",
        "executor": "ticket_semantic_search",
        "output": "ticket_examples",
        "time_range": base_payload.get("time_range"),
        "filters": {
            "store_ids": store_ids,
            "search_goal": search_goal,
            "required_concepts": required_concepts,
            "optional_concepts": [],
            "vendors": [],
            "devices": [],
            "actions": [],
        },
        "confidence": "medium",
        "source": "ai_dispatcher_source_family_mismatch",
        "reason": "AI dispatcher Phase 6A corrected source family to tickets before retrieval.",
    }


def _select_deterministic_answer(
    candidates: list[tuple[str, str | None]],
) -> tuple[str, str | None]:
    """Choose one visible deterministic answer and retain its exact provenance."""
    return next(
        ((source, answer) for source, answer in candidates if answer),
        ("none", None),
    )


def _suppress_ai_dispatcher_trace_without_deterministic_answer(
    ai_dispatcher_trace: dict[str, Any] | None,
    *,
    result_set_comparison_answer: str | None = None,
    bulk_ticket_lookup_answer: str | None = None,
    challenge_fact_check_answer: str | None = None,
    source_family_dispatcher_applied: bool = False,
    ticket_semantic_search_answer: str | None = None,
    followup_date_scope_answer: str | None = None,
    policy_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep dispatcher telemetry answer-affecting only after deterministic output exists."""
    trace = dict(ai_dispatcher_trace or {})
    if not trace.get("answer_affecting"):
        return trace

    task_class = str(trace.get("assistive_task_class") or "")
    mode = str(trace.get("mode") or "")
    prefix = "primary" if mode == "primary_for_eligible" else "assistive"
    ready_by_class = {
        ai_dispatcher_runtime.ASSISTIVE_CLASS_PRIOR_RESULT_REFERENCE: (
            bool(result_set_comparison_answer),
            "prior_result_comparison",
            "deterministic_result_set_comparison",
        ),
        ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS: (
            bool(bulk_ticket_lookup_answer),
            "bulk_ticket_lookup",
            "deterministic_bulk_ticket_lookup",
        ),
        ai_dispatcher_runtime.ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK: (
            bool(challenge_fact_check_answer),
            "challenge_fact_check",
            "deterministic_challenge_fact_check",
        ),
        ai_dispatcher_runtime.ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH: (
            bool(source_family_dispatcher_applied and ticket_semantic_search_answer),
            "source_family_mismatch",
            "deterministic_source_family_mismatch",
        ),
        ai_dispatcher_runtime.ASSISTIVE_CLASS_FOLLOWUP_DATE_SCOPE: (
            bool(followup_date_scope_answer),
            "followup_date_scope",
            "deterministic_followup_date_scope",
        ),
    }
    ready, label, reason_suffix = ready_by_class.get(task_class, (False, "", "deterministic_answer"))
    if ready:
        if policy_flags is not None:
            policy_flags[f"ai_dispatcher_{prefix}_answer_affecting"] = label
        return trace

    trace["answer_affecting"] = False
    reason_codes = [str(item) for item in trace.get("reason_codes") or [] if str(item)]
    if prefix == "primary":
        reason_codes.append("primary_no_deterministic_answer")
    reason_codes.append(f"{prefix}_no_{reason_suffix}")
    trace["reason_codes"] = list(dict.fromkeys(reason_codes))
    if policy_flags is not None:
        policy_flags[f"ai_dispatcher_{prefix}_suppressed"] = "no_deterministic_answer"
    return trace


def _clean_dispatcher_store_ids(raw: Any) -> list[str]:
    values: list[str] = []
    raw_items = raw if isinstance(raw, (list, tuple)) else [raw]
    for item in raw_items:
        for match in re.findall(r"(?<!\d)(\d{3,6})(?!\d)", str(item or "")):
            if match not in values:
                values.append(match)
    return values


def _clean_dispatcher_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            values.append(text)
    return values


def _compact_answer_validation_for_log(answer_validation: dict) -> dict:
    """Keep durable answer-policy logs compact and free of raw model payloads."""
    if not isinstance(answer_validation, dict):
        return {}
    risk_detection = answer_validation.get("risk_detection") or {}
    validator = answer_validation.get("validator") or {}
    result = validator.get("result") or {}
    repair = validator.get("repair") or {}
    refresh = repair.get("refresh") or {}
    review = repair.get("review") or {}
    loop_state = repair.get("loop") or {}
    return {
        "risk_detection": {
            "should_validate": bool(risk_detection.get("should_validate")),
            "risk_classes": list(risk_detection.get("risk_classes") or []),
            "source_families": list(risk_detection.get("source_families") or []),
        },
        "validator": {
            "ran": bool(validator.get("ran")),
            "enabled": bool(validator.get("enabled")),
            "quality": result.get("quality"),
            "risk_classes": list(result.get("risk_classes") or []),
            "recommended_action": result.get("recommended_action"),
            "answer_policy": result.get("answer_policy"),
        },
        "repair": {
            "attempted": bool(repair.get("attempted")),
            "applied": bool(repair.get("applied")),
            "requested_action": repair.get("requested_action"),
            "answer_now": {
                "requested": bool((repair.get("answer_now") or {}).get("requested")),
                "checkpoint": (repair.get("answer_now") or {}).get("checkpoint"),
                "final_policy": (repair.get("answer_now") or {}).get("final_policy"),
            },
            "stop_reason": loop_state.get("stop_reason") or repair.get("skip_reason"),
            "validation_count": loop_state.get("validation_count"),
            "repair_count": loop_state.get("repair_count"),
            "refresh_used": bool(refresh.get("used")),
            "refresh_tools": list(refresh.get("tools") or []),
            "refresh_counts": refresh.get("counts") or {},
            "review_quality": review.get("quality"),
            "review_action": review.get("recommended_action"),
            "has_improved_answer": bool(review.get("has_improved_answer")),
        },
    }


def _compact_answer_policy_for_log(flags: dict) -> dict:
    """Return compact private policy telemetry for durable Super Admin logs."""
    safe_flags = dict(flags or {})
    answer_validation = safe_flags.pop("answer_validation", {})
    return {
        "quality_gate_flags": safe_flags,
        "answer_validation": _compact_answer_validation_for_log(answer_validation),
    }


class UniversalMessage(BaseModel):
    role: str    # "user" | "assistant"
    content: str


class UniversalChatRequest(BaseModel):
    messages: list[UniversalMessage]
    ticket_id: int | None = None  # When set, inject this ticket's data as chat context
    conversation_id: str | None = None


class UniversalTurnControlRequest(BaseModel):
    action: Literal["answer_now"]
    conversation_id: str | None = None


class UniversalTurnControlResponse(BaseModel):
    status: str
    action: str
    turn_id: str
    conversation_id: str | None = None
    answer_now_requested: bool = True
    message: str | None = None


def _user_control_identity(user: dict) -> tuple[str, str]:
    email = str(user.get("email") or "").strip().lower()
    uid = str(user.get("uid") or user.get("user_id") or user.get("sub") or email).strip()
    return uid, email


@router.post("/turns/{turn_id}/control", response_model=UniversalTurnControlResponse)
async def control_universal_turn(turn_id: str, req: UniversalTurnControlRequest, user: AnyUser):
    """Request control over an active Universal Chat turn owned by the current user."""
    owner_uid, owner_email = _user_control_identity(user)
    result = firestore.request_universal_chat_answer_now(
        turn_id=turn_id,
        conversation_id=req.conversation_id,
        owner_uid=owner_uid,
        owner_email=owner_email,
    )
    status = str(result.get("status") or "")
    if status == "accepted":
        return UniversalTurnControlResponse(
            status="accepted",
            action=universal_answer_now.ANSWER_NOW_ACTION,
            turn_id=str(result.get("turn_id") or turn_id),
            conversation_id=str(result.get("conversation_id") or req.conversation_id or ""),
            answer_now_requested=True,
            message="Preparing the best available answer.",
        )
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="This turn belongs to another user.")
    if status in {"missing", "expired", "completed", "conversation_mismatch", "invalid_turn"}:
        raise HTTPException(
            status_code=409,
            detail={
                "status": status or "not_available",
                "reason": result.get("reason") or "Turn is not active.",
            },
        )
    raise HTTPException(status_code=400, detail={"status": status or "unsupported_control"})


async def _save_review_event_best_effort(loop, event: dict) -> None:
    """Persist review telemetry without breaking the engineer-facing response."""
    try:
        await loop.run_in_executor(None, lambda: firestore.save_chat_quality_review_event(event))
    except Exception as exc:
        logger.warning("chat_quality_review telemetry save failed trace=%s: %s", event.get("trace_id"), exc)


def _compact_review_route_signals(trace: dict[str, Any]) -> dict[str, Any]:
    """Extract raw-free route evidence used only for deterministic review triage."""
    def _values(value: Any) -> list[Any]:
        return list(value) if isinstance(value, (list, tuple, set)) else []

    dispatcher = trace.get("ai_dispatcher") if isinstance(trace.get("ai_dispatcher"), dict) else {}
    candidates = [
        str(item)
        for item in dispatcher.get("candidate_classes") or []
        if str(item) in ai_dispatcher_runtime.ALL_KNOWN_TASK_CLASSES
    ]
    validation = trace.get("answer_validation") if isinstance(trace.get("answer_validation"), dict) else {}
    risk_detection = validation.get("risk_detection") if isinstance(validation.get("risk_detection"), dict) else {}
    validator = validation.get("validator") if isinstance(validation.get("validator"), dict) else {}
    validator_result = validator.get("result") if isinstance(validator.get("result"), dict) else {}
    critic = trace.get("output_critic_shadow") if isinstance(trace.get("output_critic_shadow"), dict) else {}
    risk_classes = list(dict.fromkeys(
        str(item)
        for item in (
            _values(risk_detection.get("risk_classes"))
            + _values(validator_result.get("risk_classes"))
            + _values(critic.get("risk_codes"))
        )
        if str(item).strip()
    ))
    resolution_summary = trace.get("ai_dispatcher_ticket_resolution_lookup")
    return {
        "candidate_classes": candidates,
        "assistive_task_class": str(dispatcher.get("assistive_task_class") or ""),
        "risk_classes": risk_classes[:12],
        "ticket_resolution_candidate": bool(
            ai_dispatcher_runtime.ASSISTIVE_CLASS_TICKET_RESOLUTION_LOOKUP in candidates
            or resolution_summary
        ),
    }


def _compact_review_trace_metadata(trace: dict[str, Any]) -> dict[str, Any]:
    def _count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    counts = trace.get("counts") if isinstance(trace.get("counts"), dict) else {}
    return {
        "turn_id": trace.get("turn_id"),
        "conversation_id": trace.get("conversation_id"),
        "target_ticket_id": trace.get("target_ticket_id") or trace.get("ticket_id"),
        "answer_chars": _count(counts.get("answer_chars")),
        "sources_count": _count(counts.get("sources")),
        "tickets_count": _count(counts.get("tickets")),
        "review_route_signals": _compact_review_route_signals(trace),
    }


@router.post("/review", response_model=ChatQualityReviewResult)
async def review_universal_answer(req: ChatQualityReviewRequest, user: AnyUser):
    """Manually review a completed Universal Chat answer by saved trace ID."""
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    base_event = {
        "mode": "manual_review",
        "trace_id": req.trace_id,
        "requester_uid": str(user.get("uid") or ""),
        "requester_email": str(user.get("email") or "").strip().lower(),
        "requester_role": str(user.get("role") or ""),
        "user_note_present": bool(req.user_note and req.user_note.strip()),
    }
    trace = await loop.run_in_executor(
        None,
        lambda: firestore.get_chat_answer_trace(req.trace_id),
    )
    if not trace:
        await _save_review_event_best_effort(loop, {
            **base_event,
            "status": "trace_not_found",
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        raise HTTPException(status_code=404, detail="Answer trace was not found or has expired.")

    trace_owner_uid = str(trace.get("owner_uid") or "").strip()
    user_uid = str(user.get("uid") or "").strip()
    if trace_owner_uid and user_uid and trace_owner_uid != user_uid:
        await _save_review_event_best_effort(loop, {
            **base_event,
            "status": "forbidden",
            "trace_owner_uid": trace_owner_uid,
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        raise HTTPException(status_code=403, detail="This answer trace belongs to another user.")

    trace_owner = str(trace.get("owner_email") or "").strip().lower()
    user_email = str(user.get("email") or "").strip().lower()
    if (
        not trace_owner_uid
        and trace_owner
        and trace_owner != "[email]"
        and user_email
        and trace_owner != user_email
    ):
        await _save_review_event_best_effort(loop, {
            **base_event,
            "status": "forbidden",
            "trace_owner_email": trace_owner,
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        raise HTTPException(status_code=403, detail="This answer trace belongs to another user.")

    compact_trace_metadata = _compact_review_trace_metadata(trace)

    try:
        review, telemetry = await loop.run_in_executor(
            None,
            lambda: chat_quality.review_answer_trace_with_telemetry(trace, req.user_note),
        )
        if _review_should_refresh_evidence(review):
            refresh_trace = await _build_review_refresh_trace(trace, review, loop)
            refresh_block = refresh_trace.get("review_refresh") if isinstance(refresh_trace, dict) else None
            telemetry["evidence_refresh"] = {
                "attempted": True,
                "used": bool(refresh_block),
                "query": (refresh_block or {}).get("query"),
                "counts": (refresh_block or {}).get("counts", {}),
                "tools": ((refresh_block or {}).get("phase1_decision") or {}).get("tools", []),
            }
            if refresh_block:
                refreshed_review, refreshed_telemetry = await loop.run_in_executor(
                    None,
                    lambda: chat_quality.review_answer_trace_with_telemetry(refresh_trace, req.user_note),
                )
                telemetry["evidence_refresh"]["review_telemetry"] = refreshed_telemetry
                review = refreshed_review
    except Exception as exc:
        await _save_review_event_best_effort(loop, {
            **base_event,
            "status": "error",
            "trace_owner_uid": trace_owner_uid,
            "trace_owner_email": trace_owner,
            **compact_trace_metadata,
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:500],
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        raise

    await _save_review_event_best_effort(loop, {
        **base_event,
        "status": "success",
        "trace_owner_uid": trace_owner_uid,
        "trace_owner_email": trace_owner,
        **compact_trace_metadata,
        "quality": review.quality,
        "confidence": review.confidence,
        "problem_types": review.problem_types,
        "recommended_action": review.recommended_action,
        "has_improved_answer": bool(review.improved_answer),
        "evidence_refresh_used": bool((telemetry.get("evidence_refresh") or {}).get("used")),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "telemetry": telemetry,
    })
    return review


def _normalize_ticket_card(raw: dict) -> dict | None:
    """Normalize arbitrary ticket-like dict to the UI ticket card shape."""
    raw_id = raw.get("id", raw.get("ticket_id"))
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    def _json_safe_date(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return {
        "id": ticket_id,
        "subject": str(raw.get("subject", "") or ""),
        "org_name": str(raw.get("org_name", "") or ""),
        "brand": str(raw.get("brand", "") or ""),
        "status": str(raw.get("status", "") or ""),
        "store_id": str(raw.get("store_id", "") or ""),
        "issue_category": str(raw.get("issue_category", "") or ""),
        "resolution": str(raw.get("resolution", "") or ""),
        "created_at": _json_safe_date(raw.get("created_at")),
        "solved_at": _json_safe_date(raw.get("solved_at")),
    }


def _minimal_ticket_card(ticket_id: int) -> dict:
    return {
        "id": int(ticket_id),
        "subject": "",
        "org_name": "",
        "brand": "",
        "status": "",
        "store_id": "",
        "issue_category": "",
        "resolution": "",
        "created_at": None,
        "solved_at": None,
    }


def _extract_ticket_ids_for_cards(text: str) -> list[int]:
    """Extract ticket IDs mentioned in free text for deterministic UI ticket cards."""
    out: list[int] = []
    seen: set[int] = set()

    patterns = [
        re.compile(r"(?<!\w)#(\d{5,10})\b"),
        re.compile(r"(?i)\b(?:ticket|тикет)\s*#?\s*(\d{4,10})\b"),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            try:
                tid = int(match.group(1))
            except ValueError:
                continue
            if tid in seen:
                continue
            seen.add(tid)
            out.append(tid)
            if len(out) >= 30:
                return out
    return out


def _extract_explicit_ticket_id(question: str) -> int | None:
    """Extract explicit ticket reference from a natural-language question."""
    return chat_policy.extract_explicit_ticket_id(question)


def _extract_store_id(question: str) -> str | None:
    """Extract explicit store number from natural-language question."""
    return chat_policy.extract_store_id(question)


def _extract_store_ids(question: str) -> list[str]:
    """Extract ordered explicit store numbers from natural-language question."""
    return chat_policy.extract_store_ids(question)


def _extract_store_id_from_ticket_context(ticket_context: str) -> str | None:
    """Extract Store #NNNN from the formatted ticket context block."""
    match = re.search(r"(?i)\bStore\s*#\s*(\d{3,6})\b", ticket_context or "")
    return match.group(1) if match else None


def _extract_lookback_days(question: str, default_days: int = 90) -> int:
    """Extract lookback window from natural language (past/last N days/weeks/months/years)."""
    return chat_policy.extract_lookback_days(question, default_days=default_days)


def _resolve_store_history_period(question: str) -> temporal_resolver.TemporalResolution | None:
    """Resolve explicit same-store history windows such as full years or date ranges."""
    return temporal_resolver.resolve_period_text(question)


def _latest_assistant_text(messages: list[dict]) -> str:
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def _conversation_texts_for_filter_chain(messages: list[dict], latest_question: str) -> list[str]:
    texts = [str(latest_question or "")]
    for msg in reversed(messages[:-1]):
        content = str(msg.get("content") or "")
        if content:
            texts.append(content)
        if len(texts) >= 8:
            break
    return texts


def _extract_store_id_range_from_texts(texts: list[str]) -> tuple[str, str] | None:
    combined = "\n".join(texts)
    patterns = (
        r"\bstore[_\s-]*ids?\s+between\s+(?P<start>\d{3,6})\s+and\s+(?P<end>\d{3,6})\b",
        r"\bstores?\s+between\s+(?P<start>\d{3,6})\s+and\s+(?P<end>\d{3,6})\b",
        r"\bstore[_\s-]*ids?\s+from\s+(?P<start>\d{3,6})\s+(?:to|-)\s+(?P<end>\d{3,6})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            start, end = match.group("start"), match.group("end")
            return (start, end) if start <= end else (end, start)
    return None


def _extract_assignee_group_from_texts(texts: list[str]) -> str | None:
    combined = "\n".join(texts)
    quoted_patterns = (
        r"\bvendor\s+[\"“](?P<name>[^\"”]+)[\"”]",
        r"\b(?:assigned|assignment)\s+(?:to|group|queue)\s+(?:the\s+)?(?:vendor\s+|group\s+|queue\s+)?[\"“](?P<name>[^\"”]+)[\"”]",
        r"\bassignee[_\s-]*group(?:_name)?\s*=\s*[\"'](?P<name>[^\"']+)[\"']",
    )
    for pattern in quoted_patterns:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            name = re.sub(r"\s+", " ", match.group("name")).strip()
            if name:
                return name
    if re.search(r"\bfield_services_partner\b", combined, flags=re.IGNORECASE):
        return "field_services_partner"
    return None


def _is_count_followup_text(text: str) -> bool:
    lower = str(text or "").lower()
    return bool(re.search(r"\b(how many|count|total|number of)\b", lower))


def _is_list_followup_text(text: str) -> bool:
    lower = str(text or "").lower().strip()
    return bool(
        re.search(r"\b(list|show|which|what)\b", lower)
        and (
            "ticket" in lower
            or re.search(r"\b(those|these|ones|them)\b", lower)
            or "store id" in lower
        )
    )


def _count_list_filter_consistency_sql(messages: list[dict], latest_question: str) -> str | None:
    """Deterministic guard for current-ticket count/list follow-ups over the same filtered set."""
    texts = _conversation_texts_for_filter_chain(messages, latest_question)
    combined_lower = "\n".join(texts).lower()
    if not re.search(r"\b(current|active)\b", combined_lower):
        return None
    store_range = _extract_store_id_range_from_texts(texts)
    if not store_range:
        return None
    if not (_is_count_followup_text(latest_question) or _is_list_followup_text(latest_question)):
        return None
    assignee_group = _extract_assignee_group_from_texts(texts)
    start, end = store_range
    filters = [
        f"store_id BETWEEN '{start}' AND '{end}'",
        "status IN ('open', 'pending', 'hold')",
    ]
    if assignee_group:
        safe_group = re.sub(r"'", "''", assignee_group.strip().lower())
        filters.append(f"LOWER(assignee_group_name) = '{safe_group}'")
    where = " AND ".join(filters)
    table = "`esip-reference-project.esip_reference.tickets`"
    if _is_count_followup_text(latest_question) and assignee_group:
        return f"SELECT COUNT(id) AS ticket_count FROM {table} WHERE {where}"
    return (
        "SELECT id, subject, status, store_id, issue_category, created_at, assignee_group_name "
        f"FROM {table} WHERE {where} ORDER BY created_at DESC"
    )


def _resolve_label_count_period(
    *,
    latest_question: str,
    routing_question: str,
    messages: list[dict],
) -> temporal_resolver.TemporalResolution | None:
    """Resolve the period for semantic label-count follow-ups."""
    for text in (routing_question, latest_question):
        period = temporal_resolver.resolve_period_text(text)
        if period:
            return period
    for msg in reversed(messages[:-1]):
        if msg.get("role") != "user":
            continue
        period = temporal_resolver.resolve_period_text(str(msg.get("content") or ""))
        if period:
            return period
    return None


def _period_date_bounds(
    period: temporal_resolver.TemporalResolution | None,
) -> tuple[str | None, str | None]:
    if not period:
        return None, None
    return period.start_date.isoformat(), period.end_date_exclusive.isoformat()


def _store_history_scope_label(
    period: temporal_resolver.TemporalResolution | None,
    lookback_days: int,
) -> str:
    if period:
        return f"{period.display_label} (created_at Eastern window)"
    return f"last {lookback_days} days by created_at"


def _store_history_window_for_zendesk(
    period: temporal_resolver.TemporalResolution | None,
    lookback_days: int,
) -> tuple[datetime, datetime]:
    if period:
        return period.start_at, period.end_at
    tz = ZoneInfo("America/New_York")
    today = datetime.now(tz).date()
    start_day = today - timedelta(days=max(0, lookback_days - 1))
    start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
    end_at = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start_at, end_at


_GLOBAL_EVENT_STOP_WORDS = {
    "about", "any", "are", "case", "cases", "check", "couple", "did",
    "does", "been", "for", "had", "has", "have", "incident", "incidents", "last",
    "list", "logged", "number", "numbers", "opened", "created", "all",
    "over", "recorded", "restaurant", "restaurants", "store", "stores",
    "the", "there", "this", "today", "ticket", "tickets", "two", "was", "were", "where", "which",
    "year", "years",
    "был", "была", "были", "где", "года", "есть", "каком", "лет", "любом",
    "любой", "нас", "пару", "последние", "посление", "проверь", "рестаранов",
    "ресторане", "ресторанов", "случаи", "зафиксированные", "зафикисиоованные",
}

_GLOBAL_EVENT_ALIASES = {
    "пожар": "fire",
    "пожара": "fire",
    "пожаре": "fire",
    "пожаром": "fire",
    "пожары": "fire",
    "пожаров": "fire",
    "поджаров": "fire",
}


def _event_terms_from_text(value: str) -> list[str]:
    text = str(value or "").lower()
    for source, target in _GLOBAL_EVENT_ALIASES.items():
        text = re.sub(rf"\b{re.escape(source)}\b", target, text)
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z][a-z0-9_-]{2,}|[а-яё]{3,}", text, flags=re.IGNORECASE):
        token = _GLOBAL_EVENT_ALIASES.get(token, token)
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        if token in _GLOBAL_EVENT_STOP_WORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= 5:
            break
    return terms


def _extract_global_event_terms(
    latest_question: str,
    routing_question: str,
    semantic: semantic_interpreter.SemanticInterpretation,
    prior_event_text: str = "",
) -> list[str]:
    """Extract bounded event terms from the semantic frame and latest wording."""
    raw_issue = ""
    if isinstance(semantic.entities, dict):
        raw_issue = str(semantic.entities.get("issue_or_category") or "")

    # Prefer the LLM-normalized semantic event phrase. Raw user wording may
    # contain typos or non-English words; those should not leak into search labels.
    semantic_terms = _event_terms_from_text(raw_issue)
    if semantic_terms:
        return semantic_terms
    routing_terms = _event_terms_from_text(routing_question)
    if routing_terms:
        return routing_terms

    texts = [latest_question, prior_event_text]
    terms: list[str] = []
    seen: set[str] = set()
    for value in texts:
        for token in _event_terms_from_text(value):
            if token in seen:
                continue
            seen.add(token)
            terms.append(token)
            if len(terms) >= 5:
                return terms
    return terms


def _looks_like_global_event_discovery(
    latest_question: str,
    routing_question: str,
    semantic: semantic_interpreter.SemanticInterpretation,
    *,
    explicit_store_context: str | None,
    explicit_ticket_id: int | None,
    prior_event_text: str = "",
) -> dict | None:
    """Detect global "which restaurant had event X" / "any recorded X" asks."""
    task = semantic.task if isinstance(semantic.task, dict) else {}
    if str(task.get("task_type") or "") == "count_by_dimension":
        return None
    if explicit_store_context or explicit_ticket_id:
        return None
    if _public_update_staleness_sql(routing_question) or _public_update_staleness_sql(latest_question):
        return None
    if _looks_like_ticket_list_analytics(latest_question) or _looks_like_ticket_list_analytics(routing_question):
        return None
    if semantic.target_context not in {"global", "prior_turn", "unclear"}:
        return None
    terms = _extract_global_event_terms(latest_question, routing_question, semantic, prior_event_text)
    if not terms:
        return None
    # When a canonical event alias is present, keep the executor focused on the
    # event itself. The semantic interpreter can add generic words such as
    # "instance" or "our" around typo-heavy prompts; those should not become
    # search/display terms.
    if "fire" in terms:
        terms = ["fire"]
    text = f"{latest_question}\n{routing_question}\n{prior_event_text}".lower()
    eventish = _is_explicit_global_event_discovery_text(
        latest_question=latest_question,
        routing_question=routing_question,
        prior_event_text=prior_event_text,
        terms=terms,
        semantic_intent=semantic.intent,
    )
    if not eventish:
        return None
    # A broad semantic retrieval label must not veto a deterministic global
    # event shape; generic ticket searches already fail the eventish gate.
    period = _resolve_global_event_period(latest_question, routing_question)
    return {
        "terms": terms,
        "period": period,
        "query_label": _event_display_label(terms, "English"),
    }


def _is_explicit_global_event_discovery_text(
    *,
    latest_question: str,
    routing_question: str,
    prior_event_text: str,
    terms: list[str],
    semantic_intent: str,
) -> bool:
    """Return True only for explicit global event/fact discovery requests.

    Generic search verbs alone do not establish an event-discovery intent.
    """
    latest = f"{latest_question}\n{routing_question}".lower()
    combined = f"{latest}\n{prior_event_text}".lower()
    if semantic_intent == "event_fact_check" and _has_event_discovery_shape(combined, terms):
        return True
    if _has_event_discovery_shape(latest, terms):
        return True
    prior_terms = _event_terms_from_text(prior_event_text)
    if prior_terms and any(term in prior_terms for term in terms):
        return bool(
            re.search(
                r"\b(?:check|recheck|verify|look\s+again|last|past|over\s+the\s+last)\b",
                latest,
            )
            or re.search(r"\b(?:проверь|перепроверь|последн\w+|пару|года|лет)\b", latest)
        )
    return False


def _has_event_discovery_shape(text: str, terms: list[str]) -> bool:
    has_terms = bool(terms)
    if not has_terms:
        return False
    english_global_location = bool(
        re.search(
            r"\b(?:which|what)\s+(?:restaurants?|stores?)\b.{0,100}\b"
            r"(?:had|have|reported|recorded|experienced|were\s+affected\s+by)\b",
            text,
        )
        or re.search(
            r"\bwhere\b.{0,100}\b(?:did\s+it\s+)?(?:happen|occur|happened|occurred|was|were|reported|recorded)\b",
            text,
        )
    )
    russian_global_location = bool(
        re.search(r"\b(?:в\s+каком\s+ресторане|каком\s+ресторане|какие\s+рестораны|какой\s+ресторан)\b", text)
        or re.search(r"\bгде\b.{0,80}\b(?:был|была|были|произош[её]л|случил)\b", text)
    )
    direct_event_case = bool(
        re.search(r"\b(?:recorded|reported|confirmed)\b.{0,50}\b(?:cases?|incidents?)\b", text)
        or re.search(r"\b(?:зафикс\w+|случа[ийе])\b.{0,100}\b(?:рестор|рестар|магазин|store|restaurant)", text)
    )
    return english_global_location or russian_global_location or direct_event_case


def _resolve_global_event_period(
    latest_question: str,
    routing_question: str,
) -> dict:
    """Resolve event-discovery date bounds, including rolling year phrases."""
    for text in (routing_question, latest_question):
        period = temporal_resolver.resolve_period_text(text)
        if period:
            return {
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date_exclusive.isoformat(),
                "label": period.display_label,
                "matched_text": period.matched_text,
            }

    text = f"{routing_question}\n{latest_question}".lower()
    years: int | None = None
    matched = ""
    numeric = re.search(r"\b(?:last|past|over\s+the\s+last)\s+(\d+)\s+years?\b", text)
    if numeric:
        years = max(1, min(int(numeric.group(1)), 10))
        matched = numeric.group(0)
    elif re.search(r"\b(?:last|past|over\s+the\s+last)\s+(?:couple\s+of|two)\s+years?\b", text):
        years = 2
        matched = "last couple of years"
    elif re.search(r"\b(?:за\s+)?последн\w+\s+(?:пару|2|два)\s+(?:года|лет)\b", text):
        years = 2
        matched = "последние пару лет"

    if years:
        tz = ZoneInfo("America/New_York")
        today = datetime.now(tz).date()
        start = today - timedelta(days=365 * years)
        end = today + timedelta(days=1)
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "label": f"last {years} years (since {start.isoformat()})",
            "matched_text": matched,
        }

    return {
        "start_date": None,
        "end_date": None,
        "label": "all indexed ticket history",
        "matched_text": "",
    }


def _group_event_candidates(rows: list[dict], match_type: str) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows or []:
        if row.get("event_match_type") != match_type:
            continue
        key = str(row.get("store_id") or row.get("org_name") or row.get("id") or "").strip()
        if not key:
            continue
        group = grouped.setdefault(
            key,
            {
                "store_id": row.get("store_id") or "",
                "org_name": row.get("org_name") or "",
                "brand": row.get("brand") or "",
                "tickets": [],
            },
        )
        row_org = str(row.get("org_name") or "").strip()
        if row_org and _is_generic_event_org_name(str(group.get("org_name") or "")):
            group["org_name"] = row_org
        if row.get("brand") and not group.get("brand"):
            group["brand"] = row.get("brand")
        if len(group["tickets"]) < 4:
            group["tickets"].append(row)
    groups = list(grouped.values())
    for group in groups:
        group["tickets"].sort(
            key=lambda item: (
                int(item.get("event_match_score") or 0),
                str(item.get("created_at") or ""),
            ),
            reverse=True,
        )
    groups.sort(
        key=lambda group: (
            int((group.get("tickets") or [{}])[0].get("event_match_score") or 0),
            str((group.get("tickets") or [{}])[0].get("created_at") or ""),
        ),
        reverse=True,
    )
    return groups


def _is_generic_event_org_name(org_name: str) -> bool:
    return (org_name or "").strip().lower() in {"", "reference integration", "northstar hospitality group"}


def _event_group_display_name(group: dict) -> str:
    org_name = str(group.get("org_name") or "").strip()
    if org_name and not _is_generic_event_org_name(org_name):
        return org_name
    brand = str(group.get("brand") or "").strip()
    store_id = str(group.get("store_id") or "").strip()
    if brand and store_id:
        return f"{brand} {store_id}"
    if store_id:
        return f"Store {store_id}"
    return org_name or "Unknown store"


def _event_display_label(terms: list[str] | tuple[str, ...] | None, answer_language: str) -> str:
    terms = [str(term or "").lower() for term in (terms or [])]
    is_ru = str(answer_language or "").lower().startswith("ru")
    if "fire" in terms:
        return "пожары" if is_ru else "fire incidents"
    if not terms:
        return "события" if is_ru else "event"
    return ", ".join(terms[:3])


def _event_period_display_label(period: dict, answer_language: str) -> str:
    label = str((period or {}).get("label") or "all indexed ticket history")
    is_ru = str(answer_language or "").lower().startswith("ru")
    if not is_ru:
        return label
    if label == "all indexed ticket history":
        return "вся индексированная история тикетов"
    match = re.search(r"last\s+(\d+)\s+years?\s+\(since\s+(\d{4}-\d{2}-\d{2})\)", label, flags=re.IGNORECASE)
    if match:
        return f"последние {match.group(1)} года (с {match.group(2)})"
    return label


def _collapse_repeated_event_phrase(snippet: str) -> str:
    text = snippet
    # Ticket full_text often repeats subject/comment fragments. Collapse exact
    # adjacent phrase repeats so the answer reads like evidence, not raw index text.
    phrase_re = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9 ,:/#&().'\->]{5,90}?)\s+\1\b", flags=re.IGNORECASE)
    for _ in range(4):
        collapsed = phrase_re.sub(r"\1", text)
        if collapsed == text:
            break
        text = collapsed
    return re.sub(r"\s+", " ", text).strip()


def _format_event_ticket(row: dict) -> str:
    tid = row.get("id")
    date_text = str(row.get("created_at") or "")[:10]
    subject = str(row.get("subject") or "").strip()
    snippet = str(row.get("evidence_snippet") or row.get("resolution") or "").strip()
    snippet = re.sub(r"\s+", " ", snippet)
    if subject:
        snippet = re.sub(re.escape(subject), " ", snippet, flags=re.IGNORECASE)
        snippet = re.sub(r"\s+", " ", snippet).strip()
    snippet = _collapse_repeated_event_phrase(snippet)
    if snippet and subject and snippet.lower() in subject.lower():
        snippet = ""
    if len(snippet) > 90:
        snippet = snippet[:87].rstrip() + "..."
    pieces = [f"#{tid}"]
    if date_text:
        pieces.append(date_text)
    if subject:
        pieces.append(f'"{subject}"')
    if snippet:
        pieces.append(snippet)
    return " — ".join(pieces)


def _markdown_table_cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("|", "\\|")).strip()


def _event_ticket_ref(row: dict) -> str:
    tid = row.get("id")
    date_text = str(row.get("created_at") or "")[:10]
    if tid and date_text:
        return f"#{tid} ({date_text})"
    if tid:
        return f"#{tid}"
    return date_text or "ticket"


_EVENT_EVIDENCE_SIGNAL_RE = re.compile(
    r"\b(?:fire|arson|closed\s+due\s+to\s+(?:a\s+)?fire|store\s+fire|restaurant\s+fire|forest\s+fire|fire\s+damage)\b",
    flags=re.IGNORECASE,
)


def _clean_short_event_evidence(value: str) -> str:
    text = re.sub(r"(?i)\b(?:Inbound Call Details|Forwarded message)\b.*", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text


def _event_short_evidence(row: dict, *, limit: int = 84) -> str:
    subject = str(row.get("subject") or "").strip()
    snippet = str(row.get("evidence_snippet") or row.get("resolution") or row.get("symptoms") or "").strip()
    snippet = re.sub(r"\s+", " ", snippet)
    if subject:
        snippet = re.sub(re.escape(subject), " ", snippet, flags=re.IGNORECASE)
        snippet = re.sub(r"\s+", " ", snippet).strip()
    snippet = _collapse_repeated_event_phrase(snippet)
    subject = _clean_short_event_evidence(subject)
    snippet = _clean_short_event_evidence(snippet)
    subject_has_signal = bool(_EVENT_EVIDENCE_SIGNAL_RE.search(subject))
    snippet_has_signal = bool(_EVENT_EVIDENCE_SIGNAL_RE.search(snippet))
    if subject_has_signal and not snippet_has_signal:
        evidence = subject
    elif snippet_has_signal:
        evidence = snippet
    else:
        evidence = snippet or subject
    evidence = _clean_short_event_evidence(evidence)
    if not evidence and subject:
        evidence = subject
    if len(evidence) > limit:
        evidence = evidence[: limit - 3].rstrip(" ,;.-") + "..."
    return evidence or "event wording in ticket text"


def _event_group_ticket_refs(group: dict) -> str:
    tickets = group.get("tickets") or []
    refs = [_event_ticket_ref(row) for row in tickets[:3]]
    if len(tickets) > 3:
        refs.append(f"+{len(tickets) - 3} more")
    return ", ".join(refs)


def _event_group_evidence(group: dict, *, answer_language: str) -> str:
    snippets = []
    seen: set[str] = set()
    fallback = ""
    for row in (group.get("tickets") or [])[:2]:
        snippet = _event_short_evidence(row)
        if not fallback:
            fallback = snippet
        if snippets and not _EVENT_EVIDENCE_SIGNAL_RE.search(snippet):
            continue
        key = snippet.lower()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(snippet)
    if snippets:
        return "; ".join(snippets)
    if fallback:
        return fallback
    is_ru = str(answer_language or "").lower().startswith("ru")
    return "текст тикета явно описывает пожар/закрытие" if is_ru else "ticket text explicitly describes the event"


def _append_event_noise_table(
    lines: list[str],
    *,
    related_count: int,
    noise_count: int,
    keyword_count: int,
    answer_language: str,
) -> None:
    if not (related_count or noise_count or keyword_count):
        return
    is_ru = str(answer_language or "").lower().startswith("ru")
    lines.append("")
    if is_ru:
        lines.extend([
            "### Не включено в подтвержденные",
            "| Тип | Кол-во | Почему |",
            "|---|---:|---|",
        ])
        if related_count:
            lines.append(
                f"| Связанные, но не подтверждающие | {related_count} | Упоминают fire-сигнализацию, звонки или похожий контекст без подтверждения пожара в ресторане |"
            )
        if noise_count:
            lines.append(
                f"| Шумовые совпадения | {noise_count} | Например KDS-команды `fire orders`, `firewall` или `fire alarm` |"
            )
        if keyword_count:
            lines.append(
                f"| Слабые keyword-кандидаты | {keyword_count} | Есть совпадение по слову, но нужен ручной просмотр текста тикета |"
            )
        return

    lines.extend([
        "### Not Counted As Confirmed",
        "| Type | Count | Reason |",
        "|---|---:|---|",
    ])
    if related_count:
        lines.append(
            f"| Related but non-confirming | {related_count} | Mentions alarms, calls, or adjacent fire context without confirming a store fire |"
        )
    if noise_count:
        lines.append(
            f"| Noise | {noise_count} | Examples include KDS `fire orders`, `firewall`, or `fire alarm` wording |"
        )
    if keyword_count:
        lines.append(
            f"| Weak keyword candidates | {keyword_count} | Keyword matched, but ticket text needs manual review before treating it as an incident |"
        )


def _global_event_discovery_answer(
    event_request: dict,
    rows: list[dict] | None,
    answer_language: str,
) -> str:
    rows = rows or []
    confirmed_groups = _group_event_candidates(rows, "confirmed_candidate")
    related = [row for row in rows if row.get("event_match_type") == "related_non_confirming"]
    noise = [row for row in rows if row.get("event_match_type") == "noise"]
    keyword = [row for row in rows if row.get("event_match_type") == "keyword_candidate"]
    is_ru = str(answer_language or "").lower().startswith("ru")
    period_label = _event_period_display_label(event_request.get("period") or {}, answer_language)
    terms = _event_display_label(event_request.get("terms") or [], answer_language)

    if is_ru:
        lines = [
            f"Я проверил индексированные Zendesk-тикеты по теме: **{terms}**. Период: {period_label}.",
        ]
        if confirmed_groups:
            lines.extend([
                "",
                f"**Коротко:** подтвержденные кандидаты найдены по **{len(confirmed_groups)}** магазинам. Не все совпадения по слову являются реальными инцидентами.",
                "",
                "### Подтвержденные кандидаты",
                "| Магазин | Тикеты | Признак в тексте |",
                "|---|---|---|",
            ])
            display_limit = 10
            for group in confirmed_groups[:display_limit]:
                name = _markdown_table_cell(_event_group_display_name(group))
                tickets = _markdown_table_cell(_event_group_ticket_refs(group))
                evidence = _markdown_table_cell(_event_group_evidence(group, answer_language=answer_language))
                lines.append(f"| **{name}** | {tickets} | {evidence} |")
            if len(confirmed_groups) > display_limit:
                lines.append(
                    f"| Еще {len(confirmed_groups) - display_limit} групп | Не показано | Короткий ответ ограничен первыми {display_limit} группами |"
                )
            _append_event_noise_table(
                lines,
                related_count=len(related),
                noise_count=len(noise),
                keyword_count=len(keyword),
                answer_language=answer_language,
            )
        else:
            lines.append(
                f"Подтвержденных кандидатов по теме **{terms}** не нашел. "
                f"Найдено связанных/шумовых совпадений: related={len(related)}, noise={len(noise)}, "
                f"keyword_review={len(keyword)}."
            )
            for row in (related + keyword + noise)[:5]:
                lines.append(f"- {_format_event_ticket(row)}")
        lines.append('SOURCES_JSON: {"sources": []}')
        return "\n".join(lines)

    lines = [
        f"I checked indexed Zendesk tickets for `{terms}` across {period_label}.",
    ]
    if confirmed_groups:
        lines.extend([
            "",
            f"**Summary:** confirmed candidates appear for **{len(confirmed_groups)}** store group(s). Not every keyword hit is a real incident.",
            "",
            "### Confirmed Candidates",
            "| Store | Tickets | Text evidence |",
            "|---|---|---|",
        ])
        display_limit = 10
        for group in confirmed_groups[:display_limit]:
            name = _markdown_table_cell(_event_group_display_name(group))
            tickets = _markdown_table_cell(_event_group_ticket_refs(group))
            evidence = _markdown_table_cell(_event_group_evidence(group, answer_language=answer_language))
            lines.append(f"| **{name}** | {tickets} | {evidence} |")
        if len(confirmed_groups) > display_limit:
            lines.append(
                f"| {len(confirmed_groups) - display_limit} more group(s) | Not shown | Short answer limited to the first {display_limit} groups |"
            )
        _append_event_noise_table(
            lines,
            related_count=len(related),
            noise_count=len(noise),
            keyword_count=len(keyword),
            answer_language=answer_language,
        )
    else:
        lines.append(
            f"I found no confirmed candidates. Related={len(related)}, noise={len(noise)}, "
            f"keyword_review={len(keyword)}."
        )
        for row in (related + keyword + noise)[:5]:
            lines.append(f"- {_format_event_ticket(row)}")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _ticket_semantic_search_answer(
    contract: dict,
    result: dict,
    answer_language: str,
) -> str:
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    filters = contract.get("filters") if isinstance(contract.get("filters"), dict) else {}
    is_ru = str(answer_language or "").lower().startswith("ru")
    strong = [row for row in rows if row.get("semantic_match_type") == "strong_candidate"]
    related = [row for row in rows if row.get("semantic_match_type") != "strong_candidate"]
    required = filters.get("required_concepts") if isinstance(filters.get("required_concepts"), list) else []
    vendors = filters.get("vendors") if isinstance(filters.get("vendors"), list) else []
    devices = filters.get("devices") if isinstance(filters.get("devices"), list) else []
    actions = filters.get("actions") if isinstance(filters.get("actions"), list) else []
    start_date = summary.get("start_date")
    end_date = summary.get("end_date")

    def clean_list(values: list | None) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values or []:
            text = str(value or "").strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out

    def human_term(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        lower = text.lower()
        if is_ru:
            mapping = {
                "connection": "подключение",
                "connect": "подключение",
                "connect new modem": "подключение нового модема",
                "fiber": "Fiber",
                "fibre": "Fiber",
                "fiber line": "Fiber-линия",
                "fiber line upgrade": "upgrade Fiber-линии",
                "internet connection": "интернет-подключение",
                "modem": "модем",
                "network": "сеть",
                "telecom_partner": "Telecom Partner",
                "sent technician": "выезд техника",
                "technician": "техник",
                "to network": "подключение к сети",
                "upgrade": "upgrade",
            }
            return mapping.get(lower, text)
        return text

    def join_terms(values: list[str]) -> str:
        raw_labels = [human_term(value) for value in clean_list(values) if human_term(value)]
        labels: list[str] = []
        for label in raw_labels:
            key = label.lower()
            if any(
                key != other.lower()
                and key in other.lower()
                and len(key) <= 14
                for other in raw_labels
            ):
                continue
            if key not in {item.lower() for item in labels}:
                labels.append(label)
        if not labels:
            return "the requested topic" if not is_ru else "заданная тема"
        if len(labels) == 1:
            return labels[0]
        if is_ru:
            return ", ".join(labels[:-1]) + " и " + labels[-1]
        return ", ".join(labels[:-1]) + ", and " + labels[-1]

    def display_date_label() -> str:
        def display_end(value: object) -> str:
            text = str(value or "").strip()
            try:
                return (datetime.strptime(text, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
            except Exception:
                return text

        start = str(start_date or "").strip()
        end = str(end_date or "").strip()
        if start and end:
            end_display = display_end(end)
            if is_ru:
                return f"с {start} по {end_display}"
            return f"from {start} through {end_display}"
        if start:
            return f"с {start}" if is_ru else f"from {start}"
        if end:
            end_display = display_end(end)
            return f"по {end_display}" if is_ru else f"through {end_display}"
        return "вся индексированная история тикетов" if is_ru else "all indexed ticket history"

    def clean_snippet(value: object) -> str:
        text = str(value or "")
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
        text = re.sub(r"https?://\S+", " ", text)
        text = re.split(r"-{2,}\s*Forwarded message\s*-{2,}", text, flags=re.IGNORECASE)[0] or text
        text = re.sub(r"\bInbound Call Details:\s*---.*", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"\b\d{10,}\b", "[PHONE]", text)
        text = re.sub(r"-{5,}", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" -")
        return _compact_text(text, 180)

    def row_bullet(row: dict, *, include_missing: bool = False) -> str:
        ticket = f"#{row.get('ticket_id') or row.get('id')}"
        date = str(row.get("created_at") or "").strip()
        store = (
            " ".join(part for part in [str(row.get("org_name") or ""), f"(Store {row.get('store_id')})" if row.get("store_id") else ""] if part).strip()
        )
        subject = str(row.get("subject") or "").strip()
        matched_values = [
            *(row.get("matched_concepts") or []),
            *(row.get("matched_vendors") or []),
            *(row.get("matched_devices") or []),
            *(row.get("matched_actions") or []),
        ]
        matched_text = join_terms(matched_values[:6])
        snippet = clean_snippet(row.get("evidence_snippet"))
        parts: list[str] = []
        if date:
            parts.append(date)
        if store:
            parts.append(store)
        where = ", ".join(parts)
        if is_ru:
            line = f"- {ticket}"
            if where:
                line += f" — {where}"
            if subject:
                line += f". Тема: {subject}"
            if matched_text:
                line += f". Упоминания: {matched_text}"
            if include_missing:
                missing = join_terms(row.get("missing_required_concepts") or [])
                if missing and missing != "заданная тема":
                    line += f". Без явного упоминания: {missing}"
            if snippet:
                line += f". Коротко: {snippet}"
            return line
        line = f"- {ticket}"
        if where:
            line += f" — {where}"
            if subject:
                line += f". Subject: {subject}"
            if matched_text:
                line += f". Mentions: {matched_text}"
        if include_missing:
            missing = join_terms(row.get("missing_required_concepts") or [])
            if missing and missing != "the requested topic":
                line += f". No clear mention of: {missing}"
        if snippet:
            line += f". In brief: {snippet}"
        return line

    search_terms = clean_list([*required, *vendors, *devices, *actions])
    topic_label = join_terms(search_terms)
    date_label = display_date_label()
    requested_store_ids = clean_list(
        summary.get("store_ids") if isinstance(summary.get("store_ids"), list) else filters.get("store_ids")
    )

    if len(requested_store_ids) > 1:
        rows_by_store: dict[str, list[dict]] = {store_id: [] for store_id in requested_store_ids}
        for row in rows:
            store_id = str(row.get("store_id") or "").strip()
            if store_id in rows_by_store:
                rows_by_store[store_id].append(row)
        stores_with_rows = [store_id for store_id in requested_store_ids if rows_by_store.get(store_id)]
        if is_ru:
            lines = [
                (
                    f"Проверил {len(requested_store_ids)} магазинов по теме {topic_label}. "
                    f"Период: {date_label}."
                ),
                (
                    f"Нашел совпадения или связанные тикеты для {len(stores_with_rows)} "
                    f"из {len(requested_store_ids)} магазинов."
                ),
            ]
            for store_id in requested_store_ids:
                store_rows = rows_by_store.get(store_id) or []
                store_strong = [row for row in store_rows if row.get("semantic_match_type") == "strong_candidate"]
                display_rows = (store_strong or store_rows)[:4]
                lines.append("")
                if store_strong:
                    lines.append(f"**Store {store_id}: релевантные совпадения ({len(store_strong)})**")
                elif store_rows:
                    lines.append(f"**Store {store_id}: связанные тикеты для ручной проверки ({len(store_rows)})**")
                else:
                    lines.append(f"**Store {store_id}: в этом bounded check совпадений не найдено**")
                for row in display_rows:
                    lines.append(row_bullet(row, include_missing=not store_strong))
        else:
            lines = [
                (
                    f"I checked {len(requested_store_ids)} requested stores for {topic_label}. "
                    f"Scope: {date_label}."
                ),
                (
                    f"I found exact or related ticket evidence for {len(stores_with_rows)} "
                    f"of {len(requested_store_ids)} requested stores."
                ),
            ]
            for store_id in requested_store_ids:
                store_rows = rows_by_store.get(store_id) or []
                store_strong = [row for row in store_rows if row.get("semantic_match_type") == "strong_candidate"]
                display_rows = (store_strong or store_rows)[:4]
                lines.append("")
                if store_strong:
                    lines.append(f"**Store {store_id}: relevant matches ({len(store_strong)})**")
                elif store_rows:
                    lines.append(f"**Store {store_id}: related tickets to review ({len(store_rows)})**")
                else:
                    lines.append(f"**Store {store_id}: no indexed matches in this bounded check**")
                for row in display_rows:
                    lines.append(row_bullet(row, include_missing=not store_strong))
        lines.append('SOURCES_JSON: {"sources": []}')
        return "\n".join(lines)

    if is_ru:
        if strong:
            count_word = "тикет" if len(strong) == 1 else "тикета" if 2 <= len(strong) <= 4 else "тикетов"
            lines = [
                f"Нашел {len(strong)} релевантных {count_word} по теме: {topic_label}. Период: {date_label}.",
                "",
                "Вот что стоит посмотреть в первую очередь:",
            ]
            for row in strong[:5]:
                lines.append(row_bullet(row))
            if related:
                lines.append("")
                lines.append(
                    f"Еще есть {len(related)} похожих тикетов, но они не подтверждают всю тему полностью."
                )
        elif related:
            lines = [
                f"Нашел связанные тикеты по теме {topic_label}, но без полного совпадения всех признаков. Период: {date_label}.",
                "",
                "Проверь эти тикеты вручную как связанные совпадения:",
            ]
            for row in related[:5]:
                lines.append(row_bullet(row, include_missing=True))
        else:
            lines = [
                f"Я проверил Zendesk-тикеты по теме {topic_label}. Период: {date_label}.",
                "",
                "Совпадающих тикетов в индексированных данных не нашел.",
            ]
        lines.append('SOURCES_JSON: {"sources": []}')
        return "\n".join(lines)

    if strong:
        count_word = "ticket" if len(strong) == 1 else "tickets"
        lines = [
            f"I found {len(strong)} relevant {count_word} for {topic_label}. Scope: {date_label}.",
            "",
            "Start with these:",
        ]
        for row in strong[:5]:
            lines.append(row_bullet(row))
        if related:
            lines.append("")
            lines.append(
                f"I also found {len(related)} similar tickets, but they do not fully confirm the whole scenario."
            )
    elif related:
        lines = [
            f"I found related tickets for {topic_label}, but none matched every requested concept exactly. Scope: {date_label}.",
            "",
            "Review these as related matches:",
        ]
        for row in related[:5]:
            lines.append(row_bullet(row, include_missing=True))
    else:
        lines = [
            f"I checked Zendesk tickets for {topic_label}. Scope: {date_label}.",
            "",
            "I did not find matching tickets in the indexed data.",
        ]
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


async def _run_store_zendesk_history(
    store_id: str,
    period: temporal_resolver.TemporalResolution | None,
    lookback_days: int,
    loop,
    *,
    limit: int = 120,
) -> list[dict]:
    from app.services.zendesk import zendesk

    start_at, end_at = _store_history_window_for_zendesk(period, lookback_days)
    return await loop.run_in_executor(
        None,
        lambda: zendesk.search_store_tickets_created_between(
            store_id,
            start_at.astimezone(timezone.utc),
            end_at.astimezone(timezone.utc),
            limit=limit,
        ),
    )


def _zendesk_store_history_rows_to_snapshot_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        created_at = row.get("created_at")
        updated_at = row.get("updated_at")
        out.append({
            "source": "zendesk",
            "id": row.get("id"),
            "subject": row.get("subject"),
            "status": row.get("status"),
            "org_name": row.get("org_name"),
            "brand": row.get("brand"),
            "store_id": row.get("store_id"),
            "issue_category": row.get("issue_category"),
            "created_at": created_at.strftime("%Y-%m-%d") if hasattr(created_at, "strftime") else str(created_at or ""),
            "solved_at": "",
            "updated_at": updated_at.strftime("%Y-%m-%d") if hasattr(updated_at, "strftime") else str(updated_at or ""),
            "resolution": "",
        })
    return out


def _question_requests_issue_breakdown(question: str) -> bool:
    return chat_policy.question_requests_issue_breakdown(question)


def _is_store_issue_breakdown_question(question: str) -> bool:
    """True for explicit store-level issue/problem breakdown questions."""
    return chat_policy.is_store_issue_breakdown_question(question)


def _is_contextual_category_frequency_followup(question: str) -> bool:
    return chat_policy.is_contextual_category_frequency_followup(question)


def _contextual_category_count_sql(ticket_id: int) -> str:
    """
    Build a deterministic count query for "this category" follow-ups.

    The category is read from the current ticket row in BigQuery so this stays
    generic across all possible Zendesk category names and languages.
    """
    safe_ticket_id = int(ticket_id)
    current_category = (
        "SELECT issue_category "
        f"FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets` "
        f"WHERE id = {safe_ticket_id} "
        "LIMIT 1"
    )
    return f"""
    SELECT
      {safe_ticket_id} AS current_ticket_id,
      ({current_category}) AS issue_category,
      COUNT(t.id) AS total_matching_tickets,
      COUNTIF(t.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 365 DAY)) AS matching_tickets_last_365_days,
      COUNTIF(t.status IN ('new', 'open', 'pending', 'hold')) AS currently_active_matching_tickets,
      FORMAT_TIMESTAMP('%Y-%m-%d', MIN(t.created_at), 'America/New_York') AS first_reported_at,
      FORMAT_TIMESTAMP('%Y-%m-%d', MAX(t.created_at), 'America/New_York') AS latest_reported_at,
      STRING_AGG(
        FORMAT(
          '#%d | %s | %s | %s | %s',
          t.id,
          FORMAT_TIMESTAMP('%Y-%m-%d', t.created_at, 'America/New_York'),
          COALESCE(t.status, ''),
          COALESCE(t.org_name, ''),
          COALESCE(t.subject, '')
        ),
        '\\n'
        ORDER BY t.created_at DESC
        LIMIT 5
      ) AS recent_examples
    FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets` t
    WHERE ({current_category}) IS NOT NULL
      AND TRIM(({current_category})) != ''
      AND t.issue_category = ({current_category})
    LIMIT 1
    """


def _analytics_schema_limitation(question: str) -> dict | None:
    """Return unsupported ticket-analytics fields requested by the latest question."""
    text = (question or "").lower()
    if not text.strip():
        return None
    analytics_terms = (
        "average", "avg", "mean", "count", "how many", "breakdown", "break down",
        "group", "by ", "tickets", "report", "metric", "analytics", "analyst group",
        "assignee group",
    )
    asks_for_metric = any(term in text for term in analytics_terms)
    if not asks_for_metric:
        return None

    # Priority and derived incident-resolution SLA fields are now supported in
    # `esip_reference.tickets`. Keep this hook for future schema gaps, but do
    # not block SLA/priority analytics anymore.
    return None


def _looks_like_sla_analytics(question: str, *, explicit_ticket_id: int | None = None) -> bool:
    """True for aggregate/list SLA analytics that should use BQ SLA fields."""
    if explicit_ticket_id:
        return False
    text = (question or "").lower()
    if "sla" not in text:
        return False
    analytics_terms = (
        "average", "avg", "mean", "count", "how many", "rate", "breach",
        "breached", "met", "by priority", "priorities", "show me", "list",
        "all-time", "historical", "enough", "reliable", "raw", "field",
    )
    return any(term in text for term in analytics_terms)


def _sla_analytics_sql(question: str, *, explicit_ticket_id: int | None = None) -> str:
    """Deterministic SQL for narrow SLA analytics cases over supported fields."""
    if not _looks_like_sla_analytics(question, explicit_ticket_id=explicit_ticket_id):
        return ""
    text = (question or "").lower()
    if "average_sla_minutes" in text or "raw sla" in text or "raw fields" in text:
        return ""
    if "all-time" in text or "historical" in text or "enough" in text or "reliable" in text:
        return f"""
        SELECT
          COUNT(*) AS total_tickets,
          COUNTIF(resolution_sla_status IS NOT NULL) AS tickets_with_loaded_sla,
          COUNTIF(resolution_sla_breached IS TRUE) AS breached_loaded_sla_tickets,
          COUNTIF(resolution_sla_status = 'met') AS met_loaded_sla_tickets,
          ROUND(
            SAFE_DIVIDE(COUNTIF(resolution_sla_breached IS TRUE), COUNTIF(resolution_sla_status IS NOT NULL)) * 100,
            2
          ) AS loaded_sla_breach_rate_pct
        FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`
        """
    if "analyst" in text and any(term in text for term in ("average", "avg", "mean")):
        return f"""
        SELECT
          COALESCE(assignee_group_name, 'Unknown assigned group') AS assignee_group_name,
          COALESCE(priority, 'unknown') AS priority,
          COUNT(*) AS loaded_sla_ticket_count,
          ROUND(AVG(resolution_sla_target_hours), 1) AS avg_resolution_sla_target_hours,
          ROUND(AVG(resolution_sla_elapsed_hours), 1) AS avg_resolution_sla_elapsed_hours,
          COUNTIF(resolution_sla_breached IS TRUE) AS breached_count
        FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`
        WHERE LOWER(COALESCE(assignee_group_name, '')) LIKE '%analyst%'
          AND resolution_sla_status IS NOT NULL
        GROUP BY assignee_group_name, priority
        ORDER BY loaded_sla_ticket_count DESC, assignee_group_name, priority
        LIMIT 20
        """
    if "high" in text and "met" in text and ("show" in text or "list" in text or "any" in text):
        return f"""
        SELECT
          id,
          subject,
          status,
          priority,
          ticket_type,
          ticket_form_name,
          resolution_sla_target_hours,
          ROUND(resolution_sla_elapsed_hours, 1) AS resolution_sla_elapsed_hours,
          resolution_sla_status
        FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`
        WHERE LOWER(COALESCE(priority, '')) = 'high'
          AND LOWER(COALESCE(ticket_type, '')) = 'incident'
          AND resolution_sla_status = 'met'
        ORDER BY solved_at DESC NULLS LAST, updated_at DESC
        LIMIT 20
        """
    if "breach" in text and ("priority" in text or "priorities" in text):
        return f"""
        SELECT
          COALESCE(priority, 'unknown') AS priority,
          COUNTIF(resolution_sla_breached IS TRUE) AS breached_tickets,
          COUNTIF(resolution_sla_status IS NOT NULL) AS loaded_sla_tickets
        FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`
        WHERE resolution_sla_status IS NOT NULL
        GROUP BY priority
        ORDER BY breached_tickets DESC, priority
        """
    return ""


def _sla_supported_fields_answer(question: str) -> str | None:
    text = (question or "").lower()
    if "sla" not in text:
        return None
    if not ("average_sla_minutes" in text or "raw sla" in text or "raw fields" in text):
        return None
    return "\n".join([
        "ESIP does not support raw SLA fields such as `average_sla_minutes` or a generic `sla` column.",
        "",
        "The supported derived SLA fields in `esip_reference.tickets` are:",
        "- `sla_policy_name`",
        "- `resolution_sla_target_hours`",
        "- `resolution_sla_elapsed_hours`",
        "- `resolution_sla_breached`",
        "- `resolution_sla_status`",
        "",
        "For Store Incident tickets, the target is derived from Zendesk priority: urgent 8h, high 12h, normal/medium 24h, low 48h.",
        "Historical SLA coverage is still partial until the backfill completes, so broad/all-time results must be labeled as currently loaded SLA rows.",
        "",
        'SOURCES_JSON: {"sources": []}',
    ])


def _sla_analytics_answer(question: str, bq_rows: list | str | None) -> str | None:
    text = (question or "").lower()
    if not _looks_like_sla_analytics(question):
        return None
    if isinstance(bq_rows, str):
        return (
            "I could not retrieve the SLA analytics result from BigQuery for this turn. "
            f"Error/details: {bq_rows[:300]}\n\n"
            'SOURCES_JSON: {"sources": []}'
        )
    if not isinstance(bq_rows, list):
        return None

    caveat = (
        "Note: this is based only on currently loaded SLA rows in ESIP. "
        "Historical SLA backfill is still in progress, so this is not a complete all-time SLA dataset yet."
    )
    lines: list[str] = []
    if "all-time" in text or "historical" in text or "enough" in text or "reliable" in text:
        row = bq_rows[0] if bq_rows else {}
        total = row.get("total_tickets", 0) if isinstance(row, dict) else 0
        loaded = row.get("tickets_with_loaded_sla", 0) if isinstance(row, dict) else 0
        breached = row.get("breached_loaded_sla_tickets", 0) if isinstance(row, dict) else 0
        met = row.get("met_loaded_sla_tickets", 0) if isinstance(row, dict) else 0
        rate = row.get("loaded_sla_breach_rate_pct") if isinstance(row, dict) else None
        lines = [
            "No. ESIP does not yet have enough loaded historical SLA data to calculate a reliable all-time SLA breach rate across all tickets.",
            "",
            f"Currently loaded SLA coverage: {loaded} of {total} tickets.",
            f"Within the loaded SLA rows only: {breached} breached, {met} met, breach rate {rate}%.",
            "",
            caveat,
        ]
    elif "analyst" in text and any(term in text for term in ("average", "avg", "mean")):
        lines = ["Average derived SLA by priority for analyst groups, based on currently loaded SLA rows:", ""]
        if not bq_rows:
            lines.append("No currently loaded SLA rows matched analyst groups such as `Service Desk Analysts`.")
        for row in bq_rows[:10]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- **{row.get('assignee_group_name') or 'Unknown'}**, priority **{row.get('priority') or 'unknown'}**: "
                f"avg target {row.get('avg_resolution_sla_target_hours')}h, "
                f"avg elapsed {row.get('avg_resolution_sla_elapsed_hours')}h, "
                f"{row.get('breached_count')} breached across {row.get('loaded_sla_ticket_count')} loaded SLA tickets"
            )
        lines.extend(["", caveat])
    elif "high" in text and "met" in text:
        lines = ["High-priority incident tickets in currently loaded SLA data that met SLA:", ""]
        if not bq_rows:
            lines.append("No matching loaded SLA rows found.")
        for row in bq_rows[:10]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- #{row.get('id')} — target {row.get('resolution_sla_target_hours')}h, "
                f"elapsed {row.get('resolution_sla_elapsed_hours')}h, status {row.get('resolution_sla_status')}. "
                f"Subject: {row.get('subject') or ''}"
            )
        lines.extend(["", caveat])
    elif "breach" in text and ("priority" in text or "priorities" in text):
        lines = ["SLA breaches by priority in currently loaded SLA data:", ""]
        for row in bq_rows[:10]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- **{row.get('priority') or 'unknown'}**: {row.get('breached_tickets')} breached "
                f"out of {row.get('loaded_sla_tickets')} loaded SLA tickets"
            )
        lines.extend(["", caveat])
    else:
        return None

    lines.extend(["", 'SOURCES_JSON: {"sources": []}'])
    return "\n".join(lines)


def _schema_limitation_resolution_sql(question: str, limitation: dict | None) -> str:
    """Build the narrow supported substitute query for known schema-limited asks."""
    if not limitation:
        return ""
    text = (question or "").lower()
    if "analyst" not in text:
        return ""
    if not any(term in text for term in ("average", "avg", "mean", "sla")):
        return ""
    return f"""
    SELECT
      COALESCE(assignee_group_name, 'Unknown assigned group') AS assignee_group_name,
      COUNT(*) AS solved_ticket_count,
      ROUND(AVG(resolution_hours), 1) AS avg_resolution_hours
    FROM `{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`
    WHERE status IN ('solved', 'closed')
      AND resolution_hours IS NOT NULL
      AND LOWER(COALESCE(assignee_group_name, '')) LIKE '%analyst%'
    GROUP BY assignee_group_name
    ORDER BY solved_ticket_count DESC
    LIMIT 20
    """


def _analytics_schema_limitation_answer(
    question: str,
    limitation: dict | None,
    bq_rows: list | str | None,
) -> str | None:
    """Deterministic answer for analytics requests over fields absent from BQ."""
    if not limitation:
        return None
    unavailable = [str(field) for field in limitation.get("unavailable_fields", [])]
    if not unavailable:
        return None
    fields_text = " and ".join(unavailable)
    lines = [
        f"I cannot calculate this by {fields_text} from ESIP ticket analytics because "
        f"`esip_reference.tickets` does not currently contain {fields_text} field(s).",
    ]
    if "SLA" in unavailable:
        lines.append("The available timing field is `resolution_hours`; it is time-to-resolution, not SLA.")
    if "priority" in unavailable:
        lines.append("The current analytics table also cannot break results down by ticket priority.")

    if isinstance(bq_rows, list) and bq_rows:
        lines.append("")
        lines.append(
            "As a clearly labeled substitute, here is average `resolution_hours` for solved/closed "
            "tickets whose `assignee_group_name` contains `analyst`:"
        )
        for row in bq_rows[:10]:
            if not isinstance(row, dict):
                continue
            group_name = row.get("assignee_group_name") or "Unknown assigned group"
            ticket_count = row.get("solved_ticket_count")
            avg_hours = row.get("avg_resolution_hours")
            lines.append(f"- **{group_name}**: {avg_hours} hours across {ticket_count} solved/closed tickets")
        lines.append("")
        lines.append("This substitute is not SLA and is not grouped by priority.")
    elif isinstance(bq_rows, str) and bq_rows.strip():
        lines.append("")
        lines.append(
            "I also could not retrieve the supported `resolution_hours` substitute from analytics "
            f"for this turn: {bq_rows.strip()[:300]}"
        )
    else:
        lines.append("")
        lines.append(
            "I can still report supported metrics such as average `resolution_hours` by "
            "`assignee_group_name`, but not SLA by priority."
        )

    lines.append("")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _bq_rows_missing_issue_breakdown(rows: list | str | None) -> bool:
    """True when BQ rows don't contain a usable issue/topic breakdown."""
    if rows is None:
        return True
    if isinstance(rows, str):
        return True
    if not isinstance(rows, list) or not rows:
        return True

    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return True

    cols = {str(k).lower() for k in dict_rows[0].keys()}
    issue_cols = {"issue_category", "category", "issue_bucket", "issue_type", "topic"}
    if not cols.intersection(issue_cols):
        return True

    total_weight = 0
    missing_weight = 0
    has_non_empty = False

    def _weight_for_row(row: dict) -> int:
        for key in ("ticket_count", "cnt", "count", "total"):
            if key in row:
                try:
                    return max(1, int(row.get(key) or 0))
                except (TypeError, ValueError):
                    return 1
        return 1

    for row in dict_rows[:100]:
        issue_text = ""
        for key in issue_cols:
            if key in row:
                value = row.get(key)
                issue_text = "" if value is None else str(value).strip().lower()
                break
        w = _weight_for_row(row)
        total_weight += w
        if not issue_text or issue_text in {"none", "null"}:
            missing_weight += w
        else:
            has_non_empty = True

    if not has_non_empty:
        return True
    if total_weight > 0 and (missing_weight / total_weight) >= 0.6:
        return True
    return False


def _ticket_analysis_terms(question: str, ticket_context: str) -> list[str]:
    """
    Pick conservative terms for deterministic same-store history fallback.

    The fallback is only meant to avoid false no-data answers after generated
    SQL failure, so use broad operational terms instead of fragile exact
    categories.
    """
    text = f"{question}\n{ticket_context}".lower()
    candidates = [
        ("internet", ("internet", "connection", "connectivity", "degrad")),
        ("wifi", ("wifi", "wi-fi", "wireless")),
        ("network", ("network", "wan", "packet", "latency", "lagging")),
        ("modem", ("modem", "smartrg", "telecom_partner", "pepwave", "peplink")),
        ("patt", ("patt", "payment", "pinpad", "terminal")),
    ]
    terms: list[str] = []
    for term, markers in candidates:
        if any(marker in text for marker in markers):
            terms.append(term)
    return terms or ["internet", "wifi", "network"]


def _should_build_store_context_snapshot(
    question: str,
    ticket_context: str,
    explicit_ticket_id: int | None,
) -> bool:
    """
    True when Phase 2 needs a scoped current-store evidence boundary.

    This is deliberately not tied to a specific incident word. The backend
    returns a bounded same-store ticket history snapshot; the model interprets
    events and themes from those rows while preserving store scope.
    """
    if not ticket_context:
        return False
    if explicit_ticket_id and chat_policy.explicit_ticket_request_needs_tools(question):
        return True
    return chat_policy.needs_scoped_store_evidence(question)


def _store_context_snapshot_rows(rows: list[dict], note: str) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "evidence_scope": "same_store_history_snapshot",
            "source": row.get("source") or "bq",
            "id": row.get("id") or row.get("ticket_id"),
            "ticket_created_at": row.get("created_at"),
            "ticket_solved_at": row.get("solved_at"),
            "store_id": row.get("store_id"),
            "org_name": row.get("org_name"),
            "subject": row.get("subject"),
            "issue_category": row.get("issue_category"),
            "status": row.get("status"),
            "resolution": row.get("resolution"),
            "scope_note": note,
        })
    return out


def _filter_out_store_tickets(tickets: list | None, store_id: str | None) -> tuple[list | None, int]:
    """Remove current-store rows from a global similar-ticket set."""
    if not tickets or not store_id:
        return tickets, 0
    wanted = str(store_id).strip()
    kept: list = []
    removed = 0
    for ticket in tickets:
        if isinstance(ticket, dict) and str(ticket.get("store_id") or "").strip() == wanted:
            removed += 1
            continue
        kept.append(ticket)
    return kept, removed


def _extract_json_footer(text: str, marker: str):
    """Extract JSON after a marker line in the model response."""
    idx = text.rfind(marker)
    if idx == -1:
        return None
    raw = text[idx + len(marker):].strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def _strip_trace_footers(text: str) -> str:
    """Remove machine-readable footers from text before saving review traces."""
    cut = len(text)
    for marker in ("TICKETS_JSON:", "SOURCES_JSON:", "TRACE_JSON:"):
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip()


def _strip_internal_tool_blocks(text: str) -> str:
    """
    Remove model-hallucinated tool transcript blocks from user-visible answers.

    Universal Chat tools run in backend code before Phase 2. If Gemini emits
    "Tool Call:" / "Tool Output:" text, it is not an actual system trace and
    must not be shown to engineers.
    """
    if not text:
        return text
    label = r"(?:Tool Call|Tool Output|Data Retrieved)"
    text = re.sub(
        rf"(?ims)^\s*{label}:\s*\n(?:```.*?```\s*)?(?=^\s*(?:{label}):|\S|\Z)",
        "",
        text,
    )
    text = re.sub(
        rf"(?ims)^\s*{label}:\s*\n(?:```.*?```\s*|\{{.*?\}}\s*)(?=^\s*(?:{label}):|^[A-ZА-Я0-9][^\n]{{0,160}}|\Z)",
        "",
        text,
    )
    text = re.sub(
        rf"(?ims)^\s*{label}:\s*\n.*?(?=^\s*(?:{label}):|^[A-ZА-Я0-9][^\n]{{0,160}}|\Z)",
        "",
        text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_visible_answer_markdown(text: str) -> str:
    """Apply deterministic markdown polish to user-visible answer text."""
    if not text:
        return text

    bullet_count_re = re.compile(
        r"^(\s*(?:[-*]|\u2022)\s+)([^\n]*?)(\s+(?:\(\d+\s+tickets?\)|with\s+\d+\s+tickets?\.?))(\s*)$",
        flags=re.IGNORECASE,
    )
    lines: list[str] = []
    in_code_fence = False
    in_machine_footer = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            lines.append(line)
            continue
        if stripped.startswith(("TICKETS_JSON:", "SOURCES_JSON:", "TRACE_JSON:")):
            in_machine_footer = True
            lines.append(line)
            continue
        if in_code_fence or in_machine_footer:
            lines.append(line)
            continue

        def repl(match: re.Match[str]) -> str:
            label = match.group(2).strip()
            if "[" in label or "](" in label or len(label) > 160:
                return match.group(0)
            label = label.replace("**", "").strip()
            if not label:
                return match.group(0)
            return f"{match.group(1)}**{label}**{match.group(3)}{match.group(4)}"

        lines.append(bullet_count_re.sub(repl, line))
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class UniversalAnswerQualityGateResult:
    visible_answer: str
    flags: dict


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
    r"\b(?:query|sql|tool)\s+error\b|failed\s+generated\s+sql|analytics\s+helper\s+failed",
    flags=re.IGNORECASE,
)
_RAW_ARTIFACT_LINE_RE = re.compile(
    r"^\s*(?:DEBUG|TRACE(?:_ID|_JSON)?|SOURCES_JSON|TICKETS_JSON)\s*:",
    flags=re.IGNORECASE,
)
_RAW_TOOL_LABEL_RE = re.compile(
    r"^\s*(?:Tool Call|Tool Output|Data Retrieved)\s*:\s*$",
    flags=re.IGNORECASE,
)


def _strip_raw_answer_artifacts(text: str) -> tuple[str, bool]:
    before = text or ""
    precleaned_lines: list[str] = []
    skipping_tool_block = False
    for line in before.splitlines():
        if _RAW_TOOL_LABEL_RE.match(line):
            skipping_tool_block = True
            continue
        if skipping_tool_block:
            if not line.strip():
                skipping_tool_block = False
            continue
        precleaned_lines.append(line)
    precleaned = "\n".join(precleaned_lines)
    stripped = _strip_trace_footers(_strip_internal_tool_blocks(precleaned))
    kept_lines: list[str] = []
    skipping_tool_block = False
    for line in stripped.splitlines():
        if _RAW_TOOL_LABEL_RE.match(line):
            skipping_tool_block = True
            continue
        if skipping_tool_block:
            if not line.strip():
                skipping_tool_block = False
            continue
        if _RAW_ARTIFACT_LINE_RE.match(line):
            continue
        kept_lines.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return out, out != before.strip()


def _contains_query_or_tool_error(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_QUERY_ERROR_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_query_or_tool_error(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_query_or_tool_error(v) for v in value)
    return False


def _answer_claims_no_data(text: str) -> bool:
    return bool(_NO_DATA_RE.search(text or ""))


def _answer_has_checked_note(text: str) -> bool:
    lower = (text or "").lower()
    return (
        "checked:" in lower
        or "was checked" in lower
        or "were checked" in lower
        or "live zendesk was checked" in lower
    )


def _answer_has_freshness_or_limitation_note(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        marker in lower
        for marker in (
            "bq analytics are synced through",
            "may not include the full requested fresh window",
            "live zendesk was checked",
            "zendesk was checked as the ticket source of truth",
            "data limitation:",
            "bounded check",
        )
    )


def _checked_paths_for_quality_gate(
    *,
    tools: list | tuple | None,
    bq_rows,
    kb_results,
    drive_chunks,
    similar_tickets,
    live_zendesk_recent_tickets,
    zendesk_temporal_tail_tickets,
    zendesk_temporal_fallback_tickets,
) -> list[str]:
    paths: list[str] = []
    tool_set = {str(tool) for tool in (tools or [])}
    if bq_rows is not None or "run_bq_query" in tool_set:
        paths.append("BigQuery ticket analytics")
    if kb_results is not None or "search_knowledge_base" in tool_set:
        paths.append("indexed KB articles")
    if drive_chunks is not None or "search_drive_exact" in tool_set:
        paths.append("Drive documents")
    if similar_tickets is not None or "search_similar_tickets" in tool_set:
        paths.append("similar ticket history")
    if live_zendesk_recent_tickets is not None:
        paths.append("live Zendesk recent tickets")
    if zendesk_temporal_tail_tickets is not None:
        paths.append("live Zendesk fresh-tail tickets")
    if zendesk_temporal_fallback_tickets is not None:
        paths.append("live Zendesk fallback tickets")
    if not paths:
        paths.append("available indexed ESIP sources")

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _append_checked_paths(answer: str, checked_paths: list[str]) -> str:
    if _answer_has_checked_note(answer):
        return answer
    return f"{answer.rstrip()}\n\nChecked: {', '.join(checked_paths)}."


_UNSUPPORTED_PRIOR_SCOPE_PATTERNS = [
    re.compile(
        r"(?is)\bprevious\s+(?:response|answer|query|request)\b.{0,220}"
        r"\b(?:different\s+list|did\s+not\s+include|was\s+not\s+(?:in|on|part\s+of)\s+(?:that|the)\s+list)\b"
    ),
    re.compile(
        r"(?is)\b(?:store|location)\s+#?\d{3,6}\b.{0,180}"
        r"\bwas\s+not\s+(?:in|on|part\s+of)\s+(?:that|the)\s+list\b"
    ),
]


def _answer_has_unsupported_prior_scope_claim(answer: str) -> bool:
    return any(pattern.search(answer or "") for pattern in _UNSUPPORTED_PRIOR_SCOPE_PATTERNS)


def _rewrite_unsupported_prior_scope_claim(answer: str) -> str:
    if not _answer_has_unsupported_prior_scope_claim(answer):
        return answer

    paragraphs = re.split(r"\n\s*\n", (answer or "").strip())
    kept = [
        paragraph.strip()
        for paragraph in paragraphs
        if paragraph.strip() and not _answer_has_unsupported_prior_scope_claim(paragraph)
    ]
    is_ru = any("\u0400" <= ch <= "\u04ff" for ch in answer or "")
    if is_ru:
        prefix = (
            "Исправление: я не должен утверждать, что предыдущий запрос исключал магазин, "
            "пока не проверил исходный prompt. Предыдущий пропуск надо считать ошибкой scope/retrieval, "
            "а не доказательством, что магазин не был запрошен."
        )
    else:
        prefix = (
            "Correction: I should not claim that the previous question excluded a store unless I verify "
            "the original prompt. Treat the earlier omission as a scope/retrieval miss, not proof that "
            "the store was outside your request."
        )
    return "\n\n".join([prefix, *kept]).strip()


def _apply_universal_answer_quality_gate(
    candidate_answer: str,
    *,
    tools: list | tuple | None = None,
    bq_rows=None,
    kb_results=None,
    drive_chunks=None,
    similar_tickets=None,
    live_zendesk_recent_tickets=None,
    zendesk_temporal_tail_tickets=None,
    zendesk_temporal_fallback_tickets=None,
    stale_or_incomplete_data: bool = False,
) -> UniversalAnswerQualityGateResult:
    """Apply deterministic user-visible safety checks without model calls."""
    visible_answer, stripped_artifacts = _strip_raw_answer_artifacts(candidate_answer)
    checked_paths = _checked_paths_for_quality_gate(
        tools=tools,
        bq_rows=bq_rows,
        kb_results=kb_results,
        drive_chunks=drive_chunks,
        similar_tickets=similar_tickets,
        live_zendesk_recent_tickets=live_zendesk_recent_tickets,
        zendesk_temporal_tail_tickets=zendesk_temporal_tail_tickets,
        zendesk_temporal_fallback_tickets=zendesk_temporal_fallback_tickets,
    )
    query_error = _contains_query_or_tool_error(bq_rows) or _contains_query_or_tool_error(candidate_answer)
    no_data_claim = _answer_claims_no_data(visible_answer)
    changed = stripped_artifacts
    flags = {
        "changed": False,
        "stripped_artifacts": stripped_artifacts,
        "query_or_tool_error": query_error,
        "no_data_claim": no_data_claim,
        "checked_paths": checked_paths,
        "checked_paths_added": False,
        "stale_or_incomplete_caveat_added": False,
        "unsupported_prior_scope_claim": False,
        "prior_scope_claim_rewritten": False,
        "action": "unchanged",
    }

    if _answer_has_unsupported_prior_scope_claim(visible_answer):
        visible_answer = _rewrite_unsupported_prior_scope_claim(visible_answer)
        changed = True
        flags["unsupported_prior_scope_claim"] = True
        flags["prior_scope_claim_rewritten"] = True
        flags["action"] = "rewrote_unsupported_prior_scope_claim"

    if query_error and no_data_claim:
        visible_answer = (
            "I could not complete the analytics check because the data query failed. "
            "I did not treat that failure as proof that there is no data."
        )
        visible_answer = _append_checked_paths(visible_answer, checked_paths)
        changed = True
        flags["checked_paths_added"] = True
        flags["action"] = "replaced_query_error_no_data"
    else:
        if no_data_claim and stale_or_incomplete_data and not _answer_has_freshness_or_limitation_note(visible_answer):
            visible_answer = (
                f"{visible_answer.rstrip()}\n\n"
                "Data limitation: the indexed analytics source may be incomplete for the requested window, "
                "so this is a bounded check rather than proof that no tickets exist."
            )
            changed = True
            flags["stale_or_incomplete_caveat_added"] = True
            flags["action"] = "added_stale_or_incomplete_caveat"

        if no_data_claim and not _answer_has_checked_note(visible_answer):
            visible_answer = _append_checked_paths(visible_answer, checked_paths)
            changed = True
            flags["checked_paths_added"] = True
            if flags["action"] == "unchanged":
                flags["action"] = "added_checked_paths"

    flags["changed"] = changed
    return UniversalAnswerQualityGateResult(
        visible_answer=visible_answer.strip(),
        flags=flags,
    )


def _issue_count_columns(rows: list[dict]) -> tuple[str, str] | None:
    if not rows:
        return None
    headers = [str(key) for key in rows[0].keys()]
    issue_candidates = {
        "issue_category",
        "issue_topic",
        "category",
        "issue_bucket",
        "issue_type",
        "topic",
    }
    count_candidates = {
        "ticket_count",
        "tickets",
        "count",
        "cnt",
        "total_tickets",
        "issue_count",
    }
    issue_col = next((h for h in headers if h.lower() in issue_candidates), None)
    count_col = next((h for h in headers if h.lower() in count_candidates), None)
    return (issue_col, count_col) if issue_col and count_col else None


def _is_simple_top_issues_question(question: str) -> bool:
    text = (question or "").lower()
    if not (("top" in text or "most" in text) and ("issue" in text or "category" in text)):
        return False
    detail_markers = ("detail", "details", "example", "examples", "specific ticket", "tickets like")
    if any(marker in text for marker in detail_markers):
        return False
    date_markers = (
        "today",
        "yesterday",
        "this week",
        "last week",
        "last 7 days",
        "last seven days",
        "may ",
        "june ",
        "july ",
        "august ",
        "september ",
        "october ",
        "november ",
        "december ",
        "january ",
        "february ",
        "march ",
        "april ",
        "202",
    )
    return any(marker in text for marker in date_markers)


def _question_period_label(question: str) -> str:
    text = (question or "").strip()
    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_match:
        month = month_match.group(1).title()
        year = month_match.group(3) or str(datetime.now(ZoneInfo("America/New_York")).year)
        return f"On {month} {int(month_match.group(2))}, {year}"
    if re.search(r"\byesterday\b", text, flags=re.IGNORECASE):
        return "Yesterday"
    if re.search(r"\btoday\b", text, flags=re.IGNORECASE):
        return "Today"
    return "For the requested period"


def _looks_like_ticket_list_analytics(question: str) -> bool:
    """True for list/show ticket-number analytics that must not route to event discovery."""
    text = (question or "").lower()
    if not re.search(r"\b(today|yesterday|logged|created|opened|updated|new)\b", text):
        return False
    if not re.search(r"\btickets?\b", text):
        return False
    return bool(
        re.search(r"\b(?:list|show|give|provide|what|which)\b.{0,60}\bticket(?:\s+number)?s?\b", text)
        or re.search(r"\bticket\s+numbers?\b", text)
        or re.search(r"\btickets?\b.{0,60}\b(?:logged|created|opened|updated|reported|new)\b", text)
    )


def _issue_filter_is_generic_ticket_noun(question: str, issue_value: object) -> bool:
    """Avoid treating "incidents" as an issue-category filter in ticket-list asks."""
    issue = str(issue_value or "").strip().lower()
    if issue not in {"incident", "incidents", "ticket", "tickets"}:
        return False
    return _looks_like_ticket_list_analytics(question)


def _relative_ticket_analytics_window(question: str) -> dict | None:
    """Return an Eastern date window for bounded live Zendesk analytics overlays."""
    text = (question or "").lower()
    if not re.search(r"\b(today|yesterday)\b", text):
        return None
    analytics_terms = (
        "ticket",
        "tickets",
        "issue",
        "issues",
        "store",
        "stores",
        "reported",
        "created",
        "logged",
        "how many",
        "count",
        "top",
        "most",
    )
    if not any(term in text for term in analytics_terms):
        return None

    tz = ZoneInfo("America/New_York")
    today = datetime.now(tz).date()
    day = today - timedelta(days=1) if re.search(r"\byesterday\b", text) else today
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    return {
        "label": "Yesterday" if day < today else "Today",
        "date": day.isoformat(),
        "start_at": start,
        "end_at": end,
        "event_type": "updated" if re.search(r"\b(updated|modified|changed)\b", text) else "created",
    }


def _bq_zero_or_error(rows: list | str | None) -> bool:
    if rows is None:
        return True
    if isinstance(rows, str):
        return "query error:" in rows.lower()
    if not isinstance(rows, list) or not rows:
        return True
    if len(rows) == 1 and isinstance(rows[0], dict):
        values = list(rows[0].values())
        if values and all(_int_count(v) == 0 for v in values if isinstance(v, (int, float, str))):
            return True
    return False


def _bq_sync_cursor_dt() -> datetime | None:
    try:
        state = firestore.get_bq_sync_state()
        cursor = state.get("sync_cursor")
        if not cursor:
            return None
        return datetime.fromtimestamp(int(cursor), tz=timezone.utc)
    except Exception as exc:
        logger.warning("universal: could not read BQ freshness cursor: %s", exc)
        return None


def _bq_cursor_covers_window(window: dict) -> bool:
    cursor_dt = _bq_sync_cursor_dt()
    return bool(cursor_dt and cursor_dt >= window["end_at"].astimezone(timezone.utc))


def _should_run_live_zendesk_overlay(question: str, bq_rows: list | str | None) -> dict | None:
    window = _relative_ticket_analytics_window(question)
    if not window:
        return None
    # Same-day analytics are never complete from a once-nightly BQ sync; overlay
    # them with live Zendesk. Yesterday is overlaid if the BQ cursor has not
    # covered the full Eastern day or if BQ returned a zero/error result.
    if window["label"] == "Today":
        return window
    if _bq_zero_or_error(bq_rows) or not _bq_cursor_covers_window(window):
        return window
    return None


def _format_sync_freshness_note() -> str:
    cursor_dt = _bq_sync_cursor_dt()
    if cursor_dt:
        cursor_business_time = cursor_dt.astimezone(ZoneInfo("America/New_York"))
        return f"BQ analytics are synced through {cursor_business_time.strftime('%Y-%m-%d %H:%M ET')}."
    return "BQ analytics may not include the full requested fresh window."


async def _run_live_zendesk_recent_tickets(window: dict, loop) -> list[dict]:
    from app.services.zendesk import zendesk

    method = (
        zendesk.search_tickets_updated_between
        if window.get("event_type") == "updated"
        else zendesk.search_tickets_created_between
    )
    return await loop.run_in_executor(
        None,
        lambda: method(
            window["start_at"].astimezone(timezone.utc),
            window["end_at"].astimezone(timezone.utc),
            limit=LIVE_ZENDESK_RECENT_LIMIT,
        ),
    )


async def _run_temporal_zendesk_tickets(
    resolution: temporal_resolver.TemporalResolution,
    loop,
) -> list[dict]:
    from app.services.zendesk import zendesk

    method = (
        zendesk.search_tickets_updated_between
        if resolution.event_type == "updated"
        else zendesk.search_tickets_created_between
    )
    return await loop.run_in_executor(
        None,
        lambda: method(
            resolution.start_at.astimezone(timezone.utc),
            resolution.end_at.astimezone(timezone.utc),
            limit=LIVE_ZENDESK_RECENT_LIMIT,
        ),
    )


def _temporal_zendesk_tail_window(
    resolution: temporal_resolver.TemporalResolution,
) -> dict | None:
    """Return the non-BQ-covered tail for a resolved temporal window."""
    cursor_dt = _bq_sync_cursor_dt()
    if not cursor_dt:
        return None
    start_utc = resolution.start_at.astimezone(timezone.utc)
    end_utc = resolution.end_at.astimezone(timezone.utc)
    if cursor_dt >= end_utc:
        return None
    tail_start = max(start_utc, cursor_dt)
    if tail_start >= end_utc:
        return None
    return {
        "start_at": tail_start,
        "end_at": end_utc,
        "cursor_at": cursor_dt,
    }


async def _run_temporal_zendesk_tail_tickets(
    window: dict,
    event_type: str,
    loop,
) -> list[dict]:
    from app.services.zendesk import zendesk

    method = (
        zendesk.search_tickets_updated_between
        if event_type == "updated"
        else zendesk.search_tickets_created_between
    )
    return await loop.run_in_executor(
        None,
        lambda: method(
            window["start_at"],
            window["end_at"],
            limit=LIVE_ZENDESK_RECENT_LIMIT,
        ),
    )


def _temporal_bq_count(rows: list | str | None) -> int:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return 0
    for key in ("ticket_count", "count", "count_id", "f0_"):
        if key in rows[0]:
            return _int_count(rows[0].get(key))
    return _int_count(next(iter(rows[0].values()), 0))


def _temporal_bq_issue_counts(rows: list | str | None) -> Counter[str]:
    counter: Counter[str] = Counter()
    if not isinstance(rows, list):
        return counter
    dict_rows = [row for row in rows if isinstance(row, dict)]
    cols = _issue_count_columns(dict_rows)
    if not cols:
        return counter
    issue_col, count_col = cols
    for row in dict_rows:
        label = universal_chat._fmt_bq_cell(issue_col, row.get(issue_col)) or "Uncategorized"
        counter[label] += _int_count(row.get(count_col))
    return counter


def _temporal_bq_store_counts(rows: list | str | None) -> Counter[str]:
    counter: Counter[str] = Counter()
    if not isinstance(rows, list):
        return counter
    for row in rows:
        if not isinstance(row, dict):
            continue
        store_id = str(row.get("store_id") or row.get("store") or "Uncategorized")
        label = "Uncategorized" if store_id.lower() in {"unknown", "uncategorized", "none", "null"} else f"Store {store_id}"
        counter[label] += _int_count(row.get("ticket_count") or row.get("count"))
    return counter


def _tail_note(tickets: list[dict], tail_window: dict) -> str:
    cursor_business_time = tail_window["cursor_at"].astimezone(ZoneInfo("America/New_York"))
    start_business_time = tail_window["start_at"].astimezone(ZoneInfo("America/New_York"))
    end_business_time = tail_window["end_at"].astimezone(ZoneInfo("America/New_York"))
    cap_note = (
        f" Zendesk tail grouping is capped at {LIVE_ZENDESK_RECENT_LIMIT} tickets."
        if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
        else ""
    )
    return (
        f"BQ analytics are synced through {cursor_business_time.strftime('%Y-%m-%d %H:%M ET')}. "
        f"Live Zendesk was checked for the fresh tail from "
        f"{start_business_time.strftime('%Y-%m-%d %H:%M ET')} to "
        f"{end_business_time.strftime('%Y-%m-%d %H:%M ET')}.{cap_note}"
    )


def _temporal_hybrid_answer(
    resolution: temporal_resolver.TemporalResolution,
    bq_rows: list | str | None,
    tickets: list[dict],
    tail_window: dict,
) -> str | None:
    if resolution.intent == "ticket_count":
        total = _temporal_bq_count(bq_rows) + len(tickets)
        ticket_word = "ticket" if total == 1 else "tickets"
        verb = "updated" if resolution.event_type == "updated" else "created"
        count_text = (
            f"at least **{total}**"
            if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
            else f"**{total}**"
        )
        return (
            f"There were {count_text} {ticket_word} {verb} on {resolution.display_label}.\n\n"
            f"{_tail_note(tickets, tail_window)}\n\n"
            'SOURCES_JSON: {"sources": []}'
        )

    if resolution.intent == "top_issues":
        counter = _temporal_bq_issue_counts(bq_rows)
        for ticket in tickets:
            label = universal_chat._fmt_bq_cell("issue_category", ticket.get("issue_category"))
            counter[label or "Uncategorized"] += 1
        rows = [
            {"issue_category": label, "ticket_count": count}
            for label, count in counter.most_common(12)
            if count > 0
        ]
        if not rows:
            return None
        answer = _maybe_build_temporal_top_issues_answer(resolution, rows)
        if not answer:
            return None
        return answer.replace(
            'SOURCES_JSON: {"sources": []}',
            f"{_tail_note(tickets, tail_window)}\n\n"
            'SOURCES_JSON: {"sources": []}',
        )

    if resolution.intent == "top_stores":
        counter = _temporal_bq_store_counts(bq_rows)
        verb = "updated" if resolution.event_type == "updated" else "created"
        for ticket in tickets:
            store_id = str(ticket.get("store_id") or "unknown")
            org_name = str(ticket.get("org_name") or "Unknown store")
            label = org_name if store_id == "unknown" else f"{org_name} / Store {store_id}"
            counter[label] += 1
        if not counter:
            return None
        lines = [f"On {resolution.display_label}, the stores with the most {verb} tickets were:"]
        for label, count in counter.most_common(10):
            ticket_word = "ticket" if count == 1 else "tickets"
            lines.append(f"*   **{label}** with {count} {ticket_word}")
        lines.extend(["", _tail_note(tickets, tail_window), "", 'SOURCES_JSON: {"sources": []}'])
        return "\n".join(lines)

    return None


def _zendesk_temporal_answer(
    resolution: temporal_resolver.TemporalResolution,
    tickets: list[dict],
) -> str | None:
    cap_note = (
        f" Zendesk result grouping is capped at {LIVE_ZENDESK_RECENT_LIMIT} tickets."
        if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
        else ""
    )
    if resolution.intent == "ticket_count":
        verb = "updated" if resolution.event_type == "updated" else "created"
        count_text = (
            f"at least **{len(tickets)}**"
            if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
            else f"**{len(tickets)}**"
        )
        ticket_word = "ticket" if len(tickets) == 1 else "tickets"
        return (
            f"On {resolution.display_label}, Zendesk shows {count_text} {verb} {ticket_word}.\n\n"
            f"BigQuery returned no usable rows for this requested window, so Zendesk was checked as the ticket source of truth.{cap_note}\n\n"
            'SOURCES_JSON: {"sources": []}'
        )

    if resolution.intent == "top_stores":
        counter: Counter[tuple[str, str]] = Counter()
        verb = "updated" if resolution.event_type == "updated" else "created"
        for ticket in tickets:
            store_id = str(ticket.get("store_id") or "unknown")
            org_name = str(ticket.get("org_name") or "Unknown store")
            counter[(store_id, org_name)] += 1
        lines = [f"On {resolution.display_label}, Zendesk shows these stores with the most {verb} tickets:"]
        if not counter:
            lines = [f"On {resolution.display_label}, Zendesk shows **0** created tickets."]
        else:
            for (store_id, org_name), count in counter.most_common(10):
                label = org_name if store_id == "unknown" else f"{org_name} / Store {store_id}"
                ticket_word = "ticket" if count == 1 else "tickets"
                lines.append(f"*   **{label}** with {count} {ticket_word}")
        lines.extend([
            "",
            f"BigQuery returned no usable rows for this requested window, so Zendesk was checked as the ticket source of truth.{cap_note}",
            "",
            'SOURCES_JSON: {"sources": []}',
        ])
        return "\n".join(lines)

    if resolution.intent == "top_issues":
        counter: Counter[str] = Counter()
        for ticket in tickets:
            label = universal_chat._fmt_bq_cell("issue_category", ticket.get("issue_category"))
            counter[label or "Uncategorized"] += 1
        rows = [
            {"issue_category": label, "ticket_count": count}
            for label, count in counter.most_common(12)
        ]
        if not rows:
            return (
                f"On {resolution.display_label}, Zendesk shows **0** created tickets.\n\n"
                "BigQuery returned no usable rows for this requested window, so Zendesk was checked as the ticket source of truth.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
        answer = _maybe_build_temporal_top_issues_answer(resolution, rows)
        if not answer:
            return None
        return answer.replace(
            'SOURCES_JSON: {"sources": []}',
            f"BigQuery returned no usable rows for this requested window, so Zendesk was checked as the ticket source of truth.{cap_note}\n\n"
            'SOURCES_JSON: {"sources": []}',
        )
    return None


def _live_ticket_count_answer(question: str, window: dict, tickets: list[dict]) -> str | None:
    text = (question or "").lower()
    if not (("how many" in text or "count" in text) and "ticket" in text):
        return None
    ticket_word = "ticket" if len(tickets) == 1 else "tickets"
    verb = "updated" if window.get("event_type") == "updated" else "created"
    count_text = f"at least **{len(tickets)}**" if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT else f"**{len(tickets)}**"
    cap_note = (
        f" The live Zendesk overlay is capped at {LIVE_ZENDESK_RECENT_LIMIT} tickets."
        if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
        else ""
    )
    return (
        f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows "
        f"{count_text} {verb} {ticket_word}.\n\n"
        f"{_format_sync_freshness_note()} Live Zendesk was checked for the fresh same-day window.{cap_note}\n\n"
        'SOURCES_JSON: {"sources": []}'
    )


def _live_ticket_list_answer(question: str, window: dict, tickets: list[dict]) -> str | None:
    text = (question or "").lower()
    list_intent = bool(
        re.search(r"\b(?:list|show|which|what)\b.{0,40}\btickets?\b", text)
        or re.search(r"\btickets?\b.{0,40}\b(?:opened|created|logged|reported|new)\b", text)
        or re.search(r"\btoday'?s\s+new\s+tickets?\b", text)
    )
    aggregate_intent = bool(
        ("how many" in text or "count" in text)
        or ("store" in text and ("top" in text or "most" in text or "reported" in text))
        or (("top" in text or "most" in text) and ("issue" in text or "category" in text))
    )
    if not list_intent or aggregate_intent:
        return None

    verb = "updated" if window.get("event_type") == "updated" else "created"
    cap_note = (
        f" Showing the first {LIVE_ZENDESK_RECENT_LIMIT} tickets from the live Zendesk check."
        if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
        else ""
    )
    if not tickets:
        return (
            f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows **0** {verb} tickets.\n\n"
            f"{_format_sync_freshness_note()} Live Zendesk was checked for the fresh same-day window.\n\n"
            'SOURCES_JSON: {"sources": []}'
        )

    lines = [
        f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows these {verb} tickets:",
    ]
    for ticket in tickets[:LIVE_ZENDESK_RECENT_LIMIT]:
        ticket_id = ticket.get("id")
        subject = str(ticket.get("subject") or "No subject").strip()
        status = str(ticket.get("status") or "unknown").strip()
        store_id = str(ticket.get("store_id") or "").strip()
        org_name = str(ticket.get("org_name") or "").strip()
        store_label = ""
        if org_name and store_id:
            store_label = f" - {org_name} / Store {store_id}"
        elif org_name:
            store_label = f" - {org_name}"
        elif store_id:
            store_label = f" - Store {store_id}"
        lines.append(f"*   #{ticket_id} {subject} ({status}){store_label}")
    lines.extend([
        "",
        f"{_format_sync_freshness_note()} Live Zendesk was checked for the fresh same-day window.{cap_note}",
        "",
        'SOURCES_JSON: {"sources": []}',
    ])
    return "\n".join(lines)


def _live_top_stores_answer(question: str, window: dict, tickets: list[dict]) -> str | None:
    text = (question or "").lower()
    if not ("store" in text and ("top" in text or "most" in text or "reported" in text)):
        return None
    counter: Counter[tuple[str, str]] = Counter()
    verb = "updated" if window.get("event_type") == "updated" else "created"
    for ticket in tickets:
        store_id = str(ticket.get("store_id") or "unknown")
        org_name = str(ticket.get("org_name") or "Unknown store")
        counter[(store_id, org_name)] += 1
    lines = [
        f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows these stores with the most {verb} tickets:"
    ]
    if not counter:
        lines = [f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows **0** created tickets."]
    else:
        for (store_id, org_name), count in counter.most_common(10):
            label = org_name if store_id == "unknown" else f"{org_name} / Store {store_id}"
            ticket_word = "ticket" if count == 1 else "tickets"
            lines.append(f"*   **{label}** with {count} {ticket_word}")
    cap_note = (
        f" Live Zendesk result grouping is capped at {LIVE_ZENDESK_RECENT_LIMIT} tickets."
        if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
        else ""
    )
    lines.extend(["", f"{_format_sync_freshness_note()}{cap_note}", "", 'SOURCES_JSON: {"sources": []}'])
    return "\n".join(lines)


def _live_top_issues_answer(question: str, window: dict, tickets: list[dict]) -> str | None:
    if not _is_simple_top_issues_question(question):
        return None
    counter: Counter[str] = Counter()
    for ticket in tickets:
        label = universal_chat._fmt_bq_cell("issue_category", ticket.get("issue_category"))
        counter[label or "Uncategorized"] += 1
    rows = [
        {"issue_category": label, "ticket_count": count}
        for label, count in counter.most_common(12)
    ]
    if not rows:
        return (
            f"{window['label']} ({window['date']}, Eastern time), live Zendesk shows **0** created tickets.\n\n"
            f"{_format_sync_freshness_note()} Live Zendesk was checked for the fresh same-day window.\n\n"
            'SOURCES_JSON: {"sources": []}'
        )
    answer = _maybe_build_deterministic_top_issues_answer(question, rows)
    if not answer:
        return None
    return answer.replace(
        f"{window['label']},",
        f"{window['label']} ({window['date']}, Eastern time),",
        1,
    ).replace(
        'SOURCES_JSON: {"sources": []}',
        f"{_format_sync_freshness_note()} Live Zendesk was checked for the fresh same-day window."
        + (
            f" Result grouping is capped at {LIVE_ZENDESK_RECENT_LIMIT} tickets."
            if len(tickets) >= LIVE_ZENDESK_RECENT_LIMIT
            else ""
        )
        + "\n\n"
        'SOURCES_JSON: {"sources": []}',
    )


def _live_zendesk_recent_answer(question: str, window: dict, tickets: list[dict]) -> str | None:
    return (
        _live_ticket_count_answer(question, window, tickets)
        or _live_ticket_list_answer(question, window, tickets)
        or _live_top_stores_answer(question, window, tickets)
        or _live_top_issues_answer(question, window, tickets)
    )


def _live_zendesk_recent_ticket_cards(tickets: list[dict] | None, limit: int = 20) -> list[dict]:
    """Expose bounded live Zendesk rows as UI ticket cards."""
    cards: list[dict] = []
    for item in (tickets or [])[: max(0, limit)]:
        card = _normalize_ticket_card(item)
        if card:
            cards.append(card)
    return cards


def _ticket_word(count: int) -> str:
    return "ticket" if int(count) == 1 else "tickets"


def _pos_type_ticket_cards(result: dict | None, limit: int = 20) -> list[dict]:
    cards: list[dict] = []
    if not isinstance(result, dict):
        return cards
    for row in (result.get("open_tickets") or [])[:limit]:
        card = _normalize_ticket_card(row)
        if card:
            cards.append(card)
    return cards


def _format_pos_type_problem(row: dict) -> str:
    issue = universal_chat._fmt_bq_cell("issue_category", row.get("issue_category")) or "Uncategorized"
    product = universal_chat._fmt_bq_cell("product_type", row.get("product_type")) or "Unknown product"
    caused_by = universal_chat._fmt_bq_cell("caused_by", row.get("caused_by")) or "Unknown cause"
    count = int(row.get("ticket_count") or 0)
    stores = int(row.get("store_count") or 0)
    open_count = int(row.get("open_ticket_count") or 0)
    extra = f"; {open_count} open" if open_count else ""
    return f"*   **{issue}** - {count} {_ticket_word(count)} across {stores} stores ({product}; {caused_by}{extra})"


def _pos_type_analytics_answer(result: dict, request: chat_policy.PosTypeAnalyticsRequest) -> str:
    pos_type = request.pos_type or "unknown"
    filters = result.get("filters") or {}
    window = (filters.get("date_window") or {}) if isinstance(filters, dict) else {}
    window_label = (
        f"{window.get('start_date')} to {window.get('end_date')} Eastern dates"
        if window.get("start_date") and window.get("end_date")
        else "all available ticket history"
    )
    brand_note = f" and brand matching **{request.brand}**" if request.brand else ""
    status_filter = tuple(request.status_filter or ())
    status_note = ""
    if status_filter:
        status_note = " Ticket status filter: " + ", ".join(f"`{status}`" for status in status_filter) + "."
    if not result.get("available", True):
        return (
            f"I can route this correctly as **POS Type = {pos_type}** profile analytics, "
            "but the Zendesk organization profile table is not available yet.\n\n"
            f"{result.get('warning') or 'Run the organization profile sync first.'}\n\n"
            'SOURCES_JSON: {"sources": []}'
        )

    coverage = result.get("coverage") or {}
    tickets_matched = int(coverage.get("tickets_matched") or 0)
    orgs_matched = int(coverage.get("orgs_matched") or 0)
    fallback_count = int(coverage.get("store_id_fallback_ticket_count") or 0)
    no_ticket_orgs = int(coverage.get("profile_orgs_without_tickets_count") or 0)

    lines = [
        (
            f"Coverage for {window_label}: **{orgs_matched}** matching organizations and "
            f"**{tickets_matched}** matching {_ticket_word(tickets_matched)}.{status_note}"
        ),
    ]
    if fallback_count or no_ticket_orgs:
        lines.append(
            f"Coverage notes: {fallback_count} tickets used the labeled `store_id_fallback`; "
            f"{no_ticket_orgs} matching profile organizations had no tickets in this window."
        )

    stores = result.get("repeated_stores") or []
    if stores:
        lines.append("\nRepeated stores:")
        for store in stores[:8]:
            count = int(store.get("ticket_count") or 0)
            recent = int(store.get("recent_ticket_count") or 0)
            open_count = int(store.get("open_ticket_count") or 0)
            label = str(store.get("org_name") or "Unknown store")
            store_id = str(store.get("store_id") or "").strip()
            if store_id:
                label = f"{label} / Store {store_id}"
            lines.append(
                f"*   **{label}** - {count} {_ticket_word(count)}"
                f" ({recent} recent, {open_count} open)"
            )
    else:
        lines.append("\nNo repeated stores were found for this POS Type/filter.")

    problems = result.get("repeated_problems") or []
    if problems:
        lines.append("\nRepeated problems:")
        lines.extend(_format_pos_type_problem(row) for row in problems[:8])
    else:
        lines.append("\nNo repeated problem groups were found for this POS Type/filter.")

    open_tickets = result.get("open_tickets") or []
    if open_tickets:
        lines.append("\nOpen tickets:")
        for ticket in open_tickets[:8]:
            ticket_id = int(ticket.get("id") or 0)
            subject = str(ticket.get("subject") or "No subject").strip()
            status = str(ticket.get("status") or "unknown").strip()
            store = str(ticket.get("org_name") or ticket.get("store_id") or "").strip()
            store_suffix = f" - {store}" if store else ""
            lines.append(f"*   [#{ticket_id}](https://support.example.com/agent/tickets/{ticket_id}) {subject} ({status}){store_suffix}")

    lines.extend(["", 'SOURCES_JSON: {"sources": []}'])
    return "\n".join(lines)


def _pos_type_comparison_answer(result: dict, request: chat_policy.PosTypeAnalyticsRequest) -> str:
    filters = result.get("filters") or {}
    window = (filters.get("date_window") or {}) if isinstance(filters, dict) else {}
    window_label = (
        f"{window.get('start_date')} to {window.get('end_date')} Eastern dates"
        if window.get("start_date") and window.get("end_date")
        else "all available ticket history"
    )
    lines = [
        "ESIP compared POS Types using Zendesk organization profiles joined to tickets by `org_id`.",
        f"Window: {window_label}.",
    ]
    grouped = result.get("problems_by_pos_type") or {}
    for pos_type in request.pos_types:
        rows = grouped.get(pos_type) or []
        lines.append(f"\n**{pos_type}**")
        if not rows:
            lines.append("*   No matching problem groups found.")
            continue
        lines.extend(_format_pos_type_problem(row) for row in rows[:6])
    lines.extend(["", 'SOURCES_JSON: {"sources": []}'])
    return "\n".join(lines)


def _should_show_zendesk_aggregate_cards(
    latest_question: str,
    temporal_resolution: temporal_resolver.TemporalResolution | None,
) -> bool:
    """Avoid presenting bounded aggregate inputs as if they are the full result set."""
    if temporal_resolution and temporal_resolution.intent == "ticket_count":
        return False
    text = (latest_question or "").lower()
    if ("how many" in text or "count" in text) and "ticket" in text:
        return False
    return True


def _int_count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _count_list_filter_label(sql: str) -> tuple[str | None, str | None, str | None]:
    range_match = re.search(
        r"\bstore_id\s+BETWEEN\s+'(?P<start>\d{3,6})'\s+AND\s+'(?P<end>\d{3,6})'",
        sql or "",
        flags=re.IGNORECASE,
    )
    group_match = re.search(
        r"\bLOWER\s*\(\s*(?:\w+\.)?assignee_group_name\s*\)\s*=\s*'(?P<group>[^']+)'",
        sql or "",
        flags=re.IGNORECASE,
    )
    start = range_match.group("start") if range_match else None
    end = range_match.group("end") if range_match else None
    group = group_match.group("group") if group_match else None
    return start, end, group


def _display_group_name(group: str | None) -> str:
    if not group:
        return "the selected assigned group"
    known = {"field_services_partner": "Field Services Partner"}
    return known.get(group.lower(), group.title())


def _maybe_build_count_list_filter_answer(sql: str | None, rows: list | str | None) -> str | None:
    if not sql or not isinstance(rows, list):
        return None
    start, end, group = _count_list_filter_label(sql)
    if not start or not end:
        return None
    group_name = _display_group_name(group)
    is_count = bool(re.search(r"\bSELECT\s+COUNT\s*\(", sql, flags=re.IGNORECASE))
    if is_count:
        count = 0
        if rows and isinstance(rows[0], dict):
            count = _int_count(next(iter(rows[0].values()), 0))
        elif rows and isinstance(rows[0], (list, tuple)):
            count = _int_count(rows[0][0] if rows[0] else 0)
        ticket_word = "ticket" if count == 1 else "tickets"
        assignment = f" assigned to **{group_name}**" if group else ""
        group_filter_note = f" and `LOWER(assignee_group_name) = '{group}'`" if group else ""
        return (
            f"There are **{count}** current {ticket_word}{assignment} with store IDs "
            f"between **{start}** and **{end}**. Filters used: `status IN ('open', 'pending', 'hold')`"
            f"{group_filter_note}.\n\n"
            'SOURCES_JSON: {"sources": []}'
        )

    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        assignment = f" assigned to **{group_name}**" if group else ""
        return (
            f"I found no current tickets{assignment} with store IDs between **{start}** and **{end}**.\n\n"
            'SOURCES_JSON: {"sources": []}'
        )
    assignment = f" assigned to **{group_name}**" if group else ""
    lines = [
        f"Current tickets{assignment} with store IDs between **{start}** and **{end}**:",
    ]
    for row in dict_rows[:20]:
        ticket_id = row.get("id")
        subject = str(row.get("subject") or "No subject").strip()
        store_id = str(row.get("store_id") or "unknown").strip()
        status = str(row.get("status") or "unknown").strip()
        created = str(row.get("created_at") or "")[:10]
        link = f"[#{ticket_id}](https://support.example.com/agent/tickets/{ticket_id})" if ticket_id else "Ticket"
        details = [f"Store {store_id}", status]
        if created:
            details.append(f"created {created}")
        lines.append(f"- {link} — {subject} ({', '.join(details)})")
    if len(dict_rows) > 20:
        lines.append(f"- {len(dict_rows) - 20} more matching tickets are not shown in this short answer.")
    lines.append("")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _maybe_build_deterministic_top_issues_answer(
    question: str,
    rows: list | str | None,
    period_label: str | None = None,
) -> str | None:
    """Render simple top-issue count answers from BQ rows instead of model prose."""
    if not _is_simple_top_issues_question(question) or not isinstance(rows, list) or not rows:
        return None
    dict_rows = [row for row in rows if isinstance(row, dict)]
    cols = _issue_count_columns(dict_rows)
    if not cols:
        return None
    issue_col, count_col = cols
    items: list[tuple[str, int]] = []
    for row in dict_rows[:12]:
        label = universal_chat._fmt_bq_cell(issue_col, row.get(issue_col))
        count = _int_count(row.get(count_col))
        if not label or count <= 0:
            continue
        items.append((label, count))
    if not items:
        return None

    uncategorized_count = sum(count for label, count in items if label == "Uncategorized")
    categorized = [
        (label, count)
        for label, count in items
        if label != "Uncategorized" and not label.startswith("Inferred from subject:")
    ]
    inferred = [
        (label[len("Inferred from subject:"):].strip(), count)
        for label, count in items
        if label.startswith("Inferred from subject:")
    ]

    period = period_label or _question_period_label(question)
    lines: list[str] = []
    if uncategorized_count:
        lines.append(
            f"{period}, **Uncategorized** had {uncategorized_count} tickets, "
            "indicating a categorization gap."
        )
        if categorized:
            lines.append("")
            lines.append("Among the categorized issues, the top issues were:")
    else:
        lines.append(f"{period}, the top reported issues were:")

    for label, count in categorized:
        ticket_word = "ticket" if count == 1 else "tickets"
        lines.append(f"*   **{label}** with {count} {ticket_word}")

    if inferred:
        lines.append("")
        if len(inferred) == 1:
            label, count = inferred[0]
            ticket_word = "ticket" if count == 1 else "tickets"
            lines.append(
                f"Additionally, **{label}** appeared in {count} {ticket_word} inferred from ticket subjects."
            )
        else:
            lines.append("Subject-inferred items:")
            for label, count in inferred:
                ticket_word = "ticket" if count == 1 else "tickets"
                lines.append(f"*   **{label}** with {count} {ticket_word}")

    lines.append("")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _maybe_build_temporal_top_issues_answer(
    resolution: temporal_resolver.TemporalResolution | None,
    rows: list | str | None,
) -> str | None:
    if not resolution or resolution.intent != "top_issues" or not isinstance(rows, list):
        return None
    synthetic_question = f"what are the top issues on {resolution.display_label}"
    return _maybe_build_deterministic_top_issues_answer(
        synthetic_question,
        rows,
        period_label=f"On {resolution.display_label}",
    )


def _maybe_build_temporal_count_answer(
    resolution: temporal_resolver.TemporalResolution | None,
    rows: list | str | None,
) -> str | None:
    if not resolution or resolution.intent != "ticket_count" or not isinstance(rows, list):
        return None
    count = 0
    if rows and isinstance(rows[0], dict):
        count = _int_count(next(iter(rows[0].values()), 0))
    ticket_word = "ticket" if count == 1 else "tickets"
    verb = "updated" if resolution.event_type == "updated" else "created"
    return (
        f"There were **{count}** {ticket_word} {verb} on {resolution.display_label}.\n\n"
        'SOURCES_JSON: {"sources": []}'
    )


def _maybe_build_temporal_top_stores_answer(
    resolution: temporal_resolver.TemporalResolution | None,
    rows: list | str | None,
) -> str | None:
    if not resolution or resolution.intent != "top_stores" or not isinstance(rows, list):
        return None
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return (
            f"On {resolution.display_label}, no stores had created tickets in the retrieved analytics rows.\n\n"
            'SOURCES_JSON: {"sources": []}'
        )
    verb = "updated" if resolution.event_type == "updated" else "created"
    lines = [f"On {resolution.display_label}, the stores with the most {verb} tickets were:"]
    for row in dict_rows[:10]:
        store_id = str(row.get("store_id") or "Uncategorized")
        count = _int_count(row.get("ticket_count") or row.get("count"))
        if count <= 0:
            continue
        label = "Uncategorized" if store_id.lower() in {"unknown", "uncategorized", "none", "null"} else f"Store {store_id}"
        ticket_word = "ticket" if count == 1 else "tickets"
        lines.append(f"*   **{label}** with {count} {ticket_word}")
    lines.append("")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def _looks_like_drive_file_id(value: str) -> bool:
    """Google Drive file IDs are often surfaced by Vertex as source ids."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{25,80}", (value or "").strip()))


def _drive_file_id_from_url(value: str) -> str | None:
    text = (value or "").strip()
    if _looks_like_drive_file_id(text):
        return text
    patterns = (
        r"/file/d/([A-Za-z0-9_-]{25,80})",
        r"/spreadsheets/d/([A-Za-z0-9_-]{25,80})",
        r"/document/d/([A-Za-z0-9_-]{25,80})",
        r"/presentation/d/([A-Za-z0-9_-]{25,80})",
        r"[?&]id=([A-Za-z0-9_-]{25,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _maybe_build_drive_url_unavailable_answer(question: str, drive_chunks: list | None) -> str | None:
    file_id = _drive_file_id_from_url(question)
    if not file_id or drive_chunks is None or drive_chunks:
        return None
    return (
        "I recognized the Google Drive/Docs/Sheets link, but that file is not currently available "
        "in the enabled ESIP indexed Drive sources for chat search. I cannot access arbitrary "
        "external Drive URLs directly from the chat turn.\n\n"
        f"File ID checked: `{file_id}`\n\n"
        'SOURCES_JSON: {"sources": []}'
    )


def _normalize_source_for_display(src: dict, drive_chunks: list | None) -> dict | None:
    """Normalize model/Vertex sources so raw Drive ids become usable links."""
    title = str(src.get("title", "") or "").strip()
    url = str(src.get("url", "") or "").strip()
    snippet = str(src.get("snippet", "") or "")
    drive_lookup: dict[str, dict] = {}
    for chunk in drive_chunks or []:
        if not isinstance(chunk, dict):
            continue
        chunk_url = str(chunk.get("drive_url") or "").strip()
        fid = _drive_file_id_from_url(chunk_url)
        if fid:
            drive_lookup[fid] = chunk

    fid = _drive_file_id_from_url(url) or _drive_file_id_from_url(title)
    if fid:
        matched = drive_lookup.get(fid, {})
        title = str(matched.get("file_name") or title or "Google Drive file").strip()
        if title == fid:
            title = "Google Drive file"
        url = f"https://drive.google.com/file/d/{fid}/view"
        if not snippet and matched:
            snippet = str(matched.get("chunk_text") or "")[:120]

    if not url and not title:
        return None
    return {"title": title, "url": url, "snippet": snippet}


def _compact_text(value: object, limit: int = 1200) -> str:
    text = str(value or "")
    return text[:limit]


def _compact_kb_results(results: list | None) -> list[dict]:
    out: list[dict] = []
    for item in (results or [])[:8]:
        out.append({
            "title": _compact_text(getattr(item, "title", ""), 240),
            "url": _compact_text(getattr(item, "url", ""), 500),
            "snippet": _compact_text(getattr(item, "snippet", ""), 800),
        })
    return out


def _compact_drive_chunks(chunks: list | None) -> list[dict]:
    out: list[dict] = []
    for item in (chunks or [])[:8]:
        if not isinstance(item, dict):
            continue
        out.append({
            "file_name": _compact_text(item.get("file_name", ""), 240),
            "source_path": _compact_text(item.get("source_path", ""), 240),
            "drive_url": _compact_text(item.get("drive_url", ""), 500),
            "chunk_text": _compact_text(item.get("chunk_text", ""), 1200),
        })
    return out


def _compact_dict_list(items: list | None, limit: int = 8) -> list[dict]:
    out: list[dict] = []
    for item in (items or [])[:limit]:
        if not isinstance(item, dict):
            continue
        compact: dict = {}
        for key, value in item.items():
            if isinstance(value, str):
                compact[key] = _compact_text(value, 800)
            elif isinstance(value, (int, float, bool)) or value is None:
                compact[key] = value
            else:
                compact[key] = _compact_text(value, 800)
        out.append(compact)
    return out


def _compact_bq_rows(rows: list | str | None):
    if isinstance(rows, str):
        return _compact_text(rows, 1600)
    return _compact_dict_list(rows, limit=12) if isinstance(rows, list) else None


def _source_search_enabled(source_id: str) -> bool:
    return firestore.is_source_enabled(source_id)


async def _run_kb(query: str, loop) -> tuple[list, str | None]:
    try:
        r = await loop.run_in_executor(
            None, lambda: vertex_search.search_kb(query, page_size=5)
        )
        return (r, None)
    except Exception as exc:
        logger.warning("universal kb search failed: %s", exc)
        return ([], str(exc))


async def _run_drive_exact(query: str, intent: str, file_name: str | None, loop) -> list:
    try:
        return await loop.run_in_executor(
            None,
            lambda: bigquery.search_drive_exact(
                query=query,
                page_size=8,
                intent=intent,
                file_name=file_name,
            ),
        )
    except Exception as exc:
        logger.warning("universal drive search failed: %s", exc)
        return []


async def _run_similar_tickets(query: str, loop) -> list:
    """Global semantic search via Vertex AI without a store filter."""
    if not _source_search_enabled("tickets_bq"):
        logger.info("universal similar tickets skipped: tickets_bq Search is disabled")
        return []
    try:
        vertex_results = await loop.run_in_executor(
            None, lambda: vertex_search.search_tickets(query, page_size=10)
        )
        ticket_ids = [r["ticket_id"] for r in vertex_results]
        if not ticket_ids:
            return []
        return await loop.run_in_executor(
            None, lambda: bigquery.fetch_ticket_details_for_cards(ticket_ids)
        )
    except Exception as exc:
        logger.warning("universal similar tickets failed: %s", exc)
        return []


def _is_generic_ticket_resolution(resolution: str) -> bool:
    text = re.sub(r"\s+", " ", (resolution or "").strip().lower())
    if not text:
        return True
    generic_exact = {
        "x",
        "n/a",
        "na",
        "done",
        "fixed",
        "resolved",
        "closed",
        "completed",
        "training issue",
        "user error",
        "operational training issue",
    }
    if text in generic_exact:
        return True
    generic_phrases = (
        "training issue",
        "user error",
        "train their staff",
        "training needed",
        "referred to bdm",
        "will work with the store for the training",
    )
    return len(text) < 120 and any(phrase in text for phrase in generic_phrases)


def _has_useful_ticket_evidence(ticket: dict) -> bool:
    """Keep only similar-ticket examples with enough context to help an engineer."""
    resolution = str(ticket.get("resolution") or "")
    subject = str(ticket.get("subject") or "")
    issue_category = str(ticket.get("issue_category") or "")
    try:
        full_text_len = int(ticket.get("full_text_len") or 0)
    except (TypeError, ValueError):
        full_text_len = 0

    if full_text_len >= 300:
        return True
    if full_text_len >= 180 and not _is_generic_ticket_resolution(resolution):
        return True
    if len(resolution.strip()) >= 120 and not _is_generic_ticket_resolution(resolution):
        return True
    if full_text_len < 180 and _is_generic_ticket_resolution(resolution):
        return False
    return bool(subject.strip() and issue_category.strip() and not _is_generic_ticket_resolution(resolution))


def _filter_low_value_similar_tickets(tickets: list[dict] | None) -> tuple[list[dict] | None, int]:
    if not tickets:
        return tickets, 0
    kept = [ticket for ticket in tickets if _has_useful_ticket_evidence(ticket)]
    return kept, len(tickets) - len(kept)


async def _run_bq_query(sql: str, loop) -> list | str:
    """Execute pre-generated SQL (from Phase 1 JSON decision)."""
    if not _source_search_enabled("tickets_bq"):
        logger.info("universal bq query skipped: tickets_bq Search is disabled")
        return "Ticket analytics search is disabled in Admin Data Sources."

    def _rows_from_result(result: dict) -> list[dict]:
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        return [dict(zip(columns, row)) for row in rows]

    try:
        fixed_sql = _fix_timestamp_intervals(sql)
        try:
            result = await loop.run_in_executor(
                None, lambda: bigquery.execute_query(fixed_sql)
            )
            return _rows_from_result(result)
        except Exception as first_exc:
            err = str(first_exc)
            if "Unrecognized name: channel" not in err:
                raise
            # Compatibility fallback: old prompts may still emit `channel`.
            compat_sql = re.sub(r"\bchannel\b", "via_channel", fixed_sql, flags=re.IGNORECASE)
            if compat_sql == fixed_sql:
                raise
            logger.info("universal bq retry with via_channel compatibility rewrite")
            retry_result = await loop.run_in_executor(
                None, lambda: bigquery.execute_query(compat_sql)
            )
            return _rows_from_result(retry_result)
    except Exception as exc:
        logger.warning("universal bq query failed: %s", exc)
        return f"Query error: {exc}"


async def _timed_tool_result(key: str, awaitable: object) -> tuple[str, object, int]:
    started = time.monotonic()
    result = await awaitable
    return key, result, int((time.monotonic() - started) * 1000)


def _review_should_refresh_evidence(review: ChatQualityReviewResult) -> bool:
    if review.quality == "good":
        return False
    return (
        review.quality in {"likely_wrong", "insufficient_evidence"}
        or review.recommended_action in {"reroute", "run_semantic_plus_exact"}
    )


def _latest_user_question_from_trace(trace: dict) -> str:
    messages = trace.get("messages") or []
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or "").strip()
    return ""


def _review_refresh_query(trace: dict, review: ChatQualityReviewResult) -> str:
    return (review.reformulated_query or _latest_user_question_from_trace(trace)).strip()


def _messages_for_review_refresh(trace: dict, query: str) -> list[dict]:
    messages = [m for m in (trace.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return [{"role": "user", "content": query}]
    out = [dict(m) for m in messages[-8:]]
    for idx in range(len(out) - 1, -1, -1):
        if out[idx].get("role") == "user":
            out[idx]["content"] = query
            return out
    out.append({"role": "user", "content": query})
    return out


def _force_review_refresh_tools(
    decision: dict,
    review: ChatQualityReviewResult,
    query: str,
) -> dict:
    out = dict(decision or {})
    tools = [str(t) for t in (out.get("tools") or []) if str(t)]
    if review.recommended_action == "run_semantic_plus_exact":
        for tool in ("search_knowledge_base", "search_drive_exact", "search_similar_tickets"):
            if tool not in tools:
                tools.append(tool)
    elif review.recommended_action == "reroute" and not tools:
        tools = ["search_knowledge_base", "search_similar_tickets"]

    if "search_knowledge_base" in tools and not out.get("kb_query"):
        out["kb_query"] = query
    if "search_drive_exact" in tools and not (out.get("drive_query") or out.get("drive_file_name")):
        out["drive_query"] = query
        out["drive_intent"] = "search"
    if "search_similar_tickets" in tools and not out.get("tickets_query"):
        out["tickets_query"] = query
    out["tools"] = tools
    return out


async def _build_review_refresh_trace(
    trace: dict,
    review: ChatQualityReviewResult,
    loop,
) -> dict:
    """
    Build a second-pass trace with freshly retrieved evidence for Improve Answer.

    The base review stays cheap and trace-only. Only reviews that look wrong or
    under-evidenced call this path, and the refreshed evidence is returned to the
    review model without mutating the original saved answer trace.
    """
    query = _review_refresh_query(trace, review)
    if not query:
        return trace

    target_ticket_id = (
        trace.get("target_ticket_id")
        or trace.get("ticket_id")
        or chat_policy.extract_explicit_ticket_id(query)
    )
    ticket_context = ""
    if target_ticket_id:
        try:
            ticket_context = await _fetch_ticket_context(int(target_ticket_id), loop)
        except (TypeError, ValueError):
            ticket_context = ""

    messages = _messages_for_review_refresh(trace, query)
    try:
        decision = await loop.run_in_executor(
            None,
            lambda: universal_chat.decide_tools(messages, ticket_context=ticket_context),
        )
    except Exception as exc:
        logger.warning("review evidence refresh routing failed trace=%s: %s", trace.get("trace_id"), exc)
        decision = {
            "tools": [],
            "reasoning": f"Review evidence refresh routing failed: {exc}",
        }
    decision = _force_review_refresh_tools(decision, review, query)

    data_sources = firestore.get_data_sources()
    tickets_bq_search_enabled = bool(data_sources.get("tickets_bq", {}).get("enabled", False))
    any_vertex_search_enabled = any(
        cfg.get("type") == "vertex_ai" and cfg.get("enabled", False)
        for cfg in data_sources.values()
    )
    any_drive_search_enabled = any(
        sid.startswith("gdrive_") and cfg.get("enabled", False)
        for sid, cfg in data_sources.items()
    )
    tools = list(decision.get("tools") or [])
    if not tickets_bq_search_enabled:
        tools = [t for t in tools if t not in {"search_similar_tickets", "run_bq_query"}]
    if not any_vertex_search_enabled:
        tools = [t for t in tools if t != "search_knowledge_base"]
    if not any_drive_search_enabled:
        tools = [t for t in tools if t != "search_drive_exact"]
    decision["tools"] = tools

    awaitables: dict[str, object] = {}
    if "search_knowledge_base" in tools and decision.get("kb_query"):
        awaitables["kb"] = _run_kb(decision["kb_query"], loop)
    if "search_drive_exact" in tools and (decision.get("drive_query") or decision.get("drive_file_name")):
        awaitables["drive"] = _run_drive_exact(
            query=decision.get("drive_query") or decision.get("drive_file_name"),
            intent=decision.get("drive_intent", "search"),
            file_name=decision.get("drive_file_name"),
            loop=loop,
        )
    if "search_similar_tickets" in tools and decision.get("tickets_query"):
        awaitables["similar"] = _run_similar_tickets(decision["tickets_query"], loop)
    if "run_bq_query" in tools and decision.get("bq_sql"):
        awaitables["bq"] = _run_bq_query(decision["bq_sql"], loop)

    kb_results = None
    kb_error = None
    drive_chunks = None
    similar_tickets = None
    bq_rows = None
    tool_latency_ms: dict[str, int] = {}
    if awaitables:
        gathered = await asyncio.gather(
            *(_timed_tool_result(key, awaitable) for key, awaitable in awaitables.items())
        )
        for key, result, elapsed_ms in gathered:
            tool_latency_ms[key] = elapsed_ms
            if key == "kb":
                kb_results, kb_error = result
            elif key == "drive":
                drive_chunks = result
            elif key == "similar":
                similar_tickets = result
            elif key == "bq":
                bq_rows = result

    same_store_snapshot = None
    if tickets_bq_search_enabled and ticket_context:
        context_store_id = _extract_store_id_from_ticket_context(ticket_context)
        if context_store_id:
            try:
                snapshot_started = time.monotonic()
                snapshot_rows = await loop.run_in_executor(
                    None,
                    lambda: bigquery.fetch_store_history_snapshot(
                        store_id=context_store_id,
                        lookback_days=1200,
                        limit=80,
                    ),
                )
                tool_latency_ms["bq_review_same_store_snapshot"] = int(
                    (time.monotonic() - snapshot_started) * 1000
                )
                same_store_snapshot = _store_context_snapshot_rows(
                    snapshot_rows,
                    "Review evidence refresh: same-store snapshot for checking prior answer claims.",
                )
            except Exception as exc:
                logger.warning("review same-store snapshot failed trace=%s: %s", trace.get("trace_id"), exc)

    counts = {
        "kb": len(kb_results or []),
        "drive": len(drive_chunks or []),
        "similar": len(similar_tickets or []),
        "bq": len(bq_rows) if isinstance(bq_rows, list) else (1 if isinstance(bq_rows, str) else 0),
        "same_store_snapshot": len(same_store_snapshot or []),
    }
    if sum(counts.values()) == 0 and not kb_error:
        return trace

    refreshed = dict(trace)
    refreshed["review_refresh"] = {
        "query": query,
        "refresh_reason": {
            "quality": review.quality,
            "recommended_action": review.recommended_action,
            "problem_types": review.problem_types,
        },
        "phase1_decision": decision,
        "ticket_context": _compact_text(ticket_context, 5000) if ticket_context else "",
        "kb_results": _compact_kb_results(kb_results),
        "kb_error": kb_error,
        "drive_chunks": _compact_drive_chunks(drive_chunks),
        "similar_tickets": _compact_dict_list(similar_tickets, limit=12),
        "bq_sql": decision.get("bq_sql", ""),
        "bq_rows": _compact_bq_rows(bq_rows),
        "same_store_snapshot": _compact_dict_list(same_store_snapshot, limit=16),
        "counts": counts,
        "tool_latency_ms": tool_latency_ms,
        "instruction": (
            "Use this refreshed evidence to produce the improved answer. Keep current-ticket, "
            "same-store, global similar-ticket, KB, Drive, and analytics evidence separate."
        ),
    }
    return refreshed


def _fix_timestamp_intervals(sql: str) -> str:
    sql = re.sub(
        r'INTERVAL\s+(\d+)\s+MONTH\b',
        lambda m: f'INTERVAL {int(m.group(1)) * 30} DAY',
        sql, flags=re.IGNORECASE,
    )
    sql = re.sub(
        r'INTERVAL\s+(\d+)\s+YEAR\b',
        lambda m: f'INTERVAL {int(m.group(1)) * 365} DAY',
        sql, flags=re.IGNORECASE,
    )
    return sql


def _looks_like_drive_tabular_lookup(question: str) -> bool:
    return chat_policy.looks_like_drive_tabular_lookup(question)


def _public_update_staleness_sql(question: str) -> str | None:
    """Deterministic SQL for public-comment freshness analytics."""
    text = str(question or "").lower()
    mentions_public_comment = bool(
        re.search(r"\bpublic\b", text)
        and re.search(r"\b(?:update|updates|reply|replies|comment|comments)\b", text)
    )
    asks_stale_or_missing = bool(
        re.search(r"\b(?:not\s+had|has\s+not\s+had|have\s+not\s+had|without|no|missing|older\s+than|not\s+updated)\b", text)
    )
    if not (mentions_public_comment and asks_stale_or_missing):
        return None

    hours = 24
    hour_match = re.search(r"\b(?:last|past|within\s+the\s+last|in\s+the\s+last)\s+(\d{1,3})\s+hours?\b", text)
    if hour_match:
        hours = max(1, min(int(hour_match.group(1)), 720))
    elif re.search(r"\b(?:last|past|within\s+the\s+last|in\s+the\s+last)\s+(?:day|24\s+hrs?)\b", text):
        hours = 24

    ticket_table = f"`{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.tickets`"
    comments_table = f"`{bigquery.GCP_PROJECT}.{bigquery.BQ_DATASET}.ticket_comments`"
    where_status = "LOWER(COALESCE(t.status, '')) NOT IN ('solved', 'closed')"
    stale_clause = (
        f"(lp.last_public_update_at IS NULL OR "
        f"lp.last_public_update_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR))"
    )
    want_count = bool(re.search(r"\b(?:how\s+many|count|number\s+of)\b", text))
    if want_count:
        return f"""
SELECT COUNT(*) AS ticket_count
FROM {ticket_table} AS t
LEFT JOIN (
  SELECT ticket_id, MAX(created_at) AS last_public_update_at
  FROM {comments_table}
  WHERE is_internal = FALSE
  GROUP BY ticket_id
) AS lp ON lp.ticket_id = t.id
WHERE {where_status}
  AND {stale_clause}
""".strip()

    return f"""
SELECT
  t.id,
  t.subject,
  t.status,
  t.store_id,
  t.org_name,
  t.assignee_name,
  t.assignee_group_name,
  t.updated_at,
  lp.last_public_update_at
FROM {ticket_table} AS t
LEFT JOIN (
  SELECT ticket_id, MAX(created_at) AS last_public_update_at
  FROM {comments_table}
  WHERE is_internal = FALSE
  GROUP BY ticket_id
) AS lp ON lp.ticket_id = t.id
WHERE {where_status}
  AND {stale_clause}
ORDER BY
  COALESCE(lp.last_public_update_at, TIMESTAMP('1970-01-01 00:00:00+00')) ASC,
  t.updated_at ASC
LIMIT 100
""".strip()


async def _fetch_ticket_context(ticket_id: int, loop) -> str:
    """
    Fetch ticket data from Zendesk + BQ images and format as a context block.
    Used when the engineer has a specific ticket open in the left panel.
    Returns empty string on any failure (graceful degradation).
    """
    try:
        from app.services.zendesk import zendesk
        from app.services import gemini as gemini_svc
        ticket = await loop.run_in_executor(None, lambda: zendesk.get_ticket(ticket_id))
        try:
            images_by_comment = await loop.run_in_executor(
                None, lambda: bigquery.fetch_ticket_images_by_comment(ticket_id)
            )
        except Exception:
            images_by_comment = {}
        return gemini_svc._format_ticket_for_prompt(ticket, images_by_comment)
    except Exception as exc:
        logger.warning("universal: ticket context fetch failed for %s: %s", ticket_id, exc)
        return ""


async def _fetch_open_ticket_metadata(ticket_id: int | None, loop) -> dict:
    """Fetch lightweight open-ticket state for semantic interpretation."""
    if not ticket_id:
        return {}
    try:
        rows = await loop.run_in_executor(
            None,
            lambda: bigquery.fetch_ticket_details_for_cards([int(ticket_id)]),
        )
        return dict(rows[0]) if rows else {}
    except Exception as exc:
        logger.warning("universal: open ticket metadata fetch failed for %s: %s", ticket_id, exc)
        return {}


@router.post("/stream")
async def universal_stream(req: UniversalChatRequest, user: AnyUser):
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        messages = [message.model_dump() for message in req.messages]
        latest_question = messages[-1]["content"] if messages else ""
        answer, sources, tickets = reference_runtime.chat_response(latest_question, req.ticket_id)
        trace_id = f"reference-{uuid.uuid4().hex[:12]}"
        turn_id = uuid.uuid4().hex

        async def reference_event_stream():
            yield _sse({
                "status": STATUS_PREPARING_ANSWER,
                "trace_id": trace_id,
                "turn_id": turn_id,
                "conversation_id": req.conversation_id,
            })
            yield _sse({"delta": answer})
            yield _sse({
                "done": True,
                "sources": sources,
                "tickets": tickets,
                "trace_id": trace_id,
                "turn_id": turn_id,
                "conversation_id": req.conversation_id,
            })

        return StreamingResponse(reference_event_stream(), media_type="text/event-stream")

    started_at = time.monotonic()
    stage_timings = UniversalStageTimings(clock=time.monotonic)
    stage_timings.start("interpretation")
    messages = [m.model_dump() for m in req.messages]
    loop = asyncio.get_event_loop()
    latest_question = messages[-1]["content"] if messages else ""
    previous_user_question = next(
        (m["content"] for m in reversed(messages[:-1]) if m.get("role") == "user"),
        "",
    )
    chat_trace_id = uuid.uuid4().hex[:12]
    turn_id = uuid.uuid4().hex
    conversation_id = (req.conversation_id or "").strip() or f"server-{uuid.uuid4().hex}"
    owner_uid, owner_email = _user_control_identity(user)
    owner_role = firestore.get_user_role(owner_email)
    durable_logging_disabled = firestore.is_universal_chat_logging_disabled(owner_email)
    capture_config = model_transition_capture_admin.get_runtime_config()
    capture_allowed_emails = set(capture_config["allowed_emails"])
    request_capture_enabled = bool(
        capture_config["enabled"]
        and not durable_logging_disabled
        and owner_email.strip().lower() in capture_allowed_emails
    )
    request_captures: list[dict[str, Any]] = []

    def _capture_model_transition_request(envelope: dict[str, Any]) -> None:
        # The service constructs the envelope before this callback. Keep the raw
        # value only for the existing private GCS payload, never Firestore or BQ.
        if request_capture_enabled:
            request_captures.append(envelope)
    logger.info(
        "universal_trace start trace=%s turn=%s conversation=%s ticket_id=%s question=%r",
        chat_trace_id,
        turn_id,
        conversation_id,
        req.ticket_id,
        latest_question,
    )
    try:
        await loop.run_in_executor(
            None,
            lambda: firestore.register_universal_chat_turn_control(
                turn_id=turn_id,
                conversation_id=conversation_id,
                owner_uid=owner_uid,
                owner_email=owner_email,
            ),
        )
    except Exception as exc:
        logger.warning("universal turn control registration failed trace=%s turn=%s: %s", chat_trace_id, turn_id, exc)

    async def _mark_turn_control_completed_best_effort() -> None:
        try:
            await loop.run_in_executor(
                None,
                lambda: firestore.mark_universal_chat_turn_control_completed(turn_id),
            )
        except Exception as exc:
            logger.warning("universal turn control cleanup failed trace=%s turn=%s: %s", chat_trace_id, turn_id, exc)

    platform_update_cadence_answer = _platform_update_cadence_answer(latest_question)
    if platform_update_cadence_answer:
        stage_timings.finish("interpretation")
        async def platform_update_cadence_event_stream():
            stage_timings.start("answer_production")
            visible_answer = _normalize_visible_answer_markdown(platform_update_cadence_answer)
            yield _sse(_status_payload(
                STATUS_PREPARING_ANSWER,
                trace_id=chat_trace_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            ))
            yield _sse({"delta": visible_answer})
            stage_timings.finish("answer_production")
            if not durable_logging_disabled:
                log_payload = {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "trace_id": chat_trace_id,
                    "owner_uid": owner_uid,
                    "owner_email": owner_email,
                    "owner_role": owner_role,
                    "ticket_id": req.ticket_id,
                    "target_ticket_id": None,
                    "latest_question": latest_question,
                    "assistant_answer": visible_answer,
                    "messages": messages[-12:],
                    "phase1_decision": {
                        "tools": [],
                        "reasoning": "Deterministic platform update-cadence answer; skipped Phase 1 planner and final-answer generation.",
                    },
                    "policy_flags": {
                        "platform_update_cadence_answer": True,
                        "selected_executor": "platform_help_update_cadence",
                    },
                    "evidence_snapshot": {
                        "sync_cadence_source": "admin_scheduler_configuration",
                    },
                    "sources": [],
                    "tickets": [],
                    "counts": {
                        "kb": 0,
                        "drive": 0,
                        "similar": 0,
                        "bq": 0,
                        "sources": 0,
                        "tickets": 0,
                        "answer_chars": len(visible_answer),
                        "stage_timings": stage_timings.snapshot(),
                    },
                    "latency_ms": int((time.monotonic() - started_at) * 1000),
                }
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: chat_logging.save_universal_chat_turn(log_payload),
                    )
                except Exception as exc:
                    logger.warning("universal_chat_log save failed trace=%s: %s", chat_trace_id, exc)
            yield _sse({
                "done": True,
                "sources": [],
                "tickets": [],
                "trace_id": chat_trace_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            })
            await _mark_turn_control_completed_best_effort()

        return StreamingResponse(
            platform_update_cadence_event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Fast path: translation request — skip Phase 1, use thinking_budget=0
    last_msg = latest_question
    if last_msg.startswith(universal_chat.TRANSLATE_PREFIX):
        lang_code = last_msg[len(universal_chat.TRANSLATE_PREFIX):]
        # Find the last assistant message to translate
        prev_assistant = next(
            (m["content"] for m in reversed(messages[:-1]) if m["role"] == "assistant"),
            None,
        )
        if prev_assistant:
            stage_timings.finish("interpretation")
            lang_names = {
                "fr": "French", "es": "Spanish", "zh": "Chinese",
                "ru": "Russian", "hi": "Hindi", "en": "English",
            }
            language = lang_names.get(lang_code, lang_code)

            async def translate_event_stream():
                full_text = ""
                stage_timings.start("answer_production")
                yield _sse(_status_payload(
                    STATUS_PREPARING_ANSWER,
                    trace_id=chat_trace_id,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                ))
                try:
                    def _gen():
                        return list(universal_chat.translate_stream(prev_assistant, language))
                    chunks = await loop.run_in_executor(None, _gen)
                    for chunk in chunks:
                        full_text += chunk
                        yield _sse({"delta": chunk})
                        await asyncio.sleep(0)
                except Exception as exc:
                    logger.error("translate stream error: %s", exc)
                    yield _sse({"error": str(exc)})
                stage_timings.finish("answer_production")
                if not durable_logging_disabled:
                    log_payload = {
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                        "trace_id": chat_trace_id,
                        "owner_uid": owner_uid,
                        "owner_email": owner_email,
                        "owner_role": owner_role,
                        "ticket_id": req.ticket_id,
                        "target_ticket_id": None,
                        "latest_question": latest_question,
                        "assistant_answer": full_text.strip(),
                        "messages": messages[-12:],
                        "phase1_decision": {
                            "tools": [],
                            "reasoning": "Translation request; skipped Universal Chat Phase 1 routing.",
                            "translation_language": language,
                        },
                        "policy_flags": {"translation_request": True},
                        "evidence_snapshot": {"translated_from_previous_assistant": prev_assistant},
                        "sources": [],
                        "tickets": [],
                        "counts": {
                            "kb": 0,
                            "drive": 0,
                            "similar": 0,
                            "bq": 0,
                            "sources": 0,
                            "tickets": 0,
                            "answer_chars": len(full_text),
                            "stage_timings": stage_timings.snapshot(),
                        },
                        "latency_ms": int((time.monotonic() - started_at) * 1000),
                    }
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda: chat_logging.save_universal_chat_turn(log_payload),
                        )
                    except Exception as exc:
                        logger.warning("universal_chat_log save failed trace=%s: %s", chat_trace_id, exc)
                yield _sse({
                    "done": True,
                    "sources": [],
                    "tickets": [],
                    "trace_id": chat_trace_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                })
                await _mark_turn_control_completed_best_effort()

            return StreamingResponse(
                translate_event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    open_ticket_metadata = await _fetch_open_ticket_metadata(req.ticket_id, loop)
    semantic = await loop.run_in_executor(
        None,
        lambda: semantic_interpreter.interpret_latest_turn(
            messages,
            open_ticket_id=req.ticket_id,
            open_ticket_metadata=open_ticket_metadata,
        ),
    )
    semantic_frame = semantic.to_dict()
    routing_messages = semantic_interpreter.build_routing_messages(messages, semantic)
    routing_question = routing_messages[-1]["content"] if routing_messages else latest_question
    temporal_resolution = temporal_resolver.resolve_temporal_context(routing_messages)
    store_history_period = _resolve_store_history_period(routing_question)
    stage_timings.finish("interpretation")
    stage_timings.start("routing_phase1")
    stage_timings.start("retrieval")
    logger.info(
        "universal_semantic trace=%s status=%s intent=%s target=%s language=%s confidence=%s normalized=%r fallback=%r",
        chat_trace_id,
        semantic.status,
        semantic.intent,
        semantic.target_context,
        semantic.answer_language,
        semantic.confidence,
        routing_question,
        semantic.fallback_reason,
    )

    # Fetch ticket context only when it is relevant to this turn.
    # Priority:
    # 1) An explicit ticket ID in the question overrides the left-panel ticket.
    # 2) The left-panel ticket is "soft context" and is injected only for
    #    current-ticket follow-ups. Independent store/KB/analytics asks must
    #    behave the same with or without an open left ticket.
    ticket_context = ""
    raw_explicit_ticket_id = _extract_explicit_ticket_id(latest_question)
    routing_explicit_ticket_id = _extract_explicit_ticket_id(routing_question)
    if (
        routing_explicit_ticket_id
        and req.ticket_id
        and semantic.target_context == "open_ticket"
        and int(routing_explicit_ticket_id) == int(req.ticket_id)
        and not raw_explicit_ticket_id
    ):
        routing_explicit_ticket_id = None
    explicit_ticket_id = raw_explicit_ticket_id or routing_explicit_ticket_id
    raw_store_ids = _extract_store_ids(latest_question)
    routing_store_ids = _extract_store_ids(routing_question)
    explicit_store_ids = raw_store_ids or (
        routing_store_ids if semantic.target_context != "open_ticket" else []
    )
    explicit_store_context = explicit_store_ids[0] if len(explicit_store_ids) == 1 else None
    previous_assistant_text = _latest_assistant_text(messages)
    conversation_entities: dict | None = None
    try:
        conversation_entities = await loop.run_in_executor(
            None,
            lambda: firestore.get_universal_conversation_entities(conversation_id),
        )
    except Exception as exc:
        logger.warning("universal: conversation entity load failed trace=%s: %s", chat_trace_id, exc)

    followup_shadow_payload = _build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
        conversation_entities=conversation_entities,
        messages=messages,
    )
    logger.info(
        "universal_followup_shadow trace=%s emitted=%s answer_affecting=%s target=%s reasons=%s",
        chat_trace_id,
        bool(followup_shadow_payload.get("emitted")),
        bool(followup_shadow_payload.get("answer_affecting")),
        followup_shadow_payload.get("target_context"),
        followup_shadow_payload.get("reason_codes"),
    )
    followup_date_scope_payload = _build_followup_date_scope_assist_payload(
        shadow_payload=followup_shadow_payload,
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text=previous_assistant_text,
        conversation_entities=conversation_entities,
        messages=messages,
    )

    open_ticket_store_id = str((open_ticket_metadata or {}).get("store_id") or "").strip()
    task_contract = semantic_task_contracts.build_task_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text=previous_assistant_text,
        semantic_frame=semantic_frame,
        conversation_entities=conversation_entities,
        explicit_store_context=explicit_store_context,
        open_ticket_store_id=(open_ticket_store_id if req.ticket_id else None),
        messages=routing_messages,
    )
    semantic_label_contract = (
        task_contract
        if task_contract
        and task_contract.task_type == "count_by_dimension"
        and task_contract.dimension_type == "semantic_labels"
        else None
    )
    semantic_label_request = (
        semantic_label_counts.SemanticLabelRequest(
            labels=list(semantic_label_contract.labels or []),
            source=semantic_label_contract.dimension_source or "task_contract",
        )
        if semantic_label_contract
        else None
    )
    semantic_label_store_id = semantic_label_contract.store_id if semantic_label_contract else None
    semantic_label_period = semantic_label_contract.period if semantic_label_contract else None
    force_semantic_label_count = bool(
        semantic_label_request
        and semantic_label_store_id
        and semantic_label_period
    )
    target_ticket_id: int | None = None
    ticket_context_reason = "none"

    if explicit_ticket_id:
        target_ticket_id = explicit_ticket_id
        ticket_context_reason = "explicit_ticket"
    elif req.ticket_id and not explicit_store_context and not explicit_store_ids and (
        semantic.target_context == "open_ticket"
        or chat_policy.should_use_open_ticket_context(routing_question)
    ):
        target_ticket_id = req.ticket_id
        ticket_context_reason = "semantic_open_ticket" if semantic.target_context == "open_ticket" else "open_ticket_relevant"

    if target_ticket_id:
        ticket_context = await _fetch_ticket_context(target_ticket_id, loop)
        if ticket_context:
            logger.info(
                "universal: using ticket context trace=%s target_ticket_id=%s reason=%s open_ticket_id=%s",
                chat_trace_id,
                target_ticket_id,
                ticket_context_reason,
                req.ticket_id,
            )
        else:
            logger.info(
                "universal: ticket context unavailable trace=%s target_ticket_id=%s reason=%s open_ticket_id=%s",
                chat_trace_id,
                target_ticket_id,
                ticket_context_reason,
                req.ticket_id,
            )
    elif req.ticket_id:
        logger.info(
            "universal: ignored open ticket context trace=%s open_ticket_id=%s question=%r",
            chat_trace_id,
            req.ticket_id,
            latest_question,
        )

    force_context_only = bool(
        explicit_ticket_id
        and ticket_context
        and not chat_policy.explicit_ticket_request_needs_tools(routing_question)
    )
    semantic_context_only = bool(
        target_ticket_id
        and ticket_context
        and semantic.target_context == "open_ticket"
        and semantic.intent in {"current_ticket_context", "ticket_details"}
        and not chat_policy.explicitly_requests_past_tickets(routing_question)
        and not chat_policy.needs_scoped_store_evidence(routing_question)
    )
    customer_risk_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=latest_question,
        routing_question=routing_question,
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=target_ticket_id,
        explicit_store_id=explicit_store_context,
    )
    semantic_ticket_examples = chat_policy.semantic_frame_requests_ticket_examples(semantic_frame)
    raw_ticket_list_analytics = (
        _looks_like_ticket_list_analytics(latest_question)
        or _looks_like_ticket_list_analytics(routing_question)
    )
    ticket_list_analytics = raw_ticket_list_analytics and not semantic_ticket_examples
    public_update_staleness_sql = (
        _public_update_staleness_sql(routing_question)
        or _public_update_staleness_sql(latest_question)
    )
    global_event_request = _looks_like_global_event_discovery(
        latest_question,
        routing_question,
        semantic,
        explicit_store_context=explicit_store_context,
        explicit_ticket_id=explicit_ticket_id,
        prior_event_text=previous_user_question,
    )
    pos_type_profile_request = (
        chat_policy.extract_pos_type_profile_analytics_request(routing_question)
        or chat_policy.extract_pos_type_profile_analytics_request(latest_question)
    )
    force_pos_type_profile_analytics = bool(pos_type_profile_request)
    route_contract_v2 = semantic_contract_v2.build_shadow_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        open_ticket_id=req.ticket_id,
        target_ticket_id=target_ticket_id,
        explicit_store_context=explicit_store_context,
        explicit_store_ids=explicit_store_ids,
        ticket_list_analytics=ticket_list_analytics,
        public_update_staleness=bool(public_update_staleness_sql),
        task_contract=task_contract,
        customer_risk_request=customer_risk_request,
        global_event_request=global_event_request,
    )
    force_customer_risk = semantic_contract_v2.authorizes_customer_risk_analytics(
        route_contract_v2,
        customer_risk_request=customer_risk_request,
    )
    force_global_event_discovery = semantic_contract_v2.authorizes_global_event_discovery(
        route_contract_v2,
        global_event_request=global_event_request,
        force_customer_risk=force_customer_risk,
    )
    force_public_update_staleness = semantic_contract_v2.authorizes_public_update_analytics(
        route_contract_v2,
        public_update_staleness=bool(public_update_staleness_sql),
        force_customer_risk=force_customer_risk,
        force_global_event_discovery=force_global_event_discovery,
    )
    force_ticket_semantic_search = semantic_contract_v2.authorizes_ticket_semantic_search(
        route_contract_v2,
        force_customer_risk=force_customer_risk,
        force_global_event_discovery=force_global_event_discovery,
        force_context_only=force_context_only,
        semantic_context_only=semantic_context_only,
        schema_limitation=False,
    )
    sla_supported_fields_answer = _sla_supported_fields_answer(routing_question)
    sla_analytics_sql = _sla_analytics_sql(routing_question, explicit_ticket_id=explicit_ticket_id)
    if sla_supported_fields_answer or sla_analytics_sql:
        force_ticket_semantic_search = False
    if force_semantic_label_count:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked to count previously listed semantic complaint labels; "
                "using deterministic same-store label counting."
            ),
        }
    elif force_pos_type_profile_analytics:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked for POS Type store-profile analytics; using deterministic "
                "Zendesk organization profile analytics instead of generated SQL."
            ),
        }
    elif force_customer_risk:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked for Customer Risk / sentiment / repeated public follow-up analysis; "
                "using deterministic Customer Risk analytics instead of generated SQL."
            ),
        }
    elif force_global_event_discovery:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked for global event discovery; using deterministic ticket-text "
                "event search and match classification instead of generated SQL."
            ),
        }
    elif sla_supported_fields_answer:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked for unsupported raw SLA fields; using deterministic "
                "supported-field explanation."
            ),
        }
    elif sla_analytics_sql:
        decision = {
            "tools": ["run_bq_query"],
            "bq_sql": sla_analytics_sql,
            "reasoning": (
                "User asked for SLA analytics over supported derived SLA fields; "
                "using deterministic BigQuery SQL with partial-coverage caveat."
            ),
        }
    elif force_ticket_semantic_search:
        decision = {
            "tools": [],
            "reasoning": (
                "User asked to retrieve tickets matching a described scenario; "
                "using deterministic ticket semantic search from the semantic contract."
            ),
        }
    elif force_context_only or semantic_context_only:
        decision = {
            "tools": [],
            "reasoning": (
                f"User asked about ticket #{target_ticket_id}; "
                "answering from direct ticket context before additional retrieval."
            ),
        }
    else:
        # Phase 1: JSON mode tool decision (ticket context informs routing)
        decision = await loop.run_in_executor(
            None,
            lambda: universal_chat.decide_tools(
                routing_messages,
                ticket_context=ticket_context,
                capture_callback=_capture_model_transition_request if request_capture_enabled else None,
            ),
        )
    direct_ticket_context_evidence_count = 1 if (ticket_context and (force_context_only or semantic_context_only)) else 0
    logger.info("Universal decision: tools=%s", decision.get("tools", []))

    data_sources = firestore.get_data_sources()
    tickets_bq_search_enabled = bool(data_sources.get("tickets_bq", {}).get("enabled", False))
    any_vertex_search_enabled = any(
        cfg.get("type") == "vertex_ai" and cfg.get("enabled", False)
        for cfg in data_sources.values()
    )
    any_drive_search_enabled = any(
        sid.startswith("gdrive_") and cfg.get("enabled", False)
        for sid, cfg in data_sources.items()
    )

    tools = decision.get("tools") or []
    tools_before_policy = list(tools)
    if not tickets_bq_search_enabled:
        tools = [t for t in tools if t not in {"search_similar_tickets", "run_bq_query"}]
    if not any_vertex_search_enabled:
        tools = [t for t in tools if t != "search_knowledge_base"]
    if not any_drive_search_enabled:
        tools = [t for t in tools if t != "search_drive_exact"]
    decision["tools"] = tools
    force_drive_tabular = chat_policy.should_force_drive_tabular_lookup(
        routing_question,
        latest_question,
        semantic_frame=semantic_frame,
    )
    raw_force_store_issue_breakdown = _is_store_issue_breakdown_question(routing_question)
    force_store_issue_breakdown = raw_force_store_issue_breakdown and not force_semantic_label_count
    force_contextual_category_count = bool(
        tickets_bq_search_enabled
        and ticket_context
        and target_ticket_id
        and _is_contextual_category_frequency_followup(routing_question)
    )
    count_list_filter_sql = _count_list_filter_consistency_sql(messages, routing_question)
    temporal_bq_sql = temporal_resolver.build_bq_sql(temporal_resolution) if temporal_resolution else None
    semantic_issue_value = (semantic.entities or {}).get("issue_or_category")
    semantic_issue_filter = bool(semantic_issue_value)
    if semantic_issue_filter and (
        _issue_filter_is_generic_ticket_noun(routing_question, semantic_issue_value)
        or _issue_filter_is_generic_ticket_noun(latest_question, semantic_issue_value)
    ):
        semantic_issue_filter = False
    schema_limitation = _analytics_schema_limitation(routing_question)
    schema_limitation_sql = _schema_limitation_resolution_sql(routing_question, schema_limitation)
    selected_executor = semantic_contract_v2.selected_executor_label(
        force_semantic_label_count=force_semantic_label_count,
        force_customer_risk=force_customer_risk,
        force_global_event_discovery=force_global_event_discovery,
        force_ticket_semantic_search=force_ticket_semantic_search,
        public_update_staleness=force_public_update_staleness,
        temporal_ticket_analytics=bool(
            tickets_bq_search_enabled
            and temporal_bq_sql
            and not semantic_issue_filter
            and not force_semantic_label_count
        ),
        force_context_only=force_context_only,
        semantic_context_only=semantic_context_only,
        schema_limitation=bool(schema_limitation),
    )
    if force_pos_type_profile_analytics:
        selected_executor = "pos_type_profile_analytics"
    contract_v2 = semantic_contract_v2.build_shadow_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        open_ticket_id=req.ticket_id,
        target_ticket_id=target_ticket_id,
        explicit_store_context=explicit_store_context,
        explicit_store_ids=explicit_store_ids,
        ticket_list_analytics=ticket_list_analytics,
        public_update_staleness=force_public_update_staleness,
        force_context_only=force_context_only,
        semantic_context_only=semantic_context_only,
        task_contract=task_contract,
        customer_risk_request=customer_risk_request,
        global_event_request=global_event_request,
        temporal_resolution=temporal_resolution,
        selected_executor=selected_executor,
    )
    ticket_resolution_lookup_payload = _build_ticket_resolution_lookup_assist_payload(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=target_ticket_id,
        open_ticket_id=req.ticket_id,
    )
    policy_flags = {
        "semantic_frame": semantic_frame,
        "semantic_contract_v2": contract_v2.to_dict(),
        "semantic_contract_v2_route_gate": {
            "gate": "global_event_discovery",
            "contract_executor": route_contract_v2.executor,
            "contract_intent": route_contract_v2.intent,
            "authorized": force_global_event_discovery,
        },
        "semantic_contract_v2_public_update_gate": {
            "gate": "public_update_analytics",
            "contract_executor": route_contract_v2.executor,
            "contract_intent": route_contract_v2.intent,
            "authorized": force_public_update_staleness,
        },
        "semantic_contract_v2_customer_risk_gate": {
            "gate": "customer_risk_analytics",
            "contract_executor": route_contract_v2.executor,
            "contract_intent": route_contract_v2.intent,
            "authorized": force_customer_risk,
        },
        "semantic_contract_v2_ticket_semantic_search_gate": {
            "gate": "ticket_semantic_search",
            "contract_executor": route_contract_v2.executor,
            "contract_intent": route_contract_v2.intent,
            "authorized": force_ticket_semantic_search,
        },
        "selected_executor": selected_executor,
        "routing_question": routing_question,
        "explicit_store_ids": explicit_store_ids,
        "semantic_issue_filter": semantic_issue_filter,
        "schema_limited_analytics": schema_limitation,
        "sla_analytics": {
            "supported_fields_answer": bool(sla_supported_fields_answer),
            "forced_sql": bool(sla_analytics_sql),
            "partial_coverage_caveat": bool(sla_supported_fields_answer or sla_analytics_sql),
        },
        "store_history_period": (
            {
                "display_label": store_history_period.display_label,
                "start_date": store_history_period.start_date.isoformat(),
                "end_date_exclusive": store_history_period.end_date_exclusive.isoformat(),
                "matched_text": store_history_period.matched_text,
            }
            if store_history_period else None
        ),
        "semantic_label_count": (
            {
                "store_id": semantic_label_store_id,
                "labels": semantic_label_request.labels,
                "label_source": semantic_label_request.source,
                "period": semantic_label_period.display_label,
                "start_date": semantic_label_period.start_date,
                "end_date_exclusive": semantic_label_period.end_date_exclusive,
            }
            if force_semantic_label_count and semantic_label_request and semantic_label_period
            else None
        ),
        "global_event_discovery": global_event_request,
        "pos_type_profile_analytics": (
            {
                "mode": pos_type_profile_request.mode,
                "pos_type": pos_type_profile_request.pos_type,
                "pos_types": list(pos_type_profile_request.pos_types),
                "brand": pos_type_profile_request.brand,
                "start_date": pos_type_profile_request.start_date,
                "end_date": pos_type_profile_request.end_date,
            }
            if pos_type_profile_request
            else None
        ),
        "customer_risk_analytics": (
            {
                "scope": customer_risk_request.scope,
                "ticket_id": customer_risk_request.ticket_id,
                "store_id": customer_risk_request.store_id,
                "followup_min": customer_risk_request.followup_min,
                "require_after_agent_followups": customer_risk_request.require_after_agent_followups,
                "since_days": customer_risk_request.since_days,
                "minimum_score": customer_risk_request.minimum_score,
                "limit": customer_risk_request.limit,
            }
            if customer_risk_request
            else None
        ),
        "task_contract": task_contract.to_dict() if task_contract else None,
        "followup_understanding_shadow": followup_shadow_payload,
        "followup_date_scope_assist": followup_date_scope_payload,
        "ticket_resolution_lookup_assist": ticket_resolution_lookup_payload,
        "conversation_entities_loaded": bool(conversation_entities),
        "force_store_issue_breakdown": force_store_issue_breakdown,
        "store_issue_breakdown_suppressed_by_task_contract": bool(
            raw_force_store_issue_breakdown and force_semantic_label_count
        ),
        "force_semantic_label_count": force_semantic_label_count,
        "force_contextual_category_count": force_contextual_category_count,
        "count_list_filter_consistency": bool(count_list_filter_sql),
        "semantic_ticket_examples": semantic_ticket_examples,
        "ticket_list_analytics_suppressed_by_ticket_examples": bool(
            raw_ticket_list_analytics and semantic_ticket_examples
        ),
        "public_update_staleness": bool(public_update_staleness_sql),
        "force_public_update_staleness": force_public_update_staleness,
        "force_ticket_semantic_search": force_ticket_semantic_search,
        "force_drive_tabular": force_drive_tabular,
        "suppress_similar_tickets": chat_policy.should_suppress_similar_tickets(
            routing_question,
            semantic_frame=semantic_frame,
        ),
        "explicit_ticket_id": explicit_ticket_id,
        "raw_explicit_ticket_id": raw_explicit_ticket_id,
        "open_ticket_id": req.ticket_id,
        "target_ticket_id": target_ticket_id,
        "ticket_context_reason": ticket_context_reason,
        "ticket_context_used": bool(ticket_context),
        "temporal_resolution": (
            {
                "intent": temporal_resolution.intent,
                "period_type": temporal_resolution.period_type,
                "display_label": temporal_resolution.display_label,
                "start_at": temporal_resolution.start_at.isoformat(),
                "end_at": temporal_resolution.end_at.isoformat(),
                "source_plan": temporal_resolution.source_plan,
            }
            if temporal_resolution
            else None
        ),
    }
    ai_dispatcher_future = loop.run_in_executor(
        None,
        lambda: ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question=latest_question,
            routing_question=routing_question,
            messages=messages,
            semantic_frame=semantic_frame,
            conversation_id=conversation_id,
            turn_id=turn_id,
            trace_id=chat_trace_id,
            owner_role=owner_role,
            explicit_store_ids=explicit_store_ids,
            explicit_ticket_id=explicit_ticket_id,
            previous_assistant_text=previous_assistant_text,
        ),
    )
    ai_dispatcher_trace: dict | None = None

    async def _resolve_ai_dispatcher_trace() -> dict:
        nonlocal ai_dispatcher_trace
        if ai_dispatcher_trace is not None:
            return ai_dispatcher_trace
        try:
            ai_dispatcher_result = await ai_dispatcher_future
            ai_dispatcher_trace = ai_dispatcher_result.to_trace_dict()
        except Exception as exc:
            logger.warning("ai_dispatcher: shadow future failed trace=%s: %s", chat_trace_id, exc)
            ai_dispatcher_trace = {
                "mode": "unknown",
                "eligible": False,
                "planner_attempted": False,
                "planner_calls": 0,
                "answer_affecting": False,
                "accepted": False,
                "fallback_reason": "runtime_exception",
                "reason_codes": ["runtime_exception"],
                "model_surface": "universal_dispatcher_planner",
            }
        return ai_dispatcher_trace

    source_family_dispatcher_applied = False
    ticket_semantic_execution_contract: dict | None = None
    source_family_mismatch_candidate = ai_dispatcher_runtime.source_family_mismatch_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
    )
    source_family_incompatible_route = any(
        (
            force_semantic_label_count,
            force_pos_type_profile_analytics,
            force_customer_risk,
            force_global_event_discovery,
            force_public_update_staleness,
            force_context_only,
            semantic_context_only,
            bool(sla_supported_fields_answer or sla_analytics_sql),
            bool(schema_limitation),
            force_store_issue_breakdown,
            force_contextual_category_count,
            force_drive_tabular,
        )
    )
    if source_family_mismatch_candidate and not force_ticket_semantic_search:
        if not tickets_bq_search_enabled:
            policy_flags["ai_dispatcher_source_family_mismatch_skipped"] = "tickets_bq_disabled"
        elif source_family_incompatible_route:
            policy_flags["ai_dispatcher_source_family_mismatch_skipped"] = "incompatible_deterministic_route"
        else:
            source_family_dispatcher_trace = await _resolve_ai_dispatcher_trace()
            source_family_dispatcher_allowed = (
                bool(source_family_dispatcher_trace.get("answer_affecting"))
                and source_family_dispatcher_trace.get("assistive_task_class")
                == ai_dispatcher_runtime.ASSISTIVE_CLASS_SOURCE_FAMILY_MISMATCH
            )
            if source_family_dispatcher_allowed:
                ticket_semantic_execution_contract = _ai_dispatcher_source_family_ticket_contract(
                    source_family_dispatcher_trace,
                    latest_question=latest_question,
                    routing_question=routing_question,
                    base_contract=contract_v2,
                )
                if ticket_semantic_execution_contract:
                    source_family_dispatcher_applied = True
                    force_ticket_semantic_search = True
                    selected_executor = "ticket_semantic_search"
                    policy_flags["force_ticket_semantic_search"] = True
                    policy_flags["selected_executor"] = selected_executor
                    policy_flags["ai_dispatcher_source_family_mismatch"] = {
                        "applied": True,
                        "contract_source": ticket_semantic_execution_contract["source"],
                        "store_ids": ticket_semantic_execution_contract["filters"].get("store_ids") or [],
                        "required_concepts": ticket_semantic_execution_contract["filters"].get("required_concepts") or [],
                    }
                    policy_flags["semantic_contract_v2_ticket_semantic_search_gate"]["authorized"] = True
                    decision = {
                        "tools": [],
                        "reasoning": (
                            "AI dispatcher Phase 6A corrected a ticket-example source-family mismatch; "
                            "using deterministic ticket semantic search."
                        ),
                    }
                    logger.info(
                        "universal: dispatcher source-family mismatch applied trace=%s stores=%s",
                        chat_trace_id,
                        ticket_semantic_execution_contract["filters"].get("store_ids") or [],
                    )
                else:
                    policy_flags["ai_dispatcher_source_family_mismatch_skipped"] = "missing_bounded_ticket_search_contract"
            else:
                policy_flags["ai_dispatcher_source_family_mismatch_skipped"] = "dispatcher_not_answer_affecting"

    if force_pos_type_profile_analytics:
        tools = []
        decision["tools"] = tools
        decision["bq_sql"] = ""
        logger.info(
            "universal: forced POS Type profile analytics trace=%s request=%s",
            chat_trace_id,
            policy_flags["pos_type_profile_analytics"],
        )
    elif force_customer_risk:
        tools = []
        decision["tools"] = tools
        decision["bq_sql"] = ""
        logger.info(
            "universal: forced customer risk analytics trace=%s scope=%s ticket=%s store=%s",
            chat_trace_id,
            customer_risk_request.scope if customer_risk_request else None,
            customer_risk_request.ticket_id if customer_risk_request else None,
            customer_risk_request.store_id if customer_risk_request else None,
        )
    elif sla_supported_fields_answer:
        tools = []
        decision["tools"] = tools
        decision["bq_sql"] = ""
        logger.info("universal: forced SLA supported-fields answer trace=%s", chat_trace_id)
    elif tickets_bq_search_enabled and sla_analytics_sql:
        tools = ["run_bq_query"]
        decision["tools"] = tools
        decision["bq_sql"] = sla_analytics_sql
        logger.info("universal: forced SLA analytics trace=%s", chat_trace_id)
    elif force_ticket_semantic_search:
        tools = []
        decision["tools"] = tools
        decision["bq_sql"] = ""
        logger.info(
            "universal: forced ticket semantic search trace=%s scope=%s confidence=%s",
            chat_trace_id,
            route_contract_v2.scope,
            route_contract_v2.confidence,
        )
    elif schema_limitation:
        tools = ["run_bq_query"] if tickets_bq_search_enabled and schema_limitation_sql else []
        decision["tools"] = tools
        decision["bq_sql"] = schema_limitation_sql
        decision["reasoning"] = (
            "User requested ticket analytics fields that are absent from the current "
            "BigQuery schema; using a deterministic schema-limitation answer and only "
            "running a clearly labeled supported substitute when available."
        )
        logger.info(
            "universal: forced schema-limited analytics trace=%s fields=%s substitute=%s",
            chat_trace_id,
            schema_limitation.get("unavailable_fields"),
            bool(schema_limitation_sql),
        )
    elif tickets_bq_search_enabled and force_public_update_staleness and public_update_staleness_sql:
        tools = ["run_bq_query"]
        decision["bq_sql"] = public_update_staleness_sql
        decision["reasoning"] = (
            "User asked for unresolved tickets with missing/stale public comments; "
            "using ticket_comments.is_internal = FALSE to compute last public update."
        )
        decision["tools"] = tools
        logger.info(
            "universal: forced public update staleness analytics trace=%s question=%r",
            chat_trace_id,
            routing_question,
        )
    elif force_store_issue_breakdown:
        # Deterministic guard:
        # For explicit "store + top issues/problems" analytics questions, model-generated
        # SQL can drift when an unrelated ticket is open in the left panel. Bypass generated
        # SQL and use the store-scoped content breakdown path below.
        tools = ["run_bq_query"] if tickets_bq_search_enabled else []
        decision["bq_sql"] = ""
        decision["tools"] = tools
        logger.info(
            "universal: forced deterministic store issue breakdown trace=%s question=%r",
            chat_trace_id,
            routing_question,
        )
    elif force_contextual_category_count and target_ticket_id:
        # Deterministic guard for short follow-ups such as "How many times has
        # this category of issue been reported?" With an open ticket, "this
        # category" means the current ticket's issue_category.
        tools = ["run_bq_query"]
        decision["bq_sql"] = _contextual_category_count_sql(target_ticket_id)
        decision["reasoning"] = (
            "User asked for frequency of the open ticket's category; "
            "using current ticket issue_category as the BigQuery filter."
        )
        decision["tools"] = tools
        logger.info(
            "universal: forced contextual category count trace=%s ticket_id=%s question=%r",
            chat_trace_id,
            target_ticket_id,
            routing_question,
        )
    elif tickets_bq_search_enabled and count_list_filter_sql:
        tools = ["run_bq_query"]
        decision["bq_sql"] = count_list_filter_sql
        decision["reasoning"] = (
            "User is continuing a current-ticket count/list chain; preserving "
            "the same store-id range, status, and assigned-group filters."
        )
        decision["tools"] = tools
        logger.info(
            "universal: forced count/list filter consistency trace=%s question=%r",
            chat_trace_id,
            routing_question,
        )
    elif force_drive_tabular:
        # Deterministic guard for spreadsheet/table lookup questions.
        # Phase 1 occasionally drifts to KB-only selection for escalation/team-owner asks.
        # Ensure Drive exact search path always runs for these questions.
        tools = [t for t in tools if t in {"search_drive_exact", "search_similar_tickets"}]
        if chat_policy.should_suppress_similar_tickets(routing_question, semantic_frame=semantic_frame):
            tools = [t for t in tools if t != "search_similar_tickets"]
        if any_drive_search_enabled and "search_drive_exact" not in tools:
            tools.insert(0, "search_drive_exact")
        if not any_drive_search_enabled:
            tools = [t for t in tools if t != "search_drive_exact"]
        if chat_policy.is_row_provenance_question(routing_question) and previous_user_question:
            decision["drive_query"] = previous_user_question
            decision["drive_intent"] = "search"
            decision.pop("drive_file_name", None)
        else:
            # Keep the original engineer phrasing for tabular lookups. Phase 1
            # may compact "who do I escalate DispatchFlow issues to?" to just
            # "DispatchFlow Issues", which loses the team/escalation intent that
            # row filtering needs to avoid mixing section-owner rows with child
            # task rows.
            decision["drive_query"] = latest_question
            decision["drive_intent"] = "search"
            decision.pop("drive_file_name", None)
        decision["tools"] = tools
        logger.info(
            "universal: forced drive tabular path trace=%s tools=%s query=%r",
            chat_trace_id,
            tools,
            decision.get("drive_query"),
        )
    elif tickets_bq_search_enabled and temporal_bq_sql and not semantic_issue_filter and not force_semantic_label_count:
        tools = ["run_bq_query"]
        decision["tools"] = tools
        decision["bq_sql"] = temporal_bq_sql
        decision["reasoning"] = (
            "Temporal ticket analytics resolved deterministically in America/New_York; "
            f"period={temporal_resolution.display_label}; intent={temporal_resolution.intent}."
        )
        logger.info(
            "universal: forced temporal analytics path trace=%s intent=%s period=%s",
            chat_trace_id,
            temporal_resolution.intent,
            temporal_resolution.display_label,
        )
    elif tickets_bq_search_enabled and temporal_bq_sql and semantic_issue_filter and not force_semantic_label_count:
        policy_flags["semantic_skipped_temporal_override"] = (
            "preserving issue/category filter from normalized question and Phase 1 SQL"
        )

    if (
        semantic.target_context == "open_ticket"
        and semantic.intent == "event_fact_check"
        and "search_similar_tickets" in tools
        and not chat_policy.explicitly_requests_past_tickets(routing_question)
        and not chat_policy.requests_other_restaurant_comparison(routing_question)
    ):
        tools = [t for t in tools if t != "search_similar_tickets"]
        decision["tools"] = tools
        policy_flags["semantic_suppressed_similar_tickets"] = "event_fact_check_current_scope"
        logger.info(
            "universal: semantic guard suppressed global similar tickets trace=%s intent=%s target=%s",
            chat_trace_id,
            semantic.intent,
            semantic.target_context,
        )

    logger.info(
        "universal_trace policy trace=%s flags=%s tools_before=%s tools_after=%s",
        chat_trace_id,
        policy_flags,
        tools_before_policy,
        tools,
    )

    stage_timings.finish("routing_phase1")

    # Phase 1.5: parallel data gathering
    awaitables: dict[str, object] = {}

    if "search_knowledge_base" in tools and decision.get("kb_query"):
        awaitables["kb"] = _run_kb(decision["kb_query"], loop)

    drive_hint_allowed, drive_hint_skip_reason = _drive_hint_policy(
        any_drive_search_enabled=any_drive_search_enabled,
        force_drive_tabular=force_drive_tabular,
        selected_executor=selected_executor,
        tools=tools,
        has_explicit_drive_lookup=bool(decision.get("drive_query") or decision.get("drive_file_name")),
    )
    if "search_drive_exact" in tools and (decision.get("drive_query") or decision.get("drive_file_name")):
        awaitables["drive"] = _run_drive_exact(
            query=decision.get("drive_query") or decision.get("drive_file_name"),
            intent=decision.get("drive_intent", "search"),
            file_name=decision.get("drive_file_name"),
            loop=loop,
        )
    elif drive_hint_allowed:
        awaitables["drive_hint"] = _run_drive_exact(
            query=latest_question,
            intent="search",
            file_name=None,
            loop=loop,
        )
    elif drive_hint_skip_reason in _DRIVE_HINT_BLOCKED_EXECUTORS:
        policy_flags["drive_hint_skipped_non_drive_executor"] = drive_hint_skip_reason

    if "search_similar_tickets" in tools and decision.get("tickets_query"):
        awaitables["similar"] = _run_similar_tickets(decision["tickets_query"], loop)

    if "run_bq_query" in tools and decision.get("bq_sql") and not force_store_issue_breakdown:
        awaitables["bq"] = _run_bq_query(decision["bq_sql"], loop)

    kb_results = None
    drive_chunks = None
    similar_tickets = None
    bq_rows = None
    live_zendesk_recent_tickets: list[dict] | None = None
    live_zendesk_recent_answer: str | None = None
    zendesk_temporal_tail_tickets: list[dict] | None = None
    zendesk_temporal_hybrid_answer: str | None = None
    zendesk_temporal_fallback_tickets: list[dict] | None = None
    zendesk_temporal_fallback_answer: str | None = None
    global_event_discovery_answer: str | None = None
    global_event_ticket_cards: list[dict] = []
    ticket_semantic_search_answer: str | None = None
    ticket_semantic_ticket_cards: list[dict] = []
    followup_date_scope_answer: str | None = None
    ticket_resolution_lookup_answer: str | None = None
    ticket_resolution_lookup_result: dict | None = None
    ticket_resolution_ticket_cards: list[dict] = []
    result_set_comparison_request = ai_dispatcher_result_sets.detect_result_set_comparison_request(latest_question)
    bulk_ticket_lookup_request = ai_dispatcher_result_sets.detect_bulk_ticket_lookup_request(
        latest_question,
        previous_assistant_text=previous_assistant_text,
        semantic_frame=semantic_frame,
    )
    challenge_fact_check_request = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
        latest_question,
        previous_assistant_text=previous_assistant_text,
        semantic_frame=semantic_frame,
    )
    followup_result_set_candidate = followup_result_set_assist.detect_followup_result_set_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        previous_assistant_text=previous_assistant_text,
    )
    if followup_result_set_candidate.is_candidate:
        policy_flags["followup_result_set_candidate"] = followup_result_set_candidate.to_dict()
    followup_result_set_payload: dict[str, Any] = {}
    followup_result_set_refinement_answer: str | None = None
    followup_ticket_reference_answer: str | None = None
    followup_ticket_reference_lookup_result: dict | None = None
    result_set_comparison_answer: str | None = None
    result_set_comparison: dict | None = None
    bulk_ticket_lookup_answer: str | None = None
    bulk_ticket_lookup_result: dict | None = None
    challenge_fact_check_answer: str | None = None
    challenge_fact_check_result: dict | None = None
    result_set_memory_saved: dict | None = None
    customer_risk_answer: str | None = None
    customer_risk_ticket_cards: list[dict] = []
    semantic_label_count_answer: str | None = None
    semantic_label_ticket_cards: list[dict] = []
    pos_type_profile_answer: str | None = None
    pos_type_profile_ticket_cards: list[dict] = []
    pos_type_profile_result: dict | None = None
    kb_search_error: str | None = None
    tool_latency_ms: dict[str, int] = {}

    if awaitables:
        gathered = await asyncio.gather(
            *(_timed_tool_result(key, awaitable) for key, awaitable in awaitables.items())
        )
        for key, result, elapsed_ms in gathered:
            tool_latency_ms[key] = elapsed_ms
            if key == "kb":
                kb_results, kb_search_error = result
            elif key in {"drive", "drive_hint"}:
                drive_chunks = result
            elif key == "similar":
                similar_tickets = result
            elif key == "bq":
                bq_rows = result

    if force_pos_type_profile_analytics and pos_type_profile_request:
        if tickets_bq_search_enabled:
            try:
                pos_started = time.monotonic()
                if pos_type_profile_request.mode == "comparison":
                    pos_type_profile_result = await loop.run_in_executor(
                        None,
                        lambda: pos_type_analytics.get_pos_type_comparison(
                            list(pos_type_profile_request.pos_types),
                            start_date=pos_type_profile_request.start_date,
                            end_date=pos_type_profile_request.end_date,
                            brand=pos_type_profile_request.brand,
                            status_filter=pos_type_profile_request.status_filter,
                            limit=8,
                        ),
                    )
                    pos_type_profile_answer = _pos_type_comparison_answer(
                        pos_type_profile_result,
                        pos_type_profile_request,
                    )
                else:
                    pos_type_profile_result = await loop.run_in_executor(
                        None,
                        lambda: pos_type_analytics.get_pos_type_analytics(
                            pos_type_profile_request.pos_type or "",
                            start_date=pos_type_profile_request.start_date,
                            end_date=pos_type_profile_request.end_date,
                            brand=pos_type_profile_request.brand,
                            status_filter=pos_type_profile_request.status_filter,
                            limit=8,
                        ),
                    )
                    pos_type_profile_answer = _pos_type_analytics_answer(
                        pos_type_profile_result,
                        pos_type_profile_request,
                    )
                    pos_type_profile_ticket_cards = _pos_type_ticket_cards(pos_type_profile_result)
                    bq_rows = (pos_type_profile_result or {}).get("repeated_problems") or []
                tool_latency_ms["bq_pos_type_profile_analytics"] = int(
                    (time.monotonic() - pos_started) * 1000
                )
                decision["bq_sql"] = (
                    "DETERMINISTIC POS TYPE PROFILE ANALYTICS: "
                    f"mode={pos_type_profile_request.mode}, "
                    f"pos_type={pos_type_profile_request.pos_type or list(pos_type_profile_request.pos_types)}, "
                    "profile_table=zendesk_organization_profiles, primary_join=tickets.org_id, "
                    "fallback=store_id_fallback_labeled."
                )
                policy_flags["pos_type_profile_analytics_result"] = {
                    "available": bool((pos_type_profile_result or {}).get("available", True)),
                    "coverage": (pos_type_profile_result or {}).get("coverage"),
                    "open_ticket_cards": len(pos_type_profile_ticket_cards),
                }
                logger.info(
                    "universal: applied POS Type profile analytics trace=%s mode=%s",
                    chat_trace_id,
                    pos_type_profile_request.mode,
                )
            except Exception as exc:
                logger.warning("universal: POS Type profile analytics failed trace=%s: %s", chat_trace_id, exc)
                policy_flags["pos_type_profile_analytics_error"] = str(exc)
                pos_type_profile_answer = (
                    "I identified this as POS Type profile analytics, but the deterministic "
                    f"analytics helper failed: {str(exc)[:300]}\n\n"
                    'SOURCES_JSON: {"sources": []}'
                )
        else:
            pos_type_profile_answer = (
                "Ticket analytics search is disabled in Admin Data Sources, so I cannot compute "
                "POS Type profile analytics right now.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
            decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    if force_customer_risk and customer_risk_request:
        if customer_risk_request.scope == "ticket" or tickets_bq_search_enabled:
            try:
                risk_started = time.monotonic()
                risk_payload = await loop.run_in_executor(
                    None,
                    lambda: customer_risk_analytics.build_customer_risk_chat_response(customer_risk_request),
                )
                tool_latency_ms["customer_risk_analytics"] = int(
                    (time.monotonic() - risk_started) * 1000
                )
                customer_risk_answer = str(risk_payload.get("answer") or "")
                customer_risk_ticket_cards = [
                    _normalize_ticket_card(card)
                    for card in (risk_payload.get("ticket_cards") or [])
                    if isinstance(card, dict)
                ]
                customer_risk_ticket_cards = [card for card in customer_risk_ticket_cards if card]
                bq_rows = risk_payload.get("rows") or []
                decision["bq_sql"] = str(risk_payload.get("bq_summary") or "")
                policy_flags["customer_risk_analytics_result"] = {
                    "scope": risk_payload.get("scope"),
                    "row_count": len(bq_rows) if isinstance(bq_rows, list) else 0,
                    "ticket_cards": len(customer_risk_ticket_cards),
                }
                logger.info(
                    "universal: applied customer risk analytics trace=%s scope=%s rows=%d",
                    chat_trace_id,
                    risk_payload.get("scope"),
                    len(bq_rows) if isinstance(bq_rows, list) else 0,
                )
            except Exception as exc:
                provenance = error_provenance.record_exception_best_effort(
                    exc,
                    endpoint_class="customer_risk_analytics",
                    source_hint="esip",
                )
                logger.warning(
                    "universal customer_risk failure trace=%s outcome=%s reason_code=%s",
                    chat_trace_id,
                    provenance["outcome"],
                    provenance["reason_code"],
                )
                policy_flags["customer_risk_analytics_error"] = provenance
        else:
            customer_risk_answer = (
                "Ticket analytics search is disabled in Admin Data Sources, so I cannot compute "
                "broad Customer Risk lists from public comments right now.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
            decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    if force_global_event_discovery and global_event_request:
        if tickets_bq_search_enabled:
            period = global_event_request.get("period") or {}
            try:
                event_started = time.monotonic()
                event_rows = await loop.run_in_executor(
                    None,
                    lambda: bigquery.discover_global_event_tickets(
                        global_event_request.get("terms") or [],
                        start_date=period.get("start_date"),
                        end_date=period.get("end_date"),
                        limit=700,
                    ),
                )
                tool_latency_ms["bq_global_event_discovery"] = int(
                    (time.monotonic() - event_started) * 1000
                )
                bq_rows = event_rows
                global_event_discovery_answer = _global_event_discovery_answer(
                    global_event_request,
                    event_rows,
                    semantic.answer_language,
                )
                global_event_ticket_cards = []
                for row in event_rows:
                    if row.get("event_match_type") not in {"confirmed_candidate", "keyword_candidate"}:
                        continue
                    card = _normalize_ticket_card(row)
                    if card:
                        global_event_ticket_cards.append(card)
                    if len(global_event_ticket_cards) >= 20:
                        break
                decision["bq_sql"] = (
                    "DETERMINISTIC GLOBAL EVENT DISCOVERY: "
                    f"terms={global_event_request.get('terms')}, "
                    f"period={period.get('label')}, rows_checked={len(event_rows)}. "
                    "Rows are classified as confirmed_candidate, related_non_confirming, noise, or keyword_candidate."
                )
                policy_flags["global_event_discovery_counts"] = {
                    "rows": len(event_rows),
                    "confirmed": sum(1 for row in event_rows if row.get("event_match_type") == "confirmed_candidate"),
                    "related": sum(1 for row in event_rows if row.get("event_match_type") == "related_non_confirming"),
                    "noise": sum(1 for row in event_rows if row.get("event_match_type") == "noise"),
                    "keyword": sum(1 for row in event_rows if row.get("event_match_type") == "keyword_candidate"),
                }
                logger.info(
                    "universal: applied global event discovery trace=%s terms=%s rows=%d counts=%s",
                    chat_trace_id,
                    global_event_request.get("terms"),
                    len(event_rows),
                    policy_flags["global_event_discovery_counts"],
                )
            except Exception as exc:
                logger.warning("universal: global event discovery failed trace=%s: %s", chat_trace_id, exc)
                policy_flags["global_event_discovery_error"] = str(exc)
        else:
            global_event_discovery_answer = (
                "Ticket analytics search is disabled in Admin Data Sources.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
            decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    if force_ticket_semantic_search:
        if tickets_bq_search_enabled:
            try:
                ticket_search_started = time.monotonic()
                semantic_search_contract = ticket_semantic_execution_contract or contract_v2.to_dict()
                semantic_search_filters = (
                    semantic_search_contract.get("filters")
                    if isinstance(semantic_search_contract.get("filters"), dict)
                    else {}
                )
                ticket_search_limit = 50 if len(semantic_search_filters.get("store_ids") or []) > 1 else 20
                ticket_search_payload = await loop.run_in_executor(
                    None,
                    lambda: bigquery.search_tickets_semantic_contract(
                        semantic_search_contract,
                        limit=ticket_search_limit,
                        candidate_limit=700,
                    ),
                )
                tool_latency_ms["bq_ticket_semantic_search"] = int(
                    (time.monotonic() - ticket_search_started) * 1000
                )
                ticket_search_rows = ticket_search_payload.get("rows") or []
                bq_rows = ticket_search_rows
                ticket_semantic_search_answer = _ticket_semantic_search_answer(
                    semantic_search_contract,
                    ticket_search_payload,
                    semantic.answer_language,
                )
                ticket_semantic_ticket_cards = [
                    _normalize_ticket_card(row)
                    for row in ticket_search_rows
                    if isinstance(row, dict)
                ]
                ticket_semantic_ticket_cards = [card for card in ticket_semantic_ticket_cards if card]
                decision["bq_sql"] = str(ticket_search_payload.get("bq_summary") or "")
                policy_flags["ticket_semantic_search_result"] = ticket_search_payload.get("summary") or {}
                if ticket_semantic_execution_contract:
                    policy_flags["ticket_semantic_search_execution_contract"] = ticket_semantic_execution_contract
                result_set_meta = ticket_search_payload.get("result_set") if isinstance(ticket_search_payload, dict) else {}
                if (
                    isinstance(result_set_meta, dict)
                    and result_set_meta.get("ticket_ids")
                    and not result_set_comparison_request.is_comparison
                ):
                    try:
                        result_set_payload = ai_dispatcher_result_sets.build_result_set_payload(
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            trace_id=chat_trace_id,
                            source_family=str(result_set_meta.get("source_family") or "tickets"),
                            requested_store_ids=result_set_meta.get("requested_store_ids") or explicit_store_ids,
                            criteria=result_set_meta.get("criteria") or [],
                            ticket_rows=ticket_search_rows,
                            ticket_ids=result_set_meta.get("ticket_ids") or [],
                            displayed_ticket_ids=result_set_meta.get("displayed_ticket_ids") or [],
                            query_fingerprint=str(result_set_meta.get("query_fingerprint") or ""),
                            freshness=result_set_meta.get("freshness") or {},
                        )
                        result_set_memory_saved = await loop.run_in_executor(
                            None,
                            lambda: ai_dispatcher_result_sets.save_result_set(result_set_payload),
                        )
                        policy_flags["ai_dispatcher_result_set_saved"] = ai_dispatcher_result_sets.summarize_payload(result_set_payload)
                    except Exception as exc:
                        policy_flags["ai_dispatcher_result_set_save_error"] = str(exc)
                        logger.warning("universal: result-set save failed trace=%s: %s", chat_trace_id, exc)
                logger.info(
                    "universal: applied ticket semantic search trace=%s rows=%d summary=%s",
                    chat_trace_id,
                    len(ticket_search_rows),
                    policy_flags["ticket_semantic_search_result"],
                )
            except Exception as exc:
                logger.warning("universal: ticket semantic search failed trace=%s: %s", chat_trace_id, exc)
                policy_flags["ticket_semantic_search_error"] = str(exc)
        else:
            ticket_semantic_search_answer = (
                "Ticket analytics search is disabled in Admin Data Sources.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
            decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    if force_semantic_label_count and semantic_label_request and semantic_label_period and semantic_label_store_id:
        if tickets_bq_search_enabled:
            start_date = semantic_label_period.start_date
            end_date = semantic_label_period.end_date_exclusive
            try:
                label_started = time.monotonic()
                label_rows = await loop.run_in_executor(
                    None,
                    lambda: bigquery.fetch_store_tickets_for_semantic_label_count(
                        store_id=semantic_label_store_id,
                        start_date=start_date,
                        end_date=end_date,
                        limit=500,
                    ),
                )
                tool_latency_ms["bq_semantic_label_count"] = int((time.monotonic() - label_started) * 1000)
                label_results = semantic_label_counts.count_labels(semantic_label_request.labels, label_rows)
                semantic_label_count_answer = semantic_label_counts.build_answer(
                    store_id=semantic_label_store_id,
                    period_label=semantic_label_period.display_label,
                    results=label_results,
                    answer_language=semantic.answer_language,
                )
                matched_ids = set(semantic_label_counts.unique_ticket_ids(label_results))
                semantic_label_ticket_cards = [
                    _normalize_ticket_card(row)
                    for row in label_rows
                    if int(row.get("id") or 0) in matched_ids
                ]
                semantic_label_ticket_cards = [card for card in semantic_label_ticket_cards if card]
                bq_rows = [
                    {
                        "complaint_label": result.label,
                        "ticket_count": result.count,
                        "ticket_ids": ", ".join(f"#{tid}" for tid in result.ticket_ids),
                    }
                    for result in label_results
                ]
                decision["bq_sql"] = (
                    "DETERMINISTIC SEMANTIC COMPLAINT LABEL COUNT: "
                    f"store_id={semantic_label_store_id}, period={semantic_label_period.display_label}, "
                    f"labels={semantic_label_request.labels}, rows_checked={len(label_rows)}. "
                    "Labels are descriptive and may overlap; not official Zendesk issue_category values."
                )
                policy_flags["semantic_label_count_rows_checked"] = len(label_rows)
                policy_flags["semantic_label_count_unique_tickets"] = len(matched_ids)
                logger.info(
                    "universal: applied semantic label count trace=%s store=%s labels=%d rows=%d unique=%d",
                    chat_trace_id,
                    semantic_label_store_id,
                    len(semantic_label_request.labels),
                    len(label_rows),
                    len(matched_ids),
                )
            except Exception as exc:
                logger.warning("universal: semantic label count failed trace=%s: %s", chat_trace_id, exc)
                policy_flags["semantic_label_count_error"] = str(exc)
        else:
            semantic_label_count_answer = (
                "Ticket analytics search is disabled in Admin Data Sources.\n\n"
                'SOURCES_JSON: {"sources": []}'
            )
            decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    # Deterministic store-level "top issues" path. This intentionally bypasses Phase 1 SQL
    # for this intent so behavior is identical with or without an open ticket in the left panel.
    fallback_store_id = _extract_store_id(routing_question)
    if tickets_bq_search_enabled and force_store_issue_breakdown and fallback_store_id:
        lookback_days = _extract_lookback_days(routing_question, default_days=90)
        start_date, end_date = _period_date_bounds(store_history_period)
        scope_label = _store_history_scope_label(store_history_period, lookback_days)
        try:
            fallback_started = time.monotonic()
            fallback = await loop.run_in_executor(
                None,
                lambda: bigquery.infer_store_issue_breakdown_from_content(
                    store_id=fallback_store_id,
                    lookback_days=lookback_days,
                    limit=8,
                    start_date=start_date,
                    end_date=end_date,
                ),
            )
            tool_latency_ms["bq_fallback_store_issue"] = int((time.monotonic() - fallback_started) * 1000)
            fallback_rows = fallback.get("rows", []) if isinstance(fallback, dict) else []
            summary = fallback.get("summary", {}) if isinstance(fallback, dict) else {}
            if fallback_rows:
                bq_rows = fallback_rows
                total_tickets = int(summary.get("total_tickets", 0) or 0)
                missing_categories = int(summary.get("missing_issue_category", 0) or 0)
                fallback_note = (
                    f"FALLBACK CONTENT ANALYSIS: store_id={fallback_store_id}, scope={scope_label}, "
                    f"total_tickets={total_tickets}, missing_issue_category={missing_categories}"
                )
                decision["bq_sql"] = fallback_note
                logger.info(
                    "universal: applied content issue fallback trace=%s store=%s lookback_days=%d",
                    chat_trace_id,
                    fallback_store_id,
                    lookback_days,
                )
        except Exception as exc:
            logger.warning("universal: content issue fallback failed: %s", exc)
    elif force_store_issue_breakdown and not tickets_bq_search_enabled:
        bq_rows = "Ticket analytics search is disabled in Admin Data Sources."
        decision["bq_sql"] = "DISABLED: tickets_bq Search is off"

    # Legacy safety fallback for other BQ paths when issue_category is sparse/missing.
    if (
        not force_store_issue_breakdown
        and not force_semantic_label_count
        and tickets_bq_search_enabled
        and "run_bq_query" in tools
        and fallback_store_id
        and _question_requests_issue_breakdown(routing_question)
        and _bq_rows_missing_issue_breakdown(bq_rows)
    ):
        lookback_days = _extract_lookback_days(routing_question, default_days=90)
        start_date, end_date = _period_date_bounds(store_history_period)
        scope_label = _store_history_scope_label(store_history_period, lookback_days)
        try:
            fallback_started = time.monotonic()
            fallback = await loop.run_in_executor(
                None,
                lambda: bigquery.infer_store_issue_breakdown_from_content(
                    store_id=fallback_store_id,
                    lookback_days=lookback_days,
                    limit=8,
                    start_date=start_date,
                    end_date=end_date,
                ),
            )
            tool_latency_ms["bq_fallback_legacy_issue"] = int((time.monotonic() - fallback_started) * 1000)
            fallback_rows = fallback.get("rows", []) if isinstance(fallback, dict) else []
            summary = fallback.get("summary", {}) if isinstance(fallback, dict) else {}
            if fallback_rows:
                bq_rows = fallback_rows
                total_tickets = int(summary.get("total_tickets", 0) or 0)
                missing_categories = int(summary.get("missing_issue_category", 0) or 0)
                fallback_note = (
                    f"FALLBACK CONTENT ANALYSIS: store_id={fallback_store_id}, scope={scope_label}, "
                    f"total_tickets={total_tickets}, missing_issue_category={missing_categories}"
                )
                orig_sql = (decision.get("bq_sql") or "").strip()
                decision["bq_sql"] = f"{orig_sql}\n-- {fallback_note}" if orig_sql else fallback_note
                logger.info(
                    "universal: applied legacy content issue fallback trace=%s store=%s lookback_days=%d",
                    chat_trace_id,
                    fallback_store_id,
                    lookback_days,
                )
        except Exception as exc:
            logger.warning("universal: legacy content issue fallback failed: %s", exc)

    # Deterministic same-store history fallback for broad explicit-ticket analysis.
    # Phase 1 can generate malformed SQL for complex Russian/English asks. If that
    # happens, still use the known store id from the ticket context instead of
    # letting Phase 2 infer that no same-store history exists.
    if (
        tickets_bq_search_enabled
        and explicit_ticket_id
        and ticket_context
        and chat_policy.explicit_ticket_request_needs_tools(routing_question)
        and (
            bq_rows is None
            or (isinstance(bq_rows, list) and len(bq_rows) == 0)
            or (isinstance(bq_rows, str) and "query error:" in bq_rows.lower())
        )
    ):
        context_store_id = _extract_store_id_from_ticket_context(ticket_context)
        if context_store_id:
            lookback_days = _extract_lookback_days(routing_question, default_days=365)
            start_date, end_date = _period_date_bounds(store_history_period)
            scope_label = _store_history_scope_label(store_history_period, lookback_days)
            terms = _ticket_analysis_terms(routing_question, ticket_context)
            try:
                fallback_started = time.monotonic()
                store_rows = await loop.run_in_executor(
                    None,
                    lambda: bigquery.fetch_store_ticket_examples(
                        store_id=context_store_id,
                        lookback_days=lookback_days,
                        terms=terms,
                        limit=20,
                        start_date=start_date,
                        end_date=end_date,
                    ),
                )
                tool_latency_ms["bq_fallback_same_store_history"] = int(
                    (time.monotonic() - fallback_started) * 1000
                )
                if store_rows:
                    bq_rows = store_rows
                    failed_sql = (decision.get("bq_sql") or "").strip()
                    decision["bq_sql"] = (
                        "DETERMINISTIC SAME-STORE HISTORY FALLBACK: "
                        f"store_id={context_store_id}, scope={scope_label}, "
                        f"terms={terms}. Generated analytics SQL failed or returned no usable rows."
                        + (f"\nFAILED GENERATED SQL: {failed_sql[:1200]}" if failed_sql else "")
                    )
                    logger.info(
                        "universal: applied same-store history fallback trace=%s store=%s rows=%d",
                        chat_trace_id,
                        context_store_id,
                        len(store_rows),
                    )
            except Exception as exc:
                logger.warning("universal: same-store history fallback failed: %s", exc)

    # Generic scoped store-history evidence. This is not keyword/event specific:
    # it gives the model a bounded same-store history snapshot with explicit
    # store/date/ticket scope so it can reason about "this store", events,
    # before/after windows, and challenges to prior answers without treating
    # conversation history or global similar tickets as same-store evidence.
    if (
        tickets_bq_search_enabled
        and ticket_context
        and not force_contextual_category_count
        and _should_build_store_context_snapshot(routing_question, ticket_context, explicit_ticket_id)
    ):
        context_store_id = _extract_store_id_from_ticket_context(ticket_context)
        if context_store_id:
            lookback_days = max(_extract_lookback_days(routing_question, default_days=1200), 1200)
            start_date, end_date = _period_date_bounds(store_history_period)
            scope_label = _store_history_scope_label(store_history_period, lookback_days)
            snapshot_rows: list[dict] = []
            snapshot_error: str | None = None
            try:
                snapshot_started = time.monotonic()
                snapshot_rows = await loop.run_in_executor(
                    None,
                    lambda: bigquery.fetch_store_history_snapshot(
                        store_id=context_store_id,
                        lookback_days=lookback_days,
                        limit=160 if store_history_period else 120,
                        start_date=start_date,
                        end_date=end_date,
                    ),
                )
                tool_latency_ms["bq_same_store_context_snapshot"] = int(
                    (time.monotonic() - snapshot_started) * 1000
                )
            except Exception as exc:
                snapshot_error = str(exc)
                logger.warning("universal: same-store context snapshot failed: %s", exc)

            if not snapshot_rows:
                try:
                    zendesk_started = time.monotonic()
                    zendesk_rows = await _run_store_zendesk_history(
                        context_store_id,
                        store_history_period,
                        lookback_days,
                        loop,
                        limit=120,
                    )
                    tool_latency_ms["zendesk_same_store_context_snapshot"] = int(
                        (time.monotonic() - zendesk_started) * 1000
                    )
                    if zendesk_rows:
                        snapshot_rows = _zendesk_store_history_rows_to_snapshot_rows(zendesk_rows)
                        policy_flags["zendesk_same_store_context_snapshot"] = {
                            "store_id": context_store_id,
                            "scope": scope_label,
                            "ticket_count": len(snapshot_rows),
                            "reason": "bq_empty_or_error",
                        }
                except Exception as exc:
                    policy_flags["zendesk_same_store_context_snapshot_error"] = str(exc)
                    logger.warning("universal: same-store Zendesk context fallback failed: %s", exc)

            if snapshot_rows:
                source_label = "Zendesk" if str(snapshot_rows[0].get("source") or "").lower() == "zendesk" else "BigQuery"
                bq_rows = _store_context_snapshot_rows(
                    snapshot_rows,
                    (
                        f"Keyword-free same-store snapshot from {source_label}. Scope: {scope_label}. "
                        "Use only rows with this scope for current-store claims; do not treat missing rows outside this scope as proof."
                    ),
                )
                decision["bq_sql"] = (
                    "DETERMINISTIC STORE CONTEXT SNAPSHOT: "
                    f"store_id={context_store_id}, scope={scope_label}, source={source_label}, rows={len(snapshot_rows)}, "
                    "keyword_filter=none. The model must infer events/themes only from these scoped rows."
                    + (f" BQ snapshot error before Zendesk fallback: {snapshot_error[:400]}" if snapshot_error else "")
                )
                logger.info(
                    "universal: applied same-store context snapshot trace=%s store=%s source=%s rows=%d scope=%s",
                    chat_trace_id,
                    context_store_id,
                    source_label,
                    len(snapshot_rows),
                    scope_label,
                )

    if (
        similar_tickets
        and ticket_context
        and chat_policy.requests_other_restaurant_comparison(routing_question)
    ):
        context_store_id = _extract_store_id_from_ticket_context(ticket_context)
        similar_tickets, removed_current_store = _filter_out_store_tickets(similar_tickets, context_store_id)
        if removed_current_store:
            policy_flags["filtered_current_store_from_global_similar"] = removed_current_store
            logger.info(
                "universal: filtered current-store tickets from global similar set trace=%s store=%s removed=%d",
                chat_trace_id,
                context_store_id,
                removed_current_store,
            )

    if similar_tickets:
        similar_tickets, removed_low_value = _filter_low_value_similar_tickets(similar_tickets)
        if removed_low_value:
            policy_flags["filtered_low_value_similar_tickets"] = removed_low_value
            logger.info(
                "universal: filtered low-value similar tickets trace=%s removed=%d",
                chat_trace_id,
                removed_low_value,
            )

    live_window = None if (semantic_issue_filter or force_semantic_label_count) else _should_run_live_zendesk_overlay(routing_question, bq_rows)
    if live_window:
        try:
            live_started = time.monotonic()
            live_zendesk_recent_tickets = await _run_live_zendesk_recent_tickets(live_window, loop)
            tool_latency_ms["zendesk_live_recent"] = int((time.monotonic() - live_started) * 1000)
            live_zendesk_recent_answer = _live_zendesk_recent_answer(
                routing_question,
                live_window,
                live_zendesk_recent_tickets,
            )
            policy_flags["zendesk_live_recent_overlay"] = {
                "date": live_window["date"],
                "label": live_window["label"],
                "ticket_count": len(live_zendesk_recent_tickets or []),
                "reason": "fresh_relative_ticket_analytics",
            }
            logger.info(
                "universal: live Zendesk recent overlay trace=%s date=%s tickets=%d answer=%s",
                chat_trace_id,
                live_window["date"],
                len(live_zendesk_recent_tickets or []),
                bool(live_zendesk_recent_answer),
            )
        except Exception as exc:
            policy_flags["zendesk_live_recent_overlay_error"] = str(exc)
            logger.warning("universal: live Zendesk recent overlay failed trace=%s: %s", chat_trace_id, exc)

    if (
        live_zendesk_recent_answer is None
        and temporal_resolution
        and not semantic_issue_filter
        and not force_semantic_label_count
        and temporal_resolution.source_plan == "bq_plus_zendesk_tail"
        and not isinstance(bq_rows, str)
    ):
        tail_window = _temporal_zendesk_tail_window(temporal_resolution)
        if tail_window:
            try:
                tail_started = time.monotonic()
                zendesk_temporal_tail_tickets = await _run_temporal_zendesk_tail_tickets(
                    tail_window,
                    temporal_resolution.event_type,
                    loop,
                )
                tool_latency_ms["zendesk_temporal_tail"] = int((time.monotonic() - tail_started) * 1000)
                zendesk_temporal_hybrid_answer = _temporal_hybrid_answer(
                    temporal_resolution,
                    bq_rows,
                    zendesk_temporal_tail_tickets,
                    tail_window,
                )
                policy_flags["zendesk_temporal_tail"] = {
                    "display_label": temporal_resolution.display_label,
                    "intent": temporal_resolution.intent,
                    "ticket_count": len(zendesk_temporal_tail_tickets or []),
                    "tail_start": tail_window["start_at"].isoformat(),
                    "tail_end": tail_window["end_at"].isoformat(),
                    "reason": "bq_plus_zendesk_tail",
                }
                logger.info(
                    "universal: temporal Zendesk tail trace=%s period=%s tickets=%d answer=%s",
                    chat_trace_id,
                    temporal_resolution.display_label,
                    len(zendesk_temporal_tail_tickets or []),
                    bool(zendesk_temporal_hybrid_answer),
                )
            except Exception as exc:
                policy_flags["zendesk_temporal_tail_error"] = str(exc)
                logger.warning("universal: temporal Zendesk tail failed trace=%s: %s", chat_trace_id, exc)

    if (
        live_zendesk_recent_answer is None
        and zendesk_temporal_hybrid_answer is None
        and temporal_resolution
        and not semantic_issue_filter
        and not force_semantic_label_count
        and _bq_zero_or_error(bq_rows)
        and temporal_resolution.source_plan in {"bq_then_zendesk_if_empty", "zendesk_only", "bq_plus_zendesk_tail"}
    ):
        try:
            zendesk_started = time.monotonic()
            zendesk_temporal_fallback_tickets = await _run_temporal_zendesk_tickets(
                temporal_resolution,
                loop,
            )
            tool_latency_ms["zendesk_temporal_fallback"] = int((time.monotonic() - zendesk_started) * 1000)
            zendesk_temporal_fallback_answer = _zendesk_temporal_answer(
                temporal_resolution,
                zendesk_temporal_fallback_tickets,
            )
            policy_flags["zendesk_temporal_fallback"] = {
                "display_label": temporal_resolution.display_label,
                "intent": temporal_resolution.intent,
                "ticket_count": len(zendesk_temporal_fallback_tickets or []),
                "reason": "bq_zero_or_error",
            }
            logger.info(
                "universal: temporal Zendesk fallback trace=%s period=%s tickets=%d answer=%s",
                chat_trace_id,
                temporal_resolution.display_label,
                len(zendesk_temporal_fallback_tickets or []),
                bool(zendesk_temporal_fallback_answer),
            )
        except Exception as exc:
            policy_flags["zendesk_temporal_fallback_error"] = str(exc)
            logger.warning("universal: temporal Zendesk fallback failed trace=%s: %s", chat_trace_id, exc)

    should_try_drive_fallback = (
        live_zendesk_recent_answer is None
        and drive_chunks is None
        and not force_store_issue_breakdown
        and not force_drive_tabular
        and bool(routing_question.strip())
        and "search_knowledge_base" in tools
        and (
            kb_results is None
            or len(kb_results or []) == 0
            or bool(kb_search_error)
        )
    )
    if should_try_drive_fallback:
        drive_fallback_started = time.monotonic()
        drive_chunks = await _run_drive_exact(
            query=routing_question,
            intent="search",
            file_name=None,
            loop=loop,
        )
        tool_latency_ms["drive_fallback"] = int((time.monotonic() - drive_fallback_started) * 1000)
        if drive_chunks:
            decision["drive_intent"] = "search"
            logger.info(
                "Universal drive fallback used trace=%s question=%r chunks=%d",
                chat_trace_id,
                routing_question,
                len(drive_chunks),
            )

    logger.info(
        "universal_trace results trace=%s counts=%s bq_sql=%r",
        chat_trace_id,
        {
            "kb": len(kb_results or []),
            "drive": len(drive_chunks or []),
            "similar": len(similar_tickets or []),
            "bq": len(bq_rows) if isinstance(bq_rows, list) else (1 if isinstance(bq_rows, str) else 0),
            "zendesk_live_recent": len(live_zendesk_recent_tickets or []),
            "zendesk_temporal_tail": len(zendesk_temporal_tail_tickets or []),
            "zendesk_temporal_fallback": len(zendesk_temporal_fallback_tickets or []),
            "customer_risk": len(customer_risk_ticket_cards or []),
            "ticket_semantic_search": len(ticket_semantic_ticket_cards or []),
        },
        decision.get("bq_sql", ""),
    )

    # Authoritative KB retrieval status for the model (and ops logs)
    kb_status: dict | None = None
    if "search_knowledge_base" in tools and not decision.get("kb_query"):
        kb_status = {
            "state": "skipped_missing_query",
            "reason": "Phase 1 listed search_knowledge_base but did not set kb_query.",
        }
    elif "search_knowledge_base" not in tools:
        rsn = (decision.get("reasoning") or "").strip()
        kb_status = {
            "state": "not_selected",
            "reason": (
                f"search_knowledge_base not in selected tools. Router: {rsn}"
                if rsn
                else "search_knowledge_base not in selected tools for this turn."
            ),
        }
    if "search_knowledge_base" in tools and decision.get("kb_query"):
        if kb_search_error:
            kb_status = {
                "state": "error",
                "query": decision["kb_query"],
                "error": kb_search_error,
            }
        else:
            kb_status = {
                "state": "executed",
                "query": decision["kb_query"],
                "result_count": len(kb_results or []),
            }
    log_kb_status("universal", kb_status)
    if followup_date_scope_payload.get("accepted"):
        followup_date_scope_enabled = await loop.run_in_executor(
            None,
            ai_dispatcher_runtime.followup_date_scope_assist_enabled,
        )
        if isinstance(policy_flags.get("followup_date_scope_assist"), dict):
            policy_flags["followup_date_scope_assist"]["enabled"] = followup_date_scope_enabled
        if not followup_date_scope_enabled:
            policy_flags["followup_date_scope_assist_skipped"] = "assistive_class_disabled"
        elif not tickets_bq_search_enabled:
            policy_flags["followup_date_scope_assist_skipped"] = "tickets_bq_disabled"
        elif result_set_comparison_request.is_comparison or bulk_ticket_lookup_request.is_lookup or challenge_fact_check_request.is_fact_check:
            policy_flags["followup_date_scope_assist_skipped"] = "incompatible_followup_intent"
        else:
            followup_sql = _followup_date_scope_assist_bq_sql(followup_date_scope_payload)
            if not followup_sql:
                policy_flags["followup_date_scope_assist_skipped"] = "missing_bounded_sql"
            else:
                try:
                    lookup_started = time.monotonic()
                    followup_rows = await _run_bq_query(followup_sql, loop)
                    tool_latency_ms["bq_followup_date_scope"] = int((time.monotonic() - lookup_started) * 1000)
                    followup_date_scope_answer = _build_followup_date_scope_answer(
                        assist_payload=followup_date_scope_payload,
                        rows=followup_rows,
                    )
                    bq_rows = followup_rows
                    decision["bq_sql"] = followup_sql
                    if isinstance(policy_flags.get("followup_date_scope_assist"), dict):
                        policy_flags["followup_date_scope_assist"]["answer_affecting"] = bool(followup_date_scope_answer)
                        policy_flags["followup_date_scope_assist"]["row_count"] = (
                            len(followup_rows) if isinstance(followup_rows, list) else 0
                        )
                    if isinstance(followup_rows, list):
                        for row in followup_rows:
                            card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                            if card:
                                ticket_semantic_ticket_cards.append(card)
                    logger.info(
                        "universal: applied followup date-scope assist trace=%s rows=%s",
                        chat_trace_id,
                        len(followup_rows) if isinstance(followup_rows, list) else "error",
                    )
                except Exception as exc:
                    policy_flags["followup_date_scope_assist_error"] = str(exc)
                    logger.warning("universal: followup date-scope assist failed trace=%s: %s", chat_trace_id, exc)
    if ticket_resolution_lookup_payload.get("accepted"):
        ticket_resolution_enabled = await loop.run_in_executor(
            None,
            ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled,
        )
        if isinstance(policy_flags.get("ticket_resolution_lookup_assist"), dict):
            policy_flags["ticket_resolution_lookup_assist"]["enabled"] = ticket_resolution_enabled
        incompatible_resolution_route = bool(
            result_set_comparison_request.is_comparison
            or bulk_ticket_lookup_request.is_lookup
            or challenge_fact_check_request.is_fact_check
            or followup_date_scope_answer
            or force_semantic_label_count
            or force_pos_type_profile_analytics
            or force_customer_risk
            or force_global_event_discovery
            or force_public_update_staleness
            or force_ticket_semantic_search
        )
        if not ticket_resolution_enabled:
            policy_flags["ticket_resolution_lookup_assist_skipped"] = "assistive_class_disabled"
        elif not tickets_bq_search_enabled:
            policy_flags["ticket_resolution_lookup_assist_skipped"] = "tickets_bq_disabled"
        elif incompatible_resolution_route:
            policy_flags["ticket_resolution_lookup_assist_skipped"] = "incompatible_route"
        else:
            try:
                target_resolution_ticket_id = int(ticket_resolution_lookup_payload.get("target_ticket_id") or 0)
                lookup_started = time.monotonic()
                ticket_resolution_lookup_result = await loop.run_in_executor(
                    None,
                    lambda: bigquery.bulk_lookup_ticket_ids([target_resolution_ticket_id], cap=1),
                )
                tool_latency_ms["bq_ticket_resolution_lookup"] = int((time.monotonic() - lookup_started) * 1000)
                ticket_resolution_lookup_answer = _build_ticket_resolution_lookup_answer(
                    assist_payload=ticket_resolution_lookup_payload,
                    lookup_result=ticket_resolution_lookup_result,
                    answer_language=semantic.answer_language,
                )
                bq_rows = ticket_resolution_lookup_result.get("rows") or []
                decision["bq_sql"] = str(
                    ticket_resolution_lookup_result.get("bq_summary") or "BOUNDED TICKET RESOLUTION LOOKUP"
                )
                for row in bq_rows:
                    card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                    if card:
                        ticket_resolution_ticket_cards.append(card)
                documented = bool(
                    any(
                        ticket_resolution_lookup_assist.documented_resolution_text((row or {}).get("resolution"))
                        for row in bq_rows
                        if isinstance(row, dict)
                    )
                )
                if isinstance(policy_flags.get("ticket_resolution_lookup_assist"), dict):
                    policy_flags["ticket_resolution_lookup_assist"]["answer_affecting"] = bool(ticket_resolution_lookup_answer)
                    policy_flags["ticket_resolution_lookup_assist"]["row_count"] = len(bq_rows) if isinstance(bq_rows, list) else 0
                policy_flags["ai_dispatcher_ticket_resolution_lookup"] = (
                    ticket_resolution_lookup_assist.summarize_ticket_resolution_lookup(
                        ticket_resolution_lookup_result or {},
                        documented_resolution=documented,
                    )
                )
                logger.info(
                    "universal: applied ticket resolution lookup trace=%s ticket_id=%s found=%d documented=%s",
                    chat_trace_id,
                    target_resolution_ticket_id,
                    len(ticket_resolution_lookup_result.get("found_ids") or []),
                    documented,
                )
            except Exception as exc:
                policy_flags["ticket_resolution_lookup_assist_error"] = str(exc)
                logger.warning("universal: ticket resolution lookup failed trace=%s: %s", chat_trace_id, exc)
                ticket_resolution_lookup_answer = (
                    "I recognized this as a ticket-resolution question, but the bounded ticket lookup failed, "
                    f"so I cannot honestly say how it was resolved yet. Lookup error: {str(exc)[:300]}\n\n"
                    'SOURCES_JSON: {"sources": []}'
                )
    if (
        followup_result_set_candidate.is_candidate
        and not result_set_comparison_request.is_comparison
        and not bulk_ticket_lookup_request.is_lookup
        and not challenge_fact_check_request.is_fact_check
    ):
        dispatcher_followup_config = await loop.run_in_executor(
            None,
            ai_dispatcher_runtime.get_dispatcher_config,
        )
        result_set_refinement_enabled = ai_dispatcher_runtime.followup_result_set_refinement_enabled(
            dispatcher_followup_config
        )
        ticket_reference_enabled = ai_dispatcher_runtime.followup_ticket_reference_enabled(
            dispatcher_followup_config
        )
        result_set_refinement_shadow_enabled = (
            ai_dispatcher_runtime.followup_result_set_refinement_shadow_enabled(
                dispatcher_followup_config
            )
        )
        ticket_reference_shadow_enabled = (
            ai_dispatcher_runtime.followup_ticket_reference_shadow_enabled(
                dispatcher_followup_config
            )
        )
        incompatible_result_set_route = bool(
            followup_date_scope_answer
            or ticket_resolution_lookup_answer
            or force_semantic_label_count
            or force_pos_type_profile_analytics
            or force_customer_risk
            or force_global_event_discovery
            or force_public_update_staleness
            or force_ticket_semantic_search
        )
        if not (
            result_set_refinement_enabled
            or ticket_reference_enabled
            or result_set_refinement_shadow_enabled
            or ticket_reference_shadow_enabled
        ):
            policy_flags["followup_result_set_assist_skipped"] = "assistive_class_disabled"
        elif incompatible_result_set_route:
            policy_flags["followup_result_set_assist_skipped"] = "incompatible_route"
            if result_set_refinement_shadow_enabled:
                policy_flags["followup_result_set_refinement_shadow"] = (
                    _build_followup_result_set_refinement_shadow_payload(
                        rejection_reason="incompatible_route"
                    )
                )
            if ticket_reference_shadow_enabled:
                policy_flags["followup_ticket_reference_shadow"] = (
                    _build_followup_ticket_reference_shadow_payload(
                        rejection_reason="incompatible_route"
                    )
                )
        else:
            previous_result_set_for_followup: dict | None = None
            try:
                previous_result_set_for_followup = await loop.run_in_executor(
                    None,
                    lambda: ai_dispatcher_result_sets.load_latest_result_set(conversation_id),
                )
                if previous_result_set_for_followup:
                    followup_result_set_payload = _build_followup_result_set_assist_payload(
                        latest_question=latest_question,
                        routing_question=routing_question,
                        semantic_frame=semantic_frame,
                        previous_assistant_text=previous_assistant_text,
                        result_set=previous_result_set_for_followup,
                    )
                    if result_set_refinement_shadow_enabled:
                        policy_flags["followup_result_set_refinement_shadow"] = (
                            followup_result_set_assist.build_result_set_refinement_shadow_observation(
                                followup_result_set_payload
                            )
                        )
                    if ticket_reference_shadow_enabled:
                        policy_flags["followup_ticket_reference_shadow"] = (
                            followup_result_set_assist.build_ticket_reference_shadow_observation(
                                followup_result_set_payload
                            )
                        )
                    if result_set_refinement_enabled or ticket_reference_enabled:
                        policy_flags["followup_result_set_assist"] = (
                            followup_result_set_assist.summarize_followup_result_set_assist(
                                followup_result_set_payload
                            )
                        )
                    request_kind = str(followup_result_set_payload.get("request_kind") or "")
                    if request_kind in {"result_set_refinement", "omitted_result_set"}:
                        if not result_set_refinement_enabled:
                            if ticket_reference_enabled:
                                policy_flags["followup_result_set_refinement_skipped"] = "assistive_class_disabled"
                            continue_result_set_cards = False
                        else:
                            continue_result_set_cards = True
                        followup_result_set_refinement_answer = _build_followup_result_set_refinement_answer(
                            assist_payload=followup_result_set_payload,
                            result_set=previous_result_set_for_followup,
                            answer_language=semantic.answer_language,
                        ) if continue_result_set_cards else None
                        if isinstance(policy_flags.get("followup_result_set_assist"), dict):
                            policy_flags["followup_result_set_assist"]["enabled"] = result_set_refinement_enabled
                            policy_flags["followup_result_set_assist"]["answer_affecting"] = bool(
                                followup_result_set_refinement_answer
                            )
                        if continue_result_set_cards:
                            rows_by_id = {
                                int((row or {}).get("ticket_id") or (row or {}).get("id") or 0): row
                                for row in (previous_result_set_for_followup.get("tickets") or [])
                                if isinstance(row, dict)
                            }
                            selected_ids = list(followup_result_set_payload.get("selected_ticket_ids") or [])
                            if not selected_ids and request_kind == "omitted_result_set":
                                selected_ids = list(followup_result_set_payload.get("omitted_ticket_ids") or [])
                            for ticket_id in selected_ids[:12]:
                                row = rows_by_id.get(int(ticket_id or 0))
                                card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                                if card:
                                    ticket_semantic_ticket_cards.append(card)
                    elif request_kind == "ticket_reference_lookup":
                        if isinstance(policy_flags.get("followup_result_set_assist"), dict):
                            policy_flags["followup_result_set_assist"]["enabled"] = ticket_reference_enabled
                        if not ticket_reference_enabled:
                            if result_set_refinement_enabled:
                                policy_flags["followup_ticket_reference_skipped"] = "assistive_class_disabled"
                        elif not tickets_bq_search_enabled:
                            policy_flags["followup_ticket_reference_skipped"] = "tickets_bq_disabled"
                        else:
                            target_ticket_id = int(followup_result_set_payload.get("target_ticket_id") or 0)
                            lookup_started = time.monotonic()
                            followup_ticket_reference_lookup_result = await loop.run_in_executor(
                                None,
                                lambda: bigquery.bulk_lookup_ticket_ids([target_ticket_id], cap=1),
                            )
                            tool_latency_ms["bq_followup_ticket_reference"] = int(
                                (time.monotonic() - lookup_started) * 1000
                            )
                            followup_ticket_reference_answer = _build_followup_ticket_reference_answer(
                                assist_payload=followup_result_set_payload,
                                lookup_result=followup_ticket_reference_lookup_result,
                                result_set=previous_result_set_for_followup,
                                answer_language=semantic.answer_language,
                            )
                            bq_rows = followup_ticket_reference_lookup_result.get("rows") or []
                            decision["bq_sql"] = str(
                                followup_ticket_reference_lookup_result.get("bq_summary")
                                or "BOUNDED FOLLOW-UP TICKET REFERENCE LOOKUP"
                            )
                            for row in bq_rows[:1]:
                                card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                                if card:
                                    ticket_semantic_ticket_cards.append(card)
                            if isinstance(policy_flags.get("followup_result_set_assist"), dict):
                                policy_flags["followup_result_set_assist"]["answer_affecting"] = bool(
                                    followup_ticket_reference_answer
                                )
                                policy_flags["followup_result_set_assist"]["row_count"] = (
                                    len(bq_rows) if isinstance(bq_rows, list) else 0
                                )
                            policy_flags["followup_ticket_reference_lookup"] = (
                                _summarize_followup_ticket_reference_lookup(
                                    followup_ticket_reference_lookup_result
                                )
                            )
                            logger.info(
                                "universal: applied followup ticket reference trace=%s ticket_id=%s found=%d",
                                chat_trace_id,
                                target_ticket_id,
                                len(followup_ticket_reference_lookup_result.get("found_ids") or []),
                            )
                    else:
                        policy_flags["followup_result_set_assist_skipped"] = (
                            followup_result_set_payload.get("reason_codes") or ["unsupported_result_set_followup"]
                        )
                else:
                    policy_flags["followup_result_set_assist_skipped"] = "no_saved_result_set"
                    if result_set_refinement_shadow_enabled:
                        policy_flags["followup_result_set_refinement_shadow"] = (
                            _build_followup_result_set_refinement_shadow_payload(
                                rejection_reason="missing_result_set"
                            )
                        )
                    if ticket_reference_shadow_enabled:
                        policy_flags["followup_ticket_reference_shadow"] = (
                            _build_followup_ticket_reference_shadow_payload(
                                rejection_reason="missing_result_set"
                            )
                        )
            except Exception as exc:
                policy_flags["followup_result_set_assist_error"] = str(exc)
                if result_set_refinement_shadow_enabled:
                    policy_flags["followup_result_set_refinement_shadow"] = (
                        _build_followup_result_set_refinement_shadow_payload(
                            rejection_reason="shadow_evaluation_failed"
                        )
                    )
                if ticket_reference_shadow_enabled:
                    policy_flags["followup_ticket_reference_shadow"] = (
                        _build_followup_ticket_reference_shadow_payload(
                            rejection_reason="shadow_evaluation_failed"
                        )
                    )
                logger.warning("universal: followup result-set assist failed trace=%s: %s", chat_trace_id, exc)
    if result_set_comparison_request.is_comparison:
        if tickets_bq_search_enabled:
            previous_result_set: dict | None = None
            try:
                previous_result_set = await loop.run_in_executor(
                    None,
                    lambda: ai_dispatcher_result_sets.load_latest_result_set(conversation_id),
                )
                if previous_result_set:
                    lookup_started = time.monotonic()
                    lookup_result = await loop.run_in_executor(
                        None,
                        lambda: bigquery.bulk_lookup_ticket_ids(list(result_set_comparison_request.ticket_ids)),
                    )
                    tool_latency_ms["bq_bulk_ticket_lookup"] = int((time.monotonic() - lookup_started) * 1000)
                    result_set_comparison = ai_dispatcher_result_sets.compare_ticket_sets(
                        previous_result_set,
                        list(result_set_comparison_request.ticket_ids),
                        lookup_result,
                    )
                    result_set_comparison_answer = ai_dispatcher_result_sets.build_comparison_answer(
                        result_set_comparison,
                        answer_language=semantic.answer_language,
                    )
                    bq_rows = lookup_result.get("rows") or []
                    for row in bq_rows:
                        card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                        if card:
                            ticket_semantic_ticket_cards.append(card)
                    decision["bq_sql"] = str(lookup_result.get("bq_summary") or "BOUNDED BULK TICKET LOOKUP")
                    policy_flags["ai_dispatcher_result_set_comparison"] = ai_dispatcher_result_sets.summarize_comparison(
                        result_set_comparison
                    )
                    logger.info(
                        "universal: applied result-set comparison trace=%s previous=%s user_ids=%d",
                        chat_trace_id,
                        previous_result_set.get("memory_id"),
                        len(result_set_comparison_request.ticket_ids),
                    )
                else:
                    policy_flags["ai_dispatcher_result_set_comparison_skipped"] = "no_saved_result_set"
            except Exception as exc:
                policy_flags["ai_dispatcher_result_set_comparison_error"] = str(exc)
                logger.warning("universal: result-set comparison failed trace=%s: %s", chat_trace_id, exc)
                if previous_result_set:
                    result_set_comparison_answer = (
                        "I found the saved previous result set, but the bounded ticket lookup failed, "
                        f"so I cannot honestly compare those IDs yet. Lookup error: {str(exc)[:300]}\n\n"
                        'SOURCES_JSON: {"sources": []}'
                    )
        else:
            policy_flags["ai_dispatcher_result_set_comparison_skipped"] = "tickets_bq_disabled"
    bulk_dispatcher_allowed = False
    if bulk_ticket_lookup_request.is_lookup and not result_set_comparison_request.is_comparison:
        bulk_dispatcher_trace = await _resolve_ai_dispatcher_trace()
        bulk_dispatcher_allowed = (
            bool(bulk_dispatcher_trace.get("answer_affecting"))
            and bulk_dispatcher_trace.get("assistive_task_class")
            == ai_dispatcher_runtime.ASSISTIVE_CLASS_BULK_TICKET_IDS
        )
        if not bulk_dispatcher_allowed:
            policy_flags["ai_dispatcher_bulk_ticket_lookup_skipped"] = "dispatcher_not_answer_affecting"
    if bulk_dispatcher_allowed:
        if tickets_bq_search_enabled:
            try:
                lookup_started = time.monotonic()
                bulk_ticket_lookup_result = await loop.run_in_executor(
                    None,
                    lambda: bigquery.bulk_lookup_ticket_ids(list(bulk_ticket_lookup_request.ticket_ids)),
                )
                tool_latency_ms["bq_bulk_ticket_lookup"] = int((time.monotonic() - lookup_started) * 1000)
                bulk_ticket_lookup_answer = ai_dispatcher_result_sets.build_bulk_lookup_answer(
                    bulk_ticket_lookup_result,
                    answer_language=semantic.answer_language,
                )
                bq_rows = bulk_ticket_lookup_result.get("rows") or []
                for row in bq_rows:
                    card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                    if card:
                        ticket_semantic_ticket_cards.append(card)
                decision["bq_sql"] = str(bulk_ticket_lookup_result.get("bq_summary") or "BOUNDED BULK TICKET LOOKUP")
                policy_flags["ai_dispatcher_bulk_ticket_lookup"] = ai_dispatcher_result_sets.summarize_bulk_lookup(
                    bulk_ticket_lookup_result
                )
                logger.info(
                    "universal: applied bulk ticket lookup trace=%s user_ids=%d found=%d",
                    chat_trace_id,
                    len(bulk_ticket_lookup_request.ticket_ids),
                    len(bulk_ticket_lookup_result.get("found_ids") or []),
                )
            except Exception as exc:
                policy_flags["ai_dispatcher_bulk_ticket_lookup_error"] = str(exc)
                logger.warning("universal: bulk ticket lookup failed trace=%s: %s", chat_trace_id, exc)
                bulk_ticket_lookup_answer = (
                    "I recognized the ticket IDs, but the bounded ticket lookup failed, "
                    f"so I cannot honestly summarize those tickets yet. Lookup error: {str(exc)[:300]}\n\n"
                    'SOURCES_JSON: {"sources": []}'
                )
        else:
            policy_flags["ai_dispatcher_bulk_ticket_lookup_skipped"] = "tickets_bq_disabled"
    challenge_dispatcher_allowed = False
    if (
        challenge_fact_check_request.is_fact_check
        and not result_set_comparison_request.is_comparison
        and not bulk_ticket_lookup_answer
    ):
        challenge_dispatcher_trace = await _resolve_ai_dispatcher_trace()
        challenge_dispatcher_allowed = (
            bool(challenge_dispatcher_trace.get("answer_affecting"))
            and challenge_dispatcher_trace.get("assistive_task_class")
            == ai_dispatcher_runtime.ASSISTIVE_CLASS_CHALLENGE_FACT_CHECK
        )
        if not challenge_dispatcher_allowed:
            policy_flags["ai_dispatcher_challenge_fact_check_skipped"] = "dispatcher_not_answer_affecting"
    if challenge_dispatcher_allowed:
        if tickets_bq_search_enabled:
            try:
                lookup_started = time.monotonic()
                challenge_fact_check_result = await loop.run_in_executor(
                    None,
                    lambda: bigquery.bulk_lookup_ticket_ids(list(challenge_fact_check_request.ticket_ids)),
                )
                tool_latency_ms["bq_challenge_fact_check_lookup"] = int((time.monotonic() - lookup_started) * 1000)
                challenge_fact_check_answer = ai_dispatcher_result_sets.build_fact_check_answer(
                    challenge_fact_check_result,
                    answer_language=semantic.answer_language,
                )
                bq_rows = challenge_fact_check_result.get("rows") or []
                for row in bq_rows:
                    card = _normalize_ticket_card(row) if isinstance(row, dict) else None
                    if card:
                        ticket_semantic_ticket_cards.append(card)
                decision["bq_sql"] = str(
                    challenge_fact_check_result.get("bq_summary") or "BOUNDED FACT-CHECK TICKET LOOKUP"
                )
                policy_flags["ai_dispatcher_challenge_fact_check"] = ai_dispatcher_result_sets.summarize_bulk_lookup(
                    challenge_fact_check_result
                )
                logger.info(
                    "universal: applied challenge fact-check lookup trace=%s user_ids=%d found=%d",
                    chat_trace_id,
                    len(challenge_fact_check_request.ticket_ids),
                    len(challenge_fact_check_result.get("found_ids") or []),
                )
            except Exception as exc:
                policy_flags["ai_dispatcher_challenge_fact_check_error"] = str(exc)
                logger.warning("universal: challenge fact-check lookup failed trace=%s: %s", chat_trace_id, exc)
                challenge_fact_check_answer = (
                    "I recognized this as a challenge to a prior claim, but the bounded ticket lookup failed, "
                    f"so I cannot honestly verify that claim yet. Lookup error: {str(exc)[:300]}\n\n"
                    'SOURCES_JSON: {"sources": []}'
                )
        else:
            policy_flags["ai_dispatcher_challenge_fact_check_skipped"] = "tickets_bq_disabled"
    schema_limitation_answer = _analytics_schema_limitation_answer(
        routing_question,
        schema_limitation,
        bq_rows,
    )
    sla_analytics_answer = sla_supported_fields_answer or _sla_analytics_answer(routing_question, bq_rows)
    drive_url_unavailable_answer = _maybe_build_drive_url_unavailable_answer(
        latest_question,
        drive_chunks,
    ) or _maybe_build_drive_url_unavailable_answer(routing_question, drive_chunks)
    count_list_filter_answer = _maybe_build_count_list_filter_answer(
        count_list_filter_sql,
        bq_rows,
    )
    deterministic_candidates = [
        ("schema_limitation", schema_limitation_answer),
        ("sla_analytics", sla_analytics_answer),
        ("drive_url_unavailable", drive_url_unavailable_answer),
        ("count_list_filter", count_list_filter_answer),
        ("pos_type_profile", pos_type_profile_answer),
        ("customer_risk", customer_risk_answer),
        ("global_event_discovery", global_event_discovery_answer),
        ("followup_date_scope", followup_date_scope_answer),
        ("ticket_resolution_lookup", ticket_resolution_lookup_answer),
        ("followup_result_set_refinement", followup_result_set_refinement_answer),
        ("followup_ticket_reference", followup_ticket_reference_answer),
        ("prior_result_comparison", result_set_comparison_answer),
        ("challenge_fact_check", challenge_fact_check_answer),
        ("bulk_ticket_lookup", bulk_ticket_lookup_answer),
        ("ticket_semantic_search", ticket_semantic_search_answer),
        ("semantic_label_count", semantic_label_count_answer),
        ("zendesk_live_recent", live_zendesk_recent_answer),
        ("zendesk_temporal_hybrid", zendesk_temporal_hybrid_answer),
        ("zendesk_temporal_fallback", zendesk_temporal_fallback_answer),
        (
            "temporal_count",
            None if semantic_issue_filter else _maybe_build_temporal_count_answer(temporal_resolution, bq_rows),
        ),
        (
            "temporal_top_stores",
            None if semantic_issue_filter else _maybe_build_temporal_top_stores_answer(temporal_resolution, bq_rows),
        ),
        (
            "temporal_top_issues",
            None if semantic_issue_filter else _maybe_build_temporal_top_issues_answer(temporal_resolution, bq_rows),
        ),
        ("top_issues", _maybe_build_deterministic_top_issues_answer(routing_question, bq_rows)),
    ]
    deterministic_answer_source, deterministic_answer = _select_deterministic_answer(
        deterministic_candidates
    )
    stage_timings.finish("retrieval")
    policy_flags["selected_deterministic_answer"] = deterministic_answer_source
    if deterministic_answer:
        logger.info("universal_trace deterministic answer trace=%s live_recent=%s", chat_trace_id, bool(live_zendesk_recent_answer))

    quality_gate_state: dict = {
        "visible_answer": "",
        "flags": {},
    }

    async def event_stream():
        full_text = ""
        stage_timings.start("answer_production")
        answer_now_control_state: dict = {
            "requested": False,
            "checkpoint": None,
            "status": "not_requested",
        }

        async def _poll_answer_now_control(checkpoint: str) -> bool:
            try:
                control = await loop.run_in_executor(
                    None,
                    lambda: firestore.get_universal_chat_turn_control(
                        turn_id=turn_id,
                        conversation_id=conversation_id,
                        owner_uid=owner_uid,
                        owner_email=owner_email,
                    ),
                )
            except Exception as exc:
                logger.warning("universal turn control poll failed trace=%s turn=%s: %s", chat_trace_id, turn_id, exc)
                return False
            if not bool((control or {}).get("answer_now_requested")):
                return False
            answer_now_control_state.update({
                "requested": True,
                "checkpoint": checkpoint,
                "status": "requested",
            })
            policy_flags["answer_now_requested"] = True
            policy_flags["answer_now_checkpoint"] = checkpoint
            return True

        try:
            yield _sse(_status_payload(
                STATUS_CHECKING_TICKET_CONTEXT,
                trace_id=chat_trace_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            ))
            emitted_statuses = {STATUS_CHECKING_TICKET_CONTEXT}
            status_steps: list[str] = []
            if kb_results or drive_chunks or any(tool in tools for tool in ("search_knowledge_base", "search_drive_exact", "kb_search", "drive_search")):
                status_steps.append(STATUS_SEARCHING_PROCEDURES)
            if (
                bq_rows
                or pos_type_profile_result
                or customer_risk_answer
                or semantic_label_count_answer
                or any(tool in tools for tool in ("run_bq_query", "bq", "analytics"))
            ):
                status_steps.append(STATUS_CHECKING_ANALYTICS)
            if not deterministic_answer and not any([
                kb_results,
                drive_chunks,
                similar_tickets,
                bq_rows,
                pos_type_profile_result,
                customer_risk_answer,
                global_event_discovery_answer,
                ticket_semantic_search_answer,
            ]):
                status_steps.append(STATUS_CHECKING_ANOTHER_SOURCE)
            status_steps.append(STATUS_PREPARING_ANSWER)
            for status in status_steps:
                if status in emitted_statuses:
                    continue
                emitted_statuses.add(status)
                yield _sse(_status_payload(
                    status,
                    trace_id=chat_trace_id,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                ))
                await asyncio.sleep(0)

            if deterministic_answer:
                candidate_answer = _normalize_visible_answer_markdown(deterministic_answer)
            else:
                def _sync_gen():
                    return list(universal_chat.chat_stream(
                        messages=messages,
                        ticket_context=ticket_context or None,
                        kb_results=kb_results,
                        kb_status=kb_status,
                        drive_chunks=drive_chunks,
                        drive_intent=decision.get("drive_intent", "search"),
                        similar_tickets=similar_tickets,
                        similar_query=decision.get("tickets_query", ""),
                        bq_rows=bq_rows,
                        bq_sql=decision.get("bq_sql", ""),
                        capture_callback=_capture_model_transition_request if request_capture_enabled else None,
                    ))
                raw_chunks = await loop.run_in_executor(None, _sync_gen)
                candidate_answer = _normalize_visible_answer_markdown(_strip_internal_tool_blocks("".join(raw_chunks)))
            quality_gate = _apply_universal_answer_quality_gate(
                candidate_answer,
                tools=tools,
                bq_rows=bq_rows if ("run_bq_query" in tools or bq_rows or decision.get("bq_sql")) else None,
                kb_results=kb_results if ("search_knowledge_base" in tools or kb_results) else None,
                drive_chunks=drive_chunks if ("search_drive_exact" in tools or drive_chunks) else None,
                similar_tickets=similar_tickets if ("search_similar_tickets" in tools or similar_tickets) else None,
                live_zendesk_recent_tickets=(
                    live_zendesk_recent_tickets
                    if (live_window or live_zendesk_recent_tickets or "zendesk_live_recent_overlay" in policy_flags)
                    else None
                ),
                zendesk_temporal_tail_tickets=(
                    zendesk_temporal_tail_tickets
                    if (zendesk_temporal_tail_tickets or "zendesk_temporal_tail" in policy_flags)
                    else None
                ),
                zendesk_temporal_fallback_tickets=(
                    zendesk_temporal_fallback_tickets
                    if (zendesk_temporal_fallback_tickets or "zendesk_temporal_fallback" in policy_flags)
                    else None
                ),
                stale_or_incomplete_data=bool(
                    live_window
                    or policy_flags.get("zendesk_temporal_tail")
                    or policy_flags.get("zendesk_temporal_fallback")
                    or policy_flags.get("zendesk_live_recent_overlay_error")
                    or policy_flags.get("zendesk_temporal_tail_error")
                    or policy_flags.get("zendesk_temporal_fallback_error")
                ),
            )
            full_text = candidate_answer
            quality_gate_state["visible_answer"] = quality_gate.visible_answer
            quality_gate_state["flags"] = quality_gate.flags
            stage_timings.finish("answer_production")
            stage_timings.start("validator_repair")
            available_fields: list[str] = []
            if isinstance(bq_rows, list):
                seen_fields: set[str] = set()
                for row in bq_rows[:10]:
                    if not isinstance(row, dict):
                        continue
                    for field in row.keys():
                        key = str(field)
                        if key not in seen_fields:
                            seen_fields.add(key)
                            available_fields.append(key)

            answer_now_before_validator = await _poll_answer_now_control("before_optional_validator")
            if answer_now_before_validator:
                answer_now_result = universal_answer_now.answer_now_candidate_policy(
                    quality_gate_state["visible_answer"],
                    answer_now_requested=True,
                    changed_finalization_policy=True,
                )
                if not answer_now_result.flags.get("wait_for_safe_candidate"):
                    quality_gate_state["visible_answer"] = answer_now_result.visible_answer
                    quality_gate_state["flags"] = {
                        **quality_gate_state["flags"],
                        "answer_now": {
                            **answer_now_result.flags,
                            "checkpoint": "before_optional_validator",
                            "final_policy": "partial",
                        },
                    }

            validation_context = chat_answer_validator.build_answer_validation_context(
                candidate_answer=quality_gate.visible_answer,
                latest_question=latest_question,
                routing_question=routing_question,
                semantic_frame=semantic_frame,
                tools=tools,
                phase1_decision=decision,
                policy_flags=policy_flags,
                quality_gate_flags=quality_gate.flags,
                bq_sql=decision.get("bq_sql", ""),
                bq_rows=bq_rows if ("run_bq_query" in tools or bq_rows or decision.get("bq_sql")) else None,
                kb_results=kb_results if ("search_knowledge_base" in tools or kb_results) else None,
                drive_chunks=drive_chunks if ("search_drive_exact" in tools or drive_chunks) else None,
                similar_tickets=similar_tickets if ("search_similar_tickets" in tools or similar_tickets) else None,
                live_zendesk_recent_tickets=live_zendesk_recent_tickets,
                zendesk_temporal_tail_tickets=zendesk_temporal_tail_tickets,
                zendesk_temporal_fallback_tickets=zendesk_temporal_fallback_tickets,
                direct_ticket_context=direct_ticket_context_evidence_count,
                conversation_entities=conversation_entities,
                available_fields=available_fields,
                risk_hints=(
                    ["stale_data"] if quality_gate.flags.get("stale_or_incomplete_caveat_added") else []
                ),
            )
            if answer_now_before_validator:
                risk_detection = ChatAnswerRiskDetectionResult(
                    should_validate=False,
                    risk_classes=[],
                    risk_reasons={"answer_now": "Answer Now requested before optional validation."},
                    source_families=[],
                )
            else:
                risk_detection = chat_answer_validator.detect_answer_risks(validation_context)
            validation_state: dict = {
                "risk_detection": _model_to_plain_dict(risk_detection),
                "validator": {
                    "ran": False,
                    "enabled": chat_answer_validator.automatic_validator_enabled(),
                },
            }
            if answer_now_before_validator:
                validation_state["validator"]["skipped"] = "answer_now"
            if risk_detection.should_validate:
                validation_result = None
                validation_telemetry: dict = {}
                try:
                    if chat_answer_validator.automatic_validator_enabled() and STATUS_CHECKING_ANOTHER_SOURCE not in emitted_statuses:
                        emitted_statuses.add(STATUS_CHECKING_ANOTHER_SOURCE)
                        yield _sse(_status_payload(
                            STATUS_CHECKING_ANOTHER_SOURCE,
                            trace_id=chat_trace_id,
                            turn_id=turn_id,
                            conversation_id=conversation_id,
                        ))
                    validation_result, validation_telemetry = await loop.run_in_executor(
                        None,
                        lambda: chat_answer_validator.validate_answer_candidate_with_telemetry(
                            validation_context,
                        ),
                    )
                except Exception as exc:
                    logger.warning("answer validator failed trace=%s: %s", chat_trace_id, exc)
                    validation_result = chat_answer_validator.validator_error_result(str(exc))
                    validation_telemetry = {
                        "validator_enabled": True,
                        "status": "error",
                        "error": str(exc),
                    }

                updated_answer, apply_flags = chat_answer_validator.apply_validation_result_to_answer(
                    quality_gate_state["visible_answer"],
                    validation_result,
                )
                quality_gate_state["visible_answer"] = updated_answer
                validation_state["validator"] = {
                    "ran": bool(validation_result.validator_enabled),
                    "enabled": validation_result.validator_enabled,
                    "result": _model_to_plain_dict(validation_result),
                    "apply_flags": apply_flags,
                    "telemetry": validation_telemetry,
                }
                repair_state: dict = {
                    "attempted": False,
                    "applied": False,
                    "max_passes": 1,
                }
                if chat_answer_validator.should_attempt_bounded_repair(validation_result):
                    pos_type_statuses = (
                        list(pos_type_profile_request.status_filter)
                        if pos_type_profile_request and pos_type_profile_request.status_filter
                        else None
                    )
                    repair_scope = chat_repair_loop.build_scope_fingerprint(
                        latest_question=latest_question,
                        target_ticket_id=target_ticket_id or req.ticket_id,
                        store_id=(
                            _extract_store_id(latest_question)
                            or _extract_store_id(routing_question)
                            or _extract_store_id_from_ticket_context(ticket_context or "")
                        ),
                        pos_type=(
                            pos_type_profile_request.pos_type
                            if pos_type_profile_request and pos_type_profile_request.pos_type
                            else None
                        ),
                        status_filter=pos_type_statuses,
                        date_start=(
                            pos_type_profile_request.start_date
                            if pos_type_profile_request and pos_type_profile_request.start_date
                            else None
                        ),
                        date_end=(
                            pos_type_profile_request.end_date
                            if pos_type_profile_request and pos_type_profile_request.end_date
                            else None
                        ),
                        source_family=",".join(risk_detection.source_families),
                    )
                    loop_state = chat_repair_loop.default_loop_state(scope_fingerprint=repair_scope)

                    async def _automatic_repair_refresh_trace(repair_seed: ChatQualityReviewResult) -> dict:
                        repair_trace = {
                            "trace_id": chat_trace_id,
                            "messages": messages[-8:],
                            "ticket_id": req.ticket_id,
                            "target_ticket_id": target_ticket_id,
                            "ticket_context_reason": ticket_context_reason,
                            "phase1_decision": decision,
                            "policy_flags": policy_flags,
                            "kb_results": _compact_kb_results(kb_results),
                            "kb_status": kb_status,
                            "drive_chunks": _compact_drive_chunks(drive_chunks),
                            "similar_tickets": _compact_dict_list(similar_tickets),
                            "bq_sql": decision.get("bq_sql", ""),
                            "bq_rows": _compact_bq_rows(bq_rows),
                            "live_zendesk_recent_tickets": _compact_dict_list(live_zendesk_recent_tickets, limit=20),
                            "zendesk_temporal_tail_tickets": _compact_dict_list(zendesk_temporal_tail_tickets, limit=20),
                            "zendesk_temporal_fallback_tickets": _compact_dict_list(zendesk_temporal_fallback_tickets, limit=20),
                            "customer_risk_ticket_cards": _compact_dict_list(customer_risk_ticket_cards, limit=20),
                            "ticket_semantic_ticket_cards": _compact_dict_list(ticket_semantic_ticket_cards, limit=20),
                            "pos_type_profile_result": pos_type_profile_result,
                            "pos_type_profile_ticket_cards": _compact_dict_list(pos_type_profile_ticket_cards, limit=20),
                            "quality_gate_flags": quality_gate_state.get("flags") or {},
                            "answer_validation": validation_state,
                            "final_answer": quality_gate.visible_answer,
                            "automatic_repair_instruction": (
                                "This is a private automatic bounded repair. The final_answer is the "
                                "candidate being checked, not evidence. Use only retrieved evidence and "
                                "the refresh block; do not treat prior assistant prose as factual support."
                            ),
                            "sources": _compact_dict_list(sources, limit=12) if "sources" in locals() else [],
                            "tickets": [],
                            "counts": {
                                "kb": len(kb_results or []),
                                "drive": len(drive_chunks or []),
                                "similar": len(similar_tickets or []),
                                "bq": len(bq_rows) if isinstance(bq_rows, list) else (1 if isinstance(bq_rows, str) else 0),
                                "zendesk_live_recent": len(live_zendesk_recent_tickets or []),
                                "zendesk_temporal_tail": len(zendesk_temporal_tail_tickets or []),
                                "zendesk_temporal_fallback": len(zendesk_temporal_fallback_tickets or []),
                                "direct_ticket_context": direct_ticket_context_evidence_count,
                                "customer_risk": len(customer_risk_ticket_cards or []),
                                "ticket_semantic_search": len(ticket_semantic_ticket_cards or []),
                                "pos_type_profile": len(pos_type_profile_ticket_cards or []),
                            },
                        }
                        return await _build_review_refresh_trace(repair_trace, repair_seed, loop)

                    async def _automatic_repair_review_runner(refresh_trace: dict):
                        return await loop.run_in_executor(
                            None,
                            lambda: chat_quality.review_answer_trace_with_telemetry(
                                refresh_trace,
                                (
                                    "Automatic bounded repair after answer-validator risk. "
                                    "Use refreshed evidence only; do not treat the prior assistant answer as evidence."
                                ),
                            )
                        )

                    def _automatic_repair_answer_gate(improved_answer: str) -> tuple[str, dict]:
                        repair_gate = _apply_universal_answer_quality_gate(
                            _normalize_visible_answer_markdown(improved_answer),
                            tools=tools,
                            bq_rows=bq_rows if ("run_bq_query" in tools or bq_rows or decision.get("bq_sql")) else None,
                            kb_results=kb_results if ("search_knowledge_base" in tools or kb_results) else None,
                            drive_chunks=drive_chunks if ("search_drive_exact" in tools or drive_chunks) else None,
                            similar_tickets=similar_tickets if ("search_similar_tickets" in tools or similar_tickets) else None,
                            live_zendesk_recent_tickets=live_zendesk_recent_tickets,
                            zendesk_temporal_tail_tickets=zendesk_temporal_tail_tickets,
                            zendesk_temporal_fallback_tickets=zendesk_temporal_fallback_tickets,
                            stale_or_incomplete_data=bool(
                                live_window
                                or policy_flags.get("zendesk_temporal_tail")
                                or policy_flags.get("zendesk_temporal_fallback")
                                or policy_flags.get("zendesk_live_recent_overlay_error")
                                or policy_flags.get("zendesk_temporal_tail_error")
                                or policy_flags.get("zendesk_temporal_fallback_error")
                            ),
                        )
                        return repair_gate.visible_answer, repair_gate.flags

                    async def _automatic_repair_validator_runner(repaired_answer: str):
                        revalidation_context = {
                            **validation_context,
                            "candidate_answer": repaired_answer,
                            "repair_loop": {
                                "revalidation": True,
                                "previous_risk_classes": list(validation_result.risk_classes),
                                "previous_recommended_action": validation_result.recommended_action,
                            },
                        }
                        return await loop.run_in_executor(
                            None,
                            lambda: chat_answer_validator.validate_answer_candidate_with_telemetry(
                                revalidation_context,
                            ),
                        )

                    def _automatic_repair_apply_validation(
                        answer: str,
                        result,
                    ) -> tuple[str, dict]:
                        return chat_answer_validator.apply_validation_result_to_answer(answer, result)

                    def _automatic_repair_caveat(answer: str, stop_reason: str) -> tuple[str, dict]:
                        caveat_result = chat_answer_validator.validator_error_result(
                            f"repair_loop_{stop_reason}",
                        )
                        caveated, caveat_flags = chat_answer_validator.apply_validation_result_to_answer(
                            answer,
                            caveat_result,
                        )
                        return caveated, {
                            **caveat_flags,
                            "repair_loop_stop_reason": stop_reason,
                        }

                    loop_state, repair_state, repaired_answer, repair_apply_flags = await chat_repair_loop.run_revalidation_repair_loop(
                        loop_state,
                        initial_validation_result=validation_result,
                        initial_answer=updated_answer,
                        latest_question=latest_question,
                        repair_seed_builder=lambda result, question: chat_answer_validator.bounded_repair_review_seed(
                            result,
                            latest_question=question,
                        ),
                        refresh_trace_builder=_automatic_repair_refresh_trace,
                        review_runner=_automatic_repair_review_runner,
                        answer_gate=_automatic_repair_answer_gate,
                        validator_runner=_automatic_repair_validator_runner,
                        validation_result_applier=_automatic_repair_apply_validation,
                        caveat_builder=_automatic_repair_caveat,
                        candidate_scope_builder=lambda: repair_scope,
                        answer_now_checker=_poll_answer_now_control,
                    )
                    if repaired_answer is not None:
                        quality_gate_state["visible_answer"] = repaired_answer
                        validation_state["validator"]["apply_flags"] = {
                            **apply_flags,
                            **(repair_apply_flags or {}),
                            "validator_applied": (repair_apply_flags or {}).get(
                                "validator_applied",
                                "bounded_repair_loop",
                            ),
                            "validator_changed_answer": (
                                repaired_answer.strip()
                                != (quality_gate.visible_answer or "").strip()
                            ),
                        }
                    if (repair_state.get("answer_now") or {}).get("requested"):
                        validation_state["validator"]["answer_now"] = repair_state.get("answer_now")
                        quality_gate_state["flags"] = {
                            **quality_gate_state["flags"],
                            "answer_now": repair_state.get("answer_now"),
                        }

                if not repair_state.get("applied"):
                    quality_gate_state["visible_answer"] = updated_answer
                validation_state["validator"]["repair"] = repair_state
                await _save_review_event_best_effort(loop, {
                    "mode": "auto_validator",
                    "status": "success",
                    "requester_uid": owner_uid,
                    "requester_email": owner_email,
                    "trace_id": chat_trace_id,
                    "turn_id": turn_id,
                    "conversation_id": conversation_id,
                    "ticket_id": req.ticket_id,
                    "target_ticket_id": target_ticket_id,
                    "quality": validation_result.quality,
                    "confidence": validation_result.confidence,
                    "problem_types": list(validation_result.risk_classes),
                    "recommended_action": validation_result.recommended_action,
                    "validator_answer_policy": validation_result.answer_policy,
                    "has_improved_answer": bool(
                        validation_result.rewrite_from_supplied_evidence
                        or repair_state.get("applied")
                    ),
                    "repair_attempted": bool(repair_state.get("attempted")),
                    "repair_applied": bool(repair_state.get("applied")),
                    "repair_loop_stop_reason": (repair_state.get("loop") or {}).get("stop_reason"),
                    "repair_loop_validation_count": (repair_state.get("loop") or {}).get("validation_count"),
                    "repair_loop_repair_count": (repair_state.get("loop") or {}).get("repair_count"),
                    "repair_loop_revalidation_count": len(repair_state.get("revalidations") or []),
                    "answer_now_requested": bool((repair_state.get("answer_now") or {}).get("requested")),
                    "answer_now_checkpoint": (repair_state.get("answer_now") or {}).get("checkpoint"),
                    "answer_policy": _compact_answer_validation_for_log(validation_state),
                    "telemetry": {
                        "validator": {
                            "estimated_input_tokens": validation_telemetry.get("estimated_input_tokens"),
                            "estimated_output_tokens": validation_telemetry.get("estimated_output_tokens"),
                            "actual_input_tokens": validation_telemetry.get("actual_input_tokens"),
                            "actual_output_tokens": validation_telemetry.get("actual_output_tokens"),
                            "actual_thinking_tokens": validation_telemetry.get("actual_thinking_tokens"),
                            "actual_cost_cad": validation_telemetry.get("actual_cost_cad"),
                            "thinking_budget": validation_telemetry.get("thinking_budget"),
                            "max_output_tokens": validation_telemetry.get("max_output_tokens"),
                        },
                        "repair": {
                            "attempted": bool(repair_state.get("attempted")),
                            "applied": bool(repair_state.get("applied")),
                            "requested_action": repair_state.get("requested_action"),
                            "loop": repair_state.get("loop") or {},
                            "revalidation_count": len(repair_state.get("revalidations") or []),
                            "refresh": repair_state.get("refresh") or {},
                            "review": {
                                key: value
                                for key, value in (repair_state.get("review") or {}).items()
                                if key != "telemetry"
                            },
                        },
                    },
                })
                quality_gate_state["flags"] = {
                    **quality_gate_state["flags"],
                    "answer_validation": validation_state,
                }
            else:
                quality_gate_state["flags"] = {
                    **quality_gate_state["flags"],
                    "answer_validation": validation_state,
                }

            stage_timings.finish("validator_repair")
            if quality_gate_state["visible_answer"]:
                yield _sse({"delta": quality_gate_state["visible_answer"]})
            await asyncio.sleep(0)

        except asyncio.CancelledError:
            await _mark_turn_control_completed_best_effort()
            return
        except Exception as exc:
            logger.error("universal stream error: %s", exc, exc_info=True)
            yield _sse({"error": str(exc)})
            await _mark_turn_control_completed_best_effort()
            return

        # Parse footers
        tickets_json = _extract_json_footer(full_text, "TICKETS_JSON:")
        sources_json = _extract_json_footer(full_text, "SOURCES_JSON:")

        sources = sources_json.get("sources", []) if isinstance(sources_json, dict) else []
        tickets: list[dict] = []
        seen_ticket_ids: set[int] = set()

        def add_ticket(card: dict | None) -> None:
            if not card:
                return
            tid = card.get("id")
            if not isinstance(tid, int):
                return
            if tid in seen_ticket_ids:
                return
            seen_ticket_ids.add(tid)
            tickets.append(card)

        # 1) Model footer (best case)
        if isinstance(tickets_json, list):
            for item in tickets_json:
                if isinstance(item, dict):
                    add_ticket(_normalize_ticket_card(item))

        # 2) Deterministic tool-output injection (don't rely on model footer)
        if similar_tickets:
            for item in similar_tickets:
                if isinstance(item, dict):
                    add_ticket(_normalize_ticket_card(item))

        if semantic_label_ticket_cards:
            for item in semantic_label_ticket_cards:
                add_ticket(item)

        if pos_type_profile_ticket_cards:
            for item in pos_type_profile_ticket_cards:
                add_ticket(item)

        if customer_risk_ticket_cards:
            for item in customer_risk_ticket_cards:
                add_ticket(item)

        if global_event_ticket_cards:
            for item in global_event_ticket_cards:
                add_ticket(item)

        if ticket_semantic_ticket_cards:
            for item in ticket_semantic_ticket_cards:
                add_ticket(item)

        if ticket_resolution_ticket_cards:
            for item in ticket_resolution_ticket_cards:
                add_ticket(item)

        # 3) Live Zendesk overlay rows are real ticket hits and should be
        # openable from the UI, even when the deterministic aggregate answer
        # does not mention each ticket id in text.
        show_zendesk_cards = _should_show_zendesk_aggregate_cards(routing_question, temporal_resolution)

        if show_zendesk_cards and live_zendesk_recent_tickets:
            for item in _live_zendesk_recent_ticket_cards(live_zendesk_recent_tickets):
                add_ticket(item)

        if show_zendesk_cards and zendesk_temporal_tail_tickets:
            for item in _live_zendesk_recent_ticket_cards(zendesk_temporal_tail_tickets):
                add_ticket(item)

        if show_zendesk_cards and zendesk_temporal_fallback_tickets:
            for item in _live_zendesk_recent_ticket_cards(zendesk_temporal_fallback_tickets):
                add_ticket(item)

        # 4) Text mention fallback — always allow opening mentioned tickets.
        for tid in _extract_ticket_ids_for_cards(f"{latest_question}\n{full_text}"):
            add_ticket(_minimal_ticket_card(tid))

        # Inject Drive sources deterministically (don't rely on Gemini to copy URLs)
        if drive_chunks:
            seen_files: set[str] = {s.get("title", "") for s in sources}
            for chunk in drive_chunks:
                fname = chunk.get("file_name", "")
                url = chunk.get("drive_url", "")
                if fname and fname not in seen_files and url:
                    sources.append({"title": fname, "url": url, "snippet": chunk.get("chunk_text", "")[:120]})
                    seen_files.add(fname)

        # Inject KB sources deterministically
        if kb_results:
            seen_kb: set[str] = {s.get("url", "") for s in sources}
            for article in kb_results:
                url = getattr(article, "url", "") or ""
                if url and url not in seen_kb:
                    sources.append({"title": getattr(article, "title", ""), "url": url, "snippet": getattr(article, "snippet", "")[:120]})
                    seen_kb.add(url)

        # Final source de-duplication (model output + deterministic injections).
        # Preserve order to keep the most relevant source first.
        deduped_sources: list[dict] = []
        seen_source_keys: set[tuple[str, str]] = set()
        for src in sources:
            if not isinstance(src, dict):
                continue
            normalized = _normalize_source_for_display(src, drive_chunks)
            if not normalized:
                continue
            url = str(normalized.get("url", "") or "").strip()
            title = str(normalized.get("title", "") or "").strip().lower()
            key = (url, "") if url else ("", title)
            if not url and not title:
                continue
            if key in seen_source_keys:
                continue
            seen_source_keys.add(key)
            deduped_sources.append(normalized)
        sources = deduped_sources
        visible_answer = quality_gate_state.get("visible_answer") or _strip_trace_footers(full_text)

        conversation_entities_saved: dict | None = None
        try:
            saved_period = (
                semantic_label_contract.period
                if semantic_label_contract
                else semantic_task_contracts.period_contract_from_temporal(
                    _resolve_label_count_period(
                        latest_question=latest_question,
                        routing_question=routing_question,
                        messages=routing_messages,
                    )
                )
            )
            saved_store_id = (
                semantic_label_store_id
                or explicit_store_context
                or (open_ticket_store_id if req.ticket_id else None)
                or _extract_store_id_from_ticket_context(ticket_context)
            )
            conversation_entities_saved = semantic_task_contracts.build_conversation_entities(
                final_answer=visible_answer,
                task_contract=task_contract,
                semantic_frame=semantic_frame,
                store_id=saved_store_id,
                period=saved_period,
                trace_id=chat_trace_id,
                turn_id=turn_id,
            )
            if conversation_entities_saved:
                await loop.run_in_executor(
                    None,
                    lambda: firestore.save_universal_conversation_entities(
                        conversation_id,
                        conversation_entities_saved,
                    ),
                )
        except Exception as exc:
            logger.warning("universal: conversation entity save failed trace=%s: %s", chat_trace_id, exc)
        policy_flags["conversation_entities_saved"] = bool(conversation_entities_saved)

        logger.info(
            "universal_trace emit trace=%s ticket_cards=%d sources=%d text_chars=%d",
            chat_trace_id,
            len(tickets),
            len(sources),
            len(full_text),
        )

        ai_dispatcher_trace = await _resolve_ai_dispatcher_trace()
        ai_dispatcher_trace = _suppress_ai_dispatcher_trace_without_deterministic_answer(
            ai_dispatcher_trace,
            result_set_comparison_answer=(
                result_set_comparison_answer
                if deterministic_answer_source == "prior_result_comparison"
                else None
            ),
            bulk_ticket_lookup_answer=(
                bulk_ticket_lookup_answer
                if deterministic_answer_source == "bulk_ticket_lookup"
                else None
            ),
            challenge_fact_check_answer=(
                challenge_fact_check_answer
                if deterministic_answer_source == "challenge_fact_check"
                else None
            ),
            source_family_dispatcher_applied=(
                source_family_dispatcher_applied
                and deterministic_answer_source == "ticket_semantic_search"
            ),
            ticket_semantic_search_answer=(
                ticket_semantic_search_answer
                if deterministic_answer_source == "ticket_semantic_search"
                else None
            ),
            followup_date_scope_answer=(
                followup_date_scope_answer
                if deterministic_answer_source == "followup_date_scope"
                else None
            ),
            policy_flags=policy_flags,
        )

        compact_sources = _compact_dict_list(sources, limit=12)
        compact_tickets = _compact_dict_list(tickets, limit=12)
        trace_counts = {
            "kb": len(kb_results or []),
            "drive": len(drive_chunks or []),
            "similar": len(similar_tickets or []),
            "bq": len(bq_rows) if isinstance(bq_rows, list) else (1 if isinstance(bq_rows, str) else 0),
            "zendesk_live_recent": len(live_zendesk_recent_tickets or []),
            "zendesk_temporal_tail": len(zendesk_temporal_tail_tickets or []),
            "zendesk_temporal_fallback": len(zendesk_temporal_fallback_tickets or []),
            "direct_ticket_context": direct_ticket_context_evidence_count,
            "customer_risk": len(customer_risk_ticket_cards or []),
            "ticket_semantic_search": len(ticket_semantic_ticket_cards or []),
            "pos_type_profile": len(pos_type_profile_ticket_cards or []),
            "sources": len(sources),
            "tickets": len(tickets),
            "answer_chars": len(full_text),
            "tool_latency_ms": tool_latency_ms,
        }
        output_critic_trace_context = _build_output_critic_shadow_context(
            routing_question=routing_question,
            semantic_frame=semantic_frame,
            phase1_decision=decision,
            policy_flags=policy_flags,
            quality_gate_flags=quality_gate_state.get("flags") or {},
            answer_validation=(quality_gate_state.get("flags") or {}).get("answer_validation") or {},
            counts=trace_counts,
            sources=compact_sources,
            tickets=compact_tickets,
            ai_dispatcher_trace=ai_dispatcher_trace,
            followup_shadow_payload=followup_shadow_payload,
            followup_date_scope_payload=followup_date_scope_payload,
            ticket_resolution_lookup_payload=ticket_resolution_lookup_payload,
            conversation_entities_loaded=bool(conversation_entities),
            conversation_entities_saved=conversation_entities_saved,
        )
        stage_timings.start("output_critic")
        output_critic_trace = await _resolve_output_critic_shadow_payload(
            loop=loop,
            latest_question=latest_question,
            visible_answer=visible_answer,
            trace_context=output_critic_trace_context,
            timeout_ms=getattr(settings, "universal_output_critic_timeout_ms", 2500),
        )
        visible_answer, output_critic_repair_delta, output_critic_repair_trace = _resolve_output_critic_bounded_repair_payload(
            latest_question=latest_question,
            visible_answer=visible_answer,
            output_critic_trace=output_critic_trace,
            trace_context=output_critic_trace_context,
        )
        stage_timings.finish("output_critic")
        if output_critic_repair_delta:
            yield _sse({"delta": output_critic_repair_delta})
            quality_gate_state["visible_answer"] = visible_answer
            policy_flags["output_critic_bounded_repair"] = {
                "answer_affecting": True,
                "repair_kind": output_critic_repair_trace.get("repair_kind"),
                "reason_codes": output_critic_repair_trace.get("reason_codes") or [],
            }
        trace_counts["answer_chars"] = len(visible_answer)
        trace_counts["stage_timings"] = stage_timings.snapshot()
        if request_capture_enabled:
            policy_flags["model_transition_request_capture"] = {
                "enabled": True,
                "captures": [
                    model_transition_request_capture.compact_receipt(envelope)
                    for envelope in request_captures
                ],
            }

        trace_payload = {
            "owner_uid": owner_uid,
            "owner_email": owner_email,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "messages": messages[-8:],
            "ticket_id": req.ticket_id,
            "target_ticket_id": target_ticket_id,
            "ticket_context_reason": ticket_context_reason,
            "semantic_frame": semantic_frame,
            "followup_understanding_shadow": followup_shadow_payload,
            "routing_question": routing_question,
            "phase1_decision": decision,
            "tools_before_policy": tools_before_policy,
            "ai_dispatcher": ai_dispatcher_trace,
            "output_critic_shadow": output_critic_trace,
            "output_critic_bounded_repair": output_critic_repair_trace,
            "ai_dispatcher_result_set_memory_saved": result_set_memory_saved,
            "ai_dispatcher_result_set_comparison": ai_dispatcher_result_sets.summarize_comparison(result_set_comparison or {}) if result_set_comparison else None,
            "ai_dispatcher_challenge_fact_check": ai_dispatcher_result_sets.summarize_bulk_lookup(challenge_fact_check_result or {}) if challenge_fact_check_result else None,
            "ai_dispatcher_ticket_resolution_lookup": (
                ticket_resolution_lookup_assist.summarize_ticket_resolution_lookup(ticket_resolution_lookup_result or {})
                if ticket_resolution_lookup_result
                else None
            ),
            "policy_flags": policy_flags,
            "kb_results": _compact_kb_results(kb_results),
            "kb_status": kb_status,
            "drive_chunks": _compact_drive_chunks(drive_chunks),
            "similar_tickets": _compact_dict_list(similar_tickets),
            "bq_sql": decision.get("bq_sql", ""),
            "bq_rows": _compact_bq_rows(bq_rows),
            "live_zendesk_recent_tickets": _compact_dict_list(live_zendesk_recent_tickets, limit=20),
            "zendesk_temporal_tail_tickets": _compact_dict_list(zendesk_temporal_tail_tickets, limit=20),
            "zendesk_temporal_fallback_tickets": _compact_dict_list(zendesk_temporal_fallback_tickets, limit=20),
            "customer_risk_ticket_cards": _compact_dict_list(customer_risk_ticket_cards, limit=20),
            "ticket_semantic_ticket_cards": _compact_dict_list(ticket_semantic_ticket_cards, limit=20),
            "ticket_resolution_ticket_cards": _compact_dict_list(ticket_resolution_ticket_cards, limit=20),
            "pos_type_profile_result": pos_type_profile_result,
            "pos_type_profile_ticket_cards": _compact_dict_list(pos_type_profile_ticket_cards, limit=20),
            "quality_gate_flags": quality_gate_state.get("flags") or {},
            "answer_validation": (quality_gate_state.get("flags") or {}).get("answer_validation") or {},
            "final_answer": visible_answer,
            "conversation_entities_loaded": conversation_entities,
            "conversation_entities_saved": conversation_entities_saved,
            "sources": compact_sources,
            "tickets": compact_tickets,
            "counts": trace_counts,
        }
        stage_timings.start("persistence")
        try:
            await loop.run_in_executor(
                None,
                lambda: firestore.save_chat_answer_trace(chat_trace_id, trace_payload),
            )
        except Exception as exc:
            logger.warning("universal_trace save failed trace=%s: %s", chat_trace_id, exc)
        stage_timings.finish("persistence")
        trace_counts["stage_timings"] = stage_timings.snapshot()

        if not durable_logging_disabled:
            log_payload = {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "trace_id": chat_trace_id,
                "owner_uid": owner_uid,
                "owner_email": owner_email,
                "owner_role": owner_role,
                "ticket_id": req.ticket_id,
                "target_ticket_id": target_ticket_id,
                "latest_question": latest_question,
                "assistant_answer": visible_answer,
                "messages": messages[-12:],
                "phase1_decision": decision,
                "tools_before_policy": tools_before_policy,
                "ai_dispatcher": ai_dispatcher_trace,
                "output_critic_shadow": output_critic_trace,
                "output_critic_bounded_repair": output_critic_repair_trace,
                "ai_dispatcher_result_set_memory_saved": result_set_memory_saved,
                "ai_dispatcher_result_set_comparison": ai_dispatcher_result_sets.summarize_comparison(result_set_comparison or {}) if result_set_comparison else None,
                "ai_dispatcher_challenge_fact_check": ai_dispatcher_result_sets.summarize_bulk_lookup(challenge_fact_check_result or {}) if challenge_fact_check_result else None,
                "policy_flags": policy_flags,
                # Raw request envelopes are deliberately GCS-only. chat_logging
                # omits unknown payload fields from its BQ row.
                "model_transition_request_captures": request_captures,
                "answer_policy": _compact_answer_policy_for_log(quality_gate_state.get("flags") or {}),
                "evidence_snapshot": {
                    "semantic_frame": semantic_frame,
                    "followup_understanding_shadow": followup_shadow_payload,
                    "routing_question": routing_question,
                    "kb_results": _compact_kb_results(kb_results),
                    "kb_status": kb_status,
                    "drive_chunks": _compact_drive_chunks(drive_chunks),
                    "similar_tickets": _compact_dict_list(similar_tickets),
                    "bq_sql": decision.get("bq_sql", ""),
                    "bq_rows": _compact_bq_rows(bq_rows),
                    "live_zendesk_recent_tickets": _compact_dict_list(live_zendesk_recent_tickets, limit=20),
                    "customer_risk_ticket_cards": _compact_dict_list(customer_risk_ticket_cards, limit=20),
                    "ticket_semantic_ticket_cards": _compact_dict_list(ticket_semantic_ticket_cards, limit=20),
                },
                "sources": compact_sources,
                "tickets": compact_tickets,
                "counts": trace_payload["counts"],
                "latency_ms": int((time.monotonic() - started_at) * 1000),
            }
            try:
                await loop.run_in_executor(
                    None,
                    lambda: chat_logging.save_universal_chat_turn(log_payload),
                )
            except Exception as exc:
                logger.warning("universal_chat_log save failed trace=%s: %s", chat_trace_id, exc)

        yield _sse({"done": True, "sources": sources, "tickets": tickets, "trace_id": chat_trace_id, "conversation_id": conversation_id, "turn_id": turn_id})
        await _mark_turn_control_completed_best_effort()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
