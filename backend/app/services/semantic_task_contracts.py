"""Task contracts derived from semantic interpretation before Universal Chat routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any

from app.services import semantic_label_counts, temporal_resolver


@dataclass(frozen=True)
class PeriodContract:
    display_label: str
    start_date: str
    end_date_exclusive: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TaskContract:
    task_type: str
    dimension_type: str | None = None
    dimension_source: str | None = None
    labels: list[str] | None = None
    store_id: str | None = None
    period: PeriodContract | None = None
    overlap_allowed: bool = False
    requires_ticket_ids: bool = False
    official_field_only: bool = False
    answer_language: str = "English"
    confidence: str = "low"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["labels"] = list(self.labels or [])
        return payload


def build_task_contract(
    *,
    latest_question: str,
    routing_question: str,
    previous_assistant_text: str,
    semantic_frame: dict[str, Any],
    conversation_entities: dict[str, Any] | None,
    explicit_store_context: str | None,
    open_ticket_store_id: str | None,
    messages: list[dict],
) -> TaskContract | None:
    """Build the highest-confidence executable task contract for this turn."""
    if _asks_for_official_issue_category(latest_question, routing_question, semantic_frame):
        return None

    contract = _build_semantic_label_count_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text=previous_assistant_text,
        semantic_frame=semantic_frame,
        conversation_entities=conversation_entities,
        explicit_store_context=explicit_store_context,
        open_ticket_store_id=open_ticket_store_id,
        messages=messages,
    )
    return contract


def build_conversation_entities(
    *,
    final_answer: str,
    task_contract: TaskContract | None,
    semantic_frame: dict[str, Any],
    store_id: str | None,
    period: PeriodContract | None,
    trace_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """Return lightweight per-conversation entities introduced by this turn."""
    labels: list[str] = []
    label_source: str | None = None
    if task_contract and task_contract.dimension_type == "semantic_labels":
        labels = _dedupe_labels(task_contract.labels or [])
        label_source = task_contract.dimension_source
    if not labels:
        labels = semantic_label_counts.extract_labels_from_answer(final_answer)
        label_source = "assistant_answer" if labels else None

    entities: dict[str, Any] = {
        "trace_id": trace_id,
        "turn_id": turn_id,
        "semantic_labels": labels,
        "semantic_label_source": label_source,
        "store_id": _clean_store_id(store_id),
        "period": period.to_dict() if period else None,
        "semantic_task": _safe_task_from_frame(semantic_frame),
    }
    return {key: value for key, value in entities.items() if value not in (None, "", [])}


def period_contract_from_temporal(
    period: temporal_resolver.TemporalResolution | None,
) -> PeriodContract | None:
    """Convert deterministic temporal resolution into a serializable task period."""
    return _period_contract(period) if period else None


def _build_semantic_label_count_contract(
    *,
    latest_question: str,
    routing_question: str,
    previous_assistant_text: str,
    semantic_frame: dict[str, Any],
    conversation_entities: dict[str, Any] | None,
    explicit_store_context: str | None,
    open_ticket_store_id: str | None,
    messages: list[dict],
) -> TaskContract | None:
    combined_question = f"{latest_question}\n{routing_question}".strip()
    task = _safe_task_from_frame(semantic_frame)
    task_type = str(task.get("task_type") or "")
    dimension_type = str(task.get("dimension_type") or "")
    wants_semantic_count = (
        task_type == "count_by_dimension"
        and dimension_type == "semantic_labels"
    ) or semantic_label_counts.looks_like_label_count_question(combined_question)
    if not wants_semantic_count:
        return None

    label_request = semantic_label_counts.build_label_request(
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text="",
    )
    labels = label_request.labels if label_request else []
    label_source = label_request.source if label_request else ""
    task_labels = _labels_from_task(task)
    if task_labels and str(task.get("dimension_source") or "") == "current_question" and len(task_labels) >= len(labels):
        labels = task_labels
        label_source = "current_question"

    if not labels:
        labels = task_labels
        label_source = "semantic_task" if labels else ""

    if not labels and _references_prior_labels(combined_question, task):
        labels = _labels_from_entities(conversation_entities)
        label_source = "conversation_memory" if labels else ""

    if not labels:
        fallback_request = semantic_label_counts.build_label_request(
            latest_question=latest_question,
            routing_question=routing_question,
            previous_assistant_text=previous_assistant_text,
        )
        labels = fallback_request.labels if fallback_request else []
        label_source = fallback_request.source if fallback_request else ""

    if not labels:
        frame_request = semantic_label_counts.build_label_request_from_label_text(
            str(((semantic_frame.get("entities") or {}) if isinstance(semantic_frame.get("entities"), dict) else {}).get("issue_or_category") or ""),
            source="semantic_frame",
        )
        labels = frame_request.labels if frame_request else []
        label_source = frame_request.source if frame_request else ""

    labels = _dedupe_labels(labels)[:10]
    if len(labels) < 2:
        return None

    period = _resolve_period(latest_question, routing_question, messages, conversation_entities)
    if not period:
        return None

    store_id = (
        _clean_store_id(explicit_store_context)
        or _clean_store_id(((semantic_frame.get("entities") or {}) if isinstance(semantic_frame.get("entities"), dict) else {}).get("store_id"))
        or _clean_store_id((conversation_entities or {}).get("store_id"))
        or _clean_store_id(open_ticket_store_id)
    )
    if not store_id:
        return None

    return TaskContract(
        task_type="count_by_dimension",
        dimension_type="semantic_labels",
        dimension_source=label_source or "unknown",
        labels=labels,
        store_id=store_id,
        period=_period_contract(period),
        overlap_allowed=True,
        requires_ticket_ids=True,
        answer_language=str(semantic_frame.get("answer_language") or "English"),
        confidence=str(semantic_frame.get("confidence") or "low"),
        reason="semantic label count contract built before Phase 1 routing",
    )


def _asks_for_official_issue_category(
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any],
) -> bool:
    text = f"{latest_question}\n{routing_question}".lower()
    if "issue_category" in text:
        return True
    if "official" in text and "zendesk" in text and "categor" in text:
        return True
    return False


def _safe_task_from_frame(semantic_frame: dict[str, Any]) -> dict[str, Any]:
    task = semantic_frame.get("task") if isinstance(semantic_frame, dict) else None
    return task if isinstance(task, dict) else {}


def _labels_from_task(task: dict[str, Any]) -> list[str]:
    raw = task.get("labels")
    if isinstance(raw, list):
        return _dedupe_labels([str(item) for item in raw if str(item or "").strip()])
    if isinstance(raw, str):
        request = semantic_label_counts.build_label_request_from_label_text(raw, source="semantic_task")
        return request.labels if request else []
    return []


def _labels_from_entities(entities: dict[str, Any] | None) -> list[str]:
    if not isinstance(entities, dict):
        return []
    raw = entities.get("semantic_labels") or entities.get("labels")
    if not isinstance(raw, list):
        return []
    return _dedupe_labels([str(item) for item in raw if str(item or "").strip()])


def _references_prior_labels(text: str, task: dict[str, Any]) -> bool:
    source = str(task.get("dimension_source") or "").lower()
    if source in {"prior_turn", "conversation_memory", "previous_answer"}:
        return True
    lower = text.lower()
    compact = " ".join(lower.split())
    return any(
        marker in lower or marker in compact
        for marker in (
            "listed categor",
            "mentioned categor",
            "those categor",
            "each listed",
            "перечислен",
            "перечисленных",
            "каждой из",
            "каждой категории",
        )
    )


def _resolve_period(
    latest_question: str,
    routing_question: str,
    messages: list[dict],
    conversation_entities: dict[str, Any] | None,
) -> temporal_resolver.TemporalResolution | None:
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
    saved = (conversation_entities or {}).get("period")
    if isinstance(saved, dict):
        display = str(saved.get("display_label") or "")
        start_date = str(saved.get("start_date") or "")
        end_date = str(saved.get("end_date_exclusive") or "")
        if display and start_date and end_date:
            return temporal_resolver.TemporalResolution(
                intent="ticket_count",
                period_type="explicit",
                display_label=display,
                start_at=datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=temporal_resolver.BUSINESS_TZ),
                end_at=datetime.combine(date.fromisoformat(end_date), time.min, tzinfo=temporal_resolver.BUSINESS_TZ),
                source_plan="bq",
            )
    return None


def _period_contract(period: temporal_resolver.TemporalResolution) -> PeriodContract:
    return PeriodContract(
        display_label=period.display_label,
        start_date=period.start_date.isoformat(),
        end_date_exclusive=period.end_date_exclusive.isoformat(),
    )


def _dedupe_labels(labels: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        cleaned = " ".join(str(label or "").strip(" .;:").split())
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _clean_store_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None
