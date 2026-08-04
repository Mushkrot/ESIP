"""Guarded ticket-resolution lookup assist contract.

This module is pure and deterministic. It does not call tools and does not
write an answer by using a model. Universal Chat can later use the contract to
run one bounded ticket lookup and build a documented-resolution answer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.services import chat_policy
from app.services.ai_dispatcher_result_sets import extract_ticket_ids


CONTRACT_TYPE = "ticket_resolution_lookup_assist"
MAX_RESOLUTION_CHARS = 500

_RESOLUTION_TERMS = (
    "resolve",
    "resolved",
    "resolution",
    "fix",
    "fixed",
    "repair",
    "repaired",
    "solution",
    "solve",
    "solved",
    "how to resolved",
    "what fixed",
    "what was done",
    "what should we do",
    "next step",
    "next steps",
    "решить",
    "решено",
    "исправить",
    "устранить",
)
_CHALLENGE_TERMS = (
    "where did you",
    "verify",
    "fact check",
    "prove",
    "source",
    "evidence",
    "claim",
    "подтверд",
    "докажи",
    "где ты",
    "источник",
)
_ANALYTICS_TERMS = (
    "how many",
    "count",
    "average",
    "top ",
    "group by",
    "list all",
    "all tickets",
    "current tickets",
    "open tickets",
    "analytics",
)
_TICKET_SEARCH_TERMS = (
    "similar ticket",
    "similar tickets",
    "examples",
    "example tickets",
    "find tickets",
    "show tickets",
    "list tickets",
    "past cases",
    "same restaurant",
    "same store",
)
_DATE_SCOPE_TERMS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
)
_GENERIC_RESOLUTION_VALUES = {
    "resolved",
    "closed",
    "done",
    "fixed",
    "complete",
    "completed",
    "n/a",
    "na",
    "none",
    "no resolution",
    "not provided",
    "issue resolved",
    "problem resolved",
    "resolved.",
    "closed.",
}


@dataclass(frozen=True)
class TicketResolutionLookupAssist:
    accepted: bool
    contract_type: str
    answer_affecting: bool
    reason_codes: tuple[str, ...]
    target_ticket_id: int | None = None
    target_source: str = ""
    request_kind: str = "resolution_lookup"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def build_ticket_resolution_lookup_assist(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any] | None = None,
    explicit_ticket_id: int | None = None,
    target_ticket_id: int | None = None,
    open_ticket_id: int | None = None,
) -> TicketResolutionLookupAssist:
    """Return a fail-closed ticket-resolution lookup assist candidate."""
    text = f"{latest_question}\n{routing_question}".casefold()
    ticket_ids = extract_ticket_ids(text)
    if len(ticket_ids) >= 2:
        return _reject("bulk_ticket_lookup_intent")
    if _looks_like_challenge(text):
        return _reject("challenge_fact_check_intent")
    if _looks_like_date_scope_followup(text):
        return _reject("date_scope_followup_intent")
    if _looks_like_ticket_search_or_analytics(text):
        return _reject("ticket_search_or_analytics_intent")

    target, source = _resolve_target_ticket(
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=target_ticket_id,
        open_ticket_id=open_ticket_id,
        ticket_ids=ticket_ids,
    )
    if not target:
        return _reject("missing_target_ticket")

    semantic_intent = str((semantic_frame or {}).get("intent") or "").casefold()
    has_resolution_intent = _looks_like_resolution_lookup(text) or semantic_intent == "next_steps"
    if not has_resolution_intent:
        return _reject("missing_resolution_intent")

    if source in {"open_ticket", "target_ticket"} and not (
        chat_policy.is_contextual_resolution_followup(latest_question)
        or chat_policy.is_contextual_resolution_followup(routing_question)
        or semantic_intent in {"next_steps", "ticket_details", "current_ticket_context"}
        or _looks_like_resolution_lookup(text)
    ):
        return _reject("insufficient_current_ticket_context")

    reasons = [
        "target_ticket_available",
        f"target_source_{source}",
        "resolution_intent",
    ]
    if semantic_intent:
        reasons.append(f"semantic_{semantic_intent}")
    return TicketResolutionLookupAssist(
        accepted=True,
        contract_type=CONTRACT_TYPE,
        answer_affecting=False,
        reason_codes=tuple(_dedupe(reasons)),
        target_ticket_id=int(target),
        target_source=source,
    )


def build_ticket_resolution_lookup_answer(
    lookup: Mapping[str, Any],
    *,
    target_ticket_id: int | None = None,
    answer_language: str = "English",
) -> str:
    """Build a deterministic answer from a bounded lookup result."""
    is_ru = str(answer_language or "").casefold().startswith("ru")
    rows = [row for row in list((lookup or {}).get("rows") or []) if isinstance(row, Mapping)]
    row = _select_target_row(rows, target_ticket_id)
    requested = _int_list((lookup or {}).get("requested_ids"))
    found = _int_list((lookup or {}).get("found_ids"))
    missing = _int_list((lookup or {}).get("missing_ids"))
    target = _safe_int(target_ticket_id) or (requested[0] if requested else None)

    if not row:
        if is_ru:
            lines = [
                f"Я распознал ticket #{target or '?'}, но не нашел его в bounded lookup.",
                "Я не буду придумывать resolution без ticket record.",
            ]
        else:
            lines = [
                f"I recognized ticket #{target or '?'}, but I did not find it in the bounded lookup.",
                "I will not invent a resolution without a ticket record.",
            ]
        lines.append(_evidence_limits_line(lookup, requested, found, missing, documented_resolution=False))
        lines.append('SOURCES_JSON: {"sources": []}')
        return "\n".join(lines)

    resolution = documented_resolution_text(row.get("resolution"))
    status = _clean_text(row.get("status"))
    solved_at = _clean_text(row.get("solved_at"))
    subject = _clean_text(row.get("subject"))
    store_id = _clean_text(row.get("store_id"))
    issue = _clean_text(row.get("issue_category"))
    row_ticket_id = _safe_int(row.get("ticket_id") or row.get("id")) or target or "?"

    if is_ru:
        lines = [f"Ticket #{row_ticket_id}: documented-resolution lookup."]
        lines.append(f"- Статус: {status or 'unknown'}.")
        if solved_at:
            lines.append(f"- Solved at: {solved_at}.")
        if store_id or subject:
            lines.append(f"- Context: Store {store_id or '?'} - {subject or 'No subject'}.")
        if issue:
            lines.append(f"- Category: {issue}.")
        if resolution:
            lines.append(f"\nДокументированное resolution: {resolution}")
        else:
            lines.append(
                "\nЯ нашел ticket record, но не нашел полезное документированное resolution. "
                "По этому lookup нельзя честно сказать, как именно это было исправлено."
            )
    else:
        lines = [f"Ticket #{row_ticket_id}: documented-resolution lookup."]
        lines.append(f"- Status: {status or 'unknown'}.")
        if solved_at:
            lines.append(f"- Solved at: {solved_at}.")
        if store_id or subject:
            lines.append(f"- Context: Store {store_id or '?'} - {subject or 'No subject'}.")
        if issue:
            lines.append(f"- Category: {issue}.")
        if resolution:
            lines.append(f"\nDocumented resolution: {resolution}")
        else:
            lines.append(
                "\nI found the ticket record, but I did not find a useful documented resolution. "
                "I should not guess how it was fixed from this lookup."
            )
    lines.append(_evidence_limits_line(lookup, requested, found, missing, documented_resolution=bool(resolution)))
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def documented_resolution_text(value: Any) -> str:
    """Return compact resolution text only when it contains useful detail."""
    text = _clean_text(value)[:MAX_RESOLUTION_CHARS]
    if not text:
        return ""
    lower = text.casefold().strip(" .;:-")
    if lower in _GENERIC_RESOLUTION_VALUES:
        return ""
    if len(lower) <= 20 and re.fullmatch(r"(?:issue|problem)?\s*(?:resolved|closed|fixed|done)\s*", lower):
        return ""
    if len(lower.split()) <= 3 and any(word in {"resolved", "closed", "fixed", "done"} for word in lower.split()):
        return ""
    return text


def summarize_ticket_resolution_lookup(lookup: Mapping[str, Any], *, documented_resolution: bool | None = None) -> dict:
    rows = list((lookup or {}).get("rows") or [])
    if documented_resolution is None:
        documented_resolution = any(documented_resolution_text((row or {}).get("resolution")) for row in rows if isinstance(row, Mapping))
    return {
        "requested_count": len((lookup or {}).get("requested_ids") or []),
        "found_count": len((lookup or {}).get("found_ids") or []),
        "missing_count": len((lookup or {}).get("missing_ids") or []),
        "cap": (lookup or {}).get("cap") or 75,
        "truncated": bool((lookup or {}).get("truncated")),
        "documented_resolution": bool(documented_resolution),
    }


def _reject(reason: str) -> TicketResolutionLookupAssist:
    return TicketResolutionLookupAssist(
        accepted=False,
        contract_type=CONTRACT_TYPE,
        answer_affecting=False,
        reason_codes=(reason,),
    )


def _resolve_target_ticket(
    *,
    explicit_ticket_id: int | None,
    target_ticket_id: int | None,
    open_ticket_id: int | None,
    ticket_ids: list[int],
) -> tuple[int | None, str]:
    if explicit_ticket_id:
        return int(explicit_ticket_id), "explicit_ticket"
    if len(ticket_ids) == 1:
        return int(ticket_ids[0]), "explicit_ticket"
    if target_ticket_id:
        return int(target_ticket_id), "target_ticket"
    if open_ticket_id:
        return int(open_ticket_id), "open_ticket"
    return None, ""


def _looks_like_resolution_lookup(text: str) -> bool:
    return any(term in text for term in _RESOLUTION_TERMS) or bool(
        re.search(r"\bhow\s+(?:was|is|do|to)\b.{0,60}\b(?:resolve|resolved|fixed|fix|solved|solve)\b", text)
    )


def _looks_like_challenge(text: str) -> bool:
    return any(term in text for term in _CHALLENGE_TERMS)


def _looks_like_date_scope_followup(text: str) -> bool:
    if not any(term in text for term in _DATE_SCOPE_TERMS):
        return False
    return not _looks_like_resolution_lookup(text)


def _looks_like_ticket_search_or_analytics(text: str) -> bool:
    return any(term in text for term in _TICKET_SEARCH_TERMS) or any(term in text for term in _ANALYTICS_TERMS)


def _select_target_row(rows: list[Mapping[str, Any]], target_ticket_id: int | None) -> Mapping[str, Any] | None:
    target = _safe_int(target_ticket_id)
    if target:
        for row in rows:
            if _safe_int(row.get("ticket_id") or row.get("id")) == target:
                return row
    return rows[0] if rows else None


def _evidence_limits_line(
    lookup: Mapping[str, Any],
    requested: list[int],
    found: list[int],
    missing: list[int],
    *,
    documented_resolution: bool,
) -> str:
    return (
        "\nEvidence limits: "
        f"requested={len(requested)}, "
        f"found={len(found)}, "
        f"missing={len(missing)}, "
        f"cap={(lookup or {}).get('cap') or 75}, "
        f"truncated={bool((lookup or {}).get('truncated'))}, "
        f"documented_resolution={str(bool(documented_resolution)).lower()}."
    )


def _int_list(raw: Any) -> list[int]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for value in raw:
        parsed = _safe_int(value)
        if parsed and parsed not in seen:
            seen.add(parsed)
            out.append(parsed)
    return out


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


__all__ = [
    "CONTRACT_TYPE",
    "TicketResolutionLookupAssist",
    "build_ticket_resolution_lookup_answer",
    "build_ticket_resolution_lookup_assist",
    "documented_resolution_text",
    "summarize_ticket_resolution_lookup",
]
