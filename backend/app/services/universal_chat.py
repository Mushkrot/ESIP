"""
Universal Chat service.

A single chat mode that replaces the need to choose between Ticket mode and Analytics mode.
Uses JSON mode Phase 1 (deterministic routing) instead of AUTO function-calling.

Key differences from the removed legacy chat modes:
- Phase 1: JSON mode → explicit tool selection, not AUTO function-calling
- Phase 1.5: parallel asyncio.gather() for all selected tools
- search_drive_exact: supports intent="summarize" (all chunks for a document)
- Ticket context: injected only when deterministic context policy says the current turn needs it
- Analytics queries (NL→SQL) handled natively

Endpoint: POST /api/universal/stream
"""

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from app.services.genai_transport import (
    GenerationConfig,
    BoundModel,
    ToolConfig,
)

from app.config import settings
from app.services.bigquery import _slug_to_title
from app.services.kb_retrieval_context import format_kb_status_for_prompt
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
from app.services import model_transition_request_capture

# Prefix used by the frontend to mark translate requests (must match UniversalChat.tsx)
TRANSLATE_PREFIX = "__translate__:"

logger = logging.getLogger(__name__)

_model = BoundModel(settings.gemini_model, surface="universal_chat")

# ---------------------------------------------------------------------------
# Phase 1: JSON mode tool decision
# ---------------------------------------------------------------------------

_PHASE1_PROMPT = """\
You are a tool-routing expert for the Northstar Support Operations AI assistant.
The engineer is asking a question. Your job is to decide which data sources to query.
Return a JSON object only — no explanation.

TODAY'S DATE: {today} (Eastern Time (ET))

{ticket_section}
AVAILABLE DATA SOURCES:
1. search_knowledge_base — Vertex AI semantic search over KB articles + Drive docs
   Use for: how-to guides, troubleshooting procedures, device configuration, "what is X", "how do I..."
   Returns: article snippets (short, ~80 chars each). Good for procedure lookup, NOT for full summaries.

2. search_drive_exact — BigQuery substring search over Google Drive documentation chunks
   Use for:
     - Finding which document contains a specific phrase/keyword ("find doc about X", "does our doc mention X")
     - Summarizing a specific document by name ("summarize the Voice Gateway guide", "explain the network modem doc")
   Supports intent="summarize": fetches ALL chunks of a named document for complete summarization.

3. search_similar_tickets — BQ cosine similarity / Vertex AI vector search for past tickets
   Use for: "has this happened before", "find similar issues", "how was X resolved in the past"

4. run_bq_query — Natural language to SQL on ticket BigQuery database
   Use for: counts, trends, stats, "how many tickets", "which brand has most X", "last month's issues",
             any temporal/aggregate question.
   Do NOT use for a pure troubleshooting/procedure question unless the user also asks for volume,
   frequency, trends, repeat issues, top stores/issues, or examples from past tickets.

TOOL SELECTION RULES:
- "how do I" / "what is the procedure" / "step by step" / "guide for X" → search_knowledge_base
- "summarize X document" / "explain the X guide" / "what does the X doc cover" → search_drive_exact with intent=summarize
- "find exact phrase X" / "does our doc say X" / "find document containing X" → search_drive_exact with intent=search
- For spreadsheet/table questions, drive_query must be short and literal (2-6 key words from row/section text),
  not a full sentence. Example: "who do I escalate DispatchFlow issues to?" -> drive_query: "DispatchFlow Issues".
- "similar tickets" / "has this happened before" / "how was X fixed" → search_similar_tickets
- "how many" / "count" / "trend" / "last month" / "this week" / "which brand" → run_bq_query
- Pure troubleshooting/procedure questions like "Cannot close pay period, process fails" or
  "I can't run EOD" → search_knowledge_base + search_similar_tickets, but do not add run_bq_query
  unless the user explicitly asks for counts/trends/repeat issues/examples.
- Ticket evidence-quality rules below only shape SQL after run_bq_query is already justified by
  an explicit analytics/examples ask. They must not cause run_bq_query for pure troubleshooting or
  procedure questions.
- Detail/explain follow-ups after an aggregate answer (for example "explain in detail", "tell me top five",
  "analyze these issues", "what are the common reasons") → run_bq_query again with sample ticket evidence,
  not just the same aggregate count and not a raw ticket list only.
- Hybrid (e.g. "how to fix X AND how many times has it happened") → multiple tools
- General knowledge (no specific data needed) → no tools (empty tools array)

CONVERSATION HISTORY:
{history}

ENGINEER'S LATEST QUESTION: {question}

Return JSON (no markdown, no backticks):
{{
  "tools": ["search_knowledge_base", "search_drive_exact", "search_similar_tickets", "run_bq_query"],
  "kb_query": "query string for search_knowledge_base (only if included)",
  "drive_query": "query for search_drive_exact (only if included; for spreadsheets use 2-6 literal keywords)",
  "drive_intent": "search" | "summarize",
  "drive_file_name": "exact file name if known for summarize intent (optional)",
  "tickets_query": "query for search_similar_tickets (only if included)",
  "bq_sql": "SELECT ... (valid BigQuery Standard SQL, only if run_bq_query included)",
  "reasoning": "one sentence explaining why these tools were selected"
}}

tools must be a subset (or empty list) of the four available tool names.
If run_bq_query is included, bq_sql MUST be a valid SELECT query.

BIGQUERY SQL RULES:
- TIMESTAMP intervals: use DAY only. 1 month=30 DAY, 3 months=90 DAY, 1 quarter=90 DAY, 1 year=365 DAY.
  "This quarter" = TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), QUARTER) is valid; or use INTERVAL 90 DAY.
- Always use fully-qualified table names: `esip-reference-project.esip_reference.tickets`
- POS Type is an organization/store profile field, not a ticket field. For questions like
  "stores where POS Type = Simphony", use deterministic POS Type profile analytics over
  `esip-reference-project.esip_reference.zendesk_organization_profiles` joined to
  tickets by `tickets.org_id = zendesk_organization_profiles.org_id`. Never use `tickets.pos_type`.
- The tickets table includes `store_id`. Never claim store_id is unavailable.
- If the question mentions a specific store (e.g. "store 2011"), include a store_id filter unless
  the user explicitly asks to compare multiple stores.
- `store_id` is a STRING. For store-id ranges/prefixes, compare strings with quoted literals:
  `store_id BETWEEN '2000' AND '2999'`, `store_id >= '2000'`, `STARTS_WITH(store_id, '20')`.
  Do not use unquoted numeric store_id range values.
- For assigned group / assignment queue / vendor queue questions (for example Field Services Partner), use
  assignee_group_name. Do not use org_name for assigned groups; org_name is the store organization.
  Prefer case-insensitive equality such as `LOWER(assignee_group_name) = 'field_services_partner'`.
- Status values are new/open/pending/hold/solved/closed. If the user says "on-hold", use 'hold'.
- If the user asks for "current" or "active" tickets without explicitly naming statuses, use
  `status IN ('open', 'pending', 'hold')`. Do not include solved/closed, and do not include
  `new` unless the user explicitly asks for new tickets/status.
- For public/internal comment analytics, use `ticket_comments.is_internal`.
  Public comments/replies are `is_internal = FALSE`; internal notes are `is_internal = TRUE`.
  Never use `is_public` because that column does not exist.
- For public agent replies, filter `is_internal = FALSE AND author_is_agent = TRUE`.
  For customer/end-user public comments, filter `is_internal = FALSE AND author_role = 'end-user'`.
  These author-role columns may be NULL for historical rows until the comment-author backfill has run.
- For questions like "not closed tickets with no public update/reply/comment in the last N hours",
  compute `MAX(ticket_comments.created_at)` where `is_internal = FALSE` per ticket, LEFT JOIN it
  to `tickets`, filter unresolved tickets, and keep tickets where that public timestamp is NULL
  or older than the requested window.
- brand column = tenant brands only (Metro Burger, Cedar Grill, Frontier Kitchen, etc). NEVER use brand for vendors.
- For device vendors (network, Cisco, Zebra, Oracle, Meraki) use LOWER(full_text) LIKE '%nokia%'.
- For issue type searches ("network outage", "POS issue", "printer jam") — NEVER use exact equality on issue_category.
  Use: LOWER(issue_category) LIKE '%network%' OR LOWER(full_text) LIKE '%network%outage%'
  The issue_category values use hyphens (e.g. "network-issue", "pos-issue") — always use LIKE with %.
- For "top issues by store" requests, don't rely only on issue_category.
  If issue_category may be NULL, derive issue_topic with fallback to subject:
    CASE
      WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN issue_category
      ELSE CONCAT('__subject__', TRIM(subject))
    END AS issue_topic
  Then GROUP BY issue_topic and ORDER BY ticket count DESC.
- For detail/explain follow-ups after aggregate results, include evidence columns that let the answer explain
  causes and examples. Keep the grouped aggregate COUNT and add sample ticket details in the same grouped query;
  do not switch to an ungrouped raw ticket list only. Example:
  STRING_AGG(FORMAT('#%d | %s | %s', id, subject, COALESCE(resolution, '')), '\\n'
             ORDER BY created_at DESC LIMIT 3) AS sample_tickets.
- When filtering by issue type AND counting by brand, always include a NULL-safe brand alias:
  COALESCE(brand, 'unknown') AS brand
- For trend / repeat-pattern / "is this increasing" follow-ups, do not generate a single aggregate only.
  Include an Eastern business-date time bucket plus the relevant store/brand/issue dimensions, for example:
  DATE_TRUNC(DATE(created_at, 'America/New_York'), WEEK) AS week_start,
  store_id, issue_category, COUNT(*) AS ticket_count.
- When run_bq_query is already selected for an explicit example-ticket / analytics question,
  especially "was this a training issue / user error / avoidable" or "show examples", do not use
  tickets whose only evidence is a very short generic resolution. Include
  LENGTH(COALESCE(full_text, '')) AS context_chars when returning examples, prefer rows with
  context_chars >= 300, and state clearly when the only matching examples are thin or low-context.
"""

_BQ_SCHEMA_HINT = """
Tables in `esip-reference-project.esip_reference`:
- tickets(id, subject, status, org_name, org_id, brand, store_id, issue_category, symptoms, resolution,
          priority, ticket_type, ticket_form_id, ticket_form_name,
          created_at, solved_at, updated_at, via_channel, assignee_name,
          assignee_id, assignee_email, assignee_group_id, assignee_group_name,
          resolution_hours, sla_policy_name, resolution_sla_target_hours,
          resolution_sla_elapsed_hours, resolution_sla_breached, resolution_sla_status,
          escalated, vendor_dispatched, vendor_escalation,
          comment_count, public_comment_count, internal_comment_count,
          full_text)
- ticket_comments(ticket_id, body, author_id, author_name, author_email, author_role,
                  author_is_agent, via_channel, created_at, is_internal)
- zendesk_organization_profiles(org_id, org_name, store_id, brand, status, pos_type,
          simphony_store, simphony_install_date, pos_server_ip, kds, dmb, guest_wifi,
          phone_type, internet_connection, tags, source_updated_at, synced_at,
          raw_fields_json, raw_profile_json)
  POS Type lives here, not in tickets. Join to tickets by org_id first; any store_id fallback
  must be explicitly labeled as a fallback.
  `is_internal = FALSE` means public comment/reply. `is_internal = TRUE` means internal note.
  There is no `is_public` column.
brand values: "Cedar Grill", "Metro Burger", "Frontier Kitchen", "Lakeside Tavern",
              "Harbor Chicken", "Summit Burger", "Morning Brew", "Piazza Kitchen",
              "City Fries", "Foundry Burger", "Fresh Market", "Garden Table"
SLA fields: for Store Incident tickets, `resolution_sla_target_hours` is derived from
Zendesk priority (urgent=8h, high=12h, normal/medium=24h, low=48h). Use
`resolution_sla_elapsed_hours` and `resolution_sla_breached` / `resolution_sla_status` for
SLA performance. Do not invent raw fields named `sla` or `average_sla_minutes`.
Escalation/vendor fields: use `escalated`, `vendor_dispatched`, and `vendor_escalation`.
Comment volume fields: use `comment_count`, `public_comment_count`, and `internal_comment_count`.
"""


def _try_capture_request(
    capture_callback: Callable[[dict[str, Any]], None] | None,
    **kwargs: Any,
) -> None:
    """Best-effort capture must never affect model transport or the visible answer."""
    if capture_callback is None:
        return
    try:
        capture_callback(model_transition_request_capture.build_envelope(**kwargs))
    except Exception as exc:
        logger.warning("model transition request capture skipped: %s", exc)


def decide_tools(
    messages: list[dict],
    ticket_context: str = "",
    capture_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """
    Phase 1: JSON mode call to decide which tools to run.

    Returns a dict with keys:
      tools: list[str]
      kb_query, drive_query, drive_intent, drive_file_name,
      tickets_query, bq_sql, reasoning
    """
    try:
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %d, %Y")

        recent = messages[-6:] if len(messages) > 6 else messages
        history_lines = []
        for m in recent[:-1]:
            role = "User" if m["role"] == "user" else "Assistant"
            body = m.get("content", "")[:300]
            history_lines.append(f"{role}: {body}")
        history_block = "\n".join(history_lines) if history_lines else "(first message)"
        question = messages[-1]["content"] if messages else ""

        ticket_section = (
            f"OPEN TICKET CONTEXT (engineer has this ticket open in the left panel):\n"
            f"{ticket_context}\n\n"
            if ticket_context else ""
        )
        prompt = _PHASE1_PROMPT.format(
            today=today,
            ticket_section=ticket_section,
            history=history_block,
            question=question,
        ) + f"\n\nBQ SCHEMA REFERENCE:\n{_BQ_SCHEMA_HINT}"

        estimated_tokens = estimate_tokens_from_text(prompt)
        estimated_cost = calc_cost(estimated_tokens, 200, call_type=CallType.UNIVERSAL_P1)
        reserved = get_gate_reserve(CallType.UNIVERSAL_P1, estimated_cost)
        corr = new_correlation_id()
        check_cost_gate(CallType.UNIVERSAL_P1, reserved, fail_closed=False)
        record_estimate(
            CallType.UNIVERSAL_P1,
            estimated_cost,
            corr,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=200,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
        )

        generation_config_data = {
            "temperature": 0.0,
            "max_output_tokens": 1024,
            "response_mime_type": "application/json",
            "thinking_config": {"thinking_budget": 0},
        }
        _try_capture_request(
            capture_callback,
            surface="universal_chat.phase1_planner",
            model=settings.gemini_model,
            input_kind="text",
            input_value=prompt,
            generation_config=generation_config_data,
            tool_config=None,
            stream=False,
        )
        response = _model.generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict(generation_config_data),
        )

        usage = getattr(response, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual_cost = record_usage(
                CallType.UNIVERSAL_P1, t_in, t_out, t_think,
                correlation_id=corr,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )
            check_divergence(estimated_cost, actual_cost, CallType.UNIVERSAL_P1,
                             t_think, thinking_budget_was_zero=True)

        data = json.loads(response.text)
        tools = data.get("tools") or []
        drive_query = data.get("drive_query")
        if "search_drive_exact" in tools and not drive_query:
            # Keep Drive retrieval robust even when Phase 1 forgets to emit drive_query.
            drive_query = question
        logger.info("Universal Phase 1: tools=%s reasoning=%s", tools, data.get("reasoning", ""))
        return {
            "tools":          tools,
            "kb_query":       data.get("kb_query"),
            "drive_query":    drive_query,
            "drive_intent":   data.get("drive_intent", "search"),
            "drive_file_name": data.get("drive_file_name"),
            "tickets_query":  data.get("tickets_query"),
            "bq_sql":         data.get("bq_sql"),
            "reasoning":      data.get("reasoning", ""),
        }
    except Exception as exc:
        logger.warning("universal decide_tools failed — no tools: %s", exc)
        return {"tools": [], "reasoning": f"routing error: {exc}"}


# ---------------------------------------------------------------------------
# Format tool results for Phase 2 prompt
# ---------------------------------------------------------------------------

def _fmt_kb(articles: list) -> str:
    if not articles:
        return ""
    lines = [f"\nKNOWLEDGE BASE ({len(articles)} articles):"]
    for a in articles:
        lines.append(f"  [ARTICLE: {a.title}]\n    {a.snippet[:300]}\n    URL: {a.url}")
    return "\n".join(lines)


def _fmt_drive(chunks: list, intent: str) -> str:
    if not chunks:
        return ""
    file_name = chunks[0].get("file_name", "document") if chunks else ""
    if intent == "summarize":
        # Concatenate all chunks in order — full document for summarization
        full_text = "\n\n".join(c["chunk_text"] for c in chunks)
        url = chunks[0].get("drive_url", "") if chunks else ""
        return (
            f"\nSD DOCUMENTATION — Full document: {file_name}\n"
            f"URL: {url}\n\n"
            f"{full_text}"
        )
    # search intent — show matching chunks
    lines = [f"\nSD DOCUMENTATION — Matching chunks ({len(chunks)} found):"]
    for i, c in enumerate(chunks, 1):
        location_parts = []
        if c.get("sheet_name"):
            location_parts.append(f"sheet={c['sheet_name']}")
        if c.get("row_number"):
            location_parts.append(f"row={c['row_number']}")
        if c.get("table_title"):
            location_parts.append(f"table={c['table_title']}")
        if c.get("section_title"):
            location_parts.append(f"section={c['section_title']}")
        location = f"\n    Location: {'; '.join(location_parts)}" if location_parts else ""
        lines.append(
            f"  [DOC-{i}] {c['file_name']}{location}\n"
            f"    {c['chunk_text'][:500]}\n"
            f"    URL: {c['drive_url']}"
        )
    return "\n".join(lines)


def _fmt_similar(tickets: list, query: str) -> str:
    if not tickets:
        return f"\nSIMILAR PAST TICKETS for '{query}': none found."
    lines = [f"\nSIMILAR RESOLVED TICKETS ('{query}', {len(tickets)} results):"]
    for t in tickets:
        res = f"\n    Resolution: {t['resolution'][:200]}" if t.get("resolution") else ""
        ic = (t.get("issue_category") or "").strip()
        ic_disp = _slug_to_title(ic) if ic else ""
        created = t.get("created_at") or "unknown"
        solved = t.get("solved_at") or "unknown"
        store_id = str(t.get("store_id") or "").strip() or "unknown"
        lines.append(
            f"  #{t['ticket_id']} | created_at={created} | solved_at={solved} "
            f"| store_id={store_id} | {t.get('brand','')} {t.get('org_name','')}"
            f" | {ic_disp}\n    {t['subject'][:100]}{res}"
        )
    return "\n".join(lines)


def _is_exact_drive_find_question(question: str) -> bool:
    q = question.lower()
    return (
        "document" in q
        and (
            "contains" in q
            or "containing" in q
            or "exact phrase" in q
            or "find document" in q
        )
    )


def _drive_sources_footer(chunks: list) -> str:
    sources = []
    seen: set[tuple[str, str]] = set()
    for c in chunks[:5]:
        title = str(c.get("file_name") or "Drive document")
        url = str(c.get("drive_url") or "")
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        sources.append({
            "title": title,
            "url": url,
            "snippet": str(c.get("chunk_text") or "")[:240],
        })
    return json.dumps({"sources": sources}, ensure_ascii=False)


def _looks_like_slug_value(value: str) -> bool:
    return (
        value == value.lower()
        and " " not in value
        and ("_" in value or "/" in value)
        and len(value) < 120
    )


def _fmt_bq_cell(header: str, value) -> str:
    header_l = (header or "").lower()
    is_issue_field = header_l in {
        "issue_category",
        "issue_topic",
        "category",
        "issue_bucket",
        "issue_type",
        "topic",
    }
    if value is None:
        return "Uncategorized" if is_issue_field else "NULL"

    text = str(value)
    if is_issue_field and text.strip().lower() in {"", "none", "null"}:
        return "Uncategorized"
    if is_issue_field and text.startswith("__subject__"):
        subject = text[len("__subject__"):].strip()
        return f"Inferred from subject: {subject}" if subject else "Uncategorized"
    if is_issue_field and _looks_like_slug_value(text):
        return _slug_to_title(text)
    if _looks_like_slug_value(text) and ("__" in text or header_l.endswith("_category")):
        return _slug_to_title(text)
    if is_issue_field:
        return text.replace(" > ", " --> ")
    return text


def _is_issue_count_result(headers: list[str]) -> bool:
    normalized = {(header or "").lower() for header in headers}
    has_issue_field = bool(
        normalized
        & {
            "issue_category",
            "issue_topic",
            "category",
            "issue_bucket",
            "issue_type",
            "topic",
        }
    )
    has_count_field = bool(
        normalized
        & {
            "ticket_count",
            "tickets",
            "count",
            "cnt",
            "total_tickets",
            "issue_count",
        }
    )
    return has_issue_field and has_count_field


def _fmt_bq(rows: list | str, sql: str) -> str:
    is_store_content_analysis = sql.startswith("FALLBACK CONTENT ANALYSIS:")
    is_same_store_history = sql.startswith("DETERMINISTIC SAME-STORE HISTORY FALLBACK:")
    is_store_context_snapshot = sql.startswith("DETERMINISTIC STORE CONTEXT SNAPSHOT:")
    if isinstance(rows, str):
        return (
            f"\nANALYTICS QUERY ERROR (SQL: {sql}):\n{rows}\n"
            "The analytics query failed and returned no valid rows. Do not infer that matching "
            "tickets, same-store history, or trends do not exist. State that this specific "
            "automated query failed if no other ticket data is provided."
        )
    if not rows:
        return f"\nANALYTICS QUERY: No results returned.\nSQL executed: {sql}\nNote: the query ran successfully but matched zero rows. Suggest the user try broader search terms or a different time range."
    # Format up to 50 rows as a text table
    if not rows:
        return "\nANALYTICS QUERY: No results."
    headers = list(rows[0].keys()) if rows else []
    if is_store_content_analysis:
        lines = [
            f"\nSTORE-SCOPED TICKET CONTENT ANALYSIS ({len(rows)} rows):",
            sql,
            "This result is already filtered to the requested store and lookback window. "
            "Answer from these rows directly; do not say the data is unfiltered or all-store.",
        ]
    elif is_same_store_history:
        lines = [
            f"\nSAME-STORE TICKET HISTORY ({len(rows)} rows):",
            sql,
            "This result is already filtered to the store from the open or explicit ticket. "
            "Use these rows as same-store history. Do not say there are no other "
            "same-store records when rows are listed here.",
        ]
    elif is_store_context_snapshot:
        lines = [
            f"\nSAME-STORE CONTEXT SNAPSHOT ({len(rows)} rows):",
            sql,
            "This is a keyword-free snapshot of the current store's ticket history. "
            "Use it for current or same-store claims, event timing, before/after reasoning, "
            "and source challenges. Rows use ticket_created_at for the ticket open date and "
            "ticket_solved_at only for the solved/closed date. Do not use global similar-ticket "
            "rows as same-store evidence. If a prior assistant answer conflicts with these "
            "rows, these rows win.",
        ]
    else:
        lines = [
            f"\nANALYTICS QUERY ({len(rows)} rows):",
            "This query returned rows. Do not say there is no data, no results, or that the "
            "automated search did not return specific data. If the rows are limited, explain "
            "that limitation while using the returned rows.",
        ]
    lines.append(" | ".join(headers))
    lines.append("-" * 60)
    for row in rows[:50]:
        lines.append(" | ".join(_fmt_bq_cell(h, row.get(h)) for h in headers))
    if len(rows) > 50:
        lines.append(f"... and {len(rows) - 50} more rows")
    if _is_issue_count_result(headers):
        lines.append(
            "\nPRESENTATION NOTE: This is a grouped issue-category count result. "
            "Answer with the issue categories already grouped and show the ticket count next "
            "to each category. If Uncategorized appears, mention it as a categorization gap, "
            "then list the categorized issues separately. Rows labeled Inferred from subject "
            "are not formal categories; mention them after categorized issues only when useful. "
            "For a simple top-issues/count question, do not cite sample tickets or emit ticket "
            "links unless the user explicitly asks for examples, details, or specific tickets. "
            "Use bullets for 4+ categories; keep shorter lists compact."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt for Phase 2
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an AI assistant for Northstar Support Operations.
You help engineers find past ticket resolutions, look up procedures, and understand operational trends.

ROLE: You are a "second brain" for IT engineers — you work across the complete support-ticket \
history and knowledge base. Your job is to surface the right information \
instantly so engineers don't have to search manually.

This serves both experienced engineers (instant recall) and new engineers learning the systems.

LANGUAGE RULE — MANDATORY: Respond in the same language as the user's most recent message.

PROPER NOUNS RULE: Never translate or transliterate names (people, companies, products, brands, \
technical terms, error codes, store IDs, IPs, URLs). Always keep in original English form.

RESPONSE STYLE:
- Lead with the direct answer or finding
- Be concise and actionable — engineers are technical
- Write in flowing prose paragraphs. Use a bullet list ONLY when listing 4+ distinct items that are genuinely parallel (e.g. a step-by-step procedure). Do NOT use bullets for short 2-3 item answers — just write them as a sentence or two.
- Use **bold** sparingly: only for key terms or resolution names that stand out naturally. Do NOT bold entire bullet points or list headers.
- For procedure questions, give clear steps and cite sources by article title (e.g. "According to the *BCM Troubleshooting Guide*..."), not by KB-N numbers
- For analytics/trend questions, state the key finding first, then supporting detail
- Write for a human engineer, not for internal systems. Do not expose raw internal slugs, tagger
  values, or machine labels such as `applications__order_portal__reset_requested` in the visible answer.
  Use readable labels like "Applications --> Order Portal --> Reset Requested". If a category is `None`,
  `NULL`, or `Uncategorized`, treat it as a categorization gap and mention it separately rather
  than presenting "None" as the top actual issue.
- If DATA RETRIEVED contains `STORE-SCOPED TICKET CONTENT ANALYSIS`, treat it as the authoritative
  store-filtered result. Do not describe it as generic, unfiltered, all-store, or unavailable.
- If DATA RETRIEVED contains `SAME-STORE TICKET HISTORY`, treat it as authoritative same-store
  history for the explicit/open ticket. Use the listed rows and do not claim that no same-store
  records were found.
- If DATA RETRIEVED contains `SAME-STORE CONTEXT SNAPSHOT`, treat it as the authoritative current
  store history snapshot. It is keyword-free; infer events and issue themes from the rows, but
  do not use any ticket from outside this snapshot as proof for "this store".
- Event/date claims must be grounded. If you say when an incident, outage, repair, or before/after
  period happened, cite the specific same-store ticket row(s) that support that timing. If the rows
  only prove when related tickets were created, phrase it as "retrieved event-related tickets begin
  on..." rather than asserting the exact real-world event date.
- Do not convert email sent dates, ticket created_at dates, ticket solved_at dates, restoration dates,
  or dispatch dates into the occurrence date of the underlying incident. Those are evidence dates,
  not necessarily event dates. If the user challenges a date, distinguish "the event date is stated"
  from "the retrieved evidence only shows related activity/messages/tickets on these dates" and
  correct any prior overclaim.
- When citing ticket dates, "ticket from DATE" means ticket created_at. Do not use solved_at,
  closed_at, or resolution dates as the ticket creation date. If only solved_at/closed_at is visible,
  label it explicitly as solved/closed date.
- When comparing with "other stores", never label a ticket as other-store evidence unless
  its store_id/org_name differs from the current store. If global similar-ticket results contain
  current-store rows, keep them under current/same-store evidence or label them as duplicate
  same-store examples.
- If DATA RETRIEVED contains `ANALYTICS QUERY ERROR`, the query failed. Do not interpret that as
  zero matches, no trend, or no same-store history.
- `SIMILAR RESOLVED TICKETS` is a global semantic search unless explicitly labeled store-scoped.
  Do not use it alone to conclude that a specific store has no prior history.
- Cite past tickets as evidence only when they add useful context: a concrete symptom, action,
  cause, troubleshooting step, or resolution detail. Do not cite tickets whose only useful text is a
  short generic label such as "training issue", "user error", "resolved", or "x"; say the available
  examples are weak instead of padding the answer with low-context tickets.
- Conversation history is context, not evidence. If previous assistant text conflicts with DATA
  RETRIEVED, ignore the previous assistant text and answer from DATA RETRIEVED.

KNOWLEDGE RETRIEVAL POLICY — MANDATORY:
- You never ask the user to "check the knowledge base", "read internal docs", or "look in the documentation" on their own. The system already runs retrieval; your job is to use **DATA RETRIEVED** below and answer from it.
- If **RETRIEVAL STATUS** says zero articles, say clearly that the *automated* search returned no articles for that query. Do not pretend you are about to run a second search, do not output "Tool Call:" or fake tool invocations, and do not deflect the user to manual research.
- If zero results but the topic is likely documented, you may note briefly that a gap *might* be query phrasing, ranking, or indexing (without blaming the user).
- If the user asks which spreadsheet row or sheet an answer came from, cite only the explicit `Location:` metadata
  from SD DOCUMENTATION. If no row metadata is present, say that row-level provenance was not returned.

SOURCES FORMAT — append at end of every response:
SOURCES_JSON: {"sources": [{"title": "...", "url": "...", "snippet": "..."}]}
If no sources: SOURCES_JSON: {"sources": []}

TICKETS FORMAT — if you reference past tickets:
TICKETS_JSON: [{"id": 950003, "subject": "...", "org_name": "...", "brand": "...", \
"status": "solved", "store_id": "2002", "issue_category": "...", "resolution": "...", \
"created_at": "2025-11-01", "solved_at": "2025-11-15"}]
Include ALL referenced tickets. Format ticket references in text as markdown links: [#950003](https://support.example.com/agent/tickets/950003)
When citing KB articles or Drive documents inline, format them as markdown links using the URL from the context: [Article Title](url)
If no tickets: omit TICKETS_JSON entirely."""


def build_phase2_request(
    *,
    messages: list[dict],
    ticket_context: Optional[str] = None,
    kb_results: Optional[list] = None,
    kb_status: Optional[dict] = None,
    drive_chunks: Optional[list] = None,
    drive_intent: str = "search",
    similar_tickets: Optional[list] = None,
    similar_query: str = "",
    bq_rows: Optional[list | str] = None,
    bq_sql: str = "",
) -> dict[str, Any]:
    """Build the exact Phase 2 prompt inputs without calling Gemini or any service."""
    context_parts: list[str] = []
    if ticket_context:
        context_parts.append(f"\nOPEN TICKET (engineer currently has this ticket open):\n{ticket_context}")
    kb_status_text = format_kb_status_for_prompt(kb_status)
    if kb_status_text:
        context_parts.append(kb_status_text)
    if kb_results is not None:
        kb_text = _fmt_kb(kb_results)
        if kb_text:
            context_parts.append(kb_text)
    if drive_chunks is not None:
        drive_text = _fmt_drive(drive_chunks, drive_intent)
        if drive_text:
            context_parts.append(drive_text)
    if similar_tickets is not None:
        context_parts.append(_fmt_similar(similar_tickets, similar_query))
    if bq_rows is not None:
        context_parts.append(_fmt_bq(bq_rows, bq_sql))

    system_block = SYSTEM_PROMPT
    if context_parts:
        system_block += "\n\nDATA RETRIEVED FOR THIS QUESTION:\n" + "\n".join(context_parts)
    contents = [
        {"role": "user", "parts": [{"text": system_block}]},
        {"role": "model", "parts": [{"text": "Understood. I'm ready to help the engineer."}]},
    ]
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    return {"system_block": system_block, "contents": contents}


def chat_stream(
    messages: list[dict],
    ticket_context: Optional[str] = None,
    kb_results: Optional[list] = None,
    kb_status: Optional[dict] = None,
    drive_chunks: Optional[list] = None,
    drive_intent: str = "search",
    similar_tickets: Optional[list] = None,
    similar_query: str = "",
    bq_rows: Optional[list | str] = None,
    bq_sql: str = "",
    capture_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[str]:
    """Streaming Phase 2 response with gathered context."""
    latest_question = messages[-1].get("content", "") if messages else ""
    if drive_chunks and drive_intent == "search" and _is_exact_drive_find_question(latest_question):
        lines = ["The automated Drive search found matching documentation:"]
        top = []
        seen_docs: set[tuple[str, str]] = set()
        for c in drive_chunks:
            title = str(c.get("file_name") or "Drive document")
            url = str(c.get("drive_url") or "")
            key = (title, url)
            if key in seen_docs:
                continue
            seen_docs.add(key)
            top.append(c)
            if url:
                lines.append(f"- [{title}]({url})")
            else:
                lines.append(f"- {title}")
            if len(top) >= 5:
                break
        lines.append("")
        lines.append(f"SOURCES_JSON: {_drive_sources_footer(top)}")
        yield "\n".join(lines)
        return

    request = build_phase2_request(
        messages=messages,
        ticket_context=ticket_context,
        kb_results=kb_results,
        kb_status=kb_status,
        drive_chunks=drive_chunks,
        drive_intent=drive_intent,
        similar_tickets=similar_tickets,
        similar_query=similar_query,
        bq_rows=bq_rows,
        bq_sql=bq_sql,
    )
    system_block = request["system_block"]
    contents = request["contents"]

    # CIS Phase 0 — mirror gemini.chat_stream (CHAT_PHASE2): pre-flight gate, then record actuals.
    prompt_for_estimate = system_block + "".join(
        str(m.get("content", "")) for m in messages
    )
    estimated_tokens = estimate_tokens_from_text(prompt_for_estimate)
    estimated_cost = calc_cost(
        estimated_tokens,
        estimated_tokens // 3,
        tokens_thinking=1024,
        call_type=CallType.UNIVERSAL_P2,
    )
    reserved = get_gate_reserve(CallType.UNIVERSAL_P2, estimated_cost)
    corr_p2 = new_correlation_id()
    check_cost_gate(CallType.UNIVERSAL_P2, reserved, fail_closed=True)
    record_estimate(
        CallType.UNIVERSAL_P2,
        estimated_cost,
        corr_p2,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=estimated_tokens // 3,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    generation_config_data = {
        "temperature": 0.2,
        "max_output_tokens": 8192,
        "thinking_config": {"thinking_budget": 1024},
    }
    _try_capture_request(
        capture_callback,
        surface="universal_chat.phase2_final_answer",
        model=settings.gemini_model,
        input_kind="contents",
        input_value=contents,
        generation_config=generation_config_data,
        tool_config={"function_calling_config": {"mode": "NONE"}},
        stream=True,
    )
    responses = _model.generate_content(
        contents,
        generation_config=GenerationConfig.from_dict(generation_config_data),
        tool_config=ToolConfig(
            function_calling_config=ToolConfig.FunctionCallingConfig(
                mode=ToolConfig.FunctionCallingConfig.Mode.NONE,
            )
        ),
        stream=True,
    )
    last_chunk = None
    for chunk in responses:
        last_chunk = chunk
        try:
            if chunk.text:
                yield chunk.text
        except Exception:
            pass

    if last_chunk is not None:
        usage = getattr(last_chunk, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            record_usage(
                CallType.UNIVERSAL_P2, t_in, t_out, t_think,
                correlation_id=corr_p2,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )


# ---------------------------------------------------------------------------
# Translation — cheap path: no Phase 1, thinking_budget=0
# ---------------------------------------------------------------------------

_TRANSLATE_SYSTEM = (
    "You are a professional translator. "
    "Translate the following text accurately. "
    "Keep all proper nouns, technical terms, error codes, ticket numbers, "
    "URLs, brand names, and store IDs in their original English form. "
    "Preserve all markdown formatting (bold, bullets, headers, links). "
    "Return only the translated text — no commentary, no preamble."
)


def translate_stream(text_to_translate: str, language: str) -> Iterator[str]:
    """
    Cheap translation path: single Gemini call, thinking_budget=0, no tool routing.
    Called when the frontend sends a __translate__:<lang> message.
    """
    prompt = f"Translate to {language}:\n\n{text_to_translate}"
    estimated_tokens = estimate_tokens_from_text(_TRANSLATE_SYSTEM + prompt)
    estimated_cost = calc_cost(estimated_tokens, estimated_tokens // 2, call_type=CallType.UNIVERSAL_TRANSLATE)
    reserved = get_gate_reserve(CallType.UNIVERSAL_TRANSLATE, estimated_cost)
    corr_tr = new_correlation_id()
    check_cost_gate(CallType.UNIVERSAL_TRANSLATE, reserved, fail_closed=False)
    record_estimate(
        CallType.UNIVERSAL_TRANSLATE,
        estimated_cost,
        corr_tr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=estimated_tokens // 2,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    contents = [
        {"role": "user", "parts": [{"text": _TRANSLATE_SYSTEM}]},
        {"role": "model", "parts": [{"text": "Understood. I will translate accurately, preserving formatting and technical terms."}]},
        {"role": "user", "parts": [{"text": prompt}]},
    ]

    responses = _model.generate_content(
        contents,
        generation_config=GenerationConfig.from_dict({
            "temperature": 0.1,
            "max_output_tokens": 4096,
            "thinking_config": {"thinking_budget": 0},
        }),
        tool_config=ToolConfig(
            function_calling_config=ToolConfig.FunctionCallingConfig(
                mode=ToolConfig.FunctionCallingConfig.Mode.NONE,
            )
        ),
        stream=True,
    )

    t_in = t_out = t_think = 0
    for chunk in responses:
        try:
            if chunk.text:
                yield chunk.text
            usage = getattr(chunk, "usage_metadata", None)
            if usage:
                t_in = getattr(usage, "prompt_token_count", 0) or 0
                t_out = getattr(usage, "candidates_token_count", 0) or 0
                t_think = getattr(usage, "thoughts_token_count", 0) or 0
        except Exception:
            pass

    if t_in or t_out:
        actual_cost = record_usage(
            CallType.UNIVERSAL_TRANSLATE, t_in, t_out, t_think,
            correlation_id=corr_tr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        check_divergence(estimated_cost, actual_cost, CallType.UNIVERSAL_TRANSLATE,
                         t_think, thinking_budget_was_zero=True)
