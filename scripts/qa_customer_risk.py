#!/usr/bin/env python3
"""QA checks for deterministic Customer Risk assessment composition."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.models.ticket import StoreContext, TicketComment, TicketDetail  # noqa: E402
from app.services import ticket_customer_risk  # noqa: E402


def _ticket(
    status: str,
    comments: list[TicketComment],
    *,
    sla_target: float | None = None,
    sla_elapsed: float | None = None,
    sla_breached: bool | None = None,
    sla_status: str | None = None,
) -> TicketDetail:
    now = datetime.now(timezone.utc)
    return TicketDetail(
        id=950028,
        subject="Move 5000 issues",
        status=status,
        priority="low" if sla_target is not None else None,
        ticket_type="incident" if sla_target is not None else None,
        ticket_form_name="Report an incident." if sla_target is not None else None,
        created_at=now - timedelta(days=5),
        updated_at=now - timedelta(hours=1),
        sla_policy_name="Incident resolution by priority" if sla_target is not None else None,
        resolution_sla_target_hours=sla_target,
        resolution_sla_elapsed_hours=sla_elapsed,
        resolution_sla_breached=sla_breached,
        resolution_sla_status=sla_status,
        assignee_name="Engineer",
        issue_category=None,
        symptoms=None,
        workaround=None,
        next_steps=None,
        resolution=None,
        is_escalated=False,
        next_follow_up=None,
        requester_name=None,
        store=StoreContext(store_id="2002", org_name="Lakeside Tavern 2004", brand="Lakeside Tavern", org_id=1),
        comments=comments,
    )


def _comment(comment_id: int, hours_ago: int, body: str, internal: bool = False) -> TicketComment:
    return TicketComment(
        id=comment_id,
        author_name=f"User {comment_id}",
        body=body,
        is_internal=internal,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def _meta(*pairs: tuple[int, str, bool]) -> dict:
    return {
        "ticket": {"escalated": False, "vendor_dispatched": False, "vendor_escalation": False},
        "comments": {
            cid: {"author_role": role, "author_is_agent": is_agent, "via_channel": "email"}
            for cid, role, is_agent in pairs
        },
    }


def check_high_risk_guard() -> None:
    comments = [
        _comment(1, 72, "We are checking this."),
        _comment(2, 36, "Are we any closer to a solution?"),
        _comment(3, 30, "This is taking too long."),
        _comment(4, 26, "We still need help and have no update."),
    ]
    risk = ticket_customer_risk.build_customer_risk_from_precomputed(
        _ticket("open", comments),
        _meta((1, "agent", True), (2, "end-user", False), (3, "end-user", False), (4, "end-user", False)),
        {
            "tone": "frustrated",
            "label": "red",
            "summary": "The customer is repeatedly asking for progress.",
            "reasons": ["Customer says the issue is taking too long."],
            "next_action": "Continue internal investigation.",
        },
    )
    assert risk.severity == "red", risk
    assert risk.label == "High customer frustration risk", risk.label
    assert risk.score >= 80, risk
    assert risk.metrics.customer_followups_after_last_agent == 3, risk.metrics
    assert "public update" in risk.next_action.lower(), risk.next_action


def check_green_ticket() -> None:
    comments = [_comment(10, 12, "We resolved this for the store.")]
    risk = ticket_customer_risk.build_customer_risk_from_precomputed(
        _ticket("solved", comments),
        _meta((10, "agent", True)),
        {
            "tone": "calm",
            "label": "Normal customer risk",
            "summary": "There are no customer pressure signals.",
            "reasons": [],
            "next_action": "No urgent customer action is needed.",
        },
    )
    assert risk.severity == "green", risk
    assert risk.score < 20, risk
    assert risk.metrics.public_customer_comments == 0, risk.metrics


def check_sla_breach_adds_operational_urgency_without_changing_tone() -> None:
    comments = [_comment(20, 6, "We are still investigating.")]
    risk = ticket_customer_risk.build_customer_risk_from_precomputed(
        _ticket(
            "open",
            comments,
            sla_target=48.0,
            sla_elapsed=61.0,
            sla_breached=True,
            sla_status="open_breached",
        ),
        _meta((20, "agent", True)),
        {
            "tone": "calm",
            "label": "Normal customer risk",
            "summary": "The customer has not pushed for updates.",
            "reasons": [],
            "next_action": "Continue internal investigation.",
        },
    )
    assert risk.severity == "orange", risk
    assert risk.tone == "calm", risk
    assert risk.score >= 45, risk
    assert risk.label == "Elevated customer risk", risk.label
    assert any("SLA breached" in reason for reason in risk.reasons), risk.reasons
    assert "public update" in risk.next_action.lower(), risk.next_action
    assert "blocker" in risk.next_action.lower(), risk.next_action


def check_sla_breach_plus_customer_pressure_becomes_red() -> None:
    comments = [
        _comment(30, 30, "We are checking this."),
        _comment(31, 4, "Any update? We still cannot use the workstation."),
    ]
    risk = ticket_customer_risk.build_customer_risk_from_precomputed(
        _ticket(
            "open",
            comments,
            sla_target=48.0,
            sla_elapsed=54.0,
            sla_breached=True,
            sla_status="open_breached",
        ),
        _meta((30, "agent", True), (31, "end-user", False)),
        {
            "tone": "concerned",
            "label": "Watch customer risk",
            "summary": "The customer is waiting for progress.",
            "reasons": [],
            "next_action": "Continue internal investigation.",
        },
    )
    assert risk.severity == "red", risk
    assert risk.score >= 70, risk
    assert risk.tone == "concerned", risk
    assert any("SLA breached" in reason for reason in risk.reasons), risk.reasons
    assert risk.next_action.startswith("Acknowledge the SLA breach"), risk.next_action


def check_closed_sla_breach_does_not_create_live_customer_risk() -> None:
    comments = [_comment(40, 12, "This has been resolved.")]
    risk = ticket_customer_risk.build_customer_risk_from_precomputed(
        _ticket(
            "solved",
            comments,
            sla_target=48.0,
            sla_elapsed=61.0,
            sla_breached=True,
            sla_status="breached",
        ),
        _meta((40, "agent", True)),
        {
            "tone": "calm",
            "label": "Normal customer risk",
            "summary": "There are no current customer pressure signals.",
            "reasons": [],
            "next_action": "No urgent customer action is needed.",
        },
    )
    assert risk.severity == "green", risk
    assert risk.score < 20, risk
    assert not any("SLA breached" in reason for reason in risk.reasons), risk.reasons


def main() -> int:
    checks = [
        check_high_risk_guard,
        check_green_ticket,
        check_sla_breach_adds_operational_urgency_without_changing_tone,
        check_sla_breach_plus_customer_pressure_becomes_red,
        check_closed_sla_breach_does_not_create_live_customer_risk,
    ]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print(f"Customer Risk QA passed: {len(checks)}/{len(checks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
