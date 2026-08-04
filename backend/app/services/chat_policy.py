"""
Shared deterministic chat policy.

These helpers define cross-surface intent rules that must not drift between
routers. Product chat currently uses Universal Chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.reference_data.fixtures import BRAND_MARKERS


BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
KNOWN_POS_TYPES = {
    "simphony": "Simphony",
    "simphony 2": "Simphony",
    "simphony2": "Simphony",
    "micros 5.5": "Micros 5.5",
}


@dataclass(frozen=True)
class PosTypeAnalyticsRequest:
    mode: str
    pos_type: str | None = None
    pos_types: tuple[str, ...] = ()
    brand: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    status_filter: tuple[str, ...] = ()
    question: str = ""


_STORE_CONTEXT_WORD_RE = (
    r"store|stores|location|locations|restaurant|restaurants|site|sites|"
    r"ресторан\w*|магазин\w*|локаци\w*|точк\w*|"
    r"restaurante?s?|tienda?s?|ubicaci[oó]n(?:es)?|sede?s?|"
    r"magasin?s?|emplacement?s?|"
    r"restoran\w*"
)
_STORE_CONTEXT_RE = re.compile(rf"(?i)\b(?:{_STORE_CONTEXT_WORD_RE})\b")
_STORE_LIST_RE = re.compile(
    rf"(?i)\b(?:{_STORE_CONTEXT_WORD_RE})\b"
    r"\s*(?:id|ids|number|numbers|no\.?|#|номер|номера|№)?\s*[:#№-]?\s*"
    r"((?:\d{3,6}\b[\s,;/&]*(?:and|or|y|o|et|ou|и|или)?\s*){1,24})"
)
_PROBLEM_WORD_RE = re.compile(
    r"(?i)\b(?:issue|issues|problem|problems|"
    r"проблем\w*|сбо\w*|неисправност\w*|"
    r"problema?s?|fall[ao]s?|incidencia?s?|"
    r"probl[eèé]me?s?|panne?s?|"
    r"problem\w*|sbo\w*)\b"
)


def extract_store_ids(question: str) -> list[str]:
    """Extract ordered explicit store numbers from a natural-language question."""
    q = (question or "").strip()
    if not q:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        store_id = str(value or "").strip()
        if not store_id or store_id in seen:
            return
        seen.add(store_id)
        out.append(store_id)

    for match in _STORE_LIST_RE.finditer(q):
        for store_id in re.findall(r"\b\d{3,6}\b", match.group(1) or ""):
            add(store_id)

    # Some engineer prompts use "for every store" and then paste one ID per
    # line. If a store-list marker is present, keep the whole visible list.
    if (
        not out
        and _STORE_CONTEXT_RE.search(q)
        and re.search(r"(?i)\b(?:stores|locations|every store|each store)\b", q)
    ):
        for store_id in re.findall(r"\b\d{4,5}\b", q):
            add(store_id)

    return out


def extract_store_id(question: str) -> str | None:
    """Extract the first explicit store number from a natural-language question."""
    store_ids = extract_store_ids(question)
    if not store_ids:
        return None
    return store_ids[0]


def extract_explicit_ticket_id(question: str) -> int | None:
    """Extract a ticket ID explicitly mentioned by the engineer."""
    q = (question or "").strip()
    if not q:
        return None

    # "ticket 950006", "ticket #950026", "тикет 950026", "тикет #950026"
    match = re.search(r"(?i)\b(?:ticket|тикет)\s*#?\s*(\d{4,10})\b", q)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None

    # "#950026". Four-digit bare #NNNN can be a store number, so keep this
    # broad extractor only for explicit user text handling, not UI auto-linking.
    match = re.search(r"(?<!\w)#(\d{4,10})\b", q)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def extract_lookback_days(question: str, default_days: int = 90) -> int:
    """Extract a lookback window from common English time phrases."""
    q = (question or "").lower()
    match = re.search(r"\b(?:past|last)\s+(\d+)\s*(day|days|week|weeks|month|months|year|years)\b", q)
    if match:
        n = max(1, int(match.group(1)))
        unit = match.group(2)
        if unit.startswith("day"):
            return n
        if unit.startswith("week"):
            return n * 7
        if unit.startswith("month"):
            return n * 30
        return n * 365
    if "this quarter" in q:
        return 90
    if "this month" in q:
        return 30
    if "this week" in q:
        return 7
    return default_days


def _known_brand_from_text(question: str) -> str | None:
    q = (question or "").casefold()
    for brand, markers in BRAND_MARKERS.items():
        if any(marker in q for marker in markers):
            return brand
    return None


def _pos_type_date_window(question: str) -> tuple[str | None, str | None]:
    q = (question or "").casefold()
    if not re.search(r"\b(?:last|past|this|recent)\b", q):
        return None, None
    days = extract_lookback_days(question, default_days=90)
    if "recent" in q and not re.search(r"\b(?:last|past|this)\b", q):
        days = 30
    end = datetime.now(BUSINESS_TIMEZONE).date()
    start = end - timedelta(days=max(1, days) - 1)
    return start.isoformat(), end.isoformat()


def _pos_type_status_filter(question: str) -> tuple[str, ...]:
    q = (question or "").casefold()
    if not re.search(r"\b(?:ticket|tickets)\b", q):
        return ()
    if re.search(
        r"\b(?:active|current|unresolved|unsolved)\b|"
        r"\bnot\s+(?:solved|closed)\b|"
        r"\bnot\s+solved\s*(?:/|or)\s*closed\b|"
        r"\b(?:without|excluding|exclude)\s+(?:solved|closed)\b",
        q,
    ):
        return ("new", "open", "pending", "hold")

    statuses: list[str] = []
    for label in ("new", "open", "pending", "hold", "solved", "closed"):
        if re.search(rf"\b{label}\b", q):
            statuses.append(label)
    return tuple(dict.fromkeys(statuses))


def _clean_pos_type_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" .,:;?!")
    stripped = ""
    while value != stripped:
        stripped = value
        value = re.sub(
            r"(?i)\b(?:"
            r"or|and|of|with|to|for|where|in|on|by|at|from|over|during|compared|compare|"
            r"between|within|since|recent|last|past|this|that|the|is|are|was|were|has|have|had|"
            r"will|would|can|could|should|show|find|identify|summarize|group|stores|store|"
            r"ticket|tickets|problem|problems|issue|issues"
            r")$",
            "",
            value,
        ).strip()
        value = value.strip(" .,:;?!")
    return value


def _canonical_pos_type_value(value: str) -> str:
    cleaned = _clean_pos_type_value(value)
    normalized = re.sub(r"[\s_+-]+", " ", cleaned.casefold()).strip()
    compact = re.sub(r"[\s_+-]+", "", cleaned.casefold())
    if normalized in KNOWN_POS_TYPES:
        return KNOWN_POS_TYPES[normalized]
    if compact in KNOWN_POS_TYPES:
        return KNOWN_POS_TYPES[compact]
    for marker, label in KNOWN_POS_TYPES.items():
        if re.search(rf"\b{re.escape(marker)}\b", cleaned.casefold()):
            return label
    return cleaned


def _known_pos_type_from_text(question: str) -> str | None:
    q = (question or "").casefold()
    for marker, label in KNOWN_POS_TYPES.items():
        if re.search(rf"\b{re.escape(marker)}\b", q):
            return label
    return None


def _has_pos_profile_analytics_shape(question: str) -> bool:
    """Require a cohort/analytics task, not merely POS implementation context."""
    q = (question or "").casefold()
    return bool(
        re.search(
            r"\b(?:which|show|list|find|identify|summari[sz]e|summary|group|compare|"
            r"rank|count|top|most|least|repeated|breakdown|distribution)\b",
            q,
        )
        or re.search(
            r"\b(?:all|open|pending|closed|unresolved|active)\s+(?:store|stores|ticket|tickets)\b",
            q,
        )
        or re.search(r"\b(?:store|stores|ticket|tickets)\b.*\bnot\s+(?:closed|solved)\b", q)
    )


def extract_pos_type_profile_analytics_request(question: str) -> PosTypeAnalyticsRequest | None:
    """Detect analytics asks that require Zendesk organization profile POS Type filtering."""
    raw = question or ""
    q = raw.casefold()
    if re.search(r"\b(?:mention|mentions|mentioned|word|text|phrase)\b", q) and "where pos type" not in q:
        return None
    start_date, end_date = _pos_type_date_window(raw)
    status_filter = _pos_type_status_filter(raw)
    brand = _known_brand_from_text(raw)
    has_explicit_pos_type = "pos type" in q or "pos_type" in q
    if not has_explicit_pos_type and not any(
        token in q
        for token in ("ticket", "tickets", "store", "stores", "problem", "problems", "issue", "issues", "compare", "summary", "summarize")
    ):
        return None
    comparison = re.search(
        r"(?is)\bpos[_\s]*type\s*(?:=|is|:)?\s*([a-z0-9][a-z0-9 ._+-]*?)\s*[,:-]?\s*"
        r"(?:vs\.?|versus|compared\s+to|compare\s+to|to)\s+"
        r"(?:pos[_\s]*type\s*)?(?:=|is|:)?\s*([a-z0-9][a-z0-9 ._+-]*?)"
        r"(?=\s+(?:over|for|in|during|last|past|this)\b|[?!,]|$)",
        raw,
    ) if has_explicit_pos_type else None
    if comparison:
        values = tuple(
            value
            for value in (
                _clean_pos_type_value(comparison.group(1)),
                _clean_pos_type_value(comparison.group(2)),
            )
            if value
        )
        values = tuple(_canonical_pos_type_value(value) for value in values)
        if len(values) >= 2:
            return PosTypeAnalyticsRequest(
                mode="comparison",
                pos_types=values,
                brand=brand,
                start_date=start_date,
                end_date=end_date,
                status_filter=status_filter,
                question=raw,
            )

    if not _has_pos_profile_analytics_shape(raw):
        return None

    if not has_explicit_pos_type:
        pos_type = _known_pos_type_from_text(raw)
        if pos_type:
            return PosTypeAnalyticsRequest(
                mode="single",
                pos_type=pos_type,
                brand=brand,
                start_date=start_date,
                end_date=end_date,
                status_filter=status_filter,
                question=raw,
            )
        return None

    match = re.search(
        r"(?is)\bpos[_\s]*type\s*(?:=|is|:)?\s*([a-z0-9][a-z0-9 ._+-]*?)"
        r"(?=\s+(?:and|over|for|where|with|identify|group|summarize|"
        r"compare|compared|to|store|stores|ticket|tickets|problem|problems|issue|issues|"
        r"have|has|had|show|find|which|what|how|most|least|recent|during|within|then|"
        r"after|before|by|from|into|as|is|are|this|that|it|all|only|or|"
        r"last|past|this|recent)\b|[?!,]|$)",
        raw,
    )
    if not match:
        return None
    pos_type = _canonical_pos_type_value(match.group(1))
    if not pos_type:
        return None
    return PosTypeAnalyticsRequest(
        mode="single",
        pos_type=pos_type,
        brand=brand,
        start_date=start_date,
        end_date=end_date,
        status_filter=status_filter,
        question=raw,
    )


def question_requests_issue_breakdown(question: str) -> bool:
    """True when the user asks for top/common issue themes."""
    q = (question or "").lower()
    has_problem_word = bool(_PROBLEM_WORD_RE.search(q))
    if not has_problem_word:
        return False
    strong = (
        "top issue",
        "top issues",
        "main issues",
        "common issues",
        "issue types",
        "issues by type",
        "issue breakdown",
        "основные проблем",
        "главные проблем",
        "частые проблем",
        "тип проблем",
        "типы проблем",
        "категории проблем",
    )
    if any(token in q for token in strong):
        return True
    if "issue_category" in q and ("not" in q or "without" in q):
        return True
    if "ticket description" in q or "contents of the tickets" in q or "by looking at the contents" in q:
        return True
    return bool(_STORE_CONTEXT_RE.search(q) and has_problem_word)


def is_store_issue_breakdown_question(question: str) -> bool:
    """True for explicit store-level issue/problem breakdown questions."""
    return bool(extract_store_id(question) and question_requests_issue_breakdown(question))


def looks_like_drive_tabular_lookup(question: str) -> bool:
    """True for spreadsheet/table lookup asks such as escalation matrix questions."""
    q = (question or "").lower()
    if not q.strip():
        return False
    if "drive.google.com" in q or "docs.google.com" in q:
        return True
    keywords = (
        "who do i escalate",
        "escalate",
        "escalation",
        "team responsible",
        "responsible team",
        "owner",
        "who owns",
        "who handles",
        "who is responsible",
        "contact for",
        "teamviewer",
        "access code",
        "customer name",
        "postal code",
        "contract no",
        "contract number",
        "store no",
        "store number",
        "which row",
        "sheet",
        "escalation matrix",
        "в какой строк",
        "какая строк",
        "строке таблиц",
        "строка таблиц",
        "лист таблиц",
        "нашел ответ",
    )
    if any(token in q for token in keywords):
        return True
    return "vendor" in q and any(token in q for token in ("contact", "phone", "email"))


def semantic_frame_requests_ticket_examples(semantic_frame: object) -> bool:
    """True when Phase 0 explicitly interpreted the ask as scenario ticket examples."""
    if not isinstance(semantic_frame, dict):
        return False
    intent = str(semantic_frame.get("intent") or "").strip().lower()
    if intent != "ticket_retrieval":
        return False
    retrieval = semantic_frame.get("retrieval")
    if not isinstance(retrieval, dict):
        return False
    output = str(retrieval.get("output") or "").strip().lower()
    if output and output != "ticket_examples":
        return False
    searchable_fields = (
        retrieval.get("search_goal"),
        retrieval.get("required_concepts"),
        retrieval.get("optional_concepts"),
        retrieval.get("vendors"),
        retrieval.get("devices"),
        retrieval.get("actions"),
    )
    return any(bool(value) for value in searchable_fields)


def should_force_drive_tabular_lookup(
    routing_question: str,
    latest_question: str = "",
    *,
    semantic_frame: object | None = None,
) -> bool:
    """True when table/Drive lookup should override normal tool routing."""
    if semantic_frame_requests_ticket_examples(semantic_frame):
        return False
    return looks_like_drive_tabular_lookup(routing_question) or looks_like_drive_tabular_lookup(latest_question)


def is_row_provenance_question(question: str) -> bool:
    """True when the user asks where in a spreadsheet/table the prior answer came from."""
    q = (question or "").lower()
    return any(
        token in q
        for token in (
            "which row",
            "what row",
            "which sheet",
            "where in the table",
            "в какой строк",
            "какая строк",
            "в каком лист",
            "где в таблиц",
            "строке таблиц",
        )
    )


def explicitly_requests_past_tickets(question: str) -> bool:
    """True when ticket cards/search results are the user's actual ask."""
    q = (question or "").lower()
    keywords = (
        "similar ticket",
        "similar tickets",
        "similar case",
        "similar cases",
        "past ticket",
        "past tickets",
        "previous ticket",
        "previous tickets",
        "find ticket",
        "find tickets",
        "show ticket",
        "show tickets",
        "has this happened before",
        "how was this resolved",
        "how was it resolved",
    )
    return any(token in q for token in keywords)


def explicit_ticket_request_needs_tools(question: str) -> bool:
    """
    True when an explicit ticket ID is starting context, not the whole answer.

    Example: "analyze ticket 950009 and similar cases from this restaurant" must
    still run ticket search / analytics. Only narrow asks like "summarize ticket
    950032" should stay direct-ticket-only.
    """
    q = (question or "").lower()
    if not extract_explicit_ticket_id(q):
        return False
    broad_markers = (
        "similar",
        "same restaurant",
        "same store",
        "this restaurant",
        "this store",
        "other restaurant",
        "other store",
        "all tickets",
        "past ticket",
        "previous ticket",
        "last 6 months",
        "last six months",
        "last year",
        "past year",
        "trend",
        "trends",
        "pattern",
        "patterns",
        "compare",
        "comparison",
        "похож",
        "такого же",
        "такого рода",
        "этого же рестора",
        "этом рестора",
        "других рестора",
        "все тикет",
        "последн",
        "пол-года",
        "пол года",
        "год",
        "тенденц",
        "сравн",
    )
    return any(marker in q for marker in broad_markers)


def should_suppress_similar_tickets(question: str, *, semantic_frame: object | None = None) -> bool:
    """
    Suppress automatic similar-ticket retrieval for authoritative lookup asks.

    Spreadsheet/escalation lookups should answer from the matrix first. Similar
    tickets are noisy unless the engineer explicitly asks for them.
    """
    if semantic_frame_requests_ticket_examples(semantic_frame):
        return False
    return looks_like_drive_tabular_lookup(question) and not explicitly_requests_past_tickets(question)


def references_same_store_context(question: str) -> bool:
    """True for current/same store references, including hospitality synonyms and common typos."""
    q = (question or "").lower()
    markers = (
        "same restaurant",
        "same store",
        "this restaurant",
        "this store",
        "current restaurant",
        "current store",
        "именно в этом",
        "этого же рест",
        "этом рест",
        "этот рест",
        "текущий рест",
        "тот же рест",
        "том же рест",
        "этого же магазин",
        "этом магазин",
        "этот магазин",
        "тот же магазин",
    )
    return any(marker in q for marker in markers)


def is_prior_answer_challenge(question: str) -> bool:
    """
    True when the user challenges a previous answer or asks for evidence.

    These turns must re-check the current ticket/store evidence instead of
    answering from previous assistant text alone.
    """
    q = (question or "").lower()
    markers = (
        "where did you find",
        "what source",
        "source for",
        "are you sure",
        "verify",
        "confirm",
        "prove",
        "not related",
        "none of them",
        "irrelevant",
        "wrong answer",
        "wrong tickets",
        "где ты наш",
        "откуда",
        "источник",
        "на основании",
        "ты уверен",
        "уверн",
        "проверь",
        "подтвер",
        "да ладно",
        "что за бред",
        "не относится",
        "не относятся",
        "не связано",
        "не связаны",
        "не подходит",
        "не подходят",
        "не релевант",
        "неверный ответ",
        "не те тикеты",
    )
    return any(marker in q for marker in markers)


def is_event_timing_question(question: str) -> bool:
    """True when the user asks when a contextual event happened."""
    q = (question or "").lower()
    timing_markers = ("when", "what date", "date of", "когда", "какая дата", "в какую дату")
    event_markers = ("happened", "occurred", "event", "incident", "произош", "случил", "инцидент")
    return any(marker in q for marker in timing_markers) and any(marker in q for marker in event_markers)


def is_contextual_resolution_followup(question: str) -> bool:
    """
    True for short fix/prevention follow-ups that depend on the open ticket.

    Examples: "how can this be avoided", "how do we prevent this from happening
    again", "what should we do to fix it". This deliberately looks for a
    contextual pronoun plus a resolution/prevention intent, not for product or
    incident-specific keywords.
    """
    q = (question or "").lower().strip()
    if not q:
        return False

    current_ref = any(
        marker in q
        for marker in (
            "this",
            "it",
            "this issue",
            "this problem",
            "current issue",
            "current problem",
            "этого",
            "это",
            "эту",
            "этой",
            "этот",
            "его",
            "ее",
            "проблем",
        )
    )
    if not current_ref:
        return False

    resolution_intent = any(
        marker in q
        for marker in (
            "avoid",
            "avoided",
            "prevent",
            "prevented",
            "prevention",
            "stop this",
            "fix this",
            "fix it",
            "resolve this",
            "resolve it",
            "correct this",
            "correct it",
            "happening again",
            "recurring",
            "избежать",
            "предотврат",
            "не допустить",
            "исправить",
            "решить",
            "устранить",
            "повтор",
        )
    )
    return resolution_intent


def is_contextual_category_frequency_followup(question: str) -> bool:
    """
    True when the user asks for count/frequency of the open ticket's category.

    Examples: "How many times has this category of issue been reported?",
    "how often does this type happen?", "сколько раз такая категория".
    This detects the generic shape of the follow-up, not a concrete category
    name, so the router can use the open ticket's ISSUE CATEGORY as evidence.
    """
    q = (question or "").lower().strip()
    if not q:
        return False

    current_category_ref = any(
        marker in q
        for marker in (
            "this category",
            "current category",
            "same category",
            "this issue category",
            "this type",
            "same type",
            "this kind",
            "same kind",
            "category of issue",
            "type of issue",
            "kind of issue",
            "такая категория",
            "эта категория",
            "текущая категория",
            "такая же категория",
            "этот тип",
            "такой тип",
            "такого типа",
            "такого рода",
            "эта проблема",
            "такая проблема",
        )
    )
    if not current_category_ref:
        return False

    frequency_intent = any(
        marker in q
        for marker in (
            "how many",
            "how often",
            "how frequently",
            "times",
            "reported",
            "report",
            "count",
            "frequency",
            "trend",
            "сколько",
            "как часто",
            "частота",
            "раз",
            "сообщал",
            "репорт",
            "тикет",
            "количество",
        )
    )
    return frequency_intent


def needs_scoped_store_evidence(question: str) -> bool:
    """
    True when the answer needs current-store evidence boundaries.

    This is intentionally generic: it does not look for a specific incident
    word. It detects the kind of turn where same-store vs other-store scope
    matters and lets the model interpret the returned ticket rows.
    """
    q = (question or "").lower()
    scope_markers = (
        "similar",
        "same restaurant",
        "same store",
        "this restaurant",
        "this store",
        "other restaurant",
        "other store",
        "before",
        "after",
        "last year",
        "past year",
        "trend",
        "compare",
        "comparison",
        "похож",
        "такого же",
        "такого рода",
        "этого же рест",
        "этом рест",
        "других рест",
        "до ",
        "после",
        "последн",
        "год",
        "тенденц",
        "сравн",
    )
    return (
        references_same_store_context(q)
        or is_prior_answer_challenge(q)
        or is_event_timing_question(q)
        or is_contextual_category_frequency_followup(q)
        or any(marker in q for marker in scope_markers)
    )


def requests_other_restaurant_comparison(question: str) -> bool:
    """True when current-store evidence must be separated from other-store examples."""
    q = (question or "").lower()
    markers = (
        "other restaurant",
        "other restaurants",
        "other store",
        "other stores",
        "different restaurant",
        "different restaurants",
        "compare with restaurants",
        "compare to restaurants",
        "других рестора",
        "другие рестора",
        "остальных рестора",
        "других магазин",
        "другие магазин",
        "сравни с",
        "сравнить с",
    )
    return any(marker in q for marker in markers)


def should_use_open_ticket_context(question: str) -> bool:
    """
    Decide whether the left-panel ticket should be injected into Universal Chat.

    The open ticket is useful for contextual follow-ups, but harmful when the
    user asks an independent store/analytics/KB question. Keep it "soft":
    only inject when the wording points at the current ticket/issue.
    """
    q = (question or "").lower().strip()
    if not q:
        return False

    if extract_explicit_ticket_id(q):
        return False
    if extract_store_id(q):
        return False
    if looks_like_drive_tabular_lookup(q):
        return False
    if (
        is_prior_answer_challenge(q)
        or is_event_timing_question(q)
        or is_contextual_resolution_followup(q)
        or is_contextual_category_frequency_followup(q)
    ):
        return True
    if references_same_store_context(q):
        return True

    explicit_current_markers = (
        "this ticket",
        "current ticket",
        "opened ticket",
        "open ticket",
        "ticket on the left",
        "left panel",
        "this restaurant",
        "current restaurant",
        "this store",
        "current store",
        "same restaurant",
        "same store",
        "this issue",
        "current issue",
        "этот тикет",
        "текущий тикет",
        "открытый тикет",
        "тикет слева",
        "этот ресторан",
        "этом ресторане",
        "текущий ресторан",
        "этот магазин",
        "этом магазине",
        "тот же ресторан",
        "том же ресторане",
        "эта проблема",
        "текущая проблема",
    )
    if any(marker in q for marker in explicit_current_markers):
        return True

    ticket_detail_intents = (
        "summarize",
        "summary",
        "brief",
        "details",
        "detail",
        "what happened",
        "what is happening",
        "resolution",
        "root cause",
        "next step",
        "next steps",
        "who is working",
        "что случилось",
        "что произошло",
        "резюме",
        "кратко",
        "детали",
        "решение",
        "причина",
    )
    return any(token in q for token in ticket_detail_intents)
