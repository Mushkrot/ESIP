"""Deterministic Customer Risk analytics for Universal Chat."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from google.cloud import bigquery as bq

from app.models.customer_risk import CustomerRiskMetrics, CustomerRiskResponse
from app.services import ticket_customer_risk
from app.services.bigquery import BQ_DATASET, GCP_PROJECT, _get_client


CustomerRiskChatScope = Literal["ticket", "store_tickets", "ticket_list", "store_ranking"]


@dataclass(frozen=True)
class CustomerRiskChatRequest:
    scope: CustomerRiskChatScope
    question: str
    ticket_id: int | None = None
    store_id: str | None = None
    followup_min: int | None = None
    require_after_agent_followups: bool = False
    since_days: int | None = None
    minimum_score: int = 20
    limit: int = 10


def detect_customer_risk_request(
    *,
    latest_question: str,
    routing_question: str,
    explicit_ticket_id: int | None,
    target_ticket_id: int | None,
    explicit_store_id: str | None,
) -> CustomerRiskChatRequest | None:
    """Detect chat asks that should use Customer Risk analytics instead of generated SQL."""
    combined = f"{latest_question}\n{routing_question}".strip()
    text = _normalize_text(combined)
    if not _looks_like_customer_risk_text(text):
        return None

    followup_min = _extract_followup_min(text)
    require_after_agent_followups = _requires_after_agent_followups(text)
    since_days = _extract_since_days(text)
    minimum_score = _minimum_score_for_text(text)
    limit = _extract_limit(text, default=10)

    ticket_id = explicit_ticket_id or (
        target_ticket_id if re.search(r"\b(?:this|current|open)\s+ticket\b", text) else None
    )
    if ticket_id:
        return CustomerRiskChatRequest(
            scope="ticket",
            question=combined,
            ticket_id=int(ticket_id),
            followup_min=followup_min,
            require_after_agent_followups=require_after_agent_followups,
            since_days=since_days,
            minimum_score=minimum_score,
            limit=1,
        )

    if explicit_store_id:
        return CustomerRiskChatRequest(
            scope="store_tickets",
            question=combined,
            store_id=str(explicit_store_id),
            followup_min=followup_min,
            require_after_agent_followups=require_after_agent_followups,
            since_days=since_days,
            minimum_score=minimum_score,
            limit=limit,
        )

    if _asks_store_ranking(text):
        return CustomerRiskChatRequest(
            scope="store_ranking",
            question=combined,
            followup_min=followup_min,
            require_after_agent_followups=require_after_agent_followups,
            since_days=since_days,
            minimum_score=max(minimum_score, 45 if "elevated" in text else minimum_score),
            limit=limit,
        )

    return CustomerRiskChatRequest(
        scope="ticket_list",
        question=combined,
        followup_min=followup_min,
        require_after_agent_followups=require_after_agent_followups,
        since_days=since_days,
        minimum_score=minimum_score,
        limit=limit,
    )


def build_customer_risk_chat_response(request: CustomerRiskChatRequest) -> dict[str, Any]:
    """Build a deterministic answer payload for a Customer Risk chat request."""
    if request.scope == "ticket":
        if not request.ticket_id:
            raise ValueError("ticket_id is required for ticket Customer Risk requests")
        return _build_ticket_response(request.ticket_id)

    rows = fetch_customer_risk_ticket_rows(
        store_id=request.store_id,
        since_days=request.since_days,
        limit=max(150, request.limit * 30),
    )
    rows = _filter_rows_for_request(rows, request)
    rows = _sort_risk_rows(rows)

    if request.scope == "store_ranking":
        return _build_store_ranking_response(request, rows)
    return _build_ticket_list_response(request, rows)


def fetch_customer_risk_ticket_rows(
    *,
    store_id: str | None = None,
    since_days: int | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Fetch active ticket public-comment signals and score them with panel rules."""
    client = _get_client()
    ticket_table = f"`{GCP_PROJECT}.{BQ_DATASET}.tickets`"
    comments_table = f"`{GCP_PROJECT}.{BQ_DATASET}.ticket_comments`"

    where_parts = ["LOWER(COALESCE(t.status, '')) NOT IN ('solved', 'closed')"]
    params: list[bq.ScalarQueryParameter] = []
    if store_id:
        where_parts.append("t.store_id = @store_id")
        params.append(bq.ScalarQueryParameter("store_id", "STRING", str(store_id)))
    if since_days:
        days = max(1, min(int(since_days), 3650))
        where_parts.append(
            f"COALESCE(t.updated_at, t.created_at) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)"
        )
    where_sql = "\n  AND ".join(where_parts)
    row_limit = max(1, min(int(limit), 5000))

    customer_condition = (
        "c.is_internal = FALSE AND "
        "(LOWER(COALESCE(c.author_role, '')) = 'end-user' OR c.author_is_agent IS FALSE)"
    )
    agent_condition = (
        "c.is_internal = FALSE AND "
        "(c.author_is_agent IS TRUE OR LOWER(COALESCE(c.author_role, '')) IN ('agent', 'admin'))"
    )
    public_author_case = f"""
      CASE
        WHEN {customer_condition} THEN 'customer'
        WHEN {agent_condition} THEN 'agent'
        ELSE 'unknown'
      END
    """

    sql = f"""
WITH candidate_tickets AS (
  SELECT
    t.id,
    t.subject,
    t.status,
    t.store_id,
    t.org_name,
    t.brand,
    t.issue_category,
    t.assignee_name,
    t.assignee_group_name,
    t.created_at,
    t.updated_at,
    t.sla_policy_name,
    t.resolution_sla_target_hours,
    t.resolution_sla_elapsed_hours,
    t.resolution_sla_breached,
    t.resolution_sla_status,
    COALESCE(t.escalated, FALSE) AS escalated,
    COALESCE(t.vendor_dispatched, FALSE) AS vendor_dispatched,
    COALESCE(t.vendor_escalation, FALSE) AS vendor_escalation
  FROM {ticket_table} AS t
  WHERE {where_sql}
),
comment_rollup AS (
  SELECT
    c.ticket_id,
    COUNTIF({customer_condition}) AS public_customer_comments,
    COUNTIF({agent_condition}) AS public_agent_replies,
    MAX(IF({agent_condition}, c.created_at, NULL)) AS last_public_agent_reply_at,
    MAX(IF({customer_condition}, c.created_at, NULL)) AS last_public_customer_comment_at,
    ARRAY_AGG(
      IF(c.is_internal = FALSE, {public_author_case}, NULL)
      IGNORE NULLS ORDER BY c.created_at DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS last_public_author,
    ARRAY_AGG(
      IF(
        {customer_condition},
        CONCAT(
          FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC', c.created_at),
          ': ',
          SUBSTR(REGEXP_REPLACE(COALESCE(c.body, ''), r'\\s+', ' '), 1, 220)
        ),
        NULL
      )
      IGNORE NULLS ORDER BY c.created_at DESC LIMIT 2
    ) AS customer_evidence
  FROM {comments_table} AS c
  JOIN candidate_tickets AS t ON t.id = c.ticket_id
  GROUP BY c.ticket_id
),
followups AS (
  SELECT
    c.ticket_id,
    COUNTIF({customer_condition} AND (
      cr.last_public_agent_reply_at IS NULL OR c.created_at > cr.last_public_agent_reply_at
    )) AS customer_followups_after_last_agent
  FROM {comments_table} AS c
  JOIN comment_rollup AS cr ON cr.ticket_id = c.ticket_id
  GROUP BY c.ticket_id
)
SELECT
  t.*,
  COALESCE(cr.public_customer_comments, 0) AS public_customer_comments,
  COALESCE(cr.public_agent_replies, 0) AS public_agent_replies,
  COALESCE(f.customer_followups_after_last_agent, 0) AS customer_followups_after_last_agent,
  COALESCE(cr.last_public_author, 'none') AS last_public_author,
  cr.last_public_agent_reply_at,
  cr.last_public_customer_comment_at,
  cr.customer_evidence
FROM candidate_tickets AS t
LEFT JOIN comment_rollup AS cr ON cr.ticket_id = t.id
LEFT JOIN followups AS f ON f.ticket_id = t.id
ORDER BY t.updated_at DESC
LIMIT {row_limit}
""".strip()

    cfg = bq.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=10 * 1024 ** 3,
        use_query_cache=True,
    )
    bq_rows = list(client.query(sql, job_config=cfg).result())
    return [_score_row(row) for row in bq_rows]


def _build_ticket_response(ticket_id: int) -> dict[str, Any]:
    risk = ticket_customer_risk.get_customer_risk(int(ticket_id), refresh=False)
    risk_dict = _risk_response_dict(risk)
    metrics = risk.metrics
    reply_state = _agent_reply_after_customer_label(
        metrics.last_public_author,
        metrics.hours_since_last_public_agent_reply,
        metrics.hours_since_last_public_customer_comment,
    )
    lines = [
        f"Customer Risk for ticket #{risk.ticket_id}: **{_risk_label(risk.severity)}**.",
        f"Customer tone: **{risk.tone}**.",
        "",
        "Evidence from public end-user/customer comments and public agent replies:",
    ]
    for reason in risk.reasons[:5]:
        lines.append(f"- {_normalize_stale_reply_reason(reason)}")
    lines.extend([
        f"- Agent replied after the latest customer follow-up: {reply_state}",
        "",
        f"First action: **{risk.next_action}**",
    ])
    return {
        "answer": _with_trace_footers(
            "\n".join(lines),
            [{"id": int(ticket_id)}],
        ),
        "rows": [risk_dict],
        "ticket_cards": [{"id": int(ticket_id)}],
        "bq_summary": (
            "PER-TICKET CUSTOMER RISK SERVICE: used cached/fresh "
            f"Customer Risk assessment for ticket #{ticket_id}."
        ),
        "scope": "ticket",
    }


def _build_ticket_list_response(
    request: CustomerRiskChatRequest,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = rows[:request.limit]
    scope_label = f"store {request.store_id}" if request.store_id else "active tickets"
    status_filter = _requested_status_filter(request.question)
    filters: list[str] = [
        "status " + "/".join(sorted(status_filter)) if status_filter else "not solved/closed"
    ]
    if request.followup_min:
        if request.require_after_agent_followups:
            filters.append(f"{request.followup_min}+ customer follow-ups after our last public reply")
        else:
            filters.append(f"{request.followup_min}+ public customer comments/follow-ups")
    if request.minimum_score >= 70:
        filters.append("red/high Customer Risk")
    elif request.minimum_score >= 45:
        filters.append("orange-or-red/elevated Customer Risk")
    if request.since_days:
        filters.append(f"updated in the last {request.since_days} days")

    if not selected:
        answer = (
            f"I did not find {scope_label} matching these Customer Risk filters: "
            f"{'; '.join(filters)}.\n\n"
            "Customer Risk is computed from public customer follow-ups, public agent reply delay, "
            "last public commenter, unresolved status, escalation, and vendor-dispatch signals."
        )
        return {
            "answer": _with_trace_footers(answer, []),
            "rows": [],
            "ticket_cards": [],
            "bq_summary": _bq_summary(request, rows_checked=0),
            "scope": request.scope,
        }

    overall_tone = _overall_tone(selected)
    highest_risk = _risk_label(str(selected[0]["severity"]))
    lines = [
        f"Customer Risk review for {scope_label}: **{overall_tone}** overall tone, highest risk **{highest_risk}**.",
        f"Filters used: {', '.join(filters)}.",
        "",
        "| Ticket | Status | Customer Risk | Public customer comments | Follow-ups after agent | Agent after latest customer? | Evidence |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for row in selected:
        lines.append(
            "| "
            f"#{row['id']} | "
            f"{_table_cell(row.get('status'))} | "
            f"{_risk_label(str(row['severity']))} | "
            f"{row['public_customer_comments']} | "
            f"{row['customer_followups_after_last_agent']} | "
            f"{_agent_reply_after_customer_label(row['last_public_author'], row['hours_since_last_public_agent_reply'], row['hours_since_last_public_customer_comment'])} | "
            f"{_table_cell(_evidence_summary(row))} |"
        )
    lines.extend([
        "",
        f"First action: **{_next_action(selected[0])}**",
        "",
        "This is Customer Risk/frustration-risk, not a native stored Zendesk sentiment score.",
    ])
    return {
        "answer": _with_trace_footers("\n".join(lines), [_ticket_card(row) for row in selected]),
        "rows": [_compact_row(row) for row in selected],
        "ticket_cards": [_ticket_card(row) for row in selected],
        "bq_summary": _bq_summary(request, rows_checked=len(rows)),
        "scope": request.scope,
    }


def _build_store_ranking_response(
    request: CustomerRiskChatRequest,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("store_id") or "unknown")
        if key in {"", "unknown", "none", "null"}:
            continue
        group = grouped.setdefault(
            key,
            {
                "store_id": key,
                "org_name": row.get("org_name") or "",
                "tickets": [],
                "max_score": 0,
                "red_count": 0,
                "orange_count": 0,
                "followups": 0,
                "public_customer_comments": 0,
                "stale_public_reply_count": 0,
                "vendor_dispatch_count": 0,
                "escalated_count": 0,
            },
        )
        group["tickets"].append(row)
        group["max_score"] = max(int(group["max_score"]), int(row["risk_score"]))
        group["red_count"] += 1 if row["severity"] == "red" else 0
        group["orange_count"] += 1 if row["severity"] == "orange" else 0
        group["followups"] += int(row["customer_followups_after_last_agent"])
        group["public_customer_comments"] = int(group.get("public_customer_comments") or 0) + int(
            row["public_customer_comments"]
        )
        group["stale_public_reply_count"] += 1 if _stale_public_reply(row) else 0
        group["vendor_dispatch_count"] += 1 if row.get("vendor_dispatched") else 0
        group["escalated_count"] += 1 if row.get("escalated") else 0

    groups = list(grouped.values())
    if "multiple active tickets" in _normalize_text(request.question):
        groups = [group for group in groups if len(group["tickets"]) >= 2]
    groups.sort(
        key=lambda group: (
            int(group["red_count"]),
            int(group["orange_count"]),
            int(group["max_score"]),
            len(group["tickets"]),
        ),
        reverse=True,
    )
    selected = groups[:request.limit]

    if not selected:
        answer = (
            "I did not find stores matching the requested Customer Risk ranking filters.\n\n"
            "The ranking uses active tickets, public customer follow-ups, public reply delays, "
            "last public commenter, escalation, and vendor-dispatch signals."
        )
        return {
            "answer": _with_trace_footers(answer, []),
            "rows": [],
            "ticket_cards": [],
            "bq_summary": _bq_summary(request, rows_checked=len(rows)),
            "scope": "store_ranking",
        }

    period = f"last {request.since_days} days" if request.since_days else "active unresolved tickets"
    lines = [
        f"Stores ranked by elevated Customer Risk ({period}):",
        "",
        "| Store | Organization | Elevated tickets | Highest risk | Red / orange | Signals | Example tickets |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    ticket_cards: list[dict[str, Any]] = []
    trace_rows: list[dict[str, Any]] = []
    for group in selected:
        tickets = _sort_risk_rows(group["tickets"])[:3]
        examples = ", ".join(f"#{row['id']}" for row in tickets)
        signals = (
            f"{group['public_customer_comments']} public customer comments; "
            f"{group['followups']} after-agent follow-ups; "
            f"{group['stale_public_reply_count']} stale public replies; "
            f"{group['escalated_count']} escalated; "
            f"{group['vendor_dispatch_count']} vendor dispatch"
        )
        lines.append(
            "| "
            f"{_table_cell(group['store_id'])} | "
            f"{_table_cell(group['org_name'])} | "
            f"{len(group['tickets'])} | "
            f"{_risk_label(str(tickets[0]['severity']))} | "
            f"{group['red_count']} / {group['orange_count']} | "
            f"{_table_cell(signals)} | "
            f"{examples} |"
        )
        for row in tickets:
            ticket_cards.append(_ticket_card(row))
        trace_rows.append({
            "store_id": group["store_id"],
            "org_name": group["org_name"],
            "elevated_ticket_count": len(group["tickets"]),
            "max_risk_score": group["max_score"],
            "red_count": group["red_count"],
            "orange_count": group["orange_count"],
            "example_ticket_ids": [row["id"] for row in tickets],
        })
    lines.extend([
        "",
        "This is a deterministic Customer Risk ranking, not a native stored Zendesk sentiment score.",
    ])
    return {
        "answer": _with_trace_footers("\n".join(lines), ticket_cards),
        "rows": trace_rows,
        "ticket_cards": ticket_cards,
        "bq_summary": _bq_summary(request, rows_checked=len(rows)),
        "scope": "store_ranking",
    }


def _score_row(row: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    created_at = _ensure_utc(getattr(row, "created_at", None))
    last_agent = _ensure_utc(getattr(row, "last_public_agent_reply_at", None))
    last_customer = _ensure_utc(getattr(row, "last_public_customer_comment_at", None))
    status = str(getattr(row, "status", "") or "").lower()
    is_active = status not in {"solved", "closed"}
    metrics = CustomerRiskMetrics(
        public_customer_comments=int(getattr(row, "public_customer_comments", 0) or 0),
        public_agent_replies=int(getattr(row, "public_agent_replies", 0) or 0),
        customer_followups_after_last_agent=int(getattr(row, "customer_followups_after_last_agent", 0) or 0),
        last_public_author=str(getattr(row, "last_public_author", "none") or "none"),
        hours_since_last_public_agent_reply=_hours_since(last_agent, now),
        hours_since_last_public_customer_comment=_hours_since(last_customer, now),
        active_ticket_age_hours=_hours_since(created_at, now) if created_at and is_active else None,
        is_active=is_active,
        is_escalated=bool(getattr(row, "escalated", False)),
        vendor_dispatched=bool(getattr(row, "vendor_dispatched", False)),
        vendor_escalation=bool(getattr(row, "vendor_escalation", False)),
        sla_policy_name=str(getattr(row, "sla_policy_name", "") or "") or None,
        resolution_sla_target_hours=_float_or_none(getattr(row, "resolution_sla_target_hours", None)),
        resolution_sla_elapsed_hours=_float_or_none(getattr(row, "resolution_sla_elapsed_hours", None)),
        resolution_sla_hours_remaining=_sla_hours_remaining_from_row(row),
        resolution_sla_progress_pct=_sla_progress_pct_from_row(row),
        resolution_sla_breached=getattr(row, "resolution_sla_breached", None),
        resolution_sla_status=str(getattr(row, "resolution_sla_status", "") or "") or None,
    )
    score, severity = ticket_customer_risk.score_customer_risk_metrics(metrics)
    evidence = [str(item) for item in (getattr(row, "customer_evidence", None) or []) if str(item or "").strip()]
    return {
        "id": int(getattr(row, "id")),
        "subject": str(getattr(row, "subject", "") or ""),
        "status": str(getattr(row, "status", "") or ""),
        "store_id": str(getattr(row, "store_id", "") or ""),
        "org_name": str(getattr(row, "org_name", "") or ""),
        "brand": str(getattr(row, "brand", "") or ""),
        "issue_category": str(getattr(row, "issue_category", "") or ""),
        "assignee_name": str(getattr(row, "assignee_name", "") or ""),
        "assignee_group_name": str(getattr(row, "assignee_group_name", "") or ""),
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": _datetime_iso(getattr(row, "updated_at", None)),
        "public_customer_comments": metrics.public_customer_comments,
        "public_agent_replies": metrics.public_agent_replies,
        "customer_followups_after_last_agent": metrics.customer_followups_after_last_agent,
        "last_public_author": metrics.last_public_author,
        "hours_since_last_public_agent_reply": metrics.hours_since_last_public_agent_reply,
        "hours_since_last_public_customer_comment": metrics.hours_since_last_public_customer_comment,
        "escalated": metrics.is_escalated,
        "vendor_dispatched": metrics.vendor_dispatched,
        "vendor_escalation": metrics.vendor_escalation,
        "sla_policy_name": metrics.sla_policy_name,
        "resolution_sla_target_hours": metrics.resolution_sla_target_hours,
        "resolution_sla_elapsed_hours": metrics.resolution_sla_elapsed_hours,
        "resolution_sla_hours_remaining": metrics.resolution_sla_hours_remaining,
        "resolution_sla_progress_pct": metrics.resolution_sla_progress_pct,
        "resolution_sla_breached": metrics.resolution_sla_breached,
        "resolution_sla_status": metrics.resolution_sla_status,
        "risk_score": score,
        "severity": severity,
        "tone": _tone_for(score, metrics),
        "reasons": ticket_customer_risk.customer_risk_reasons(metrics),
        "customer_evidence": evidence,
    }


def _filter_rows_for_request(rows: list[dict[str, Any]], request: CustomerRiskChatRequest) -> list[dict[str, Any]]:
    out = []
    status_filter = _requested_status_filter(request.question)
    for row in rows:
        if status_filter and str(row.get("status") or "").lower() not in status_filter:
            continue
        if request.followup_min:
            if request.require_after_agent_followups:
                if int(row["customer_followups_after_last_agent"]) < request.followup_min:
                    continue
            elif max(
                int(row["customer_followups_after_last_agent"]),
                int(row["public_customer_comments"]),
            ) < request.followup_min:
                continue
        if int(row["risk_score"]) < int(request.minimum_score):
            continue
        out.append(row)
    return out


def _sort_risk_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            int(row["risk_score"]),
            int(row["customer_followups_after_last_agent"]),
            int(row["public_customer_comments"]),
            float(row.get("hours_since_last_public_agent_reply") or 0),
        ),
        reverse=True,
    )


def _requested_status_filter(question: str) -> set[str] | None:
    text = _normalize_text(question)
    if re.search(r"\bopen\s+or\s+pending\b|\bopen\s*/\s*pending\b", text) and "hold" not in text:
        return {"open", "pending"}
    if re.search(r"\bopen\b", text) and re.search(r"\bpending\b", text) and re.search(r"\b(?:hold|on[-\s]?hold)\b", text):
        return {"open", "pending", "hold"}
    return None


def _looks_like_customer_risk_text(text: str) -> bool:
    if re.search(r"\bcustomer\s+risk\b|\bcustomer[-\s]?frustration\b|\bfrustration[-\s]?risk\b", text):
        return True
    if re.search(r"\b(sentiment|tone|frustrat(?:ed|ion|ing)?)\b", text):
        return True
    if re.search(r"\b(?:customer|store)\b.{0,60}\b(?:follow(?:ed)?[-\s]?ups?|followed\s+up)\b", text):
        return True
    if re.search(r"\b(?:last\s+public\s+commenter|last\s+public\s+comment\s+came\s+from\s+the\s+customer|customer\s+was\s+the\s+last)\b", text):
        return True
    if "public reply" in text and "customer" in text and "risk" in text:
        return True
    return False


def _asks_store_ranking(text: str) -> bool:
    return bool(
        re.search(r"\brank\s+stores?\b", text)
        or re.search(r"\bstores?\b.{0,80}\b(?:negative\s+sentiment|customer\s+risk|frustration[-\s]?risk|elevated\s+customer\s+risk)\b", text)
        or "multiple active tickets" in text
    )


def _extract_followup_min(text: str) -> int | None:
    patterns = [
        r"\b(\d{1,2})\s*\+\s*(?:customer\s+)?(?:public\s+)?follow[-\s]?ups?\b",
        r"\b(\d{1,2})\s+or\s+more\s+(?:customer\s+)?(?:public\s+)?follow[-\s]?ups?\b",
        r"\b(?:customer|store)\b.{0,40}\b(\d{1,2})\s*(?:public\s+)?follow[-\s]?ups?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, min(int(match.group(1)), 20))
    if "repeated" in text and "follow" in text:
        return 2
    if "multiple" in text and "follow" in text:
        return 2
    return None


def _requires_after_agent_followups(text: str) -> bool:
    return bool(
        re.search(r"\bafter\s+(?:our|the)\s+last\s+public\s+repl(?:y|ies)\b", text)
        or re.search(r"\bafter\s+(?:our|the)\s+last\s+public\s+agent\s+repl(?:y|ies)\b", text)
        or re.search(r"\bfollow(?:ed)?[-\s]?ups?\b.{0,50}\bafter\s+(?:our|the)\s+last\s+public\b", text)
    )


def _extract_since_days(text: str) -> int | None:
    match = re.search(r"\b(?:last|past|previous)\s+(\d{1,4})\s+days?\b", text)
    if match:
        return max(1, min(int(match.group(1)), 3650))
    if re.search(r"\blast\s+month\b|\bpast\s+month\b", text):
        return 30
    return None


def _minimum_score_for_text(text: str) -> int:
    if re.search(r"\bhigh\b.{0,40}\b(?:customer\s+risk|risk|frustration)\b", text):
        return 70
    if re.search(r"\b(?:elevated|negative)\b.{0,40}\b(?:customer\s+risk|risk|sentiment)\b", text):
        return 45
    return 20


def _extract_limit(text: str, *, default: int) -> int:
    match = re.search(r"\b(?:top|first|show|list)\s+(\d{1,2})\b", text)
    if match:
        return max(1, min(int(match.group(1)), 25))
    return default


def _overall_tone(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no matching Customer Risk signal"
    if rows[0]["severity"] == "red":
        return "escalated/frustrated"
    if rows[0]["severity"] == "orange":
        return "frustrated"
    if rows[0]["severity"] == "yellow":
        return "concerned"
    return "calm"


def _tone_for(score: int, metrics: CustomerRiskMetrics) -> str:
    if score >= 70 and metrics.customer_followups_after_last_agent >= 4:
        return "escalated"
    if (
        score >= 45
        and (
            metrics.customer_followups_after_last_agent >= 1
            or metrics.public_customer_comments >= 3
            or metrics.last_public_author == "customer"
        )
    ):
        return "frustrated"
    if score >= 20:
        return "concerned"
    return "calm"


def _risk_label(severity: str) -> str:
    labels = {
        "green": "Green - Normal customer risk",
        "yellow": "Yellow - Watch customer risk",
        "orange": "Orange - Elevated customer risk",
        "red": "Red - High customer frustration risk",
    }
    return labels.get(str(severity or "").lower(), "Customer risk")


def _evidence_summary(row: dict[str, Any]) -> str:
    reasons = [str(item) for item in row.get("reasons", []) if str(item or "").strip()]
    if reasons:
        return "; ".join(_normalize_stale_reply_reason(reason) for reason in reasons[:2])
    evidence = row.get("customer_evidence") or []
    if evidence:
        return str(evidence[0])[:180]
    return "No repeated customer follow-up or stale public-reply signal"


def _normalize_stale_reply_reason(reason: str) -> str:
    text = str(reason or "").strip()
    match = re.fullmatch(r"No public agent reply in (\d+) hours", text)
    if match:
        return f"Last public agent reply was {match.group(1)} hours ago"
    return text


def _next_action(row: dict[str, Any]) -> str:
    last_author = str(row.get("last_public_author") or "")
    hours = row.get("hours_since_last_public_agent_reply")
    severity = str(row.get("severity") or "")
    sla_breached = row.get("resolution_sla_status") == "open_breached" or row.get("resolution_sla_breached") is True
    sla_near_breach = (
        row.get("resolution_sla_status") == "open_within_target"
        and row.get("resolution_sla_hours_remaining") is not None
        and float(row.get("resolution_sla_hours_remaining") or 0) <= 6
    )
    if sla_breached and last_author == "customer" and (hours is None or float(hours) >= 24):
        return "Acknowledge the SLA breach with a public update or call the store before continuing internal-only work."
    if sla_breached:
        return "Review the blocker and post a clear public update with current status and the next resolution step."
    if sla_near_breach and last_author == "customer":
        return "Post a public update before the SLA target is reached and confirm the next resolution step."
    if severity == "red" and last_author == "customer" and (hours is None or float(hours) >= 24):
        return "Post a public update or call the store before continuing internal-only work."
    if int(row.get("customer_followups_after_last_agent") or 0) >= 3:
        return "Acknowledge the repeated follow-ups publicly and give the store a concrete next step or ETA."
    if hours is None or float(hours or 0) >= 24:
        return "Post a clear public status update with the current owner and next step."
    return "Keep monitoring and make the next public update explicit."


def _agent_reply_after_customer_label(
    last_public_author: str,
    hours_since_agent: float | None,
    hours_since_customer: float | None,
) -> str:
    if hours_since_customer is None:
        return "n/a"
    if hours_since_agent is None:
        return "No public agent reply"
    if last_public_author == "agent":
        return "Yes"
    return "No"


def _stale_public_reply(row: dict[str, Any]) -> bool:
    hours = row.get("hours_since_last_public_agent_reply")
    return hours is None or float(hours or 0) >= 24


def _ticket_card(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "subject": row.get("subject") or "",
        "org_name": row.get("org_name") or "",
        "brand": row.get("brand") or "",
        "status": row.get("status") or "",
        "store_id": row.get("store_id") or "",
        "issue_category": row.get("issue_category") or "",
        "resolution": "",
        "created_at": row.get("created_at"),
        "solved_at": None,
    }


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = [
        "id",
        "subject",
        "status",
        "store_id",
        "org_name",
        "risk_score",
        "severity",
        "tone",
        "public_customer_comments",
        "customer_followups_after_last_agent",
        "last_public_author",
        "hours_since_last_public_agent_reply",
        "escalated",
        "vendor_dispatched",
        "vendor_escalation",
        "sla_policy_name",
        "resolution_sla_target_hours",
        "resolution_sla_elapsed_hours",
        "resolution_sla_hours_remaining",
        "resolution_sla_progress_pct",
        "resolution_sla_breached",
        "resolution_sla_status",
        "reasons",
    ]
    return {key: row.get(key) for key in keep}


def _risk_response_dict(risk: CustomerRiskResponse) -> dict[str, Any]:
    payload = risk.model_dump()
    payload["generated_at"] = risk.generated_at.isoformat()
    return payload


def _bq_summary(request: CustomerRiskChatRequest, *, rows_checked: int) -> str:
    return (
        "DETERMINISTIC CUSTOMER RISK ANALYTICS: "
        f"scope={request.scope}, store_id={request.store_id}, since_days={request.since_days}, "
        f"followup_min={request.followup_min}, require_after_agent_followups={request.require_after_agent_followups}, "
        f"minimum_score={request.minimum_score}, rows_checked={rows_checked}. "
        "Signals use ticket_comments.is_internal = FALSE, customer/end-user public comments, public agent replies, "
        "last public author, active status, escalated, vendor_dispatched, vendor_escalation, and derived resolution SLA urgency."
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sla_hours_remaining_from_row(row: Any) -> float | None:
    target = _float_or_none(getattr(row, "resolution_sla_target_hours", None))
    elapsed = _float_or_none(getattr(row, "resolution_sla_elapsed_hours", None))
    if target is None or elapsed is None:
        return None
    return round(target - elapsed, 2)


def _sla_progress_pct_from_row(row: Any) -> float | None:
    target = _float_or_none(getattr(row, "resolution_sla_target_hours", None))
    elapsed = _float_or_none(getattr(row, "resolution_sla_elapsed_hours", None))
    if target is None or elapsed is None or target <= 0:
        return None
    return round(max(0.0, elapsed / target * 100.0), 1)


def _with_trace_footers(answer: str, ticket_cards: list[dict[str, Any]]) -> str:
    return (
        answer.strip()
        + "\n\n"
        + f"TICKETS_JSON: {json.dumps(ticket_cards[:20])}\n"
        + 'SOURCES_JSON: {"sources": []}'
    )


def _table_cell(value: object) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "\\|").strip()
    return text or "-"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _ensure_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_iso(value: Any) -> str | None:
    dt = _ensure_utc(value)
    return dt.isoformat() if dt else None


def _hours_since(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (now - value).total_seconds() / 3600.0), 2)
