"""
Pydantic models for Zendesk ticket data.
These represent the cleaned/enriched shape returned by the backend to the frontend.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class TicketComment(BaseModel):
    id: int
    author_name: str
    body: str
    is_internal: bool  # True when public=False in Zendesk
    created_at: datetime


class StoreContext(BaseModel):
    store_id: str  # e.g. "2001"
    org_name: str  # e.g. "Cedar Grill 2001"
    brand: str     # e.g. "Cedar Grill"
    org_id: int


class TicketDetail(BaseModel):
    """Full ticket with comments, enriched for AI analysis."""
    id: int
    subject: str
    status: str          # open, pending, solved, closed
    priority: Optional[str]   # low, normal, high, urgent
    ticket_type: Optional[str] = None
    ticket_form_id: Optional[int] = None
    ticket_form_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    sla_policy_name: Optional[str] = None
    resolution_sla_target_hours: Optional[float] = None
    resolution_sla_elapsed_hours: Optional[float] = None
    resolution_sla_breached: Optional[bool] = None
    resolution_sla_status: Optional[str] = None

    # Assignee
    assignee_name: Optional[str]

    # Custom fields (may be None if not filled)
    issue_category: Optional[str]
    symptoms: Optional[str]
    workaround: Optional[str]
    next_steps: Optional[str]
    resolution: Optional[str]
    is_escalated: bool
    next_follow_up: Optional[str]
    requester_name: Optional[str]  # First & Last Name field

    # Store and organization profile
    store: Optional[StoreContext]

    # Comments sorted by created_at ASC
    comments: list[TicketComment]


class TicketListItem(BaseModel):
    """Compact ticket representation for list views (store search, history)."""
    id: int
    subject: str
    status: str
    priority: Optional[str]
    updated_at: datetime
    assignee_name: Optional[str]
    issue_category: Optional[str]
    store_id: Optional[str]
    org_name: Optional[str]


class TicketListResponse(BaseModel):
    tickets: list[TicketListItem]
    total: int
    page: int
    per_page: int
    store_id: Optional[str] = None
    org_name: Optional[str] = None
