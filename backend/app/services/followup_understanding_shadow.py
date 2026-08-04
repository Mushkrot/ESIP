"""Shadow-only understanding contract for contextual follow-up turns.

This module is intentionally pure: it reads already-available chat context and
returns private metadata. It must not affect routing, tools, or user-visible
answers until a later assistive phase explicitly promotes one narrow behavior.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from app.services import temporal_resolver


_CONTEXT_TARGETS = {"prior_turn", "conversation_memory", "previous_answer"}
_FOLLOWUP_MARKERS = (
    " just ",
    " only ",
    " one ",
    " ones ",
    " this ",
    " that ",
    " those ",
    " them ",
    " same ",
    " previous ",
    " prior ",
    " listed ",
    " mentioned ",
    " what about ",
    " instead ",
    " for these ",
    " for those ",
    " i want the ",
    " i need the ",
    " show me the ",
)


@dataclass(frozen=True)
class MonthWindow:
    display_label: str
    start_date: str
    end_date_exclusive: str
    timezone: str = "America/New_York"
    source: str = "latest_question"
    current_month_to_date: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriorTopicSignals:
    issue_or_category: str | None = None
    semantic_labels: tuple[str, ...] = ()
    store_id: str | None = None
    period: dict[str, Any] | None = None
    prior_trace_id: str | None = None
    prior_turn_id: str | None = None
    source: str = "none"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["semantic_labels"] = list(self.semantic_labels)
        return {key: value for key, value in payload.items() if value not in (None, "", [], ())}


@dataclass(frozen=True)
class FollowupUnderstandingShadow:
    emitted: bool
    answer_affecting: bool
    contract_type: str
    target_context: str
    confidence: str
    reason_codes: tuple[str, ...]
    month_windows: tuple[MonthWindow, ...]
    prior_topic: PriorTopicSignals
    needs_assistive_promotion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "emitted": self.emitted,
            "answer_affecting": self.answer_affecting,
            "contract_type": self.contract_type,
            "target_context": self.target_context,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "month_windows": [window.to_dict() for window in self.month_windows],
            "prior_topic": self.prior_topic.to_dict(),
            "needs_assistive_promotion": self.needs_assistive_promotion,
        }


def build_followup_understanding_shadow(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any] | None,
    previous_assistant_text: str = "",
    conversation_entities: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> FollowupUnderstandingShadow:
    """Return a private shadow contract for contextual follow-up understanding."""
    semantic = semantic_frame if isinstance(semantic_frame, dict) else {}
    messages = messages or []
    now_business_time = (now or datetime.now(temporal_resolver.BUSINESS_TZ)).astimezone(temporal_resolver.BUSINESS_TZ)
    latest = str(latest_question or "")
    routing = str(routing_question or "")
    combined = f" {latest}\n{routing} ".lower()

    target_context = _target_context(semantic)
    reason_codes: list[str] = []
    if target_context in _CONTEXT_TARGETS:
        reason_codes.append("semantic_prior_context")
    if _contains_followup_marker(combined):
        reason_codes.append("followup_marker")

    month_windows = tuple(_extract_month_windows(latest or routing, now_business_time))
    if len(month_windows) >= 2:
        reason_codes.append("multi_month_shorthand")
    elif month_windows:
        reason_codes.append("month_reference")

    prior_topic = _prior_topic_signals(
        semantic_frame=semantic,
        conversation_entities=conversation_entities,
        messages=messages,
    )
    if prior_topic.source != "none":
        reason_codes.append("prior_topic_available")

    has_prior_context = bool(
        previous_assistant_text.strip()
        or (conversation_entities and any(conversation_entities.values()))
        or any(str((message or {}).get("role") or "") == "assistant" for message in messages[:-1])
    )
    if _is_self_contained_ticket_retrieval(latest):
        return _empty_shadow(
            target_context=target_context,
            prior_topic=prior_topic,
            reason_codes=("self_contained_ticket_retrieval",),
        )
    is_followup = target_context in _CONTEXT_TARGETS or _contains_followup_marker(combined)

    if not is_followup:
        return _empty_shadow(
            target_context=target_context,
            prior_topic=prior_topic,
            reason_codes=("not_contextual_followup",),
        )
    if not has_prior_context:
        return _empty_shadow(
            target_context=target_context,
            prior_topic=prior_topic,
            reason_codes=tuple(reason_codes + ["insufficient_prior_context"]),
        )
    if _has_explicit_date_range(latest or routing):
        return _empty_shadow(
            target_context=target_context,
            prior_topic=prior_topic,
            reason_codes=tuple(reason_codes + ["explicit_date_range_present"]),
        )

    needs_promotion = bool(month_windows or prior_topic.source != "none")
    confidence = "high" if len(month_windows) >= 2 and prior_topic.source != "none" else "medium"
    return FollowupUnderstandingShadow(
        emitted=True,
        answer_affecting=False,
        contract_type="followup_understanding_shadow",
        target_context=target_context,
        confidence=confidence,
        reason_codes=tuple(_dedupe(reason_codes)),
        month_windows=month_windows,
        prior_topic=prior_topic,
        needs_assistive_promotion=needs_promotion,
    )


def _empty_shadow(
    *,
    target_context: str,
    prior_topic: PriorTopicSignals,
    reason_codes: tuple[str, ...],
) -> FollowupUnderstandingShadow:
    return FollowupUnderstandingShadow(
        emitted=False,
        answer_affecting=False,
        contract_type="followup_understanding_shadow",
        target_context=target_context,
        confidence="low",
        reason_codes=tuple(_dedupe(reason_codes)),
        month_windows=(),
        prior_topic=prior_topic,
        needs_assistive_promotion=False,
    )


def _target_context(semantic_frame: dict[str, Any]) -> str:
    value = semantic_frame.get("target_context")
    if value:
        return str(value)
    task = semantic_frame.get("task") if isinstance(semantic_frame.get("task"), dict) else {}
    source = task.get("dimension_source") if isinstance(task, dict) else None
    return str(source or "")


def _contains_followup_marker(text: str) -> bool:
    padded = f" {' '.join(str(text or '').lower().split())} "
    return any(marker in padded for marker in _FOLLOWUP_MARKERS)


def _is_self_contained_ticket_retrieval(text: str) -> bool:
    """Keep a complete new ticket search out of follow-up-only assist paths."""
    q = " ".join(str(text or "").casefold().split())
    return bool(
        re.search(r"\b(?:can\s+you\s+)?(?:find|search|list|show|check)\b", q)
        and re.search(r"\b(?:tickets?|cases?|incidents?)\b", q)
        and not re.search(r"\b(?:same|those|these|previous|prior|listed|mentioned)\b", q)
    )


def _extract_month_windows(text: str, now: datetime) -> list[MonthWindow]:
    if _has_explicit_date_range(text):
        return []
    month_map = _month_map()
    found: list[tuple[int, str, int]] = []
    for match in re.finditer(r"\b([A-Za-z]{3,9})(?:\s+(\d{4}))?\b", text or ""):
        name = match.group(1).lower()
        month = month_map.get(name)
        if not month:
            continue
        year = int(match.group(2)) if match.group(2) else now.year
        found.append((month, calendar.month_name[month], year))
    windows: list[MonthWindow] = []
    seen: set[tuple[int, int]] = set()
    for month, month_name, year in found:
        key = (year, month)
        if key in seen:
            continue
        seen.add(key)
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        current_month = year == now.year and month == now.month
        windows.append(
            MonthWindow(
                display_label=f"{month_name} {year}",
                start_date=start.isoformat(),
                end_date_exclusive=end.isoformat(),
                current_month_to_date=current_month,
            )
        )
    return windows


def _month_map() -> dict[str, int]:
    months = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
    months.update({name.lower(): idx for idx, name in enumerate(calendar.month_abbr) if name})
    return months


def _has_explicit_date_range(text: str) -> bool:
    q = str(text or "").lower()
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", q):
        return True
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b", q):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", q):
        return True
    return False


def _prior_topic_signals(
    *,
    semantic_frame: dict[str, Any],
    conversation_entities: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> PriorTopicSignals:
    entities = semantic_frame.get("entities") if isinstance(semantic_frame.get("entities"), dict) else {}
    task = semantic_frame.get("task") if isinstance(semantic_frame.get("task"), dict) else {}
    memory = conversation_entities if isinstance(conversation_entities, dict) else {}
    labels = _labels_from_any(memory.get("semantic_labels") or memory.get("labels") or task.get("labels"))
    issue = _clean_text(entities.get("issue_or_category") or task.get("dimension_label") or task.get("task_type"))
    store_id = _clean_store_id(entities.get("store_id") or memory.get("store_id"))
    period = memory.get("period") if isinstance(memory.get("period"), dict) else None
    prior_trace_id = _clean_text(memory.get("trace_id"))
    prior_turn_id = _clean_text(memory.get("turn_id"))
    source = "conversation_entities" if memory else "semantic_frame" if (labels or issue or store_id) else "messages"

    if not (labels or issue or store_id or period or prior_trace_id or prior_turn_id):
        last_assistant = _last_assistant_message(messages)
        if last_assistant:
            source = "messages"
        else:
            source = "none"

    return PriorTopicSignals(
        issue_or_category=issue,
        semantic_labels=tuple(labels),
        store_id=store_id,
        period=period,
        prior_trace_id=prior_trace_id,
        prior_turn_id=prior_turn_id,
        source=source,
    )


def _last_assistant_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages[:-1]):
        if str((message or {}).get("role") or "") == "assistant":
            content = str((message or {}).get("content") or "").strip()
            if content:
                return content
    return ""


def _labels_from_any(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe([str(item) for item in value if str(item or "").strip()])
    if isinstance(value, str):
        return _dedupe([item for item in re.split(r"[,;/]", value) if item.strip()])
    return []


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value or "").strip(" .;:").split())
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _clean_text(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    return text or None


def _clean_store_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None
