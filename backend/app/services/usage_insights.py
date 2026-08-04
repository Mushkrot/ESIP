"""Admin usage insights over durable Universal Chat logs.

This module intentionally returns aggregate/sanitized management data only.
Raw answers, full transcripts, and GCS payload URIs stay behind the existing
Super Admin chat-log endpoint.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from google.cloud import bigquery

from app.config import settings
from app.services import bigquery as bq_service
from app.services import firestore

QUESTION_TYPES = [
    "Procedure / how-to",
    "Document lookup",
    "Ticket lookup",
    "Similar past cases",
    "Store history / store issue analysis",
    "Analytics / reporting",
    "Escalation / ownership",
    "Summary / translation",
    "Access / login / user support",
    "General platform help",
    "Unknown / other",
]

WEAK_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(no|not enough|insufficient)\s+(specific\s+)?(data|information|details|evidence)\b",
        r"\b(could not|can't|cannot|unable to)\s+(find|locate|determine)\b",
        r"\bautomated search (did not|didn't) (return|find)\b",
        r"\bno (results|matching tickets|relevant tickets|documents?)\b",
        r"\bquery error\b",
        r"\bunrecognized name\b",
        r"\bnot found inside\b",
    ]
]

_STOPWORDS = {
    "about",
    "after",
    "again",
    "answer",
    "anything",
    "because",
    "before",
    "could",
    "does",
    "find",
    "from",
    "have",
    "help",
    "into",
    "need",
    "please",
    "question",
    "show",
    "tell",
    "that",
    "their",
    "there",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}


def get_usage_dashboard(
    *,
    range_mode: str = "to_date",
    start_date: str | None = None,
    end_date: str | None = None,
    user_email: str | None = None,
    question_type: str | None = None,
    include_translation_turns: bool = False,
    include_super_admin_usage: bool = False,
    include_technical_quality: bool = False,
    compare_previous: bool = True,
    group_by: str = "user",
    min_activity: int = 0,
    problem_signal: str = "all",
    category_focus: str = "all",
) -> dict[str, Any]:
    """Return a sanitized Usage Insights dashboard payload for Admin Panel."""
    resolved_range = _resolve_range(range_mode, start_date, end_date)
    user_filter = (user_email or "").strip().lower()
    type_filter = (question_type or "").strip()
    if type_filter and type_filter not in QUESTION_TYPES:
        type_filter = ""
    safe_group_by = group_by if group_by in {"user", "question_type", "day", "week"} else "user"
    safe_min_activity = max(0, min(int(min_activity or 0), 1000))
    safe_problem_signal = (
        problem_signal if problem_signal in {"all", "no_sources", "weak_answers", "high_latency", "repeated_topic"}
        else "all"
    )
    if safe_problem_signal == "weak_answers" and not include_technical_quality:
        safe_problem_signal = "all"
    safe_category_focus = (
        category_focus if category_focus in {"all", "knowledge_documents", "exclude_summaries", "exclude_ticket_lookups"}
        else "all"
    )

    rows = _fetch_turn_rows(
        start_at=resolved_range["start_at"],
        end_at=resolved_range["end_at"],
        user_email=user_filter or None,
        include_translation_turns=include_translation_turns,
    )
    users_to_date = _fetch_user_counts_to_date(include_translation_turns=include_translation_turns)
    user_profiles = _load_user_profiles()
    if not include_super_admin_usage:
        rows = [
            row for row in rows
            if not _is_super_admin_owner(row.owner_email, row.owner_role, user_profiles)
        ]
        users_to_date = {
            email: data for email, data in users_to_date.items()
            if not _is_super_admin_owner(email, data.get("owner_role"), user_profiles)
        }

    classified: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_row(row)
        normalized["question_type"] = classify_question_type(normalized)
        normalized["weak_answer"] = _contains_weak_language(normalized.get("assistant_answer") or "")
        normalized["sources_count"] = _positive_int(normalized["counts"], "sources")
        normalized["tickets_count"] = _positive_int(normalized["counts"], "tickets")
        normalized["analytics_count"] = _positive_int(normalized["counts"], "bq")
        normalized["evidence_count"] = _visible_evidence_count(normalized)
        normalized["no_evidence_shown"] = normalized["evidence_count"] == 0
        normalized["question_theme"] = _theme_from_question(normalized["latest_question"], normalized["question_type"])
        classified.append(normalized)

    classified = _apply_analysis_filters(
        classified,
        question_type=type_filter,
        problem_signal=safe_problem_signal,
        category_focus=safe_category_focus,
    )

    users_all = _build_user_rows(classified, users_to_date, user_profiles, type_filter=type_filter)
    users = _apply_user_activity_threshold(users_all, safe_min_activity)
    summary = _build_summary(classified, users_all, users_to_date)
    question_types = _build_question_types(classified)
    timeseries = _build_timeseries(classified, resolved_range)
    source_coverage = _build_source_coverage(classified)
    document_gaps = _build_document_gaps(classified)
    grouped_breakdown = _build_grouped_breakdown(classified, users_all, group_by=safe_group_by)
    comparison = (
        _build_previous_period_comparison(
            resolved_range=resolved_range,
            user_email=user_filter or None,
            question_type=type_filter,
            include_translation_turns=include_translation_turns,
            include_super_admin_usage=include_super_admin_usage,
            user_profiles=user_profiles,
            problem_signal=safe_problem_signal,
            category_focus=safe_category_focus,
        )
        if compare_previous
        else None
    )

    filter_users = _build_filter_users(users_to_date, user_profiles)

    return {
        "range": {
            "mode": resolved_range["mode"],
            "start_at": _iso_or_none(resolved_range["start_at"]),
            "end_at": _iso_or_none(resolved_range["end_at"]),
            "include_translation_turns": include_translation_turns,
            "user_email": user_filter or None,
            "question_type": type_filter or None,
        },
        "controls": {
            "compare_previous": compare_previous,
            "group_by": safe_group_by,
            "min_activity": safe_min_activity,
            "problem_signal": safe_problem_signal,
            "category_focus": safe_category_focus,
            "technical_quality_enabled": include_technical_quality,
        },
        "summary": summary,
        "comparison": comparison,
        "users": users,
        "users_unfiltered_count": len(users_all),
        "question_types": question_types,
        "timeseries": timeseries,
        "grouped_breakdown": grouped_breakdown,
        "source_coverage": source_coverage,
        "document_gaps": document_gaps,
        "filters": {
            "users": filter_users,
            "question_types": QUESTION_TYPES,
        },
        "notes": [
            "Query counts are completed Universal Chat turns from BigQuery durable logs.",
            "Translation turns are excluded by default because they are not new business questions.",
            "Question types are deterministic labels inferred from existing tool/policy fields.",
            "Super Admin usage is excluded from management Usage Insights.",
        ],
    }


def classify_question_type(row: dict[str, Any]) -> str:
    question = str(row.get("latest_question") or "").strip()
    q_lower = question.lower()
    tools = set(_tools(row.get("phase1_decision")))
    flags = _json_dict(row.get("policy_flags"))

    if flags.get("translation_request") or q_lower.startswith("__translate__:"):
        return "Summary / translation"
    if flags.get("force_store_issue_breakdown"):
        return "Store history / store issue analysis"
    if "run_bq_query" in tools or "analytics" in q_lower or "report" in q_lower:
        return "Analytics / reporting"
    if flags.get("force_drive_tabular") or "search_drive_exact" in tools:
        return "Document lookup"
    if row.get("target_ticket_id") or row.get("ticket_id") or flags.get("explicit_ticket_id"):
        if "search_similar_tickets" in tools and not flags.get("explicit_ticket_id"):
            return "Similar past cases"
        return "Ticket lookup"
    if "search_similar_tickets" in tools:
        return "Similar past cases"
    if "search_knowledge_base" in tools:
        return "Procedure / how-to"
    if any(term in q_lower for term in ["who owns", "owner", "escalate", "support group", "assignment group"]):
        return "Escalation / ownership"
    if any(term in q_lower for term in ["login", "log in", "password", "access", "auth", "permission"]):
        return "Access / login / user support"
    if any(term in q_lower for term in ["how do i", "how to", "procedure", "steps", "setup", "configure"]):
        return "Procedure / how-to"
    if any(term in q_lower for term in ["document", "article", "kb", "guide", "job aid", "manual"]):
        return "Document lookup"
    if any(term in q_lower for term in ["summarize", "summary", "recap"]):
        return "Summary / translation"
    if question:
        return "General platform help"
    return "Unknown / other"


def _resolve_range(
    range_mode: str,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    mode = (range_mode or "to_date").strip().lower()
    if mode in {"7d", "30d", "90d"}:
        days = int(mode[:-1])
        return {"mode": mode, "start_at": now - timedelta(days=days), "end_at": now}
    if mode == "custom":
        start = _parse_date_or_dt(start_date)
        end = _parse_date_or_dt(end_date, end_of_day=True) or now
        if start and start > end:
            start, end = end, start
        return {"mode": "custom", "start_at": start, "end_at": end}
    return {"mode": "to_date", "start_at": None, "end_at": now}


def _parse_date_or_dt(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed = datetime.combine(date.fromisoformat(raw), datetime.max.time() if end_of_day else datetime.min.time())
            return parsed.replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fetch_turn_rows(
    *,
    start_at: datetime | None,
    end_at: datetime | None,
    user_email: str | None,
    include_translation_turns: bool,
) -> list[Any]:
    conditions = ["created_at <= @end_at"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end_at or datetime.now(timezone.utc)),
    ]
    if start_at is not None:
        conditions.append("created_at >= @start_at")
        params.append(bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start_at))
    if user_email:
        conditions.append("LOWER(IFNULL(owner_email, '')) = @user_email")
        params.append(bigquery.ScalarQueryParameter("user_email", "STRING", user_email))
    if not include_translation_turns:
        conditions.append(
            "NOT (IFNULL(JSON_VALUE(policy_flags, '$.translation_request'), 'false') = 'true' "
            "OR STARTS_WITH(IFNULL(latest_question, ''), '__translate__:'))"
        )

    sql = f"""
        SELECT
          created_at,
          event_date,
          surface,
          owner_email,
          owner_role,
          ticket_id,
          target_ticket_id,
          latest_question,
          assistant_answer,
          phase1_decision,
          policy_flags,
          counts,
          latency_ms
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC
        LIMIT 50000
    """
    client = bq_service._get_client()
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return list(client.query(sql, job_config=job_config).result())


def _fetch_user_counts_to_date(*, include_translation_turns: bool) -> dict[str, dict[str, Any]]:
    conditions = ["owner_email IS NOT NULL", "owner_email != ''"]
    if not include_translation_turns:
        conditions.append(
            "NOT (IFNULL(JSON_VALUE(policy_flags, '$.translation_request'), 'false') = 'true' "
            "OR STARTS_WITH(IFNULL(latest_question, ''), '__translate__:'))"
        )
    sql = f"""
        SELECT
          LOWER(owner_email) AS owner_email,
          ANY_VALUE(owner_role) AS owner_role,
          MIN(created_at) AS first_seen_at,
          MAX(created_at) AS last_active_at,
          COUNT(*) AS queries_to_date,
          AVG(latency_ms) AS avg_latency_ms
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE {' AND '.join(conditions)}
        GROUP BY owner_email
        ORDER BY queries_to_date DESC, owner_email
        LIMIT 5000
    """
    client = bq_service._get_client()
    out: dict[str, dict[str, Any]] = {}
    for row in client.query(sql).result():
        email = str(row.owner_email or "").lower()
        out[email] = {
            "owner_email": email,
            "owner_role": row.owner_role,
            "first_seen_at": row.first_seen_at,
            "last_active_at": row.last_active_at,
            "queries_to_date": int(row.queries_to_date or 0),
            "avg_latency_ms_to_date": _round_or_none(row.avg_latency_ms),
        }
    return out


def _build_previous_period_comparison(
    *,
    resolved_range: dict[str, Any],
    user_email: str | None,
    question_type: str,
    include_translation_turns: bool,
    include_super_admin_usage: bool,
    user_profiles: dict[str, dict[str, Any]],
    problem_signal: str,
    category_focus: str,
) -> dict[str, Any] | None:
    start_at = resolved_range.get("start_at")
    end_at = resolved_range.get("end_at")
    if not isinstance(start_at, datetime) or not isinstance(end_at, datetime):
        return None
    period = end_at - start_at
    if period.total_seconds() <= 0:
        return None
    previous_end = start_at
    previous_start = start_at - period
    rows = _fetch_turn_rows(
        start_at=previous_start,
        end_at=previous_end,
        user_email=user_email,
        include_translation_turns=include_translation_turns,
    )
    if not include_super_admin_usage:
        rows = [
            row for row in rows
            if not _is_super_admin_owner(row.owner_email, row.owner_role, user_profiles)
        ]
    classified: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_row(row)
        normalized["question_type"] = classify_question_type(normalized)
        normalized["weak_answer"] = _contains_weak_language(normalized.get("assistant_answer") or "")
        normalized["sources_count"] = _positive_int(normalized["counts"], "sources")
        normalized["tickets_count"] = _positive_int(normalized["counts"], "tickets")
        normalized["analytics_count"] = _positive_int(normalized["counts"], "bq")
        normalized["evidence_count"] = _visible_evidence_count(normalized)
        normalized["no_evidence_shown"] = normalized["evidence_count"] == 0
        normalized["question_theme"] = _theme_from_question(normalized["latest_question"], normalized["question_type"])
        classified.append(normalized)
    filtered = _apply_analysis_filters(
        classified,
        question_type=question_type,
        problem_signal=problem_signal,
        category_focus=category_focus,
    )
    total = len(filtered)
    active_users = len({row["owner_email"] or "unknown" for row in filtered})
    weak = sum(1 for row in filtered if row["weak_answer"])
    no_source = sum(1 for row in filtered if row["no_evidence_shown"])
    return {
        "start_at": _iso_or_none(previous_start),
        "end_at": _iso_or_none(previous_end),
        "total_queries_in_range": total,
        "active_users_in_range": active_users,
        "weak_or_no_answer_turns": weak,
        "no_source_turns": no_source,
    }


def _load_user_profiles() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        for doc in firestore.get_db().collection("users").stream():
            email = str(doc.id or "").strip().lower()
            data = doc.to_dict() or {}
            display_name = firestore.clean_auth_display_name(
                data.get("display_name")
                or data.get("name")
                or data.get("full_name")
                or "",
                email,
            )
            out[email] = {
                "email": email,
                "display_name": display_name,
                "role": firestore._role_from_doc(data),
                "active": bool(data.get("active", False)),
            }
    except Exception:
        return out
    return out


def _is_super_admin_owner(
    email: str | None,
    owner_role: str | None,
    profiles: dict[str, dict[str, Any]],
) -> bool:
    safe_email = str(email or "").strip().lower()
    profile_role = (profiles.get(safe_email) or {}).get("role")
    return profile_role == firestore.ROLE_SUPER_ADMIN or owner_role == firestore.ROLE_SUPER_ADMIN


def _normalize_row(row: Any) -> dict[str, Any]:
    return {
        "created_at": row.created_at,
        "event_date": row.event_date,
        "surface": row.surface,
        "owner_email": str(row.owner_email or "").strip().lower(),
        "owner_role": row.owner_role,
        "ticket_id": row.ticket_id,
        "target_ticket_id": row.target_ticket_id,
        "latest_question": row.latest_question or "",
        "assistant_answer": row.assistant_answer or "",
        "phase1_decision": row.phase1_decision,
        "policy_flags": row.policy_flags,
        "counts": _json_dict(row.counts),
        "latency_ms": row.latency_ms,
    }


def _build_user_rows(
    rows: list[dict[str, Any]],
    users_to_date: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    *,
    type_filter: str,
) -> list[dict[str, Any]]:
    range_counts: Counter[str] = Counter()
    weak_counts: Counter[str] = Counter()
    no_source_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    documentation_opportunity_counts: Counter[str] = Counter()
    latency_sums: defaultdict[str, float] = defaultdict(float)
    latency_counts: Counter[str] = Counter()
    type_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    active_days: defaultdict[str, set[str]] = defaultdict(set)

    for row in rows:
        email = row["owner_email"] or "unknown"
        range_counts[email] += 1
        if row.get("created_at"):
            active_days[email].add(row["created_at"].date().isoformat())
        if row["weak_answer"]:
            weak_counts[email] += 1
        if row["no_evidence_shown"]:
            no_source_counts[email] += 1
        if row["evidence_count"] > 0:
            evidence_counts[email] += 1
        if _is_documentation_opportunity(row):
            documentation_opportunity_counts[email] += 1
        latency = row.get("latency_ms")
        if latency is not None:
            latency_sums[email] += float(latency)
            latency_counts[email] += 1
        type_counts[email][row["question_type"]] += 1

    emails = set(users_to_date) | set(range_counts)
    out = []
    for email in emails:
        to_date = users_to_date.get(email, {})
        profile = profiles.get(email, {})
        role = profile.get("role") or to_date.get("owner_role") or "user"
        display_name = profile.get("display_name") or ""
        avg_latency = (
            latency_sums[email] / latency_counts[email]
            if latency_counts[email]
            else to_date.get("avg_latency_ms_to_date")
        )
        first_seen = to_date.get("first_seen_at")
        last_active = to_date.get("last_active_at")
        top_type = type_counts[email].most_common(1)[0][0] if type_counts[email] else None
        out.append({
            "email": None if email == "unknown" else email,
            "display_name": display_name,
            "role": role,
            "active": bool(profile.get("active", False)),
            "first_seen_at": _iso_or_none(first_seen),
            "last_active_at": _iso_or_none(last_active),
            "queries_to_date": int(to_date.get("queries_to_date") or 0),
            "queries_in_range": int(range_counts[email]),
            "avg_latency_ms": _round_or_none(avg_latency),
            "weak_or_no_answer": int(weak_counts[email]),
            "no_source_count": int(no_source_counts[email]),
            "evidence_rate_pct": _pct(evidence_counts[email], range_counts[email]),
            "documentation_opportunities": int(documentation_opportunity_counts[email]),
            "active_days": len(active_days[email]),
            "top_question_type": top_type,
        })
    out.sort(
        key=lambda item: (
            item["queries_in_range"],
            item["queries_to_date"],
            item["display_name"] or item["email"] or "",
        ),
        reverse=True,
    )
    if type_filter:
        return [item for item in out if item["queries_in_range"] > 0]
    return out


def _apply_user_activity_threshold(users: list[dict[str, Any]], min_activity: int) -> list[dict[str, Any]]:
    if min_activity <= 0:
        return users
    return [user for user in users if int(user.get("queries_in_range") or 0) >= min_activity]


def _apply_analysis_filters(
    rows: list[dict[str, Any]],
    *,
    question_type: str,
    problem_signal: str,
    category_focus: str,
) -> list[dict[str, Any]]:
    out = rows
    if question_type:
        out = [row for row in out if row["question_type"] == question_type]
    if category_focus == "knowledge_documents":
        out = [
            row for row in out
            if row["question_type"] in {"Procedure / how-to", "Document lookup", "Access / login / user support"}
        ]
    elif category_focus == "exclude_summaries":
        out = [row for row in out if row["question_type"] != "Summary / translation"]
    elif category_focus == "exclude_ticket_lookups":
        out = [row for row in out if row["question_type"] not in {"Ticket lookup", "Similar past cases"}]

    if problem_signal == "all":
        return out
    if problem_signal == "no_sources":
        return [row for row in out if row["no_evidence_shown"]]
    if problem_signal == "weak_answers":
        return [row for row in out if row["weak_answer"]]
    if problem_signal == "high_latency":
        return [row for row in out if int(row.get("latency_ms") or 0) >= 15000]
    if problem_signal == "repeated_topic":
        theme_counts = Counter((row["question_type"], row.get("question_theme") or "") for row in out)
        return [
            row for row in out
            if theme_counts[(row["question_type"], row.get("question_theme") or "")] >= 2
        ]
    return out


def _build_summary(
    rows: list[dict[str, Any]],
    users: list[dict[str, Any]],
    users_to_date: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    total_in_range = len(rows)
    active_users = len({row["owner_email"] or "unknown" for row in rows})
    weak = sum(1 for row in rows if row["weak_answer"])
    no_source = sum(1 for row in rows if row["no_evidence_shown"])
    type_counter = Counter(row["question_type"] for row in rows)
    role_counter = Counter(user["role"] for user in users if user["queries_in_range"] > 0)
    total_to_date = sum(int(item.get("queries_to_date") or 0) for item in users_to_date.values())
    return {
        "total_queries_to_date": total_to_date,
        "total_queries_in_range": total_in_range,
        "active_users_in_range": active_users,
        "avg_queries_per_active_user": round(total_in_range / active_users, 1) if active_users else 0,
        "weak_or_no_answer_turns": weak,
        "no_source_turns": no_source,
        "top_question_type": type_counter.most_common(1)[0][0] if type_counter else None,
        "usage_by_role": [{"role": role, "count": count} for role, count in role_counter.most_common()],
    }


def _build_grouped_breakdown(
    rows: list[dict[str, Any]],
    users: list[dict[str, Any]],
    *,
    group_by: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    user_lookup = {user.get("email") or "unknown": user for user in users}
    total_rows = len(rows)
    for row in rows:
        email = row["owner_email"] or "unknown"
        if group_by == "question_type":
            key = row["question_type"]
            label = key
        elif group_by == "day":
            key = row["created_at"].date().isoformat() if row.get("created_at") else str(row.get("event_date") or "")
            label = key
        elif group_by == "week":
            if row.get("created_at"):
                iso_year, iso_week, _ = row["created_at"].isocalendar()
                key = f"{iso_year}-W{iso_week:02d}"
            else:
                key = str(row.get("event_date") or "unknown")
            label = key
        else:
            key = email
            label = user_lookup.get(email, {}).get("display_name") or email or "Unknown user"

        item = grouped.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "count": 0,
                "users": set(),
                "weak_or_no_answer": 0,
                "no_source_count": 0,
                "with_evidence": 0,
                "documentation_opportunities": 0,
                "active_days": set(),
                "last_active_at": None,
                "question_types": Counter(),
            },
        )
        item["count"] += 1
        item["users"].add(email)
        if row.get("created_at"):
            item["active_days"].add(row["created_at"].date().isoformat())
            if item["last_active_at"] is None or row["created_at"] > item["last_active_at"]:
                item["last_active_at"] = row["created_at"]
        item["question_types"][row["question_type"]] += 1
        if row["weak_answer"]:
            item["weak_or_no_answer"] += 1
        if row["no_evidence_shown"]:
            item["no_source_count"] += 1
        else:
            item["with_evidence"] += 1
        if _is_documentation_opportunity(row):
            item["documentation_opportunities"] += 1

    out = []
    for item in grouped.values():
        top_question_type = item["question_types"].most_common(1)[0][0] if item["question_types"] else None
        out.append({
            "key": item["key"],
            "label": item["label"],
            "count": item["count"],
            "users": len(item["users"]),
            "share_pct": _pct(item["count"], total_rows),
            "active_days": len(item["active_days"]),
            "last_active_at": _iso_or_none(item["last_active_at"]),
            "top_question_type": top_question_type,
            "weak_or_no_answer": item["weak_or_no_answer"],
            "no_source_count": item["no_source_count"],
            "evidence_rate_pct": _pct(item["with_evidence"], item["count"]),
            "documentation_opportunities": item["documentation_opportunities"],
        })
    out.sort(key=lambda item: (item["count"], item["label"]), reverse=True)
    return out[:25]


def _build_question_types(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {
        label: {
            "question_type": label,
            "count": 0,
            "users": set(),
            "weak_or_no_answer": 0,
            "no_source_count": 0,
            "with_evidence": 0,
        }
        for label in QUESTION_TYPES
    }
    for row in rows:
        item = grouped[row["question_type"]]
        item["count"] += 1
        item["users"].add(row["owner_email"] or "unknown")
        if row["weak_answer"]:
            item["weak_or_no_answer"] += 1
        if row["no_evidence_shown"]:
            item["no_source_count"] += 1
        else:
            item["with_evidence"] += 1
    out = []
    for item in grouped.values():
        if item["count"] == 0:
            continue
        out.append({
            "question_type": item["question_type"],
            "count": item["count"],
            "users": len(item["users"]),
            "weak_or_no_answer": item["weak_or_no_answer"],
            "no_source_count": item["no_source_count"],
            "evidence_rate_pct": _pct(item["with_evidence"], item["count"]),
        })
    out.sort(key=lambda item: item["count"], reverse=True)
    return out


def _build_timeseries(rows: list[dict[str, Any]], resolved_range: dict[str, Any]) -> list[dict[str, Any]]:
    by_day: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        day = row["created_at"].date().isoformat() if row.get("created_at") else str(row.get("event_date") or "")
        if not day:
            continue
        by_day[day]["queries"] += 1
        by_day[day]["users"] += 0
        if row["weak_answer"]:
            by_day[day]["weak_or_no_answer"] += 1
        if row["no_evidence_shown"]:
            by_day[day]["no_source"] += 1
    users_by_day: defaultdict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.get("created_at"):
            users_by_day[row["created_at"].date().isoformat()].add(row["owner_email"] or "unknown")
    keys = sorted(by_day)
    return [
        {
            "date": key,
            "queries": int(by_day[key]["queries"]),
            "active_users": len(users_by_day[key]),
            "weak_or_no_answer": int(by_day[key]["weak_or_no_answer"]),
            "no_source": int(by_day[key]["no_source"]),
        }
        for key in keys
    ]


def _build_source_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    with_sources = sum(1 for row in rows if row["sources_count"] > 0)
    with_tickets = sum(1 for row in rows if row["tickets_count"] > 0)
    with_analytics = sum(1 for row in rows if row["analytics_count"] > 0)
    with_evidence = sum(1 for row in rows if row["evidence_count"] > 0)
    return {
        "total_turns": total,
        "with_sources": with_sources,
        "with_tickets": with_tickets,
        "with_analytics": with_analytics,
        "with_evidence": with_evidence,
        "without_sources": total - with_evidence,
        "source_coverage_pct": round((with_sources / total) * 100, 1) if total else 0,
        "evidence_coverage_pct": _pct(with_evidence, total),
        "ticket_usage_pct": round((with_tickets / total) * 100, 1) if total else 0,
    }


def _build_document_gaps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        no_source = row["no_evidence_shown"]
        weak = row["weak_answer"]
        if not no_source and not weak:
            continue
        theme = row["question_type"]
        key = (row["question_type"], theme)
        item = grouped.setdefault(
            key,
            {
                "theme": theme,
                "question_type": row["question_type"],
                "count": 0,
                "users": set(),
                "no_source_count": 0,
                "weak_or_no_answer": 0,
            },
        )
        item["count"] += 1
        item["users"].add(row["owner_email"] or "unknown")
        if no_source:
            item["no_source_count"] += 1
        if weak:
            item["weak_or_no_answer"] += 1
    out = []
    for item in grouped.values():
        out.append({
            "theme": item["theme"],
            "question_type": item["question_type"],
            "count": item["count"],
            "users_affected": len(item["users"]),
            "no_source_count": item["no_source_count"],
            "weak_or_no_answer": item["weak_or_no_answer"],
            "example_question": "",
            "suggested_action": _suggested_action(item["question_type"], item["no_source_count"], item["weak_or_no_answer"]),
        })
    out.sort(key=lambda item: (item["count"], item["users_affected"], item["no_source_count"]), reverse=True)
    return out[:12]


def _build_filter_users(
    users_to_date: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out = []
    for email, data in users_to_date.items():
        profile = profiles.get(email, {})
        out.append({
            "email": email,
            "display_name": profile.get("display_name") or "",
            "role": profile.get("role") or data.get("owner_role") or "user",
            "queries_to_date": int(data.get("queries_to_date") or 0),
        })
    out.sort(key=lambda item: (item["queries_to_date"], item["display_name"] or item["email"]), reverse=True)
    return out


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        data = json.loads(str(value))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tools(value: Any) -> list[str]:
    data = _json_dict(value)
    raw = data.get("tools") or data.get("tool_calls") or []
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("tool") or item.get("function")
                if name:
                    out.append(str(name))
        return out
    return []


def _contains_weak_language(answer: str) -> bool:
    return any(pattern.search(answer or "") for pattern in WEAK_PATTERNS)


def _positive_int(data: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(data.get(key) or 0))
    except Exception:
        return 0


def _visible_evidence_count(row: dict[str, Any]) -> int:
    return int(row.get("sources_count") or 0) + int(row.get("tickets_count") or 0) + int(row.get("analytics_count") or 0)


def _pct(numerator: int | float, denominator: int | float) -> float:
    try:
        denominator_f = float(denominator)
        if denominator_f <= 0:
            return 0
        return round((float(numerator) / denominator_f) * 100, 1)
    except Exception:
        return 0


def _is_documentation_opportunity(row: dict[str, Any]) -> bool:
    if not row.get("no_evidence_shown"):
        return False
    return row.get("question_type") in {
        "Procedure / how-to",
        "Document lookup",
        "Access / login / user support",
    }


def _theme_from_question(question: str, question_type: str) -> str:
    normalized = re.sub(r"\b(ticket|store|restaurant)\s*#?\s*\d+\b", " ", question.lower())
    normalized = re.sub(r"\b\d{3,}\b", " ", normalized)
    words = [
        word
        for word in re.findall(r"[a-z][a-z0-9_-]{2,}", normalized)
        if word not in _STOPWORDS
    ]
    if not words:
        return question_type
    return " ".join(words[:5]).title()


def _sanitize_question(question: str, *, limit: int = 220) -> str:
    text = " ".join(str(question or "").split())
    text = re.sub(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "[email]", text)
    text = re.sub(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "[phone]", text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _suggested_action(question_type: str, no_source_count: int, weak_count: int) -> str:
    if question_type == "Document lookup":
        return "Add or enable the missing Drive/KB document"
    if question_type == "Procedure / how-to":
        return "Add or improve a procedure article"
    if question_type == "Analytics / reporting":
        return "Review analytics wording or add report guidance"
    if question_type == "Similar past cases":
        return "Review ticket coverage and routing"
    if no_source_count > weak_count:
        return "Improve search routing or source coverage"
    return "Needs manual review"


def _round_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except Exception:
        return None


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
