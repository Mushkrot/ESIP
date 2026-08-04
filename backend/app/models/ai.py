"""
Pydantic models for AI summary and chat endpoints.
"""

from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class SummaryRequest(BaseModel):
    ticket_id: int
    mode: Literal["brief", "detailed"] = "brief"
    language: str = "en"  # ISO 639-1 code; default English
    refresh: bool = False  # if True, bypass cache and regenerate


class KBArticleSource(BaseModel):
    type: Literal["kb_article"] = "kb_article"
    title: str
    snippet: str
    url: str


class TicketSource(BaseModel):
    type: Literal["ticket"] = "ticket"
    id: int
    subject: str
    url: str


Source = KBArticleSource | TicketSource


class BriefSummary(BaseModel):
    overview: str              # narrative overview of the incident
    current_status: str        # current state of the ticket
    key_actions_taken: list[str]
    kb_suggestions: list[KBArticleSource]  # up to 3


class DetailedSummary(BaseModel):
    # New format: markdown_text contains the full narrative summary.
    # When present, all other fields except kb_suggestions are ignored by the frontend.
    markdown_text: Optional[str] = None   # full markdown narrative (Variant B)
    kb_suggestions: list[KBArticleSource]  # up to 5
    # Legacy structured fields (kept for backward-compat with cached old-format data)
    participants: list[str] = []
    problem_summary: str = ""
    timeline: list[str] = []
    root_cause: Optional[str] = None
    actions_taken: list[str] = []
    final_status: str = ""
    additional_issues: list[str] = []
    open_questions: list[str] = []
    next_steps: list[str] = []


class SummaryResponse(BaseModel):
    ticket_id: int
    mode: Literal["brief", "detailed"]
    summary: BriefSummary | DetailedSummary
    generated_at: datetime
    from_cache: bool


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    ticket_id: int
    messages: list[ChatMessage]  # full conversation history
    include_kb: bool = True
    include_history: bool = True


# SSE stream events sent from backend to frontend.
# Each event is a JSON line: data: {payload}\n\n
#
#   delta event:  {"delta": "...text chunk..."}
#   done event:   {"done": true, "sources": [...]}
#   error event:  {"error": "message"}
#   keepalive:    ": keepalive"  (SSE comment, no JSON)


class ChatStreamDelta(BaseModel):
    delta: str


class ChatStreamDone(BaseModel):
    done: bool = True
    sources: list[Source] = []


class ChatStreamError(BaseModel):
    error: str
