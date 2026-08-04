"""
Knowledge Gap Analysis service.

Identifies documentation gaps by cross-referencing high-volume ticket categories
with existing KB article coverage, then generates actionable recommendations via Gemini.

Two-phase pattern (same as other chat endpoints):
  Phase 1 — fetch_top_categories(): BQ query, returns structured data
  Phase 2 — gap_stream(): Gemini streaming report + chat
"""

import logging
from collections.abc import Iterator
from typing import Optional

import vertexai
from google.oauth2 import service_account
from vertexai.generative_models import GenerationConfig, GenerativeModel, ToolConfig

from app.config import settings
from app.services.cost_tracker import (
    CallType,
    calc_cost,
    check_cost_gate,
    check_divergence,
    estimate_tokens_from_text,
    get_gate_reserve,
    new_correlation_id,
    record_estimate,
    record_usage,
)

logger = logging.getLogger(__name__)

try:
    _creds = service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    vertexai.init(
        project=settings.google_cloud_project_id,
        location=settings.gemini_location,
        credentials=_creds,
    )
except Exception:
    vertexai.init(
        project=settings.google_cloud_project_id,
        location=settings.gemini_location,
    )

_model = GenerativeModel(settings.gemini_model)

# ---------------------------------------------------------------------------
# BQ query — top categories
# ---------------------------------------------------------------------------

def fetch_top_categories(days: int, min_tickets: int, sort_by: str) -> list[dict]:
    """
    Query BigQuery for top ticket categories in the given period.

    Returns list of dicts with keys:
      issue_category, ticket_count, avg_resolution_hours, avg_open_days,
      pct_escalated, sample_subjects (list of up to 3 subjects)
    """
    from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET, _slug_to_title
    from google.cloud import bigquery as bq

    sort_col = {
        "volume": "ticket_count",
        "resolution_time": "avg_resolution_hours",
        "open_time": "avg_open_days",
    }.get(sort_by, "ticket_count")

    sql = f"""
    WITH base AS (
      SELECT
        COALESCE(issue_category, 'uncategorized') AS issue_category,
        COUNT(*) AS ticket_count,
        ROUND(AVG(resolution_hours), 1) AS avg_resolution_hours,
        ROUND(AVG(TIMESTAMP_DIFF(
          COALESCE(solved_at, CURRENT_TIMESTAMP()),
          created_at,
          HOUR
        ) / 24.0), 1) AS avg_open_days,
        ROUND(COUNTIF(escalated IS TRUE) * 100.0 / COUNT(*), 1) AS pct_escalated
      FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
      WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
        AND issue_category IS NOT NULL
        AND issue_category != ''
        AND issue_category != 'uncategorized'
      GROUP BY issue_category
      HAVING ticket_count >= @min_tickets
      ORDER BY {sort_col} DESC
      LIMIT 15
    ),
    samples AS (
      SELECT
        issue_category,
        ARRAY_AGG(subject ORDER BY created_at DESC LIMIT 3) AS sample_subjects
      FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
      WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
        AND issue_category IS NOT NULL
        AND issue_category != ''
        AND issue_category != 'uncategorized'
      GROUP BY issue_category
    )
    SELECT b.*, s.sample_subjects
    FROM base b
    LEFT JOIN samples s USING (issue_category)
    """

    client = _get_client()
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("days", "INT64", days),
            bq.ScalarQueryParameter("min_tickets", "INT64", min_tickets),
        ]
    )
    rows = list(client.query(sql, job_config=job_config).result())

    results = []
    for row in rows:
        results.append({
            "issue_category": row.issue_category,
            "display_name": _slug_to_title(row.issue_category),
            "ticket_count": row.ticket_count,
            "avg_resolution_hours": row.avg_resolution_hours,
            "avg_open_days": row.avg_open_days,
            "pct_escalated": row.pct_escalated,
            "sample_subjects": list(row.sample_subjects or []),
        })
    return results


# ---------------------------------------------------------------------------
# Format categories for prompt
# ---------------------------------------------------------------------------

def _fmt_categories(categories: list[dict], days: int, sort_by: str) -> str:
    sort_label = {"volume": "ticket count", "resolution_time": "avg resolution time", "open_time": "avg days open"}.get(sort_by, "ticket count")
    lines = [f"\nTOP TICKET CATEGORIES (last {days} days, sorted by {sort_label}, {len(categories)} categories):"]
    for i, c in enumerate(categories, 1):
        subjects = "; ".join(c["sample_subjects"][:2]) if c["sample_subjects"] else ""
        lines.append(
            f"\n  {i}. {c['display_name']} [{c['issue_category']}]"
            f"\n     Tickets: {c['ticket_count']} | Avg resolution: {c['avg_resolution_hours']}h"
            f" | Avg open: {c['avg_open_days']} days | Escalated: {c['pct_escalated']}%"
            + (f"\n     Examples: {subjects}" if subjects else "")
        )
    return "\n".join(lines)


def _fmt_kb_coverage(coverage: dict[str, list]) -> str:
    """Format KB search results per category for the prompt."""
    lines = ["\nKB COVERAGE PER CATEGORY:"]
    for category, articles in coverage.items():
        if articles:
            article_list = "; ".join(f'"{a.title}"' for a in articles[:3])
            lines.append(f"  {category}: {len(articles)} article(s) found — {article_list}")
        else:
            lines.append(f"  {category}: NO articles found")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a Knowledge Gap Analyst for Northstar Support Operations.

Your job is to analyze ticket data and KB article coverage to identify documentation gaps,
then produce actionable recommendations that will reduce ticket volume.

You will receive:
- TOP TICKET CATEGORIES: ranked by volume/resolution time, with metrics
- KB COVERAGE: for each category, which KB articles currently exist (or none)

ANALYSIS TASK:
For each category, assess documentation coverage and classify as:
- 🔴 NO COVERAGE — high volume, no relevant KB article → recommend creating new article
- 🟡 PARTIAL COVERAGE — article exists but tickets keep coming → article likely incomplete or outdated, review recommended
- 🟢 GOOD COVERAGE — well documented, low escalation rate

RECOMMENDATIONS must be specific and actionable:
- For NO COVERAGE: suggest article title, key topics to cover, audience (engineer-facing vs customer-facing)
- For PARTIAL COVERAGE: suggest what gaps to fill, what to update
- Prioritize by: ticket_count × avg_resolution_hours (time saved per improvement)

RESPONSE FORMAT:
1. Start with a 2-sentence executive summary of the overall documentation health
2. For each category: one section with metrics, coverage assessment, and specific recommendation
3. End with a prioritized action list (top 3-5 items)

Be specific. Reference actual ticket counts and resolution times.
A recommendation like "create an article about network issues" is not useful.
A recommendation like "create engineer-facing article: network modem factory reset procedure
(340 tickets/month, avg 4.2h resolution — a step-by-step reset guide could cut this in half)"
is useful.

SOURCES_JSON: {"sources": []} must be appended at the end (even if no KB sources cited).
"""

# ---------------------------------------------------------------------------
# Phase 2: streaming gap report + chat
# ---------------------------------------------------------------------------

def gap_stream(
    messages: list[dict],
    categories: Optional[list[dict]] = None,
    kb_coverage: Optional[dict] = None,
    days: int = 90,
    sort_by: str = "volume",
) -> Iterator[str]:
    """Streaming generator for gap analysis report and follow-up chat."""
    context_parts = []
    if categories:
        context_parts.append(_fmt_categories(categories, days, sort_by))
    if kb_coverage:
        context_parts.append(_fmt_kb_coverage(kb_coverage))

    context_block = "\n".join(context_parts)
    system_block = SYSTEM_PROMPT
    if context_block:
        system_block += f"\n\nDATA:\n{context_block}"

    contents = [
        {"role": "user", "parts": [{"text": system_block}]},
        {"role": "model", "parts": [{"text": "Understood. I'll analyze the ticket data and KB coverage to identify documentation gaps."}]},
    ]
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    full_prompt = system_block + "".join(m["content"] for m in messages)
    estimated_tokens = estimate_tokens_from_text(full_prompt)
    # Stream uses thinking_budget=1024 — include thinking in pre-estimate (CIS Phase 0).
    estimated_cost = calc_cost(
        estimated_tokens,
        estimated_tokens // 3,
        tokens_thinking=1024,
        call_type=CallType.DOCS_GAP,
    )
    reserved = get_gate_reserve(CallType.DOCS_GAP, estimated_cost)
    corr = new_correlation_id()
    check_cost_gate(CallType.DOCS_GAP, reserved, fail_closed=True)
    record_estimate(
        CallType.DOCS_GAP,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=estimated_tokens // 3,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    responses = _model.generate_content(
        contents,
        generation_config=GenerationConfig.from_dict({"temperature": 0.2, "max_output_tokens": 4096, "thinking_config": {"thinking_budget": 1024}}),
        tool_config=ToolConfig(
            function_calling_config=ToolConfig.FunctionCallingConfig(
                mode=ToolConfig.FunctionCallingConfig.Mode.NONE,
            )
        ),
        stream=True,
    )
    t_in = t_out = t_think = 0
    for chunk in responses:
        if chunk.text:
            yield chunk.text
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
    if t_in or t_out or t_think:
        actual_cost = record_usage(
            CallType.DOCS_GAP, t_in, t_out, t_think,
            correlation_id=corr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        check_divergence(
            estimated_cost,
            actual_cost,
            CallType.DOCS_GAP,
            t_think,
            thinking_budget_was_zero=False,
        )
