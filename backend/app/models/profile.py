"""
Pydantic models for user profile stored in Firestore.
"""

from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field

from app.models.ticket import TicketListItem


class TabInfo(BaseModel):
    """Minimal ticket metadata stored per open tab in the user's profile."""
    id: int
    subject: str
    status: str
    priority: Optional[str]


class UserProfile(BaseModel):
    uid: str                    # Firebase UID
    email: str
    name: str
    display_name: Optional[str] = None
    picture: Optional[str]
    theme: Literal["light", "dark"] = "light"
    search_mode: Literal["ticket", "store"] = "ticket"
    open_tabs: list[TabInfo] = []   # max 8; each entry has id, subject, status, priority
    search_history: list[dict] = []  # last 10 searches
    sidebar_sections: dict[str, bool] = Field(default_factory=lambda: {
        "tools": True,
        "admin": True,
        "my_unsolved": True,
        "open_in_esip": False,
    })


class ProfileUpdateRequest(BaseModel):
    theme: Optional[Literal["light", "dark"]] = None
    search_mode: Optional[Literal["ticket", "store"]] = None
    sidebar_sections: Optional[dict[str, bool]] = None


class AddTabRequest(BaseModel):
    ticket_id: int
    subject: str
    status: str
    priority: Optional[str] = None


class AddSearchHistoryRequest(BaseModel):
    query: str
    type: Literal["ticket", "store"]


class MyUnsolvedTicketsResponse(BaseModel):
    items: list[TicketListItem]
    count: int
    source: Literal["zendesk_live_cache", "bq_snapshot"]
    fetched_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    refresh_status: Literal["fresh", "queued", "refreshing", "failed"]
    warning: Optional[str] = None


class AuthUserResponse(BaseModel):
    """Returned by /api/auth/me after successful token validation."""
    email: str
    name: str
    picture: Optional[str]
    uid: str
    is_admin: bool = False  # kept for backward compat (True when role is admin or super_admin)
    role: str = "user"      # "user" | "admin" | "super_admin"
    actual_role: str = "user"
    effective_role: str = "user"
    role_preview: bool = False
    can_preview_roles: bool = False
