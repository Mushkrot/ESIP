"""Deterministic temporal resolution for Universal Chat ticket analytics."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


BUSINESS_TZ = ZoneInfo("America/New_York")

MONTHS = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
MONTH_ABBR = {name.lower(): idx for idx, name in enumerate(calendar.month_abbr) if name}
MONTHS.update(MONTH_ABBR)

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


@dataclass(frozen=True)
class TemporalResolution:
    intent: str
    period_type: str
    start_at: datetime
    end_at: datetime
    display_label: str
    event_type: str = "created"
    source_plan: str = "bq_only"
    confidence: str = "high"
    matched_text: str = ""

    @property
    def start_date(self) -> date:
        return self.start_at.date()

    @property
    def end_date_exclusive(self) -> date:
        return self.end_at.date()


def resolve_temporal_context(
    messages: list[dict],
    *,
    now: datetime | None = None,
) -> TemporalResolution | None:
    """Resolve latest turn plus prior context into one Eastern time window."""
    if not messages:
        return None
    now_business_time = (now or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    latest = str((messages[-1] or {}).get("content") or "")
    normalized_latest = _normalize_query_text(latest)
    intent = _detect_intent(normalized_latest) or _infer_prior_intent(messages[:-1])
    if not intent:
        return None
    event_type = _detect_event_type(normalized_latest) or _infer_prior_event_type(messages[:-1])

    resolved = _resolve_text_period(normalized_latest, now_business_time)
    if not resolved and _references_prior_day(latest):
        resolved = _resolve_prior_period(messages[:-1], now_business_time)
    if not resolved and _should_inherit_prior_period(latest):
        resolved = _resolve_prior_period(messages[:-1], now_business_time)
    if not resolved:
        return None

    start_day, end_day, period_type, matched = resolved
    start_at = datetime.combine(start_day, time.min, tzinfo=BUSINESS_TZ)
    end_at = datetime.combine(end_day, time.min, tzinfo=BUSINESS_TZ)
    return TemporalResolution(
        intent=intent,
        period_type=period_type,
        start_at=start_at,
        end_at=end_at,
        display_label=_display_label(start_day, end_day, period_type),
        event_type=event_type,
        source_plan=_source_plan(start_day, end_day, now_business_time.date()),
        matched_text=matched,
    )


def resolve_period_text(
    text: str,
    *,
    now: datetime | None = None,
) -> TemporalResolution | None:
    """Resolve a date/range phrase without requiring an analytics intent."""
    now_business_time = (now or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    normalized = _normalize_query_text(text)
    resolved = _resolve_text_period(normalized, now_business_time)
    if not resolved:
        return None
    start_day, end_day, period_type, matched = resolved
    start_at = datetime.combine(start_day, time.min, tzinfo=BUSINESS_TZ)
    end_at = datetime.combine(end_day, time.min, tzinfo=BUSINESS_TZ)
    return TemporalResolution(
        intent=_detect_intent(normalized) or "period",
        period_type=period_type,
        start_at=start_at,
        end_at=end_at,
        display_label=_display_label(start_day, end_day, period_type),
        event_type=_detect_event_type(normalized) or "created",
        source_plan=_source_plan(start_day, end_day, now_business_time.date()),
        matched_text=matched,
    )


def bq_date_filter(resolution: TemporalResolution) -> str:
    """Return a BigQuery filter that uses Eastern business dates explicitly."""
    start = resolution.start_date.isoformat()
    end = resolution.end_date_exclusive.isoformat()
    field = "updated_at" if resolution.event_type == "updated" else "created_at"
    if resolution.period_type == "single_day":
        return f"DATE({field}, 'America/New_York') = DATE '{start}'"
    return (
        f"DATE({field}, 'America/New_York') >= DATE '{start}' "
        f"AND DATE({field}, 'America/New_York') < DATE '{end}'"
    )


def build_bq_sql(resolution: TemporalResolution) -> str | None:
    """Build first-slice deterministic analytics SQL for supported intents."""
    where = bq_date_filter(resolution)
    if resolution.intent == "ticket_count":
        return (
            "SELECT COUNT(id) AS ticket_count\n"
            "FROM `esip-reference-project.esip_reference.tickets`\n"
            f"WHERE {where}"
        )
    if resolution.intent == "top_stores":
        return (
            "SELECT COALESCE(NULLIF(TRIM(store_id), ''), 'Uncategorized') AS store_id,\n"
            "       COUNT(id) AS ticket_count\n"
            "FROM `esip-reference-project.esip_reference.tickets`\n"
            f"WHERE {where}\n"
            "GROUP BY store_id\n"
            "ORDER BY ticket_count DESC\n"
            "LIMIT 20"
        )
    if resolution.intent == "top_issues":
        return (
            "SELECT\n"
            "  CASE\n"
            "    WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN issue_category\n"
            "    ELSE 'Uncategorized'\n"
            "  END AS issue_category,\n"
            "  COUNT(id) AS ticket_count\n"
            "FROM `esip-reference-project.esip_reference.tickets`\n"
            f"WHERE {where}\n"
            "GROUP BY issue_category\n"
            "ORDER BY ticket_count DESC\n"
            "LIMIT 20"
        )
    return None


def _detect_intent(text: str) -> str | None:
    q = _normalize_query_text(text)
    if not q.strip():
        return None
    if "store" in q and any(token in q for token in ("most", "top", "driving", "which stores", "reported")):
        return "top_stores"
    if ("how many" in q or "count" in q) and "ticket" in q:
        return "ticket_count"
    if ("top" in q or "most" in q) and ("issue" in q or "category" in q):
        return "top_issues"
    return None


def _detect_event_type(text: str) -> str | None:
    q = _normalize_query_text(text)
    if re.search(r"\b(updated|modified|changed)\b", q):
        return "updated"
    if re.search(r"\b(created|opened|logged|reported)\b", q):
        return "created"
    return None


def _infer_prior_event_type(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        event_type = _detect_event_type(str(msg.get("content") or ""))
        if event_type:
            return event_type
    return "created"


def _infer_prior_intent(messages: list[dict]) -> str | None:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        intent = _detect_intent(str(msg.get("content") or ""))
        if intent:
            return intent
    return None


def _resolve_prior_period(messages: list[dict], now: datetime) -> tuple[date, date, str, str] | None:
    for msg in reversed(messages):
        text = _normalize_query_text(str(msg.get("content") or ""))
        resolved = _resolve_text_period(text, now)
        if resolved:
            return resolved
    return None


def _resolve_text_period(text: str, now: datetime) -> tuple[date, date, str, str] | None:
    q = _normalize_query_text(text)
    today = now.date()

    explicit_range = _explicit_date_range(q, today.year)
    if explicit_range:
        return explicit_range

    year_to_present = _explicit_year_to_present_range(q, today)
    if year_to_present:
        return year_to_present

    explicit = _explicit_month_day(q, today.year)
    if explicit:
        day, matched = explicit
        return day, day + timedelta(days=1), "single_day", matched

    if re.search(r"\btoday\b", q):
        return today, today + timedelta(days=1), "single_day", "today"
    if re.search(r"\byesterday\b", q):
        day = today - timedelta(days=1)
        return day, day + timedelta(days=1), "single_day", "yesterday"

    ago = re.search(r"\b(\d+)\s+days?\s+ago\b", q)
    if ago:
        day = today - timedelta(days=int(ago.group(1)))
        return day, day + timedelta(days=1), "single_day", ago.group(0)

    weekday = re.search(r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", q)
    if weekday:
        target = WEEKDAYS[weekday.group(1)]
        delta = (today.weekday() - target) % 7
        if delta == 0:
            delta = 7
        day = today - timedelta(days=delta)
        return day, day + timedelta(days=1), "single_day", weekday.group(0)

    rolling = re.search(r"\b(?:within\s+)?(?:last|past)\s+(\d+)\s*(day|days|week|weeks)\b", q)
    if rolling:
        n = int(rolling.group(1))
        if rolling.group(2).startswith("week"):
            n *= 7
        start = today - timedelta(days=max(0, n - 1))
        return start, today + timedelta(days=1), "rolling_range", rolling.group(0)

    if re.search(r"\bwithin\s+1\s+week\b", q):
        return today - timedelta(days=6), today + timedelta(days=1), "rolling_range", "within 1 week"

    if re.search(r"\bthis\s+week\b", q):
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7), "calendar_week", "this week"
    if re.search(r"\blast\s+week\b", q):
        this_start = today - timedelta(days=today.weekday())
        start = this_start - timedelta(days=7)
        return start, this_start, "calendar_week", "last week"

    if re.search(r"\bthis\s+month\b", q):
        start = date(today.year, today.month, 1)
        return start, today + timedelta(days=1), "calendar_month", "this month"
    if re.search(r"\bpast\s+month\b", q):
        return today - timedelta(days=29), today + timedelta(days=1), "rolling_range", "past month"
    if re.search(r"\blast\s+month\b", q):
        this_start = date(today.year, today.month, 1)
        last_end = this_start
        last_start = date(this_start.year - 1, 12, 1) if this_start.month == 1 else date(this_start.year, this_start.month - 1, 1)
        return last_start, last_end, "calendar_month", "last month"

    if re.search(r"\bthis\s+year\b", q):
        start = date(today.year, 1, 1)
        return start, today + timedelta(days=1), "calendar_year", "this year"
    if re.search(r"\blast\s+year\b", q):
        start = date(today.year - 1, 1, 1)
        return start, date(today.year, 1, 1), "calendar_year", "last year"

    month = _explicit_month(q, today.year)
    if month:
        year, month_num, matched = month
        start = date(year, month_num, 1)
        end = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
        if start <= today < end:
            end = today + timedelta(days=1)
        return start, end, "calendar_month", matched

    explicit_year = _explicit_year(q)
    if explicit_year:
        year, matched = explicit_year
        return date(year, 1, 1), date(year + 1, 1, 1), "calendar_year", matched

    return None


def _normalize_query_text(text: str) -> str:
    """Normalize small chat typos before deterministic intent/date parsing."""
    q = (text or "").lower()
    for word, value in NUMBER_WORDS.items():
        q = re.sub(rf"\b{word}\s+days?\s+ago\b", f"{value} days ago", q)
        q = re.sub(
            rf"\b((?:within\s+)?(?:last|past))\s+{word}\s+(day|days|week|weeks)\b",
            rf"\1 {value} \2",
            q,
        )
    q = re.sub(r"\bwithon\b", "within", q)
    q = re.sub(r"\bwithing\b", "within", q)
    q = re.sub(r"\bwihin\b", "within", q)
    q = re.sub(r"\bopenn?ed\b", "created", q)
    q = re.sub(r"\bhow\s+n\W*may\b", "how many", q)
    q = re.sub(r"\bhow\s+m\W*any\b", "how many", q)
    return q


def _explicit_month_day(text: str, default_year: int) -> tuple[date, str] | None:
    pattern = (
        r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|"
        r"august|aug|september|sep|sept|october|oct|november|nov|december|dec)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    year = int(match.group(3) or default_year)
    day_num = int(match.group(2))
    try:
        return date(year, month, day_num), match.group(0)
    except ValueError:
        return None


def _explicit_date_range(text: str, default_year: int) -> tuple[date, date, str, str] | None:
    iso = re.search(
        r"\b(?:from|between)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|through|until|and)\s+(\d{4}-\d{2}-\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if iso:
        try:
            start = date.fromisoformat(iso.group(1))
            end_inclusive = date.fromisoformat(iso.group(2))
            if start <= end_inclusive:
                return start, end_inclusive + timedelta(days=1), "date_range", iso.group(0)
        except ValueError:
            pass

    md = re.search(
        r"\b(?:from|between)\s+"
        r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\s+"
        r"(?:to|through|until|and)\s+"
        r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if md:
        start_year = int(md.group(3) or md.group(6) or default_year)
        end_year = int(md.group(6) or start_year)
        try:
            start = date(start_year, MONTHS[md.group(1).lower()], int(md.group(2)))
            end_inclusive = date(end_year, MONTHS[md.group(4).lower()], int(md.group(5)))
            if start <= end_inclusive:
                return start, end_inclusive + timedelta(days=1), "date_range", md.group(0)
        except (KeyError, ValueError):
            pass

    month_range = re.search(
        r"\b(?:from|between)\s+"
        r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{4})\s+"
        r"(?:to|through|until|and)\s+"
        r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_range:
        try:
            start_month = MONTHS[month_range.group(1).lower()]
            end_month = MONTHS[month_range.group(3).lower()]
            start = date(int(month_range.group(2)), start_month, 1)
            end_year = int(month_range.group(4))
            end = date(end_year + 1, 1, 1) if end_month == 12 else date(end_year, end_month + 1, 1)
            if start < end:
                return start, end, "date_range", month_range.group(0)
        except (KeyError, ValueError):
            pass

    boundary = re.search(
        r"\b(?:from\s+)?(?:the\s+)?(?:beginning|start)\s+of\s+"
        r"(?:(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+)?(\d{4})\s+"
        r"(?:to|through|until|and)\s+(?:the\s+)?(?:end|close)\s+of\s+"
        r"(?:(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|"
        r"september|sep|sept|october|oct|november|nov|december|dec)\s+)?(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if boundary:
        try:
            start_month = MONTHS.get((boundary.group(1) or "january").lower(), 1)
            end_month = MONTHS.get((boundary.group(3) or "december").lower(), 12)
            start = date(int(boundary.group(2)), start_month, 1)
            end_year = int(boundary.group(4))
            end = date(end_year + 1, 1, 1) if end_month == 12 else date(end_year, end_month + 1, 1)
            if start < end:
                return start, end, "date_range", boundary.group(0)
        except ValueError:
            pass

    return None


def _explicit_year_to_present_range(text: str, today: date) -> tuple[date, date, str, str] | None:
    patterns = [
        r"\b(?:from|since|starting\s+(?:from|in)|beginning\s+(?:in|of)|back\s+to)\s+(20\d{2})\s+"
        r"(?:to|through|until|and|up\s+to)\s+(?:today|now|current|present)\b",
        r"\b(?:since|from)\s+(20\d{2})\s+(?:onward|onwards|forward|forwards)\b",
        r"\b(?:начиная\s+(?:с|от)|с)\s+(20\d{2})\s+(?:и\s+)?(?:по|до)\s+сегодня\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        year = int(match.group(1))
        if year > today.year + 1:
            continue
        return date(year, 1, 1), today + timedelta(days=1), "date_range", match.group(0)
    return None


def _explicit_month(text: str, default_year: int) -> tuple[int, int, str] | None:
    pattern = (
        r"\b(?:in\s+)?(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)(?:\s+(\d{4}))?\b"
    )
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    year = int(match.group(2) or default_year)
    return year, month, match.group(0)


def _explicit_year(text: str) -> tuple[int, str] | None:
    match = re.search(r"\b(?:in|during|for|throughout)\s+(?:the\s+year\s+)?(20\d{2})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1)), match.group(0)


def _references_prior_day(text: str) -> bool:
    q = (text or "").lower()
    return bool(re.search(r"\b(that day|same day|that date|same date|those days|that period)\b", q))


def _should_inherit_prior_period(text: str) -> bool:
    q = (text or "").lower().strip()
    if not q:
        return False
    if _resolve_text_period(q, datetime.now(BUSINESS_TZ)):
        return False
    words = re.findall(r"[a-z0-9']+", q)
    if len(words) > 8:
        return False
    return bool(
        re.search(r"\b(which stores|top stores|reported most|driving those|how many|show examples|examples)\b", q)
    )


def _display_label(start: date, end: date, period_type: str) -> str:
    if period_type == "single_day":
        return _format_day(start)
    end_inclusive = end - timedelta(days=1)
    if start.year == end_inclusive.year:
        return f"{_format_month_day(start)}-{_format_month_day(end_inclusive)}, {start.year}"
    return f"{_format_day(start)}-{_format_day(end_inclusive)}"


def _format_day(day: date) -> str:
    return f"{calendar.month_name[day.month]} {day.day}, {day.year}"


def _format_month_day(day: date) -> str:
    return f"{calendar.month_name[day.month]} {day.day}"


def _source_plan(start: date, end: date, today: date) -> str:
    if start <= today < end:
        return "bq_plus_zendesk_tail"
    return "bq_then_zendesk_if_empty"
