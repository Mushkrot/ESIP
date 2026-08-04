"""Short-lived structured result-set memory for Universal Chat dispatcher follow-ups."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.services import firestore

COLLECTION = "universal_chat_result_sets"
DEFAULT_TTL_DAYS = 7
MAX_RESULT_SETS_PER_CONVERSATION = 8
MAX_STORED_TICKET_IDS = 100
MAX_DISPLAYED_TICKET_IDS = 30
MAX_CRITERIA = 12
MAX_STORES = 30
MAX_TICKET_ROWS = 100
MAX_STRING_CHARS = 180
MAX_LOOKUP_IDS = 75


@dataclass(frozen=True)
class ResultSetComparisonRequest:
    is_comparison: bool
    ticket_ids: tuple[int, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BulkTicketLookupRequest:
    is_lookup: bool
    ticket_ids: tuple[int, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class FactCheckPriorAnswerRequest:
    is_fact_check: bool
    ticket_ids: tuple[int, ...]
    reason_codes: tuple[str, ...]


def build_result_set_payload(
    *,
    conversation_id: str,
    turn_id: str,
    trace_id: str,
    source_family: str,
    requested_store_ids: list[str] | tuple[str, ...] = (),
    criteria: list[str] | tuple[str, ...] = (),
    ticket_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    ticket_ids: list[int] | tuple[int, ...] | None = None,
    displayed_ticket_ids: list[int] | tuple[int, ...] | None = None,
    query_fingerprint: str = "",
    freshness: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict:
    """Build the compact stored result-set payload.

    This function intentionally accepts structured rows and metadata only. It
    does not accept raw messages, raw assistant answers, or raw full text.
    """
    safe_conversation_id = _safe_doc_id(conversation_id)
    if safe_conversation_id == "unknown":
        raise ValueError("conversation_id is required")
    safe_turn_id = _safe_doc_id(turn_id)
    safe_trace_id = _safe_doc_id(trace_id)
    now = _ensure_aware(now)
    expires_at = now + timedelta(days=max(1, min(int(ttl_days or DEFAULT_TTL_DAYS), 14)))

    compact_rows = [_compact_ticket_row(row) for row in ticket_rows[:MAX_TICKET_ROWS]]
    compact_rows = [row for row in compact_rows if row.get("ticket_id")]
    all_ids = _unique_ints(ticket_ids if ticket_ids is not None else [row["ticket_id"] for row in compact_rows])
    displayed_ids = _unique_ints(displayed_ticket_ids if displayed_ticket_ids is not None else all_ids[:MAX_DISPLAYED_TICKET_IDS])
    all_ids = all_ids[:MAX_STORED_TICKET_IDS]
    displayed_ids = [ticket_id for ticket_id in displayed_ids[:MAX_DISPLAYED_TICKET_IDS] if ticket_id in set(all_ids)]
    rows_by_id = {int(row["ticket_id"]): row for row in compact_rows}
    compact_rows = [rows_by_id[ticket_id] for ticket_id in all_ids if ticket_id in rows_by_id][:MAX_TICKET_ROWS]

    raw_payload = {
        "memory_id": _memory_id(safe_conversation_id, safe_turn_id, safe_trace_id, all_ids),
        "conversation_id": safe_conversation_id,
        "turn_id": safe_turn_id,
        "trace_id": safe_trace_id,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "source_family": _clean_string(source_family, 40) or "tickets",
        "requested_store_ids": _clean_string_list(requested_store_ids, MAX_STORES, 12),
        "criteria": _clean_string_list(criteria, MAX_CRITERIA, 80),
        "ticket_ids": all_ids,
        "displayed_ticket_ids": displayed_ids,
        "tickets": compact_rows,
        "counts": {
            "ticket_ids": len(all_ids),
            "displayed_ticket_ids": len(displayed_ids),
            "ticket_rows": len(compact_rows),
            "truncated_ticket_ids": max(0, len(_unique_ints(ticket_ids or [])) - len(all_ids)) if ticket_ids else 0,
        },
        "query_fingerprint": _clean_string(query_fingerprint, 120) or _fingerprint({
            "source_family": source_family,
            "requested_store_ids": list(requested_store_ids),
            "criteria": list(criteria),
            "ticket_ids": all_ids,
        }),
        "freshness": _compact_mapping(freshness or {}, max_keys=12),
    }
    return _redact(raw_payload)


def save_result_set(payload: Mapping[str, Any]) -> dict:
    """Persist a result-set payload in a bounded per-conversation document."""
    conversation_id = str((payload or {}).get("conversation_id") or "").strip()
    safe_conversation_id = _safe_doc_id(conversation_id)
    if safe_conversation_id == "unknown":
        raise ValueError("conversation_id is required")
    cleaned = _redact(dict(payload))
    now = datetime.now(timezone.utc)
    ref = firestore.get_db().collection(COLLECTION).document(safe_conversation_id)
    existing_doc = ref.get()
    existing = existing_doc.to_dict() if existing_doc.exists else {}
    existing_sets = (existing or {}).get("sets") if isinstance(existing, dict) else []
    kept = _unexpired_sets(existing_sets if isinstance(existing_sets, list) else [], now=now)
    memory_id = str(cleaned.get("memory_id") or "")
    kept = [item for item in kept if str(item.get("memory_id") or "") != memory_id]
    sets = [cleaned, *kept][:MAX_RESULT_SETS_PER_CONVERSATION]
    ref.set(
        {
            "conversation_id": safe_conversation_id,
            "updated_at": now.isoformat(),
            "expires_at": max(str(item.get("expires_at") or "") for item in sets if isinstance(item, dict)),
            "sets": sets,
        },
        merge=True,
    )
    return {"status": "saved", "conversation_id": safe_conversation_id, "memory_id": memory_id, "sets": len(sets)}


def load_latest_result_set(conversation_id: str, *, now: datetime | None = None) -> dict | None:
    safe_conversation_id = _safe_doc_id(conversation_id)
    if safe_conversation_id == "unknown":
        return None
    doc = firestore.get_db().collection(COLLECTION).document(safe_conversation_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return select_latest_result_set(data, now=now)


def select_latest_result_set(document: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict | None:
    now = _ensure_aware(now)
    sets = (document or {}).get("sets") if isinstance(document, Mapping) else []
    candidates = _unexpired_sets(sets if isinstance(sets, list) else [], now=now)
    if not candidates:
        return None
    candidates.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return candidates[0]


def detect_result_set_comparison_request(text: str) -> ResultSetComparisonRequest:
    ticket_ids = extract_ticket_ids(text, cap=MAX_LOOKUP_IDS)
    lowered = (text or "").casefold()
    reasons: list[str] = []
    if ticket_ids:
        reasons.append("explicit_ticket_ids")
    if len(ticket_ids) >= 2:
        reasons.append("bulk_ticket_ids")
    prior_terms = (
        "previous",
        "prior",
        "your list",
        "your previous",
        "last answer",
        "compare",
        "comparison",
        "missed",
        "omitted",
        "сравн",
        "предыдущ",
        "прошл",
        "спис",
    )
    if any(term in lowered for term in prior_terms):
        reasons.append("prior_result_reference")
    is_comparison = "bulk_ticket_ids" in reasons and "prior_result_reference" in reasons
    return ResultSetComparisonRequest(
        is_comparison=is_comparison,
        ticket_ids=tuple(ticket_ids),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def detect_bulk_ticket_lookup_request(
    text: str,
    *,
    previous_assistant_text: str = "",
    semantic_frame: Mapping[str, Any] | None = None,
) -> BulkTicketLookupRequest:
    ticket_ids = extract_ticket_ids(text, cap=MAX_LOOKUP_IDS)
    lowered = (text or "").casefold()
    reasons: list[str] = []
    if ticket_ids:
        reasons.append("explicit_ticket_ids")
    if len(ticket_ids) >= 2:
        reasons.append("bulk_ticket_ids")
    if previous_assistant_text:
        reasons.append("has_previous_assistant")
    target_context = str((semantic_frame or {}).get("target_context") or "").casefold()
    if target_context in {"prior_turn", "conversation"}:
        reasons.append("prior_context")
    if any(
        term in lowered
        for term in (
            "previous",
            "prior",
            "last answer",
            "your list",
            "your previous",
            "these",
            "those",
            "details",
            "lookup",
            "check",
            "предыдущ",
            "прошл",
            "эти",
            "проверь",
        )
    ):
        reasons.append("prior_context")
    if _looks_like_bare_ticket_id_followup(text):
        reasons.append("bare_ticket_ids")
    comparison = detect_result_set_comparison_request(text)
    is_lookup = (
        "bulk_ticket_ids" in reasons
        and "has_previous_assistant" in reasons
        and ("prior_context" in reasons or "bare_ticket_ids" in reasons)
        and not comparison.is_comparison
    )
    return BulkTicketLookupRequest(
        is_lookup=is_lookup,
        ticket_ids=tuple(ticket_ids),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def detect_fact_check_prior_answer_request(
    text: str,
    *,
    previous_assistant_text: str = "",
    semantic_frame: Mapping[str, Any] | None = None,
) -> FactCheckPriorAnswerRequest:
    ticket_ids = extract_ticket_ids(text, cap=MAX_LOOKUP_IDS)
    lowered = (text or "").casefold()
    reasons: list[str] = []
    if ticket_ids:
        reasons.append("explicit_ticket_ids")
    if previous_assistant_text:
        reasons.append("has_previous_assistant")
    target_context = str((semantic_frame or {}).get("target_context") or "").casefold()
    if target_context in {"prior_turn", "conversation"}:
        reasons.append("prior_context")
    semantic_intent = str((semantic_frame or {}).get("intent") or "").casefold()
    if semantic_intent in {"event_fact_check", "fact_check_prior_answer"}:
        reasons.append("semantic_fact_check")
    if any(
        term in lowered
        for term in (
            "where did you",
            "prove",
            "confirm",
            "verify",
            "source",
            "evidence",
            "подтверд",
            "докажи",
            "где ты",
            "источник",
        )
    ):
        reasons.append("challenge_fact_check")
    is_fact_check = bool(ticket_ids) and (
        "challenge_fact_check" in reasons or "semantic_fact_check" in reasons
    )
    return FactCheckPriorAnswerRequest(
        is_fact_check=is_fact_check,
        ticket_ids=tuple(ticket_ids),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def extract_ticket_ids(text: str, *, cap: int = MAX_LOOKUP_IDS) -> list[int]:
    values: list[int] = []
    for match in re.findall(r"(?<!\d)(\d{5,10})(?!\d)", text or ""):
        try:
            values.append(int(match))
        except ValueError:
            continue
    return _unique_ints(values)[: max(1, min(int(cap or MAX_LOOKUP_IDS), MAX_LOOKUP_IDS))]


def compare_ticket_sets(previous: Mapping[str, Any], user_ticket_ids: list[int] | tuple[int, ...], lookup: Mapping[str, Any]) -> dict:
    previous_ids = _unique_ints((previous or {}).get("ticket_ids") or [])
    user_ids = _unique_ints(user_ticket_ids)
    found_ids = _unique_ints((lookup or {}).get("found_ids") or [row.get("ticket_id") for row in (lookup or {}).get("rows") or []])
    missing_ids = _unique_ints((lookup or {}).get("missing_ids") or [])
    previous_set = set(previous_ids)
    user_set = set(user_ids)
    return {
        "previous_memory_id": previous.get("memory_id"),
        "previous_trace_id": previous.get("trace_id"),
        "previous_turn_id": previous.get("turn_id"),
        "criteria": list(previous.get("criteria") or []),
        "requested_store_ids": list(previous.get("requested_store_ids") or []),
        "source_family": previous.get("source_family") or "tickets",
        "previous_ticket_ids": previous_ids,
        "user_ticket_ids": user_ids,
        "common_ids": [ticket_id for ticket_id in previous_ids if ticket_id in user_set],
        "previous_only_ids": [ticket_id for ticket_id in previous_ids if ticket_id not in user_set],
        "user_only_ids": [ticket_id for ticket_id in user_ids if ticket_id not in previous_set],
        "lookup_found_ids": found_ids,
        "missing_lookup_ids": missing_ids,
        "lookup_rows": list((lookup or {}).get("rows") or []),
        "evidence_limits": {
            "previous_result_count": len(previous_ids),
            "user_id_count": len(user_ids),
            "lookup_found_count": len(found_ids),
            "lookup_missing_count": len(missing_ids),
            "lookup_cap": (lookup or {}).get("cap") or MAX_LOOKUP_IDS,
            "lookup_truncated": bool((lookup or {}).get("truncated")),
        },
    }


def build_comparison_answer(comparison: Mapping[str, Any], *, answer_language: str = "English") -> str:
    is_ru = str(answer_language or "").casefold().startswith("ru")
    criteria = ", ".join(str(item) for item in comparison.get("criteria") or []) or ("заданные критерии" if is_ru else "the original criteria")
    stores = ", ".join(str(item) for item in comparison.get("requested_store_ids") or []) or ("not stored" if not is_ru else "не сохранены")
    common = _ids_line(comparison.get("common_ids") or [])
    previous_only = _ids_line(comparison.get("previous_only_ids") or [])
    user_only = _ids_line(comparison.get("user_only_ids") or [])
    missing = _ids_line(comparison.get("missing_lookup_ids") or [])
    user_only_rows = [
        row
        for row in comparison.get("lookup_rows") or []
        if int(row.get("ticket_id") or 0) in set(comparison.get("user_only_ids") or [])
    ][:10]

    if is_ru:
        lines = [
            "Я сравнил сохраненный предыдущий структурированный результат с текущим списком ticket IDs.",
            f"Критерии предыдущего поиска: {criteria}.",
            f"Магазины в сохраненном scope: {stores}.",
            f"- Есть в обоих списках: {common}.",
            f"- Были только в предыдущем результате: {previous_only}.",
            f"- Есть только в списке пользователя: {user_only}.",
        ]
        if missing:
            lines.append(f"- Не удалось найти в bounded lookup: {missing}.")
        if user_only_rows:
            lines.append("\nПроверенные user-only тикеты:")
            for row in user_only_rows:
                lines.append(f"- #{row.get('ticket_id')} — Store {row.get('store_id') or '?'} — {row.get('subject') or 'No subject'}")
    else:
        lines = [
            "I compared the saved previous structured result set with the ticket IDs in your message.",
            f"Original criteria: {criteria}.",
            f"Saved store scope: {stores}.",
            f"- In both lists: {common}.",
            f"- Previous-result only: {previous_only}.",
            f"- User-list only: {user_only}.",
        ]
        if missing:
            lines.append(f"- Missing from bounded lookup: {missing}.")
        if user_only_rows:
            lines.append("\nChecked user-only tickets:")
            for row in user_only_rows:
                lines.append(f"- #{row.get('ticket_id')} — Store {row.get('store_id') or '?'} — {row.get('subject') or 'No subject'}")
    limits = comparison.get("evidence_limits") or {}
    lines.append(
        "\nEvidence limits: "
        f"previous={limits.get('previous_result_count', 0)}, "
        f"user_ids={limits.get('user_id_count', 0)}, "
        f"lookup_found={limits.get('lookup_found_count', 0)}, "
        f"lookup_missing={limits.get('lookup_missing_count', 0)}, "
        f"cap={limits.get('lookup_cap', MAX_LOOKUP_IDS)}."
    )
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def build_bulk_lookup_answer(lookup: Mapping[str, Any], *, answer_language: str = "English") -> str:
    is_ru = str(answer_language or "").casefold().startswith("ru")
    requested = _unique_ints((lookup or {}).get("requested_ids") or [])
    found = _unique_ints((lookup or {}).get("found_ids") or [])
    missing = _unique_ints((lookup or {}).get("missing_ids") or [])
    rows = list((lookup or {}).get("rows") or [])[:12]

    if is_ru:
        lines = [
            "Я сделал bounded lookup по ticket IDs из вашего сообщения.",
            f"- Запрошено: {_ids_line(requested)}.",
            f"- Найдено: {_ids_line(found)}.",
            f"- Не найдено в bounded lookup: {_ids_line(missing)}.",
        ]
        if rows:
            lines.append("\nПроверенные тикеты:")
            for row in rows:
                lines.append(_ticket_lookup_row_line(row))
    else:
        lines = [
            "I ran a bounded lookup for the ticket IDs in your message.",
            f"- Requested: {_ids_line(requested)}.",
            f"- Found: {_ids_line(found)}.",
            f"- Missing from bounded lookup: {_ids_line(missing)}.",
        ]
        if rows:
            lines.append("\nChecked tickets:")
            for row in rows:
                lines.append(_ticket_lookup_row_line(row))
    if bool((lookup or {}).get("truncated")):
        lines.append(
            (
                f"\nLimit note: I checked the first {lookup.get('cap', MAX_LOOKUP_IDS)} ticket IDs only."
                if not is_ru
                else f"\nОграничение: я проверил только первые {lookup.get('cap', MAX_LOOKUP_IDS)} ticket IDs."
            )
        )
    lines.append(
        "\nEvidence limits: "
        f"requested={len(requested)}, "
        f"found={len(found)}, "
        f"missing={len(missing)}, "
        f"cap={(lookup or {}).get('cap') or MAX_LOOKUP_IDS}, "
        f"truncated={bool((lookup or {}).get('truncated'))}."
    )
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def build_fact_check_answer(lookup: Mapping[str, Any], *, answer_language: str = "English") -> str:
    is_ru = str(answer_language or "").casefold().startswith("ru")
    requested = _unique_ints((lookup or {}).get("requested_ids") or [])
    found = _unique_ints((lookup or {}).get("found_ids") or [])
    missing = _unique_ints((lookup or {}).get("missing_ids") or [])
    rows = list((lookup or {}).get("rows") or [])[:12]

    if is_ru:
        lines = [
            "Я проверил спорное утверждение через bounded lookup по ticket IDs, а не по тексту предыдущего ответа.",
            f"- Проверенные ticket IDs: {_ids_line(requested)}.",
            f"- Найдены ticket records: {_ids_line(found)}.",
            f"- Не найдены в bounded lookup: {_ids_line(missing)}.",
        ]
        if rows:
            lines.append("\nEvidence from lookup:")
            for row in rows:
                lines.append(_ticket_lookup_row_line(row))
        if not rows:
            lines.append("\nЯ не нашел ticket records в этом bounded lookup, поэтому не могу подтвердить утверждение этим путем.")
        lines.append(
            "\nВажно: предыдущий ответ ассистента использован только как контекст вопроса, не как фактическое доказательство."
        )
    else:
        lines = [
            "I checked the challenged claim through a bounded ticket-ID lookup, not through the previous assistant wording.",
            f"- Checked ticket IDs: {_ids_line(requested)}.",
            f"- Ticket records found: {_ids_line(found)}.",
            f"- Missing from bounded lookup: {_ids_line(missing)}.",
        ]
        if rows:
            lines.append("\nEvidence from lookup:")
            for row in rows:
                lines.append(_ticket_lookup_row_line(row))
        if not rows:
            lines.append("\nI did not find ticket records in this bounded lookup, so I cannot verify the claim through this path.")
        lines.append(
            "\nImportant: the previous assistant answer is used only as question context, not as factual evidence."
        )
    if bool((lookup or {}).get("truncated")):
        lines.append(
            (
                f"\nLimit note: I checked the first {lookup.get('cap', MAX_LOOKUP_IDS)} ticket IDs only."
                if not is_ru
                else f"\nОграничение: я проверил только первые {lookup.get('cap', MAX_LOOKUP_IDS)} ticket IDs."
            )
        )
    lines.append(
        "\nEvidence limits: "
        f"requested={len(requested)}, "
        f"found={len(found)}, "
        f"missing={len(missing)}, "
        f"cap={(lookup or {}).get('cap') or MAX_LOOKUP_IDS}, "
        f"truncated={bool((lookup or {}).get('truncated'))}, "
        "previous_assistant_text_used_as_evidence=false."
    )
    lines.append('SOURCES_JSON: {"sources": []}')
    return "\n".join(lines)


def summarize_payload(payload: Mapping[str, Any]) -> dict:
    return {
        "memory_id": payload.get("memory_id"),
        "trace_id": payload.get("trace_id"),
        "turn_id": payload.get("turn_id"),
        "source_family": payload.get("source_family"),
        "requested_store_ids": list(payload.get("requested_store_ids") or []),
        "criteria": list(payload.get("criteria") or []),
        "counts": dict(payload.get("counts") or {}),
        "expires_at": payload.get("expires_at"),
    }


def summarize_comparison(comparison: Mapping[str, Any]) -> dict:
    limits = comparison.get("evidence_limits") or {}
    return {
        "previous_memory_id": comparison.get("previous_memory_id"),
        "previous_trace_id": comparison.get("previous_trace_id"),
        "common_count": len(comparison.get("common_ids") or []),
        "previous_only_count": len(comparison.get("previous_only_ids") or []),
        "user_only_count": len(comparison.get("user_only_ids") or []),
        "lookup_missing_count": len(comparison.get("missing_lookup_ids") or []),
        "lookup_found_count": limits.get("lookup_found_count", 0),
        "lookup_cap": limits.get("lookup_cap", MAX_LOOKUP_IDS),
    }


def summarize_bulk_lookup(lookup: Mapping[str, Any]) -> dict:
    return {
        "requested_count": len((lookup or {}).get("requested_ids") or []),
        "found_count": len((lookup or {}).get("found_ids") or []),
        "missing_count": len((lookup or {}).get("missing_ids") or []),
        "cap": (lookup or {}).get("cap") or MAX_LOOKUP_IDS,
        "truncated": bool((lookup or {}).get("truncated")),
    }


def _unexpired_sets(values: list[Any], *, now: datetime) -> list[dict]:
    out: list[dict] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        expires_at = _parse_datetime(item.get("expires_at"))
        if expires_at and expires_at <= now:
            continue
        ticket_ids = _unique_ints(item.get("ticket_ids") or [])
        if not ticket_ids:
            continue
        clean = dict(item)
        clean["ticket_ids"] = ticket_ids[:MAX_STORED_TICKET_IDS]
        out.append(clean)
    return out


def _compact_ticket_row(row: Mapping[str, Any]) -> dict:
    raw_id = row.get("ticket_id", row.get("id"))
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        ticket_id = 0
    return {
        "ticket_id": ticket_id,
        "subject": _clean_string(row.get("subject"), MAX_STRING_CHARS),
        "store_id": _clean_string(row.get("store_id"), 20),
        "org_name": _clean_string(row.get("org_name"), MAX_STRING_CHARS),
        "brand": _clean_string(row.get("brand"), 60),
        "status": _clean_string(row.get("status"), 40),
        "issue_category": _clean_string(row.get("issue_category"), 120),
        "created_at": _clean_string(row.get("created_at"), 40),
        "semantic_match_type": _clean_string(row.get("semantic_match_type"), 60),
        "matched_concepts": _clean_string_list(row.get("matched_concepts") or [], 8, 80),
        "missing_required_concepts": _clean_string_list(row.get("missing_required_concepts") or [], 8, 80),
        "evidence_snippet": _clean_string(row.get("evidence_snippet") or row.get("resolution"), 260),
    }


def _compact_mapping(value: Mapping[str, Any], *, max_keys: int) -> dict:
    out: dict[str, Any] = {}
    for key, raw in list(value.items())[:max_keys]:
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            out[str(key)[:60]] = _clean_string(raw, MAX_STRING_CHARS) if isinstance(raw, str) else raw
        elif isinstance(raw, list):
            out[str(key)[:60]] = _clean_string_list(raw, 12, 80)
        elif isinstance(raw, Mapping):
            out[str(key)[:60]] = _compact_mapping(raw, max_keys=8)
    return out


def _redact(payload: dict) -> dict:
    redactor = getattr(firestore, "_redact_trace_value", None)
    return redactor(payload) if callable(redactor) else payload


def _safe_doc_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return safe[:180] or "unknown"


def _clean_string(value: object, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_chars]


def _clean_string_list(values: Any, cap: int, max_chars: int) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_string(item, max_chars)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _unique_ints(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _ensure_aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_aware(parsed)


def _memory_id(conversation_id: str, turn_id: str, trace_id: str, ticket_ids: list[int]) -> str:
    return _fingerprint({"conversation_id": conversation_id, "turn_id": turn_id, "trace_id": trace_id, "ticket_ids": ticket_ids})[:24]


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _ids_line(values: Any) -> str:
    ids = _unique_ints(values)
    return ", ".join(f"#{ticket_id}" for ticket_id in ids) if ids else "none"


def _ticket_lookup_row_line(row: Mapping[str, Any]) -> str:
    created = f" created {row.get('created_at')}" if row.get("created_at") else ""
    status = f" [{row.get('status')}]" if row.get("status") else ""
    store = row.get("store_id") or "?"
    subject = row.get("subject") or "No subject"
    issue = f" - {row.get('issue_category')}" if row.get("issue_category") else ""
    return f"- #{row.get('ticket_id')} - Store {store}{status}{created}{issue} - {subject}"


def _looks_like_bare_ticket_id_followup(text: str) -> bool:
    if len(extract_ticket_ids(text, cap=MAX_LOOKUP_IDS)) < 2:
        return False
    without_ids = re.sub(r"(?<!\d)\d{5,10}(?!\d)", " ", text or "")
    words = re.findall(r"[a-zа-яё]+", without_ids.casefold())
    allowed = {
        "ticket",
        "tickets",
        "id",
        "ids",
        "these",
        "those",
        "check",
        "lookup",
        "look",
        "up",
        "show",
        "details",
        "тикет",
        "тикеты",
        "тикетов",
        "эти",
        "проверь",
        "посмотри",
    }
    return len(words) <= 6 and all(word in allowed for word in words)


__all__ = [
    "BulkTicketLookupRequest",
    "FactCheckPriorAnswerRequest",
    "ResultSetComparisonRequest",
    "build_bulk_lookup_answer",
    "build_comparison_answer",
    "build_fact_check_answer",
    "build_result_set_payload",
    "compare_ticket_sets",
    "detect_bulk_ticket_lookup_request",
    "detect_fact_check_prior_answer_request",
    "detect_result_set_comparison_request",
    "extract_ticket_ids",
    "load_latest_result_set",
    "save_result_set",
    "select_latest_result_set",
    "summarize_bulk_lookup",
    "summarize_comparison",
    "summarize_payload",
]
