"""Deterministic counting for semantic complaint labels in chat follow-ups."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SemanticLabelRequest:
    labels: list[str]
    source: str


@dataclass(frozen=True)
class LabelCount:
    label: str
    ticket_ids: list[int]

    @property
    def count(self) -> int:
        return len(self.ticket_ids)


_LISTED_LABEL_MARKERS = (
    "listed categor",
    "mentioned categor",
    "each categor",
    "each listed",
    " перечислен",
    " каждой из",
    " каждой категории",
    " перечисленных катег",
    " перечисленным катег",
    " перечисленных типов",
)

_COUNT_MARKERS = (
    "how many",
    "count",
    "number of",
    "tickets",
    "ticket",
    "сколько",
    "количеств",
    "тикет",
)

_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "by",
    "for",
    "from",
    "in",
    "of",
    "or",
    "the",
    "to",
    "with",
    "issue",
    "issues",
    "problem",
    "problems",
    "complaint",
    "complaints",
    "ticket",
    "tickets",
    "category",
    "categories",
}


def build_label_request(
    *,
    latest_question: str,
    routing_question: str,
    previous_assistant_text: str,
) -> SemanticLabelRequest | None:
    """Return semantic labels when the user asks to count listed categories."""
    combined_question = f"{latest_question}\n{routing_question}".strip()
    if not _looks_like_label_count_question(combined_question):
        return None

    labels = _extract_labels_from_question(latest_question) or _extract_labels_from_question(routing_question)
    source = "current_question"
    if not labels:
        labels = _extract_labels_from_previous_answer(previous_assistant_text)
        source = "previous_answer"
    labels = _dedupe_labels(labels)
    if len(labels) < 2:
        return None
    return SemanticLabelRequest(labels=labels[:10], source=source)


def build_label_request_from_label_text(value: str, *, source: str) -> SemanticLabelRequest | None:
    """Build a request from a model-provided comma/and/or label phrase."""
    labels = _dedupe_labels([label for label in _split_label_phrase(value) if _looks_like_label(label)])
    if len(labels) < 2:
        return None
    return SemanticLabelRequest(labels=labels[:10], source=source)


def extract_labels_from_answer(text: str) -> list[str]:
    """Extract descriptive labels introduced by a visible assistant answer."""
    return _dedupe_labels(_extract_labels_from_previous_answer(text))[:10]


def looks_like_label_count_question(text: str) -> bool:
    return _looks_like_label_count_question(text)


def count_labels(labels: list[str], rows: list[dict[str, Any]]) -> list[LabelCount]:
    """Count possibly-overlapping semantic labels across ticket rows."""
    results: list[LabelCount] = []
    for label in labels:
        ids: list[int] = []
        for row in rows:
            ticket_id = _safe_int(row.get("id") or row.get("ticket_id"))
            if not ticket_id:
                continue
            if _label_matches_ticket(label, row):
                ids.append(ticket_id)
        results.append(LabelCount(label=label, ticket_ids=sorted(set(ids))))
    return results


def unique_ticket_ids(results: list[LabelCount]) -> list[int]:
    ids: set[int] = set()
    for result in results:
        ids.update(result.ticket_ids)
    return sorted(ids)


def build_answer(
    *,
    store_id: str,
    period_label: str,
    results: list[LabelCount],
    answer_language: str,
) -> str:
    """Build a deterministic user-visible report for label counts."""
    russian = (answer_language or "").lower().startswith("russian")
    unique_ids = unique_ticket_ids(results)

    if russian:
        lines = [
            "Я посчитал это как смысловые типы жалоб, а не как официальные Zendesk `issue_category`.",
            "Один тикет может попадать в несколько строк, поэтому суммы по строкам могут быть больше количества уникальных тикетов.",
            "",
            f"Store {store_id}, период: {period_label}. Уникальных тикетов в этих строках: **{len(unique_ids)}**.",
            "",
            "| Категория жалобы | Тикетов | Ticket IDs |",
            "|---|---:|---|",
        ]
        for result in results:
            ids = ", ".join(f"#{tid}" for tid in result.ticket_ids) if result.ticket_ids else "none"
            lines.append(f"| {result.label} | {result.count} | {ids} |")
        lines.extend([
            "",
            "Метод: проверены subject, issue category, resolution и комментарии тикетов за указанный период.",
            "",
            'SOURCES_JSON: {"sources": []}',
        ])
        return "\n".join(lines)

    lines = [
        "I counted these as semantic complaint labels, not official Zendesk `issue_category` values.",
        "One ticket can appear in multiple rows, so row totals can exceed the unique ticket count.",
        "",
        f"Store {store_id}, period: {period_label}. Unique tickets across these rows: **{len(unique_ids)}**.",
        "",
        "| Complaint label | Tickets | Ticket IDs |",
        "|---|---:|---|",
    ]
    for result in results:
        ids = ", ".join(f"#{tid}" for tid in result.ticket_ids) if result.ticket_ids else "none"
        lines.append(f"| {result.label} | {result.count} | {ids} |")
    lines.extend([
        "",
        "Method: checked ticket subjects, issue categories, resolutions, and comments for the requested period.",
        "",
        'SOURCES_JSON: {"sources": []}',
    ])
    return "\n".join(lines)


def _looks_like_label_count_question(text: str) -> bool:
    q_raw = str(text or "").lower()
    q = _compact_lower(text)
    if not q_raw.strip():
        return False
    has_count = any(marker in q_raw or marker in q for marker in _COUNT_MARKERS)
    has_listed_ref = any(marker in q_raw or marker in q for marker in _LISTED_LABEL_MARKERS)
    has_inline_labels = len(_extract_labels_from_question(text)) >= 2
    return has_count and (has_listed_ref or has_inline_labels)


def _extract_labels_from_question(question: str) -> list[str]:
    labels: list[str] = []
    for raw_line in (question or "").splitlines():
        line = _clean_label_line(raw_line)
        if not line:
            continue
        if _looks_like_question_sentence(line):
            continue
        if _looks_like_label(line):
            labels.append(line)
    if len(labels) >= 2:
        return labels
    return []


def _extract_labels_from_previous_answer(text: str) -> list[str]:
    if not text:
        return []
    compact = re.sub(r"\s+", " ", text).strip()
    candidates: list[str] = []
    patterns = (
        r"\b(?:issues|complaints|problems)\s+(?:including|included)\s+(.+?)(?:\.|$)",
        r"\b(?:including|included)\s+(.+?)(?:\.|$)",
        r"\b(?:specifically|especially)\s+(.+?)(?:\.|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, flags=re.IGNORECASE)
        if match:
            candidates = _split_label_phrase(match.group(1))
            labels = [label for label in candidates if _looks_like_label(label)]
            if labels:
                return labels
    if not candidates:
        candidates = _extract_labels_from_question(text)
    return [label for label in candidates if _looks_like_label(label)]


def _split_label_phrase(value: str) -> list[str]:
    text = re.sub(r"[*_`]+", "", value or "")
    text = re.sub(r"\b(?:specifically|especially|mainly)\b", ",", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .;:")
    text = re.sub(r",\s+and\s+", ", ", text, flags=re.IGNORECASE)
    parts: list[str] = []
    for part in [p.strip(" .;:") for p in text.split(",") if p.strip(" .;:")]:
        part = re.sub(r"^and\s+", "", part, flags=re.IGNORECASE).strip(" .;:")
        parts.extend(_split_compound_label(part))
    return parts


def _split_compound_label(value: str) -> list[str]:
    text = value.strip(" .;:")
    if not text:
        return []
    if re.search(r"\s+or\s+", text, flags=re.IGNORECASE):
        return [p.strip(" .;:") for p in re.split(r"\s+or\s+", text, flags=re.IGNORECASE) if p.strip(" .;:")]

    match = re.fullmatch(
        r"([a-z][a-z -]+?)\s+and\s+([a-z][a-z -]+?)\s+(connections?|issues?|outages?|failures?|wi-?fi|wifi|wireless)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        noun = match.group(3)
        return [f"{match.group(1).strip()} {noun}", f"{match.group(2).strip()} {noun}"]
    return [text]


def _dedupe_labels(labels: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        cleaned = re.sub(r"\s+", " ", label.strip(" .;:"))
        key = _compact_lower(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _clean_label_line(line: str) -> str:
    text = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+|[•]\s*)", "", line or "").strip()
    text = text.strip("`'\" ")
    return re.sub(r"\s+", " ", text).strip(" .;:")


def _looks_like_question_sentence(line: str) -> bool:
    q = line.lower()
    return "?" in line or any(
        marker in q
        for marker in (
            "how many",
            "count",
            "give me",
            "report",
            "сколько",
            "дай мне",
            "отчет",
            "категор",
        )
    )


def _looks_like_label(line: str) -> bool:
    words = re.findall(r"[a-z0-9][a-z0-9-]*", line.lower())
    if not 1 <= len(words) <= 8:
        return False
    if len(line) > 120:
        return False
    return any(
        token in line.lower()
        for token in (
            "internet",
            "wifi",
            "wi-fi",
            "network",
            "connection",
            "connectivity",
            "wan",
            "outage",
            "speed",
            "lag",
            "drop",
            "inconsistent",
            "primary",
            "backup",
        )
    )


def _label_matches_ticket(label: str, row: dict[str, Any]) -> bool:
    label_text = _compact_lower(label)
    text = _row_search_text(row)
    core_text = _row_core_text(row)

    if "wan 2" in label_text or "wan2" in label_text or "backup" in label_text:
        return _has_any(core_text, (r"\bwan\s*2\b", r"\bwan2\b", r"\bbackup connection\b", r"\bbackup internet\b")) and _has_any(
            core_text, (r"\bdown\b", r"\bfail", r"\boffline\b", r"\bnot working\b")
        )

    if "primary" in label_text and ("outage" in label_text or "down" in label_text or "internet" in label_text):
        if _has_any(core_text, (r"\bwan\s*2\b", r"\bwan2\b", r"\bbackup connection\b")) and not _has_any(
            core_text, (r"\bprimary\b", r"\bwan\s*1\b", r"\bwan1\b")
        ):
            return False
        return _has_any(
            core_text,
            (
                r"\bprimary internet down\b",
                r"\binternet no internet\b",
                r"\bno internet\b",
                r"\bprimary\b.*\bdown\b",
                r"\bwan\s*1\b.*\bdown\b",
                r"\bwan1\b.*\bdown\b",
            ),
        )

    if "lag" in label_text and _has_any(label_text, (r"\bwifi\b", r"\bwi fi\b", r"\bwireless\b")):
        return _has_any(
            text,
            (
                r"\blagging\s+(?:wifi|wi fi|wireless)\b",
                r"\b(?:wifi|wi fi|wireless)\s+(?:is\s+|keeps\s+)?(?:lagging|slow|freezing)\b",
                r"\b(?:wifi|wi fi|wireless)\s+performance\b",
            ),
        )

    if "drop" in label_text:
        return _is_connection_text(text) and _has_any(
            text,
            (
                r"\bdrop",
                r"\bdisconnect",
                r"\bcutting out\b",
                r"\bcut out\b",
                r"\bintermittent",
                r"\bdropping connection\b",
            ),
        )

    if "inconsistent" in label_text:
        return _is_connection_text(text) and _has_any(
            text,
            (
                r"\binconsistent\b",
                r"\bintermittent",
                r"\bflaky\b",
                r"\bunstable\b",
                r"\bnot stable\b",
            ),
        )

    if ("degraded" in label_text or "slow" in label_text or "speed" in label_text) and _is_connection_text(label_text):
        return _is_connection_text(text) and _has_any(
            text,
            (
                r"\bdegraded\b",
                r"\bdegradation\b",
                r"\bslow",
                r"\blag",
                r"\bspeed",
                r"\bslowness\b",
                r"\bpacket loss\b",
                r"\blatency\b",
            ),
        )

    if _has_any(label_text, (r"\bwifi\b", r"\bwi fi\b", r"\bwireless\b")):
        return _is_wifi_text(text)

    return _generic_label_match(label_text, text)


def _generic_label_match(label_text: str, row_text: str) -> bool:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", label_text)
        if len(token) > 2 and token not in _STOPWORDS
    ]
    if not tokens:
        return False
    matched = sum(1 for token in tokens if token in row_text)
    threshold = len(tokens) if len(tokens) <= 3 else max(2, round(len(tokens) * 0.65))
    return matched >= threshold


def _row_search_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("subject"),
        row.get("issue_category"),
        row.get("symptoms"),
        row.get("resolution"),
        row.get("full_text"),
        row.get("comments_text"),
    ]
    return _compact_lower(" ".join(str(part or "") for part in parts))


def _row_core_text(row: dict[str, Any]) -> str:
    parts = [row.get("subject"), row.get("issue_category")]
    return _compact_lower(" ".join(str(part or "") for part in parts))


def _compact_lower(text: str) -> str:
    value = str(text or "").lower()
    value = value.replace("_", " ").replace("-", " ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_wifi_text(text: str) -> bool:
    return _has_any(text, (r"\bwifi\b", r"\bwi fi\b", r"\bwireless\b", r"\bssid\b", r"\baccess point"))


def _is_connection_text(text: str) -> bool:
    return _has_any(
        text,
        (
            r"\binternet\b",
            r"\bnetwork\b",
            r"\bconnection\b",
            r"\bconnectivity\b",
            r"\bwifi\b",
            r"\bwi fi\b",
            r"\bwireless\b",
            r"\bwan\b",
            r"\bmodem\b",
            r"\baccess point",
            r"\bap'?s\b",
        ),
    )


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
