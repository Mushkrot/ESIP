"""Validate whether a follow-up can reuse the prior topic with a new date scope.

Pure helpers return eligibility and scope metadata; the router applies the result."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


CONTRACT_TYPE = "followup_date_scope_assist"

_MAX_MONTH_WINDOWS = 3
_CHALLENGE_TERMS = (
    "verify",
    "fact check",
    "where did you find",
    "source",
    "prove",
    "evidence",
    "claim",
)
_BULK_TICKET_TERMS = (
    "ticket",
    "tickets",
    "case",
    "cases",
    "compare",
)


@dataclass(frozen=True)
class FollowupDateScopeAssist:
    accepted: bool
    contract_type: str
    answer_affecting: bool
    reason_codes: tuple[str, ...]
    month_windows: tuple[dict[str, Any], ...]
    prior_topic: dict[str, Any]
    display_label: str = ""
    current_month_to_date: bool = False
    source: str = "followup_understanding_shadow"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["month_windows"] = [dict(item) for item in self.month_windows]
        payload["prior_topic"] = dict(self.prior_topic)
        return payload


def build_followup_date_scope_assist(
    *,
    shadow_payload: Mapping[str, Any] | None,
    latest_question: str,
    routing_question: str,
    previous_assistant_text: str = "",
    conversation_entities: Mapping[str, Any] | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> FollowupDateScopeAssist:
    """Return a fail-closed assist candidate from existing shadow metadata."""
    shadow = dict(shadow_payload or {})
    reasons: list[str] = []
    text = f"{latest_question}\n{routing_question}".casefold()

    if not shadow.get("emitted"):
        return _reject("shadow_not_emitted")
    if shadow.get("answer_affecting"):
        return _reject("shadow_already_answer_affecting")
    if not shadow.get("needs_assistive_promotion"):
        return _reject("shadow_does_not_need_promotion")

    shadow_reasons = _string_list(shadow.get("reason_codes"))
    if "explicit_date_range_present" in set(shadow_reasons) or _has_explicit_day_or_date(text):
        return _reject("explicit_date_range_present")

    if _looks_like_challenge_or_fact_check(text):
        return _reject("challenge_fact_check_intent")
    if _looks_like_bulk_ticket_lookup(text):
        return _reject("bulk_ticket_lookup_intent")

    windows = _valid_month_windows(shadow.get("month_windows"))
    if not windows:
        return _reject("missing_month_windows")
    if len(windows) > _MAX_MONTH_WINDOWS:
        return _reject("too_many_month_windows")

    prior_topic = _clean_prior_topic(shadow.get("prior_topic"))
    if not _has_topic_scope(prior_topic):
        return _reject("missing_prior_topic")

    if not _has_prior_context(
        prior_topic=prior_topic,
        previous_assistant_text=previous_assistant_text,
        conversation_entities=conversation_entities,
        messages=messages,
    ):
        return _reject("insufficient_prior_context")

    reasons.extend(
        [
            "shadow_emitted",
            "month_windows_available",
            "prior_topic_available",
            "prior_context_available",
        ]
    )
    if any(bool(item.get("current_month_to_date")) for item in windows):
        reasons.append("current_month_to_date")

    return FollowupDateScopeAssist(
        accepted=True,
        contract_type=CONTRACT_TYPE,
        answer_affecting=False,
        reason_codes=tuple(_dedupe(reasons)),
        month_windows=tuple(windows),
        prior_topic=prior_topic,
        display_label=_display_label(windows),
        current_month_to_date=any(bool(item.get("current_month_to_date")) for item in windows),
    )


def _reject(reason: str) -> FollowupDateScopeAssist:
    return FollowupDateScopeAssist(
        accepted=False,
        contract_type=CONTRACT_TYPE,
        answer_affecting=False,
        reason_codes=(reason,),
        month_windows=(),
        prior_topic={},
    )


def _valid_month_windows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    windows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return []
        start = _clean_text(item.get("start_date"))
        end = _clean_text(item.get("end_date_exclusive"))
        label = _clean_text(item.get("display_label"))
        if not start or not end or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            return []
        windows.append(
            {
                "display_label": label or f"{start} to {end}",
                "start_date": start,
                "end_date_exclusive": end,
                "timezone": _clean_text(item.get("timezone")) or "America/New_York",
                "source": _clean_text(item.get("source")) or "latest_question",
                "current_month_to_date": bool(item.get("current_month_to_date")),
            }
        )
    return windows


def _clean_prior_topic(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    labels = _dedupe(_string_list(raw.get("semantic_labels")))
    topic: dict[str, Any] = {
        "semantic_labels": labels,
    }
    for key in ("issue_or_category", "store_id", "prior_trace_id", "prior_turn_id", "source"):
        value = _clean_text(raw.get(key))
        if value:
            topic[key] = value
    period = raw.get("period")
    if isinstance(period, Mapping):
        topic["period"] = dict(period)
    return {key: value for key, value in topic.items() if value not in ("", [], None)}


def _has_topic_scope(prior_topic: Mapping[str, Any]) -> bool:
    return bool(
        _clean_text(prior_topic.get("store_id"))
        or _string_list(prior_topic.get("semantic_labels"))
        or _clean_text(prior_topic.get("issue_or_category"))
    )


def _has_prior_context(
    *,
    prior_topic: Mapping[str, Any],
    previous_assistant_text: str,
    conversation_entities: Mapping[str, Any] | None,
    messages: Sequence[Mapping[str, Any]] | None,
) -> bool:
    if str(previous_assistant_text or "").strip():
        return True
    if isinstance(conversation_entities, Mapping) and any(conversation_entities.values()):
        return True
    if _clean_text(prior_topic.get("prior_trace_id")) or _clean_text(prior_topic.get("prior_turn_id")):
        return True
    for message in list(messages or [])[:-1]:
        if str((message or {}).get("role") or "") == "assistant" and str((message or {}).get("content") or "").strip():
            return True
    return False


def _looks_like_challenge_or_fact_check(text: str) -> bool:
    return any(term in text for term in _CHALLENGE_TERMS)


def _looks_like_bulk_ticket_lookup(text: str) -> bool:
    ticket_ids = re.findall(r"#?\b\d{5,7}\b", text)
    if len(ticket_ids) >= 2:
        return True
    if ticket_ids and any(term in text for term in _BULK_TICKET_TERMS):
        return True
    return bool(ticket_ids and re.fullmatch(r"[\s#,;:/\\andor0-9-]+", text or ""))


def _has_explicit_day_or_date(text: str) -> bool:
    q = str(text or "").casefold()
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", q):
        return True
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}\b", q):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", q):
        return True
    return False


def _display_label(windows: Sequence[Mapping[str, Any]]) -> str:
    labels = [_clean_text(item.get("display_label")) for item in windows]
    labels = [label for label in labels if label]
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item or "").strip()]
    if isinstance(raw, tuple):
        return [str(item) for item in raw if str(item or "").strip()]
    if isinstance(raw, str):
        return [item for item in re.split(r"[,;/]", raw) if item.strip()]
    return []


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value).strip(" .;:")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out
