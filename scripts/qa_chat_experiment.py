#!/usr/bin/env python3
"""Targeted QA for Universal Chat quality experiments."""

from __future__ import annotations

import argparse
import os
import re
import sys
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.services import bigquery, chat_answer_validator, chat_policy, customer_risk_analytics, output_critic_bounded_repair, output_critic_shadow, quality_telemetry, universal_chat  # noqa: E402
from app.routers import universal  # noqa: E402
from app.services import ai_dispatcher_planner  # noqa: E402
from app.services import ai_dispatcher_result_sets  # noqa: E402
from app.services import ai_dispatcher_runtime  # noqa: E402
from app.services import semantic_contract_v2, semantic_interpreter, semantic_label_counts, semantic_task_contracts  # noqa: E402
from app.services import temporal_resolver  # noqa: E402
from app.services.universal_chat import _fmt_bq, decide_tools  # noqa: E402


PASS = "PASS"
FAIL = "FAIL"


def _check(condition: bool, case_id: str, message: str) -> bool:
    print(f"{PASS if condition else FAIL} {case_id}: {message}")
    return condition


def case_chat_013() -> bool:
    sql = """
    SELECT id, subject, store_id
    FROM `esip-reference-project.esip_reference.tickets`
    WHERE store_id = 2007
    ORDER BY created_at DESC
    LIMIT 5
    """
    try:
        result = bigquery.execute_query(sql)
    except Exception as exc:
        return _check(False, "CHAT-013", f"store_id numeric equality should not fail: {exc}")
    executed = result.get("sql_executed", "")
    return _check(
        "store_id = '2007'" in executed,
        "CHAT-013",
        f"store_id numeric equality rewritten; rows={result.get('row_count')}; sql={executed}",
    )


def case_chat_014() -> bool:
    sql = """
    SELECT COUNT(*) AS ticket_count
    FROM `esip-reference-project.esip_reference.tickets`
    WHERE created_at >= TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), DAY)
    LIMIT 5
    """
    try:
        result = bigquery.execute_query(sql)
    except Exception as exc:
        return _check(False, "CHAT-014", f"Eastern today rewrite should not fail: {exc}")
    executed = result.get("sql_executed", "")
    return _check(
        "DATE(created_at, 'America/New_York') = CURRENT_DATE('America/New_York')" in executed,
        "CHAT-014",
        f"today filter uses Eastern business date; rows={result.get('row_count')}; sql={executed}",
    )


def case_chat_017() -> bool:
    sql = """
    SELECT id, subject, store_id
    FROM `esip-reference-project.esip_reference.tickets`
    WHERE LOWER(full_text) LIKE '%satellite%' AND store_id IN (2525, 2656)
    ORDER BY created_at DESC
    LIMIT 10
    """
    try:
        result = bigquery.execute_query(sql)
    except Exception as exc:
        return _check(False, "CHAT-017", f"store_id numeric IN should not fail: {exc}")
    executed = result.get("sql_executed", "")
    return _check(
        "store_id IN ('2525', '2656')" in executed,
        "CHAT-017",
        f"store_id numeric IN rewritten; rows={result.get('row_count')}; sql={executed}",
    )


def case_chat_016() -> bool:
    messages = [
        {
            "role": "user",
            "content": "How many tickets are there open, pending, or on-hold where Field Services Partner is the assigned group?",
        }
    ]
    decision = decide_tools(messages)
    sql = str(decision.get("bq_sql") or "")
    uses_group_name = bool(re.search(r"\bassignee_group_name\b", sql, flags=re.IGNORECASE))
    avoids_org_name = not bool(re.search(r"\borg_name\b", sql, flags=re.IGNORECASE))
    avoids_hallucinated_field = not bool(re.search(r"\bassignee_group\b(?!_name|_id)", sql, flags=re.IGNORECASE))
    status_filter = bool(re.search(r"\b(open|pending|hold)\b", sql, flags=re.IGNORECASE))
    avoids_on_hold_literal = "'on-hold'" not in sql.lower() and '"on-hold"' not in sql.lower()
    return _check(
        "run_bq_query" in (decision.get("tools") or [])
        and uses_group_name
        and avoids_org_name
        and avoids_hallucinated_field
        and status_filter
        and avoids_on_hold_literal,
        "CHAT-016",
        f"Field Services Partner assigned-group SQL uses assignee_group_name, not org_name; sql={sql}",
    )


def case_chat_015() -> bool:
    messages = [
        {
            "role": "user",
            "content": "Which stores have the most Store Operations Depletion tickets in the last 90 days?",
        },
        {
            "role": "assistant",
            "content": "Top stores include 6411, 2525, and 2656 with repeated Store Operations Depletion tickets.",
        },
        {"role": "user", "content": "Are there any trends with these issues or stores?"},
    ]
    decision = decide_tools(messages)
    sql = str(decision.get("bq_sql") or "")
    has_bq = "run_bq_query" in (decision.get("tools") or [])
    has_time_bucket = bool(
        re.search(r"\b(DATE_TRUNC|TIMESTAMP_TRUNC|EXTRACT)\s*\(", sql, flags=re.IGNORECASE)
        or re.search(r"\bDATE\s*\(\s*(?:\w+\.)?created_at", sql, flags=re.IGNORECASE)
    )
    has_dimension = bool(
        re.search(r"\bstore_id\b", sql, flags=re.IGNORECASE)
        and re.search(r"\b(issue_category|subject|full_text|brand)\b", sql, flags=re.IGNORECASE)
    )
    has_grouping = bool(re.search(r"\bGROUP\s+BY\b", sql, flags=re.IGNORECASE))
    return _check(
        has_bq and has_time_bucket and has_dimension and has_grouping,
        "CHAT-015",
        f"trend follow-up requests time bucket plus store/issue dimensions; sql={sql}",
    )


def case_chat_018() -> bool:
    messages = [
        {
            "role": "user",
            "content": "Can you tell me top five issues for last 7 days?",
        },
        {
            "role": "assistant",
            "content": (
                "The top five issues reported in the last seven days are: "
                "1. payment_device__move5000__comm_errors with 40 tickets "
                "2. applications__order_portal__reset_requested with 24 tickets "
                "3. applications__store_operations__avst with 18 tickets "
                "4. infrastructure_issue__internet_connection__primary with 15 tickets "
                "5. server__database with 11 tickets"
            ),
        },
        {"role": "user", "content": "Can you explain in a detailed manner? Tell me top five"},
    ]
    decision = decide_tools(messages)
    sql = str(decision.get("bq_sql") or "")
    has_bq = "run_bq_query" in (decision.get("tools") or [])
    keeps_issue_breakdown = bool(re.search(r"\b(issue_category|issue_topic)\b", sql, flags=re.IGNORECASE))
    has_ticket_evidence = bool(
        re.search(r"\b(STRING_AGG|ARRAY_AGG)\s*\(", sql, flags=re.IGNORECASE)
        or re.search(r"\b(resolution|full_text|sample_tickets|sample_details)\b", sql, flags=re.IGNORECASE)
    )
    has_aggregation = bool(re.search(r"\bCOUNT\s*\(", sql, flags=re.IGNORECASE))
    return _check(
        has_bq and keeps_issue_breakdown and has_ticket_evidence and has_aggregation,
        "CHAT-018",
        f"detail follow-up after aggregate retrieves sample ticket evidence; sql={sql}",
    )


def case_chat_019() -> bool:
    context = _fmt_bq(
        [
            {"issue_topic": "server__hard_drive", "ticket_count": 2},
            {"issue_topic": None, "ticket_count": 1},
            {"issue_topic": "__subject__Pinpad keeps disconnecting", "ticket_count": 1},
        ],
        "SELECT issue_topic, COUNT(*) AS ticket_count FROM tickets GROUP BY issue_topic LIMIT 5",
    )
    has_positive_count = "ANALYTICS QUERY (3 rows)" in context
    has_no_data_guard = "Do not say" in context and "no data" in context.lower()
    has_human_issue_labels = (
        "Server --> Hard Drive" in context
        and "server__hard_drive" not in context
        and "Uncategorized" in context
        and "Inferred from subject: Pinpad keeps disconnecting" in context
        and "Subject-derived" not in context
    )
    has_grouped_issue_contract = (
        "PRESENTATION NOTE" in context
        and "grouped issue-category count result" in context
        and "show the ticket count next to each category" in context
        and "do not cite sample tickets" in context
    )
    return _check(
        has_positive_count and has_no_data_guard and has_human_issue_labels and has_grouped_issue_contract,
        "CHAT-019",
        f"positive BQ rows include anti-contradiction instruction, human-readable labels, and grouped-count presentation contract; context={context}",
    )


def case_chat_020() -> bool:
    decision = decide_tools([{"role": "user", "content": "Cannot close pay period, process fails"}])
    tools = decision.get("tools") or []
    has_kb = "search_knowledge_base" in tools
    has_similar = "search_similar_tickets" in tools
    avoids_bq = "run_bq_query" not in tools
    return _check(
        has_kb and has_similar and avoids_bq,
        "CHAT-020",
        f"pure troubleshooting procedure avoids unnecessary BQ trend query; tools={tools}; sql={decision.get('bq_sql')}",
    )


def case_chat_023() -> bool:
    from app.services import chat_policy

    questions = [
        "None of them is related to this ticket",
        "These tickets are not related to the current issue",
        "Эти тикеты не связаны с текущей проблемой",
    ]
    ok = all(
        chat_policy.is_prior_answer_challenge(q)
        and chat_policy.should_use_open_ticket_context(q)
        and chat_policy.needs_scoped_store_evidence(q)
        for q in questions
    )
    return _check(
        ok,
        "CHAT-023",
        "irrelevant-answer challenges force evidence refresh instead of using prior assistant text",
    )


def case_chat_024() -> bool:
    import asyncio

    from app.models.chat_quality import ChatQualityReviewRequest, ChatQualityReviewResult
    from app.routers import universal
    from app.services import chat_quality, firestore

    normalized = chat_quality._normalize_review_data({
        "quality": "likely_wrong",
        "confidence": 2.0,
        "problem_types": ["wrong_routing", "not_allowed"],
        "recommended_action": "reroute",
        "explanation": "test",
        "user_visible_note": "test note",
        "improved_answer": "better",
    })

    saved_events: list[dict] = []
    orig_get_trace = firestore.get_chat_answer_trace
    orig_review = chat_quality.review_answer_trace_with_telemetry
    orig_save_event = firestore.save_chat_quality_review_event
    try:
        firestore.get_chat_answer_trace = lambda trace_id: {
            "trace_id": trace_id,
            "owner_uid": "user-1",
            "owner_email": "alex.morgan@northstar.example.com",
            "ticket_id": 950032,
            "target_ticket_id": 950032,
        }
        chat_quality.review_answer_trace_with_telemetry = lambda trace, user_note=None: (
            ChatQualityReviewResult(
                quality="weak",
                confidence=0.6,
                problem_types=["weak_sources"],
                recommended_action="rewrite_with_existing_sources",
                reformulated_query=None,
                explanation="test explanation",
                user_visible_note="Reviewed.",
                improved_answer="Improved test answer.",
            ),
            {"correlation_id": "corr-test", "actual_cost_cad": 0.001, "actual_thinking_tokens": 0},
        )
        firestore.save_chat_quality_review_event = lambda event: saved_events.append(event) or "review-test"

        result = asyncio.run(universal.review_universal_answer(
            ChatQualityReviewRequest(trace_id="trace-test"),
            {"uid": "user-1", "email": "alex.morgan@northstar.example.com", "role": "agent"},
        ))
    finally:
        firestore.get_chat_answer_trace = orig_get_trace
        chat_quality.review_answer_trace_with_telemetry = orig_review
        firestore.save_chat_quality_review_event = orig_save_event

    event = saved_events[-1] if saved_events else {}
    _prepared_id, prepared = firestore._prepare_chat_quality_review_event({
        "review_id": "review-test",
        "requester_email": "alex.morgan@northstar.example.com",
        "trace_owner_email": "olivia.parker@northstar.example.com",
        "free_text": "contact priya.shah@northstar.example.com for details",
    })
    ok = (
        normalized["quality"] == "likely_wrong"
        and normalized["confidence"] == 1.0
        and normalized["problem_types"] == ["wrong_routing"]
        and normalized["recommended_action"] == "reroute"
        and result.quality == "weak"
        and event.get("status") == "success"
        and event.get("trace_id") == "trace-test"
        and event.get("quality") == "weak"
        and event.get("recommended_action") == "rewrite_with_existing_sources"
        and event.get("has_improved_answer") is True
        and event.get("telemetry", {}).get("correlation_id") == "corr-test"
        and _prepared_id == "review-test"
        and prepared.get("requester_email") == "alex.morgan@northstar.example.com"
        and prepared.get("trace_owner_email") == "olivia.parker@northstar.example.com"
        and "[EMAIL]" in prepared.get("free_text", "")
    )
    return _check(
        ok,
        "CHAT-024",
        "Improve Answer review has telemetry entrypoint and stable normalized result contract",
    )


def case_chat_025() -> bool:
    from app.models.chat_quality import ChatQualityReviewResult
    from app.services import chat_quality

    weak_result = ChatQualityReviewResult(
        quality="insufficient_evidence",
        confidence=0.5,
        problem_types=["weak_sources"],
        recommended_action="reroute",
        reformulated_query=None,
        explanation="The original date is unsupported.",
        user_visible_note="The trace does not support the original date.",
        improved_answer=None,
    )
    improved, used_fallback = chat_quality._ensure_improved_answer(
        {"messages": [{"role": "user", "content": "Когда это произошло?"}]},
        weak_result,
    )
    good_result = ChatQualityReviewResult(
        quality="good",
        confidence=0.95,
        problem_types=[],
        recommended_action="keep",
        reformulated_query=None,
        explanation="Supported.",
        user_visible_note="Looks supported.",
        improved_answer=None,
    )
    good_improved, good_used_fallback = chat_quality._ensure_improved_answer(
        {"final_answer": "Supported answer.\n\nSOURCES_JSON {\"hidden\": true}"},
        good_result,
    )
    sla_improved, sla_used_fallback = chat_quality._ensure_improved_answer(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Can you tell me what was the SLA for this ticket 950010?",
                }
            ],
            "final_answer": (
                "The provided ticket details for #950033 do not include information about "
                "its SLA performance or targets."
            ),
        },
        good_result,
    )
    live_improved, live_used_fallback = chat_quality._ensure_improved_answer(
        {
            "messages": [{"role": "user", "content": "What are the top issues reported today?"}],
            "counts": {"zendesk_live_recent": 3},
            "final_answer": (
                "Based on live Zendesk tickets created today, the top issue is POS Terminal --> "
                "Dining Room."
            ),
        },
        ChatQualityReviewResult(
            quality="weak",
            confidence=0.4,
            problem_types=["weak_sources"],
            recommended_action="rewrite_with_existing_sources",
            reformulated_query=None,
            explanation="Weak answer.",
            user_visible_note="Could not confirm analytics.",
            improved_answer="applications__store_operations__scheduling couldn't retrieve from analytics database",
        ),
    )
    ok = (
        "clicked \"Improve answer\"" in chat_quality._REVIEW_PROMPT
        and "improved_answer\nMUST contain" in chat_quality._REVIEW_PROMPT
        and used_fallback is True
        and improved.improved_answer is not None
        and "не могу подтвердить" in improved.improved_answer
        and "повторный поиск" in improved.improved_answer
        and good_used_fallback is True
        and good_improved.improved_answer == "Supported answer."
        and sla_used_fallback is True
        and sla_improved.quality == "weak"
        and "official Zendesk SLA target or breach data" in (sla_improved.improved_answer or "")
        and live_used_fallback is True
        and live_improved.quality == "good"
        and "live Zendesk tickets created today" in (live_improved.improved_answer or "")
        and "applications__store_operations" not in (live_improved.improved_answer or "")
    )
    return _check(
        ok,
        "CHAT-025",
        "Improve Answer always returns a user-facing replacement and preserves live Zendesk answers",
    )


def case_chat_026() -> bool:
    import asyncio

    from app.models.chat_quality import ChatQualityReviewRequest, ChatQualityReviewResult
    from app.routers import universal
    from app.services import chat_quality, firestore

    weak_review = ChatQualityReviewResult(
        quality="insufficient_evidence",
        confidence=0.45,
        problem_types=["weak_sources"],
        recommended_action="reroute",
        reformulated_query="verify the date from ticket evidence",
        explanation="The first answer did not have enough evidence.",
        user_visible_note="I need to rerun evidence.",
        improved_answer="Trace-only fallback answer.",
    )
    refreshed_review = ChatQualityReviewResult(
        quality="weak",
        confidence=0.75,
        problem_types=["weak_sources"],
        recommended_action="rewrite_with_existing_sources",
        reformulated_query=None,
        explanation="Refreshed evidence supports a safer answer.",
        user_visible_note="I checked refreshed evidence.",
        improved_answer="Refreshed improved answer.",
    )
    calls: list[str] = []
    saved_events: list[dict] = []

    orig_get_trace = firestore.get_chat_answer_trace
    orig_review = chat_quality.review_answer_trace_with_telemetry
    orig_save_event = firestore.save_chat_quality_review_event
    orig_refresh = universal._build_review_refresh_trace

    async def fake_refresh(trace, review, loop):
        calls.append("refresh")
        return {
            **trace,
            "review_refresh": {
                "query": review.reformulated_query,
                "phase1_decision": {"tools": ["search_knowledge_base", "search_similar_tickets"]},
                "counts": {"kb": 1, "drive": 0, "similar": 1, "bq": 0, "same_store_snapshot": 1},
            },
        }

    def fake_review(trace, user_note=None):
        if trace.get("review_refresh"):
            calls.append("second_review")
            return refreshed_review, {"correlation_id": "refresh-corr"}
        calls.append("first_review")
        return weak_review, {"correlation_id": "initial-corr"}

    try:
        firestore.get_chat_answer_trace = lambda trace_id: {
            "trace_id": trace_id,
            "owner_uid": "user-1",
            "owner_email": "alex.morgan@northstar.example.com",
            "ticket_id": 950032,
            "target_ticket_id": 950032,
            "messages": [{"role": "user", "content": "Где ты нашел дату?"}],
            "final_answer": "Unsupported date answer.",
        }
        chat_quality.review_answer_trace_with_telemetry = fake_review
        firestore.save_chat_quality_review_event = lambda event: saved_events.append(event) or "review-refresh"
        universal._build_review_refresh_trace = fake_refresh

        result = asyncio.run(universal.review_universal_answer(
            ChatQualityReviewRequest(trace_id="trace-refresh"),
            {"uid": "user-1", "email": "alex.morgan@northstar.example.com", "role": "agent"},
        ))
    finally:
        firestore.get_chat_answer_trace = orig_get_trace
        chat_quality.review_answer_trace_with_telemetry = orig_review
        firestore.save_chat_quality_review_event = orig_save_event
        universal._build_review_refresh_trace = orig_refresh

    event = saved_events[-1] if saved_events else {}
    telemetry = event.get("telemetry") or {}
    ok = (
        universal._review_should_refresh_evidence(weak_review) is True
        and universal._review_should_refresh_evidence(refreshed_review) is False
        and universal._review_refresh_query({"messages": []}, weak_review) == "verify the date from ticket evidence"
        and result.improved_answer == "Refreshed improved answer."
        and calls == ["first_review", "refresh", "second_review"]
        and event.get("evidence_refresh_used") is True
        and telemetry.get("evidence_refresh", {}).get("used") is True
        and telemetry.get("evidence_refresh", {}).get("counts", {}).get("same_store_snapshot") == 1
    )
    return _check(
        ok,
        "CHAT-026",
        "Improve Answer reroute performs evidence refresh and second review",
    )


def case_chat_031() -> bool:
    from app.routers import universal
    from app.services import chat_policy

    true_questions = [
        "How many times has this category of issue been reported?",
        "how often does this type happen?",
        "how many tickets are there for this kind of issue?",
        "сколько раз такая категория встречалась?",
    ]
    false_questions = [
        "How many hard drive issues were reported?",
        "How many categories were reported this week?",
    ]
    sql = universal._contextual_category_count_sql(950034)
    result = bigquery.execute_query(sql)
    row = (result.get("rows") or [[None, None, 0, 0]])[0]
    ctx = "TICKET ID: #950034\nRESTAURANT: Summit Burger 2006 (Store #2006, Brand: Summit Burger)\nISSUE CATEGORY: Server --> Hard Drive"
    force_contextual_category_count = (
        chat_policy.should_use_open_ticket_context(true_questions[0])
        and chat_policy.is_contextual_category_frequency_followup(true_questions[0])
    )
    would_replace_with_store_snapshot = (
        not force_contextual_category_count
        and universal._should_build_store_context_snapshot(true_questions[0], ctx, None)
    )
    ok = (
        all(chat_policy.is_contextual_category_frequency_followup(q) for q in true_questions)
        and all(chat_policy.should_use_open_ticket_context(q) for q in true_questions)
        and all(chat_policy.needs_scoped_store_evidence(q) for q in true_questions)
        and all(not chat_policy.is_contextual_category_frequency_followup(q) for q in false_questions)
        and force_contextual_category_count
        and not would_replace_with_store_snapshot
        and "WHERE id = 950034" in sql
        and "issue_category" in sql
        and "total_matching_tickets" in sql
        and "matching_tickets_last_365_days" in sql
        and result.get("row_count") == 1
        and row[1] == "server__hard_drive"
        and int(row[2] or 0) > 0
    )
    return _check(
        ok,
        "CHAT-031",
        "contextual category/frequency follow-up uses open ticket issue_category instead of clarification",
    )


def case_chat_032() -> bool:
    bad_sql = """SELECT
  CASE
    WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN issue_category
    ELSE CONCAT('__subject__', TRIM(subject))
  END AS issue_topic,
  COUNT(*) AS ticket_count,
  STRING_AGG(FORMAT('#%d | %s | %s', id, subject, COALESCE(resolution, '')), '
' ORDER BY created_at DESC LIMIT 3) AS sample_tickets
FROM
  `esip-reference-project.esip_reference.tickets`
WHERE
  DATE(created_at, 'America/New_York') = '2026-05-08'
GROUP BY
  issue_topic
ORDER BY
  ticket_count DESC
LIMIT 3"""
    result = bigquery.execute_query(bad_sql)
    rows = result.get("rows") or []
    executed = result.get("sql_executed", "")
    ok = (
        result.get("row_count", 0) > 0
        and rows
        and "'\\n'" in executed
        and any(int(row[1] or 0) > 0 for row in rows)
    )
    return _check(
        ok,
        "CHAT-032",
        "Gemini literal-newline STRING_AGG delimiter is sanitized and May 8 analytics returns rows",
    )


def case_chat_033() -> bool:
    leaked = """TOOL CALL:

```json
{
  "tool_code": "print(analytics_tool.run(query_type='top_issues_by_date', date_range='yesterday'))"
}
```

DATA RETRIEVED:

```json
{
  "data": {
    "issue_topic": [
      "Applications --> Store Operations --> End Of Day",
      "Applications --> Store Operations --> Avst"
    ],
    "ticket_count": [10, 7]
  }
}
```

Yesterday, the most frequent issue was **Applications --> Store Operations --> End Of Day** with 10 tickets.
"""
    cleaned = universal._strip_internal_tool_blocks(leaked)
    ok = (
        "TOOL CALL" not in cleaned.upper()
        and "DATA RETRIEVED" not in cleaned.upper()
        and "tool_code" not in cleaned
        and "Yesterday, the most frequent issue" in cleaned
        and "Applications --> Store Operations --> End Of Day" in cleaned
    )
    return _check(
        ok,
        "CHAT-033",
        f"uppercase pseudo-tool transcript blocks are stripped while preserving final answer; cleaned={cleaned}",
    )


def case_chat_035() -> bool:
    answer = """On May 6, the top reported issue was **Uncategorized** with 32 tickets.

Beyond that, the most frequent categorized issues were:
- Applications --> Store Operations --> End Of Day (4 tickets)
- Workstation --> Maintenance --> Errors (2 tickets)
- **Payment Device --> Wired Payment Terminal --> Pinpad Not Ready** (3 tickets)
*   **New Voicemail: Support_Voicemail** (inferred from subject) with 3 tickets.

SOURCES_JSON: {"sources": []}
"""
    normalized = universal._normalize_visible_answer_markdown(answer)
    ok = (
        "- **Applications --> Store Operations --> End Of Day** (4 tickets)" in normalized
        and "- **Workstation --> Maintenance --> Errors** (2 tickets)" in normalized
        and "- **Payment Device --> Wired Payment Terminal --> Pinpad Not Ready** (3 tickets)" in normalized
        and "*   **New Voicemail: Support_Voicemail (inferred from subject)** with 3 tickets." in normalized
        and "****" not in normalized
        and 'SOURCES_JSON: {"sources": []}' in normalized
    )
    return _check(
        ok,
        "CHAT-035",
        f"counted analytics bullets get consistent bold labels without touching machine footers; normalized={normalized}",
    )


def case_chat_036() -> bool:
    rows = [
        {"issue_category": None, "ticket_count": 32},
        {"issue_category": "applications__store_operations__end_of_day", "ticket_count": 4},
        {"issue_category": "workstation__maintenance__errors", "ticket_count": 2},
        {"issue_category": "menu_maintenance__pos_takeout__add_new_button", "ticket_count": 2},
        {"issue_category": "server__database", "ticket_count": 2},
        {"issue_category": "infrastructure_issue__internet_connection__primary", "ticket_count": 2},
    ]
    answer = universal._maybe_build_deterministic_top_issues_answer(
        "what are the top issues on may 6?",
        rows,
    )
    bullet_lines = [
        line for line in (answer or "").splitlines()
        if line.lstrip().startswith(("- ", "* ", "• "))
    ]
    ok = (
        answer is not None
        and "**Uncategorized** had 32 tickets" in answer
        and "*   **Applications --> Store Operations --> End Of Day** with 4 tickets" in answer
        and "*   **Workstation --> Maintenance --> Errors** with 2 tickets" in answer
        and "*   **Menu Maintenance --> POS Takeout --> Add New Button** with 2 tickets" in answer
        and len(bullet_lines) >= 5
        and "SOURCES_JSON" in answer
        and "****" not in answer
    )
    return _check(
        ok,
        "CHAT-036",
        f"simple top-issues answer is rendered deterministically as one category per bullet; answer={answer}",
    )


def case_chat_037() -> bool:
    rows = [
        {"issue_category": "server__database", "ticket_count": 3},
        {"issue_category": "aggregators__door_dash", "ticket_count": 3},
    ]
    answer = universal._maybe_build_deterministic_top_issues_answer(
        "what are the top issues on may 10?",
        rows,
    )
    explicit_answer = universal._maybe_build_deterministic_top_issues_answer(
        "what are the top issues on may 10 2025?",
        rows,
    )
    ok = (
        answer is not None
        and "On May 10, 2026" in answer
        and "On May 10, the top reported issues" not in answer
        and explicit_answer is not None
        and "On May 10, 2025" in explicit_answer
    )
    return _check(
        ok,
        "CHAT-037",
        f"month-day analytics labels include an explicit year; answer={answer}; explicit={explicit_answer}",
    )


def case_chat_038() -> bool:
    today_window = universal._relative_ticket_analytics_window("How many tickets are created today?")
    yesterday_window = universal._relative_ticket_analytics_window("top stores that reported an issue yesterday")
    should_today = universal._should_run_live_zendesk_overlay("How many tickets are created today?", [{"ticket_count": 0}])
    old_note = universal._format_sync_freshness_note
    universal._format_sync_freshness_note = lambda: "BQ analytics are synced through test cursor."
    try:
        count_answer = universal._live_zendesk_recent_answer(
            "How many tickets are created today?",
            today_window,
            [
                {"id": 950001, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
                {"id": 950003, "issue_category": "applications__store_operations__end_of_day", "store_id": "2003", "org_name": "Frontier Kitchen 2003"},
            ],
        )
        store_answer = universal._live_zendesk_recent_answer(
            "stores reported most tickets today",
            today_window,
            [
                {"id": 950001, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
                {"id": 950002, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
                {"id": 950003, "issue_category": "applications__store_operations__end_of_day", "store_id": "2003", "org_name": "Frontier Kitchen 2003"},
            ],
        )
        issues_answer = universal._live_zendesk_recent_answer(
            "What are the top issues reported today",
            today_window,
            [
                {"id": 950001, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
                {"id": 950002, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
                {"id": 950003, "issue_category": None, "store_id": "2003", "org_name": "Frontier Kitchen 2003"},
            ],
        )
        most_answer = universal._live_zendesk_recent_answer(
            "what were the most issues reported today?",
            today_window,
            [
                {"id": 950001, "issue_category": "server__database", "store_id": "2001", "org_name": "Cedar Grill 2001"},
            ],
        )
        list_answer = universal._live_zendesk_recent_answer(
            "List tickets created today",
            today_window,
            [
                {
                    "id": 950037,
                    "subject": "support workstation is slow",
                    "issue_category": "workstation__maintenance__slow_performance",
                    "store_id": "2008",
                    "org_name": "Piazza Kitchen 2008",
                    "status": "new",
                },
                {
                    "id": 950038,
                    "subject": "Menu price issue",
                    "issue_category": "menu_maintenance__pos_takeout__incorrect_price",
                    "store_id": "2003",
                    "org_name": "Frontier Kitchen 2003",
                    "status": "open",
                },
            ],
        )
        incident_list_answer = universal._live_zendesk_recent_answer(
            "List all ticket numbers for tickets logged today that are incidents",
            today_window,
            [
                {
                    "id": 950039,
                    "subject": "Internet issue",
                    "issue_category": "infrastructure_issue__internet_connection__primary",
                    "store_id": "2004",
                    "org_name": "Lakeside Tavern 2004",
                    "status": "new",
                },
            ],
        )
        incident_semantic = semantic_interpreter.interpret_latest_turn([
            {
                "role": "user",
                "content": "List all ticket numbers for tickets logged today that are incidents",
            }
        ])
        incident_event_request = universal._looks_like_global_event_discovery(
            "List all ticket numbers for tickets logged today that are incidents",
            incident_semantic.normalized_question_en,
            incident_semantic,
            explicit_store_context=None,
            explicit_ticket_id=None,
            prior_event_text="",
        )
        incident_generic_issue = universal._issue_filter_is_generic_ticket_noun(
            "List all ticket numbers for tickets logged today that are incidents",
            (incident_semantic.entities or {}).get("issue_or_category"),
        )
        cards = universal._live_zendesk_recent_ticket_cards([
            {
                "id": 950037,
                "subject": "support workstation is slow",
                "issue_category": "workstation__maintenance__slow_performance",
                "store_id": "2008",
                "org_name": "Piazza Kitchen 2008",
                "status": "new",
            }
        ])
    finally:
        universal._format_sync_freshness_note = old_note

    ok = (
        today_window is not None
        and yesterday_window is not None
        and should_today is not None
        and "**2** created tickets" in (count_answer or "")
        and "**Cedar Grill 2001 / Store 2001** with 2 tickets" in (store_answer or "")
        and "**Server --> Database** with 2 tickets" in (issues_answer or "")
        and "**Server --> Database** with 1 ticket" in (most_answer or "")
        and "Live Zendesk" in (issues_answer or "")
        and "#950037 support workstation is slow (new) - Piazza Kitchen 2008 / Store 2008" in (list_answer or "")
        and "#950038 Menu price issue (open) - Frontier Kitchen 2003 / Store 2003" in (list_answer or "")
        and incident_event_request is None
        and incident_generic_issue is True
        and "#950039 Internet issue (new) - Lakeside Tavern 2004 / Store 2002" in (incident_list_answer or "")
        and "keyword_review" not in (incident_list_answer or "")
        and "no tickets" not in (list_answer or "").lower()
        and len(cards) == 1
        and cards[0]["id"] == 950037
        and cards[0]["store_id"] == "2008"
        and "SOURCES_JSON" in (count_answer or "")
        and "SOURCES_JSON" in (store_answer or "")
        and "SOURCES_JSON" in (issues_answer or "")
        and "SOURCES_JSON" in (list_answer or "")
        and "SOURCES_JSON" in (incident_list_answer or "")
    )
    return _check(
        ok,
        "CHAT-038",
        "today/yesterday ticket analytics can use bounded live Zendesk overlay instead of confident stale BQ zero",
    )


def case_chat_039() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 5, 13, 10, 25, tzinfo=ZoneInfo("America/New_York"))

    def resolve(question: str, history: list[dict] | None = None):
        return temporal_resolver.resolve_temporal_context(
            [*(history or []), {"role": "user", "content": question}],
            now=now,
        )

    checks = []
    cases = [
        ("What are the top issues reported today?", "May 13, 2026", "top_issues", "single_day"),
        ("How many tickets are created yesterday?", "May 12, 2026", "ticket_count", "single_day"),
        ("How many tickets were updated within last 5 days?", "May 9-May 13, 2026", "ticket_count", "rolling_range"),
        ("How many tickets are created 2 days ago?", "May 11, 2026", "ticket_count", "single_day"),
        ("What are the top issues last Friday?", "May 8, 2026", "top_issues", "single_day"),
        ("What are the top issues this week?", "May 11-May 17, 2026", "top_issues", "calendar_week"),
        ("What are the top issues last week?", "May 4-May 10, 2026", "top_issues", "calendar_week"),
        ("What are the top issues within last 5 days?", "May 9-May 13, 2026", "top_issues", "rolling_range"),
        ("How n)may tickets were opened withon last 5 days?", "May 9-May 13, 2026", "ticket_count", "rolling_range"),
        ("What are the top issues on May 8?", "May 8, 2026", "top_issues", "single_day"),
        ("What are the top issues in April?", "April 1-April 30, 2026", "top_issues", "calendar_month"),
        ("What are the top issues last month?", "April 1-April 30, 2026", "top_issues", "calendar_month"),
        ("How many tickets were created this month?", "May 1-May 13, 2026", "ticket_count", "calendar_month"),
        ("How many tickets were created in May 2026?", "May 1-May 13, 2026", "ticket_count", "calendar_month"),
        ("How many tickets were created on May 8 2021?", "May 8, 2021", "ticket_count", "single_day"),
    ]
    for question, label, intent, period_type in cases:
        resolved = resolve(question)
        checks.append(
            resolved is not None
            and resolved.display_label == label
            and resolved.intent == intent
            and resolved.period_type == period_type
            and ("updated_at" in temporal_resolver.build_bq_sql(resolved) if "updated" in question.lower() else True)
        )

    history = [
        {"role": "user", "content": "What are the top issues on April 10?"},
        {"role": "assistant", "content": "On April 10, 2026, top issues included POS terminals."},
        {"role": "user", "content": "What are the top issues on May 8?"},
        {"role": "assistant", "content": "On May 8, 2026, top issues included Order Portal reset requests."},
    ]
    chain = resolve("How many tickets were created that day?", history)
    chain_stores = resolve("Which stores reported the most tickets?", history)
    sql = temporal_resolver.build_bq_sql(chain) if chain else ""
    stores_sql = temporal_resolver.build_bq_sql(chain_stores) if chain_stores else ""
    checks.append(
        chain is not None
        and chain.intent == "ticket_count"
        and chain.display_label == "May 8, 2026"
        and "DATE '2026-05-08'" in sql
        and "CURRENT_TIMESTAMP" not in sql
    )
    checks.append(
        chain_stores is not None
        and chain_stores.intent == "top_stores"
        and chain_stores.display_label == "May 8, 2026"
        and "DATE '2026-05-08'" in stores_sql
    )

    last_friday = resolve("What are the top issues last Friday?")
    last_friday_sql = temporal_resolver.build_bq_sql(last_friday) if last_friday else ""
    checks.append(
        last_friday is not None
        and "DATE '2026-05-08'" in last_friday_sql
        and "DAYOFWEEK" not in last_friday_sql
    )

    return _check(
        all(checks),
        "CHAT-039",
        "temporal resolver normalizes Eastern dates, chains, and backend-owned BQ date filters",
    )


def case_chat_044() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 5, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    period = temporal_resolver.resolve_period_text(
        "Look at tickets for this restaurant from the beginning of January 2024 to the end of 2024.",
        now=now,
    )
    if not period:
        return _check(False, "CHAT-044", "explicit 2024 store-history period did not resolve")

    rows = bigquery.fetch_store_history_snapshot(
        "2009",
        start_date=period.start_date.isoformat(),
        end_date=period.end_date_exclusive.isoformat(),
        limit=160,
    )
    ids = {int(row.get("id")) for row in rows if row.get("id")}
    years = {str(row.get("created_at", ""))[:4] for row in rows if row.get("created_at")}
    examples = bigquery.fetch_store_ticket_examples(
        "2009",
        terms=["internet", "wifi", "network"],
        start_date=period.start_date.isoformat(),
        end_date=period.end_date_exclusive.isoformat(),
        limit=50,
    )
    example_ids = {int(row.get("id")) for row in examples if row.get("id")}
    ok = (
        period.start_date.isoformat() == "2024-01-01"
        and period.end_date_exclusive.isoformat() == "2025-01-01"
        and years == {"2024"}
        and {950002, 950003, 950004, 950005}.issubset(ids)
        and bool({950002, 950003, 950004, 950005}.intersection(example_ids))
        and 950008 not in ids
        and 950013 not in ids
    )
    return _check(
        ok,
        "CHAT-044",
        f"same-store history uses exact 2024 window; rows={len(rows)} ids_sample={sorted(ids)[:4]}...",
    )


def case_chat_045() -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 5, 14, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    full_year = temporal_resolver.resolve_period_text("Analyze this restaurant during 2024", now=now)
    last_year = temporal_resolver.resolve_period_text("Compare network issues for this restaurant last year", now=now)
    start_at, end_at = universal._store_history_window_for_zendesk(last_year, 1200)
    converted = universal._store_context_snapshot_rows(
        universal._zendesk_store_history_rows_to_snapshot_rows([
            {
                "id": 950005,
                "subject": "Store fire - cables damaged",
                "status": "closed",
                "created_at": start_at,
                "updated_at": start_at,
                "store_id": "2009",
                "org_name": "City Fries 2009",
                "brand": "City Fries",
                "issue_category": "network__meraki_firewall__bad_cable",
            }
        ]),
        "Keyword-free same-store snapshot from Zendesk. Scope: last year.",
    )
    ok = (
        full_year is not None
        and full_year.start_date.isoformat() == "2024-01-01"
        and full_year.end_date_exclusive.isoformat() == "2025-01-01"
        and last_year is not None
        and last_year.start_date.isoformat() == "2025-01-01"
        and last_year.end_date_exclusive.isoformat() == "2026-01-01"
        and start_at.date().isoformat() == "2025-01-01"
        and end_at.date().isoformat() == "2026-01-01"
        and converted[0]["source"] == "zendesk"
        and converted[0]["id"] == 950005
        and "Scope" in converted[0]["scope_note"]
    )
    return _check(
        ok,
        "CHAT-045",
        "same-store before/after helpers preserve explicit windows and Zendesk source boundaries",
    )


def case_chat_046() -> bool:
    previous_answer = (
        "Since the beginning of 2026, City Fries 2009 has consistently reported issues including "
        "degraded internet speed, lagging Wi-Fi, dropping and inconsistent connections, and "
        "complete primary internet outages or WAN 2 failures."
    )
    question = "Сколько тикетов по каждой из перечисленных категорий за 2026 год"
    request = semantic_label_counts.build_label_request(
        latest_question=question,
        routing_question="How many tickets were there in each of the listed categories for 2026?",
        previous_assistant_text=previous_answer,
    )
    if not request:
        return _check(False, "CHAT-046", "listed semantic labels were not extracted from prior answer")
    explicit_labels = [
        "degraded internet speed",
        "lagging Wi-Fi",
        "dropping connections",
        "inconsistent connections",
        "complete primary internet outages",
    ]
    explicit_question = """В таколм случае дай мне уверенный отчет и количество таикетов по кажой из категорий в течение 2026 года
degraded internet speed
lagging Wi-Fi
dropping connections
inconsistent connections
complete primary internet outages"""
    explicit_request = semantic_label_counts.build_label_request(
        latest_question=explicit_question,
        routing_question=explicit_question,
        previous_assistant_text="",
    )
    period = temporal_resolver.resolve_period_text("How many tickets are there for each category for the year 2026?")

    rows = bigquery.fetch_store_tickets_for_semantic_label_count(
        "2009",
        start_date="2026-01-01",
        end_date="2027-01-01",
        limit=500,
    )
    results = semantic_label_counts.count_labels(explicit_labels, rows)
    by_label = {result.label: set(result.ticket_ids) for result in results}
    answer = semantic_label_counts.build_answer(
        store_id="2008",
        period_label="January 1-December 31, 2026",
        results=results,
        answer_language="Russian",
    )
    ok = (
        request.source == "previous_answer"
        and explicit_request is not None
        and explicit_request.source == "current_question"
        and explicit_request.labels == explicit_labels
        and period is not None
        and period.start_date.isoformat() == "2026-01-01"
        and period.end_date_exclusive.isoformat() == "2027-01-01"
        and {"degraded internet speed", "lagging Wi-Fi", "dropping connections", "inconsistent connections"}.issubset(
            set(request.labels)
        )
        and {950032, 950029, 950013}.issubset(by_label["degraded internet speed"])
        and {950032, 950029}.issubset(by_label["lagging Wi-Fi"])
        and 950019 not in by_label["lagging Wi-Fi"]
        and {950032, 950013, 950019}.issubset(by_label["dropping connections"])
        and {950032, 950013}.issubset(by_label["inconsistent connections"])
        and 950023 in by_label["complete primary internet outages"]
        and 950018 not in by_label["complete primary internet outages"]
        and "смысловые типы жалоб" in answer
        and "WiFi Issues" not in answer
        and "TICKETS_JSON" not in answer.split("SOURCES_JSON", 1)[0]
    )
    return _check(
        ok,
        "CHAT-046",
        "semantic complaint-label follow-up preserves prior labels, counts overlaps, and uses ticket comments",
    )


def case_chat_047() -> bool:
    question = "Сколько тикетов по каждой из перечисленных категорий за 2026 год"
    memory = {
        "semantic_labels": [
            "degraded internet speed",
            "lagging Wi-Fi",
            "dropping connections",
            "inconsistent connections",
            "complete primary internet outages",
        ],
        "store_id": "2009",
    }
    semantic_frame = {
        "answer_language": "Russian",
        "confidence": "high",
        "target_context": "prior_turn",
        "task": {
            "task_type": "count_by_dimension",
            "dimension_type": "semantic_labels",
            "dimension_source": "prior_turn",
            "labels": [],
            "overlap_allowed": True,
        },
        "entities": {"store_id": None, "date_phrase": "2026"},
    }
    contract = semantic_task_contracts.build_task_contract(
        latest_question=question,
        routing_question="How many tickets were there in each of the listed categories for 2026?",
        previous_assistant_text="",
        semantic_frame=semantic_frame,
        conversation_entities=memory,
        explicit_store_context=None,
        open_ticket_store_id=None,
        messages=[
            {"role": "assistant", "content": "[truncated answer without labels]"},
            {"role": "user", "content": question},
        ],
    )
    introduced_labels = semantic_label_counts.extract_labels_from_answer(
        "Since 2026, City Fries 2009 has reported issues with **network connectivity**, "
        "specifically **lagging and dropping Wi-Fi**, inconsistent connections, and degraded speeds. "
        "These problems impacted devices, including PATT terminals, POS systems, and payment terminals."
    )
    ok = (
        contract is not None
        and contract.task_type == "count_by_dimension"
        and contract.dimension_type == "semantic_labels"
        and contract.dimension_source == "conversation_memory"
        and contract.store_id == "2008"
        and contract.period is not None
        and contract.period.start_date == "2026-01-01"
        and contract.period.end_date_exclusive == "2027-01-01"
        and contract.labels == memory["semantic_labels"]
        and contract.overlap_allowed
        and contract.requires_ticket_ids
        and {"lagging Wi-Fi", "dropping Wi-Fi", "inconsistent connections", "degraded speeds"}.issubset(
            set(introduced_labels)
        )
    )
    return _check(
        ok,
        "CHAT-047",
        "semantic task contract can use conversation-memory labels when prior answer text is unavailable",
    )


def case_chat_048() -> bool:
    latest = """Count tickets for each of these categories in 2026:
primary internet outages
WAN 2 failures
payment terminal communication errors"""
    memory = {
        "semantic_labels": ["old degraded speed", "old Wi-Fi label"],
        "store_id": "2009",
    }
    semantic_frame = {
        "answer_language": "English",
        "confidence": "high",
        "task": {
            "task_type": "count_by_dimension",
            "dimension_type": "semantic_labels",
            "dimension_source": "current_question",
            "labels": ["primary internet outages", "WAN 2 failures", "payment terminal communication errors"],
            "overlap_allowed": True,
        },
        "entities": {},
    }
    contract = semantic_task_contracts.build_task_contract(
        latest_question=latest,
        routing_question=latest,
        previous_assistant_text="",
        semantic_frame=semantic_frame,
        conversation_entities=memory,
        explicit_store_context=None,
        open_ticket_store_id=None,
        messages=[{"role": "user", "content": latest}],
    )
    ok = (
        contract is not None
        and contract.dimension_source == "current_question"
        and contract.labels == [
            "primary internet outages",
            "WAN 2 failures",
            "payment terminal communication errors",
        ]
        and "old degraded speed" not in (contract.labels or [])
    )
    return _check(
        ok,
        "CHAT-048",
        "current-question semantic labels override stale conversation memory",
    )


def case_chat_049() -> bool:
    question = "For store 2008 in 2026, count tickets by official Zendesk issue_category."
    semantic_frame = {
        "answer_language": "English",
        "confidence": "high",
        "task": {
            "task_type": "count_by_dimension",
            "dimension_type": "issue_category",
            "dimension_source": "official_field",
            "labels": [],
            "overlap_allowed": False,
        },
        "entities": {"store_id": "2009", "date_phrase": "2026"},
    }
    contract = semantic_task_contracts.build_task_contract(
        latest_question=question,
        routing_question=question,
        previous_assistant_text="",
        semantic_frame=semantic_frame,
        conversation_entities={"semantic_labels": ["degraded internet speed"], "store_id": "2009"},
        explicit_store_context="2009",
        open_ticket_store_id=None,
        messages=[{"role": "user", "content": question}],
    )
    return _check(
        contract is None,
        "CHAT-049",
        "official Zendesk issue_category requests do not force semantic-label counting",
    )


def case_chat_050() -> bool:
    question = """For store 2008, count tickets for each of these listed issues in 2026:
degraded internet speed
lagging Wi-Fi
dropping connections"""
    semantic_frame = {
        "answer_language": "English",
        "confidence": "high",
        "task": {
            "task_type": "count_by_dimension",
            "dimension_type": "semantic_labels",
            "dimension_source": "current_question",
            "labels": ["degraded internet speed", "lagging Wi-Fi", "dropping connections"],
            "overlap_allowed": True,
        },
        "entities": {"store_id": "2009", "date_phrase": "2026"},
    }
    contract = semantic_task_contracts.build_task_contract(
        latest_question=question,
        routing_question=question,
        previous_assistant_text="",
        semantic_frame=semantic_frame,
        conversation_entities=None,
        explicit_store_context="2009",
        open_ticket_store_id=None,
        messages=[{"role": "user", "content": question}],
    )
    force_semantic_label_count = bool(contract and contract.dimension_type == "semantic_labels")
    raw_store_breakdown = universal._is_store_issue_breakdown_question(question)
    effective_store_breakdown = raw_store_breakdown and not force_semantic_label_count
    ok = (
        contract is not None
        and contract.dimension_type == "semantic_labels"
        and contract.store_id == "2008"
        and raw_store_breakdown
        and not effective_store_breakdown
    )
    return _check(
        ok,
        "CHAT-050",
        "explicit store + listed semantic labels suppresses generic store issue breakdown",
    )


def case_chat_051() -> bool:
    messages = [
        {"role": "user", "content": "what were the top issues on May 10?"},
        {"role": "assistant", "content": "On May 10, 2026, the top issues were POS and network."},
        {"role": "user", "content": "what about May 8?"},
        {"role": "assistant", "content": "On May 8, 2026, the top issues were payment and network."},
        {"role": "user", "content": "today"},
        {"role": "assistant", "content": "Today, May 19, 2026, there are recent tickets."},
        {"role": "user", "content": "yesterday"},
        {"role": "assistant", "content": "Yesterday, May 18, 2026, there were recent tickets."},
        {"role": "user", "content": "в каком ресторане у нас был пожар?"},
    ]
    semantic = semantic_interpreter.interpret_latest_turn(messages)
    request = universal._looks_like_global_event_discovery(
        messages[-1]["content"],
        semantic.normalized_question_en,
        semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
        prior_event_text="",
    )
    rows = bigquery.discover_global_event_tickets(
        (request or {}).get("terms") or [],
        limit=700,
    )
    confirmed = [row for row in rows if row.get("event_match_type") == "confirmed_candidate"]
    noise = [row for row in rows if row.get("event_match_type") == "noise"]
    confirmed_ids = {int(row.get("id") or 0) for row in confirmed}
    answer = universal._global_event_discovery_answer(
        request or {"terms": ["fire"], "query_label": "fire", "period": {"label": "all indexed ticket history"}},
        rows,
        "Russian",
    )
    ok = (
        request is not None
        and request.get("terms") == ["fire"]
        and (request.get("period") or {}).get("start_date") is None
        and {950004, 950005, 950010, 950012}.issubset(confirmed_ids)
        and any("fire orders" in str(row.get("evidence_snippet") or "").lower() for row in noise)
        and "Подтвержденные кандидаты" in answer
        and "**Коротко:**" in answer
        and "| Ресторан/магазин | Тикеты | Признак в тексте |" in answer
        and "| Тип | Кол-во | Почему |" in answer
        and "fire orders" in answer
        and "482" not in answer
        and "https://example.zendesk" not in answer
        and "![]" not in answer
        and "SOURCES_JSON" in answer
    )
    return _check(
        ok,
        "CHAT-051",
        "global event discovery finds store-level fire candidates and separates noisy keyword uses",
    )


def case_chat_052() -> bool:
    messages = [
        {"role": "user", "content": "в каком ресторане у нас был пожар?"},
        {"role": "assistant", "content": "Я нашел несколько кандидатов, но нужно проверить период."},
        {"role": "user", "content": "Проверь за последние пару лет"},
    ]
    semantic = semantic_interpreter.interpret_latest_turn(messages)
    request = universal._looks_like_global_event_discovery(
        messages[-1]["content"],
        semantic.normalized_question_en,
        semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
        prior_event_text=messages[-3]["content"],
    )
    period = (request or {}).get("period") or {}
    rows = bigquery.discover_global_event_tickets(
        (request or {}).get("terms") or [],
        start_date=period.get("start_date"),
        end_date=period.get("end_date"),
        limit=700,
    )
    answer = universal._global_event_discovery_answer(request or {}, rows, "Russian")
    confirmed = [row for row in rows if row.get("event_match_type") == "confirmed_candidate"]
    confirmed_dates = {str(row.get("created_at") or "")[:4] for row in confirmed}
    typo_question = "Есть ли у нас зафикисиоованные случаи поджаров в любом из рестаранов за посление 2 года?"
    typo_semantic = semantic_interpreter.interpret_latest_turn([{"role": "user", "content": typo_question}])
    typo_request = universal._looks_like_global_event_discovery(
        typo_question,
        typo_semantic.normalized_question_en,
        typo_semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
    )
    typo_rows = bigquery.discover_global_event_tickets(
        (typo_request or {}).get("terms") or [],
        start_date=((typo_request or {}).get("period") or {}).get("start_date"),
        end_date=((typo_request or {}).get("period") or {}).get("end_date"),
        limit=700,
    )
    typo_answer = universal._global_event_discovery_answer(typo_request or {}, typo_rows, "Russian")
    typo_bad_terms = [
        "зафикисиоованные",
        "поджаров",
        "рестаранов",
        "fire, been",
        "last 2 years",
        "https://example.zendesk",
        "![]",
    ]
    today = datetime.now(ZoneInfo("America/New_York")).date()
    expected_start_date = (today - timedelta(days=365 * 2)).isoformat()
    expected_end_date = (today + timedelta(days=1)).isoformat()
    ok = (
        request is not None
        and request.get("terms") == ["fire"]
        and period.get("start_date") == expected_start_date
        and period.get("end_date") == expected_end_date
        and "last 2 years" in period.get("label", "")
        and "2024-12-15" in answer
        and "2025-11-05" in answer
        and confirmed_dates.issubset({"2024", "2025", "2026"})
        and typo_request is not None
        and typo_request.get("terms") == ["fire"]
        and "пожары" in typo_answer
        and "последние 2 года" in typo_answer
        and "| Ресторан/магазин | Тикеты | Признак в тексте |" in typo_answer
        and "| Тип | Кол-во | Почему |" in typo_answer
        and all(term not in typo_answer for term in typo_bad_terms)
        and "SOURCES_JSON" in answer
    )
    return _check(
        ok,
        "CHAT-052",
        "follow-up preserves event topic, applies two-year window, and answers only with retrieved ticket dates",
    )


def case_chat_053() -> bool:
    question = "Ok. Can you tell me the average SLA by priorities for the analyst group?"
    limitation = universal._analytics_schema_limitation(question)
    substitute_sql = universal._schema_limitation_resolution_sql(question, limitation)
    answer = universal._analytics_schema_limitation_answer(question, limitation, [])
    valid_sql = """
    SELECT
      t.priority,
      COUNT(*) AS ticket_count,
      ROUND(AVG(t.resolution_sla_elapsed_hours), 1) AS avg_resolution_sla_elapsed_hours,
      COUNTIF(t.resolution_sla_breached) AS breached_count
    FROM `esip-reference-project.esip_reference.tickets` AS t
    WHERE t.assignee_group_name LIKE '%Analyst%'
      AND t.resolution_sla_target_hours IS NOT NULL
    GROUP BY t.priority
    """
    bad_sql = """
    SELECT
      t.priority,
      AVG(TIMESTAMP_DIFF(t.solved_at, t.created_at, MINUTE)) AS average_sla_minutes
    FROM `esip-reference-project.esip_reference.tickets` AS t
    GROUP BY t.priority
    """
    try:
        bigquery.execute_query(bad_sql)
        bad_sql_blocked = False
        bad_sql_error = ""
    except ValueError as exc:
        bad_sql_blocked = True
        bad_sql_error = str(exc)

    try:
        bigquery._reject_unavailable_ticket_fields(valid_sql)
        valid_sql_allowed = True
    except ValueError:
        valid_sql_allowed = False

    ok = (
        limitation is None
        and substitute_sql == ""
        and answer is None
        and "priority" in universal_chat._BQ_SCHEMA_HINT.split("- ticket_comments", 1)[0]
        and "resolution_sla_target_hours" in universal_chat._BQ_SCHEMA_HINT
        and "resolution_sla_breached" in universal_chat._BQ_SCHEMA_HINT
        and valid_sql_allowed
        and bad_sql_blocked
        and "supported SLA fields" in bad_sql_error
    )
    return _check(
        ok,
        "CHAT-053",
        "SLA/priority analytics uses supported derived SLA fields and blocks invented raw SLA aliases",
    )


def case_chat_064() -> bool:
    analyst_question = "Ok. Can you tell me the average SLA by priorities for the analyst group?"
    high_met_question = "Show me any high-priority incident tickets in the current SLA data that met SLA, with elapsed hours."
    all_time_question = "Do we have enough historical SLA data now to calculate a reliable all-time SLA breach rate across all tickets?"
    raw_field_question = "Please calculate SLA using average_sla_minutes or raw sla fields if available."

    analyst_sql = universal._sla_analytics_sql(analyst_question)
    high_met_sql = universal._sla_analytics_sql(high_met_question)
    all_time_sql = universal._sla_analytics_sql(all_time_question)
    raw_sql = universal._sla_analytics_sql(raw_field_question)
    raw_answer = universal._sla_supported_fields_answer(raw_field_question) or ""
    all_time_answer = universal._sla_analytics_answer(
        all_time_question,
        [{
            "total_tickets": 83200,
            "tickets_with_loaded_sla": 151,
            "breached_loaded_sla_tickets": 100,
            "met_loaded_sla_tickets": 51,
            "loaded_sla_breach_rate_pct": 66.23,
        }],
    ) or ""
    high_met_answer = universal._sla_analytics_answer(
        high_met_question,
        [{
            "id": 950042,
            "subject": "All the pos are asking for backup mode",
            "resolution_sla_target_hours": 12,
            "resolution_sla_elapsed_hours": 8.2,
            "resolution_sla_status": "met",
        }],
    ) or ""
    visible = universal._strip_trace_footers("Answer\n\nSOURCES_JSON: {\"sources\": []}")

    ok = (
        "LIKE '%analyst%'" in analyst_sql
        and "resolution_sla_elapsed_hours" in analyst_sql
        and "ticket_semantic" not in high_met_sql.lower()
        and "priority" in high_met_sql
        and "resolution_sla_status = 'met'" in high_met_sql
        and "COUNTIF(resolution_sla_status IS NOT NULL)" in all_time_sql
        and raw_sql == ""
        and "average_sla_minutes" in raw_answer
        and "resolution_sla_target_hours" in raw_answer
        and "No. ESIP does not yet have enough loaded historical SLA data" in all_time_answer
        and "currently loaded SLA rows" in all_time_answer
        and "#950042" in high_met_answer
        and "elapsed 8.2h" in high_met_answer
        and visible == "Answer"
    )
    return _check(
        ok,
        "CHAT-064",
        "SLA analytics guard handles partial coverage, raw-field explanation, BQ routing, and footer stripping",
    )


def case_chat_065() -> bool:
    from app.services import chat_policy, pos_type_analytics

    question = (
        "Summarize all tickets for stores where POS Type = Simphony. "
        "Identify repeated problems and repeated stores."
    )
    request = chat_policy.extract_pos_type_profile_analytics_request(question)
    queries = pos_type_analytics.build_pos_type_analytics_queries("Simphony")
    sql = "\n".join(query for query, _ in queries.values())
    ok = (
        request is not None
        and request.mode == "single"
        and request.pos_type == "Simphony"
        and "zendesk_organization_profiles" in universal_chat._BQ_SCHEMA_HINT
        and "store_profiles.pos_type" in sql
        and "t.org_id = store_profiles.org_id" in sql
        and "t.pos_type" not in sql
    )
    return _check(
        ok,
        "CHAT-065",
        "POS Type profile analytics routes to organization profile join instead of tickets.pos_type",
    )


def case_chat_066() -> bool:
    from app.services import chat_policy

    question = "Which Simphony stores have the most repeated issues in the last 90 days?"
    request = chat_policy.extract_pos_type_profile_analytics_request(question)
    ok = (
        request is not None
        and request.mode == "single"
        and request.pos_type == "Simphony"
        and request.start_date is not None
        and request.end_date is not None
    )
    return _check(
        ok,
        "CHAT-066",
        "Simphony-store shorthand routes to POS Type profile analytics with a date window",
    )


def case_chat_070() -> bool:
    from app.services import chat_policy

    question = "Search tickets that mention the word Simphony."
    request = chat_policy.extract_pos_type_profile_analytics_request(question)
    ok = request is None
    return _check(
        ok,
        "CHAT-070",
        "text mention search for Simphony is not reinterpreted as POS Type profile filtering",
    )


def case_chat_055() -> bool:
    file_id = "REFERENCE_SPREADSHEET_FILE_ID_000001"
    question = f"https://docs.google.com/spreadsheets/d/{file_id}/edit#gid=0"
    parsed = universal._drive_file_id_from_url(question)
    unavailable = universal._maybe_build_drive_url_unavailable_answer(question, [])
    available = universal._maybe_build_drive_url_unavailable_answer(question, [{"file_name": "Indexed.xlsx"}])
    ok = (
        parsed == file_id
        and unavailable is not None
        and "not currently available" in unavailable
        and file_id in unavailable
        and "arbitrary external Drive URLs" in unavailable
        and available is None
    )
    return _check(
        ok,
        "CHAT-055",
        "Drive/Sheets URLs are parsed by file id and unknown files get a specific indexed-source limitation answer",
    )


def _first_numeric_cell(rows: list) -> int | None:
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        for value in first.values():
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
    if isinstance(first, (list, tuple)):
        for value in first:
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def _status_filter_values(sql: str) -> set[str]:
    match = re.search(r"\bstatus\s+IN\s*\((?P<items>[^)]+)\)", sql, flags=re.IGNORECASE)
    if not match:
        return set()
    return {item.lower() for item in re.findall(r"'([^']+)'", match.group("items"))}


def _dict_rows(result: dict) -> list[dict]:
    columns = result.get("columns") or []
    return [dict(zip(columns, row)) for row in (result.get("rows") or [])]


def case_chat_056() -> bool:
    base_question = "list me the current tickets with a store id between 2000 and 2999"
    count_question = 'how many are assigned to the vendor "field_services_partner"'
    list_question = "what are those"
    base_messages = [{"role": "user", "content": base_question}]
    count_messages = [
        {"role": "user", "content": base_question},
        {"role": "assistant", "content": "I found current tickets for store IDs between 2000 and 2999."},
        {"role": "user", "content": count_question},
    ]
    list_messages = [
        *count_messages,
        {"role": "assistant", "content": "There are matching current Field Services Partner tickets in that store-id range."},
        {"role": "user", "content": list_question},
    ]

    decisions = [decide_tools(messages) for messages in (base_messages, count_messages, list_messages)]
    sqls = [str(decision.get("bq_sql") or "") for decision in decisions]
    results = [bigquery.execute_query(sql) for sql in sqls]
    executed = [str(result.get("sql_executed") or "") for result in results]
    guard_sqls = [
        universal._count_list_filter_consistency_sql(messages, messages[-1]["content"])
        for messages in (base_messages, count_messages, list_messages)
    ]
    guard_results = [bigquery.execute_query(sql or "") for sql in guard_sqls]
    guard_executed = [str(result.get("sql_executed") or "") for result in guard_results]
    guard_count_answer = universal._maybe_build_count_list_filter_answer(
        guard_sqls[1],
        _dict_rows(guard_results[1]),
    )
    guard_list_answer = universal._maybe_build_count_list_filter_answer(
        guard_sqls[2],
        _dict_rows(guard_results[2]),
    )
    count_value = _first_numeric_cell(results[1].get("rows") or [])
    list_count = int(results[2].get("row_count") or 0)
    guard_count_value = _first_numeric_cell(guard_results[1].get("rows") or [])
    guard_list_count = int(guard_results[2].get("row_count") or 0)
    guard_list_ids = {
        str(row.get("id"))
        for row in _dict_rows(guard_results[2])
        if row.get("id") is not None
    }
    guard_count_answer_has_value = bool(
        guard_count_answer
        and guard_count_value is not None
        and re.search(rf"\b{re.escape(str(guard_count_value))}\b", guard_count_answer)
    )
    guard_list_answer_has_ids = bool(
        guard_list_answer
        and guard_list_ids
        and all(f"#{ticket_id}" in guard_list_answer for ticket_id in sorted(guard_list_ids))
    )
    quoted_range = all(
        re.search(r"\bstore_id\s+BETWEEN\s+'2000'\s+AND\s+'2999'", sql, flags=re.IGNORECASE)
        for sql in executed + guard_executed
    )
    no_unquoted_range = not any(
        re.search(r"\bstore_id\s+BETWEEN\s+2000\s+AND\s+2999\b", sql, flags=re.IGNORECASE)
        for sql in executed + guard_executed
    )
    current_statuses = all(_status_filter_values(sql) == {"open", "pending", "hold"} for sql in executed + guard_executed)
    group_filters = all(
        re.search(
            r"\bLOWER\s*\(\s*(?:\w+\.)?assignee_group_name\s*\)\s*=\s*'field_services_partner'",
            sql,
            flags=re.IGNORECASE,
        )
        for sql in executed[1:] + guard_executed[1:]
    )
    no_org_vendor = not any(re.search(r"\borg_name\s*=", sql, flags=re.IGNORECASE) for sql in executed[1:] + guard_executed[1:])
    ok = (
        all("run_bq_query" in (decision.get("tools") or []) for decision in decisions)
        and all(guard_sqls)
        and quoted_range
        and no_unquoted_range
        and current_statuses
        and group_filters
        and no_org_vendor
        and count_value == list_count
        and guard_count_value == guard_list_count
        and count_value == guard_count_value
        and count_value is not None
        and guard_count_answer is not None
        and guard_count_answer_has_value
        and "vendor assignment information is not available" not in guard_count_answer
        and guard_list_answer is not None
        and guard_list_answer_has_ids
    )
    return _check(
        ok,
        "CHAT-056",
        (
            "store-id range, current status, and assigned-group filters stay consistent "
            f"across count/list follow-ups; count={count_value}, listed={list_count}; sql={executed}"
        ),
    )


def case_chat_059() -> bool:
    question = "show me all tickets not closed that have not had a public update in the last 24 hours"
    sql = universal._public_update_staleness_sql(question) or ""
    result = bigquery.execute_query(sql)
    executed = str(result.get("sql_executed") or "")
    row_count = int(result.get("row_count") or 0)
    schema_hint = universal_chat._BQ_SCHEMA_HINT
    semantic = semantic_interpreter.interpret_latest_turn([{"role": "user", "content": question}])
    event_request = universal._looks_like_global_event_discovery(
        question,
        semantic.normalized_question_en,
        semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
    )
    ok = (
        bool(sql)
        and "is_internal = FALSE" in sql
        and "is_public" not in sql
        and "ticket_comments" in sql
        and "last_public_update_at" in sql
        and "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)" in sql
        and re.search(r"LOWER\(COALESCE\(t\.status, ''\)\) NOT IN \('solved', 'closed'\)", sql)
        and "is_internal" in schema_hint
        and "ticket_comments(ticket_id, body, author_name, created_at, is_public)" not in schema_hint
        and "There is no `is_public` column" in schema_hint
        and row_count > 0
        and "Unrecognized name" not in str(result)
        and "is_public" not in executed
        and event_request is None
    )
    return _check(
        ok,
        "CHAT-059",
        f"public-update freshness uses ticket_comments.is_internal=false and executes; rows={row_count}",
    )


def case_chat_060() -> bool:
    def has_visible_customer_risk_score(answer: str) -> bool:
        return bool(
            re.search(
                r"\b(?:red|orange|yellow|green)\s*/\s*\d{1,3}\b"
                r"|\b(?:red|orange|yellow|green)\s+\d{1,3}\b"
                r"|\btop score\b"
                r"|\bmax risk\b",
                answer,
                flags=re.IGNORECASE,
            )
        )

    store_question = (
        "For store 2002, review active tickets with 4+ customer public follow-ups "
        "and summarize the customer tone from public comments."
    )
    store_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=store_question,
        routing_question=store_question,
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id="2004",
    )
    store_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(store_request)
        if store_request
        else {}
    )
    store_answer = str(store_payload.get("answer") or "")
    store_rows = store_payload.get("rows") or []

    colleague_store_question = (
        "For store 2002, a ticket had 4+ customer public follow-ups. "
        "What is the general Customer Risk / sentiment of the store?"
    )
    colleague_store_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=colleague_store_question,
        routing_question=colleague_store_question,
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id="2004",
    )
    colleague_store_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(colleague_store_request)
        if colleague_store_request
        else {}
    )
    colleague_store_answer = str(colleague_store_payload.get("answer") or "")

    broad_question = (
        "Which open or pending tickets have high Customer Risk because the customer "
        "was the last public commenter or we have not replied publicly in 24+ hours?"
    )
    broad_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=broad_question,
        routing_question=broad_question,
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id=None,
    )
    broad_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(broad_request)
        if broad_request
        else {}
    )
    broad_answer = str(broad_payload.get("answer") or "")
    broad_rows = broad_payload.get("rows") or []

    multiple_question = (
        "Show active tickets where the customer followed up multiple times after our last public reply, "
        "and recommend what we should do first."
    )
    multiple_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=multiple_question,
        routing_question=multiple_question,
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id=None,
    )
    multiple_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(multiple_request)
        if multiple_request
        else {}
    )
    multiple_answer = str(multiple_payload.get("answer") or "")
    multiple_rows = multiple_payload.get("rows") or []

    ranking_question = (
        "Rank stores over the last 30 days by likely negative sentiment using repeated "
        "customer follow-ups, unresolved tickets, public-response delays, escalations, and vendor dispatches."
    )
    ranking_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=ranking_question,
        routing_question=ranking_question,
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id=None,
    )
    ranking_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(ranking_request)
        if ranking_request
        else {}
    )
    ranking_answer = str(ranking_payload.get("answer") or "")

    ticket_question = (
        "For ticket #950028, analyze Customer Risk and explain why it is red."
    )
    ticket_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=ticket_question,
        routing_question=ticket_question,
        explicit_ticket_id=950028,
        target_ticket_id=None,
        explicit_store_id=None,
    )
    ticket_payload = (
        customer_risk_analytics.build_customer_risk_chat_response(ticket_request)
        if ticket_request
        else {}
    )
    ticket_answer = str(ticket_payload.get("answer") or "")

    ok = (
        store_request is not None
        and store_request.scope == "store_tickets"
        and store_request.followup_min == 4
        and "#950028" in store_answer
        and "Customer Risk" in store_answer
        and "Agent after latest customer" in store_answer
        and not has_visible_customer_risk_score(store_answer)
        and any(int(row.get("id") or 0) == 950028 for row in store_rows)
        and colleague_store_request is not None
        and colleague_store_request.scope == "store_tickets"
        and "#950028" in colleague_store_answer
        and "Stores ranked by elevated Customer Risk" not in colleague_store_answer
        and not has_visible_customer_risk_score(colleague_store_answer)
        and broad_request is not None
        and broad_request.scope == "ticket_list"
        and broad_request.minimum_score == 70
        and "Customer Risk" in broad_answer
        and not has_visible_customer_risk_score(broad_answer)
        and all(int(row.get("risk_score") or 0) >= 70 for row in broad_rows[:5])
        and all(str(row.get("status") or "").lower() in {"open", "pending"} for row in broad_rows[:5])
        and multiple_request is not None
        and multiple_request.followup_min == 2
        and multiple_request.require_after_agent_followups is True
        and "2+ customer follow-ups after our last public reply" in multiple_answer
        and "No public agent reply in" not in multiple_answer
        and not has_visible_customer_risk_score(multiple_answer)
        and len(multiple_rows) > 0
        and all(int(row.get("customer_followups_after_last_agent") or 0) >= 2 for row in multiple_rows)
        and ranking_request is not None
        and ranking_request.scope == "store_ranking"
        and ranking_request.since_days == 30
        and "Highest risk" in ranking_answer
        and not has_visible_customer_risk_score(ranking_answer)
        and ticket_request is not None
        and ticket_request.scope == "ticket"
        and ticket_request.ticket_id == 950008
        and not has_visible_customer_risk_score(ticket_answer)
    )
    return _check(
        ok,
        "CHAT-060",
        (
            "Customer Risk chat route detects ticket/store/broad/ranking sentiment asks "
            f"and returns color-only public-comment evidence; store rows={len(store_rows)}, "
            f"broad rows={len(broad_rows)}, multiple rows={len(multiple_rows)}"
        ),
    )


def case_chat_062() -> bool:
    question = (
        "найди мне любой тикет из лога ресторана, где мы направляем в ресторан техника "
        "из codgeco для того, чтобы тот подключил новый моем к сети."
    )
    semantic = semantic_interpreter.interpret_latest_turn([{"role": "user", "content": question}])
    request = universal._looks_like_global_event_discovery(
        question,
        semantic.normalized_question_en,
        semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
    )
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=question,
        routing_question=semantic.normalized_question_en,
        semantic_frame=semantic.to_dict(),
        selected_executor="ticket_semantic_search",
    )
    ticket_search_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    generic_answer = universal._global_event_discovery_answer(
        {"terms": ["find", "from", "log"], "period": {"label": "all indexed ticket history"}},
        [],
        "Russian",
    )
    ok = (
        request is None
        and semantic.target_context == "global"
        and "modem" in semantic.normalized_question_en.lower()
        and re.search(r"cog|cod", semantic.normalized_question_en, flags=re.IGNORECASE)
        and contract.executor == "ticket_semantic_search"
        and ticket_search_authorized
        and "пожар ресторана" not in generic_answer.lower()
    )
    return _check(
        ok,
        "CHAT-062",
        "scenario-based ticket retrieval does not route to event discovery or leak fire-template wording",
    )


def case_chat_063() -> bool:
    question = (
        "Проверь все тикеты, начиная от 2023 и по сегодня, связанные с сетью или подключением. "
        "Были ли упоминания upgrade на Fiber линию от Telecom Partner?"
    )
    semantic = semantic_interpreter.interpret_latest_turn([{"role": "user", "content": question}])
    request = universal._looks_like_global_event_discovery(
        question,
        semantic.normalized_question_en,
        semantic,
        explicit_store_context=None,
        explicit_ticket_id=None,
    )
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=question,
        routing_question=semantic.normalized_question_en,
        semantic_frame=semantic.to_dict(),
        selected_executor="ticket_semantic_search",
    )
    ticket_search_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    period = temporal_resolver.resolve_period_text(semantic.normalized_question_en)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    ok = (
        request is None
        and semantic.intent == "ticket_retrieval"
        and semantic.target_context == "global"
        and contract.executor == "ticket_semantic_search"
        and ticket_search_authorized
        and period is not None
        and period.start_date.isoformat() == "2023-01-01"
        and period.end_date_exclusive == today + timedelta(days=1)
        and "telecom_partner" in semantic.normalized_question_en.lower()
        and "fiber" in semantic.normalized_question_en.lower()
    )
    return _check(
        ok,
        "CHAT-063",
        "historical Telecom Partner/fiber mention search keeps full from-2023 range and stays out of event discovery",
    )


def case_chat_099() -> bool:
    question = (
        "For every store 2012 2011 2002 2003 2004 2006 find all "
        "internet/speed/connection interruption related tickets for the last 365 days"
    )
    store_ids = universal._extract_store_ids(question)
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=question,
        routing_question=question,
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "store",
            "confidence": "high",
            "entities": {"store_ids": store_ids, "date_phrase": "last 365 days"},
            "task": {"task_type": "general_answer"},
            "retrieval": {
                "search_goal": "internet/speed/connection interruption related tickets",
                "required_concepts": ["internet", "speed", "connection interruption"],
                "output": "ticket_examples",
            },
        },
        explicit_store_ids=store_ids,
        selected_executor="ticket_semantic_search",
    )
    authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    ok = (
        store_ids == ["2012", "2011", "2002", "2003", "2004", "2006"]
        and contract.scope == "store"
        and contract.filters.get("store_ids") == store_ids
        and contract.filters.get("store_id") in (None, "")
        and authorized
    )
    return _check(
        ok,
        "CHAT-099",
        "multi-store ticket retrieval preserves all requested store IDs instead of collapsing to the first store",
    )


def case_chat_100() -> bool:
    contract = {
        "filters": {
            "required_concepts": ["internet", "speed", "connection interruption"],
            "store_ids": ["2012", "2011", "2002", "2003", "2004", "2006"],
        }
    }
    result = {
        "summary": {
            "store_ids": ["2012", "2011", "2002", "2003", "2004", "2006"],
            "start_date": "2025-07-01",
            "end_date": "2026-07-01",
        },
        "rows": [
            {
                "ticket_id": 950053,
                "id": 950053,
                "subject": "Internet interruption",
                "created_at": "2026-06-12",
                "org_name": "Store 2003",
                "store_id": "2002",
                "semantic_match_type": "related_incomplete",
                "matched_concepts": ["internet"],
                "missing_required_concepts": ["speed", "connection interruption"],
                "evidence_snippet": "Internet connection intermittent.",
            }
        ],
    }
    answer = universal._ticket_semantic_search_answer(contract, result, "English")
    ok = (
        "I checked 6 requested stores" in answer
        and "**Store 2003: related tickets to review" in answer
        and "#950053" in answer
        and "**Store 2012: no indexed matches" in answer
        and "I did not find a clear confirmation" not in answer
    )
    return _check(
        ok,
        "CHAT-100",
        "multi-store ticket semantic answer is grouped by requested store and does not turn related rows into no-data wording",
    )


def case_chat_101() -> bool:
    answer = (
        "You are correct. My previous response did not include ticket #950053 because the previous query "
        "was for a different list of store IDs (2012, 2011, 2003, 2004, 2006), and store 2003 was not "
        "in that list.\n\nTicket #950053 is for store 2003."
    )
    result = universal._apply_universal_answer_quality_gate(answer)
    ok = (
        result.flags.get("prior_scope_claim_rewritten") is True
        and "scope/retrieval miss" in result.visible_answer
        and "Ticket #950053" in result.visible_answer
        and "2002 was not in that list" not in result.visible_answer
    )
    return _check(
        ok,
        "CHAT-101",
        "unsupported prior-scope explanation is rewritten before it reaches the engineer",
    )


def case_chat_102() -> bool:
    from app.services import chat_answer_validator

    context = chat_answer_validator.build_answer_validation_context(
        candidate_answer=(
            "The fire happened on December 23, 2024. "
            "Ticket #950032 confirms that date."
        ),
        latest_question="Where did you find the fire date? Do not invent; verify the source.",
        tools=["run_bq_query"],
        bq_rows=[{
            "id": 950032,
            "created_at": "2026-04-27",
            "subject": "Internet degradation after repairs",
        }],
        tickets=[{"id": 950032}],
        conversation_entities={"ticket_id": 950032},
        available_fields=["id", "created_at", "subject"],
    )
    risk = chat_answer_validator.detect_answer_risks(context)
    ok = (
        risk.should_validate
        and "unsupported_strong_claim" in risk.risk_classes
        and "weak_evidence" not in risk.risk_classes
    )
    return _check(
        ok,
        "CHAT-102",
        "exact event/date claims in challenged answers require supporting fields or validator review",
    )


def case_chat_103() -> bool:
    answer = "No matching trends or tickets were found for the requested top stores."
    result = universal._apply_universal_answer_quality_gate(
        answer,
        tools=["run_bq_query"],
        bq_rows="Query error: Syntax error: Unexpected identifier at [1:1]",
    )
    visible = result.visible_answer.lower()
    ok = (
        result.flags.get("action") == "replaced_query_error_no_data"
        and result.flags.get("query_or_tool_error") is True
        and "query failed" in visible
        and "proof that there is no data" in visible
        and "syntax error" not in visible
        and "no matching trends" not in visible
    )
    return _check(
        ok,
        "CHAT-103",
        "query/tool errors are not converted into confident no-data answers or raw SQL leaks",
    )


def case_chat_104() -> bool:
    from app.services import chat_answer_validator

    context = chat_answer_validator.build_answer_validation_context(
        candidate_answer="The Drive documentation mentions Telecom Partner escalation steps.",
        latest_question=(
            "Find examples of tickets where an engineer opened a Telecom Partner ticket "
            "for a degraded line."
        ),
        tools=["search_drive_exact"],
        drive_chunks=[{"title": "Telecom Partner escalation matrix"}],
    )
    risk = chat_answer_validator.detect_answer_risks(context)
    ok = (
        risk.should_validate
        and "source_family_mismatch" in risk.risk_classes
        and "drive" in risk.source_families
        and "tickets" not in risk.source_families
    )
    return _check(
        ok,
        "CHAT-104",
        "ticket-example scenario answered from Drive-only evidence is flagged as a source-family mismatch",
    )


def case_chat_105() -> bool:
    from app.services import chat_answer_validator

    context = chat_answer_validator.build_answer_validation_context(
        candidate_answer="I cannot find the Tempo Systems vendor contact phone number in the retrieved KB article.",
        latest_question="What is the Tempo Systems vendor contact phone number?",
        tools=["search_knowledge_base"],
        kb_results=[{"title": "Tempo Systems timer troubleshooting"}],
    )
    risk = chat_answer_validator.detect_answer_risks(context)
    ok = (
        risk.should_validate
        and "source_family_mismatch" in risk.risk_classes
        and "kb" in risk.source_families
        and "drive" not in risk.source_families
    )
    return _check(
        ok,
        "CHAT-105",
        "vendor contact/table lookup without Drive/table evidence is flagged for exact-source repair",
    )


def case_chat_106() -> bool:
    from app.services import chat_answer_validator

    context = chat_answer_validator.build_answer_validation_context(
        candidate_answer="Breaking it down by brand, the grouped rows cover 181 tickets.",
        latest_question="Break it down by brand.",
        tools=["run_bq_query"],
        bq_rows=[{"brand": "Brand A", "ticket_count": 181}],
        risk_hints=["denominator_drift"],
    )
    risk = chat_answer_validator.detect_answer_risks(context)
    ok = (
        risk.should_validate
        and "denominator_drift" in risk.risk_classes
        and risk.risk_reasons.get("denominator_drift")
    )
    return _check(
        ok,
        "CHAT-106",
        "denominator/filter drift hints survive into the answer validator risk detector",
    )


def _dispatcher_prior_compare_payload() -> dict:
    return {
        "task_type": "ticket_set_compare",
        "answer_language": "user",
        "confidence": 0.88,
        "requires_dispatcher": True,
        "scope": {
            "store_ids": ["2012", "2011", "2002"],
            "ticket_ids": [950053, 950027],
            "source_family": "tickets",
        },
        "prior_result_refs": [
            {
                "turn_id": "turn-multistore-search",
                "use_full_result_set": True,
                "reason": "Compare against the prior full ticket result set.",
            }
        ],
        "actions": [
            {
                "name": "load_prior_result_set",
                "inputs": {"turn_id": "turn-multistore-search"},
                "max_rows": 100,
                "required": True,
            },
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 50,
                "required": True,
            },
            {
                "name": "compare_ticket_sets",
                "inputs": {"criteria": ["internet", "speed", "connection interruption"]},
                "max_rows": 50,
                "required": True,
            },
        ],
        "comparison_contract": {
            "left_label": "ESIP previous result set",
            "right_label": "User supplied IDs",
            "criteria": ["internet", "speed", "connection interruption"],
            "must_report": ["common_ids", "left_only", "right_only", "unknown_needs_more_evidence"],
        },
        "answer_contract": {
            "must_include": ["checked_scope", "criteria", "evidence_limits"],
            "must_not_claim": ["cannot_access_ticket_details_without_attempting_lookup"],
        },
        "risk_flags": ["prior_result_truncation", "bulk_id_followup"],
        "cost_plan": {"max_planner_calls": 1, "max_tool_actions": 4, "max_ticket_ids": 75, "max_rows": 100},
        "fallback": "legacy_universal_chat",
    }


def _dispatcher_bulk_lookup_payload() -> dict:
    return {
        "task_type": "bulk_ticket_lookup",
        "answer_language": "user",
        "confidence": 0.86,
        "requires_dispatcher": True,
        "scope": {"store_ids": [], "ticket_ids": [950053, 950027], "source_family": "tickets"},
        "prior_result_refs": [],
        "actions": [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950053, 950027]},
                "max_rows": 25,
                "required": True,
            }
        ],
        "comparison_contract": {"left_label": "", "right_label": "", "criteria": [], "must_report": []},
        "answer_contract": {"must_include": ["checked_scope"], "must_not_claim": []},
        "risk_flags": ["bulk_id_followup"],
        "cost_plan": {"max_planner_calls": 1, "max_tool_actions": 1, "max_ticket_ids": 75, "max_rows": 25},
        "fallback": "legacy_universal_chat",
    }


def _dispatcher_source_family_payload(**overrides) -> dict:
    payload = {
        "task_type": "ticket_set_search",
        "answer_language": "user",
        "confidence": 0.84,
        "requires_dispatcher": True,
        "scope": {"store_ids": ["2012"], "ticket_ids": [], "source_family": "tickets"},
        "prior_result_refs": [],
        "actions": [
            {
                "name": "ticket_set_search",
                "inputs": {
                    "store_ids": ["2012"],
                    "criteria": ["Telecom Partner", "degraded line", "opened vendor ticket"],
                    "query": "Telecom Partner degraded line opened vendor ticket",
                },
                "max_rows": 50,
                "required": True,
            }
        ],
        "comparison_contract": {"left_label": "", "right_label": "", "criteria": [], "must_report": []},
        "answer_contract": {
            "must_include": ["checked_scope", "criteria", "evidence_limits"],
            "must_not_claim": ["searched_drive_instead_of_tickets"],
        },
        "risk_flags": ["source_family_mismatch"],
        "cost_plan": {"max_planner_calls": 1, "max_tool_actions": 1, "max_ticket_ids": 75, "max_rows": 50},
        "fallback": "legacy_universal_chat",
    }
    payload.update(overrides)
    return payload


def case_chat_107() -> bool:
    decision = ai_dispatcher_planner.compile_dispatcher_payload(
        _dispatcher_prior_compare_payload(),
        mode=ai_dispatcher_planner.DispatcherMode.SHADOW,
    )
    ok = (
        decision.accepted
        and not decision.execute_actions
        and decision.effective_scope.store_ids == ("2012", "2011", "2002")
        and decision.effective_scope.ticket_ids == (950053, 950027)
        and decision.actions[1].inputs.get("ticket_ids") == [950053, 950027]
        and decision.reason_codes == ()
    )
    return _check(
        ok,
        "CHAT-107",
        "prior result-set comparison planner contract compiles in shadow without claiming execution",
    )


def case_chat_108() -> bool:
    payload = {
        "task_type": "bulk_ticket_lookup",
        "answer_language": "user",
        "confidence": 0.86,
        "requires_dispatcher": True,
        "scope": {"ticket_ids": [950020, 950021, 950022], "source_family": "tickets"},
        "actions": [
            {
                "name": "bulk_ticket_id_lookup",
                "inputs": {"ticket_ids": [950020, 950021, 950022]},
                "max_rows": 25,
                "required": True,
            }
        ],
        "answer_contract": {
            "must_include": ["checked_scope", "evidence_limits"],
            "must_not_claim": ["cannot_access_ticket_details_without_attempting_lookup"],
        },
        "risk_flags": ["bulk_id_followup"],
        "cost_plan": {"max_planner_calls": 1, "max_tool_actions": 1, "max_ticket_ids": 75, "max_rows": 25},
        "fallback": "legacy_universal_chat",
    }
    decision = ai_dispatcher_planner.compile_dispatcher_payload(payload, mode=ai_dispatcher_planner.DispatcherMode.SHADOW)
    ok = (
        decision.accepted
        and not decision.execute_actions
        and decision.effective_scope.ticket_ids == (950020, 950021, 950022)
        and decision.actions[0].inputs.get("ticket_ids") == [950020, 950021, 950022]
        and decision.actions[0].max_rows == 25
    )
    return _check(
        ok,
        "CHAT-108",
        "bare ticket-ID list can compile as bounded bulk lookup when the task context supplies comparison intent",
    )


def case_chat_109() -> bool:
    now = datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC"))
    payload = ai_dispatcher_result_sets.build_result_set_payload(
        conversation_id="qa-chat-109",
        turn_id="turn-109",
        trace_id="trace-109",
        source_family="tickets",
        requested_store_ids=["2012", "2011", "2002"],
        criteria=["internet", "speed", "connection interruption"],
        ticket_rows=[
            {"ticket_id": 950053, "store_id": "2002", "subject": "Internet connection interrupted"},
            {"ticket_id": 950027, "store_id": "2003", "subject": "Network slow"},
            {"ticket_id": 950030, "store_id": "2011", "subject": "Primary internet issue"},
        ],
        ticket_ids=[950053, 950027, 950030, 950031],
        displayed_ticket_ids=[950053, 950027],
        freshness={"start_date": "2025-07-01", "end_date": "2026-07-01", "ranked_rows": 4},
        now=now,
    )
    selected = ai_dispatcher_result_sets.select_latest_result_set({"sets": [payload]}, now=now)
    comparison = ai_dispatcher_result_sets.compare_ticket_sets(
        selected or {},
        [950053, 950030, 950058],
        {
            "found_ids": [950053, 950030],
            "missing_ids": [950058],
            "rows": [
                {"ticket_id": 950053, "store_id": "2002", "subject": "Internet connection interrupted"},
                {"ticket_id": 950030, "store_id": "2011", "subject": "Primary internet issue"},
            ],
            "cap": 75,
        },
    )
    ok = (
        selected is not None
        and selected["ticket_ids"] == [950053, 950027, 950030, 950031]
        and selected["displayed_ticket_ids"] == [950053, 950027]
        and selected["counts"]["ticket_ids"] == 4
        and selected["counts"]["displayed_ticket_ids"] == 2
        and "messages" not in selected
        and "final_answer" not in selected
        and comparison["common_ids"] == [950053, 950030]
        and comparison["previous_only_ids"] == [950027, 950031]
        and comparison["user_only_ids"] == [950058]
        and comparison["missing_lookup_ids"] == [950058]
    )
    return _check(
        ok,
        "CHAT-109",
        "result-set memory preserves full bounded IDs separately from displayed subset for follow-up comparison",
    )


def case_chat_110() -> bool:
    payload = _dispatcher_prior_compare_payload()
    payload["actions"][1]["name"] = "run_arbitrary_tool"
    decision = ai_dispatcher_planner.compile_dispatcher_payload(
        payload,
        mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
    )
    ok = (
        not decision.accepted
        and not decision.execute_actions
        and decision.actions == ()
        and decision.reason_codes == ("planner_validation_failed",)
        and "actions[1].name" in {issue.field for issue in decision.issues}
    )
    return _check(
        ok,
        "CHAT-110",
        "unknown planner action is rejected before execution and falls back safely",
    )


def case_chat_111() -> bool:
    trim_payload = _dispatcher_prior_compare_payload()
    trim_payload["scope"]["ticket_ids"] = [950053, 950054, 950055]
    trim_payload["actions"][1]["inputs"]["ticket_ids"] = [950053, 950054, 950055]
    trim_payload["actions"][1]["max_rows"] = 50
    limits = ai_dispatcher_planner.DispatcherPolicyLimits(max_explicit_ticket_ids=2, max_action_rows=10)
    trimmed = ai_dispatcher_planner.compile_dispatcher_payload(
        trim_payload,
        mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
        limits=limits,
    )

    reject_payload = _dispatcher_prior_compare_payload()
    reject_payload["actions"] = reject_payload["actions"] + [
        {"name": "search_knowledge_base", "inputs": {"query": "internet speed"}, "required": False},
        {"name": "search_drive_exact", "inputs": {"query": "internet speed"}, "required": False},
    ]
    rejected = ai_dispatcher_planner.compile_dispatcher_payload(reject_payload)

    ok = (
        trimmed.accepted
        and trimmed.execute_actions
        and trimmed.effective_scope.ticket_ids == (950053, 950054)
        and trimmed.actions[1].max_rows == 10
        and {"ticket_ids_trimmed", "action_rows_trimmed"}.issubset(set(trimmed.reason_codes))
        and not rejected.accepted
        and "too_many_actions" in rejected.reason_codes
    )
    return _check(
        ok,
        "CHAT-111",
        "dispatcher compiler trims bounded ticket/row overages and rejects excessive action plans before execution",
    )


def case_chat_112() -> bool:
    import copy
    import json
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, payload: dict):
            self.text = json.dumps(payload)

        def generate_content(self, _prompt: str, generation_config=None):
            return FakeResponse(self.text)

    legacy_decision = {
        "tools": ["search_knowledge_base"],
        "reasoning": "Routine procedure lookup.",
        "kb_query": "reset internet router",
    }
    legacy_sources = [{"title": "Router reset procedure", "url": "https://example.invalid/kb"}]
    legacy_tickets = [{"id": 950053, "subject": "Internet speed issue"}]
    before = {
        "decision": copy.deepcopy(legacy_decision),
        "sources": copy.deepcopy(legacy_sources),
        "tickets": copy.deepcopy(legacy_tickets),
    }

    disabled = ai_dispatcher_runtime.run_dispatcher_shadow(
        latest_question="How do I reset an internet router?",
        routing_question="How do I reset an internet router?",
        messages=[{"role": "user", "content": "How do I reset an internet router?"}],
        semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
        conversation_id="qa-chat-112",
        turn_id="turn-disabled",
        trace_id="trace-disabled",
        owner_role="admin",
        explicit_store_ids=[],
        explicit_ticket_id=None,
        previous_assistant_text="",
        config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
            mode=ai_dispatcher_planner.DispatcherMode.DISABLED,
        ),
    )

    with patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner should not run")):
        shadow_good_route = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question="How do I reset an internet router?",
            routing_question="How do I reset an internet router?",
            messages=[{"role": "user", "content": "How do I reset an internet router?"}],
            semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
            conversation_id="qa-chat-112",
            turn_id="turn-shadow",
            trace_id="trace-shadow",
            owner_role="super_admin",
            explicit_store_ids=[],
            explicit_ticket_id=None,
            previous_assistant_text="",
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.SHADOW,
            ),
        )

    with (
        patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(_dispatcher_prior_compare_payload())),
        patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
        patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
        patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
        patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
        patch.object(ai_dispatcher_runtime, "check_cost_gate"),
        patch.object(ai_dispatcher_runtime, "record_estimate"),
        patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
        patch.object(ai_dispatcher_runtime, "check_divergence"),
    ):
        shadow_regular_user = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question="Compare ticket 950012 and 950027 with your previous result.",
            routing_question="Compare ticket 950012 and 950027 with your previous result.",
            messages=[{"role": "user", "content": "Compare ticket 950012 and 950027"}],
            semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
            conversation_id="qa-chat-112",
            turn_id="turn-shadow-regular-user",
            trace_id="trace-shadow-regular-user",
            owner_role="user",
            explicit_store_ids=[],
            explicit_ticket_id=None,
            previous_assistant_text="Previous tickets: 950053, 950027",
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.SHADOW,
            ),
        )

    after = {
        "decision": legacy_decision,
        "sources": legacy_sources,
        "tickets": legacy_tickets,
    }
    ok = (
        before == after
        and disabled.planner_calls == 0
        and disabled.fallback_reason == "dispatcher_disabled"
        and disabled.answer_affecting is False
        and shadow_good_route.planner_calls == 0
        and shadow_good_route.fallback_reason == "not_eligible"
        and shadow_good_route.answer_affecting is False
        and shadow_regular_user.planner_calls == 1
        and shadow_regular_user.planner_attempted is True
        and shadow_regular_user.accepted is True
        and shadow_regular_user.answer_affecting is False
    )
    return _check(
        ok,
        "CHAT-112",
        "disabled/shadow-ineligible paths preserve legacy artifacts and enabled dispatcher can run for regular users",
    )


def case_chat_113() -> bool:
    import json
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, payload: dict):
            self.text = json.dumps(payload)

        def generate_content(self, _prompt: str, generation_config=None):
            return FakeResponse(self.text)

    def run(payload: dict, *, latest_question: str, semantic_frame: dict, previous_assistant_text: str = ""):
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(payload)),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            return ai_dispatcher_runtime.run_dispatcher_shadow(
                latest_question=latest_question,
                routing_question=latest_question,
                messages=[{"role": "user", "content": latest_question}],
                semantic_frame=semantic_frame,
                conversation_id="qa-chat-113",
                turn_id="turn-113",
                trace_id="trace-113",
                owner_role="super_admin",
                explicit_store_ids=[],
                explicit_ticket_id=None,
                previous_assistant_text=previous_assistant_text,
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                    mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
                    assistive_task_classes=("prior_result_reference",),
                ),
            )

    prior_result = run(
        _dispatcher_prior_compare_payload(),
        latest_question="Compare ticket 950012 and 950027 with your previous result.",
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        previous_assistant_text="Previous tickets: 950053, 950027",
    )

    bulk_payload = _dispatcher_bulk_lookup_payload()
    bulk_result = run(
        bulk_payload,
        latest_question="950053 950027",
        semantic_frame={"intent": "retrieve_context", "target_context": "prior_turn"},
    )

    challenge_payload = dict(bulk_payload)
    challenge_payload["task_type"] = "fact_check_prior_answer"
    challenge_result = run(
        challenge_payload,
        latest_question="Where did you find that claim? Verify it.",
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        previous_assistant_text="Prior answer had a claim.",
    )

    ok = (
        prior_result.accepted
        and prior_result.answer_affecting
        and prior_result.assistive_task_class == "prior_result_reference"
        and "assistive_prior_result_comparison" in prior_result.reason_codes
        and prior_result.compile_decision.get("execute_actions") is False
        and bulk_result.accepted
        and not bulk_result.answer_affecting
        and "assistive_class_locked" in bulk_result.reason_codes
        and challenge_result.accepted
        and not challenge_result.answer_affecting
        and "assistive_class_locked" in challenge_result.reason_codes
    )
    return _check(
        ok,
        "CHAT-113",
        "Phase 5A assistive runtime marks only prior result-set comparison as answer-affecting",
    )


def case_chat_114() -> bool:
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, payload: dict):
            self.payload = payload

        def generate_content(self, *_args, **_kwargs):
            import json

            return FakeResponse(json.dumps(self.payload))

    def run(payload: dict, *, previous_assistant_text: str, semantic_frame: dict):
        with (
            patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(payload)),
            patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
            patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
            patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
            patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
            patch.object(ai_dispatcher_runtime, "check_cost_gate"),
            patch.object(ai_dispatcher_runtime, "record_estimate"),
            patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
            patch.object(ai_dispatcher_runtime, "check_divergence"),
        ):
            return ai_dispatcher_runtime.run_dispatcher_shadow(
                latest_question="950053 950027",
                routing_question="950053 950027",
                messages=[{"role": "user", "content": "950053 950027"}],
                semantic_frame=semantic_frame,
                conversation_id="qa-chat-114",
                turn_id="turn-114",
                trace_id="trace-114",
                owner_role="super_admin",
                explicit_store_ids=[],
                explicit_ticket_id=None,
                previous_assistant_text=previous_assistant_text,
                config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                    mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
                    assistive_task_classes=("prior_result_reference", "bulk_ticket_ids"),
                ),
            )

    prior_context = run(
        _dispatcher_bulk_lookup_payload(),
        previous_assistant_text="Earlier answer compared a ticket result set.",
        semantic_frame={"intent": "retrieve_context", "target_context": "prior_turn"},
    )
    no_context = run(
        _dispatcher_bulk_lookup_payload(),
        previous_assistant_text="",
        semantic_frame={"intent": "retrieve_context", "target_context": "global"},
    )
    fact_check_payload = _dispatcher_bulk_lookup_payload()
    fact_check_payload["task_type"] = "fact_check_prior_answer"
    fact_check = run(
        fact_check_payload,
        previous_assistant_text="Earlier answer made a specific claim.",
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
    )
    lookup = {
        "requested_ids": [950053, 950027, 950058],
        "found_ids": [950053, 950027],
        "missing_ids": [950058],
        "rows": [{"ticket_id": 950053, "store_id": "2002", "status": "solved", "subject": "Internet interrupted"}],
        "cap": 75,
        "truncated": False,
    }
    answer = ai_dispatcher_result_sets.build_bulk_lookup_answer(lookup)

    ok = (
        prior_context.accepted
        and prior_context.answer_affecting
        and prior_context.assistive_task_class == "bulk_ticket_ids"
        and "assistive_bulk_ticket_lookup" in prior_context.reason_codes
        and no_context.accepted
        and not no_context.answer_affecting
        and fact_check.accepted
        and not fact_check.answer_affecting
        and "Requested: #950053, #950027, #950058" in answer
        and "Missing from bounded lookup: #950058" in answer
        and "cap=75" in answer
    )
    return _check(
        ok,
        "CHAT-114",
        "Phase 5B assistive runtime marks only prior-context bulk ticket-ID lookup as answer-affecting",
    )


def case_chat_115() -> bool:
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, payload: dict):
            self.payload = payload

        def generate_content(self, *_args, **_kwargs):
            import json

            return FakeResponse(json.dumps(self.payload))

    fact_check_payload = _dispatcher_bulk_lookup_payload()
    fact_check_payload["task_type"] = "fact_check_prior_answer"
    fact_check_payload["actions"] = [
        {
            "name": "load_prior_result_set",
            "inputs": {"turn_id": "turn-prior-claim"},
            "max_rows": 100,
            "required": True,
        },
        {
            "name": "bulk_ticket_id_lookup",
            "inputs": {"ticket_ids": [950053, 950027]},
            "max_rows": 100,
            "required": True,
        },
    ]
    latest_question = "Where did you find that claim? Verify 950053 and 950027."
    prior_assistant_text = "Earlier answer claimed an unsupported fire date from ticket 950012."
    with (
        patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(fact_check_payload)),
        patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
        patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
        patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
        patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
        patch.object(ai_dispatcher_runtime, "check_cost_gate"),
        patch.object(ai_dispatcher_runtime, "record_estimate"),
        patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
        patch.object(ai_dispatcher_runtime, "check_divergence"),
    ):
        fact_check = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question=latest_question,
            routing_question=latest_question,
            messages=[{"role": "user", "content": latest_question}],
            semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
            conversation_id="qa-chat-115",
            turn_id="turn-115",
            trace_id="trace-115",
            owner_role="super_admin",
            explicit_store_ids=[],
            explicit_ticket_id=None,
            previous_assistant_text=prior_assistant_text,
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
                assistive_task_classes=("prior_result_reference", "bulk_ticket_ids", "challenge_fact_check"),
            ),
        )

    detected = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
        latest_question,
        previous_assistant_text=prior_assistant_text,
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
    )
    no_ids = ai_dispatcher_result_sets.detect_fact_check_prior_answer_request(
        "Where did you find that claim?",
        previous_assistant_text=prior_assistant_text,
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
    )
    lookup = {
        "requested_ids": [950053, 950027, 950058],
        "found_ids": [950053, 950027],
        "missing_ids": [950058],
        "rows": [
            {
                "ticket_id": 950053,
                "store_id": "2002",
                "status": "solved",
                "created_at": "2026-06-01",
                "issue_category": "Network",
                "subject": "Internet interrupted",
                "evidence_snippet": "private raw details should not be rendered",
            },
        ],
        "cap": 75,
        "truncated": False,
    }
    answer = ai_dispatcher_result_sets.build_fact_check_answer(lookup)

    ok = (
        fact_check.accepted
        and fact_check.answer_affecting
        and fact_check.assistive_task_class == "challenge_fact_check"
        and "assistive_challenge_fact_check" in fact_check.reason_codes
        and detected.is_fact_check
        and detected.ticket_ids == (950053, 950027)
        and not no_ids.is_fact_check
        and "bounded ticket-ID lookup" in answer
        and "Checked ticket IDs: #950053, #950027, #950058" in answer
        and "Ticket records found: #950053, #950027" in answer
        and "Missing from bounded lookup: #950058" in answer
        and "previous_assistant_text_used_as_evidence=false" in answer
        and "private raw details" not in answer
        and "unsupported fire date" not in answer
    )
    return _check(
        ok,
        "CHAT-115",
        "Project Phase 5C fact-check uses bounded lookup evidence rather than prior assistant prose",
    )


def case_chat_116() -> bool:
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, payload: dict):
            self.payload = payload

        def generate_content(self, *_args, **_kwargs):
            import json

            return FakeResponse(json.dumps(self.payload))

    question = "Find examples of tickets for store 2012 where an engineer opened a Telecom Partner ticket for a degraded line."
    payload = _dispatcher_source_family_payload()
    with (
        patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(payload)),
        patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
        patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
        patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
        patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
        patch.object(ai_dispatcher_runtime, "check_cost_gate"),
        patch.object(ai_dispatcher_runtime, "record_estimate"),
        patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
        patch.object(ai_dispatcher_runtime, "check_divergence"),
    ):
        source_family = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question=question,
            routing_question=question,
            messages=[{"role": "user", "content": question}],
            semantic_frame={"intent": "general", "target_context": "global"},
            conversation_id="qa-chat-116",
            turn_id="turn-116",
            trace_id="trace-116",
            owner_role="super_admin",
            explicit_store_ids=["2012"],
            explicit_ticket_id=None,
            previous_assistant_text="",
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
                assistive_task_classes=(
                    "prior_result_reference",
                    "bulk_ticket_ids",
                    "challenge_fact_check",
                    "source_family_mismatch",
                ),
            ),
        )

    contact_question = "What is the Tempo Systems vendor contact phone number?"
    contact_candidate = ai_dispatcher_runtime.source_family_mismatch_candidate(
        latest_question=contact_question,
        routing_question=contact_question,
        semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
    )
    wrong_source = ai_dispatcher_planner.compile_dispatcher_payload(
        _dispatcher_source_family_payload(scope={"store_ids": ["2012"], "ticket_ids": [], "source_family": "drive"}),
        mode=ai_dispatcher_planner.DispatcherMode.SHADOW,
    )

    ok = (
        source_family.accepted
        and source_family.answer_affecting
        and source_family.assistive_task_class == "source_family_mismatch"
        and "assistive_source_family_mismatch" in source_family.reason_codes
        and source_family.compile_decision.get("task_type") == "ticket_set_search"
        and source_family.compile_decision.get("effective_scope", {}).get("source_family") == "tickets"
        and not contact_candidate
        and wrong_source.accepted
        and wrong_source.effective_scope.source_family is ai_dispatcher_planner.DispatcherSourceFamily.DRIVE
    )
    return _check(
        ok,
        "CHAT-116",
        "Project Phase 6A source-family mismatch opens only scoped ticket-example semantic search",
    )


def case_chat_117() -> bool:
    old_cursor = universal._bq_sync_cursor_dt
    universal._bq_sync_cursor_dt = lambda: datetime(2026, 7, 7, 9, 15, tzinfo=ZoneInfo("UTC"))
    try:
        answer = universal._platform_update_cadence_answer("how soon will your db update?")
    finally:
        universal._bq_sync_cursor_dt = old_cursor

    old_cursor = universal._bq_sync_cursor_dt
    universal._bq_sync_cursor_dt = lambda: None
    try:
        no_cursor_answer = universal._platform_update_cadence_answer("when will your database update?")
    finally:
        universal._bq_sync_cursor_dt = old_cursor

    ok = (
        answer is not None
        and "configured scheduler" in answer
        and "exact times" in answer
        and "BQ analytics are synced through 2026-07-07 05:15 ET" in answer
        and "does not start a sync job" in answer
        and no_cursor_answer is not None
        and "could not read the current BQ freshness cursor" in no_cursor_answer
    )
    return _check(
        ok,
        "CHAT-117",
        f"platform update-cadence answer is deterministic and bounded; answer={answer}; no_cursor={no_cursor_answer}",
    )


def case_chat_118() -> bool:
    latest_question = "i just want the one for July and Jun"
    shadow = universal._build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=latest_question,
        semantic_frame={
            "target_context": "prior_turn",
            "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
            "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
        },
        previous_assistant_text="Earlier answer listed internet-speed tickets for store 2008.",
        conversation_entities={
            "trace_id": "trace-chat-118-prior",
            "turn_id": "turn-chat-118-prior",
            "semantic_labels": ["internet speed", "connection drops"],
            "store_id": "2009",
        },
        messages=[
            {"role": "assistant", "content": "Earlier answer listed internet-speed tickets for store 2008."},
            {"role": "user", "content": latest_question},
        ],
    )
    assist = universal._build_followup_date_scope_assist_payload(
        shadow_payload=shadow,
        latest_question=latest_question,
        routing_question=latest_question,
        previous_assistant_text="Earlier answer listed internet-speed tickets for store 2008.",
        conversation_entities={"trace_id": "trace-chat-118-prior", "turn_id": "turn-chat-118-prior"},
        messages=[
            {"role": "assistant", "content": "Earlier answer listed internet-speed tickets for store 2008."},
            {"role": "user", "content": latest_question},
        ],
    )
    sql = universal._followup_date_scope_assist_bq_sql(assist)
    answer = universal._build_followup_date_scope_answer(
        assist_payload=assist,
        rows=[
            {
                "id": 950052,
                "subject": "Internet speed degraded",
                "store_id": "2009",
                "status": "open",
                "issue_category": "Network",
                "created_at": "2026-07-03T14:00:00+00:00",
                "full_text": "Internet speed degraded at store 2008.",
            },
            {
                "id": 950051,
                "subject": "Connection drops",
                "store_id": "2009",
                "status": "solved",
                "issue_category": "Network",
                "created_at": "2026-06-18T09:30:00+00:00",
                "full_text": "Connection drops and internet speed issues.",
            },
        ],
    )
    no_context_shadow = universal._build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=latest_question,
        semantic_frame={"target_context": "prior_turn"},
        previous_assistant_text="",
        conversation_entities=None,
        messages=[{"role": "user", "content": latest_question}],
    )
    no_context_assist = universal._build_followup_date_scope_assist_payload(
        shadow_payload=no_context_shadow,
        latest_question=latest_question,
        routing_question=latest_question,
        previous_assistant_text="",
        conversation_entities=None,
        messages=[{"role": "user", "content": latest_question}],
    )

    ok = (
        assist.get("accepted") is True
        and assist.get("answer_affecting") is False
        and (assist.get("prior_topic") or {}).get("store_id") == "2009"
        and "internet speed" in ((assist.get("prior_topic") or {}).get("semantic_labels") or [])
        and sql is not None
        and "DATE '2026-07-01'" in sql
        and "DATE '2026-08-01'" in sql
        and "DATE '2026-06-01'" in sql
        and "store_id = '2008'" in sql
        and answer is not None
        and "July 2026 and June 2026" in answer
        and "not as July 1-2 only" in answer
        and "Current-month windows are month-to-date" in answer
        and "#950052" in answer
        and "#950051" in answer
        and "2026-07-01 through 2026-07-02" not in answer
        and no_context_shadow.get("emitted") is False
        and no_context_assist.get("accepted") is False
    )
    return _check(
        ok,
        "CHAT-118",
        "Project Phase 6D visible follow-up date scope preserves prior topic and June/July month windows",
    )


def case_chat_119() -> bool:
    context = chat_answer_validator.build_answer_validation_context(
        candidate_answer=(
            "Ticket #950050 details: the latest note says the store is waiting "
            "for IT to confirm the next step."
        ),
        latest_question="How to resolved this?",
        direct_ticket_context=True,
        risk_hints=["weak_evidence"],
    )
    result = chat_answer_validator.detect_answer_risks(context)
    unsupported_context = chat_answer_validator.build_answer_validation_context(
        candidate_answer="Ticket #950050 breached SLA because no public agent reply was sent.",
        latest_question="Did this ticket breach SLA?",
        direct_ticket_context=True,
        available_fields=["id", "status"],
    )
    unsupported = chat_answer_validator.detect_answer_risks(unsupported_context)
    ok = (
        context["counts"].get("direct_ticket_context") == 1
        and "tickets" in result.source_families
        and not result.should_validate
        and "weak_evidence" not in result.risk_classes
        and unsupported.should_validate
        and "unsupported_strong_claim" in unsupported.risk_classes
    )
    return _check(
        ok,
        "CHAT-119",
        "Project Phase 6B direct ticket context counts as evidence without disabling strong-claim safety",
    )


def case_chat_120() -> bool:
    customer_allowed, customer_reason = universal._drive_hint_policy(
        any_drive_search_enabled=True,
        force_drive_tabular=True,
        selected_executor="customer_risk_analytics",
        tools=[],
    )
    ticket_allowed, ticket_reason = universal._drive_hint_policy(
        any_drive_search_enabled=True,
        force_drive_tabular=True,
        selected_executor="ticket_semantic_search",
        tools=[],
    )
    direct_allowed, direct_reason = universal._drive_hint_policy(
        any_drive_search_enabled=True,
        force_drive_tabular=True,
        selected_executor="direct_ticket_context",
        tools=[],
    )
    drive_allowed, drive_reason = universal._drive_hint_policy(
        any_drive_search_enabled=True,
        force_drive_tabular=True,
        selected_executor="phase1_tools",
        tools=[],
    )
    ok = (
        not customer_allowed
        and customer_reason == "customer_risk_analytics"
        and not ticket_allowed
        and ticket_reason == "ticket_semantic_search"
        and not direct_allowed
        and direct_reason == "direct_ticket_context"
        and drive_allowed
        and drive_reason is None
    )
    return _check(
        ok,
        "CHAT-120",
        "Project Phase 6B drive_hint skips non-Drive executors and still allows Drive tabular fallback",
    )


def case_chat_121() -> bool:
    latest_question = "i just want the one for July and Jun"
    routing_question = latest_question
    tools_before_policy = ["search_tickets"]
    selected_executor = "phase1_tools"
    payload = universal._build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame={
            "target_context": "prior_turn",
            "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
            "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
        },
        previous_assistant_text="Earlier answer listed internet-speed tickets for store 2008.",
        conversation_entities={
            "trace_id": "trace-chat-118-prior",
            "turn_id": "turn-chat-118-prior",
            "semantic_labels": ["internet speed", "connection drops"],
            "store_id": "2009",
        },
        messages=[
            {"role": "assistant", "content": "Earlier answer listed internet-speed tickets for store 2008."},
            {"role": "user", "content": latest_question},
        ],
    )
    no_context = universal._build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame={"target_context": "prior_turn"},
        previous_assistant_text="",
        conversation_entities=None,
        messages=[{"role": "user", "content": latest_question}],
    )
    windows = payload.get("month_windows") or []
    labels = [str(item.get("display_label") or "") for item in windows]
    ok = (
        payload.get("emitted") is True
        and payload.get("answer_affecting") is False
        and payload.get("needs_assistive_promotion") is True
        and "multi_month_shorthand" in (payload.get("reason_codes") or [])
        and len(windows) == 2
        and labels[0].startswith("July ")
        and labels[1].startswith("June ")
        and (payload.get("prior_topic") or {}).get("store_id") == "2009"
        and (payload.get("prior_topic") or {}).get("prior_trace_id") == "trace-chat-118-prior"
        and no_context.get("emitted") is False
        and "insufficient_prior_context" in (no_context.get("reason_codes") or [])
        and routing_question == latest_question
        and tools_before_policy == ["search_tickets"]
        and selected_executor == "phase1_tools"
    )
    return _check(
        ok,
        "CHAT-121",
        "Project Phase 6C shadow detects CHAT-118-style follow-up metadata only; visible answer repair remains Project Phase 6D",
    )


def case_chat_122() -> bool:
    latest_question = "How to resolved this?"
    payload = universal._build_ticket_resolution_lookup_assist_payload(
        latest_question=latest_question,
        routing_question=latest_question,
        semantic_frame={"intent": "next_steps", "target_context": "open_ticket"},
        explicit_ticket_id=None,
        target_ticket_id=950050,
        open_ticket_id=950050,
    )
    enabled = ai_dispatcher_runtime.DispatcherRuntimeConfig(
        mode=ai_dispatcher_planner.DispatcherMode.ASSISTIVE,
        assistive_task_classes=("ticket_resolution_lookup",),
    )
    documented_answer = universal._build_ticket_resolution_lookup_answer(
        assist_payload=payload,
        lookup_result={
            "requested_ids": [950050],
            "found_ids": [950050],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950050,
                    "subject": "Internet down",
                    "status": "solved",
                    "store_id": "2012",
                    "issue_category": "Network",
                    "solved_at": "2026-07-01",
                    "resolution": "Telecom Partner replaced the modem and verified connectivity.",
                    "full_text": "private raw text should not render",
                }
            ],
            "cap": 75,
            "truncated": False,
        },
        answer_language="English",
    )
    no_resolution_answer = universal._build_ticket_resolution_lookup_answer(
        assist_payload=payload,
        lookup_result={
            "requested_ids": [950050],
            "found_ids": [950050],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950050,
                    "subject": "Internet down",
                    "status": "open",
                    "store_id": "2012",
                    "issue_category": "Network",
                    "resolution": "resolved",
                }
            ],
            "cap": 75,
            "truncated": False,
        },
        answer_language="English",
    )
    generic = universal._build_ticket_resolution_lookup_assist_payload(
        latest_question="How do I fix a pinpad?",
        routing_question="How do I fix a pinpad?",
        semantic_frame={"intent": "procedure_lookup"},
        explicit_ticket_id=None,
        target_ticket_id=None,
        open_ticket_id=None,
    )
    bulk = universal._build_ticket_resolution_lookup_assist_payload(
        latest_question="How were tickets 950011 and 950053 resolved?",
        routing_question="How were tickets 950011 and 950053 resolved?",
        semantic_frame={"intent": "ticket_details"},
        explicit_ticket_id=None,
        target_ticket_id=None,
        open_ticket_id=None,
    )
    fact_check = universal._build_ticket_resolution_lookup_assist_payload(
        latest_question="Where did you find that claim? Verify 950050.",
        routing_question="Where did you find that claim? Verify 950050.",
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        explicit_ticket_id=None,
        target_ticket_id=None,
        open_ticket_id=None,
    )
    date_scope = universal._build_ticket_resolution_lookup_assist_payload(
        latest_question="i just want the one for July and Jun",
        routing_question="i just want the one for July and Jun",
        semantic_frame={"intent": "ticket_retrieval", "target_context": "prior_turn"},
        explicit_ticket_id=None,
        target_ticket_id=950050,
        open_ticket_id=950050,
    )

    ok = (
        payload.get("accepted") is True
        and payload.get("target_ticket_id") == 950050
        and payload.get("answer_affecting") is False
        and ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(enabled)
        and documented_answer is not None
        and "Documented resolution: Telecom Partner replaced the modem" in documented_answer
        and "documented_resolution=true" in documented_answer
        and "private raw text" not in documented_answer
        and no_resolution_answer is not None
        and "did not find a useful documented resolution" in no_resolution_answer
        and "documented_resolution=false" in no_resolution_answer
        and generic.get("accepted") is False
        and "missing_target_ticket" in (generic.get("reason_codes") or [])
        and bulk.get("accepted") is False
        and "bulk_ticket_lookup_intent" in (bulk.get("reason_codes") or [])
        and fact_check.get("accepted") is False
        and "challenge_fact_check_intent" in (fact_check.get("reason_codes") or [])
        and date_scope.get("accepted") is False
        and "date_scope_followup_intent" in (date_scope.get("reason_codes") or [])
    )
    return _check(
        ok,
        "CHAT-122",
        "Project Phase 6E ticket resolution lookup answers only targeted current/explicit ticket resolution asks",
    )


def case_chat_123() -> bool:
    latest_question = "i just want the one for July and Jun"
    visible_answer = "I found these for July 1-2 only."
    context = universal._build_output_critic_shadow_context(
        routing_question=latest_question,
        semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
        phase1_decision={"selected_executor": "ticket_semantic_search"},
        policy_flags={"selected_executor": "ticket_semantic_search", "raw_prompt": "SECRET PROMPT"},
        quality_gate_flags={"answer_validation": {"risk": "low"}},
        answer_validation={"status": "ok", "raw_model_response": "MODEL RAW"},
        counts={"sources": 1, "tickets": 1, "answer_chars": len(visible_answer)},
        sources=[{"title": "Ticket evidence", "snippet": "safe excerpt", "html_body": "RAW HTML"}],
        tickets=[{"ticket_id": 950050, "subject": "Internet speed", "full_text": "RAW TICKET FULL TEXT"}],
        ai_dispatcher_trace={"answer_affecting": False, "raw_response": "RAW DISPATCHER"},
        followup_shadow_payload={"emitted": True, "answer_affecting": False, "reason_codes": ["multi_month_shorthand"]},
        followup_date_scope_payload={"accepted": True, "answer_affecting": False},
        ticket_resolution_lookup_payload={"accepted": False},
        conversation_entities_loaded=True,
        conversation_entities_saved={"store_id": "2009"},
    )
    suspect = output_critic_shadow.compact_trace_metadata(
        output_critic_shadow.normalize_output_critic_payload(
            {
                "risk_level": "high",
                "risk_codes": ["followup_scope_loss"],
                "confidence": 0.91,
                "reason_short": "The answer narrowed a June/July follow-up to July 1-2 only.",
                "answer_affecting": True,
                "raw_model_response": "RAW MODEL RESPONSE",
            }
        )
    )
    good_control = output_critic_shadow.compact_trace_metadata(
        output_critic_shadow.normalize_output_critic_payload(
            {
                "risk_level": "none",
                "risk_codes": [],
                "confidence": 0.6,
                "reason_short": "The answer matches the compact trace.",
            }
        )
    )
    disabled = output_critic_shadow.skipped_result("disabled_by_config").to_trace_dict()
    rendered = repr(context) + repr(suspect) + repr(good_control) + repr(disabled)

    ok = (
        suspect.get("answer_affecting") is False
        and suspect.get("risk_level") == "high"
        and suspect.get("risk_codes") == ["followup_scope_loss"]
        and suspect.get("contract_type") == output_critic_shadow.CONTRACT_TYPE
        and visible_answer == "I found these for July 1-2 only."
        and good_control.get("risk_level") == "none"
        and good_control.get("risk_codes") is None
        and disabled.get("attempted") is False
        and disabled.get("skipped_reason") == "disabled_by_config"
        and "tool_calls" not in output_critic_shadow.OUTPUT_CRITIC_RESPONSE_SCHEMA["properties"]
        and "final_answer" not in output_critic_shadow.OUTPUT_CRITIC_RESPONSE_SCHEMA["properties"]
        and context["counts"]["answer_chars"] == len(visible_answer)
        and "SECRET PROMPT" not in rendered
        and "MODEL RAW" not in rendered
        and "RAW HTML" not in rendered
        and "RAW TICKET FULL TEXT" not in rendered
        and "RAW MODEL RESPONSE" not in rendered
    )
    return _check(
        ok,
        "CHAT-123",
        "Project Phase 6F output critic is private shadow metadata only and strips raw artifacts",
    )


def case_chat_124() -> bool:
    no_data = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="Find matching tickets",
        visible_answer="No matching tickets found.",
        output_critic_trace={
            "attempted": True,
            "risk_level": "high",
            "risk_codes": ["tool_error_as_absence"],
            "confidence": 0.93,
        },
        trace_context={"policy_flags": {"bq_error": "query failed"}},
        enabled=True,
    )
    raw_artifact = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="Question",
        visible_answer="Useful answer.\nTRACE_JSON: {\"raw\":\"secret\"}\ninternal",
        output_critic_trace={
            "attempted": True,
            "risk_level": "high",
            "risk_codes": ["raw_artifact"],
            "confidence": 0.9,
        },
        enabled=True,
    )
    followup_missing_context = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="i just want July and Jun",
        visible_answer="Only July 1-2.",
        output_critic_trace={
            "attempted": True,
            "risk_level": "high",
            "risk_codes": ["followup_scope_loss"],
            "confidence": 0.91,
        },
        trace_context={"followup_date_scope_assist": {"accepted": True, "answer_affecting": False}},
        enabled=True,
    )
    low_confidence = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="Question",
        visible_answer="No matching tickets found.",
        output_critic_trace={
            "attempted": True,
            "risk_level": "high",
            "risk_codes": ["tool_error_as_absence"],
            "confidence": 0.4,
        },
        trace_context={"policy_flags": {"bq_error": "query failed"}},
        enabled=True,
    )
    disabled = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="Question",
        visible_answer="No matching tickets found.",
        output_critic_trace={
            "attempted": True,
            "risk_level": "high",
            "risk_codes": ["tool_error_as_absence"],
            "confidence": 0.93,
        },
        trace_context={"policy_flags": {"bq_error": "query failed"}},
        enabled=False,
    )
    with mock.patch.object(
        output_critic_bounded_repair,
        "output_critic_bounded_repair_enabled",
        return_value=False,
    ):
        universal_answer, universal_delta, universal_metadata = universal._resolve_output_critic_bounded_repair_payload(
            latest_question="Question",
            visible_answer="No matching tickets found.",
            output_critic_trace={
                "attempted": True,
                "risk_level": "high",
                "risk_codes": ["tool_error_as_absence"],
                "confidence": 0.93,
            },
            trace_context={"policy_flags": {"bq_error": "query failed"}},
        )
    rendered_metadata = (
        repr(no_data.to_trace_dict())
        + repr(raw_artifact.to_trace_dict())
        + repr(followup_missing_context.to_trace_dict())
        + repr(low_confidence.to_trace_dict())
        + repr(disabled.to_trace_dict())
        + repr(universal_metadata)
    )

    ok = (
        no_data.repair_applied
        and no_data.repair_kind == "false_no_data_or_tool_error"
        and "Correction:" in no_data.stream_delta
        and "unverified" in no_data.repaired_answer
        and raw_artifact.repair_applied
        and "TRACE_JSON" not in raw_artifact.stream_delta
        and "secret" not in repr(raw_artifact.to_trace_dict())
        and followup_missing_context.repair_applied is False
        and followup_missing_context.skipped_reason == "missing_deterministic_followup_answer"
        and low_confidence.repair_applied is False
        and low_confidence.skipped_reason == "low_confidence"
        and disabled.repair_applied is False
        and disabled.skipped_reason == "disabled_by_config"
        and universal_answer == "No matching tickets found."
        and universal_delta == ""
        and universal_metadata.get("answer_affecting") is False
        and "query failed" not in rendered_metadata
        and "secret" not in rendered_metadata
        and "raw_model_response" not in rendered_metadata
    )
    return _check(
        ok,
        "CHAT-124",
        "Project Phase 6G output critic bounded repair is deterministic, default-off, and fail-closed",
    )


def _phase6h_result_set_fixture() -> dict:
    return {
        "memory_id": "mem-phase-6h",
        "conversation_id": "conv-phase-6h",
        "turn_id": "turn-prior",
        "trace_id": "trace-prior",
        "source_family": "tickets",
        "requested_store_ids": ["2012", "2002"],
        "criteria": ["internet", "speed", "connection"],
        "ticket_ids": [950052, 950051, 950053],
        "displayed_ticket_ids": [950052],
        "counts": {
            "ticket_ids": 3,
            "displayed_ticket_ids": 1,
            "ticket_rows": 3,
            "truncated_ticket_ids": 0,
        },
        "tickets": [
            {
                "ticket_id": 950052,
                "subject": "Internet speed degraded",
                "store_id": "2012",
                "status": "pending",
                "created_at": "2026-07-03T14:00:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET FULL TEXT MUST NOT RENDER",
            },
            {
                "ticket_id": 950051,
                "subject": "Connection drops",
                "store_id": "2012",
                "status": "solved",
                "created_at": "2026-06-18T09:30:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET FULL TEXT MUST NOT RENDER",
            },
            {
                "ticket_id": 950053,
                "subject": "Internet outage",
                "store_id": "2002",
                "status": "open",
                "created_at": "2026-07-05T16:45:00+00:00",
                "issue_category": "Network",
                "full_text": "RAW TICKET FULL TEXT MUST NOT RENDER",
            },
        ],
    }


def _phase6h_payload(value) -> dict:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return dict(value or {})


def case_chat_125() -> bool:
    try:
        from app.services import followup_result_set_assist  # noqa: PLC0415
    except Exception as exc:
        return _check(False, "CHAT-125", f"Project Phase 6H result-set refinement service missing: {exc}")

    result_set = _phase6h_result_set_fixture()
    accepted = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones for store 2003",
            routing_question="only the open ones for store 2003",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="I found internet-speed tickets for stores 2012 and 2002.",
            result_set=result_set,
        )
    )
    answer = followup_result_set_assist.build_result_set_refinement_answer(
        accepted,
        result_set,
        answer_language="English",
    )
    no_context = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="only the open ones",
            routing_question="only the open ones",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            result_set=None,
        )
    )
    date_scope = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="i just want the one for July and Jun",
            routing_question="i just want the one for July and Jun",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior answer",
            result_set=result_set,
        )
    )
    bulk = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="Compare 950051 and 950053 with the prior result",
            routing_question="Compare 950051 and 950053 with the prior result",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Prior answer",
            result_set=result_set,
        )
    )

    ok = (
        accepted.get("accepted") is True
        and accepted.get("request_kind") == "result_set_refinement"
        and accepted.get("answer_affecting") is False
        and accepted.get("filters", {}).get("store_ids") == ["2002"]
        and accepted.get("filters", {}).get("statuses") == ["open"]
        and answer is not None
        and "saved previous result set" in answer
        and "#950053" in answer
        and "#950052" not in answer
        and "RAW TICKET FULL TEXT" not in answer
        and no_context.get("accepted") is False
        and "missing_result_set" in (no_context.get("reason_codes") or [])
        and date_scope.get("accepted") is False
        and "date_scope_followup_intent" in (date_scope.get("reason_codes") or [])
        and bulk.get("accepted") is False
        and "bulk_ticket_lookup_intent" in (bulk.get("reason_codes") or [])
    )
    return _check(
        ok,
        "CHAT-125",
        "Project Phase 6H saved result-set refinement uses structured memory and rejects competing intents",
    )


def case_chat_126() -> bool:
    try:
        from app.services import followup_result_set_assist  # noqa: PLC0415
    except Exception as exc:
        return _check(False, "CHAT-126", f"Project Phase 6H omission service missing: {exc}")

    result_set = _phase6h_result_set_fixture()
    omitted = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="which tickets did you omit and why?",
            routing_question="which tickets did you omit and why?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="I showed one ticket from a larger saved result.",
            result_set=result_set,
        )
    )
    answer = followup_result_set_assist.build_result_set_refinement_answer(
        omitted,
        result_set,
        answer_language="English",
    )

    ok = (
        omitted.get("accepted") is True
        and omitted.get("request_kind") == "omitted_result_set"
        and answer is not None
        and "saved previous result set" in answer
        and "Displayed" in answer
        and "Omitted" in answer
        and "#950052" in answer
        and "#950051" in answer
        and "#950053" in answer
        and "visible assistant prose" not in answer.casefold()
        and "RAW TICKET FULL TEXT" not in answer
    )
    return _check(
        ok,
        "CHAT-126",
        "Project Phase 6H omitted-ticket follow-up distinguishes displayed and omitted saved IDs",
    )


def case_chat_127() -> bool:
    try:
        from app.services import followup_result_set_assist  # noqa: PLC0415
    except Exception as exc:
        return _check(False, "CHAT-127", f"Project Phase 6H ticket-reference service missing: {exc}")

    result_set = _phase6h_result_set_fixture()
    accepted = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
    )
    answer = followup_result_set_assist.build_ticket_reference_lookup_answer(
        accepted,
        {
            "requested_ids": [950052],
            "found_ids": [950052],
            "missing_ids": [],
            "rows": [
                {
                    "ticket_id": 950052,
                    "subject": "Internet speed degraded",
                    "store_id": "2012",
                    "status": "pending",
                    "created_at": "2026-07-03T14:00:00+00:00",
                    "issue_category": "Network",
                    "full_text": "RAW TICKET FULL TEXT MUST NOT RENDER",
                }
            ],
            "cap": 1,
            "truncated": False,
        },
        result_set,
        answer_language="English",
    )
    ambiguous = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what about that one?",
            routing_question="what about that one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
    )
    resolution = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="what fixed the first one?",
            routing_question="what fixed the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "next_steps"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
    )

    ok = (
        accepted.get("accepted") is True
        and accepted.get("request_kind") == "ticket_reference_lookup"
        and accepted.get("target_ticket_id") == 950052
        and accepted.get("lookup_cap") == 1
        and accepted.get("answer_affecting") is False
        and answer is not None
        and "#950052" in answer
        and "cap=1" in answer
        and "saved previous result set" in answer
        and "RAW TICKET FULL TEXT" not in answer
        and ambiguous.get("accepted") is False
        and "ambiguous_ticket_reference" in (ambiguous.get("reason_codes") or [])
        and resolution.get("accepted") is False
        and "ticket_resolution_lookup_intent" in (resolution.get("reason_codes") or [])
    )
    return _check(
        ok,
        "CHAT-127",
        "Project Phase 6H single-ticket follow-up lookup is bounded and rejects ambiguous/competing intents",
    )


def _run_dispatcher_with_fake_model(payload: dict, *, config, latest_question: str, semantic_frame: dict, previous_assistant_text: str = "", explicit_store_ids=None):
    import json
    from unittest.mock import patch

    class FakeUsage:
        prompt_token_count = 100
        candidates_token_count = 20
        thoughts_token_count = 0

    class FakeResponse:
        def __init__(self, text: str):
            self.text = text
            self.usage_metadata = FakeUsage()

    class FakeModel:
        def __init__(self, value: dict):
            self.payload = value

        def generate_content(self, *_args, **_kwargs):
            return FakeResponse(json.dumps(self.payload))

    with (
        patch.object(ai_dispatcher_runtime, "_get_model", return_value=FakeModel(payload)),
        patch.object(ai_dispatcher_runtime.GenerationConfig, "from_dict", side_effect=lambda data: data),
        patch.object(ai_dispatcher_runtime, "_today_dispatcher_spent_cad", return_value=0.0),
        patch.object(ai_dispatcher_runtime, "calc_cost", return_value=0.001),
        patch.object(ai_dispatcher_runtime, "get_gate_reserve", return_value=0.002),
        patch.object(ai_dispatcher_runtime, "check_cost_gate"),
        patch.object(ai_dispatcher_runtime, "record_estimate"),
        patch.object(ai_dispatcher_runtime, "record_usage", return_value=0.0001),
        patch.object(ai_dispatcher_runtime, "check_divergence"),
    ):
        return ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question=latest_question,
            routing_question=latest_question,
            messages=[{"role": "user", "content": latest_question}],
            semantic_frame=semantic_frame,
            conversation_id="qa-chat-phase7",
            turn_id="turn-phase7",
            trace_id="trace-phase7",
            owner_role="super_admin",
            explicit_store_ids=explicit_store_ids or [],
            explicit_ticket_id=None,
            previous_assistant_text=previous_assistant_text,
            config=config,
        )


def _primary_config(*classes: str):
    return ai_dispatcher_runtime.DispatcherRuntimeConfig(
        mode=ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE,
        assistive_task_classes=tuple(classes),
        eligible_task_classes=(
            "prior_result_reference",
            "bulk_ticket_ids",
            "challenge_fact_check",
            "source_family_mismatch",
            "followup_date_scope",
        ),
    )


def case_chat_128() -> bool:
    primary = _run_dispatcher_with_fake_model(
        _dispatcher_prior_compare_payload(),
        config=_primary_config("prior_result_reference"),
        latest_question="Compare ticket 950012 and 950027 with your previous result.",
        semantic_frame={"intent": "fact_check_prior_answer", "target_context": "prior_turn"},
        previous_assistant_text="Previous tickets: 950053, 950027",
    )
    ok = (
        primary.accepted
        and primary.answer_affecting
        and primary.assistive_task_class == "prior_result_reference"
        and "assistive_prior_result_comparison" in primary.reason_codes
        and primary.compile_decision.get("mode") == "primary_for_eligible"
        and primary.compile_decision.get("execute_actions") is True
        and primary.planner_calls == 1
        and primary.thinking_budget == 0
        and primary.output_tokens_limit <= 2048
    )
    return _check(
        ok,
        "CHAT-128",
        "Project Phase 7 primary mode can promote proven prior-result comparison without changing planner caps",
    )


def case_chat_129() -> bool:
    latest_question = "i just want the one for July and Jun"
    shadow = universal._build_followup_understanding_shadow_payload(
        latest_question=latest_question,
        routing_question=latest_question,
        semantic_frame={
            "target_context": "prior_turn",
            "entities": {"issue_or_category": "internet speed", "store_id": "2009"},
            "task": {"task_type": "ticket_lookup", "labels": ["internet speed"]},
        },
        previous_assistant_text="Earlier answer listed internet-speed tickets for store 2008.",
        conversation_entities={
            "trace_id": "trace-chat-129-prior",
            "turn_id": "turn-chat-129-prior",
            "semantic_labels": ["internet speed", "connection drops"],
            "store_id": "2009",
        },
        messages=[
            {"role": "assistant", "content": "Earlier answer listed internet-speed tickets for store 2008."},
            {"role": "user", "content": latest_question},
        ],
    )
    assist = universal._build_followup_date_scope_assist_payload(
        shadow_payload=shadow,
        latest_question=latest_question,
        routing_question=latest_question,
        previous_assistant_text="Earlier answer listed internet-speed tickets for store 2008.",
        conversation_entities={"trace_id": "trace-chat-129-prior", "turn_id": "turn-chat-129-prior"},
        messages=[
            {"role": "assistant", "content": "Earlier answer listed internet-speed tickets for store 2008."},
            {"role": "user", "content": latest_question},
        ],
    )
    primary_enabled = ai_dispatcher_runtime.followup_date_scope_assist_enabled(
        _primary_config("followup_date_scope")
    )
    sql = universal._followup_date_scope_assist_bq_sql(assist)
    answer = universal._build_followup_date_scope_answer(
        assist_payload=assist,
        rows=[
            {"id": 950052, "subject": "Internet speed degraded", "store_id": "2009", "created_at": "2026-07-03"},
            {"id": 950051, "subject": "Connection drops", "store_id": "2009", "created_at": "2026-06-18"},
        ],
    )
    ok = (
        primary_enabled
        and assist.get("accepted") is True
        and sql is not None
        and "DATE '2026-07-01'" in sql
        and "DATE '2026-06-01'" in sql
        and "store_id = '2008'" in sql
        and answer is not None
        and "July 2026 and June 2026" in answer
        and "not as July 1-2 only" in answer
    )
    return _check(
        ok,
        "CHAT-129",
        "Project Phase 7 primary mode can enable proven follow-up date scope through the existing deterministic contract",
    )


def case_chat_130() -> bool:
    proven = set(getattr(ai_dispatcher_runtime, "PRIMARY_ANSWER_AFFECTING_TASK_CLASSES", ()))
    excluded = {
        "ticket_resolution_lookup",
        "followup_result_set_refinement",
        "followup_ticket_reference",
        "output_critic_shadow",
        "output_critic_bounded_repair",
        "multi_store_ticket_search",
    }
    primary_unproven = _primary_config(
        "ticket_resolution_lookup",
        "followup_result_set_refinement",
        "followup_ticket_reference",
    )
    ok = (
        {"prior_result_reference", "bulk_ticket_ids", "challenge_fact_check", "source_family_mismatch", "followup_date_scope"}.issubset(proven)
        and not (proven & excluded)
        and not ai_dispatcher_runtime.ticket_resolution_lookup_assist_enabled(primary_unproven)
        and not ai_dispatcher_runtime.followup_result_set_refinement_enabled(primary_unproven)
        and not ai_dispatcher_runtime.followup_ticket_reference_enabled(primary_unproven)
    )
    return _check(
        ok,
        "CHAT-130",
        "Project Phase 7 primary allowlist excludes unproven 6E/6H/critic and standalone multi-store classes",
    )


def case_chat_131() -> bool:
    from app.services import ai_dispatcher_admin  # noqa: PLC0415

    safe = ai_dispatcher_admin.validate_config_patch(
        {
            "mode": "primary_for_eligible",
            "assistive_task_classes": [
                "prior_result_reference",
                "bulk_ticket_ids",
                "challenge_fact_check",
                "source_family_mismatch",
                "followup_date_scope",
            ],
            "eligible_task_classes": [
                "prior_result_reference",
                "bulk_ticket_ids",
                "challenge_fact_check",
                "source_family_mismatch",
            ],
            "planner_daily_cap_cad": 2.0,
            "max_planner_calls_per_turn": 1,
            "max_tool_actions": 4,
            "max_explicit_ticket_ids": 75,
            "max_action_rows": 100,
            "max_output_tokens": 1024,
        },
        actor_email="morgan.chen@northstar.example.com",
    )
    public = ai_dispatcher_admin._public_config(safe)
    try:
        ai_dispatcher_admin.validate_config_patch(
            {
                "mode": "primary_for_eligible",
                "assistive_task_classes": ["ticket_resolution_lookup"],
            },
            actor_email="morgan.chen@northstar.example.com",
        )
        rejected_unproven = False
    except ai_dispatcher_admin.DispatcherAdminValidationError:
        rejected_unproven = True
    try:
        ai_dispatcher_admin.validate_config_patch(
            {"mode": "primary_for_eligible", "planner_daily_cap_cad": 20},
            actor_email="morgan.chen@northstar.example.com",
        )
        rejected_cap = False
    except ai_dispatcher_admin.DispatcherAdminValidationError:
        rejected_cap = True

    ok = (
        safe.get("mode") == "primary_for_eligible"
        and public.get("answer_affecting_allowed") is True
        and "primary_for_eligible" in set(public.get("allowed_modes") or [])
        and "primary_for_eligible" not in set(public.get("blocked_modes") or [])
        and rejected_unproven
        and rejected_cap
    )
    return _check(
        ok,
        "CHAT-131",
        "Project Phase 7 admin validation allows only safe proven primary config",
    )


def case_chat_132() -> bool:
    suppress = getattr(universal, "_suppress_ai_dispatcher_trace_without_deterministic_answer", None)
    if suppress is None:
        return _check(False, "CHAT-132", "Project Phase 7 suppression helper is missing")
    trace = {
        "mode": "primary_for_eligible",
        "accepted": True,
        "answer_affecting": True,
        "assistive_task_class": "prior_result_reference",
        "reason_codes": ["primary_prior_result_comparison"],
    }
    suppressed = suppress(
        trace,
        result_set_comparison_answer="",
        bulk_ticket_lookup_answer="",
        challenge_fact_check_answer="",
        source_family_dispatcher_applied=False,
        ticket_semantic_search_answer="",
        followup_date_scope_answer="",
    )
    compiler_accepts_standalone_multi_store = ai_dispatcher_planner.compile_dispatcher_payload(
        _dispatcher_source_family_payload(scope={"store_ids": ["2012", "2002"], "ticket_ids": [], "source_family": "tickets"}),
        mode=ai_dispatcher_planner.DispatcherMode.PRIMARY_FOR_ELIGIBLE,
    )
    ok = (
        suppressed.get("answer_affecting") is False
        and "primary_no_deterministic_answer" in (suppressed.get("reason_codes") or [])
        and compiler_accepts_standalone_multi_store.accepted
        and compiler_accepts_standalone_multi_store.execute_actions
        and "multi_store_ticket_search" not in set(getattr(ai_dispatcher_runtime, "PRIMARY_ANSWER_AFFECTING_TASK_CLASSES", ()))
    )
    return _check(
        ok,
        "CHAT-132",
        "Project Phase 7 suppresses accepted primary traces without deterministic output and keeps compiler acceptance narrower than rollout eligibility",
    )


def case_chat_133() -> bool:
    question = "Для ресторана 2008 есть ли у него проблемы с интернетом, сетью, подключением?"
    store_ids = universal._extract_store_ids(question)
    similar_context = universal_chat._fmt_similar(
        [
            {
                "ticket_id": 950009,
                "created_at": "2025-07-03",
                "solved_at": "2025-07-11",
                "store_id": "2008",
                "brand": "Piazza Kitchen",
                "org_name": "Piazza Kitchen 2008",
                "issue_category": "POS Terminal > Dining Room > Micros Will Not Load",
                "subject": "Serv3 pos offline",
                "resolution": "Reseted Network cable.",
            }
        ],
        "internet network connection for restaurant 2008",
    )
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=question,
        routing_question=question,
        semantic_frame={
            "intent": "store_analytics",
            "target_context": "store",
            "confidence": "medium",
            "entities": {"store_ids": store_ids, "store_id": "2008"},
            "task": {"task_type": "general_answer"},
            "retrieval": {},
        },
        explicit_store_context=store_ids[0] if len(store_ids) == 1 else None,
        explicit_store_ids=store_ids,
        open_ticket_id=950049,
        selected_executor="ticket_analytics",
    )
    ok = (
        store_ids == ["2008"]
        and universal._is_store_issue_breakdown_question(question)
        and not chat_policy.should_use_open_ticket_context(question)
        and contract.scope == "store"
        and contract.filters.get("store_id") == "2008"
        and contract.filters.get("store_ids") == ["2008"]
        and "store_id=2008" in similar_context
    )
    return _check(
        ok,
        "CHAT-133",
        "Russian explicit-store problem question preserves store 2008 scope and suppresses unrelated open-ticket context",
    )


def _store_scope_contract(
    *,
    latest_question: str,
    routing_question: str | None = None,
    semantic_frame: dict | None = None,
    explicit_store_ids: list[str] | None = None,
    selected_executor: str = "ticket_analytics",
):
    ids = list(explicit_store_ids if explicit_store_ids is not None else universal._extract_store_ids(latest_question))
    frame = semantic_frame or {
        "intent": "store_analytics",
        "target_context": "store",
        "confidence": "medium",
        "entities": {"store_ids": ids, "store_id": ids[0] if len(ids) == 1 else None},
        "task": {"task_type": "general_answer"},
        "retrieval": {},
    }
    return semantic_contract_v2.build_shadow_contract(
        latest_question=latest_question,
        routing_question=routing_question or latest_question,
        semantic_frame=frame,
        explicit_store_context=ids[0] if len(ids) == 1 else None,
        explicit_store_ids=ids,
        open_ticket_id=950049,
        selected_executor=selected_executor,
    )


def _ticket_search_contract_for_stores(question: str, store_ids: list[str]):
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=question,
        routing_question=question,
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "store",
            "confidence": "high",
            "entities": {"store_ids": store_ids, "date_phrase": "last 365 days"},
            "task": {"task_type": "general_answer"},
            "retrieval": {
                "search_goal": "internet speed connection tickets",
                "required_concepts": ["internet", "speed", "connection"],
                "output": "ticket_examples",
            },
        },
        explicit_store_ids=store_ids,
        selected_executor="ticket_semantic_search",
    )
    authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    return contract, authorized


def case_chat_134() -> bool:
    question = "Para el restaurante 2008, hay problemas de internet, red o conexión?"
    store_ids = universal._extract_store_ids(question)
    contract = _store_scope_contract(latest_question=question)
    ok = (
        store_ids == ["2008"]
        and universal._is_store_issue_breakdown_question(question)
        and not chat_policy.should_use_open_ticket_context(question)
        and contract.scope == "store"
        and contract.filters.get("store_id") == "2008"
        and contract.filters.get("store_ids") == ["2008"]
    )
    return _check(
        ok,
        "CHAT-134",
        "Spanish explicit-store problem question preserves store 2008 scope and suppresses unrelated open-ticket context",
    )


def case_chat_135() -> bool:
    question = "Pour le restaurant 2008, y a-t-il des problèmes d'internet, de réseau ou de connexion?"
    store_ids = universal._extract_store_ids(question)
    contract = _store_scope_contract(latest_question=question)
    ok = (
        store_ids == ["2008"]
        and universal._is_store_issue_breakdown_question(question)
        and contract.scope == "store"
        and contract.filters.get("store_id") == "2008"
    )
    return _check(
        ok,
        "CHAT-135",
        "French accented problem wording keeps explicit store 2008 issue/evidence scope",
    )


def case_chat_136() -> bool:
    question = "Dlya restorana 2008 est problemy s internetom, setyu ili podklyucheniem?"
    store_ids = universal._extract_store_ids(question)
    contract = _store_scope_contract(latest_question=question)
    ok = (
        store_ids == ["2008"]
        and universal._is_store_issue_breakdown_question(question)
        and not chat_policy.should_use_open_ticket_context(question)
        and contract.scope == "store"
        and contract.filters.get("store_ids") == ["2008"]
    )
    return _check(
        ok,
        "CHAT-136",
        "transliterated Russian explicit-store problem question preserves store scope",
    )


def case_chat_137() -> bool:
    question = "餐厅2008有互联网、网络或连接问题吗？"
    raw_store_ids = universal._extract_store_ids(question)
    contract = _store_scope_contract(
        latest_question=question,
        routing_question="Does restaurant 2008 have internet, network, or connection issues?",
        explicit_store_ids=raw_store_ids,
        semantic_frame={
            "answer_language": "Chinese",
            "intent": "store_analytics",
            "target_context": "store",
            "confidence": "high",
            "entities": {"store_ids": ["2008"], "store_id": "2008"},
            "task": {"task_type": "general_answer"},
            "retrieval": {},
        },
    )
    ok = (
        raw_store_ids == []
        and contract.scope == "store"
        and contract.filters.get("store_id") == "2008"
        and contract.filters.get("store_ids") == ["2008"]
    )
    return _check(
        ok,
        "CHAT-137",
        "Chinese semantic-frame store IDs drive the route contract when raw extraction is insufficient",
    )


def case_chat_138() -> bool:
    question = "هل لدى المطعم 2008 مشاكل في الإنترنت أو الشبكة؟"
    raw_store_ids = universal._extract_store_ids(question)
    contract = _store_scope_contract(
        latest_question=question,
        routing_question="Does restaurant 2008 have internet or network issues?",
        explicit_store_ids=raw_store_ids,
        semantic_frame={
            "answer_language": "Arabic",
            "intent": "store_analytics",
            "target_context": "store",
            "confidence": "high",
            "entities": {"store_ids": ["2008"], "store_id": "2008"},
            "task": {"task_type": "general_answer"},
            "retrieval": {},
        },
    )
    ok = (
        raw_store_ids == []
        and contract.scope == "store"
        and contract.filters.get("store_id") == "2008"
        and contract.filters.get("store_ids") == ["2008"]
    )
    return _check(
        ok,
        "CHAT-138",
        "Arabic semantic-frame store IDs drive the route contract when raw extraction is insufficient",
    )


def case_chat_139() -> bool:
    question = "Para las tiendas 2012, 2011 y 2002, busca tickets de internet, velocidad o conexión de los últimos 365 días."
    store_ids = universal._extract_store_ids(question)
    contract, authorized = _ticket_search_contract_for_stores(question, store_ids)
    ok = (
        store_ids == ["2012", "2011", "2002"]
        and contract.scope == "store"
        and contract.filters.get("store_ids") == store_ids
        and contract.filters.get("store_id") in (None, "")
        and authorized
    )
    return _check(
        ok,
        "CHAT-139",
        "Spanish multi-store retrieval preserves every requested store ID through ticket semantic search contract",
    )


def case_chat_140() -> bool:
    question = "Pour les restaurants 2012, 2011 et 2002, trouve les tickets internet, vitesse ou connexion des 365 derniers jours."
    store_ids = universal._extract_store_ids(question)
    contract, authorized = _ticket_search_contract_for_stores(question, store_ids)
    ok = (
        store_ids == ["2012", "2011", "2002"]
        and contract.scope == "store"
        and contract.filters.get("store_ids") == store_ids
        and contract.filters.get("store_id") in (None, "")
        and authorized
    )
    return _check(
        ok,
        "CHAT-140",
        "French multi-store retrieval preserves every requested store ID through ticket semantic search contract",
    )


def case_chat_141() -> bool:
    latest_question = "Encuentra ejemplos de tickets para tienda 2012 donde un ingeniero abrió un ticket con Telecom Partner por una línea degradada."
    routing_question = "Find examples of tickets for store 2012 where an engineer opened a Telecom Partner ticket for a degraded line."
    frame = {
        "intent": "ticket_retrieval",
        "target_context": "store",
        "retrieval": {
            "output": "ticket_examples",
            "search_goal": "Telecom Partner degraded line opened vendor ticket",
            "required_concepts": ["Telecom Partner", "degraded line", "opened vendor ticket"],
        },
    }
    candidate = ai_dispatcher_runtime.source_family_mismatch_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=frame,
    )
    ok = candidate is True
    return _check(
        ok,
        "CHAT-141",
        "Spanish ticket-example ask can use normalized routing text for source-family mismatch detection",
    )


def case_chat_142() -> bool:
    latest_question = "A quien escalo DispatchFlow?"
    routing_question = "Who do I escalate DispatchFlow issues to?"
    frame = {"intent": "procedure_lookup", "target_context": "global"}
    candidate = ai_dispatcher_runtime.source_family_mismatch_candidate(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=frame,
    )
    forced_drive = chat_policy.should_force_drive_tabular_lookup(routing_question, semantic_frame=frame)
    suppressed_similar = chat_policy.should_suppress_similar_tickets(routing_question, semantic_frame=frame)
    ok = forced_drive and suppressed_similar and not candidate
    return _check(
        ok,
        "CHAT-142",
        "Spanish escalation/contact control stays Drive/procedure and does not trigger ticket source-family mismatch",
    )


def case_chat_143() -> bool:
    try:
        from app.services import followup_result_set_assist  # noqa: PLC0415
    except Exception as exc:
        return _check(False, "CHAT-143", f"saved result-set follow-up service missing: {exc}")

    result_set = _phase6h_result_set_fixture()
    omitted = _phase6h_payload(
        followup_result_set_assist.build_followup_result_set_assist(
            latest_question="какие тикеты ты пропустил и почему?",
            routing_question="which tickets did you omit and why?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="Я показал один тикет из большего сохраненного результата.",
            result_set=result_set,
        )
    )
    answer = followup_result_set_assist.build_result_set_refinement_answer(
        omitted,
        result_set,
        answer_language="Russian",
    )
    ok = (
        omitted.get("accepted") is True
        and omitted.get("request_kind") == "omitted_result_set"
        and omitted.get("displayed_ticket_ids") == [950052]
        and omitted.get("omitted_ticket_ids") == [950051, 950053]
        and answer is not None
        and "сохраненный предыдущий структурированный результат" in answer
        and "#950051" in answer
        and "#950053" in answer
        and "RAW TICKET FULL TEXT" not in answer
    )
    return _check(
        ok,
        "CHAT-143",
        "Russian omitted-ticket follow-up uses saved structured result-set memory and keeps raw ticket text hidden",
    )


def case_chat_151() -> bool:
    from app.services import ai_dispatcher_admin  # noqa: PLC0415

    question = (
        "For every store 2012 2011 2002 2003 2004 2006 find all "
        "internet/speed/connection tickets for the last 365 days"
    )
    with mock.patch.object(ai_dispatcher_runtime, "_get_model", side_effect=AssertionError("planner call forbidden")):
        candidate = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question=question,
            routing_question=question,
            messages=[{"role": "user", "content": question}],
            semantic_frame={"intent": "ticket_retrieval", "target_context": "store"},
            conversation_id="qa-exposure",
            turn_id="turn-exposure",
            trace_id="trace-exposure",
            owner_role="user",
            explicit_store_ids=["2012", "2011", "2002", "2003", "2004", "2006"],
            explicit_ticket_id=None,
            previous_assistant_text="",
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.DISABLED,
            ),
        )
        routine = ai_dispatcher_runtime.run_dispatcher_shadow(
            latest_question="How do I reset an internet router?",
            routing_question="How do I reset an internet router?",
            messages=[{"role": "user", "content": "How do I reset an internet router?"}],
            semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
            conversation_id="qa-exposure",
            turn_id="turn-routine",
            trace_id="trace-routine",
            owner_role="user",
            config=ai_dispatcher_runtime.DispatcherRuntimeConfig(
                mode=ai_dispatcher_planner.DispatcherMode.DISABLED,
            ),
        )

    trace = {
        "trace_id": "trace-exposure",
        "turn_id": "turn-exposure",
        "conversation_id": "qa-exposure",
        **candidate.to_trace_dict(),
        "messages": "RAW USER TEXT",
        "final_answer": "RAW ANSWER",
    }
    dashboard = ai_dispatcher_admin.summarize_dashboard(
        config={"mode": "disabled"},
        traces=[trace],
        quality_events=[
            {"trace_id": "trace-exposure", "quality": "suspect"},
            {"trace_id": "trace-exposure", "quality": "suspect"},
        ],
        turn_rows=[{"trace_id": "trace-exposure", "latency_ms": 4200}],
    )
    multi_store = next(
        row for row in dashboard["class_exposure"]
        if row["task_class"] == "multi_store_ticket_search"
    )
    ok = (
        candidate.planner_calls == 0
        and candidate.answer_affecting is False
        and "multi_store_ticket_search" in candidate.candidate_classes
        and routine.candidate_classes == ()
        and multi_store["candidate_turns"] == 1
        and multi_store["planner_attempted_turns"] == 0
        and multi_store["answer_affecting_turns"] == 0
        and multi_store["quality_turns"] == 1
        and multi_store["turn_latency"]["p50_ms"] == 4200.0
        and dashboard["quality_join"]["quality_event_count"] == 2
        and dashboard["quality_join"]["unique_quality_answer_traces"] == 1
        and not ai_dispatcher_admin.payload_contains_raw_text_keys(dashboard)
    )
    return _check(
        ok,
        "CHAT-151",
        "disabled multi-store opportunity reaches the raw-free per-class funnel without planner calls or answer impact",
    )


def case_chat_152() -> bool:
    event = {
        "trace_id": "trace-review-taxonomy",
        "turn_id": "turn-review-taxonomy",
        "conversation_id": "conversation-review-taxonomy",
        "created_at": "2026-07-13T12:00:00Z",
        "quality": "insufficient_evidence",
        "problem_types": ["ambiguous_question", "wrong_routing"],
        "review_route_signals": {
            "ticket_resolution_candidate": True,
            "risk_classes": ["missing_prior_context"],
        },
        "question": "RAW USER TEXT",
        "answer": "RAW ANSWER TEXT",
    }
    conflicting_review = {
        "trace_id": "trace-review-taxonomy",
        "turn_id": "turn-review-taxonomy",
        "created_at": "2026-07-13T12:01:00Z",
        "quality": "good",
        "reviewer_note": "RAW REVIEW NOTE",
    }
    turn = {
        "trace_id": "trace-review-taxonomy",
        "turn_id": "turn-review-taxonomy",
        "conversation_id": "conversation-review-taxonomy",
        "latency_ms": quality_telemetry.REVIEW_SLOW_LATENCY_MS,
        "answer_chars": quality_telemetry.REVIEW_TOO_LONG_ANSWER_CHARS,
        "sources_count": 0,
        "tickets_count": 0,
    }
    queue = quality_telemetry.build_review_queue(
        quality_events=[event, conflicting_review],
        turn_rows=[turn],
    )
    below_boundary = quality_telemetry.classify_review_reason_tags(
        {"quality": "good"},
        {
            "latency_ms": quality_telemetry.REVIEW_SLOW_LATENCY_MS - 1,
            "answer_chars": quality_telemetry.REVIEW_TOO_LONG_ANSWER_CHARS - 1,
        },
    )
    row = queue["rows"][0] if queue.get("rows") else {}
    ok = (
        tuple(row.get("reason_tags") or ()) == quality_telemetry.REVIEW_REASON_TAGS
        and queue.get("answer_count") == 1
        and row.get("event_count") == 2
        and row.get("conflicting_verdict") is True
        and below_boundary == ()
        and not quality_telemetry.contains_raw_text_keys(queue)
        and "RAW" not in repr(queue)
    )
    return _check(
        ok,
        "CHAT-152",
        "six deterministic raw-free review tags respect boundaries and duplicate/conflicting reviews remain one answer",
    )


def case_chat_153() -> bool:
    from app.services import quality_telemetry as stage_quality_telemetry  # noqa: PLC0415

    clock_values = iter([1.0, 1.125, 2.0, 2.900, 3.0, 3.010])
    timings = universal.UniversalStageTimings(clock=lambda: next(clock_values))
    timings.start("interpretation")
    timings.finish("interpretation")
    timings.start("retrieval")
    timings.finish("retrieval")
    timings.start("output_critic")
    timings.finish("output_critic")
    timings.start("raw_question")
    snapshot = timings.snapshot()
    summary = stage_quality_telemetry.summarize_stage_timings([
        {"stage_timings": snapshot},
        {"stage_timings": {"durations_ms": {"interpretation": 375}}},
        {},
    ])
    stages = {row["stage"]: row for row in summary["stages"]}
    ok = (
        snapshot["durations_ms"] == {
            "interpretation": 125,
            "retrieval": 900,
            "output_critic": 10,
        }
        and snapshot["partial"] is True
        and "raw_question" not in repr(snapshot)
        and summary["measured_turn_count"] == 2
        and summary["unmeasured_turn_count"] == 1
        and stages["interpretation"]["p50_ms"] == 125.0
        and stages["interpretation"]["p90_ms"] == 375.0
        and stages["validator_repair"]["count"] == 0
        and stages["validator_repair"]["p50_ms"] is None
    )
    return _check(
        ok,
        "CHAT-153",
        "allowlisted monotonic stage timings aggregate partial paths without inventing zero-duration stages",
    )


def case_chat_154() -> bool:
    import requests  # noqa: PLC0415
    from app.services import error_provenance  # noqa: PLC0415

    response = requests.Response()
    response.status_code = 503
    response._content = b'{"token":"RAW SECRET","customer":"RAW CUSTOMER TEXT"}'
    upstream = error_provenance.classify_exception(
        requests.HTTPError("RAW SECRET", response=response),
        endpoint_class="zendesk_ticket",
        source_hint="zendesk",
    )
    timeout = error_provenance.classify_exception(
        requests.Timeout("RAW SECRET"),
        endpoint_class="zendesk_search",
        source_hint="zendesk",
    )
    fallback = error_provenance.fallback_event(
        endpoint_class="customer_risk_model",
        reason_code="customer_risk_model_invalid_payload",
    )
    internal = error_provenance.classify_exception(
        RuntimeError("RAW SECRET"),
        endpoint_class="customer_risk_analytics",
    )
    events = [upstream, timeout, fallback, internal]
    summary = quality_telemetry.summarize_error_provenance(events)
    ok = (
        upstream["outcome"] == "upstream_http_error"
        and upstream["status_code"] == 503
        and timeout["outcome"] == "upstream_timeout"
        and fallback["outcome"] == "esip_fallback_used"
        and fallback["fallback_used"] is True
        and internal["outcome"] == "esip_failure"
        and summary["event_count"] == 4
        and "RAW" not in repr(events)
        and "token" not in repr(events).lower()
    )
    return _check(
        ok,
        "CHAT-154",
        "upstream Zendesk, successful deterministic fallback, and ESIP failure remain distinct compact raw-free outcomes",
    )


def case_chat_155() -> bool:
    from app.services import followup_result_set_assist  # noqa: PLC0415

    result_set = _phase6h_result_set_fixture()
    forbidden = AssertionError("result-set refinement shadow external call forbidden")
    with (
        mock.patch.object(ai_dispatcher_runtime, "_get_model", side_effect=forbidden) as model_call,
        mock.patch.object(bigquery, "bulk_lookup_ticket_ids", side_effect=forbidden) as bq_call,
        mock.patch.object(universal.vertex_search, "search_kb", side_effect=forbidden) as kb_call,
        mock.patch.object(universal.vertex_search, "search_tickets", side_effect=forbidden) as ticket_call,
        mock.patch.object(universal, "_run_drive_exact", side_effect=forbidden) as drive_call,
        mock.patch.object(universal, "_run_store_zendesk_history", side_effect=forbidden) as zendesk_call,
        mock.patch.object(universal.ai_dispatcher_result_sets, "save_result_set", side_effect=forbidden) as save_call,
        mock.patch.object(
            universal,
            "_build_followup_result_set_refinement_answer",
            side_effect=forbidden,
        ) as answer_call,
    ):
        accepted = universal._build_followup_result_set_refinement_shadow_payload(
            latest_question="only the open ones for store 2003",
            routing_question="only the open ones for store 2003",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
        no_context = universal._build_followup_result_set_refinement_shadow_payload(
            latest_question="only the open ones",
            routing_question="only the open ones",
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="",
            result_set=None,
        )
        ticket_reference = universal._build_followup_result_set_refinement_shadow_payload(
            latest_question="what about the first one?",
            routing_question="what about the first one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
        unsupported = universal._build_followup_result_set_refinement_shadow_payload(
            latest_question="more details",
            routing_question="more details",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_retrieval"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
    competing_candidates = [
        followup_result_set_assist.detect_followup_result_set_candidate(
            latest_question=question,
            routing_question=question,
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="I listed matching tickets.",
        )
        for question in (
            "i just want the one for July and Jun",
            "what fixed the first one?",
            "how do I reset this from the KB?",
            "where did you find that claim?",
            "Compare 950051 and 950053 with the prior result",
        )
    ]
    ok = (
        accepted.get("accepted") is True
        and accepted.get("answer_affecting") is False
        and accepted.get("enabled_as_shadow") is True
        and accepted.get("selected_count") == 1
        and accepted.get("filter_names") == ["statuses", "store_ids"]
        and no_context.get("accepted") is False
        and "missing_result_set" in (no_context.get("reason_codes") or [])
        and ticket_reference.get("accepted") is False
        and "different_shadow_class_ticket_reference" in (ticket_reference.get("reason_codes") or [])
        and unsupported.get("accepted") is False
        and "unsupported_result_set_followup" in (unsupported.get("reason_codes") or [])
        and all(not candidate.is_candidate for candidate in competing_candidates)
        and all(
            item.call_count == 0
            for item in (
                model_call,
                bq_call,
                kb_call,
                ticket_call,
                drive_call,
                zendesk_call,
                save_call,
                answer_call,
            )
        )
        and "950053" not in repr(accepted)
        and "RAW" not in repr(accepted)
    )
    return _check(
        ok,
        "CHAT-155",
        "configured saved result-set refinement shadow is compact, no-call, non-answer-affecting, and rejects conflicts",
    )


def case_chat_156() -> bool:
    from app.services import followup_result_set_assist  # noqa: PLC0415

    result_set = _phase6h_result_set_fixture()
    result_set["displayed_ticket_ids"] = [950052, 950051, 950053, 950051]
    forbidden = AssertionError("ticket-reference shadow external call forbidden")
    with (
        mock.patch.object(ai_dispatcher_runtime, "_get_model", side_effect=forbidden) as model_call,
        mock.patch.object(bigquery, "bulk_lookup_ticket_ids", side_effect=forbidden) as bq_call,
        mock.patch.object(universal.vertex_search, "search_kb", side_effect=forbidden) as kb_call,
        mock.patch.object(universal.vertex_search, "search_tickets", side_effect=forbidden) as ticket_call,
        mock.patch.object(universal, "_run_drive_exact", side_effect=forbidden) as drive_call,
        mock.patch.object(universal, "_run_store_zendesk_history", side_effect=forbidden) as zendesk_call,
        mock.patch.object(universal.ai_dispatcher_result_sets, "save_result_set", side_effect=forbidden) as save_call,
        mock.patch.object(
            universal,
            "_build_followup_ticket_reference_answer",
            side_effect=forbidden,
        ) as answer_call,
    ):
        resolved = {
            ordinal: universal._build_followup_ticket_reference_shadow_payload(
                latest_question=f"what about the {ordinal} one?",
                routing_question=f"what about the {ordinal} one?",
                semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
                previous_assistant_text="I listed matching tickets.",
                result_set=result_set,
            )
            for ordinal in ("first", "second", "last")
        }
        ambiguous = universal._build_followup_ticket_reference_shadow_payload(
            latest_question="what about that one?",
            routing_question="what about that one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
        one_displayed = _phase6h_result_set_fixture()
        out_of_range = universal._build_followup_ticket_reference_shadow_payload(
            latest_question="what about the second one?",
            routing_question="what about the second one?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed one matching ticket.",
            result_set=one_displayed,
        )
        explicit = universal._build_followup_ticket_reference_shadow_payload(
            latest_question="what about ticket 950012?",
            routing_question="what about ticket 950012?",
            semantic_frame={"target_context": "prior_turn", "intent": "ticket_details"},
            previous_assistant_text="I listed matching tickets.",
            result_set=result_set,
        )
        missing = universal._build_followup_ticket_reference_shadow_payload(
            rejection_reason="missing_result_set"
        )
    competing_candidates = [
        followup_result_set_assist.detect_followup_result_set_candidate(
            latest_question=question,
            routing_question=question,
            semantic_frame={"target_context": "prior_turn"},
            previous_assistant_text="I listed matching tickets.",
        )
        for question in (
            "what about the first one in July?",
            "what fixed the first one?",
            "how do I fix the first one?",
            "where is the evidence for the first one?",
            "compare 950052 and 950053",
            "what about ticket 950012?",
        )
    ]
    result_shadow_for_ticket = followup_result_set_assist.build_result_set_refinement_shadow_observation(
        _phase6h_payload(
            followup_result_set_assist.build_followup_result_set_assist(
                latest_question="what about the first one?",
                routing_question="what about the first one?",
                semantic_frame={"target_context": "prior_turn"},
                previous_assistant_text="I listed matching tickets.",
                result_set=result_set,
            )
        )
    )
    ok = (
        resolved["first"].get("target_ticket_id") == 950052
        and resolved["second"].get("target_ticket_id") == 950051
        and resolved["last"].get("target_ticket_id") == 950053
        and all(item.get("accepted") is True for item in resolved.values())
        and all(item.get("lookup_performed") is False for item in resolved.values())
        and all(item.get("answer_affecting") is False for item in resolved.values())
        and ambiguous.get("accepted") is False
        and "ambiguous_ticket_reference" in (ambiguous.get("reason_codes") or [])
        and out_of_range.get("accepted") is False
        and "ordinal_out_of_range" in (out_of_range.get("reason_codes") or [])
        and explicit.get("accepted") is False
        and "explicit_ticket_lookup_intent" in (explicit.get("reason_codes") or [])
        and missing.get("accepted") is False
        and "missing_result_set" in (missing.get("reason_codes") or [])
        and all(not candidate.is_candidate for candidate in competing_candidates)
        and result_shadow_for_ticket.get("accepted") is False
        and "different_shadow_class_ticket_reference" in (result_shadow_for_ticket.get("reason_codes") or [])
        and all(
            item.call_count == 0
            for item in (
                model_call,
                bq_call,
                kb_call,
                ticket_call,
                drive_call,
                zendesk_call,
                save_call,
                answer_call,
            )
        )
        and all(key not in repr(resolved) for key in ("RAW", "assistant", "full_text", "bq_sql", "cards", "sources"))
    )
    return _check(
        ok,
        "CHAT-156",
        "ticket-reference shadow resolves visible ordinals without lookup and rejects ambiguity, hidden ordinals, explicit IDs, and conflicts",
    )


def case_chat_157() -> bool:
    suspect = output_critic_shadow.compact_trace_metadata(
        output_critic_shadow.normalize_output_critic_payload(
            {
                "risk_level": "high",
                "risk_codes": ["followup_scope_loss"],
                "confidence": 0.91,
                "reason_short": "The response narrowed June and July to July 1-2.",
                "raw_model_response": "RAW MODEL RESPONSE",
            }
        )
    )
    good = output_critic_shadow.compact_trace_metadata(
        output_critic_shadow.normalize_output_critic_payload(
            {"risk_level": "none", "risk_codes": [], "confidence": 0.7, "reason_short": "Supported."}
        )
    )
    disabled_repair = output_critic_bounded_repair.evaluate_output_critic_bounded_repair(
        latest_question="i just want the one for July and Jun",
        visible_answer="I found these for July 1-2 only.",
        output_critic_trace=suspect,
        enabled=False,
    )
    rendered = repr(suspect) + repr(good) + repr(disabled_repair.to_trace_dict())
    ok = (
        suspect.get("risk_level") == "high"
        and suspect.get("risk_codes") == ["followup_scope_loss"]
        and suspect.get("answer_affecting") is False
        and good.get("risk_level") == "none"
        and good.get("answer_affecting") is False
        and disabled_repair.repair_applied is False
        and disabled_repair.skipped_reason == "disabled_by_config"
        and "RAW MODEL RESPONSE" not in rendered
    )
    return _check(ok, "CHAT-157", "output critic remains raw-free shadow metadata and bounded repair stays disabled")


def case_chat_158() -> bool:
    from app.services import ai_dispatcher_admin  # noqa: PLC0415

    question = (
        "For every store 2012 2011 2002 2003 2004 2006 find all "
        "internet/speed/connection interruption related tickets for the last 365 days"
    )
    store_ids = universal._extract_store_ids(question)
    candidate = ai_dispatcher_runtime.detect_dispatcher_candidates(
        latest_question=question,
        routing_question=question,
        messages=[{"role": "user", "content": question}],
        semantic_frame={"intent": "ticket_retrieval", "target_context": "global"},
        deterministic_entities=ai_dispatcher_planner.DispatcherDeterministicEntities(
            store_ids=tuple(store_ids),
        ),
    )
    procedure = "How do I reset routers at stores 2012 and 2011?"
    procedure_candidate = ai_dispatcher_runtime.detect_dispatcher_candidates(
        latest_question=procedure,
        routing_question=procedure,
        messages=[{"role": "user", "content": procedure}],
        semantic_frame={"intent": "procedure_lookup", "target_context": "global"},
        deterministic_entities=ai_dispatcher_planner.DispatcherDeterministicEntities(
            store_ids=("2012", "2011"),
        ),
    )
    defaults = ai_dispatcher_runtime.DispatcherRuntimeConfig()
    ok = (
        store_ids == ["2012", "2011", "2002", "2003", "2004", "2006"]
        and "multi_store_ticket_search" in candidate.classes
        and "multi_store_ticket_search" not in procedure_candidate.classes
        and chat_policy.extract_store_ids("For store 2008 find internet tickets") == ["2008"]
        and chat_policy.extract_explicit_ticket_id("For store 2008 find internet tickets") is None
        and chat_policy.extract_store_ids("Show ticket #1048") == []
        and chat_policy.extract_explicit_ticket_id("Show ticket #1048") == 1048
        and defaults.max_tool_actions == 4
        and defaults.max_explicit_ticket_ids == 75
        and defaults.max_action_rows == 100
        and "multi_store_ticket_search" not in ai_dispatcher_admin.PRIMARY_PLANNER_TASK_CLASSES
        and "multi_store_ticket_search" not in ai_dispatcher_runtime.PRIMARY_ANSWER_AFFECTING_TASK_CLASSES
    )
    return _check(
        ok,
        "CHAT-158",
        "multi-store scope preserves all six stores while standalone multi-store remains outside primary",
    )


def case_chat_159() -> bool:
    current_ticket = chat_policy.should_use_open_ticket_context("How to resolve this?")
    exact_procedure = chat_policy.should_force_drive_tabular_lookup("Who do I escalate DispatchFlow issues to?")
    suppress_ticket_examples = chat_policy.should_suppress_similar_tickets("Who do I escalate DispatchFlow issues to?")
    incidental_pos = ai_dispatcher_runtime.source_family_mismatch_candidate(
        latest_question="Extra bacon modifier is not appearing on KDS at our Simphony store.",
        routing_question="Extra bacon modifier is not appearing on KDS at our Simphony store.",
        semantic_frame={"intent": "procedure_lookup", "target_context": "store"},
    )
    ok = current_ticket and exact_procedure and suppress_ticket_examples and not incidental_pos
    return _check(
        ok,
        "CHAT-159",
        "current-ticket and exact-procedure routes retain precedence while incidental source words stay negative",
    )


def case_chat_160() -> bool:
    context = universal._build_output_critic_shadow_context(
        routing_question="Question",
        semantic_frame={"intent": "ticket_retrieval"},
        phase1_decision={"selected_executor": "ticket_semantic_search", "prompt": "RAW PROMPT"},
        policy_flags={"selected_executor": "ticket_semantic_search", "raw_prompt": "SECRET PROMPT"},
        quality_gate_flags={},
        answer_validation={},
        counts={"sources": 1, "tickets": 1, "answer_chars": 18},
        sources=[{"title": "Evidence", "html_body": "RAW HTML"}],
        tickets=[{"ticket_id": 950053, "full_text": "RAW TICKET FULL TEXT"}],
        ai_dispatcher_trace={"answer_affecting": False, "raw_response": "RAW DISPATCHER"},
        followup_shadow_payload=None,
        followup_date_scope_payload=None,
        ticket_resolution_lookup_payload=None,
        conversation_entities_loaded=False,
        conversation_entities_saved=None,
    )
    rendered = repr(context)
    forbidden = ("RAW PROMPT", "SECRET PROMPT", "RAW HTML", "RAW TICKET FULL TEXT", "RAW DISPATCHER")
    ok = all(value not in rendered for value in forbidden) and context.get("counts", {}).get("tickets") == 1
    return _check(ok, "CHAT-160", "compact critic context preserves counts and strips raw prompt, model, and ticket artifacts")


CASES = {
    "CHAT-013": case_chat_013,
    "CHAT-014": case_chat_014,
    "CHAT-015": case_chat_015,
    "CHAT-016": case_chat_016,
    "CHAT-017": case_chat_017,
    "CHAT-018": case_chat_018,
    "CHAT-019": case_chat_019,
    "CHAT-020": case_chat_020,
    "CHAT-023": case_chat_023,
    "CHAT-024": case_chat_024,
    "CHAT-025": case_chat_025,
    "CHAT-026": case_chat_026,
    "CHAT-031": case_chat_031,
    "CHAT-032": case_chat_032,
    "CHAT-033": case_chat_033,
    "CHAT-035": case_chat_035,
    "CHAT-036": case_chat_036,
    "CHAT-037": case_chat_037,
    "CHAT-038": case_chat_038,
    "CHAT-039": case_chat_039,
    "CHAT-044": case_chat_044,
    "CHAT-045": case_chat_045,
    "CHAT-046": case_chat_046,
    "CHAT-047": case_chat_047,
    "CHAT-048": case_chat_048,
    "CHAT-049": case_chat_049,
    "CHAT-050": case_chat_050,
    "CHAT-051": case_chat_051,
    "CHAT-052": case_chat_052,
    "CHAT-053": case_chat_053,
    "CHAT-064": case_chat_064,
    "CHAT-065": case_chat_065,
    "CHAT-066": case_chat_066,
    "CHAT-070": case_chat_070,
    "CHAT-055": case_chat_055,
    "CHAT-056": case_chat_056,
    "CHAT-059": case_chat_059,
    "CHAT-060": case_chat_060,
    "CHAT-062": case_chat_062,
    "CHAT-063": case_chat_063,
    "CHAT-099": case_chat_099,
    "CHAT-100": case_chat_100,
    "CHAT-101": case_chat_101,
    "CHAT-102": case_chat_102,
    "CHAT-103": case_chat_103,
    "CHAT-104": case_chat_104,
    "CHAT-105": case_chat_105,
    "CHAT-106": case_chat_106,
    "CHAT-107": case_chat_107,
    "CHAT-108": case_chat_108,
    "CHAT-109": case_chat_109,
    "CHAT-110": case_chat_110,
    "CHAT-111": case_chat_111,
    "CHAT-112": case_chat_112,
    "CHAT-113": case_chat_113,
    "CHAT-114": case_chat_114,
    "CHAT-115": case_chat_115,
    "CHAT-116": case_chat_116,
    "CHAT-117": case_chat_117,
    "CHAT-118": case_chat_118,
    "CHAT-119": case_chat_119,
    "CHAT-120": case_chat_120,
    "CHAT-121": case_chat_121,
    "CHAT-122": case_chat_122,
    "CHAT-123": case_chat_123,
    "CHAT-124": case_chat_124,
    "CHAT-125": case_chat_125,
    "CHAT-126": case_chat_126,
    "CHAT-127": case_chat_127,
    "CHAT-128": case_chat_128,
    "CHAT-129": case_chat_129,
    "CHAT-130": case_chat_130,
    "CHAT-131": case_chat_131,
    "CHAT-132": case_chat_132,
    "CHAT-133": case_chat_133,
    "CHAT-134": case_chat_134,
    "CHAT-135": case_chat_135,
    "CHAT-136": case_chat_136,
    "CHAT-137": case_chat_137,
    "CHAT-138": case_chat_138,
    "CHAT-139": case_chat_139,
    "CHAT-140": case_chat_140,
    "CHAT-141": case_chat_141,
    "CHAT-142": case_chat_142,
    "CHAT-143": case_chat_143,
    "CHAT-151": case_chat_151,
    "CHAT-152": case_chat_152,
    "CHAT-153": case_chat_153,
    "CHAT-154": case_chat_154,
    "CHAT-155": case_chat_155,
    "CHAT-156": case_chat_156,
    "CHAT-157": case_chat_157,
    "CHAT-158": case_chat_158,
    "CHAT-159": case_chat_159,
    "CHAT-160": case_chat_160,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run targeted chat experiment QA cases.")
    parser.add_argument("--case", choices=sorted(CASES), action="append", help="Run one case. Repeat to run multiple cases.")
    parser.add_argument("--all", action="store_true", help="Run all cases.")
    args = parser.parse_args()
    selected = list(args.case or sorted(CASES))
    if args.all:
        selected = sorted(CASES)

    results = [CASES[case_id]() for case_id in selected]
    passed = sum(1 for ok in results if ok)
    print(f"RESULTS: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
