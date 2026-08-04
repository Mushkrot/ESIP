"""Authenticated product telemetry endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.dependencies import AnyUser
from app.services import impact_telemetry

router = APIRouter()


class ImpactEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    surface: str = Field(default="web", max_length=80)
    workflow: str | None = Field(default=None, max_length=80)
    ticket_id: int | None = None
    target_ticket_id: int | None = None
    conversation_id: str | None = Field(default=None, max_length=180)
    turn_id: str | None = Field(default=None, max_length=180)
    trace_id: str | None = Field(default=None, max_length=80)
    object_type: str | None = Field(default=None, max_length=80)
    object_id: str | None = Field(default=None, max_length=240)
    object_label: str | None = Field(default=None, max_length=500)
    target_url: str | None = Field(default=None, max_length=1200)
    metadata: dict[str, Any] | None = None


@router.post("/impact-event")
def record_impact_event(req: ImpactEventRequest, user: AnyUser) -> dict[str, Any]:
    """Record a non-invasive ESIP product interaction event."""
    accepted = impact_telemetry.save_event(event=req.model_dump(), user=user)
    return {"accepted": accepted}
