"""
Analytics chat endpoint.

POST /api/analytics/chat/stream
    — Streaming analytics chat powered by Gemini + BigQuery + Vertex AI KB search.

      Phase 1: Gemini decides which data sources to query (JSON mode):
               - BigQuery (ticket data) → generates SQL
               - Vertex AI KB search (articles) → generates search query
               - Both (cross-source questions)
               - Neither (general knowledge)

      Phase 2a: Execute SQL against BigQuery (if needed).
      Phase 2b: Search KB articles via Vertex AI (if needed).
      Phase 3:  Gemini streams a natural-language answer with all results injected as text.

SSE format:
  data: {"delta": "..."}
  data: {"done": true, "sql": "SELECT ...", "row_count": 42, "table": {...}, "kb_results": [...]}
  data: {"error": "message"}
"""

import asyncio
import json
import logging
from typing import Iterator, Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dependencies import CurrentUser
from app.config import settings
from app.services import reference_runtime
from app.services import firestore
from app.services import bigquery as bq_service
from app.services.bigquery import search_drive_exact, _fix_timestamp_intervals
from app.services.vertex_search import search_kb, KBArticleSource
from app.services.gemini import _model, GenerationConfig
from app.services.cost_tracker import (
    CallType,
    CostDivergenceError,
    CostGateError,
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
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AnalyticsMessage(BaseModel):
    role: str    # "user" | "assistant"
    content: str

class AnalyticsChatRequest(BaseModel):
    messages: list[AnalyticsMessage]
    timeframe_days: int = 365    # filter context for Gemini (not enforced in SQL)
    brand_filter: Optional[str] = None  # e.g. "Cedar Grill" — Gemini can use in SQL


# ---------------------------------------------------------------------------
# KB result formatting
# ---------------------------------------------------------------------------

def _format_kb_results(results: list) -> str:
    """Format Vertex AI KB search results as text for Gemini context."""
    if not results:
        return "KB SEARCH RESULT: No relevant articles found."

    lines = [
        f"=== KNOWLEDGE BASE ARTICLES ({len(results)} results) ===",
        "These are real Zendesk KB articles. Use their content to answer the question.",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[Article {i}] {r.title}")
        lines.append(f"URL: {r.url}")
        if r.snippet:
            lines.append(f"Excerpt: {r.snippet}")
    lines.append("\n=== END OF KB ARTICLES ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 1 prompt: data source decision via JSON mode
# ---------------------------------------------------------------------------

def _build_sql_decision_prompt(
    timeframe_days: int,
    brand_filter: Optional[str],
    messages: list[dict],
) -> str:
    """
    Builds a prompt for Phase 1: asks Gemini to decide which data sources are needed.
    Uses JSON mode to avoid MALFORMED_FUNCTION_CALL.

    Two data sources available:
      1. BigQuery (ticket data) — structured SQL queries
      2. Vertex AI KB (Zendesk articles + future Google Drive docs) — semantic search
    """
    filter_note = ""
    if brand_filter:
        filter_note = (
            f"\nACTIVE FILTER: The user selected brand = '{brand_filter}'. "
            f"Add `WHERE brand = '{brand_filter}'` unless the question explicitly asks about all brands."
        )
    timeframe_note = (
        f"\nDEFAULT TIMEFRAME: The user is looking at the last {timeframe_days} days. "
        f"Apply `created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {timeframe_days} DAY)` "
        f"unless the question specifies a different period."
    )

    # Recent conversation history for follow-up context
    history_lines: list[str] = []
    for m in messages[:-1]:
        role_label = "User" if m["role"] == "user" else "Assistant"
        body = m["content"][:300] + ("…" if len(m["content"]) > 300 else "")
        history_lines.append(f"{role_label}: {body}")
    history_block = "\n".join(history_lines) if history_lines else "(first message)"

    last_question = messages[-1]["content"] if messages else ""

    return f"""You are a data routing expert for the Northstar Support Operations analytics system.
{timeframe_note}{filter_note}

You have access to TWO data sources:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 1 — BigQuery ticket database (structured data)
Use for: ticket counts, trends, resolution times, brand stats, engineer performance,
         escalation rates, recurring issues, store rankings, channel analysis, root causes.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{bq_service.SCHEMA_DESCRIPTION}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 2 — Knowledge Base articles (semantic search)
Use for: how-to procedures, troubleshooting guides, device setup, configuration steps,
         best practices, policy documents, training materials.
The KB contains Zendesk help articles and Google Drive SD Documentation files.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCE 3 — Drive document exact search (substring match)
Use for: finding which document contains a specific exact phrase or text string.
Examples: "find document that contains X", "is there a document where it says X",
          "find document with the exact inclusion X", "which document mentions exactly X".
This does a literal substring search — use the exact phrase from the user's question.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CONVERSATION HISTORY:
{history_block}

USER'S LATEST QUESTION: {last_question}

Decide which data sources to query. You may use multiple sources if needed.

Rules:
- tickets/trends/counts/stats → use BigQuery (needs_query: true, provide sql)
- procedures/guides/how-to/articles → use KB search (needs_kb_search: true, provide kb_query)
- find document containing exact phrase → use Drive exact search (needs_drive_exact: true, provide drive_exact_query)
- general knowledge question needing none: set all to false

Always use fully-qualified table names: `esip-reference-project.esip_reference.tickets`
Always write Standard SQL. No DDL. SELECT only.
TIMESTAMP_SUB RULE — CRITICAL: TIMESTAMP_SUB does NOT support MONTH or YEAR intervals.
Always use DAY: 1 month = 30 DAY, 3 months = 90 DAY, 6 months = 180 DAY, 1 year = 365 DAY.
CORRECT: created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
WRONG:   created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 MONTH)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SQL SEARCH RULES — CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

THEMATIC / TOPICAL SEARCHES ("find all tickets about X"):
  Rule 1 — Search subject + issue_category FIRST, then full_text as fallback.
           subject and issue_category are the most reliable signal.
  Rule 2 — Use OR between synonyms and related terms, not AND.
           The user may use different words than what's in the ticket.
  Rule 3 — Pick the 1-2 most distinctive keywords. Never AND all user words together.
  Rule 4 — Use broad LIKE patterns (e.g. '%nokia%') not tight multi-condition chains.

  WRONG — too restrictive (misses most tickets):
    WHERE LOWER(full_text) LIKE '%nokia%'
      AND LOWER(full_text) LIKE '%modem%'
      AND LOWER(full_text) LIKE '%telecom_partner%'
      AND LOWER(full_text) LIKE '%installation%'

  CORRECT — broad OR search across best fields:
    WHERE (
      LOWER(subject)        LIKE '%nokia%'
      OR LOWER(issue_category) LIKE '%nokia%'
      OR LOWER(full_text)   LIKE '%nokia%'
    )

  CORRECT — two distinctive terms joined with AND only when truly both are needed:
    WHERE LOWER(subject) LIKE '%nokia%'
      AND LOWER(full_text) LIKE '%modem%'

FACTUAL / AGGREGATION QUERIES ("how many tickets by brand?", "avg resolution time"):
  Use exact WHERE filters and GROUP BY as usual. AND is fine here.
  Example: WHERE status IN ('solved','closed') AND resolution_hours IS NOT NULL
  For assigned group / assignment queue / vendor queue questions (for example Field Services Partner),
  filter on assignee_group_name. Do not use org_name for assigned groups; org_name is the
  store organization.
  Status values are new/open/pending/hold/solved/closed. If the user says "on-hold", use 'hold'.

NULL CATEGORY RULE — CRITICAL:
  When grouping or ranking by issue_category, symptoms, resolution, or any text field:
  - ALWAYS add WHERE issue_category IS NOT NULL (or the relevant field IS NOT NULL)
  - This prevents NULL from appearing as the "most common" category, which is misleading.
  - If the user specifically asks about uncategorized tickets, use WHERE issue_category IS NULL.
  - CORRECT: GROUP BY issue_category HAVING issue_category IS NOT NULL ... ORDER BY count DESC
  - WRONG:   GROUP BY issue_category ORDER BY count DESC  ← NULL will dominate results

FOLLOW-UP FILTERS (user references a value seen in previous results):
  Use LOWER() + LIKE for slug columns (caused_by, issue_category, product_type, etc.)
  Example: WHERE LOWER(caused_by) LIKE '%training%'
  NEVER use display names in WHERE clauses for slug columns.

SCREENSHOT / OCR SEARCHES (content visible only in ticket images):
  When the user asks about IP addresses, whitelist entries, error messages in screenshots,
  configuration values, Slack messages, or any text that might appear in images rather than
  ticket text fields — JOIN ticket_attachment_ocr to search the OCR-extracted image text.

  Example — find tickets where screenshots mention "whitelist":
    SELECT DISTINCT t.id, t.subject, t.created_at, t.status,
      LEFT(o.ocr_text, 300) AS screenshot_text
    FROM `esip-reference-project.esip_reference.tickets` t
    JOIN `esip-reference-project.esip_reference.ticket_attachment_ocr` o
      ON t.id = o.ticket_id
    WHERE o.processing_status = 'success'
      AND o.ocr_text IS NOT NULL
      AND LOWER(o.ocr_text) LIKE '%whitelist%'
    ORDER BY t.created_at DESC
    LIMIT 20

  You should ALSO search tickets directly (subject/full_text) with OR to catch all matches:
    WHERE (
      LOWER(t.subject)    LIKE '%whitelist%'
      OR LOWER(t.full_text) LIKE '%whitelist%'
    )
    UNION DISTINCT
    SELECT DISTINCT t.id, t.subject, t.created_at, t.status,
      LEFT(o.ocr_text, 300) AS screenshot_text
    FROM tickets JOIN ticket_attachment_ocr ...

  Or combine both in one query with a LEFT JOIN and OR across all text fields.

Respond ONLY with valid JSON in this format:
{{
  "needs_query": true or false,
  "sql": "SELECT ... (only if needs_query is true)",
  "needs_kb_search": true or false,
  "kb_query": "search phrase for KB articles (only if needs_kb_search is true)",
  "needs_drive_exact": true or false,
  "drive_exact_query": "exact phrase to search for in Drive documents (only if needs_drive_exact is true)",
  "description": "one-line description of what data you are retrieving"
}}"""


# ---------------------------------------------------------------------------
# Phase 2 prompt: streaming answer
# ---------------------------------------------------------------------------

def _build_answer_prompt(
    timeframe_days: int,
    brand_filter: Optional[str],
    messages: list[dict],
    query_result: Optional[dict],
    kb_results: Optional[list],
    sql_description: Optional[str],
    drive_exact_results: Optional[list] = None,
) -> list[dict]:
    """
    Builds the contents list for Phase 3 streaming answer.
    Injects BQ results and/or KB article results as text context.
    """
    filter_ctx = f" (filtered to brand: {brand_filter})" if brand_filter else ""
    system = (
        f"You are an AI analytics assistant for the Northstar Support Operations.\n"
        f"You help IT managers and engineers understand ticket trends, recurring issues, "
        f"brand performance, resolution times, operational metrics, and find KB procedures.\n\n"
        f"LANGUAGE RULE — MANDATORY: Respond in the same language as the user's latest message.\n"
        f"- User writes in English → respond in English.\n"
        f"- User writes in French → respond in French.\n"
        f"- Never default to any other language.\n\n"
        f"PROPER NOUNS RULE — NEVER translate or transliterate the following, regardless of response language:\n"
        f"- People's names (e.g. 'Alex Morgan', 'Casey Nguyen')\n"
        f"- Company and brand names (e.g. 'Northstar Hospitality Group', 'Frontier Kitchen')\n"
        f"- Product and application names (e.g. 'Orbit View', 'Zendesk', 'Micros', 'KDS')\n"
        f"- Technical terms, error codes, ticket fields, store/location IDs, IP addresses, URLs\n"
        f"- Any proper noun — always keep in its original English form.\n"
        f"- Exception: if the user explicitly asks you to translate a specific name or term, you may do so.\n\n"
        f"Context: analysing the last {timeframe_days} days{filter_ctx}.\n\n"
        f"DATA RULE — CRITICAL: The actual database query results and KB articles are injected below.\n"
        f"You MUST use ONLY those exact numbers and article content. Do NOT invent, estimate, or add any data.\n"
        f"Do NOT write SQL. Do NOT call any tools. Report the numbers exactly as they appear in the results.\n\n"
        f"CATEGORY NAME RULE — MANDATORY: All category values in the data are already formatted for display.\n"
        f"You MUST copy them VERBATIM as they appear in the data table. NEVER convert them to technical format.\n"
        f"- CORRECT: 'Infrastructure Issue --> Internet Connection --> Primary'\n"
        f"- WRONG:   'infrastructure_issue__internet_connection__primary'\n"
        f"- CORRECT: 'Applications --> Store Operations --> End Of Day'\n"
        f"- WRONG:   'applications__store_operations__end_of_day'\n"
        f"- CORRECT: 'End User - Inquiry'\n"
        f"- WRONG:   'end_user_-_inquiry_'\n"
        f"This applies to ALL fields: issue categories, root causes, product types, channels, statuses.\n\n"
        f"NULL VALUE RULE: If any field in the results is NULL or empty string:\n"
        f"- For category/type fields: describe as 'Not categorized' or 'No category assigned'\n"
        f"- Do NOT say 'NULL' to the user — it is meaningless to them\n"
        f"- If NULL appears as the top result in a ranking (e.g. most common category), note that\n"
        f"  these are tickets without a category assigned, and list the actual named categories below it.\n\n"
        f"DATE FORMAT RULE: All times are in Eastern Time (ET).\n"
        f"- Dates: 'March 17, 2026' (not '2026-03-17')\n"
        f"- Timestamps: 'March 17, 2026 at 10:13 ET' (not '2026-03-17 15:13:37+00:00' or 'UTC')\n"
        f"- Time differences: '18 hours', '2 days 3 hours' (not raw numbers)"
    )

    contents: list[dict] = [
        {"role": "user",  "parts": [{"text": system}]},
        {"role": "model", "parts": [{"text": "Understood. I'll analyze the provided data and answer directly."}]},
    ]

    # Inject conversation history (all but last user message)
    for m in messages[:-1]:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    # Build final user turn: question + all available results
    last_question = messages[-1]["content"] if messages else ""
    data_blocks: list[str] = []

    if query_result:
        result_text = bq_service.format_result_for_gemini(query_result)
        data_blocks.append(f"[Ticket database data — {sql_description}]\n{result_text}")

    if kb_results:
        kb_text = _format_kb_results(kb_results)
        data_blocks.append(f"[Knowledge Base articles]\n{kb_text}")

    if drive_exact_results:
        lines = ["[Drive document exact search results]"]
        for r in drive_exact_results:
            lines.append(f"File: {r['file_name']}\nURL: {r['drive_url']}\nExcerpt: {r['chunk_text'][:500]}")
            lines.append("---")
        data_blocks.append("\n".join(lines))

    if data_blocks:
        final_turn = last_question + "\n\n" + "\n\n".join(data_blocks)
    else:
        final_turn = last_question

    contents.append({"role": "user", "parts": [{"text": final_turn}]})
    return contents


# ---------------------------------------------------------------------------
# Phase 1: decide data sources (JSON mode — no function calling)
# ---------------------------------------------------------------------------

def _decide_sources(
    timeframe_days: int,
    brand_filter: Optional[str],
    messages: list[dict],
) -> dict:
    """
    Non-streaming Gemini call using JSON mode to decide data sources.

    Returns dict with keys:
      needs_query: bool
      sql: str | None
      needs_kb_search: bool
      kb_query: str | None
      description: str
    """
    prompt = _build_sql_decision_prompt(timeframe_days, brand_filter, messages)
    est_tokens = estimate_tokens_from_text(prompt)
    est_cost = calc_cost(est_tokens, est_tokens // 4, call_type=CallType.ANALYTICS_DECISION)
    reserved = get_gate_reserve(CallType.ANALYTICS_DECISION, est_cost)
    corr = new_correlation_id()
    check_cost_gate(CallType.ANALYTICS_DECISION, reserved, fail_closed=False)
    record_estimate(
        CallType.ANALYTICS_DECISION,
        est_cost,
        corr,
        est_basis="formula",
        est_input_tokens=est_tokens,
        est_output_tokens=est_tokens // 4,
        expected_cost_cad=est_cost,
        reserved_cost_cad=reserved,
    )
    try:
        response = _model.generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict({"temperature": 0.0, "max_output_tokens": 512, "response_mime_type": "application/json", "thinking_config": {"thinking_budget": 0}}),
        )
        usage = getattr(response, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual = record_usage(
                CallType.ANALYTICS_DECISION, t_in, t_out, t_think,
                correlation_id=corr,
                est_cost_usd=est_cost,
                expected_cost_cad=est_cost,
            )
            check_divergence(
                est_cost,
                actual,
                CallType.ANALYTICS_DECISION,
                t_think,
                thinking_budget_was_zero=True,
            )
        data = json.loads(response.text)
        return {
            "needs_query":       bool(data.get("needs_query")),
            "sql":               data.get("sql") if data.get("needs_query") else None,
            "needs_kb_search":   bool(data.get("needs_kb_search")),
            "kb_query":          data.get("kb_query") if data.get("needs_kb_search") else None,
            "needs_drive_exact": bool(data.get("needs_drive_exact")),
            "drive_exact_query": data.get("drive_exact_query") if data.get("needs_drive_exact") else None,
            "description":       data.get("description", ""),
        }
    except (CostGateError, CostDivergenceError):
        raise
    except Exception as exc:
        logger.warning("Analytics source decision failed: %s", exc)
        return {
            "needs_query": False,
            "sql": None,
            "needs_kb_search": False,
            "kb_query": None,
            "needs_drive_exact": False,
            "drive_exact_query": None,
            "description": "",
        }


# ---------------------------------------------------------------------------
# Phase 3: stream answer
# ---------------------------------------------------------------------------

def _stream_answer(
    timeframe_days: int,
    brand_filter: Optional[str],
    messages: list[dict],
    query_result: Optional[dict],
    kb_results: Optional[list],
    sql_description: Optional[str],
    drive_exact_results: Optional[list] = None,
) -> Iterator[str]:
    """Stream the final Gemini answer with all data sources injected as text context."""
    contents = _build_answer_prompt(
        timeframe_days, brand_filter, messages, query_result, kb_results, sql_description,
        drive_exact_results=drive_exact_results,
    )
    prompt_blob = "".join(
        p.get("text", "")
        for c in contents
        for p in c.get("parts", [])
    )
    est_tokens = estimate_tokens_from_text(prompt_blob)
    est_cost = calc_cost(
        est_tokens,
        est_tokens // 3,
        tokens_thinking=1024,
        call_type=CallType.ANALYTICS_ANSWER,
    )
    reserved = get_gate_reserve(CallType.ANALYTICS_ANSWER, est_cost)
    corr_ans = new_correlation_id()
    check_cost_gate(CallType.ANALYTICS_ANSWER, reserved, fail_closed=True)
    record_estimate(
        CallType.ANALYTICS_ANSWER,
        est_cost,
        corr_ans,
        est_basis="formula",
        est_input_tokens=est_tokens,
        est_output_tokens=est_tokens // 3,
        expected_cost_cad=est_cost,
        reserved_cost_cad=reserved,
    )

    responses = _model.generate_content(
        contents,
        generation_config=GenerationConfig.from_dict({"temperature": 0.2, "max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}}),
        stream=True,
    )
    last_chunk = None
    for chunk in responses:
        last_chunk = chunk
        try:
            if chunk.text:
                yield chunk.text
        except Exception:
            # Skip function-call parts (Gemini sometimes generates them despite no tools declared)
            pass

    if last_chunk is not None:
        usage = getattr(last_chunk, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual = record_usage(
                CallType.ANALYTICS_ANSWER, t_in, t_out, t_think,
                correlation_id=corr_ans,
                est_cost_usd=est_cost,
                expected_cost_cad=est_cost,
            )
            check_divergence(
                est_cost,
                actual,
                CallType.ANALYTICS_ANSWER,
                t_think,
                thinking_budget_was_zero=False,
            )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/chat/stream")
async def analytics_chat_stream(req: AnalyticsChatRequest, user: CurrentUser):
    """
    Streaming analytics chat with dual data sources.

    Phase 1: Gemini decides which sources to query (JSON mode — reliable, no function-calling quirks).
    Phase 2a: Execute SQL against BigQuery (if needed, with self-correction retry ×3).
    Phase 2b: Search Vertex AI KB articles (if needed).
    Phase 3: Gemini streams a natural-language answer with all results injected as text.
    """
    if settings.runtime_profile == "reference":
        question = next((message.content for message in reversed(req.messages) if message.role == "user"), "")
        answer, table, kb_results = reference_runtime.analytics_response(question, req.brand_filter)

        async def reference_events():
            yield f"data: {json.dumps({'delta': answer})}\n\n"
            yield f"data: {json.dumps({'done': True, 'sql': 'REFERENCE DATASET AGGREGATION', 'row_count': len(table['rows']), 'table': table, 'kb_results': kb_results})}\n\n"

        return StreamingResponse(
            reference_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    messages_dicts = [m.model_dump() for m in req.messages]
    loop = asyncio.get_event_loop()

    # Phase 1: decide which data sources to query
    decision = await loop.run_in_executor(
        None,
        lambda: _decide_sources(req.timeframe_days, req.brand_filter, messages_dicts),
    )
    logger.info("Analytics decision: needs_query=%s needs_kb=%s description=%s",
                decision["needs_query"], decision["needs_kb_search"], decision["description"])

    # Phase 2a: execute BQ SQL with self-correction retry (max 3 attempts)
    query_result = None
    sql_executed = None
    sql_description = decision["description"]

    ticket_analytics_enabled = firestore.is_source_enabled("tickets_bq")

    if decision["needs_query"] and decision.get("sql") and not ticket_analytics_enabled:
        sql_description = "Ticket analytics search is disabled in Admin Data Sources."
        query_result = {
            "columns": ["status"],
            "rows": [["Ticket analytics search is disabled in Admin Data Sources."]],
            "row_count": 1,
            "sql_executed": None,
        }
        logger.info("analytics bq query skipped: tickets_bq Search is disabled")
    elif decision["needs_query"] and decision.get("sql"):
        raw_sql = _fix_timestamp_intervals(decision["sql"])
        last_error: Optional[str] = None

        for attempt in range(3):
            try:
                query_result = await loop.run_in_executor(
                    None,
                    lambda sql=raw_sql: bq_service.execute_query(sql),
                )
                sql_executed = query_result.get("sql_executed")
                last_error = None
                break  # success
            except Exception as exc:
                last_error = str(exc)
                logger.warning("BQ query attempt %d failed: %s", attempt + 1, exc)
                if attempt < 2:
                    fix_messages = messages_dicts + [{
                        "role": "assistant",
                        "content": f"I tried this SQL but got an error: {last_error}\nSQL: {raw_sql}"
                    }, {
                        "role": "user",
                        "content": f"Please fix the SQL. The error was: {last_error}"
                    }]
                    fixed = await loop.run_in_executor(
                        None,
                        lambda: _decide_sources(req.timeframe_days, req.brand_filter, fix_messages),
                    )
                    if fixed.get("sql"):
                        raw_sql = fixed["sql"]
                        logger.info("Self-corrected SQL (attempt %d): %s", attempt + 2, raw_sql[:100])
                    else:
                        break

        if last_error:
            logger.error("BQ query failed after retries: %s", last_error)

    # Phase 2b: search KB articles (if needed)
    kb_results = None

    if decision["needs_kb_search"] and decision.get("kb_query"):
        try:
            kb_results = await loop.run_in_executor(
                None,
                lambda: search_kb(decision["kb_query"], page_size=5),
            )
            logger.info("KB search for '%s' returned %d results", decision["kb_query"], len(kb_results))
        except Exception as exc:
            logger.warning("KB search failed: %s", exc)
            kb_results = []

    # Phase 2c: exact Drive document search (if needed)
    drive_exact_results = None

    if decision.get("needs_drive_exact") and decision.get("drive_exact_query"):
        try:
            drive_exact_results = await loop.run_in_executor(
                None,
                lambda: search_drive_exact(decision["drive_exact_query"], page_size=5),
            )
            logger.info("Drive exact search for '%s' returned %d results",
                        decision["drive_exact_query"], len(drive_exact_results))
        except Exception as exc:
            logger.warning("Drive exact search failed: %s", exc)
            drive_exact_results = []

    # Phase 3: stream answer
    async def event_stream():
        full_text = ""
        try:
            chunks = await loop.run_in_executor(
                None,
                lambda: list(_stream_answer(
                    req.timeframe_days,
                    req.brand_filter,
                    messages_dicts,
                    query_result,
                    kb_results,
                    sql_description,
                    drive_exact_results=drive_exact_results,
                )),
            )
            for chunk in chunks:
                full_text += chunk
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Analytics streaming error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        done_payload: dict = {
            "done":      True,
            "sql":       sql_executed,
            "row_count": query_result["row_count"] if query_result else None,
        }

        # Include BQ table data for frontend rendering
        if query_result and query_result["row_count"] > 0:
            cols = query_result["columns"]
            done_payload["table"] = {
                "columns": cols,
                "rows": [
                    [bq_service._display_value(c, v) for c, v in zip(cols, row)]
                    for row in query_result["rows"][:100]
                ],
            }

        # Include KB article links for frontend rendering
        if kb_results:
            done_payload["kb_results"] = [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in kb_results
            ]

        yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )
