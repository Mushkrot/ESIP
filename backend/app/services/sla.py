"""Zendesk SLA helpers for ESIP ticket analytics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


INCIDENT_RESOLUTION_TARGET_HOURS: dict[str, float] = {
    "urgent": 8.0,
    "high": 12.0,
    "normal": 24.0,
    "medium": 24.0,
    "low": 48.0,
}

INCIDENT_SLA_POLICY_NAME = "Incident resolution by priority"


def normalize_priority(value: Any) -> str | None:
    priority = str(value or "").strip().lower()
    return priority or None


def priority_display(value: Any) -> str:
    priority = normalize_priority(value)
    if not priority:
        return "Unknown"
    if priority == "normal":
        return "Medium"
    return priority.capitalize()


def parse_zendesk_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_incident_ticket(ticket: dict[str, Any], ticket_form_name: str | None = None) -> bool:
    ticket_type = str(ticket.get("type") or "").strip().lower()
    if ticket_type == "incident":
        return True
    form_name = str(ticket_form_name or "").strip().lower()
    return "incident" in form_name


def incident_resolution_target_hours(priority: Any, *, is_incident: bool) -> float | None:
    if not is_incident:
        return None
    normalized = normalize_priority(priority)
    if not normalized:
        return None
    return INCIDENT_RESOLUTION_TARGET_HOURS.get(normalized)


def build_sla_fields(
    ticket: dict[str, Any],
    *,
    ticket_form_name: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build derived SLA fields for a Zendesk ticket export row."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    priority = normalize_priority(ticket.get("priority"))
    ticket_type = str(ticket.get("type") or "").strip().lower() or None
    is_incident = is_incident_ticket(ticket, ticket_form_name)
    target_hours = incident_resolution_target_hours(priority, is_incident=is_incident)

    created_at = parse_zendesk_ts(ticket.get("created_at"))
    updated_at = parse_zendesk_ts(ticket.get("updated_at"))
    status = str(ticket.get("status") or "").strip().lower()
    is_closed = status in {"solved", "closed"}
    end_at = updated_at if is_closed else now

    elapsed_hours = None
    if created_at and end_at:
        elapsed_hours = (end_at - created_at).total_seconds() / 3600

    breached = None
    sla_status = "not_applicable"
    if target_hours is not None and elapsed_hours is not None:
        breached = elapsed_hours > target_hours
        if is_closed:
            sla_status = "breached" if breached else "met"
        else:
            sla_status = "open_breached" if breached else "open_within_target"
    elif target_hours is not None:
        sla_status = "unknown"

    return {
        "priority": priority,
        "ticket_type": ticket_type,
        "ticket_form_id": ticket.get("ticket_form_id"),
        "ticket_form_name": ticket_form_name,
        "sla_policy_name": INCIDENT_SLA_POLICY_NAME if target_hours is not None else None,
        "resolution_sla_target_hours": target_hours,
        "resolution_sla_elapsed_hours": elapsed_hours,
        "resolution_sla_breached": breached,
        "resolution_sla_status": sla_status,
    }
