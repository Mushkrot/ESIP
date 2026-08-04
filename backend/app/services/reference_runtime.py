"""Credential-free state and fixtures for the ESIP reference runtime."""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models.ai import KBArticleSource
from app.models.customer_risk import CustomerRiskMetrics, CustomerRiskResponse
from app.models.profile import UserProfile
from app.models.ticket import StoreContext, TicketComment, TicketDetail, TicketListItem
from app.reference_data.fixtures import (
    PLATFORM_ADMIN_EMAIL,
    STORE_BY_ID,
    SUPPORT_ADMIN_EMAIL,
    SUPPORT_TICKETS,
    TICKET_BY_ID,
    USERS,
    USER_BY_EMAIL,
)


REFERENCE_SESSION_TOKEN = "esip-reference-session"
REFERENCE_UID = "reference-platform-admin"

_lock = threading.RLock()
_profiles: dict[str, dict[str, Any]] = {}
_feedback: dict[str, list[dict[str, Any]]] = {}
_dismissed_announcements: set[str] = set()
_impact_events: list[dict[str, Any]] = []
_maintenance_enabled = False
_notification_recipients = [PLATFORM_ADMIN_EMAIL, "alerts@northstar.example.com"]
_admin_users: dict[str, dict[str, Any]] = {
    user.email: {
        "email": user.email,
        "display_name": user.name,
        "active": True,
        "role": user.role,
        "universal_chat_logging_disabled": user.key == "vendor_liaison",
        "esip_access_granted_at": "2026-05-26",
    }
    for user in USERS
}
_change_candidates: dict[str, dict[str, Any]] = {
    "reference-change-evidence": {
        "id": "reference-change-evidence",
        "title": "Evidence workspace improvements",
        "summary": "Ticket context now keeps related cases, knowledge references, and follow-up actions in one workspace.",
        "change_type": "workflow",
        "impact": "high",
        "source": "release_review",
        "related_files": [],
        "audience_roles": ["user", "admin"],
        "surfaces": ["web"],
        "severity": "info",
        "status": "published",
        "created_at": "2026-07-27T14:00:00+00:00",
        "published_as_announcement_id": "reference-release-notes",
    },
    "reference-change-gap-review": {
        "id": "reference-change-gap-review",
        "title": "Knowledge gap review queue",
        "summary": "Evidence-backed documentation gaps can now be reviewed and converted into article drafts.",
        "change_type": "workflow",
        "impact": "medium",
        "source": "release_review",
        "related_files": [],
        "audience_roles": ["admin"],
        "surfaces": ["web"],
        "severity": "info",
        "status": "draft",
        "created_at": "2026-07-31T16:30:00+00:00",
        "published_as_announcement_id": None,
    },
}
_product_announcements: dict[str, dict[str, Any]] = {
    "reference-release-notes": {
        "id": "reference-release-notes",
        "title": "Evidence workspace improvements",
        "body_markdown": "Ticket context now keeps related cases, knowledge references, and follow-up actions in one workspace.",
        "severity": "info",
        "status": "published",
        "audience_roles": ["user", "admin"],
        "surfaces": ["web"],
        "published_at": "2026-07-28T13:00:00+00:00",
        "published_by": PLATFORM_ADMIN_EMAIL,
        "unpublished_at": None,
        "unpublished_by": None,
        "republished_at": None,
        "republished_by": None,
        "edited_at": None,
        "edited_by": None,
    }
}
_dispatcher_config: dict[str, Any] = {
    "mode": "shadow",
    "enabled": True,
    "answer_affecting_allowed": False,
    "planner_daily_cap_cad": 2.0,
    "max_planner_calls_per_turn": 1,
    "max_tool_actions": 4,
    "max_explicit_ticket_ids": 12,
    "max_action_rows": 50,
    "max_output_tokens": 1024,
    "eligible_task_classes": ["ticket_investigation", "knowledge_lookup", "analytics_question"],
    "assistive_task_classes": ["ticket_investigation", "knowledge_lookup"],
    "shadow_task_classes": ["analytics_question"],
    "allowed_modes": ["disabled", "shadow", "assistive"],
    "blocked_modes": ["autonomous"],
    "allowed_task_classes": ["ticket_investigation", "knowledge_lookup", "analytics_question"],
    "allowed_assistive_task_classes": ["ticket_investigation", "knowledge_lookup"],
    "allowed_shadow_task_classes": ["analytics_question"],
    "primary_eligible_task_classes": ["ticket_investigation", "knowledge_lookup", "analytics_question"],
    "primary_answer_affecting_task_classes": ["ticket_investigation", "knowledge_lookup"],
    "updated_at": "2026-07-31T18:00:00+00:00",
    "updated_by": PLATFORM_ADMIN_EMAIL,
    "emergency_disabled": False,
    "emergency_disabled_at": None,
    "emergency_disabled_by": None,
}
_capture_config: dict[str, Any] = {
    "enabled": False,
    "allowed_emails": [],
    "max_allowed_emails": 5,
    "updated_at": "2026-07-31T18:00:00+00:00",
    "updated_by": PLATFORM_ADMIN_EMAIL,
}
_kpi_delivery_config: dict[str, Any] = {
    "enabled": True,
    "timezone": "America/New_York",
    "recipients": [SUPPORT_ADMIN_EMAIL],
    "cc_recipients": ["service.analytics@northstar.example.com"],
    "slots": [{"id": "slot-1030", "time": "10:30", "enabled": True, "label": "Daily operations brief"}],
    "last_delivery": {"status": "sent", "report_date": "2026-08-02", "sent_at": "2026-08-03T14:31:00+00:00"},
    "last_test": None,
    "transport": "reference",
    "safe_test_recipients": [PLATFORM_ADMIN_EMAIL, SUPPORT_ADMIN_EMAIL],
    "scheduler": {"enabled": True, "state": "ENABLED", "schedule": "30 10 * * *", "schedule_time": "10:30", "time_zone": "America/New_York"},
}
_INITIAL_NOTIFICATION_RECIPIENTS = list(_notification_recipients)
_INITIAL_ADMIN_USERS = deepcopy(_admin_users)
_INITIAL_CHANGE_CANDIDATES = deepcopy(_change_candidates)
_INITIAL_PRODUCT_ANNOUNCEMENTS = deepcopy(_product_announcements)
_INITIAL_DISPATCHER_CONFIG = deepcopy(_dispatcher_config)
_INITIAL_CAPTURE_CONFIG = deepcopy(_capture_config)
_INITIAL_KPI_DELIVERY_CONFIG = deepcopy(_kpi_delivery_config)
_cost_budget = {
    "daily_limit_usd": 10.0,
    "daily_limit_cad": 10.0,
    "monthly_limit_usd": 100.0,
    "monthly_limit_cad": 100.0,
    "soft_cap_pct": 70.0,
    "hard_cap_pct": 90.0,
    "divergence_threshold": 2.0,
}
_sources: dict[str, dict[str, Any]] = {
    "zendesk_operations_articles": {"display_name": "Operations knowledge", "type": "vertex_ai", "enabled": True, "sync_enabled": True, "doc_count": 184, "last_sync": "2026-07-31T10:20:00+00:00", "exclude_path_prefixes": []},
    "zendesk_support_articles": {"display_name": "Support knowledge", "type": "vertex_ai", "enabled": True, "sync_enabled": True, "doc_count": 326, "last_sync": "2026-07-31T10:20:00+00:00", "exclude_path_prefixes": []},
    "reference_support_docs": {"display_name": "Reference procedures", "type": "vertex_ai", "enabled": True, "sync_enabled": False, "doc_count": 68, "last_sync": "2026-07-30T15:10:00+00:00", "exclude_path_prefixes": ["archive/"]},
    "gdrive_operations_job_aids": {"display_name": "Operations job aids", "type": "vertex_ai", "enabled": True, "sync_enabled": True, "doc_count": 142, "last_sync": "2026-07-31T11:05:00+00:00", "exclude_path_prefixes": ["drafts/"]},
    "gdrive_support_repository": {"display_name": "Support repository", "type": "vertex_ai", "enabled": True, "sync_enabled": True, "doc_count": 211, "last_sync": "2026-07-31T11:05:00+00:00", "exclude_path_prefixes": []},
    "tickets_zendesk": {"display_name": "Support ticket API", "type": "zendesk_api", "enabled": True, "sync_enabled": True, "doc_count": 240, "last_sync": "2026-07-31T12:00:00+00:00"},
    "tickets_bq": {"display_name": "Ticket analytics warehouse", "type": "bigquery", "enabled": True, "sync_enabled": True, "doc_count": 240, "last_sync": "2026-07-31T12:03:00+00:00"},
}
_schedulers: dict[str, dict[str, Any]] = {
    "esip-bq-sync": {"job_id": "esip-bq-sync", "display_name": "Ticket analytics refresh", "purpose": "Refresh ticket analytics and retrieval indexes", "state": "ENABLED", "enabled": True, "schedule": "0 6 * * *", "schedule_time": "06:00", "time_zone": "America/New_York", "last_attempt_time": "2026-07-31T10:00:00+00:00", "active_run_running": False, "active_run_progress": None, "active_run_heartbeat_age_sec": None},
    "esip-drive-sync": {"job_id": "esip-drive-sync", "display_name": "Knowledge source refresh", "purpose": "Refresh changed knowledge documents", "state": "ENABLED", "enabled": True, "schedule": "30 6 * * *", "schedule_time": "06:30", "time_zone": "America/New_York", "last_attempt_time": "2026-07-31T10:30:00+00:00", "active_run_running": False, "active_run_progress": None, "active_run_heartbeat_age_sec": None},
}
_gap_runs: dict[str, dict[str, Any]] = {}

_BASE_TIME = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
_ISSUE_CATEGORIES = (
    "Payment Processing",
    "Kitchen Systems",
    "Printing",
    "Network",
    "Menu Management",
    "Access Management",
)
_KNOWLEDGE_ARTICLES = (
    KBArticleSource(
        title="Payment terminal authorization recovery",
        snippet="Validate network reachability, confirm the terminal lane mapping, and retry a controlled authorization.",
        url="https://docs.example.com/payment-terminal-recovery",
    ),
    KBArticleSource(
        title="Store network failover checklist",
        snippet="A bounded checklist for circuit health, gateway status, DNS, and escalation evidence.",
        url="https://docs.example.com/store-network-failover",
    ),
    KBArticleSource(
        title="Kitchen display restart and queue validation",
        snippet="Restore a kitchen display without losing active order-routing state.",
        url="https://docs.example.com/kitchen-display-recovery",
    ),
    KBArticleSource(
        title="Receipt printer queue diagnostics",
        snippet="Identify duplicate jobs, clear a stalled queue, and verify the assigned printer route.",
        url="https://docs.example.com/printer-queue-diagnostics",
    ),
)


def _ticket_index(ticket_id: int) -> int:
    return ticket_id - SUPPORT_TICKETS[0].ticket_id


def ticket_list_item(ticket_id: int) -> TicketListItem:
    ticket = TICKET_BY_ID[ticket_id]
    store = STORE_BY_ID[ticket.store_id]
    index = _ticket_index(ticket_id)
    created_at = _BASE_TIME + timedelta(hours=index * 5)
    owner = USER_BY_EMAIL[ticket.owner_email]
    return TicketListItem(
        id=ticket.ticket_id,
        subject=ticket.subject,
        status=ticket.status,
        priority=("normal", "high", "urgent", "low")[index % 4],
        updated_at=created_at + timedelta(hours=3 + index % 7),
        assignee_name=owner.name,
        issue_category=_ISSUE_CATEGORIES[index % len(_ISSUE_CATEGORIES)],
        store_id=store.store_id,
        org_name=store.name,
    )


def ticket_detail(ticket_id: int) -> TicketDetail | None:
    ticket = TICKET_BY_ID.get(ticket_id)
    if ticket is None:
        return None
    item = ticket_list_item(ticket_id)
    store = STORE_BY_ID[ticket.store_id]
    index = _ticket_index(ticket_id)
    created_at = item.updated_at - timedelta(hours=3 + index % 7)
    is_active = ticket.status in {"open", "pending", "hold"}
    comments = [
        TicketComment(
            id=ticket_id * 10 + 1,
            author_name=f"{store.name} Manager",
            body=f"The store reported: {ticket.subject.lower()}. The issue affects one primary workstation.",
            is_internal=False,
            created_at=created_at,
        ),
        TicketComment(
            id=ticket_id * 10 + 2,
            author_name=item.assignee_name or "Support Engineer",
            body="Validated the store context, collected device state, and followed the bounded recovery checklist.",
            is_internal=True,
            created_at=created_at + timedelta(hours=1),
        ),
        TicketComment(
            id=ticket_id * 10 + 3,
            author_name=f"{store.name} Manager",
            body="The affected workflow is stable after validation. Monitoring continues through the next service window.",
            is_internal=False,
            created_at=item.updated_at,
        ),
    ]
    return TicketDetail(
        id=ticket.ticket_id,
        subject=ticket.subject,
        status=ticket.status,
        priority=item.priority,
        ticket_type="incident",
        ticket_form_id=1001,
        ticket_form_name="Store Technology Support",
        created_at=created_at,
        updated_at=item.updated_at,
        sla_policy_name="Store Operations Standard",
        resolution_sla_target_hours=8.0,
        resolution_sla_elapsed_hours=float(3 + index % 7),
        resolution_sla_breached=False,
        resolution_sla_status="active" if is_active else "met",
        assignee_name=item.assignee_name,
        issue_category=item.issue_category,
        symptoms="Intermittent service interruption reported by the store team.",
        workaround="Use the secondary station while validation is in progress." if is_active else None,
        next_steps="Confirm stability and attach the final validation notes." if is_active else None,
        resolution=None if is_active else "Service restored after configuration validation and a controlled restart.",
        is_escalated=index % 9 == 0,
        next_follow_up="Next business day" if is_active else None,
        requester_name=f"{store.name} Manager",
        store=StoreContext(
            store_id=store.store_id,
            org_name=store.name,
            brand=store.brand,
            org_id=store.organization_id,
        ),
        comments=comments,
    )


def tickets_by_store(store_id: str, page: int, per_page: int) -> tuple[list[TicketListItem], int, str | None]:
    store = STORE_BY_ID.get(store_id)
    if store is None:
        return [], 0, None
    items = [ticket_list_item(ticket.ticket_id) for ticket in SUPPORT_TICKETS if ticket.store_id == store_id]
    start = (page - 1) * per_page
    return items[start:start + per_page], len(items), store.name


def ticket_history(ticket_id: int, limit: int = 20) -> list[TicketListItem]:
    ticket = TICKET_BY_ID.get(ticket_id)
    if ticket is None:
        return []
    return [
        ticket_list_item(item.ticket_id)
        for item in SUPPORT_TICKETS
        if item.store_id == ticket.store_id and item.ticket_id != ticket_id
    ][:limit]


def similar_tickets(ticket_id: int, limit: int = 5) -> list[dict[str, Any]]:
    source = TICKET_BY_ID.get(ticket_id)
    if source is None:
        return []
    candidates = [item for item in SUPPORT_TICKETS if item.ticket_id != ticket_id]
    candidates.sort(key=lambda item: (item.subject != source.subject, item.store_id == source.store_id, item.ticket_id))
    results: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        row = ticket_list_item(candidate.ticket_id).model_dump(mode="json")
        row["ticket_id"] = candidate.ticket_id
        row["resolution"] = "Recovered with the documented validation sequence."
        results.append(row)
    return results


def my_unsolved_tickets(limit: int) -> dict[str, Any]:
    items = [
        ticket_list_item(ticket.ticket_id)
        for ticket in SUPPORT_TICKETS
        if ticket.status in {"open", "pending"}
    ][:limit]
    return {
        "items": items,
        "count": len(items),
        "source": "bq_snapshot",
        "fetched_at": _BASE_TIME,
        "expires_at": _BASE_TIME + timedelta(hours=1),
        "refresh_status": "fresh",
        "warning": None,
    }


def knowledge_suggestions(query: str, page_size: int = 5) -> list[KBArticleSource]:
    words = {word.lower() for word in query.split() if len(word) > 3}
    ranked = sorted(
        _KNOWLEDGE_ARTICLES,
        key=lambda article: -sum(word in f"{article.title} {article.snippet}".lower() for word in words),
    )
    return list(ranked[:page_size])


def chat_response(question: str, ticket_id: int | None = None) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Build a deterministic answer with linked reference evidence."""
    normalized = question.strip().lower()
    articles = knowledge_suggestions(question, 3)
    sources = [article.model_dump() for article in articles]

    if ticket_id is not None:
        ticket = ticket_detail(ticket_id)
        if ticket is None:
            return f"Ticket #{ticket_id} is not present in the reference dataset.", [], []
        store_label = ticket.store.org_name if ticket.store else "the selected store"
        answer = (
            f"### Ticket #{ticket.id}\n\n"
            f"**{ticket.subject}** is currently **{ticket.status}** for **{store_label}**. "
            "The evidence shows a bounded recovery workflow: confirm the affected device, validate connectivity and configuration, "
            "then record the post-change check before closing the case.\n\n"
            f"**Next action:** {ticket.next_steps or 'Retain the resolution evidence and monitor for recurrence.'}"
        )
        related = similar_tickets(ticket.id, 3)
        for row in related:
            store = STORE_BY_ID[str(row["store_id"])]
            row.update({"brand": store.brand, "solved_at": row["updated_at"]})
        return answer, sources, related

    if any(term in normalized for term in ("how many", "most", "trend", "quarter", "last month")):
        network = [ticket for ticket in SUPPORT_TICKETS if "network" in ticket.subject.lower()]
        answer = (
            f"The reference dataset contains **{len(network)} network-failover tickets** across "
            f"**{len({ticket.store_id for ticket in network})} stores**. The repeated pattern is loss of the primary circuit "
            "without a clean transition to the backup path.\n\n"
            "For an operational review, group these cases by brand and compare active versus solved status before choosing a follow-up cohort."
        )
        cards = []
        for ticket in network[:5]:
            row = ticket_list_item(ticket.ticket_id).model_dump(mode="json")
            store = STORE_BY_ID[ticket.store_id]
            row.update({
                "brand": store.brand,
                "resolution": "Validated gateway state and restored the documented failover path.",
                "solved_at": row["updated_at"] if ticket.status == "solved" else None,
            })
            cards.append(row)
        return answer, sources, cards

    if any(term in normalized for term in ("procedure", "guide", "reset", "checklist", "summarize")):
        top = articles[0]
        answer = (
            f"### {top.title}\n\n"
            "1. Confirm the store and affected device before changing state.\n"
            "2. Capture connectivity, configuration, and queue evidence.\n"
            "3. Apply one controlled recovery action.\n"
            "4. Validate the business workflow from end to end.\n"
            "5. Record the outcome and escalation threshold in the ticket.\n\n"
            "This sequence keeps the troubleshooting path reproducible and limits unnecessary changes."
        )
        return answer, sources, []

    resolved = [ticket for ticket in SUPPORT_TICKETS if ticket.status == "solved"][:5]
    cards = []
    for ticket in resolved:
        row = ticket_list_item(ticket.ticket_id).model_dump(mode="json")
        store = STORE_BY_ID[ticket.store_id]
        row.update({
            "brand": store.brand,
            "resolution": "Recovered with the documented validation sequence.",
            "solved_at": row["updated_at"],
        })
        cards.append(row)
    answer = (
        "I found a set of resolved reference cases with reusable evidence. Start with the cases below, compare the store and issue context, "
        "and apply only the recovery steps supported by the selected ticket."
    )
    return answer, sources, cards


def summary(ticket_id: int, mode: str) -> dict[str, Any] | None:
    ticket = ticket_detail(ticket_id)
    if ticket is None:
        return None
    articles = knowledge_suggestions(ticket.subject, 3 if mode == "brief" else 4)
    if mode == "brief":
        return {
            "overview": f"{ticket.subject} at store {ticket.store.store_id if ticket.store else 'unknown'}. Support validated the affected workflow and contained the impact.",
            "current_status": f"Ticket is {ticket.status}; the next validation step is recorded in the case.",
            "key_actions_taken": [
                "Confirmed the affected device and store context.",
                "Applied the bounded recovery checklist.",
                "Recorded validation evidence and the next follow-up.",
            ],
            "kb_suggestions": [article.model_dump() for article in articles],
        }
    return {
        "markdown_text": (
            f"## Situation\n{ticket.subject} was reported for **{ticket.store.org_name if ticket.store else 'the store'}**.\n\n"
            "## Investigation\nSupport confirmed scope, reviewed the device state, and compared the symptoms with known cases.\n\n"
            "## Actions and outcome\nThe team followed a controlled recovery sequence, validated the primary workflow, and documented the remaining follow-up.\n\n"
            f"## Current state\nThe ticket is **{ticket.status}** with no unresolved data-quality warning."
        ),
        "kb_suggestions": [article.model_dump() for article in articles],
    }


def customer_risk(ticket_id: int) -> CustomerRiskResponse | None:
    ticket = ticket_detail(ticket_id)
    if ticket is None:
        return None
    active = ticket.status in {"open", "pending", "hold"}
    severity = "orange" if ticket.is_escalated else ("yellow" if active else "green")
    score = {"green": 18, "yellow": 46, "orange": 72}[severity]
    return CustomerRiskResponse(
        ticket_id=ticket_id,
        severity=severity,
        score=score,
        label={"green": "Stable", "yellow": "Needs follow-up", "orange": "Escalation risk"}[severity],
        tone="escalated" if ticket.is_escalated else ("concerned" if active else "calm"),
        summary="The latest response is constructive, but the active workflow should be confirmed within the stated follow-up window." if active else "The resolution is confirmed and the interaction is stable.",
        reasons=["Active store workflow", "Follow-up window recorded"] if active else ["Resolution confirmed"],
        next_action=ticket.next_steps or "No immediate action; retain the resolution evidence.",
        metrics=CustomerRiskMetrics(
            public_customer_comments=2,
            public_agent_replies=1,
            customer_followups_after_last_agent=1,
            last_public_author="customer",
            hours_since_last_public_agent_reply=2.0,
            hours_since_last_public_customer_comment=0.5,
            active_ticket_age_hours=6.0,
            is_active=active,
            is_escalated=ticket.is_escalated,
            vendor_dispatched=False,
            vendor_escalation=False,
            sla_policy_name=ticket.sla_policy_name,
            resolution_sla_target_hours=ticket.resolution_sla_target_hours,
            resolution_sla_elapsed_hours=ticket.resolution_sla_elapsed_hours,
            resolution_sla_hours_remaining=2.0 if active else 0.0,
            resolution_sla_progress_pct=75.0 if active else 100.0,
            resolution_sla_breached=False,
            resolution_sla_status=ticket.resolution_sla_status,
        ),
        generated_at=_BASE_TIME + timedelta(days=30),
        from_cache=True,
        prompt_version="reference-v1",
        score_version="reference-v1",
        comment_fingerprint=f"reference-{ticket_id}",
    )


def announcement_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for announcement in _product_announcements.values():
        if announcement.get("status") != "published":
            continue
        item = deepcopy(announcement)
        item["active_from"] = item["published_at"]
        item["read_state"] = None
        if item["id"] in _dismissed_announcements:
            item["read_state"] = {"dismissed_at": "2026-08-01T13:00:00+00:00"}
        items.append(item)
    return sorted(items, key=lambda item: item["published_at"], reverse=True)


def dismiss_announcement(announcement_id: str) -> dict[str, Any] | None:
    if announcement_id not in {item["id"] for item in announcement_items()}:
        return None
    _dismissed_announcements.add(announcement_id)
    return {"id": announcement_id, "dismissed": True}


def feedback_inbox() -> list[dict[str, Any]]:
    return []


def notification_recipients() -> list[str]:
    return list(_notification_recipients)


def add_notification_recipient(email: str) -> list[str]:
    with _lock:
        if email not in _notification_recipients:
            _notification_recipients.append(email)
            _notification_recipients.sort()
        return list(_notification_recipients)


def remove_notification_recipient(email: str) -> list[str]:
    with _lock:
        _notification_recipients[:] = [item for item in _notification_recipients if item != email]
        return list(_notification_recipients)


def admin_users() -> list[dict[str, Any]]:
    return [deepcopy(_admin_users[email]) for email in sorted(_admin_users)]


def admin_user(email: str) -> dict[str, Any] | None:
    item = _admin_users.get(email)
    return deepcopy(item) if item is not None else None


def add_admin_user(email: str, role: str, *, chat_logging_disabled: bool = False) -> dict[str, Any]:
    local_part = email.split("@", 1)[0]
    display_name = " ".join(part.capitalize() for part in local_part.replace("_", ".").split("."))
    item = {
        "email": email,
        "display_name": display_name,
        "active": True,
        "role": role,
        "universal_chat_logging_disabled": chat_logging_disabled,
        "esip_access_granted_at": datetime.now(timezone.utc).date().isoformat(),
    }
    with _lock:
        _admin_users[email] = item
    return deepcopy(item)


def update_admin_user(email: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _lock:
        item = _admin_users.get(email)
        if item is None:
            return None
        item.update(deepcopy(updates))
        return deepcopy(item)


def delete_admin_user(email: str) -> bool:
    with _lock:
        return _admin_users.pop(email, None) is not None


def change_candidates(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    items = list(_change_candidates.values())
    if status:
        items = [item for item in items if item.get("status") == status]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return deepcopy(items[:limit])


def create_change_candidate(payload: dict[str, Any], created_by: str) -> dict[str, Any]:
    candidate_id = f"reference-change-{uuid4().hex[:10]}"
    item = {
        "id": candidate_id,
        "title": payload["title"],
        "summary": payload["summary"],
        "change_type": payload.get("change_type", "other"),
        "impact": payload.get("impact", "medium"),
        "source": payload.get("source", "manual"),
        "related_files": deepcopy(payload.get("related_files", [])),
        "audience_roles": deepcopy(payload.get("audience_roles", ["user", "admin"])),
        "surfaces": deepcopy(payload.get("surfaces", ["web"])),
        "severity": payload.get("severity", "info"),
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "published_as_announcement_id": None,
    }
    with _lock:
        _change_candidates[candidate_id] = item
    return deepcopy(item)


def publish_change_candidate(candidate_id: str, payload: dict[str, Any], published_by: str) -> dict[str, Any] | None:
    with _lock:
        candidate = _change_candidates.get(candidate_id)
        if candidate is None:
            return None
        announcement_id = candidate.get("published_as_announcement_id") or f"reference-announcement-{uuid4().hex[:10]}"
        announcement = {
            "id": announcement_id,
            "title": payload.get("title") or candidate["title"],
            "body_markdown": payload.get("body_markdown") or candidate["summary"],
            "severity": payload.get("severity", candidate.get("severity", "info")),
            "status": "published",
            "audience_roles": deepcopy(payload.get("audience_roles", candidate.get("audience_roles", ["user", "admin"]))),
            "surfaces": deepcopy(payload.get("surfaces", candidate.get("surfaces", ["web"]))),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "published_by": published_by,
            "unpublished_at": None,
            "unpublished_by": None,
            "republished_at": None,
            "republished_by": None,
            "edited_at": None,
            "edited_by": None,
        }
        _product_announcements[announcement_id] = announcement
        candidate["status"] = "published"
        candidate["published_as_announcement_id"] = announcement_id
        return {"candidate": deepcopy(candidate), "announcement": deepcopy(announcement)}


def ignore_change_candidate(candidate_id: str, ignored_by: str) -> dict[str, Any] | None:
    with _lock:
        candidate = _change_candidates.get(candidate_id)
        if candidate is None:
            return None
        candidate["status"] = "ignored"
        candidate["ignored_by"] = ignored_by
        candidate["ignored_at"] = datetime.now(timezone.utc).isoformat()
        return deepcopy(candidate)


def product_announcements(limit: int = 100) -> list[dict[str, Any]]:
    items = sorted(_product_announcements.values(), key=lambda item: item["published_at"], reverse=True)
    return deepcopy(items[:limit])


def announcement_stats(announcement_id: str) -> dict[str, Any] | None:
    item = _product_announcements.get(announcement_id)
    if item is None:
        return None
    reads = [
        {"id": "reference-read-1", "user_email": "alex.morgan@northstar.example.com", "dismissed_at": "2026-07-29T13:15:00+00:00"},
        {"id": "reference-read-2", "user_email": "casey.nguyen@northstar.example.com", "dismissed_at": None},
        {"id": "reference-read-3", "user_email": "riley.brooks@northstar.example.com", "dismissed_at": "2026-07-30T09:40:00+00:00"},
    ]
    return {"announcement": deepcopy(item), "read_count": len(reads), "dismissed_count": 2, "reads": reads}


def update_product_announcement(announcement_id: str, updates: dict[str, Any], edited_by: str) -> dict[str, Any] | None:
    with _lock:
        item = _product_announcements.get(announcement_id)
        if item is None:
            return None
        for key in ("title", "body_markdown", "severity", "audience_roles", "surfaces"):
            if updates.get(key) is not None:
                item[key] = deepcopy(updates[key])
        item["edited_at"] = datetime.now(timezone.utc).isoformat()
        item["edited_by"] = edited_by
        return deepcopy(item)


def set_announcement_published(announcement_id: str, published: bool, actor: str) -> dict[str, Any] | None:
    with _lock:
        item = _product_announcements.get(announcement_id)
        if item is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        item["status"] = "published" if published else "unpublished"
        if published:
            item["republished_at"] = now
            item["republished_by"] = actor
        else:
            item["unpublished_at"] = now
            item["unpublished_by"] = actor
        return deepcopy(item)


def delete_product_announcement(announcement_id: str) -> bool:
    with _lock:
        return _product_announcements.pop(announcement_id, None) is not None


def list_feedback(uid: str) -> list[dict[str, Any]]:
    return deepcopy(_feedback.get(uid, []))


def all_feedback() -> list[dict[str, Any]]:
    return [deepcopy(entry) for entries in _feedback.values() for entry in entries]


def add_feedback(uid: str, email: str, name: str, text: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": f"ref-{uuid4().hex[:10]}",
        "uid": uid,
        "text": text,
        "created_at": now,
        "updated_at": now,
        "user_email": email,
        "user_name": name,
        "replies": [],
    }
    with _lock:
        _feedback.setdefault(uid, []).insert(0, entry)
    return deepcopy(entry)


def add_impact_event(event: dict[str, Any]) -> None:
    """Retain reference-session telemetry in process without external writes."""
    with _lock:
        _impact_events.append(deepcopy(event))


def impact_events() -> list[dict[str, Any]]:
    return deepcopy(_impact_events)


def maintenance_enabled() -> bool:
    return _maintenance_enabled


def set_maintenance(enabled: bool) -> bool:
    global _maintenance_enabled
    _maintenance_enabled = enabled
    return enabled


def update_cost_budget(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        _cost_budget.update(deepcopy(updates))
        _cost_budget["daily_limit_cad"] = _cost_budget["daily_limit_usd"]
        _cost_budget["monthly_limit_cad"] = _cost_budget["monthly_limit_usd"]
        return deepcopy(_cost_budget)


def _cost_rows() -> list[dict[str, Any]]:
    costs = (0.31, 0.42, 0.28, 0.51, 0.39, 0.47, 0.46)
    calls = (16, 19, 14, 24, 18, 21, 22)
    start = _BASE_TIME.date() + timedelta(days=24)
    return [
        {"date": (start + timedelta(days=index)).isoformat(), "cost": cost, "calls": calls[index]}
        for index, cost in enumerate(costs)
    ]


def cost_snapshot() -> dict[str, Any]:
    rows = _cost_rows()
    today = rows[-1]
    by_type = {
        "universal_p2": {"cost_usd": 0.21, "cost_cad": 0.21, "calls": 7},
        "embed_query": {"cost_usd": 0.08, "cost_cad": 0.08, "calls": 9},
        "vertex_search": {"cost_usd": 0.17, "cost_cad": 0.17, "calls": 6},
    }
    return {
        "currency": "CAD",
        "today": {
            "date": today["date"],
            "cost_usd": today["cost"],
            "cost_cad": today["cost"],
            "calls": today["calls"],
            "by_type": by_type,
            "pct_of_daily_limit": round(today["cost"] / _cost_budget["daily_limit_usd"] * 100, 1),
        },
        "history": [
            {"date": row["date"], "cost_usd": row["cost"], "cost_cad": row["cost"], "calls": row["calls"]}
            for row in rows
        ],
        "budget": deepcopy(_cost_budget),
        "alert": None,
        "note": "Reference workload with deterministic generation, search, and embedding cost signals.",
    }


def cost_range(from_dt: str, to_dt: str) -> dict[str, Any]:
    rows = _cost_rows()
    by_type = {
        "universal_p2": {"cost_usd": 1.31, "cost_cad": 1.31, "calls": 43, "tokens_in": 68400, "tokens_out": 11200},
        "embed_query": {"cost_usd": 0.52, "cost_cad": 0.52, "calls": 58, "tokens_in": 21400, "tokens_out": 0},
        "vertex_search": {"cost_usd": 1.01, "cost_cad": 1.01, "calls": 33, "tokens_in": 0, "tokens_out": 0},
    }
    return {
        "from_dt": from_dt,
        "to_dt": to_dt,
        "granularity": "daily",
        "total_cost_usd": 2.84,
        "total_cost_cad": 2.84,
        "total_calls": 134,
        "by_type": by_type,
        "buckets": [
            {"key": row["date"], "label": row["date"][5:], "cost_usd": row["cost"], "cost_cad": row["cost"], "calls": row["calls"]}
            for row in rows
        ],
    }


def cost_billing(from_dt: str, to_dt: str) -> dict[str, Any]:
    rows = _cost_rows()
    return {
        "from_dt": from_dt,
        "to_dt": to_dt,
        "currency": "CAD",
        "total_cost_usd": 3.12,
        "total_cost_cad": 3.12,
        "buckets": [
            {"key": row["date"], "label": row["date"][5:], "cost_usd": round(row["cost"] * 1.1, 2), "cost_cad": round(row["cost"] * 1.1, 2)}
            for row in rows
        ],
        "latest_day": rows[-1]["date"],
        "lag_note": "Reference billing series with a representative daily reporting lag.",
    }


def cost_reconciliation(from_dt: str, to_dt: str) -> dict[str, Any]:
    rows = _cost_rows()
    buckets = []
    for row in rows:
        billed = round(row["cost"] * 1.1, 2)
        untracked = round(billed - row["cost"], 2)
        buckets.append({
            "key": row["date"], "label": row["date"][5:], "billed_cad": billed,
            "net_billed_cad": billed, "credits_cad": 0.0, "app_actual_cad": row["cost"],
            "app_estimated_cad": row["cost"], "untracked_cad": untracked,
            "coverage_pct": round(row["cost"] / billed * 100, 1),
        })
    service_row = {
        "day": rows[-1]["date"], "service": "Managed application runtime", "billed_cad": 0.51,
        "net_billed_cad": 0.51, "app_actual_cad": 0.46, "app_estimated_cad": 0.46,
        "untracked_cad": 0.05, "coverage_pct": 90.2, "app_actual_events": 22,
        "call_types": "universal_p2, embed_query, vertex_search", "billing_rows": 3,
    }
    return {
        "from_dt": from_dt, "to_dt": to_dt, "currency": "CAD",
        "totals": {"billed_cad": 3.12, "credits_cad": 0.0, "net_billed_cad": 3.12,
                   "app_actual_cad": 2.84, "app_estimated_cad": 2.84, "untracked_cad": 0.28,
                   "coverage_pct": 91.0},
        "buckets": buckets, "service_rows": [service_row], "sku_rows": [],
        "top_untracked_services": [service_row],
        "lag_note": "Reference reconciliation compares application meters with representative billed totals.",
    }


def sources() -> dict[str, dict[str, Any]]:
    return deepcopy(_sources)


def update_source(source_id: str, **updates: Any) -> dict[str, Any] | None:
    with _lock:
        if source_id not in _sources:
            return None
        _sources[source_id].update(deepcopy(updates))
        return deepcopy(_sources[source_id])


def schedulers() -> list[dict[str, Any]]:
    return deepcopy(list(_schedulers.values()))


def update_scheduler(job_id: str, *, enabled: bool | None = None, schedule_time: str | None = None) -> dict[str, Any] | None:
    with _lock:
        if job_id not in _schedulers:
            return None
        job = _schedulers[job_id]
        if enabled is not None:
            job["enabled"] = enabled
            job["state"] = "ENABLED" if enabled else "PAUSED"
        if schedule_time is not None:
            hour, minute = schedule_time.split(":", 1)
            job["schedule_time"] = schedule_time
            job["schedule"] = f"{int(minute)} {int(hour)} * * *"
        return deepcopy(job)


def sync_runs_list() -> list[dict[str, Any]]:
    return [
        {"id": "ref-sync-002", "run_id": "ref-sync-002", "job_id": "esip-drive-sync", "trigger": "schedule", "status": "success", "started_at": "2026-07-31T10:30:00+00:00", "finished_at": "2026-07-31T10:31:34+00:00", "duration_sec": 94, "summary": {"documents_checked": 37, "documents_updated": 4}, "error_count": 0, "error_message": None},
        {"id": "ref-sync-001", "run_id": "ref-sync-001", "job_id": "esip-bq-sync", "trigger": "schedule", "status": "success", "started_at": "2026-07-31T10:00:00+00:00", "finished_at": "2026-07-31T10:02:18+00:00", "duration_sec": 138, "summary": {"tickets_checked": 240, "tickets_updated": 18}, "error_count": 0, "error_message": None},
    ]


def sync_run(run_id: str) -> dict[str, Any] | None:
    item = next((row for row in sync_runs_list() if row["id"] == run_id), None)
    if item is None:
        return None
    item["events"] = [
        {"id": f"{run_id}-1", "timestamp": item["started_at"], "level": "INFO", "stage": "discover", "message": "Reference change scan started."},
        {"id": f"{run_id}-2", "timestamp": item["finished_at"], "level": "INFO", "stage": "complete", "message": "Incremental refresh completed and checkpoints were recorded."},
    ]
    return item


def knowledge_gap_analysis(criteria: dict[str, Any]) -> dict[str, Any]:
    run_id = f"reference-gap-{len(_gap_runs) + 1:03d}"
    created_at = datetime.now(timezone.utc).isoformat()
    patterns = [
        ("payment-validation", "Payment recovery guidance misses the final validation step", "incomplete_documentation", 48, "Payment Processing", "update"),
        ("network-failover", "Network failover procedure is difficult to find", "hard_to_find", 36, "Network", "retitle_or_tag"),
        ("kitchen-queue", "Kitchen queue recovery steps differ across two sources", "conflicting_documentation", 29, "Kitchen Systems", "merge"),
    ][: int(criteria.get("pattern_limit", 3))]
    findings = []
    for index, (slug, title, gap_type, count, category, action) in enumerate(patterns):
        tickets = []
        for ticket in SUPPORT_TICKETS[index * 3:index * 3 + 3]:
            store = STORE_BY_ID[ticket.store_id]
            tickets.append({"ticket_id": str(ticket.ticket_id), "subject": ticket.subject, "status": ticket.status, "brand": store.brand, "store_id": store.store_id, "issue_category": category, "resolution_summary": "Recovered after a controlled validation sequence.", "url": f"https://support.example.com/agent/tickets/{ticket.ticket_id}"})
        findings.append({
            "finding_id": slug, "title": title,
            "summary": "Resolved tickets repeatedly include an operational step that is absent or unclear in the current guidance.",
            "gap_type": gap_type, "confidence": "high" if index < 2 else "medium",
            "impact_score": float(92 - index * 11), "impact_summary": f"{count} related tickets; a clearer procedure would reduce repeated diagnosis and handoff time.",
            "ticket_cluster": {"cluster_id": f"cluster-{slug}", "title": title, "issue_category": category, "ticket_count": count, "avg_resolution_hours": float(5 + index * 2), "avg_open_days": 0.8 + index * 0.4, "escalation_rate_pct": float(12 + index * 5), "reopen_rate_pct": float(4 + index * 2), "affected_brands": sorted({STORE_BY_ID[t.store_id].brand for t in SUPPORT_TICKETS[index * 3:index * 3 + 3]}), "affected_store_ids": [t["store_id"] for t in tickets], "common_symptoms": ["Workflow interruption", "Repeat troubleshooting after handoff"], "common_resolution_steps": ["Confirm affected scope", "Apply one bounded recovery action", "Validate the business workflow"], "sample_tickets": tickets},
            "source_coverage": [{"source": "zendesk_articles", "status": "partial" if index < 2 else "conflicting", "summary": "Core diagnosis is present, but the operational closeout needs clearer sequencing.", "documents": [{"source": "zendesk_articles", "title": "Support recovery checklist", "url": "https://docs.example.com/support-recovery", "document_id": f"doc-{slug}", "snippet": "Confirm scope, recover service, and record validation evidence.", "matched_queries": [category], "freshness": "reviewed 2026-06", "owner": "Support Enablement"}], "source_specific_issue": title}],
            "mismatches": [{"docs_claim": "The primary recovery action completes the procedure.", "ticket_reality": "Engineers also verify the end-to-end workflow and capture evidence before closure.", "conclusion": "Add the validation and evidence step to the canonical procedure.", "confidence": "high"}],
            "recommended_actions": [{"action_type": action, "source": "zendesk_articles", "title": "Strengthen the canonical recovery procedure", "rationale": "Align guidance with the repeatable steps visible in resolved cases.", "expected_impact": "Faster handoffs and fewer reopened tickets.", "owner_hint": "Support Enablement"}],
            "draft_options": [{"draft_type": "zendesk_rewrite", "label": "Draft article update", "target_source": "zendesk_articles", "description": "Prepare a reviewable update grounded in the selected evidence."}],
        })
    result = {
        "criteria": deepcopy(criteria),
        "findings": findings,
        "notes": ["Evidence is limited to the selected ticket period and knowledge sources."],
        "run_id": run_id,
        "created_at": created_at,
    }
    with _lock:
        _gap_runs[run_id] = {"run_id": run_id, "title": "Ticket reality vs knowledge coverage", "created_at": created_at, "owner_uid": REFERENCE_UID, "owner_email": PLATFORM_ADMIN_EMAIL, "result": deepcopy(result)}
    return result


def knowledge_gap_runs() -> list[dict[str, Any]]:
    return [{"run_id": row["run_id"], "title": row["title"], "created_at": row["created_at"], "period_days": row["result"]["criteria"]["period_days"], "min_tickets": row["result"]["criteria"]["min_tickets"], "mode": row["result"]["criteria"]["mode"], "sources": row["result"]["criteria"].get("sources", []), "finding_count": len(row["result"]["findings"]), "top_findings": [item["title"] for item in row["result"]["findings"][:3]]} for row in reversed(list(_gap_runs.values()))]


def knowledge_gap_run(run_id: str) -> dict[str, Any] | None:
    return deepcopy(_gap_runs.get(run_id))


def knowledge_gap_draft(request: dict[str, Any]) -> dict[str, Any]:
    finding = request["finding"]
    return {"draft_type": request["draft_type"], "title": f"Draft: {finding['title']}", "target_source": request.get("target_source"), "sections": [{"heading": "Purpose", "body": finding["summary"], "basis": "ticket_fact"}, {"heading": "Procedure update", "body": "Confirm scope, apply one controlled recovery action, validate the business workflow end to end, and attach the final evidence before closure.", "basis": "recommendation"}, {"heading": "Review checkpoint", "body": "Confirm ownership, escalation thresholds, and links to adjacent procedures.", "basis": "open_question"}], "evidence_used": [ticket["ticket_id"] for ticket in (finding.get("ticket_cluster") or {}).get("sample_tickets", [])], "open_questions": ["Who owns the final content review?"], "review_required": True}


def analytics_response(question: str, brand_filter: str | None = None) -> tuple[str, dict[str, Any], list[dict[str, str]]]:
    normalized = question.lower()
    if "brand" in normalized or "most tickets" in normalized:
        counts: dict[str, int] = {}
        for ticket in SUPPORT_TICKETS:
            brand = STORE_BY_ID[ticket.store_id].brand
            if brand_filter and brand != brand_filter:
                continue
            counts[brand] = counts.get(brand, 0) + 1
        rows = [[brand, count] for brand, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
        leaders = ", ".join(f"**{brand}** ({count})" for brand, count in rows[:3])
        answer = f"Across the selected reference period, the highest ticket volumes are {leaders}. The distribution is intentionally balanced enough to compare brands without one synthetic tenant overwhelming the result."
        table = {"columns": ["brand", "ticket_count"], "rows": rows}
    elif "category" in normalized or "root cause" in normalized:
        categories = [("Payment Processing", 44), ("Kitchen Systems", 41), ("Network", 38), ("Printing", 33), ("Menu Management", 29)]
        answer = "Payment Processing, Kitchen Systems, and Network are the leading issue groups in the selected period. Review the knowledge-gap findings alongside these counts to separate high volume from weak guidance."
        table = {"columns": ["issue_category", "ticket_count"], "rows": [[name, count] for name, count in categories]}
    else:
        statuses: dict[str, int] = {}
        for ticket in SUPPORT_TICKETS:
            statuses[ticket.status] = statuses.get(ticket.status, 0) + 1
        answer = "The reference dataset supports cohort, trend, workload, and resolution questions. This result summarizes the current ticket-status mix for the selected period."
        table = {"columns": ["status", "ticket_count"], "rows": [[name, count] for name, count in sorted(statuses.items())]}
    kb = [{"title": article.title, "url": article.url, "snippet": article.snippet} for article in knowledge_suggestions(question, 2)]
    return answer, table, kb


def usage_dashboard(
    *,
    range_mode: str,
    start_date: str | None,
    end_date: str | None,
    user_email: str | None,
    question_type: str | None,
    compare_previous: bool,
    group_by: str,
    min_activity: int,
    problem_signal: str,
    category_focus: str,
    technical_quality_enabled: bool,
) -> dict[str, Any]:
    end_day = end_date or "2026-08-03"
    start_day = start_date or "2026-07-28"
    types = ["Ticket investigation", "Document lookup", "Procedure / how-to", "Analytics / reporting"]
    users: list[dict[str, Any]] = []
    for index, fixture in enumerate(user for user in USERS if user.role != "super_admin"):
        queries = 18 + (index * 7) % 31
        row = {
            "email": fixture.email,
            "display_name": fixture.name,
            "role": fixture.role,
            "active": True,
            "first_seen_at": "2026-05-26T13:00:00+00:00",
            "last_active_at": f"2026-08-{3 - (index % 3):02d}T{13 + index % 5:02d}:20:00+00:00",
            "queries_to_date": 94 + index * 23,
            "queries_in_range": queries,
            "avg_latency_ms": 3180 + index * 165,
            "weak_or_no_answer": 1 if index in {4, 8} else 0,
            "no_source_count": 1 if index in {2, 4, 7} else 0,
            "evidence_rate_pct": round((queries - (1 if index in {2, 4, 7} else 0)) / queries * 100, 1),
            "documentation_opportunities": 1 if index in {2, 4, 7} else 0,
            "active_days": 4 + index % 3,
            "top_question_type": types[index % len(types)],
        }
        users.append(row)
    filters = {
        "users": [
            {"email": item["email"], "display_name": item["display_name"], "role": item["role"], "queries_to_date": item["queries_to_date"]}
            for item in users
        ],
        "question_types": types,
    }
    if user_email:
        users = [item for item in users if item["email"] == user_email]
    if question_type:
        users = [item for item in users if item["top_question_type"] == question_type]
    unfiltered_count = len(users)
    users = [item for item in users if item["queries_in_range"] >= max(0, min_activity)]
    total = sum(item["queries_in_range"] for item in users)
    active = len(users)
    weak = sum(item["weak_or_no_answer"] for item in users)
    no_source = sum(item["no_source_count"] for item in users)
    question_types = []
    for label in types:
        count = sum(item["queries_in_range"] for item in users if item["top_question_type"] == label)
        if count == 0:
            count = max(6, total // (len(types) + 2))
        question_types.append({
            "question_type": label,
            "count": count,
            "users": sum(1 for item in users if item["top_question_type"] == label) or min(active, 3),
            "weak_or_no_answer": 1 if label == "Analytics / reporting" and weak else 0,
            "no_source_count": 1 if label == "Document lookup" and no_source else 0,
            "evidence_rate_pct": 96.0 if label != "Document lookup" else 91.0,
        })
    top_type = max(question_types, key=lambda item: item["count"])["question_type"] if question_types else None
    day_counts = (48, 53, 44, 61, 57, 39, 46)
    start_obj = datetime.fromisoformat(start_day).date()
    timeseries = [
        {
            "date": (start_obj + timedelta(days=index)).isoformat(),
            "queries": count,
            "active_users": 5 + index % 4,
            "weak_or_no_answer": 1 if index in {2, 5} else 0,
            "no_source": 1 if index in {1, 4, 6} else 0,
        }
        for index, count in enumerate(day_counts)
    ]
    if group_by == "question_type":
        grouped = [
            {
                "key": item["question_type"], "label": item["question_type"], "count": item["count"],
                "users": item["users"], "share_pct": round(item["count"] / max(total, 1) * 100, 1),
                "active_days": 7, "last_active_at": f"{end_day}T19:20:00+00:00",
                "top_question_type": item["question_type"], "weak_or_no_answer": item["weak_or_no_answer"],
                "no_source_count": item["no_source_count"], "evidence_rate_pct": item["evidence_rate_pct"],
                "documentation_opportunities": item["no_source_count"],
            }
            for item in question_types
        ]
    else:
        grouped = [
            {
                "key": item["email"], "label": item["display_name"], "count": item["queries_in_range"],
                "users": 1, "share_pct": round(item["queries_in_range"] / max(total, 1) * 100, 1),
                "active_days": item["active_days"], "last_active_at": item["last_active_at"],
                "top_question_type": item["top_question_type"], "weak_or_no_answer": item["weak_or_no_answer"],
                "no_source_count": item["no_source_count"], "evidence_rate_pct": item["evidence_rate_pct"],
                "documentation_opportunities": item["documentation_opportunities"],
            }
            for item in users
        ]
    return {
        "range": {"mode": range_mode, "start_at": f"{start_day}T04:00:00+00:00", "end_at": f"{end_day}T23:59:59+00:00", "include_translation_turns": False, "user_email": user_email, "question_type": question_type},
        "controls": {"compare_previous": compare_previous, "group_by": group_by, "min_activity": min_activity, "problem_signal": problem_signal, "category_focus": category_focus, "technical_quality_enabled": technical_quality_enabled},
        "summary": {"total_queries_to_date": 2148, "total_queries_in_range": total, "active_users_in_range": active, "avg_queries_per_active_user": round(total / active, 1) if active else 0, "weak_or_no_answer_turns": weak, "no_source_turns": no_source, "top_question_type": top_type, "usage_by_role": [{"role": role, "count": sum(item["queries_in_range"] for item in users if item["role"] == role)} for role in ("user", "admin")]},
        "comparison": {"start_at": "2026-07-21T04:00:00+00:00", "end_at": "2026-07-27T23:59:59+00:00", "total_queries_in_range": max(0, total - 37), "active_users_in_range": max(0, active - 1), "weak_or_no_answer_turns": weak + 2, "no_source_turns": no_source + 3} if compare_previous else None,
        "users": users,
        "users_unfiltered_count": unfiltered_count,
        "question_types": question_types,
        "timeseries": timeseries,
        "grouped_breakdown": grouped,
        "source_coverage": {"total_turns": total, "with_sources": max(0, total - no_source), "with_tickets": round(total * 0.38), "with_analytics": round(total * 0.18), "with_evidence": max(0, total - no_source), "without_sources": no_source, "source_coverage_pct": round((total - no_source) / max(total, 1) * 100, 1), "evidence_coverage_pct": round((total - no_source) / max(total, 1) * 100, 1), "ticket_usage_pct": 38.0},
        "document_gaps": [
            {"theme": "Network failover", "question_type": "Document lookup", "count": 7, "users_affected": 4, "no_source_count": 2, "weak_or_no_answer": 1, "example_question": "Where is the store failover validation sequence?", "suggested_action": "Add a direct evidence link to the network recovery checklist."},
            {"theme": "Payment validation", "question_type": "Procedure / how-to", "count": 5, "users_affected": 3, "no_source_count": 1, "weak_or_no_answer": 0, "example_question": "What closes the terminal recovery procedure?", "suggested_action": "Clarify the final authorization and receipt validation step."},
        ],
        "filters": filters,
        "notes": ["The report uses reference-session activity and excludes the platform administrator from management totals."],
    }


def service_desk_kpi_report(report_date: str | None = None) -> dict[str, Any]:
    day = report_date or "2026-08-03"
    engineers = [user for user in USERS if user.role == "user"]
    rows: list[dict[str, Any]] = []
    for index, fixture in enumerate(engineers):
        solved = 5 + index % 4
        outstanding = 3 + index % 5
        rows.append({
            "email": fixture.email,
            "display_name": fixture.name,
            "inbound_calls": 7 + index * 2,
            "avg_call_time_seconds": 318 + index * 11,
            "new_assigned_tickets": 6 + index % 5,
            "same_day_solved_tickets": 3 + index % 3,
            "solved_tickets": solved,
            "outstanding_tickets": outstanding,
            "median_resolution_hours": round(4.8 + index * 0.35, 2),
            "single_cycle_solved": max(1, solved - 1),
            "single_cycle_total": solved,
            "single_cycle_solve_rate_pct": round(max(1, solved - 1) / solved * 100, 1),
            "sla_breached": 1 if index in {3, 6} else 0,
            "sla_known": solved,
            "sla_breach_rate_pct": round((1 if index in {3, 6} else 0) / solved * 100, 1),
            "first_reply_tickets": 5 + index % 4,
            "median_first_public_reply_hours": round(0.7 + index * 0.08, 2),
            "customer_reply_count": 4 + index % 5,
            "median_customer_reply_hours": round(1.1 + index * 0.1, 2),
        })

    def total(field: str) -> int:
        return int(sum(int(row[field]) for row in rows))

    solved_total = total("solved_tickets")
    single_total = total("single_cycle_total")
    sla_known = total("sla_known")
    group_total = {
        "email": "service-desk",
        "display_name": "Service Desk",
        "inbound_calls": total("inbound_calls"),
        "avg_call_time_seconds": round(sum(float(row["avg_call_time_seconds"] or 0) for row in rows) / len(rows), 1),
        "new_assigned_tickets": total("new_assigned_tickets"),
        "same_day_solved_tickets": total("same_day_solved_tickets"),
        "solved_tickets": solved_total,
        "outstanding_tickets": total("outstanding_tickets"),
        "median_resolution_hours": 5.9,
        "single_cycle_solved": total("single_cycle_solved"),
        "single_cycle_total": single_total,
        "single_cycle_solve_rate_pct": round(total("single_cycle_solved") / single_total * 100, 1),
        "sla_breached": total("sla_breached"),
        "sla_known": sla_known,
        "sla_breach_rate_pct": round(total("sla_breached") / sla_known * 100, 1),
        "first_reply_tickets": total("first_reply_tickets"),
        "median_first_public_reply_hours": 0.92,
        "customer_reply_count": total("customer_reply_count"),
        "median_customer_reply_hours": 1.42,
        "group_key": "service-desk",
        "member_names": [row["display_name"] for row in rows],
        "mapped_row_names": [row["display_name"] for row in rows],
        "active_rows": len(rows),
        "unmapped": False,
        "assignment_notes": [],
    }
    group_rows = [group_total]
    group_sections = [{
        "group_name": "Service Desk",
        "group_key": "service-desk",
        "total": group_total,
        "rows": [{**row, "group_name": "Service Desk", "row_type": "engineer"} for row in rows],
    }]
    return {
        "date": day,
        "timezone": "America/New_York",
        "generated_at": f"{day}T21:30:00+00:00",
        "summary": {
            "active_engineers": len(rows), "inbound_calls": total("inbound_calls"),
            "avg_call_time_seconds": group_total["avg_call_time_seconds"],
            "new_assigned_tickets": total("new_assigned_tickets"),
            "same_day_solved_tickets": total("same_day_solved_tickets"),
            "solved_tickets": solved_total, "outstanding_tickets": total("outstanding_tickets"),
            "customer_replies": total("customer_reply_count"),
            "median_first_public_reply_hours": group_total["median_first_public_reply_hours"],
            "median_customer_reply_hours": group_total["median_customer_reply_hours"],
            "single_cycle_solved": total("single_cycle_solved"), "single_cycle_total": single_total,
            "single_cycle_solve_rate_pct": group_total["single_cycle_solve_rate_pct"],
            "sla_breached": total("sla_breached"), "sla_known": sla_known,
            "sla_breach_rate_pct": group_total["sla_breach_rate_pct"],
        },
        "coverage": {
            "relevant_tickets": total("new_assigned_tickets") + total("outstanding_tickets"),
            "tickets_with_internal_assignee_email": total("new_assigned_tickets") + total("outstanding_tickets"),
            "assignee_email_coverage_pct": 100.0,
            "public_agent_replies": total("customer_reply_count") + total("first_reply_tickets"),
            "public_agent_replies_with_email": total("customer_reply_count") + total("first_reply_tickets"),
            "comment_author_email_coverage_pct": 100.0,
            "telephony": {"status": "ready", "available": True, "contact_count": total("inbound_calls"), "agent_count": len(rows), "api_total_records": total("inbound_calls"), "warning": None},
        },
        "rows": rows,
        "group_rows": group_rows,
        "group_sections": group_sections,
        "grouping": {"source": "reference_team_map", "multi_group_members": [], "unmapped_active_rows": 0, "warnings": []},
        "notes": ["Ticket, reply, SLA, and telephony metrics share the selected business date."],
        "warnings": [],
    }


def service_desk_kpi_snapshots(limit: int = 31) -> list[dict[str, Any]]:
    items = []
    for index in range(min(limit, 7)):
        day = (datetime(2026, 8, 3).date() - timedelta(days=index)).isoformat()
        items.append({"report_date": day, "bucket": "esip-reference-reports", "blob_name": f"service-desk-kpi/{day}.pdf", "gcs_uri": None, "filename": f"service-desk-kpi-{day}.pdf", "media_type": "application/pdf", "size_bytes": 28640 + index * 440, "updated_at": f"{day}T21:31:00+00:00"})
    return items


def kpi_delivery_config() -> dict[str, Any]:
    return deepcopy(_kpi_delivery_config)


def update_kpi_delivery_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        for key in ("enabled", "timezone", "recipients", "cc_recipients", "slots", "last_delivery", "last_test"):
            if key in updates and updates[key] is not None:
                _kpi_delivery_config[key] = deepcopy(updates[key])
        slots = _kpi_delivery_config.get("slots") or []
        primary = next((slot for slot in slots if slot.get("enabled")), slots[0] if slots else {"time": "10:30"})
        _kpi_delivery_config["scheduler"].update({"enabled": bool(_kpi_delivery_config["enabled"]), "state": "ENABLED" if _kpi_delivery_config["enabled"] else "PAUSED", "schedule_time": primary["time"], "schedule": f"{int(primary['time'].split(':')[1])} {int(primary['time'].split(':')[0])} * * *"})
        return deepcopy(_kpi_delivery_config)


def impact_dashboard(rollout_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    rollout = datetime.fromisoformat(rollout_date or "2026-05-26").date()
    end = datetime.fromisoformat(end_date or "2026-08-03").date()
    ramp_end = rollout + timedelta(days=7)
    period_labels = {"baseline": "Baseline", "ramp": "Ramp-up", "post": "Post rollout"}
    period_values = {
        "baseline": {"events": 0, "active_users": 0, "assisted_tickets": 0},
        "ramp": {"events": 138, "active_users": 7, "assisted_tickets": 42},
        "post": {"events": 684, "active_users": 11, "assisted_tickets": 196},
    }
    ranges = {
        "baseline": {"label": "Baseline", "start_date": (rollout - timedelta(days=28)).isoformat(), "end_date": rollout.isoformat()},
        "ramp": {"label": "Ramp-up", "start_date": rollout.isoformat(), "end_date": ramp_end.isoformat()},
        "post": {"label": "Post rollout", "start_date": ramp_end.isoformat(), "end_date": end.isoformat()},
    }
    adoption_periods = {key: {"label": period_labels[key], **value} for key, value in period_values.items()}
    response_periods = {
        "baseline": {"replies": 188, "assisted_replies": 0, "median_latency_hours": 2.8, "assisted_median_latency_hours": None, "non_assisted_median_latency_hours": 2.8},
        "ramp": {"replies": 74, "assisted_replies": 31, "median_latency_hours": 2.2, "assisted_median_latency_hours": 1.7, "non_assisted_median_latency_hours": 2.6},
        "post": {"replies": 362, "assisted_replies": 214, "median_latency_hours": 1.6, "assisted_median_latency_hours": 1.2, "non_assisted_median_latency_hours": 2.1},
    }
    outcome_periods = {
        "baseline": {"solved_tickets": 164, "median_resolution_hours": 9.4, "sla_breach_rate_pct": 8.5, "sla_known_tickets": 153, "single_cycle_solve_rate_pct": 61.0},
        "ramp": {"solved_tickets": 58, "median_resolution_hours": 8.2, "sla_breach_rate_pct": 7.0, "sla_known_tickets": 57, "single_cycle_solve_rate_pct": 65.5},
        "post": {"solved_tickets": 318, "median_resolution_hours": 6.8, "sla_breach_rate_pct": 5.4, "sla_known_tickets": 312, "single_cycle_solve_rate_pct": 72.3},
    }
    quality_periods = {
        "baseline": {"chat_turns": 0, "weak_or_no_answer": 0, "no_evidence": 0, "documentation_gap_signals": 0, "answer_feedback_events": 0, "answer_copied_events": 0, "not_useful_events": 0},
        "ramp": {"chat_turns": 86, "weak_or_no_answer": 5, "no_evidence": 4, "documentation_gap_signals": 8, "answer_feedback_events": 23, "answer_copied_events": 17, "not_useful_events": 2},
        "post": {"chat_turns": 412, "weak_or_no_answer": 12, "no_evidence": 9, "documentation_gap_signals": 21, "answer_feedback_events": 108, "answer_copied_events": 76, "not_useful_events": 5},
    }
    user_rows = []
    for index, fixture in enumerate(user for user in USERS if user.role == "user"):
        post_events = 38 + index * 9
        user_rows.append({
            "key": fixture.key, "email": fixture.email, "display_name": fixture.name,
            "access_granted_at": "2026-05-26", "confidence": "Enough data" if index < 5 else "Ramp-up",
            "confidence_reason": "Activity spans multiple weeks and assisted tickets." if index < 5 else "More post-rollout activity is needed.",
            "identity_source": "access_allowlist",
            "usage": {"baseline": {"events": 0, "active_days": 0, "assisted_tickets": 0}, "ramp": {"events": 8 + index, "active_days": 3, "assisted_tickets": 3 + index}, "post": {"events": post_events, "active_days": 14 + index, "assisted_tickets": 15 + index * 3}},
            "replies": {"baseline": {"replies": 22 + index, "assisted_replies": 0, "median_latency_hours": 2.5 + index * 0.1}, "ramp": {"replies": 9 + index, "assisted_replies": 4 + index, "median_latency_hours": 1.9}, "post": {"replies": 42 + index * 3, "assisted_replies": 24 + index * 2, "median_latency_hours": round(1.1 + index * 0.12, 2)}},
            "solved": {"baseline": {"solved_tickets": 18 + index, "median_resolution_hours": 9.2, "sla_breach_rate_pct": 8.0, "single_cycle_solve_rate_pct": 60.0}, "ramp": {"solved_tickets": 7 + index, "median_resolution_hours": 8.0, "sla_breach_rate_pct": 7.0, "single_cycle_solve_rate_pct": 65.0}, "post": {"solved_tickets": 31 + index * 3, "median_resolution_hours": round(6.1 + index * 0.2, 1), "sla_breach_rate_pct": 4.5 + index * 0.4, "single_cycle_solve_rate_pct": 70.0 + index}},
            "outstanding_tickets": 4 + index,
            "reply_latency_delta": {"value": round(-1.1 + index * 0.06, 2), "direction": "faster", "label": "faster"},
            "solved_delta": {"value": 11 + index * 2, "direction": "up", "label": "more solved"},
        })
    timeline = [
        {"date": (ramp_end + timedelta(days=index * 9)).isoformat(), "events": 48 + index * 11, "active_users": 7 + index % 4, "assisted_tickets": 16 + index * 4}
        for index in range(7)
    ]
    result = {
        "range": {**ranges, "rollout_date": rollout.isoformat(), "as_of_date": end.isoformat(), "post_started": True, "current_phase": "post", "display_period": "post", "ramp_day": 7},
        "summary": {"display_period": "post", "display_period_label": "Post rollout", "post_started": True, "ramp_day": 7, "active_users_post": 11, "active_users_display": 11, "repeat_users_post": 9, "esip_assisted_tickets_post": 196, "esip_assisted_tickets_display": 196, "median_reply_latency_post_hours": 1.6, "median_reply_latency_display_hours": 1.6, "solved_tickets_post": 318, "solved_tickets_display": 318, "outstanding_tickets": 37, "cautious_label": "Directional improvement with stable evidence coverage"},
        "adoption": {"periods": adoption_periods, "workflows": [{"period": period, "workflow": workflow, "events": events, "users": users, "tickets": tickets} for period, workflow, events, users, tickets in (("ramp", "Ticket investigation", 62, 7, 29), ("ramp", "Knowledge lookup", 41, 6, 18), ("post", "Ticket investigation", 302, 11, 138), ("post", "Knowledge lookup", 214, 10, 104), ("post", "Analytics", 98, 7, 32), ("post", "Knowledge gap review", 70, 5, 21))], "timeline": timeline, "repeat_users_post": 9},
        "response_speed": {"periods": {**response_periods, "headline_delta": {"value": -1.2, "direction": "faster", "label": "1.2h faster"}}},
        "first_action": {"periods": {"baseline": {"tickets": 176, "median_first_action_hours": 1.4}, "ramp": {"tickets": 68, "median_first_action_hours": 1.1}, "post": {"tickets": 344, "median_first_action_hours": 0.8}}},
        "outcomes": {"periods": outcome_periods, "headline_solved_delta": {"value": 154, "direction": "up", "label": "154 more solved"}, "headline_resolution_delta": {"value": -2.6, "direction": "faster", "label": "2.6h faster"}, "outstanding_tickets": 37, "assignee_email_coverage": True},
        "quality": {"periods": quality_periods},
        "users": user_rows,
        "insights": {"wins": ["Assisted replies are faster while evidence coverage remains high.", "Repeat usage spans ticket, knowledge, and analytics workflows."], "concerns": ["A small set of document lookups still ends without direct evidence.", "Two engineers remain in ramp-up confidence."], "caveats": ["The dashboard reports association, not causation.", "Baseline and post periods differ in ticket mix and staffing coverage."]},
        "warnings": [],
    }
    result["report"] = {"title": "ESIP Early Impact Report", "subtitle": "Adoption, response, outcomes, and quality", "headline": result["summary"]["cautious_label"], "generated_at": "2026-08-03T20:00:00+00:00", "kpis": [{"label": "Active users", "value": 11, "detail": "Post-rollout active users"}, {"label": "Assisted tickets", "value": 196, "detail": "Tickets with an ESIP workflow touch"}, {"label": "Median reply", "value": 1.6, "suffix": "h", "detail": "Post-rollout public reply latency"}, {"label": "Solved tickets", "value": 318, "detail": "Post-rollout solved workload"}]}
    return result


def chat_logs(limit: int = 25) -> list[dict[str, Any]]:
    questions = (
        ("How should I validate a payment terminal recovery?", 950001),
        ("Show recent network failover cases.", 950004),
        ("Which brands have the most open tickets?", None),
        ("Summarize the evidence for this kitchen display incident.", 950002),
        ("Where is the receipt printer queue checklist?", 950003),
        ("What documentation gaps recur in solved cases?", None),
    )
    rows = []
    for index, (question, ticket_id) in enumerate(questions):
        owner = USERS[2 + index % (len(USERS) - 2)]
        answer, sources, tickets = chat_response(question, ticket_id)
        rows.append({"created_at": f"2026-08-03T{19 - index:02d}:20:00+00:00", "conversation_id": f"reference-conversation-{index + 1:02d}", "turn_id": f"reference-turn-{index + 1:02d}", "trace_id": f"reference-trace-{index + 1:02d}", "owner_email": owner.email, "owner_display_name": owner.name, "owner_role": owner.role, "ticket_id": ticket_id, "target_ticket_id": ticket_id, "question": question, "answer": answer, "sources_count": len(sources), "tickets_count": len(tickets), "answer_chars": len(answer), "latency_ms": 2860 + index * 240, "gcs_payload_uri": None})
    return rows[: max(1, min(limit, 100))]


def quality_dashboard(*, start_date: str | None, end_date: str | None, limit: int = 25, **filters: Any) -> dict[str, Any]:
    events = [
        {"created_at": "2026-08-03T19:20:00+00:00", "mode": "ticket", "quality": "good", "risk_classes": [], "validator_action": "none", "answer_policy": "grounded", "repair_action": None, "repair_stop_reason": "accepted", "model": "reference-reasoner", "model_surface": "universal_chat", "latency_ms": 2860, "estimated_cost_cad": 0.012, "trace_id": "reference-trace-01", "turn_id": "reference-turn-01", "conversation_id": "reference-conversation-01", "requester_email": "alex.morgan@northstar.example.com"},
        {"created_at": "2026-08-03T18:20:00+00:00", "mode": "general", "quality": "weak", "risk_classes": ["weak_sources"], "validator_action": "rewrite_with_existing_sources", "answer_policy": "repaired", "repair_action": "rewrite_with_existing_sources", "repair_stop_reason": "accepted", "model": "reference-reasoner", "model_surface": "universal_chat", "latency_ms": 4210, "estimated_cost_cad": 0.018, "trace_id": "reference-trace-02", "turn_id": "reference-turn-02", "conversation_id": "reference-conversation-02", "requester_email": "casey.nguyen@northstar.example.com"},
        {"created_at": "2026-08-03T17:20:00+00:00", "mode": "analytics", "quality": "good", "risk_classes": [], "validator_action": "none", "answer_policy": "grounded", "repair_action": None, "repair_stop_reason": "accepted", "model": "reference-reasoner", "model_surface": "analytics", "latency_ms": 3540, "estimated_cost_cad": 0.015, "trace_id": "reference-trace-03", "turn_id": "reference-turn-03", "conversation_id": "reference-conversation-03", "requester_email": "riley.brooks@northstar.example.com"},
    ]
    if filters.get("quality"):
        events = [item for item in events if item["quality"] == filters["quality"]]
    if filters.get("risk"):
        events = [item for item in events if filters["risk"] in item["risk_classes"]]
    events = events[: max(1, min(limit, 100))]
    quality_counts = {"good": 34, "weak": 4, "suspect": 1, "insufficient_evidence": 2}
    review_rows = [{"created_at": item["created_at"], "trace_id": item["trace_id"], "turn_id": item["turn_id"], "conversation_id": item["conversation_id"], "target_ticket_id": 950002 if item["mode"] == "ticket" else None, "event_count": 1, "modes": [item["mode"]], "qualities": [item["quality"]], "conflicting_verdict": False, "reason_tags": ["weak_sources"] if item["quality"] == "weak" else [], "latency_ms": item["latency_ms"], "answer_chars": 820, "sources_count": 3, "tickets_count": 2} for item in events]
    return {
        "range": {"timezone": "America/New_York", "start_date": start_date or "2026-07-28", "end_date": end_date or "2026-08-03", "start_at": f"{start_date or '2026-07-28'}T04:00:00+00:00", "end_at": f"{end_date or '2026-08-03'}T23:59:59+00:00", "max_days": 31},
        "summary": {"quality_event_count": sum(quality_counts.values()), "unique_answer_count": 39, "auto_validator_event_count": 36, "manual_review_event_count": 5, "duplicate_event_count": 2, "conflicting_verdict_count": 1, "turn_count": 41, "suspect_event_count": 5, "repair_attempt_count": 4, "repair_validation_count": 4, "repair_revalidation_count": 1},
        "coverage": {"quality_events_available": True, "turn_logs_available": True, "quality_events_count": 41, "turn_logs_count": 41, "partial": False},
        "quality_distribution": [{"name": key, "count": value} for key, value in quality_counts.items()],
        "unique_turn_quality_distribution": [{"name": key, "count": value - (1 if key in {"good", "weak"} else 0)} for key, value in quality_counts.items()],
        "validator_actions": [{"name": "none", "count": 36}, {"name": "rewrite_with_existing_sources", "count": 3}, {"name": "rebuild_scoped_evidence", "count": 2}],
        "risk_classes": [{"name": "weak_sources", "count": 3}, {"name": "context_loss", "count": 1}, {"name": "stale_data", "count": 1}],
        "repair_actions": [{"name": "rewrite_with_existing_sources", "count": 3}, {"name": "rebuild_scoped_evidence", "count": 1}],
        "repair_stop_reasons": [{"name": "accepted", "count": 4}],
        "model_surfaces": [{"name": "universal_chat", "count": 33}, {"name": "analytics", "count": 8}],
        "latency": {"count": 41, "avg_ms": 3470, "p95_ms": 5280, "max_ms": 6410},
        "stage_timings": {"available": True, "turn_count": 41, "measured_turn_count": 39, "unmeasured_turn_count": 2, "partial_measured_turn_count": 1, "stages": [{"stage": "routing", "count": 39, "p50_ms": 84, "p90_ms": 132, "max_ms": 188}, {"stage": "retrieval", "count": 38, "p50_ms": 610, "p90_ms": 960, "max_ms": 1280}, {"stage": "generation", "count": 39, "p50_ms": 2140, "p90_ms": 3260, "max_ms": 4010}, {"stage": "validation", "count": 39, "p50_ms": 290, "p90_ms": 440, "max_ms": 620}], "missing_by_stage": [{"stage": "retrieval", "count": 3}], "caveat": "Stage timing coverage is complete for 39 of 41 turns."},
        "error_provenance": {"event_count": 3, "fallback_used_count": 2, "sources": [{"name": "knowledge_search", "count": 2}, {"name": "ticket_lookup", "count": 1}], "outcomes": [{"name": "recovered", "count": 2}, {"name": "bounded_failure", "count": 1}], "endpoint_classes": [{"name": "retrieval", "count": 3}], "status_codes": [{"name": "timeout", "count": 2}], "reason_codes": [{"name": "provider_timeout", "count": 2}, {"name": "insufficient_evidence", "count": 1}]},
        "cost": {"count": 41, "estimated_total_cad": 0.57, "estimated_avg_cad": 0.0139},
        "trend": [{"day": f"2026-07-{28 + index:02d}", "events": 5 + index, "good": 4 + index, "weak": 1 if index in {1, 4} else 0, "suspect": 1 if index == 5 else 0, "likely_wrong": 0, "unsafe": 0} for index in range(7)],
        "recent_events": events,
        "review_queue": {"available_reason_tags": ["weak_sources", "slow", "no_data", "needs_ticket_resolution"], "selected_reason_tag": filters.get("reason_tag"), "thresholds": {"too_long_answer_chars": 5000, "slow_latency_ms": 12000}, "answer_count": len(review_rows), "untagged_answer_count": sum(not item["reason_tags"] for item in review_rows), "reason_distribution": [{"name": "weak_sources", "count": 1}], "rows": review_rows},
        "warnings": [], "caveats": ["Telemetry contains aggregate operational signals and excludes raw answer text."],
        "filters": {**filters, "limit": limit},
        "source_counts": {"quality_events_before_filters": 41, "quality_events_after_filters": len(events), "turn_logs": 41, "provenance_events": 3},
    }


def dispatcher_config() -> dict[str, Any]:
    return deepcopy(_dispatcher_config)


def update_dispatcher_config(updates: dict[str, Any], actor: str) -> dict[str, Any]:
    allowed = {"mode", "planner_daily_cap_cad", "max_planner_calls_per_turn", "max_tool_actions", "max_explicit_ticket_ids", "max_action_rows", "max_output_tokens", "eligible_task_classes", "assistive_task_classes", "shadow_task_classes"}
    with _lock:
        _dispatcher_config.update({key: deepcopy(value) for key, value in updates.items() if key in allowed})
        _dispatcher_config["enabled"] = _dispatcher_config["mode"] != "disabled"
        _dispatcher_config["answer_affecting_allowed"] = _dispatcher_config["mode"] == "assistive"
        _dispatcher_config["updated_at"] = datetime.now(timezone.utc).isoformat()
        _dispatcher_config["updated_by"] = actor
        _dispatcher_config["emergency_disabled"] = False
        return deepcopy(_dispatcher_config)


def emergency_disable_dispatcher(actor: str) -> dict[str, Any]:
    with _lock:
        now = datetime.now(timezone.utc).isoformat()
        _dispatcher_config.update({"mode": "disabled", "enabled": False, "answer_affecting_allowed": False, "emergency_disabled": True, "emergency_disabled_at": now, "emergency_disabled_by": actor, "updated_at": now, "updated_by": actor})
        return deepcopy(_dispatcher_config)


def dispatcher_dashboard(start_date: str | None, end_date: str | None, limit: int = 50) -> dict[str, Any]:
    traces = [{"created_at": f"2026-08-03T{18 - index:02d}:10:00+00:00", "trace_id": f"reference-dispatch-{index + 1:02d}", "turn_id": f"reference-turn-{index + 1:02d}", "conversation_id": f"reference-conversation-{index + 1:02d}", "mode": "shadow", "eligible": True, "planner_attempted": True, "planner_calls": 1, "accepted": index != 2, "answer_affecting": False, "assistive_task_class": ("ticket_investigation", "knowledge_lookup", "analytics_question")[index % 3], "fallback_reason": "policy_shadow_only" if index == 2 else None, "reason_codes": ["shadow_observation"], "latency_ms": 185 + index * 24, "estimated_cost_cad": 0.0018, "actual_cost_cad": 0.0018, "model_surface": "dispatcher", "result_set_saved": False, "result_set_comparison": False} for index in range(6)][:limit]
    class_exposure = []
    for task_class in _dispatcher_config["eligible_task_classes"]:
        class_exposure.append({"task_class": task_class, "candidate_turns": 8, "planner_attempted_turns": 8, "accepted_plans": 6, "rejected_plans": 2, "suppressed_or_locked_turns": 1, "answer_affecting_turns": 0, "quality_turns": 7, "quality_distribution": [{"name": "good", "count": 6}, {"name": "weak", "count": 1}], "reason_codes": [{"name": "shadow_observation", "count": 8}], "planner_latency": {"count": 8, "p50_ms": 210, "p90_ms": 290, "max_ms": 340}, "turn_latency": {"count": 8, "p50_ms": 3380, "p90_ms": 4820, "max_ms": 5510}})
    return {"config": dispatcher_config(), "range": {"timezone": "America/New_York", "start_date": start_date or "2026-07-28", "end_date": end_date or "2026-08-03", "start_at": f"{start_date or '2026-07-28'}T04:00:00+00:00", "end_at": f"{end_date or '2026-08-03'}T23:59:59+00:00", "max_days": 31}, "summary": {"trace_count": 24, "eligible_turns": 22, "planner_attempted_turns": 22, "accepted_plans": 17, "vetoed_or_rejected_plans": 5, "answer_affecting_turns": 0, "partial": False}, "mode_distribution": [{"name": "shadow", "count": 24}], "fallback_reasons": [{"name": "policy_shadow_only", "count": 5}], "reason_codes": [{"name": "shadow_observation", "count": 24}], "latency": {"count": 22, "p50_ms": 218, "p90_ms": 312, "max_ms": 388}, "turn_latency": {"count": 24, "p50_ms": 3410, "p90_ms": 4890, "max_ms": 5620}, "class_exposure": class_exposure, "quality_join": {"quality_event_count": 21, "unique_quality_answer_traces": 20, "duplicate_quality_events": 1, "matched_dispatcher_traces": 19}, "cost": {"estimated_total_cad": 0.0432, "actual_total_cad": 0.0432, "estimated_count": 24, "actual_count": 24}, "recent_traces": traces, "warnings": [], "caveats": ["Shadow mode evaluates plans without changing the final answer path."]}


def capture_config() -> dict[str, Any]:
    return deepcopy(_capture_config)


def update_capture_config(enabled: bool, allowed_emails: list[str], actor: str) -> dict[str, Any]:
    with _lock:
        _capture_config.update({"enabled": enabled, "allowed_emails": sorted(set(allowed_emails)), "updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": actor})
        return deepcopy(_capture_config)


def claims() -> dict[str, Any]:
    user = USER_BY_EMAIL[PLATFORM_ADMIN_EMAIL]
    return {
        "uid": REFERENCE_UID,
        "email": user.email,
        "name": user.name,
        "picture": None,
        "reference_runtime": True,
        "actual_role": user.role,
        "effective_role": user.role,
        "role": user.role,
        "role_preview": False,
        "can_preview_roles": True,
    }


def get_profile(uid: str, email: str, name: str, picture: str | None) -> UserProfile:
    with _lock:
        if uid not in _profiles:
            _profiles[uid] = UserProfile(
                uid=uid,
                email=email,
                name=name,
                display_name=name,
                picture=picture,
            ).model_dump()
        return UserProfile(**deepcopy(_profiles[uid]))


def update_profile(uid: str, updates: dict[str, Any]) -> None:
    with _lock:
        profile = _profiles.setdefault(uid, get_profile(uid, "", "", None).model_dump())
        profile.update(deepcopy(updates))


def add_open_tab(uid: str, tab_info: dict[str, Any], max_tabs: int = 8) -> tuple[list[dict], int | None]:
    with _lock:
        profile = _profiles.setdefault(uid, get_profile(uid, "", "", None).model_dump())
        tabs = [tab for tab in profile.get("open_tabs", []) if tab.get("id") != tab_info["id"]]
        evicted_id = tabs[-1].get("id") if len(tabs) >= max_tabs else None
        tabs.insert(0, deepcopy(tab_info))
        profile["open_tabs"] = tabs[:max_tabs]
        return deepcopy(profile["open_tabs"]), evicted_id


def remove_open_tab(uid: str, ticket_id: int) -> None:
    with _lock:
        profile = _profiles.setdefault(uid, get_profile(uid, "", "", None).model_dump())
        profile["open_tabs"] = [tab for tab in profile.get("open_tabs", []) if tab.get("id") != ticket_id]


def add_search_history(uid: str, query: str, search_type: str, max_items: int = 10) -> None:
    with _lock:
        profile = _profiles.setdefault(uid, get_profile(uid, "", "", None).model_dump())
        history = [
            item
            for item in profile.get("search_history", [])
            if not (item.get("query") == query and item.get("type") == search_type)
        ]
        history.insert(0, {"query": query, "type": search_type})
        profile["search_history"] = history[:max_items]


def reset() -> None:
    """Clear process-local state for tests."""
    global _maintenance_enabled
    with _lock:
        _profiles.clear()
        _feedback.clear()
        _dismissed_announcements.clear()
        _impact_events.clear()
        _gap_runs.clear()
        _notification_recipients[:] = _INITIAL_NOTIFICATION_RECIPIENTS
        _admin_users.clear()
        _admin_users.update(deepcopy(_INITIAL_ADMIN_USERS))
        _change_candidates.clear()
        _change_candidates.update(deepcopy(_INITIAL_CHANGE_CANDIDATES))
        _product_announcements.clear()
        _product_announcements.update(deepcopy(_INITIAL_PRODUCT_ANNOUNCEMENTS))
        _dispatcher_config.clear()
        _dispatcher_config.update(deepcopy(_INITIAL_DISPATCHER_CONFIG))
        _capture_config.clear()
        _capture_config.update(deepcopy(_INITIAL_CAPTURE_CONFIG))
        _kpi_delivery_config.clear()
        _kpi_delivery_config.update(deepcopy(_INITIAL_KPI_DELIVERY_CONFIG))
        _maintenance_enabled = False
