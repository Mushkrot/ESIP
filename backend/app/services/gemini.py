"""Ticket summaries, chat, and OCR through the shared AI transport."""

import json
import logging
import time
from datetime import datetime
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from app.services.genai_transport import (
    FunctionDeclaration,
    GenerationConfig,
    GenAITransportError,
    GenAITransportErrorKind,
    BoundModel,
    Part,
    Tool,
    ToolConfig,
)

from app.config import settings
from app.models.ai import KBArticleSource
from app.models.ticket import TicketListItem
from app.models.ticket import TicketDetail
from app.services.bigquery import _slug_to_title
from app.services.kb_retrieval_context import format_kb_status_for_prompt
from app.services.cost_tracker import (
    CallType,
    CostDivergenceError,
    CostGateError,
    OCR_MEASURED_EXPECTED_CAD,
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

# ---------------------------------------------------------------------------
# Language support
# ---------------------------------------------------------------------------

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "zh": "Chinese (Simplified)",
    "ru": "Russian",
    "hi": "Hindi",
}

_model = BoundModel(settings.gemini_model, surface="gemini")

# ---------------------------------------------------------------------------
# Prompt size limits
# ---------------------------------------------------------------------------
MAX_TICKET_TOKENS = 60_000
MAX_KB_TOKENS = 8_000
MAX_HISTORY_TOKENS = 20_000
MAX_SYSTEM_TOKENS = 3_000
MAX_CHAT_HISTORY_TOKENS = 15_000

# Rough chars-per-token estimate for truncation (Gemini Flash ~4 chars/token)
CHARS_PER_TOKEN = 4
MAX_TICKET_CHARS = MAX_TICKET_TOKENS * CHARS_PER_TOKEN   # ~240 000
FIRST_COMMENTS_KEEP = 5
LAST_COMMENTS_KEEP = 20
MIDDLE_COMMENT_PREVIEW = 150  # characters per middle comment in compact form


# ---------------------------------------------------------------------------
# Prompt assembly helpers
# ---------------------------------------------------------------------------

def _format_image(img: dict, indent: str = "") -> list[str]:
    """Format a single screenshot entry. Returns lines to append."""
    ocr = (img.get("ocr_text") or "").strip()
    if not ocr:
        return []
    fname = img.get("filename") or "screenshot"
    return [f"{indent}[Screenshot — {fname}]:", f"{indent}{ocr[:1500]}"]


def _format_ticket_for_prompt(
    ticket: TicketDetail,
    images_by_comment: dict | None = None,
) -> str:
    """
    Serialize ticket to a structured text block for Gemini.

    Screenshots are interleaved chronologically with comments:
    - Images WITH comment_id appear immediately after their parent comment.
    - Images WITHOUT comment_id (key=None) are inserted by created_at as standalone events.

    Applies tiered compression to comments if the ticket is very long.
    """
    images = images_by_comment or {}

    lines: list[str] = [
        f"TICKET ID: #{ticket.id}",
        f"SUBJECT: {ticket.subject}",
        f"STATUS: {ticket.status}  PRIORITY: {ticket.priority or 'none'}",
        f"CREATED: {ticket.created_at.date()}  UPDATED: {ticket.updated_at.date()}",
        f"ASSIGNEE: {ticket.assignee_name or 'Unassigned'}",
    ]
    if ticket.ticket_form_name or ticket.ticket_type:
        lines.append(
            "TICKET FORM/TYPE: "
            f"{ticket.ticket_form_name or 'unknown form'} / {ticket.ticket_type or 'unknown type'}"
        )
    if ticket.resolution_sla_target_hours is not None:
        elapsed = (
            f"{ticket.resolution_sla_elapsed_hours:.1f}h"
            if ticket.resolution_sla_elapsed_hours is not None
            else "unknown"
        )
        breached = (
            "Yes" if ticket.resolution_sla_breached is True
            else "No" if ticket.resolution_sla_breached is False
            else "Unknown"
        )
        lines.append(
            "RESOLUTION SLA: "
            f"policy={ticket.sla_policy_name or 'unknown'}, "
            f"target={ticket.resolution_sla_target_hours:g}h, "
            f"elapsed={elapsed}, status={ticket.resolution_sla_status}, breached={breached}"
        )

    if ticket.store:
        store = ticket.store
        lines.append(f"STORE: {store.org_name} (Store #{store.store_id}, Brand: {store.brand})")

    if ticket.is_escalated:
        lines.append("ESCALATED: Yes")

    if ticket.issue_category:
        lines.append(f"ISSUE CATEGORY: {_slug_to_title(ticket.issue_category)}")

    for label, value in [
        ("SYMPTOMS", ticket.symptoms),
        ("WORKAROUND", ticket.workaround),
        ("NEXT STEPS", ticket.next_steps),
        ("RESOLUTION", ticket.resolution),
    ]:
        if value:
            lines.append(f"{label}: {value}")

    comments = ticket.comments
    total = len(comments)
    lines.append(f"\nCOMMENTS ({total} total):")
    lines.append("-" * 40)

    # Standalone images (no comment_id) — inserted into the stream by created_at
    standalone = sorted(images.get(None, []), key=lambda x: x.get("created_at") or 0)

    # Build merged event list: (created_at, "comment"|"image", data)
    events: list[tuple] = [(c.created_at, "comment", c) for c in comments]
    for img in standalone:
        events.append((img.get("created_at") or comments[-1].created_at if comments else 0, "image", img))
    events.sort(key=lambda x: x[0])

    # Determine compression buckets based on comment count
    comment_list = [e for e in events if e[1] == "comment"]
    if total <= FIRST_COMMENTS_KEEP + LAST_COMMENTS_KEEP:
        compact_ids: set[int] = set()
    else:
        middle_comments = comment_list[FIRST_COMMENTS_KEEP: total - LAST_COMMENTS_KEEP]
        compact_ids = {e[2].id for e in middle_comments}
        lines.append(f"\n[NOTE: {len(compact_ids)} middle comments shown in compact form due to ticket length]")

    for _, etype, data in events:
        if etype == "comment":
            c = data
            if c.id in compact_ids:
                kind = "INT" if c.is_internal else "PUB"
                preview = c.body[:MIDDLE_COMMENT_PREVIEW].replace("\n", " ")
                lines.append(f"  [{kind}] {c.author_name} ({c.created_at.strftime('%m-%d %H:%M')}): {preview}...")
                # Compact comments: show truncated OCR only
                for img in images.get(c.id, []):
                    ocr = (img.get("ocr_text") or "").strip()
                    if ocr:
                        lines.append(f"  [Screenshot]: {ocr[:150]}...")
            else:
                kind = "[INTERNAL NOTE]" if c.is_internal else "[PUBLIC]"
                lines.append(f"\n{kind} {c.author_name} — {c.created_at.strftime('%Y-%m-%d %H:%M')}")
                lines.append(c.body)
                # Full OCR for verbatim comments
                for img in images.get(c.id, []):
                    lines.extend(_format_image(img))
        else:
            # Standalone image event
            lines.extend(_format_image(data))

    return "\n".join(lines)


def _format_kb_results(results: list[KBArticleSource]) -> str:
    if not results:
        return ""
    lines = ["\nRELEVANT KNOWLEDGE BASE ARTICLES:"]
    for r in results:
        lines.append(f"\n[ARTICLE: {r.title}]")
        lines.append(f"URL: {r.url}")
        if r.snippet:
            lines.append(f"Excerpt: {r.snippet}")
    return "\n".join(lines)


def _format_gdrive_results(results: list[KBArticleSource]) -> str:
    if not results:
        return ""
    import re as _re
    lines = ["\nRELEVANT SD DOCUMENTATION (Google Drive):"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[DOC-{i}] {r.title}")
        lines.append(f"URL: {r.url}")
        if r.snippet:
            # Normalize multiple whitespace to single space (pypdf double-space artifact)
            normalized = _re.sub(r"[ \t]{2,}", " ", r.snippet)
            lines.append(f"Excerpt: {normalized}")
    return "\n".join(lines)


def _format_history(tickets: list) -> str:
    if not tickets:
        return ""
    lines = ["\nRECENT STORE HISTORY (same store, solved tickets):"]
    for t in tickets:
        lines.append(
            f"  #{t.id} | {t.updated_at.date()} | {t.status} | {t.subject[:60]}"
            + (f" | Category: {_slug_to_title(t.issue_category)}" if t.issue_category else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compact ticket header for Phase 1 (function-calling) context
# ---------------------------------------------------------------------------

def _format_ticket_header(ticket: TicketDetail) -> str:
    """
    ~500-token compact summary of a ticket for use in the Phase 1 non-streaming call.
    Includes the last 3 comments so Gemini can formulate meaningful tool queries.
    """
    lines: list[str] = [
        f"TICKET #{ticket.id}: {ticket.subject}",
        f"STATUS: {ticket.status}  PRIORITY: {ticket.priority or 'none'}",
    ]
    if ticket.store:
        store = ticket.store
        lines.append(f"STORE: {store.org_name} (Store #{store.store_id}, Brand: {store.brand})")
    if ticket.issue_category:
        lines.append(f"ISSUE CATEGORY: {_slug_to_title(ticket.issue_category)}")

    if ticket.symptoms:
        lines.append(f"SYMPTOMS: {ticket.symptoms}")

    # Last 3 comments only
    recent = ticket.comments[-3:] if ticket.comments else []
    if recent:
        lines.append("RECENT COMMENTS:")
        for c in recent:
            kind = "[INT]" if c.is_internal else "[PUB]"
            preview = c.body[:300].replace("\n", " ")
            lines.append(f"  {kind} {c.author_name} ({c.created_at.strftime('%m-%d')}): {preview}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini tool declarations (Phase 1 function calling)
# ---------------------------------------------------------------------------

_FUNC_SEARCH_KB = FunctionDeclaration(
    name="search_knowledge_base",
    description=(
        "Search the IT service desk knowledge base for step-by-step procedures, "
        "troubleshooting guides, and configuration instructions. "
        "Formulate the query using ticket context (device type, error, category) "
        "— not the raw user question."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Enriched search query based on ticket context",
            }
        },
        "required": ["query"],
    },
)

_FUNC_HISTORY = FunctionDeclaration(
    name="get_store_history",
    description=(
        "Fetch recent solved/closed tickets for the same store. "
        "Use when asked about past incidents, recurring issues, or history at this store."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you are looking for in the store history",
            }
        },
        "required": ["question"],
    },
)

_FUNC_ALL_TICKETS = FunctionDeclaration(
    name="search_all_tickets",
    description=(
        "Search tickets across all stores using Zendesk keyword full-text search. "
        "Use for counting incidents, trending analysis, or finding tickets by specific keywords. "
        "NOT suitable for semantic similarity — use search_similar_tickets for that."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search terms (e.g. 'Store Operations crash', 'Voice Gateway firmware', 'internet outage')",
            },
            "days": {
                "type": "integer",
                "description": "Restrict to last N days (optional, e.g. 90 for 3 months)",
            },
        },
        "required": ["query"],
    },
)

_FUNC_SIMILAR_TICKETS = FunctionDeclaration(
    name="search_similar_tickets",
    description=(
        "Find semantically similar resolved tickets using vector similarity search. "
        "Use when the engineer asks: 'Has this issue happened before?', "
        "'Show me similar past cases', 'How was this type of problem resolved before?', "
        "'Are there recurring issues like this?'. "
        "Uses AI embeddings to find tickets with similar meaning, not just matching keywords. "
        "Returns solved/closed tickets with their resolutions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Description of the issue to search for. Use ticket context: "
                    "device type, symptoms, error, category. E.g. 'KDS screen not turning on after power outage'"
                ),
            },
            "same_store_only": {
                "type": "boolean",
                "description": (
                    "If true, restrict results to this same store only. "
                    "Use when engineer asks about history at THIS store specifically."
                ),
            },
        },
        "required": ["query"],
    },
)

_FUNC_SEARCH_DRIVE_EXACT = FunctionDeclaration(
    name="search_drive_exact",
    description=(
        "Search indexed Google Drive documentation for an exact phrase or specific text string. "
        "Use when: (1) the engineer asks which document contains a specific phrase or wording "
        "('find document that contains X', 'is there a document that mentions X', "
        "'find where it says X exactly'); "
        "(2) the engineer asks for a verbatim quote or exact paragraph from a document. "
        "This tool does substring matching — it finds documents that literally contain the phrase. "
        "Returns verbatim text chunks and a link to the file. "
        "Prefer search_knowledge_base for general conceptual or 'how to' questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The exact phrase to search for — extract ONLY the phrase itself, "
                    "not the surrounding question. "
                    "Example: user says 'find document that contains Oracle POS and Cash Drawers' "
                    "→ query = 'Oracle POS and Cash Drawers' (not 'find document that contains...')."
                ),
            }
        },
        "required": ["query"],
    },
)

# Default tool set; _build_tools() creates filtered subsets per request
_TOOLS = Tool(function_declarations=[
    _FUNC_SEARCH_KB, _FUNC_SEARCH_DRIVE_EXACT,
    _FUNC_HISTORY, _FUNC_ALL_TICKETS, _FUNC_SIMILAR_TICKETS,
])


def _build_tools(
    include_kb: bool,
    include_history: bool,
    include_similar: bool = True,
    include_all_tickets: bool = True,
    include_drive_exact: bool = False,
) -> Tool:
    """
    Build a Tool object containing only the function declarations that are allowed
    for this request. AUTO mode ignores allowed_function_names, so we control
    access by simply omitting disallowed declarations from the Tool.
    """
    decls = []
    if include_kb:
        decls.append(_FUNC_SEARCH_KB)
    if include_similar:
        decls.append(_FUNC_SIMILAR_TICKETS)
    if include_all_tickets:
        decls.append(_FUNC_ALL_TICKETS)
    if include_history:
        decls.append(_FUNC_HISTORY)
    if include_drive_exact:
        decls.append(_FUNC_SEARCH_DRIVE_EXACT)
    # Fallback: always allow at least similar tickets so Phase 1 can call something
    if not decls:
        decls = [_FUNC_SIMILAR_TICKETS]
    return Tool(function_declarations=decls)


def decide_tool_calls(
    ticket: TicketDetail,
    messages: list[dict],
    include_kb: bool = True,
    include_history: bool = True,
    include_similar: bool = True,
    include_all_tickets: bool = True,
    include_drive_exact: bool = False,
) -> list[dict]:
    """
    Phase 1 non-streaming call: ask Gemini which tools to invoke and with what args.

    Returns a list of dicts like [{"name": "search_knowledge_base", "args": {"query": "..."}}].
    Returns [] if Gemini decides no tools are needed, or on any exception (graceful degradation).

    Args:
        ticket: The current ticket (used to build compact context header).
        messages: Full chat message history.
        include_kb: Whether search_knowledge_base is allowed.
        include_history: Whether get_store_history is allowed.
    """
    try:
        header = _format_ticket_header(ticket)

        # Include last 6 messages as conversation context so Gemini understands
        # follow-up questions like "and for the last month?" or "what about other brands?"
        recent = messages[-6:] if len(messages) > 6 else messages
        conversation_lines: list[str] = []
        for m in recent:
            role_label = "User" if m.get("role") == "user" else "Assistant"
            # Truncate long assistant replies to keep the Phase 1 prompt compact
            body = m.get("content", "")
            if m.get("role") == "assistant" and len(body) > 300:
                body = body[:300] + "…"
            conversation_lines.append(f"{role_label}: {body}")
        conversation_block = "\n".join(conversation_lines)

        prompt = (
            f"You are a tool-routing assistant for a Service Desk AI.\n\n"
            f"TICKET CONTEXT:\n{header}\n\n"
            f"RECENT CONVERSATION:\n{conversation_block}\n\n"
            f"Decide which tools (if any) to call to best answer the latest user message. "
            f"Pay attention to follow-up questions that reference previous tool results "
            f"(e.g. 'and for the last month?' means repeat the same search with a longer window). "
            f"If the question can be answered from the ticket context alone, call no tools.\n\n"
            f"TOOL SELECTION RULES:\n"
            f"- search_drive_exact: use when the engineer says 'find document that contains X', "
            f"'find where it says X', 'is there a document mentioning X', or quotes an exact phrase to locate. "
            f"Pass ONLY the phrase itself as the query (e.g. 'Oracle POS and Cash Drawers'), not the full question.\n"
            f"- search_knowledge_base: use for general how-to, troubleshooting procedures, conceptual questions.\n"
            f"- Never use search_knowledge_base for exact phrase location requests — use search_drive_exact."
        )

        # Only expose allowed declarations — AUTO mode picks from whatever is in the Tool
        tools = _build_tools(
            include_kb, include_history, include_similar, include_all_tickets, include_drive_exact
        )

        estimated_tokens = estimate_tokens_from_text(prompt)
        estimated_cost = calc_cost(estimated_tokens, estimated_tokens // 4, call_type=CallType.CHAT_PHASE1)
        reserved = get_gate_reserve(CallType.CHAT_PHASE1, estimated_cost)
        corr = new_correlation_id()
        check_cost_gate(CallType.CHAT_PHASE1, reserved, fail_closed=False)
        record_estimate(
            CallType.CHAT_PHASE1,
            estimated_cost,
            corr,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=estimated_tokens // 4,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
        )

        response = _model.generate_content(
            prompt,
            tools=[tools],
            tool_config=ToolConfig(
                function_calling_config=ToolConfig.FunctionCallingConfig(
                    mode=ToolConfig.FunctionCallingConfig.Mode.AUTO,
                )
            ),
            generation_config=GenerationConfig.from_dict({"temperature": 0.0, "max_output_tokens": 1024, "thinking_config": {"thinking_budget": 0}}),
        )

        usage = getattr(response, "usage_metadata", None)
        if usage:
            t_in  = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual_cost = record_usage(
                CallType.CHAT_PHASE1, t_in, t_out, t_think,
                correlation_id=corr,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )
            check_divergence(estimated_cost, actual_cost, CallType.CHAT_PHASE1,
                             t_think, thinking_budget_was_zero=True)

        # Extract function calls from the response
        calls: list[dict] = []
        candidate = response.candidates[0] if response.candidates else None
        if candidate:
            if getattr(candidate, "finish_reason", None) in (15, "MALFORMED_FUNCTION_CALL"):
                logger.warning("Skipping malformed model function call")
                return []
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                if part.function_call:
                    fc = part.function_call
                    calls.append({
                        "name": fc.name,
                        "args": dict(fc.args),
                    })

        return calls

    except Exception as exc:
        logger.warning("decide_tool_calls failed (graceful degradation): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Phase 0 — Query Analyzer
# ---------------------------------------------------------------------------

_PHASE0_PROMPT = """You are a query analyzer for ESIP, used by support engineers at a multi-site hospitality company.

Your only job is to classify the engineer's intent and decide how to respond — answer directly or ask one clarifying question first.

You are NOT answering the question. You are deciding HOW to answer it.

CONTEXT YOU RECEIVE:
- The current ticket (subject, status, store, recent comments)
- The full conversation history

AVAILABLE KNOWLEDGE SOURCES (classify intent only — do not mention these names to engineers):
- procedure_knowledge: step-by-step guides, troubleshooting procedures, how-to articles
- past_cases: similar resolved tickets from other stores, how past issues were fixed
- local_context: history of issues at this specific store
- aggregate_data: counts, trends, patterns across all stores and brands

GOAL CLASSIFICATION:
Choose the primary user goal. Use "mixed" only when the engineer is clearly asking for more than one distinct type of outcome in the same request.
- resolve_issue: the engineer wants a fix, a procedure, or what to do right now
- find_precedent: the engineer wants to know if this has happened before and how it was resolved
- understand_history: the engineer wants context on this store's past issues
- analyze_trend: the engineer wants counts, frequencies, or patterns across brands or time
- mixed: the request clearly contains two or more of the above goals

SCOPE CLASSIFICATION:
- current_ticket: the engineer is asking about the issue in the current ticket specifically
- current_store: the engineer is asking about the store's broader history or recurring issues beyond this single ticket
- broader_operations: the engineer is asking across multiple stores, brands, or time periods
- unknown: the scope cannot be determined reliably from the question and context

DECISION RULES:

Choose "answer" when:
- The question is specific enough to retrieve useful information
- The scope is clear, or one reasonable answer path is clearly more likely than the others
- It is a follow-up in an ongoing conversation — use existing context to resolve scope and intent; do NOT clarify unless the new message introduces a genuinely new ambiguity
- The question naturally contains multiple sub-questions with clear scope — answer in structured sections, do NOT clarify

Choose "clarify" when:
- The missing detail would materially change WHICH sources are used or WHAT scope to search
- Without clarification, there is a high chance of answering the wrong question entirely
- Use this sparingly — most questions can be answered usefully without clarification

NEVER ask for clarification if:
- A useful first-pass answer is possible even with some ambiguity
- The detail is already present in the ticket or conversation history
- The question is broad but naturally answered in sections
- One answer path is clearly more likely — just take it
- The question uses "this issue", "this problem", "this kind", "this type" — the current ticket subject defines what "this" means; answer immediately using ticket context
- The question asks how to fix, resolve, or handle something — default to answer with KB + past_cases; do NOT ask whether they want general vs specific
- The ambiguity is only between "general procedure" vs "specific to this ticket" — this is never worth clarifying, answer covers both
- The question is a natural repair/fix request in the context of an open ticket (e.g. "how do I fix this", "what should I do", "how to resolve")
- The question uses a standard temporal expression: "last month", "this week", "last week", "last quarter", "last year", "yesterday", "today", "this month" — these are unambiguous, answer immediately using aggregate_data

CLARIFYING QUESTION RULES:
- Ask at most ONE question by default
- Two questions only in rare cases where one question truly cannot resolve the ambiguity
- Write in plain conversational English — no technical terms, no tool names, no abbreviations
- Make it easy to answer: offer options when possible
- Bad: "Should I prioritize KB retrieval or aggregate BQ analytics for this query?"
- Good: "Are you looking for a step-by-step fix, or do you want to know how often this happens company-wide?"

SOURCE SELECTION RULES:
- Be complete but conservative: include every source class that is clearly needed, exclude any that are only weakly or hypothetically relevant
- For "how to fix / what to do" → procedure_knowledge, past_cases
- For "has this happened before / similar issues" → past_cases, local_context
- For "how often / how many / trends across brands" → aggregate_data
- For clear mixed questions → include all relevant source classes

Respond ONLY with valid JSON:
{
  "goal": "resolve_issue | find_precedent | understand_history | analyze_trend | mixed",
  "scope": "current_ticket | current_store | broader_operations | unknown",
  "decision": "answer | clarify",
  "source_classes": ["procedure_knowledge", "past_cases", "local_context", "aggregate_data"],
  "clarifying_questions": [],
  "reasoning": "one brief factual sentence — do not restate the question"
}

If decision is "answer" → clarifying_questions must be [].
If decision is "clarify" → source_classes should reflect what you expect to need after clarification."""


class QueryAnalysis:
    """Result of Phase 0 query analysis."""
    __slots__ = ("goal", "scope", "decision", "source_classes", "clarifying_questions", "reasoning")

    def __init__(self, data: dict) -> None:
        self.goal: str = data.get("goal", "resolve_issue")
        self.scope: str = data.get("scope", "unknown")
        self.decision: str = data.get("decision", "answer")
        self.source_classes: list[str] = data.get("source_classes") or []
        self.clarifying_questions: list[str] = data.get("clarifying_questions") or []
        self.reasoning: str = data.get("reasoning", "")

    @property
    def needs_procedure_knowledge(self) -> bool:
        return "procedure_knowledge" in self.source_classes

    @property
    def needs_past_cases(self) -> bool:
        return "past_cases" in self.source_classes

    @property
    def needs_local_context(self) -> bool:
        return "local_context" in self.source_classes

    @property
    def needs_aggregate_data(self) -> bool:
        return "aggregate_data" in self.source_classes


_PHASE0_FALLBACK = QueryAnalysis({
    "goal": "resolve_issue",
    "scope": "current_ticket",
    "decision": "answer",
    "source_classes": ["procedure_knowledge", "past_cases", "local_context"],
    "clarifying_questions": [],
    "reasoning": "fallback — Phase 0 failed",
})


def analyze_query(
    ticket: TicketDetail,
    messages: list[dict],
) -> QueryAnalysis:
    """
    Phase 0: classify the engineer's intent before tool execution.

    Makes a fast non-streaming Gemini call (JSON mode) and returns a QueryAnalysis.
    On any failure, returns a safe fallback that enables the standard tool set
    so the conversation degrades gracefully rather than breaking.

    Args:
        ticket: Current ticket (used for compact context header).
        messages: Full conversation history including the latest user message.
    """
    try:
        header = _format_ticket_header(ticket)

        recent = messages[-6:] if len(messages) > 6 else messages
        history_lines: list[str] = []
        for m in recent:
            role_label = "User" if m.get("role") == "user" else "Assistant"
            body = m.get("content", "")
            if m.get("role") == "assistant" and len(body) > 200:
                body = body[:200] + "…"
            history_lines.append(f"{role_label}: {body}")
        history_block = "\n".join(history_lines)

        today = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %d, %Y")
        prompt = (
            f"{_PHASE0_PROMPT}\n\n"
            f"TODAY'S DATE: {today} (Eastern Time (ET))\n\n"
            f"TICKET CONTEXT:\n{header}\n\n"
            f"CONVERSATION (most recent last):\n{history_block}"
        )

        estimated_tokens = estimate_tokens_from_text(prompt)
        estimated_cost = calc_cost(estimated_tokens, estimated_tokens // 4, call_type=CallType.CHAT_PHASE0)
        reserved = get_gate_reserve(CallType.CHAT_PHASE0, estimated_cost)
        corr = new_correlation_id()
        check_cost_gate(CallType.CHAT_PHASE0, reserved, fail_closed=False)
        record_estimate(
            CallType.CHAT_PHASE0,
            estimated_cost,
            corr,
            est_basis="formula",
            est_input_tokens=estimated_tokens,
            est_output_tokens=estimated_tokens // 4,
            expected_cost_cad=estimated_cost,
            reserved_cost_cad=reserved,
        )

        response = _model.generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict({"temperature": 0.0, "max_output_tokens": 512, "response_mime_type": "application/json", "thinking_config": {"thinking_budget": 0}}),
        )

        usage = getattr(response, "usage_metadata", None)
        if usage:
            t_in  = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual_cost = record_usage(
                CallType.CHAT_PHASE0, t_in, t_out, t_think,
                correlation_id=corr,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )
            check_divergence(estimated_cost, actual_cost, CallType.CHAT_PHASE0,
                             t_think, thinking_budget_was_zero=True)

        data = json.loads(response.text)
        analysis = QueryAnalysis(data)
        logger.info(
            "Phase 0: goal=%s scope=%s decision=%s sources=%s",
            analysis.goal, analysis.scope, analysis.decision, analysis.source_classes,
        )
        return analysis

    except (CostGateError, CostDivergenceError):
        raise
    except Exception as exc:
        logger.warning("analyze_query failed — using fallback: %s", exc)
        return _PHASE0_FALLBACK


# ---------------------------------------------------------------------------
# All-tickets result formatter
# ---------------------------------------------------------------------------

def _format_all_tickets_results(tickets: list, query: str) -> str:
    if not tickets:
        return f"\nCROSS-STORE SEARCH for '{query}': No matching tickets found."
    lines = [f"\nCROSS-STORE SEARCH for '{query}' ({len(tickets)} results):"]
    for t in tickets:
        org = f" | {t.org_name}" if t.org_name else ""
        cat = f" | {_slug_to_title(t.issue_category)}" if t.issue_category else ""
        lines.append(
            f"  #{t.id} | {t.updated_at.date()} | {t.status}{org}{cat} | {t.subject[:70]}"
        )
    return "\n".join(lines)


def _format_similar_tickets_results(tickets: list[dict], query: str, same_store_only: bool) -> str:
    scope = "SAME STORE" if same_store_only else "ALL STORES"
    if not tickets:
        return f"\nSIMILAR RESOLVED TICKETS ({scope}, query: '{query}'): No similar tickets found."
    lines = [f"\nSIMILAR RESOLVED TICKETS — {scope} (semantic search for: '{query}', {len(tickets)} results):"]
    for t in tickets:
        org_part = f" | {t['org_name']}" if t.get("org_name") else ""
        cat_part = f" | {_slug_to_title(str(t['issue_category']))}" if t.get("issue_category") else ""
        resolution_part = f"\n    Resolution: {t['resolution'][:200]}" if t.get("resolution") else ""
        lines.append(
            f"  #{t['ticket_id']} | {t.get('solved_at', '')} | {t.get('brand', '')}{org_part}"
            f"{cat_part}\n    Subject: {t['subject'][:100]}{resolution_part}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vision OCR
# ---------------------------------------------------------------------------

_OCR_MAX_RETRIES   = 3
_OCR_RETRY_BACKOFF = 5


def ocr_image(image_bytes: bytes, mime_type: str) -> tuple[str | None, str]:
    """
    Extract visible text and a one-line image description using Gemini Vision (single call).

    The model must output `[IMAGE DESCRIPTION: …]` as the first line, then a blank line, then
    extracted text. Description and transcription share one max_output_tokens budget (2048);
    if truncated, the description line stays complete and the tail of the transcription is cut.

    Returns (ocr_text, error_str).
      - ocr_text is None only when the model returns an empty response after strip.
      - error_str is "" on success, non-empty on failure.
    """
    # CIS: gate check with real projected cost, then record ESTIMATE before call.
    # The expected-cost signal comes from the reviewed pricing profile; the gate
    # reserves a separate conservative upper bound for variable image sizes.
    # Gate is a pre-call UPPER BOUND — over-estimating is safe, under-estimating risks uncapped spend.
    _ocr_expected = OCR_MEASURED_EXPECTED_CAD
    _ocr_reserved = get_gate_reserve(CallType.OCR_IMAGE, _ocr_expected)
    _ocr_corr_id = new_correlation_id()
    try:
        check_cost_gate(CallType.OCR_IMAGE, projected_additional_cost=_ocr_reserved, fail_closed=True)
    except CostGateError:
        raise
    record_estimate(
        CallType.OCR_IMAGE,
        _ocr_expected,
        correlation_id=_ocr_corr_id,
        est_basis="measured_avg",
        expected_cost_cad=_ocr_expected,
        reserved_cost_cad=_ocr_reserved,
    )

    for attempt in range(1, _OCR_MAX_RETRIES + 1):
        try:
            image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
            response = _model.generate_content(
                [
                    image_part,
                    (
                        "You are processing an image attached to an IT support ticket "
                        "from a multi-site support operation. Images may show: "
                        "error dialogs, application interfaces, network configuration screens, "
                        "terminal/log output, Slack or email conversations, remote desktop sessions, "
                        "POS system screens, physical equipment, cables, hardware, or damage.\n\n"
                        "OUTPUT ORDER — follow strictly:\n\n"
                        "PART 1 (must be the VERY FIRST line of your entire response, before any other text):\n"
                        "[IMAGE DESCRIPTION: one sentence describing what the image shows — "
                        "type of equipment, physical state, visible damage, screen content, "
                        "cable types, hardware components, or any other relevant visual detail]\n\n"
                        "Then a single blank line.\n\n"
                        "PART 2 — Extract ALL visible text from the image after that blank line:\n"
                        "- Preserve exact wording of error messages, codes, and technical values\n"
                        "- Keep IP addresses, hostnames, port numbers, version numbers exactly as shown\n"
                        "- Preserve line breaks and structure in logs, stack traces, and config output\n"
                        "- Include all UI labels, button text, menu items, and window titles\n"
                        "- Include all chat/email text — sender names, timestamps, message content\n"
                        "- Do NOT use markdown — plain text only\n\n"
                        "If you must truncate due to length, keep PART 1 complete and truncate from the end of PART 2."
                    ),
                ],
                generation_config=GenerationConfig.from_dict({"temperature": 0.0, "max_output_tokens": 2048, "thinking_config": {"thinking_budget": 0}}),
            )
            # Record actual usage; thinking guard fires if budget=0 was ignored.
            usage = getattr(response, "usage_metadata", None)
            if usage:
                t_in  = getattr(usage, "prompt_token_count", 0) or 0
                t_out = getattr(usage, "candidates_token_count", 0) or 0
                t_think = getattr(usage, "thoughts_token_count", 0) or 0
                actual_cost = record_usage(
                    CallType.OCR_IMAGE, t_in, t_out, t_think,
                    correlation_id=_ocr_corr_id,
                    est_cost_usd=_ocr_expected,
                    expected_cost_cad=_ocr_expected,
                )
                check_divergence(
                    estimated_cost=_ocr_expected,
                    actual_cost=actual_cost,
                    call_type=CallType.OCR_IMAGE,
                    tokens_thinking=t_think,
                    thinking_budget_was_zero=True,
                )
            text = (response.text or "").strip()
            return (text if text else None), ""
        except (CostGateError, CostDivergenceError):
            raise
        except Exception as exc:
            error_kind = exc.kind if isinstance(exc, GenAITransportError) else None
            if isinstance(exc, TimeoutError) or error_kind is GenAITransportErrorKind.TIMEOUT:
                return None, f"gemini_error:{exc}"
            err_str = str(exc)
            is_rate_limit = error_kind is GenAITransportErrorKind.QUOTA or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
            if attempt < _OCR_MAX_RETRIES:
                wait = _OCR_RETRY_BACKOFF * (2 if is_rate_limit else 1)
                logger.warning("ocr_image attempt %d failed: %s — retrying in %ds", attempt, exc, wait)
                time.sleep(wait)
            else:
                return None, f"gemini_error:{exc}"
    return None, "ocr_failed"




# ---------------------------------------------------------------------------
# Summary generation (non-streaming)
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """You are an expert IT Service Desk analyst for Northstar Hospitality Group,
the fictional Northstar Hospitality Group. Analyze the following support ticket in full detail.

SCREENSHOT CONTEXT — IMPORTANT:
The ticket context below may include blocks labeled [Screenshot — filename.png] with OCR-extracted text.
These screenshots are images attached by engineers during the ticket — they often contain critical technical
details not present in the comment text: error messages, IP addresses, configuration values, log output,
Slack conversations, remote desktop views, order management screens, etc.
You MUST read and use the content of these screenshot blocks when building the summary.
Include specific values from screenshots (IPs, error codes, config items, app states) in the relevant sections.

WRITING STYLE:
- Write in clear, professional narrative prose — not bureaucratic lists of facts.
- Tell the story of the incident: what happened, why, what was done, how it ended.
- Use only facts present in the ticket. Do not fabricate or speculate beyond what is documented.
- If something is unclear or unknown, say so explicitly ("root cause was not conclusively determined").
- Be thorough — engineers read this to avoid reading every comment.
- Do not over-compress operationally important details. Capture the initial report, business impact,
  vendor/internal actions, later updates, unresolved follow-ups, and concrete measurements.
- Preserve exact evidence when it matters: speeds, packet loss, dates, vendor case numbers, device names,
  SSIDs, IP addresses, error messages, and status changes.
- Keep all technical terms, product names, store IDs, error codes, IP addresses, and proper nouns in English,
  even when the summary is requested in another language.

Always return valid JSON matching the requested schema exactly.
For null/empty fields: use null for strings, [] for arrays — never omit required keys.
JSON ENCODING: all string values must be valid JSON — use \\n for line breaks within strings, never literal newlines."""

BRIEF_SUMMARY_SCHEMA = """Return a JSON object with this exact structure:
{
  "overview": "...",
  "current_status": "...",
  "key_actions_taken": ["...", ...],
  "kb_suggestions": [{"type": "kb_article", "title": "...", "snippet": "...", "url": "..."}]
}

FIELD RULES:
overview — Narrative description of the incident in flowing prose. Use \\n\\n to separate logical paragraphs (e.g. problem description, what was tried, resolution). No bullet points. No sentence count limit.
current_status — Current state: resolved/pending/escalated and what that means in practice. One or two sentences.
key_actions_taken — Every distinct step taken, one per list item. Format each item as: "**Person Name** — what they did". Always bold the person's name with **...**. Do not merge multiple distinct actions into one item.
kb_suggestions — Up to 3 most relevant articles from KB context. Return [] if no KB provided."""

# Vertex constrained decoding — avoids invalid JSON (unescaped quotes in markdown_text, truncation mid-token).
_SUMMARY_KB_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "snippet": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["type", "title", "snippet", "url"],
}

BRIEF_SUMMARY_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "current_status": {"type": "string"},
        "key_actions_taken": {"type": "array", "items": {"type": "string"}},
        "kb_suggestions": {"type": "array", "items": _SUMMARY_KB_ITEM_SCHEMA},
    },
    "required": ["overview", "current_status", "key_actions_taken", "kb_suggestions"],
}

DETAILED_SUMMARY_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "markdown_text": {"type": "string"},
        "kb_suggestions": {"type": "array", "items": _SUMMARY_KB_ITEM_SCHEMA},
    },
    "required": ["markdown_text", "kb_suggestions"],
}

CUSTOMER_RISK_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tone": {"type": "string"},
        "label": {"type": "string"},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
    },
    "required": ["tone", "label", "summary", "reasons", "next_action"],
}

DETAILED_SUMMARY_SCHEMA = """Return a JSON object with this exact structure:
{
  "markdown_text": "<full markdown narrative — see format below>",
  "kb_suggestions": [
    {"type": "kb_article", "title": "...", "snippet": "...", "url": "..."}
  ]
}

The markdown_text field must contain a complete incident analysis written as markdown, using the following section structure. Write in narrative prose within each section — not mechanical lists.

## 📋 General Info
| Field | Value |
|---|---|
| Ticket | #{ticket_id} |
| Store | {store_number} — {restaurant_name} |
| Category | <human-readable — match ISSUE CATEGORY line from ticket; never raw underscore slug> |
| Status | {status} |
| Created | {created_at} |
| Resolved | {solved_at or "Open"} |
| Assigned to | {assignee name} |

## 🔍 Incident Essence
[2–3 paragraphs: what the user reported, what the actual technical problem was, and what the business impact was. Use facts from ticket fields, comments, and screenshots.]

## ✅ Final Status
[1–2 sentences: how the ticket ended — resolved/pending/escalated, what the outcome was.]

## 🛠️ What Was Done
[Narrative of the resolution work, organized by person or phase if multiple agents were involved. Use sub-headings (###) for each distinct person or phase. Prose style — not a bullet list unless listing specific sequential steps.]

## 📅 Timeline
[Every significant event in chronological order, including dates and times from comments and screenshots.]
- **YYYY-MM-DD HH:MM** — event description

## 👥 Participants
[Everyone who appeared in the ticket with their role.]
- **Name** — role (e.g. Reported / Assigned / Also involved / Vendor)

## 🧠 Key Decisions
[Significant decisions made during the incident — e.g. escalating to vendor, ordering a part, scheduling on-site visit. Write "No significant decisions documented" if none.]

## ⚠️ Difficulties & Complications
[Blockers, dead ends, or complications encountered. Write "None documented" if none.]

## 🔎 Root Cause Analysis
[Root cause if determinable. If not, explain what is known and what remains unclear. Never write "Unknown" without explanation.]

## 📊 Task Status
| Task | Status |
|---|---|
| {task description} | Done / In Progress / Pending |

## 💡 Conclusion
[2–3 sentences: what this ticket was about, how it was resolved, and any key takeaway for future reference.]

RULES FOR markdown_text:
- In General Info, Category must read like plain English for engineers (same meaning as the ticket's ISSUE CATEGORY line, e.g. "Online Ordering > Printer > Not Receiving Orders"). Never paste machine tags with underscores.
- Use ONLY information from the ticket. Do not fabricate.
- Include ALL screenshot/OCR content in relevant sections where applicable.
- Write narrative prose. Avoid dry mechanical lists — the reader should understand the full story.
- The English detailed summary is the canonical source for translated summaries. Make it complete enough
  that a translated version will not lose important context.
- Do not omit late-ticket updates, vendor responses, scheduled tests, failed coordination attempts,
  unresolved next steps, or conflicting evidence between the store, ISP, and internal monitoring.
- In "What Was Done", cover every meaningful participant or phase that materially changed the ticket.
- In "Timeline", include every significant operational event, especially escalations, closures,
  re-opened concerns, vendor updates, and pending action requests.
- In "Root Cause Analysis", separate confirmed facts from plausible-but-unconfirmed links.
- If a section has no data (e.g. no complications), still include it with the specified placeholder text.
- Escape all newlines as \\n in the JSON string value.

kb_suggestions: up to 5 most relevant articles from KB context. Return [] if no KB provided."""

_TEXT_ARRAY_SCHEMA = {"type": "array", "items": {"type": "string"}}

_WORK_DONE_SECTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "heading": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["heading", "text"],
}

_TIMELINE_EVENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "datetime": {"type": "string"},
        "event": {"type": "string"},
    },
    "required": ["datetime", "event"],
}

_PARTICIPANT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "role": {"type": "string"},
    },
    "required": ["name", "role"],
}

_TASK_STATUS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["task", "status"],
}

DETAILED_STRUCTURED_SUMMARY_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "incident_essence_paragraphs": _TEXT_ARRAY_SCHEMA,
        "final_status": {"type": "string"},
        "work_done_sections": {"type": "array", "items": _WORK_DONE_SECTION_SCHEMA},
        "timeline_events": {"type": "array", "items": _TIMELINE_EVENT_SCHEMA},
        "participants": {"type": "array", "items": _PARTICIPANT_SCHEMA},
        "key_decisions": _TEXT_ARRAY_SCHEMA,
        "difficulties": _TEXT_ARRAY_SCHEMA,
        "root_cause_analysis": {"type": "string"},
        "task_status_rows": {"type": "array", "items": _TASK_STATUS_SCHEMA},
        "conclusion": {"type": "string"},
        "kb_suggestions": {"type": "array", "items": _SUMMARY_KB_ITEM_SCHEMA},
    },
    "required": [
        "incident_essence_paragraphs",
        "final_status",
        "work_done_sections",
        "timeline_events",
        "participants",
        "key_decisions",
        "difficulties",
        "root_cause_analysis",
        "task_status_rows",
        "conclusion",
        "kb_suggestions",
    ],
}

DETAILED_STRUCTURED_SUMMARY_SCHEMA = """Return a JSON object with structured section data for the detailed summary.
The backend will render the final product markdown headings and General Info table.

Required JSON fields:
- incident_essence_paragraphs: array of prose paragraphs.
- final_status: current outcome/state in prose.
- work_done_sections: array of {heading, text}; one item per material person, vendor, or phase.
- timeline_events: array of {datetime, event}; chronological significant events.
- participants: array of {name, role}; all named people/vendors/store contacts visible in the ticket.
- key_decisions: array of concrete decisions/actions.
- difficulties: array of blockers, contradictions, coordination failures, and unresolved dependencies.
- root_cause_analysis: prose analysis separating confirmed facts, suspected contributors, and unknowns.
- task_status_rows: array of {task, status}; granular completed/in-progress/pending work.
- conclusion: concise final takeaway.
- kb_suggestions: up to 5 relevant KB article objects; [] if no KB context is useful.

QUALITY BAR FOR CANONICAL ENGLISH DETAILED SUMMARY:
- This is a senior Service Desk handoff, not a short recap.
- For simple solved tickets, stay concise. For unresolved, escalated, multi-day, vendor-involved,
  or recurring incidents, be deliberately detailed.
- Preserve concrete evidence from the ticket whenever present: exact speeds, latency/packet loss,
  dates/times, case numbers, prior ticket/case references, device names, network names/SSIDs,
  IPs, vendor names, and explicit pending requests.
- Include late vendor updates, failed contact attempts, pending tests, internal handoffs,
  and unresolved next steps.
- If vendor statements conflict with store reports or internal monitoring, state the discrepancy.
- If a possible historical trigger is mentioned but not proven, include it as unconfirmed context.
- Use only ticket facts. Do not invent.

FIELD DEPTH RULES:
- incident_essence_paragraphs: 2-4 substantial paragraphs for unresolved/escalated tickets.
- work_done_sections: include each material participant/vendor/phase; do not omit vendor coordination.
- timeline_events: every significant dated event; multi-day unresolved tickets should usually have 15+
  events when supported.
- key_decisions: concrete operational decisions/actions, not generic observations.
- difficulties: concrete blockers/complications; multi-party unresolved cases often need 5+.
- task_status_rows: granular completed/in-progress/pending work; unresolved vendor cases often need 8+.
- root_cause_analysis: never flatten unresolved multi-factor cases into one vague cause."""

SUMMARY_TRANSLATION_SYSTEM_PROMPT = """You are a professional IT Service Desk translator for Northstar Hospitality Group.
Translate an already-generated canonical English ticket summary into the requested language.

Rules:
- Do not summarize again, shorten, expand, reorder, reinterpret, or add facts.
- Preserve the JSON schema exactly.
- Preserve markdown formatting, tables, bullets, bold text, links, and emoji.
- For detailed summaries, keep the product section headings exactly as written in English
  (for example: "General Info", "Incident Essence", "Final Status", "What Was Done",
  "Timeline", "Participants", "Key Decisions", "Difficulties & Complications",
  "Root Cause Analysis", "Task Status", "Conclusion").
- Keep people's names, company names, tenant brands, product names, application names,
  device names, SSIDs, store/location IDs, ticket numbers, case numbers, IP addresses,
  URLs, error codes, and other proper nouns in their original English form.
- Translate literal English idioms into professional service-desk language in the target language.
  For Russian, avoid awkward literal phrasing; prefer professional terms such as
  "after the 2024 incident" or "after restoration/wiring work" when that is what the ticket context means.
- Return valid JSON only."""

_DETAILED_PRODUCT_HEADINGS = [
    "## 📋 General Info",
    "## 🔍 Incident Essence",
    "## ✅ Final Status",
    "## 🛠️ What Was Done",
    "## 📅 Timeline",
    "## 👥 Participants",
    "## 🧠 Key Decisions",
    "## ⚠️ Difficulties & Complications",
    "## 🔎 Root Cause Analysis",
    "## 📊 Task Status",
    "## 💡 Conclusion",
]

_GENERAL_INFO_LABELS = ["Ticket", "Store", "Category", "Status", "Created", "Resolved", "Assigned to"]


def _parse_summary_json_response(raw: str | None, mode: str) -> dict:
    """Strip optional ``` fences and parse JSON; raises ValueError on failure."""
    if raw is None or not str(raw).strip():
        raise ValueError("Gemini returned empty response for summary")
    text = str(raw).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:500].replace("\n", " ")
        tail = text[-500:].replace("\n", " ") if len(text) > 500 else ""
        logger.error(
            "Summary JSON parse failed mode=%s len=%s: %s preview=%r tail=%r",
            mode,
            len(text),
            exc,
            preview,
            tail,
        )
        raise ValueError("Gemini returned invalid JSON for summary") from exc


def _as_nonempty_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _summary_store_value(ticket: TicketDetail) -> str:
    if not ticket.store:
        return "Unknown"
    store_id = ticket.store.store_id or "Unknown"
    org_name = ticket.store.org_name or ticket.store.brand or "Unknown"
    return f"{store_id} — {org_name}"


def _summary_resolved_value(ticket: TicketDetail) -> str:
    if ticket.status.lower() in {"solved", "closed"}:
        return ticket.updated_at.date().isoformat()
    return "Open"


def _render_detailed_summary_markdown(ticket: TicketDetail, data: dict) -> str:
    """Render structured detailed summary data into the product markdown format."""
    lines: list[str] = [
        "## 📋 General Info",
        "| Field | Value |",
        "|---|---|",
        f"| Ticket | #{ticket.id} |",
        f"| Store | {_summary_store_value(ticket)} |",
        f"| Category | {_slug_to_title(ticket.issue_category) if ticket.issue_category else 'Uncategorized'} |",
        f"| Status | {ticket.status} |",
        f"| Created | {ticket.created_at.date().isoformat()} |",
        f"| Resolved | {_summary_resolved_value(ticket)} |",
        f"| Assigned to | {ticket.assignee_name or 'Unassigned'} |",
        "",
        "## 🔍 Incident Essence",
    ]

    essence = _as_nonempty_strings(data.get("incident_essence_paragraphs"))
    lines.append("\n\n".join(essence) if essence else "No incident essence documented.")

    lines.extend(["", "## ✅ Final Status", str(data.get("final_status") or "No final status documented.")])

    lines.extend(["", "## 🛠️ What Was Done"])
    work_sections = data.get("work_done_sections")
    if isinstance(work_sections, list) and work_sections:
        for section in work_sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "Work Phase").strip()
            text = str(section.get("text") or "").strip()
            if text:
                lines.extend([f"### {heading}", text, ""])
    else:
        lines.append("No work details documented.")

    lines.append("## 📅 Timeline")
    timeline_events = data.get("timeline_events")
    if isinstance(timeline_events, list) and timeline_events:
        for event in timeline_events:
            if not isinstance(event, dict):
                continue
            dt = str(event.get("datetime") or "").strip()
            text = str(event.get("event") or "").strip()
            if dt and text:
                lines.append(f"- **{dt}** — {text}")
    else:
        lines.append("No significant timeline events documented.")

    lines.extend(["", "## 👥 Participants"])
    participants = data.get("participants")
    if isinstance(participants, list) and participants:
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            name = str(participant.get("name") or "").strip()
            role = str(participant.get("role") or "").strip()
            if name:
                lines.append(f"- **{name}** — {role or 'Also involved'}")
    else:
        lines.append("No participants documented.")

    lines.extend(["", "## 🧠 Key Decisions"])
    decisions = _as_nonempty_strings(data.get("key_decisions"))
    lines.extend([f"- {item}" for item in decisions] or ["No significant decisions documented."])

    lines.extend(["", "## ⚠️ Difficulties & Complications"])
    difficulties = _as_nonempty_strings(data.get("difficulties"))
    lines.extend([f"- {item}" for item in difficulties] or ["None documented."])

    lines.extend(
        [
            "",
            "## 🔎 Root Cause Analysis",
            str(data.get("root_cause_analysis") or "Root cause was not conclusively determined."),
            "",
            "## 📊 Task Status",
            "| Task | Status |",
            "|---|---|",
        ]
    )
    task_rows = data.get("task_status_rows")
    if isinstance(task_rows, list) and task_rows:
        for row in task_rows:
            if not isinstance(row, dict):
                continue
            task = str(row.get("task") or "").strip()
            status = str(row.get("status") or "").strip()
            if task:
                lines.append(f"| {task} | {status or 'Pending'} |")
    else:
        lines.append("| No specific tasks documented | Pending |")

    lines.extend(["", "## 💡 Conclusion", str(data.get("conclusion") or "No conclusion documented.")])
    return "\n".join(lines).strip()


def _render_detailed_summary(ticket: TicketDetail, data: dict) -> dict:
    return {
        "markdown_text": _render_detailed_summary_markdown(ticket, data),
        "kb_suggestions": _normalize_summary_kb_suggestions(data.get("kb_suggestions")),
    }


def _normalize_summary_kb_suggestions(value: object) -> list[dict]:
    """Normalize model-returned KB suggestions to the strict API schema."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title:
            continue
        out.append({
            "type": "kb_article",
            "title": title,
            "snippet": snippet,
            "url": url,
        })
    return out


def _normalize_summary_payload(summary: dict, mode: str) -> dict:
    summary["kb_suggestions"] = _normalize_summary_kb_suggestions(summary.get("kb_suggestions"))
    if mode == "brief":
        summary.setdefault("key_actions_taken", [])
    return summary


CUSTOMER_RISK_PROMPT_VERSION = "customer_risk_v2"


def generate_customer_risk_assessment(assessment_input: dict[str, Any]) -> dict[str, Any]:
    """
    Interpret deterministic customer-risk facts into concise UI copy.

    The caller computes counts, timestamps, and severity guardrails. Gemini may
    classify tone and explain evidence, but it must not invent metrics or change
    factual counts.
    """
    prompt = (
        "You are ESIP's ticket customer-risk assessor for Northstar Support Operations.\n"
        "Use only the supplied structured facts and public customer comment excerpts.\n"
        "Do not invent counts, timestamps, people, or actions. Do not use internal notes.\n"
        "Classify the customer's tone as exactly one of: calm, concerned, frustrated, escalated.\n"
        "Write concise operator-facing copy for an engineer opening this ticket.\n"
        "The next_action must be the first practical action the engineer should take.\n"
        "If deterministic facts show delayed public response or customer spoke last, prioritize a public update or customer contact.\n"
        "Return JSON matching the configured schema.\n\n"
        f"ASSESSMENT_INPUT:\n{json.dumps(assessment_input, ensure_ascii=False, default=str)}"
    )

    estimated_tokens = estimate_tokens_from_text(prompt)
    est_output_tokens = 512
    estimated_cost = calc_cost(estimated_tokens, est_output_tokens, call_type=CallType.CUSTOMER_RISK)
    reserved = get_gate_reserve(CallType.CUSTOMER_RISK, estimated_cost)
    corr = new_correlation_id()
    check_cost_gate(CallType.CUSTOMER_RISK, reserved, fail_closed=True)
    record_estimate(
        CallType.CUSTOMER_RISK,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=est_output_tokens,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    response = _model.generate_content(
        prompt,
        generation_config=GenerationConfig.from_dict(
            {
                "response_mime_type": "application/json",
                "response_schema": CUSTOMER_RISK_RESPONSE_SCHEMA,
                "temperature": 0.1,
                "max_output_tokens": 1024,
                "thinking_config": {"thinking_budget": 0},
            }
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        t_in = getattr(usage, "prompt_token_count", 0) or 0
        t_out = getattr(usage, "candidates_token_count", 0) or 0
        t_think = getattr(usage, "thoughts_token_count", 0) or 0
        actual_cost = record_usage(
            CallType.CUSTOMER_RISK,
            t_in,
            t_out,
            t_think,
            correlation_id=corr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        check_divergence(
            estimated_cost,
            actual_cost,
            CallType.CUSTOMER_RISK,
            t_think,
            thinking_budget_was_zero=True,
        )

    data = json.loads(response.text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Customer risk Gemini response was not a JSON object")
    return data


def _restore_detailed_product_headings(markdown_text: str) -> str:
    """Keep detailed-summary section headings in the product's English style after translation."""
    heading_idx = 0
    lines: list[str] = []
    in_general_info_table = False
    general_info_row_idx = -1
    in_task_status_table = False
    task_status_row_idx = -1

    for line in str(markdown_text or "").splitlines():
        if line.startswith("## ") and heading_idx < len(_DETAILED_PRODUCT_HEADINGS):
            restored_heading = _DETAILED_PRODUCT_HEADINGS[heading_idx]
            lines.append(restored_heading)
            in_general_info_table = restored_heading == "## 📋 General Info"
            general_info_row_idx = -1
            in_task_status_table = restored_heading == "## 📊 Task Status"
            task_status_row_idx = -1
            heading_idx += 1
        elif in_general_info_table and line.startswith("|"):
            general_info_row_idx += 1
            lines.append(_restore_general_info_table_line(line, general_info_row_idx))
        elif in_task_status_table and line.startswith("|"):
            task_status_row_idx += 1
            if task_status_row_idx == 0:
                lines.append("| Task | Status |")
            elif task_status_row_idx == 1:
                lines.append("|---|---|")
            else:
                lines.append(line)
        else:
            if line.strip() == "":
                in_general_info_table = False
                in_task_status_table = False
            lines.append(line)
    return "\n".join(lines).strip()


def _restore_general_info_table_line(line: str, row_idx: int) -> str:
    if row_idx == 0:
        return "| Field | Value |"
    if row_idx == 1:
        return "|---|---|"
    label_idx = row_idx - 2
    if label_idx < 0 or label_idx >= len(_GENERAL_INFO_LABELS):
        return line
    cells = [part.strip() for part in line.strip().strip("|").split("|")]
    value = cells[1] if len(cells) > 1 else ""
    return f"| {_GENERAL_INFO_LABELS[label_idx]} | {value} |"


def generate_summary(
    ticket: TicketDetail,
    mode: str,
    kb_results: Optional[list[KBArticleSource]] = None,
    language: str = "en",
    images_by_comment: dict | None = None,
) -> dict:
    """
    Generate a structured AI summary for a ticket.
    Returns a dict matching BriefSummary or DetailedSummary schema.
    If language != "en", all text values are returned in that language.
    images_by_comment: chronological screenshot context (see fetch_ticket_images_by_comment).
    """
    ticket_text = _format_ticket_for_prompt(ticket, images_by_comment)
    kb_text = _format_kb_results(kb_results or [])
    schema = BRIEF_SUMMARY_SCHEMA if mode == "brief" else DETAILED_STRUCTURED_SUMMARY_SCHEMA

    lang_instruction = ""
    if language != "en":
        lang_name = _LANGUAGE_NAMES.get(language, language)
        detailed_keys = (
            "detailed structured fields: incident_essence_paragraphs, final_status, "
            "work_done_sections, timeline_events, participants, key_decisions, difficulties, "
            "root_cause_analysis, task_status_rows, conclusion, kb_suggestions"
        )
        lang_instruction = (
            f"\n\nIMPORTANT: Translate all text values in the JSON response into {lang_name}. "
            "JSON keys must remain in English exactly as specified "
            "(brief: overview, current_status, key_actions_taken, kb_suggestions; "
            f"{detailed_keys}). "
            "Do NOT translate: people's names, company names, product names (e.g. Orbit View, Zendesk), "
            "technical terms, application names, store/location IDs, ticket numbers, error codes, "
            "IP addresses, URLs, or any proper nouns. Keep all such terms in their original English form."
        )

    prompt = (
        f"{SUMMARY_SYSTEM_PROMPT}\n\n"
        f"{ticket_text}"
        f"{kb_text}\n\n"
        f"INSTRUCTIONS:\n{schema}"
        f"{lang_instruction}"
    )

    estimated_tokens = estimate_tokens_from_text(prompt)
    # Detailed: long markdown_text in one JSON field; truncation → invalid JSON. response_schema helps validity; still need headroom.
    max_output_tokens = 4096 if mode == "brief" else 16384
    est_output_tokens = estimated_tokens // 4
    if mode == "detailed":
        est_output_tokens = max(est_output_tokens, min(8192, max_output_tokens))
    response_schema = (
        BRIEF_SUMMARY_RESPONSE_SCHEMA if mode == "brief" else DETAILED_STRUCTURED_SUMMARY_RESPONSE_SCHEMA
    )
    estimated_cost = calc_cost(estimated_tokens, est_output_tokens, call_type=CallType.SUMMARY)
    reserved = get_gate_reserve(CallType.SUMMARY, estimated_cost)
    corr = new_correlation_id()
    check_cost_gate(CallType.SUMMARY, reserved, fail_closed=True)
    record_estimate(
        CallType.SUMMARY,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=est_output_tokens,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    response = _model.generate_content(
        prompt,
        generation_config=GenerationConfig.from_dict(
            {
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": 0.3,
                "max_output_tokens": max_output_tokens,
                "thinking_config": {"thinking_budget": 0},
            }
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        t_in  = getattr(usage, "prompt_token_count", 0) or 0
        t_out = getattr(usage, "candidates_token_count", 0) or 0
        t_think = getattr(usage, "thoughts_token_count", 0) or 0
        actual_cost = record_usage(
            CallType.SUMMARY, t_in, t_out, t_think,
            correlation_id=corr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        check_divergence(estimated_cost, actual_cost, CallType.SUMMARY,
                         t_think, thinking_budget_was_zero=True)

    cand = response.candidates[0] if getattr(response, "candidates", None) else None
    finish_reason = getattr(cand, "finish_reason", None) if cand else None
    raw_text = getattr(response, "text", None)
    try:
        parsed = _parse_summary_json_response(raw_text, mode)
        if mode == "detailed":
            return _render_detailed_summary(ticket, parsed)
        return _normalize_summary_payload(parsed, mode)
    except ValueError:
        logger.error(
            "Summary response unusable mode=%s finish_reason=%s max_output_tokens=%s text_len=%s",
            mode,
            finish_reason,
            max_output_tokens,
            len(raw_text or ""),
        )
        raise


def translate_summary(summary: dict, mode: str, language: str) -> dict:
    """
    Translate a canonical English summary into another language without re-summarizing.
    The output keeps the same JSON schema as generate_summary().
    """
    if language == "en":
        return summary

    lang_name = _LANGUAGE_NAMES.get(language, language)
    response_schema = BRIEF_SUMMARY_RESPONSE_SCHEMA if mode == "brief" else DETAILED_SUMMARY_RESPONSE_SCHEMA
    source_json = json.dumps(summary, ensure_ascii=False)
    prompt = (
        f"{SUMMARY_TRANSLATION_SYSTEM_PROMPT}\n\n"
        f"Target language: {lang_name}\n\n"
        f"Canonical English summary JSON:\n{source_json}"
    )

    estimated_tokens = estimate_tokens_from_text(prompt)
    # Translation output should be close to source length; keep detailed headroom for one markdown_text field.
    max_output_tokens = 4096 if mode == "brief" else 16384
    est_output_tokens = min(max_output_tokens, max(1024, estimated_tokens))
    estimated_cost = calc_cost(estimated_tokens, est_output_tokens, call_type=CallType.UNIVERSAL_TRANSLATE)
    reserved = get_gate_reserve(CallType.UNIVERSAL_TRANSLATE, estimated_cost)
    corr = new_correlation_id()
    check_cost_gate(CallType.UNIVERSAL_TRANSLATE, reserved, fail_closed=False)
    record_estimate(
        CallType.UNIVERSAL_TRANSLATE,
        estimated_cost,
        corr,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=est_output_tokens,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    response = _model.generate_content(
        prompt,
        generation_config=GenerationConfig.from_dict(
            {
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": 0.1,
                "max_output_tokens": max_output_tokens,
                "thinking_config": {"thinking_budget": 0},
            }
        ),
        tool_config=ToolConfig(
            function_calling_config=ToolConfig.FunctionCallingConfig(
                mode=ToolConfig.FunctionCallingConfig.Mode.NONE,
            )
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        t_in = getattr(usage, "prompt_token_count", 0) or 0
        t_out = getattr(usage, "candidates_token_count", 0) or 0
        t_think = getattr(usage, "thoughts_token_count", 0) or 0
        actual_cost = record_usage(
            CallType.UNIVERSAL_TRANSLATE,
            t_in,
            t_out,
            t_think,
            correlation_id=corr,
            est_cost_usd=estimated_cost,
            expected_cost_cad=estimated_cost,
        )
        check_divergence(
            estimated_cost,
            actual_cost,
            CallType.UNIVERSAL_TRANSLATE,
            t_think,
            thinking_budget_was_zero=True,
        )

    cand = response.candidates[0] if getattr(response, "candidates", None) else None
    finish_reason = getattr(cand, "finish_reason", None) if cand else None
    raw_text = getattr(response, "text", None)
    try:
        parsed = _parse_summary_json_response(raw_text, mode)
        if mode == "detailed" and isinstance(parsed.get("markdown_text"), str):
            parsed["markdown_text"] = _restore_detailed_product_headings(parsed["markdown_text"])
        return _normalize_summary_payload(parsed, mode)
    except ValueError:
        logger.error(
            "Summary translation unusable mode=%s language=%s finish_reason=%s max_output_tokens=%s text_len=%s",
            mode,
            language,
            finish_reason,
            max_output_tokens,
            len(raw_text or ""),
        )
        raise


# ---------------------------------------------------------------------------
# Chat (streaming)
# ---------------------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are an AI assistant embedded in a Service Desk tool for Northstar Hospitality Group,
the fictional Northstar Hospitality Group. You help support engineers understand tickets,
find solutions, and retrieve knowledge base articles.

LANGUAGE RULE — MANDATORY: You MUST respond in the same language as the user's most recent message.
- User writes in Russian → you respond entirely in Russian.
- User writes in French → you respond entirely in French.
- User writes in English → you respond in English.
- Never default to English regardless of the ticket content or KB article language.

PROPER NOUNS RULE — NEVER translate or transliterate the following, regardless of response language:
- People's names (e.g. "Alex Morgan", "Casey Nguyen", "Samir Patel")
- Company and brand names (e.g. "Northstar Hospitality Group", "Frontier Kitchen", "Network Provider", "Telecom Partner")
- Product and application names (e.g. "Orbit View", "Zendesk", "Micros", "KDS")
- Technical terms, error codes, ticket fields, store/location IDs, IP addresses, URLs
- Any proper noun — always keep in its original English form.
- Exception: if the user explicitly asks you to translate a specific name or term, you may do so.

SOURCE HIERARCHY — follow this priority order strictly:
1. PRIMARY — Current ticket data (all fields, comments, timeline, assignee, status)
   → Use for: questions about this specific ticket, what happened, who did what, current state
2. SECONDARY — Store history (past tickets for the same store)
   → Use for: has this happened before, recurring issues, the store's track record
3. TERTIARY — Knowledge base articles (listed below as [ARTICLE: title])
   → Use for: how-to questions, procedures, troubleshooting steps, policy lookups
4. ANALYTICS DATA — BigQuery results injected below under "=== ANALYTICS DATA ==="
   → Use for: counts, frequencies, trends, rankings across all brands and stores
   → When present, use these numbers directly — do NOT say you cannot provide aggregate stats

ROUTING RULES:
- "What is the issue / status / problem / what happened?" → ticket data ONLY
- "What steps were taken / what was done?" → ticket data ONLY
- "Who is assigned / when was it created?" → ticket data ONLY
- "Has this happened before / what is the store history?" → store history (same store)
- "How was this type of issue resolved in the past?" → similar resolved tickets (semantic search)
- "Show me similar tickets / past cases like this" → similar resolved tickets
- "How do I fix / what is the procedure / what does the KB say?" → KB articles
- "What do you recommend next?" → combine ticket + KB + history + similar as needed
- "How many / how often / which brand most / trend across all stores?" → ANALYTICS DATA section

RESPONSE GUIDELINES:
- Be direct and professional. Engineers are technical.
- Write in flowing prose paragraphs. Use a bullet list ONLY when listing 4+ genuinely parallel items (e.g. step-by-step procedure). For short 2-3 item answers, write as a sentence or two.
- Use **bold** sparingly: only for key terms that naturally stand out. Do NOT bold entire bullet lines or list headers.
- When citing KB articles, refer to them by their article title (e.g. "According to the *BCM Troubleshooting Guide*..."). Do not use KB-N numbering.
- When referencing tickets, format them as markdown links: [#950003](https://support.example.com/agent/tickets/950003)
- When citing KB articles inline, format them as markdown links using the URL from context: [Article Title](url)
- Only reference sources that were actually provided in the context below.
- If context for a question type is absent (e.g. no KB provided), say so — do not fabricate.

After your answer, return a JSON block on a new line in this exact format:
SOURCES_JSON: {"sources": [...]}

Where sources is a list of objects you actually referenced, each with:
  {"type": "kb_article", "title": "...", "snippet": "...", "url": "..."}
  or {"type": "ticket", "id": 950003, "subject": "...", "url": "https://support.example.com/agent/tickets/950003"}

If you referenced no sources, return: SOURCES_JSON: {"sources": []}"""

CHAT_KB_RETRIEVAL_POLICY = """
KNOWLEDGE RETRIEVAL POLICY — MANDATORY:
- Never ask the user to "check the knowledge base", "read the documentation", or look up internal docs on their own. The service already runs retrieval; use only the CONTEXT block below.
- If === KNOWLEDGE BASE (Vertex) — RETRIEVAL STATUS === says zero articles, state clearly that the *automated* search returned no matches. Do not pretend to run another search, do not output "Tool Call:" or fake tool lines, and do not deflect to manual research.
- If zero results but the topic is likely in Zendesk, you may note briefly that a gap *might* be query phrasing, ranking, or indexing.
"""


def chat_stream(
    ticket: TicketDetail,
    messages: list[dict],           # [{"role": "user"|"assistant", "content": "..."}]
    kb_results: Optional[list[KBArticleSource]] = None,
    kb_status: Optional[dict] = None,
    gdrive_results: Optional[list[KBArticleSource]] = None,
    history_tickets: Optional[list] = None,
    all_tickets_results: Optional[list] = None,
    all_tickets_query: str = "",
    similar_tickets: Optional[list[dict]] = None,
    similar_tickets_query: str = "",
    similar_tickets_same_store: bool = False,
    images_by_comment: dict | None = None,
    bq_analytics_text: str = "",
) -> Iterator[str]:
    """
    Async generator yielding raw text chunks from Gemini.
    The router wraps these into SSE data: events.

    images_by_comment: screenshot OCR keyed by comment_id (see fetch_ticket_images_by_comment).
                       Screenshots are interleaved with comments in chronological order.

    Yields text chunks followed by a final "SOURCES_JSON: {...}" line.
    """
    ticket_text   = _format_ticket_for_prompt(ticket, images_by_comment)
    kb_status_text = format_kb_status_for_prompt(kb_status)
    kb_text       = _format_kb_results(kb_results or [])
    gdrive_text   = _format_gdrive_results(gdrive_results or [])
    history_text  = _format_history(history_tickets or [])
    all_tickets_text = (
        _format_all_tickets_results(all_tickets_results, all_tickets_query)
        if all_tickets_results is not None
        else ""
    )
    similar_text = (
        _format_similar_tickets_results(similar_tickets, similar_tickets_query, similar_tickets_same_store)
        if similar_tickets is not None
        else ""
    )

    # Build the full prompt — screenshots are already interleaved inside ticket_text
    context_block = (
        ticket_text
        + (kb_status_text or "")
        + kb_text
        + gdrive_text
        + history_text
        + all_tickets_text
        + similar_text
        + bq_analytics_text
    )
    system_block = f"{CHAT_SYSTEM_PROMPT}\n{CHAT_KB_RETRIEVAL_POLICY}\n\nCONTEXT:\n{context_block}"

    # Convert message history to a single conversation string
    # (Gemini Flash supports multi-turn via contents list)
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    # Prepend system context as the first user message if history is empty,
    # or inject it as a system instruction
    full_prompt_with_context = [
        {"role": "user", "parts": [{"text": system_block}]},
        {"role": "model", "parts": [{"text": "Understood. I'm ready to help with this ticket."}]},
    ] + contents

    # Pre-flight gate check (chat Phase 2 uses thinking — budget 1024 tokens).
    # Use full context length as estimate; thinking tokens will be real-billed.
    estimated_tokens = estimate_tokens_from_text(system_block)
    estimated_cost = calc_cost(estimated_tokens, estimated_tokens // 3, tokens_thinking=1024, call_type=CallType.CHAT_PHASE2)
    reserved = get_gate_reserve(CallType.CHAT_PHASE2, estimated_cost)
    corr_p2 = new_correlation_id()
    check_cost_gate(CallType.CHAT_PHASE2, reserved, fail_closed=True)
    record_estimate(
        CallType.CHAT_PHASE2,
        estimated_cost,
        corr_p2,
        est_basis="formula",
        est_input_tokens=estimated_tokens,
        est_output_tokens=estimated_tokens // 3,
        expected_cost_cad=estimated_cost,
        reserved_cost_cad=reserved,
    )

    responses = _model.generate_content(
        full_prompt_with_context,
        generation_config=GenerationConfig.from_dict({"temperature": 0.2, "max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}}),
        # Phase 2: disable all tool/function calling — tools were already resolved in Phase 1.
        # Without Mode.NONE, gemini-2.5-flash attempts code execution, producing finish_reason=15.
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
            # Skip chunks with no readable text (safety filter, finish_reason=15, etc.)
            pass

    # Record actual usage from the final chunk's usage_metadata.
    if last_chunk is not None:
        usage = getattr(last_chunk, "usage_metadata", None)
        if usage:
            t_in  = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            record_usage(
                CallType.CHAT_PHASE2, t_in, t_out, t_think,
                correlation_id=corr_p2,
                est_cost_usd=estimated_cost,
                expected_cost_cad=estimated_cost,
            )
            # No ratio divergence check for Phase 2 — thinking_budget=1024 makes variance high vs pre-estimate.
