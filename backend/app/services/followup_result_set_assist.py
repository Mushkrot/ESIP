"""Pure deterministic assist contracts for saved result-set follow-ups.

The functions in this module do not call models, databases, Firestore, Zendesk,
Drive, GCS, Vertex, or any external service. They only inspect an already-loaded
structured result set and produce compact metadata or deterministic text.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


CONTRACT_TYPE = "followup_result_set_assist"
SHADOW_CONTRACT_TYPE = "followup_result_set_refinement_shadow"
TICKET_REFERENCE_SHADOW_CONTRACT_TYPE = "followup_ticket_reference_shadow"
MAX_RENDERED_ROWS = 12

_STATUS_ALIASES = {
    "open": "open",
    "opened": "open",
    "pending": "pending",
    "hold": "hold",
    "on-hold": "hold",
    "on hold": "hold",
    "solved": "solved",
    "resolved": "solved",
    "closed": "closed",
}
_FACT_CHECK_TERMS = (
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
_RESOLUTION_LOOKUP_PATTERNS = (
    r"\bwhat\s+fixed\b",
    r"\bhow\s+(?:was\s+)?(?:it\s+|this\s+|that\s+)?(?:fixed|resolved)\b",
    r"\bwhat\s+was\s+done\b",
    r"\bnext\s+steps?\b",
    r"\bresolve\s+this\b",
    r"\bresolution\s+for\b",
    r"\bрешить\b",
    r"\bрешено\b",
    r"\bисправить\b",
)
_PROCEDURE_TERMS = (
    "how do i",
    "how can i",
    "what should i do",
    "procedure",
    "kb",
    "article",
    "job aid",
    "document",
)


@dataclass(frozen=True)
class FollowupResultSetAssist:
    accepted: bool
    contract_type: str = CONTRACT_TYPE
    answer_affecting: bool = False
    request_kind: str = ""
    reason_codes: tuple[str, ...] = ()
    filters: Mapping[str, Any] = field(default_factory=dict)
    selected_ticket_ids: tuple[int, ...] = ()
    displayed_ticket_ids: tuple[int, ...] = ()
    omitted_ticket_ids: tuple[int, ...] = ()
    target_ticket_id: int | None = None
    target_source: str = ""
    lookup_cap: int | None = None
    evidence_limits: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["filters"] = dict(self.filters)
        payload["selected_ticket_ids"] = list(self.selected_ticket_ids)
        payload["displayed_ticket_ids"] = list(self.displayed_ticket_ids)
        payload["omitted_ticket_ids"] = list(self.omitted_ticket_ids)
        payload["evidence_limits"] = dict(self.evidence_limits)
        return {key: value for key, value in payload.items() if value not in (None, "", [], (), {})}


@dataclass(frozen=True)
class FollowupResultSetCandidate:
    is_candidate: bool
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"is_candidate": self.is_candidate, "reason_codes": list(self.reason_codes)}


def detect_followup_result_set_candidate(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any] | None = None,
    previous_assistant_text: str = "",
) -> FollowupResultSetCandidate:
    """Return whether a turn is worth loading saved result-set memory for."""
    text = _normalize_text(f"{latest_question}\n{routing_question}")
    if not text.strip():
        return FollowupResultSetCandidate(False, ("empty_question",))
    competing = _competing_intent(text)
    if competing:
        return FollowupResultSetCandidate(False, (competing,))
    target_context = _normalize_text((semantic_frame or {}).get("target_context"))
    if not previous_assistant_text and target_context not in {"prior_turn", "conversation", "conversation_memory", "previous_answer"}:
        return FollowupResultSetCandidate(False, ("no_prior_context_signal",))
    if _looks_like_omitted_request(text):
        return FollowupResultSetCandidate(True, ("omitted_displayed_request",))
    if _extract_filters(text):
        return FollowupResultSetCandidate(True, ("explicit_result_set_filter",))
    target_id, _source, target_reason = _target_ticket_reference(text, {}, (), ())
    if target_reason:
        return FollowupResultSetCandidate(True, (target_reason,))
    if target_id:
        return FollowupResultSetCandidate(True, ("ticket_reference_followup",))
    if any(term in text for term in ("first", "second", "third", "last", "that one", "this one", "details")):
        return FollowupResultSetCandidate(True, ("ticket_reference_followup",))
    return FollowupResultSetCandidate(False, ("unsupported_result_set_followup",))


def build_followup_result_set_assist(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: Mapping[str, Any] | None = None,
    previous_assistant_text: str = "",
    conversation_entities: Mapping[str, Any] | None = None,
    result_set: Mapping[str, Any] | None = None,
) -> FollowupResultSetAssist:
    """Return a fail-closed contract for saved result-set follow-up turns."""
    del conversation_entities  # Reserved for future compact context, not factual evidence.
    text = _normalize_text(f"{latest_question}\n{routing_question}")
    result = dict(result_set or {})
    if not _has_result_set(result):
        return _reject("missing_result_set")

    competing = _competing_intent(text)
    if competing:
        return _reject(competing)

    if not _has_prior_context(previous_assistant_text=previous_assistant_text, result_set=result):
        return _reject("insufficient_prior_context")

    tickets = _ticket_rows(result)
    ticket_ids = _unique_ints(result.get("ticket_ids") or [row.get("ticket_id") for row in tickets])
    displayed_ids = _unique_ints(result.get("displayed_ticket_ids") or ticket_ids[:1])
    evidence_limits = _evidence_limits(result, ticket_ids, displayed_ids, tickets)

    if _looks_like_omitted_request(text):
        omitted_ids = [ticket_id for ticket_id in ticket_ids if ticket_id not in set(displayed_ids)]
        return FollowupResultSetAssist(
            accepted=True,
            request_kind="omitted_result_set",
            reason_codes=("saved_result_set_available", "omitted_displayed_request"),
            displayed_ticket_ids=tuple(displayed_ids),
            omitted_ticket_ids=tuple(omitted_ids),
            evidence_limits=evidence_limits,
        )

    target_id, target_source, target_reason = _target_ticket_reference(text, result, ticket_ids, displayed_ids)
    if target_reason:
        return _reject(target_reason)
    if target_id:
        return FollowupResultSetAssist(
            accepted=True,
            request_kind="ticket_reference_lookup",
            reason_codes=("saved_result_set_available", f"target_source_{target_source}"),
            target_ticket_id=target_id,
            target_source=target_source,
            lookup_cap=1,
            selected_ticket_ids=(target_id,),
            evidence_limits=evidence_limits,
        )

    filters = _extract_filters(text)
    if not filters:
        return _reject("unsupported_result_set_followup")

    selected_ids = _apply_filters(tickets, filters)
    return FollowupResultSetAssist(
        accepted=True,
        request_kind="result_set_refinement",
        reason_codes=("saved_result_set_available", "explicit_result_set_filter"),
        filters=filters,
        selected_ticket_ids=tuple(selected_ids),
        displayed_ticket_ids=tuple(displayed_ids),
        evidence_limits={**evidence_limits, "selected_count": len(selected_ids)},
    )


def build_result_set_refinement_answer(
    assist_payload: Mapping[str, Any],
    result_set: Mapping[str, Any],
    *,
    answer_language: str = "English",
) -> str | None:
    """Build a deterministic answer from saved structured result-set memory."""
    assist = dict(assist_payload or {})
    if not assist.get("accepted"):
        return None
    kind = str(assist.get("request_kind") or "")
    if kind not in {"result_set_refinement", "omitted_result_set"}:
        return None

    is_ru = str(answer_language or "").casefold().startswith("ru")
    rows = _rows_by_id(_ticket_rows(result_set))
    ticket_ids = _unique_ints(result_set.get("ticket_ids") or rows.keys())
    displayed_ids = _unique_ints(assist.get("displayed_ticket_ids") or result_set.get("displayed_ticket_ids") or [])
    criteria = ", ".join(_clean_string_list(result_set.get("criteria"))) or ("saved criteria" if not is_ru else "сохраненные критерии")
    stores = ", ".join(_clean_string_list(result_set.get("requested_store_ids"))) or ("not stored" if not is_ru else "не сохранены")

    if kind == "omitted_result_set":
        omitted_ids = _unique_ints(assist.get("omitted_ticket_ids") or [ticket_id for ticket_id in ticket_ids if ticket_id not in set(displayed_ids)])
        lines = (
            [
                "I used the saved previous result set to compare displayed vs omitted ticket IDs.",
                f"Saved criteria: {criteria}.",
                f"Saved store scope: {stores}.",
                f"Displayed: {_ids_line(displayed_ids)}.",
                f"Omitted: {_ids_line(omitted_ids)}.",
            ]
            if not is_ru
            else [
                "Я использовал сохраненный предыдущий структурированный результат, чтобы сравнить displayed vs omitted ticket IDs.",
                f"Критерии: {criteria}.",
                f"Store scope: {stores}.",
                f"Displayed: {_ids_line(displayed_ids)}.",
                f"Omitted: {_ids_line(omitted_ids)}.",
            ]
        )
        if omitted_ids:
            lines.append("Omitted ticket details from saved rows:")
            lines.extend(_ticket_line(rows.get(ticket_id)) for ticket_id in omitted_ids[:MAX_RENDERED_ROWS])
    else:
        selected_ids = _unique_ints(assist.get("selected_ticket_ids"))
        lines = (
            [
                "I used the saved previous result set and applied your follow-up filter to it.",
                f"Saved criteria: {criteria}.",
                f"Saved store scope: {stores}.",
                f"Matched after filter: {_ids_line(selected_ids)}.",
            ]
            if not is_ru
            else [
                "Я использовал сохраненный предыдущий структурированный результат и применил к нему ваш follow-up filter.",
                f"Критерии: {criteria}.",
                f"Store scope: {stores}.",
                f"После filter: {_ids_line(selected_ids)}.",
            ]
        )
        if selected_ids:
            lines.append("Filtered tickets:")
            lines.extend(_ticket_line(rows.get(ticket_id)) for ticket_id in selected_ids[:MAX_RENDERED_ROWS])
        else:
            lines.append("No saved tickets matched that filter.")

    lines.append(_limits_line(assist.get("evidence_limits"), len(ticket_ids), len(displayed_ids)))
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(line for line in lines if line)


def build_ticket_reference_lookup_answer(
    assist_payload: Mapping[str, Any],
    lookup_result: Mapping[str, Any],
    result_set: Mapping[str, Any],
    *,
    answer_language: str = "English",
) -> str | None:
    """Build a deterministic answer for one bounded ticket reference lookup."""
    assist = dict(assist_payload or {})
    if not assist.get("accepted") or assist.get("request_kind") != "ticket_reference_lookup":
        return None
    is_ru = str(answer_language or "").casefold().startswith("ru")
    rows = [row for row in list((lookup_result or {}).get("rows") or []) if isinstance(row, Mapping)]
    target = _safe_int(assist.get("target_ticket_id"))
    row = next((_compact_lookup_row(item) for item in rows if _safe_int(item.get("ticket_id") or item.get("id")) == target), None)
    criteria = ", ".join(_clean_string_list(result_set.get("criteria"))) or ("saved criteria" if not is_ru else "сохраненные критерии")
    cap = _safe_int((lookup_result or {}).get("cap")) or _safe_int(assist.get("lookup_cap")) or 1

    if not row:
        lines = (
            [
                f"I resolved the saved-result reference to ticket #{target or '?'}, but the bounded lookup did not return that row.",
                "I will not invent ticket details without lookup evidence.",
            ]
            if not is_ru
            else [
                f"Я распознал reference из сохраненного результата как ticket #{target or '?'}, но bounded lookup не вернул эту строку.",
                "Я не буду придумывать ticket details без lookup evidence.",
            ]
        )
    else:
        lines = (
            [
                f"I resolved your follow-up to ticket #{row.get('ticket_id')} from the saved previous result set.",
                f"Saved criteria: {criteria}.",
                _ticket_line(row),
            ]
            if not is_ru
            else [
                f"Я связал ваш follow-up с ticket #{row.get('ticket_id')} из сохраненного предыдущего результата.",
                f"Критерии: {criteria}.",
                _ticket_line(row),
            ]
        )
    found = len(_unique_ints((lookup_result or {}).get("found_ids") or [row.get("ticket_id") for row in rows]))
    missing = len(_unique_ints((lookup_result or {}).get("missing_ids") or []))
    lines.append(f"Evidence limits: bounded_ticket_lookup=true, requested=1, found={found}, missing={missing}, cap={cap}, saved_previous_result_set=true.")
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(line for line in lines if line)


def summarize_followup_result_set_assist(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return compact trace-safe metadata for Universal policy flags."""
    data = dict(payload or {})
    return {
        "accepted": bool(data.get("accepted")),
        "contract_type": data.get("contract_type") or CONTRACT_TYPE,
        "request_kind": data.get("request_kind") or "",
        "answer_affecting": bool(data.get("answer_affecting")),
        "reason_codes": list(data.get("reason_codes") or []),
        "filters": dict(data.get("filters") or {}),
        "selected_ticket_ids": _unique_ints(data.get("selected_ticket_ids")),
        "displayed_ticket_ids": _unique_ints(data.get("displayed_ticket_ids")),
        "omitted_ticket_ids": _unique_ints(data.get("omitted_ticket_ids")),
        "target_ticket_id": _safe_int(data.get("target_ticket_id")),
        "target_source": data.get("target_source") or "",
        "lookup_cap": _safe_int(data.get("lookup_cap")),
        "evidence_limits": dict(data.get("evidence_limits") or {}),
    }


def build_result_set_refinement_shadow_observation(
    assist_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return compact metadata for a configured refinement shadow evaluation."""
    data = dict(assist_payload or {})
    request_kind = _clean_text(data.get("request_kind"))[:60]
    contract_accepted = bool(data.get("accepted"))
    accepted = contract_accepted and request_kind in {"result_set_refinement", "omitted_result_set"}

    if accepted:
        reason_codes = _safe_reason_codes(data.get("reason_codes")) or ["shadow_contract_accepted"]
    elif contract_accepted and request_kind == "ticket_reference_lookup":
        reason_codes = ["different_shadow_class_ticket_reference"]
    else:
        reason_codes = _safe_reason_codes(data.get("reason_codes")) or ["shadow_contract_rejected"]

    selected_values = (
        data.get("omitted_ticket_ids")
        if request_kind == "omitted_result_set"
        else data.get("selected_ticket_ids")
    )
    limits = dict(data.get("evidence_limits") or {})
    compact_limits = {
        key: _safe_nonnegative_int(limits.get(key))
        for key in ("ticket_count", "displayed_count", "stored_row_count")
        if _safe_nonnegative_int(limits.get(key)) is not None
    }
    if "truncated" in limits:
        compact_limits["truncated"] = bool(limits.get("truncated"))

    filters = data.get("filters") if isinstance(data.get("filters"), Mapping) else {}
    return {
        "task_class": "followup_result_set_refinement",
        "contract_type": SHADOW_CONTRACT_TYPE,
        "candidate": True,
        "enabled_as_shadow": True,
        "accepted": accepted,
        "rejected": not accepted,
        "answer_affecting": False,
        "request_kind": request_kind,
        "reason_codes": reason_codes,
        "selected_count": len(_unique_ints(selected_values)),
        "filter_names": sorted(
            str(key)
            for key in filters
            if str(key) in {"statuses", "store_ids", "displayed_only"}
        ),
        "evidence_limits": compact_limits,
    }


def build_ticket_reference_shadow_observation(
    assist_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return compact metadata for a configured saved-set ticket-reference shadow."""
    data = dict(assist_payload or {})
    request_kind = _clean_text(data.get("request_kind"))[:60]
    target_ticket_id = _safe_int(data.get("target_ticket_id"))
    contract_accepted = bool(data.get("accepted"))
    accepted = (
        contract_accepted
        and request_kind == "ticket_reference_lookup"
        and target_ticket_id is not None
    )

    if accepted:
        reason_codes = _safe_reason_codes(data.get("reason_codes")) or ["shadow_target_resolved"]
    elif contract_accepted and request_kind in {"result_set_refinement", "omitted_result_set"}:
        reason_codes = ["different_shadow_class_result_set_refinement"]
    elif contract_accepted:
        reason_codes = ["missing_target_ticket"]
    else:
        reason_codes = _safe_reason_codes(data.get("reason_codes")) or ["shadow_contract_rejected"]

    target_source = _clean_text(data.get("target_source"))[:80]
    if not re.fullmatch(r"displayed_ordinal_(?:first|second|third|fourth|last)", target_source):
        target_source = ""
    limits = dict(data.get("evidence_limits") or {})
    compact_limits = {
        key: _safe_nonnegative_int(limits.get(key))
        for key in ("ticket_count", "displayed_count", "stored_row_count")
        if _safe_nonnegative_int(limits.get(key)) is not None
    }
    if "truncated" in limits:
        compact_limits["truncated"] = bool(limits.get("truncated"))

    return {
        "task_class": "followup_ticket_reference",
        "contract_type": TICKET_REFERENCE_SHADOW_CONTRACT_TYPE,
        "candidate": True,
        "enabled_as_shadow": True,
        "accepted": accepted,
        "rejected": not accepted,
        "answer_affecting": False,
        "request_kind": request_kind,
        "reason_codes": reason_codes,
        "target_ticket_id": target_ticket_id if accepted else None,
        "target_source": target_source if accepted else "",
        "lookup_performed": False,
        "evidence_limits": compact_limits,
    }


def _reject(reason: str) -> FollowupResultSetAssist:
    return FollowupResultSetAssist(accepted=False, reason_codes=(reason,))


def _safe_reason_codes(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else []
    return _dedupe(
        str(item).strip()
        for item in values
        if re.fullmatch(r"[a-z0-9_]{1,80}", str(item).strip())
    )


def _safe_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _has_result_set(result_set: Mapping[str, Any]) -> bool:
    return bool(_unique_ints((result_set or {}).get("ticket_ids")) or _ticket_rows(result_set))


def _has_prior_context(*, previous_assistant_text: str, result_set: Mapping[str, Any]) -> bool:
    return bool(str(previous_assistant_text or "").strip() or result_set.get("trace_id") or result_set.get("turn_id"))


def _competing_intent(text: str) -> str:
    ticket_ids = list(dict.fromkeys(re.findall(r"(?<!\d)\d{5,10}(?!\d)", text)))
    if len(ticket_ids) >= 2:
        return "bulk_ticket_lookup_intent"
    if len(ticket_ids) == 1:
        return "explicit_ticket_lookup_intent"
    if any(term in text for term in _FACT_CHECK_TERMS):
        return "challenge_fact_check_intent"
    if any(term in text for term in _DATE_SCOPE_TERMS):
        return "date_scope_followup_intent"
    if any(re.search(pattern, text) for pattern in _RESOLUTION_LOOKUP_PATTERNS):
        return "ticket_resolution_lookup_intent"
    if any(term in text for term in _PROCEDURE_TERMS):
        return "procedure_lookup_intent"
    return ""


def _looks_like_omitted_request(text: str) -> bool:
    return any(term in text for term in ("omit", "omitted", "hidden", "not show", "not displayed", "left out"))


def _target_ticket_reference(
    text: str,
    result_set: Mapping[str, Any],
    ticket_ids: Sequence[int],
    displayed_ids: Sequence[int],
) -> tuple[int | None, str, str]:
    del result_set
    if not any(term in text for term in ("first", "second", "third", "fourth", "last", "that one", "this one", "details")):
        return None, "", ""
    if any(term in text for term in ("that one", "this one")) and not any(term in text for term in ("first", "second", "third", "fourth", "last")):
        return None, "", "ambiguous_ticket_reference"
    ordered = list(displayed_ids or ticket_ids)
    if not ordered:
        return None, "", "missing_target_ticket"
    if "first" in text:
        return ordered[0], "displayed_ordinal_first", ""
    if "second" in text:
        return (ordered[1], "displayed_ordinal_second", "") if len(ordered) >= 2 else (None, "", "ordinal_out_of_range")
    if "third" in text:
        return (ordered[2], "displayed_ordinal_third", "") if len(ordered) >= 3 else (None, "", "ordinal_out_of_range")
    if "fourth" in text:
        return (ordered[3], "displayed_ordinal_fourth", "") if len(ordered) >= 4 else (None, "", "ordinal_out_of_range")
    if "last" in text:
        return ordered[-1], "displayed_ordinal_last", ""
    return None, "", ""


def _extract_filters(text: str) -> dict[str, Any]:
    statuses = _extract_statuses(text)
    store_ids = _extract_store_ids(text)
    filters: dict[str, Any] = {}
    if statuses:
        filters["statuses"] = statuses
    if store_ids:
        filters["store_ids"] = store_ids
    if "displayed" in text or "shown" in text:
        filters["displayed_only"] = True
    return filters


def _extract_statuses(text: str) -> list[str]:
    found: list[str] = []
    for raw, normalized in _STATUS_ALIASES.items():
        if re.search(rf"\b{re.escape(raw)}\b", text):
            found.append(normalized)
    return _dedupe(found)


def _extract_store_ids(text: str) -> list[str]:
    stores: list[str] = []
    for match in re.finditer(r"\b(?:store\s*)?#?(\d{4})\b", text):
        stores.append(match.group(1))
    return _dedupe(stores)


def _apply_filters(rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Any]) -> list[int]:
    selected: list[int] = []
    statuses = {str(item).casefold() for item in filters.get("statuses") or []}
    stores = {str(item) for item in filters.get("store_ids") or []}
    for row in rows:
        ticket_id = _safe_int(row.get("ticket_id") or row.get("id"))
        if not ticket_id:
            continue
        status = _normalize_status(row.get("status"))
        store_id = _clean_text(row.get("store_id"))
        if statuses and status not in statuses:
            continue
        if stores and store_id not in stores:
            continue
        selected.append(ticket_id)
    return _unique_ints(selected)


def _ticket_rows(result_set: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_rows = (result_set or {}).get("tickets") if isinstance(result_set, Mapping) else []
    for row in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        compact = _compact_lookup_row(row)
        if compact.get("ticket_id"):
            rows.append(compact)
    return rows


def _compact_lookup_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": _safe_int(row.get("ticket_id") or row.get("id")),
        "subject": _clean_text(row.get("subject"))[:160],
        "store_id": _clean_text(row.get("store_id"))[:20],
        "status": _clean_text(row.get("status"))[:40],
        "created_at": _clean_text(row.get("created_at"))[:40],
        "issue_category": _clean_text(row.get("issue_category"))[:120],
    }


def _rows_by_id(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        ticket_id = _safe_int(row.get("ticket_id"))
        if ticket_id:
            out[ticket_id] = dict(row)
    return out


def _ticket_line(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "- #? - saved row unavailable"
    ticket_id = _safe_int(row.get("ticket_id")) or "?"
    store = _clean_text(row.get("store_id")) or "?"
    status = _clean_text(row.get("status")) or "unknown"
    subject = _clean_text(row.get("subject")) or "No subject"
    created = _clean_text(row.get("created_at"))
    issue = _clean_text(row.get("issue_category"))
    parts = [f"- #{ticket_id}", f"Store {store}", status, subject]
    if created:
        parts.append(created)
    if issue:
        parts.append(issue)
    return " - ".join(parts)


def _limits_line(limits: Any, ticket_count: int, displayed_count: int) -> str:
    data = dict(limits or {})
    return (
        "Evidence limits: "
        f"saved={data.get('ticket_count', ticket_count)}, "
        f"displayed={data.get('displayed_count', displayed_count)}, "
        f"selected={data.get('selected_count', 'n/a')}, "
        f"truncated={bool(data.get('truncated'))}, "
        "source=saved_previous_result_set."
    )


def _evidence_limits(
    result_set: Mapping[str, Any],
    ticket_ids: Sequence[int],
    displayed_ids: Sequence[int],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    counts = result_set.get("counts") if isinstance(result_set.get("counts"), Mapping) else {}
    return {
        "ticket_count": len(ticket_ids),
        "displayed_count": len(displayed_ids),
        "stored_row_count": len(rows),
        "truncated": bool((counts or {}).get("truncated_ticket_ids")),
        "memory_id": _clean_text(result_set.get("memory_id"))[:80],
        "trace_id": _clean_text(result_set.get("trace_id"))[:80],
    }


def _ids_line(values: Any) -> str:
    ids = _unique_ints(values)
    return ", ".join(f"#{item}" for item in ids) if ids else "none"


def _normalize_status(value: Any) -> str:
    text = _clean_text(value).casefold()
    return _STATUS_ALIASES.get(text, text)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _clean_string_list(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [_clean_text(item) for item in raw if _clean_text(item)]
    if isinstance(raw, str):
        return [_clean_text(item) for item in re.split(r"[,;/]", raw) if _clean_text(item)]
    return []


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _unique_ints(values: Any) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    iterable = values if isinstance(values, (list, tuple, set, dict_keys_compat())) else []
    for value in iterable:
        item = _safe_int(value)
        if item is not None and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def dict_keys_compat():
    return type({}.keys())


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out
