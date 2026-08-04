"""Models for per-ticket customer risk assessment."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


CustomerRiskSeverity = Literal["green", "yellow", "orange", "red"]
CustomerTone = Literal["calm", "concerned", "frustrated", "escalated"]


class CustomerRiskMetrics(BaseModel):
    public_customer_comments: int = 0
    public_agent_replies: int = 0
    customer_followups_after_last_agent: int = 0
    last_public_author: Literal["customer", "agent", "unknown", "none"] = "none"
    hours_since_last_public_agent_reply: float | None = None
    hours_since_last_public_customer_comment: float | None = None
    active_ticket_age_hours: float | None = None
    is_active: bool = False
    is_escalated: bool = False
    vendor_dispatched: bool = False
    vendor_escalation: bool = False
    sla_policy_name: str | None = None
    resolution_sla_target_hours: float | None = None
    resolution_sla_elapsed_hours: float | None = None
    resolution_sla_hours_remaining: float | None = None
    resolution_sla_progress_pct: float | None = None
    resolution_sla_breached: bool | None = None
    resolution_sla_status: str | None = None


class CustomerRiskResponse(BaseModel):
    ticket_id: int
    severity: CustomerRiskSeverity
    score: int
    label: str
    tone: CustomerTone
    summary: str
    reasons: list[str]
    next_action: str
    metrics: CustomerRiskMetrics
    generated_at: datetime
    from_cache: bool = False
    prompt_version: str
    score_version: str
    comment_fingerprint: str
