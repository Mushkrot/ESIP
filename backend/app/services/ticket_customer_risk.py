"""Per-ticket customer frustration/sentiment risk assessment."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery as bq

from app.models.customer_risk import CustomerRiskMetrics, CustomerRiskResponse
from app.models.ticket import TicketDetail
from app.services import error_provenance, firestore, gemini
from app.services.bigquery import BQ_DATASET, GCP_PROJECT, _get_client
from app.services.zendesk import zendesk

SCORE_VERSION = "customer_risk_score_v2"
PROMPT_VERSION = gemini.CUSTOMER_RISK_PROMPT_VERSION
BUSINESS_TZ = ZoneInfo("America/New_York")
ACTIVE_STATUSES = {"new", "open", "pending", "hold"}
logger = logging.getLogger(__name__)


def get_customer_risk(ticket_id: int, refresh: bool = False) -> CustomerRiskResponse:
    """Return cached or freshly generated customer risk assessment."""
    ticket = zendesk.get_ticket(ticket_id)
    bq_data = _fetch_bq_risk_data(ticket_id)
    facts = _build_customer_risk_facts(ticket, bq_data)

    if not refresh:
        cached = firestore.get_cached_customer_risk(ticket_id)
        if _usable_cache(cached, facts["comment_fingerprint"]):
            cached_payload = dict(cached or {})
            cached_payload["from_cache"] = True
            return CustomerRiskResponse(**cached_payload)

    interpretation = _generate_interpretation_or_fallback(facts)
    payload = _compose_response_payload(ticket, facts, interpretation)
    firestore.set_cached_customer_risk(ticket_id, payload, ticket.status)
    return CustomerRiskResponse(**payload)


def build_customer_risk_from_precomputed(
    ticket: TicketDetail,
    bq_data: dict[str, Any],
    interpretation: dict[str, Any],
) -> CustomerRiskResponse:
    """Test helper: compose a response without network or Gemini calls."""
    facts = _build_customer_risk_facts(ticket, bq_data)
    return CustomerRiskResponse(**_compose_response_payload(ticket, facts, interpretation))


def _generate_interpretation_or_fallback(facts: dict[str, Any]) -> dict[str, Any]:
    """Use model copy when valid, otherwise preserve the fact-based assessment."""
    try:
        interpretation = gemini.generate_customer_risk_assessment(_gemini_input(facts))
        if not isinstance(interpretation, dict) or not interpretation:
            raise ValueError("Customer risk model returned an empty or non-object payload")
        return interpretation
    except Exception as exc:
        # The assessment facts have already been collected. Do not retry the model
        # or include model/customer content in logs when only presentation failed.
        logger.warning("Customer risk model fallback: %s", exc.__class__.__name__)
        if isinstance(exc, ValueError):
            reason_code = "customer_risk_model_invalid_payload"
        else:
            reason_code = "customer_risk_model_presentation_error"
        error_provenance.record_event_best_effort(error_provenance.fallback_event(
            endpoint_class="customer_risk_model",
            reason_code=reason_code,
        ))
        return _deterministic_interpretation(facts)


def _deterministic_interpretation(facts: dict[str, Any]) -> dict[str, Any]:
    severity = str(facts["severity"])
    metrics: CustomerRiskMetrics = facts["metrics"]
    tone = "calm" if severity == "green" else "concerned"
    if not metrics.is_active:
        summary = "The available ticket activity does not show an active customer-risk signal."
    elif severity == "green":
        summary = "The available ticket activity does not show repeated customer pressure or a stale public reply."
    else:
        summary = "The available ticket activity shows customer-risk signals that need attention."
    return {
        "tone": tone,
        "label": "",
        "summary": summary,
        "reasons": [],
        "next_action": "",
    }


def score_customer_risk_metrics(metrics: CustomerRiskMetrics) -> tuple[int, str]:
    """Public scoring wrapper used by chat analytics to match the ticket panel."""
    return _score_metrics(metrics)


def customer_risk_reasons(metrics: CustomerRiskMetrics) -> list[str]:
    """Public reason wrapper used by chat analytics to match the ticket panel."""
    return _deterministic_reasons(metrics)


def _usable_cache(cached: dict | None, comment_fingerprint: str) -> bool:
    if not cached:
        return False
    return (
        cached.get("comment_fingerprint") == comment_fingerprint
        and cached.get("score_version") == SCORE_VERSION
        and cached.get("prompt_version") == PROMPT_VERSION
    )


def _fetch_bq_risk_data(ticket_id: int) -> dict[str, Any]:
    client = _get_client()
    ticket_sql = f"""
    SELECT escalated, vendor_dispatched, vendor_escalation
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id = @ticket_id
    LIMIT 1
    """
    comments_sql = f"""
    SELECT
      comment_id,
      author_role,
      author_is_agent,
      via_channel
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments`
    WHERE ticket_id = @ticket_id
    """
    cfg = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("ticket_id", "INT64", int(ticket_id))]
    )
    ticket_rows = list(client.query(ticket_sql, job_config=cfg).result())
    comment_rows = list(client.query(comments_sql, job_config=cfg).result())

    ticket_meta: dict[str, Any] = {}
    if ticket_rows:
        row = ticket_rows[0]
        ticket_meta = {
            "escalated": bool(getattr(row, "escalated", False)),
            "vendor_dispatched": bool(getattr(row, "vendor_dispatched", False)),
            "vendor_escalation": bool(getattr(row, "vendor_escalation", False)),
        }
    comments = {
        int(row.comment_id): {
            "author_role": row.author_role,
            "author_is_agent": row.author_is_agent,
            "via_channel": row.via_channel,
        }
        for row in comment_rows
        if getattr(row, "comment_id", None) is not None
    }
    return {"ticket": ticket_meta, "comments": comments}


def _build_customer_risk_facts(ticket: TicketDetail, bq_data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    comment_meta: dict[int, dict[str, Any]] = bq_data.get("comments") or {}
    ticket_meta: dict[str, Any] = bq_data.get("ticket") or {}

    public_comments: list[dict[str, Any]] = []
    customer_comments: list[dict[str, Any]] = []
    agent_comments: list[dict[str, Any]] = []
    enriched_comments: list[dict[str, Any]] = []

    for comment in ticket.comments:
        meta = comment_meta.get(int(comment.id), {})
        row = {
            "id": int(comment.id),
            "author_name": comment.author_name or "Unknown",
            "body": _clean_comment_text(comment.body),
            "created_at": _ensure_utc(comment.created_at),
            "is_internal": bool(comment.is_internal),
            "author_role": meta.get("author_role"),
            "author_is_agent": meta.get("author_is_agent"),
        }
        enriched_comments.append(row)
        if row["is_internal"]:
            continue
        public_comments.append(row)
        if _is_customer_public_comment(row):
            customer_comments.append(row)
        elif _is_agent_public_comment(row):
            agent_comments.append(row)

    last_public = public_comments[-1] if public_comments else None
    last_customer = customer_comments[-1] if customer_comments else None
    last_agent = agent_comments[-1] if agent_comments else None
    if last_public is None:
        last_public_author = "none"
    elif _is_customer_public_comment(last_public):
        last_public_author = "customer"
    elif _is_agent_public_comment(last_public):
        last_public_author = "agent"
    else:
        last_public_author = "unknown"

    if last_agent:
        followups_after_last_agent = sum(
            1 for c in customer_comments if c["created_at"] > last_agent["created_at"]
        )
    else:
        followups_after_last_agent = len(customer_comments)

    status = (ticket.status or "").lower()
    is_active = status in ACTIVE_STATUSES
    active_age_hours = _hours_between(ticket.created_at, now) if is_active else None
    hours_since_agent = _hours_between(last_agent["created_at"], now) if last_agent else None
    hours_since_customer = _hours_between(last_customer["created_at"], now) if last_customer else None

    metrics = CustomerRiskMetrics(
        public_customer_comments=len(customer_comments),
        public_agent_replies=len(agent_comments),
        customer_followups_after_last_agent=followups_after_last_agent,
        last_public_author=last_public_author,
        hours_since_last_public_agent_reply=hours_since_agent,
        hours_since_last_public_customer_comment=hours_since_customer,
        active_ticket_age_hours=active_age_hours,
        is_active=is_active,
        is_escalated=bool(ticket_meta.get("escalated", ticket.is_escalated)),
        vendor_dispatched=bool(ticket_meta.get("vendor_dispatched", False)),
        vendor_escalation=bool(ticket_meta.get("vendor_escalation", False)),
        sla_policy_name=ticket.sla_policy_name,
        resolution_sla_target_hours=ticket.resolution_sla_target_hours,
        resolution_sla_elapsed_hours=ticket.resolution_sla_elapsed_hours,
        resolution_sla_hours_remaining=_sla_hours_remaining(ticket),
        resolution_sla_progress_pct=_sla_progress_pct(ticket),
        resolution_sla_breached=ticket.resolution_sla_breached,
        resolution_sla_status=ticket.resolution_sla_status,
    )
    score, severity = _score_metrics(metrics)
    deterministic_reasons = _deterministic_reasons(metrics)

    return {
        "ticket": ticket,
        "metrics": metrics,
        "score": score,
        "severity": severity,
        "deterministic_reasons": deterministic_reasons,
        "customer_comment_excerpts": _customer_comment_excerpts(customer_comments),
        "comment_fingerprint": _comment_fingerprint(ticket, enriched_comments),
    }


def _score_metrics(metrics: CustomerRiskMetrics) -> tuple[int, str]:
    score = 0
    count = metrics.public_customer_comments
    if count >= 10:
        score += 35
    elif count >= 5:
        score += 25
    elif count >= 3:
        score += 15
    elif count >= 1:
        score += 5

    followups = metrics.customer_followups_after_last_agent
    if followups >= 4:
        score += 30
    elif followups >= 2:
        score += 20
    elif followups >= 1:
        score += 10

    if metrics.last_public_author == "customer":
        score += 15

    hours = metrics.hours_since_last_public_agent_reply
    if metrics.is_active:
        if hours is None:
            score += 20
        elif hours >= 168:
            score += 45
        elif hours >= 72:
            score += 35
        elif hours >= 48:
            score += 30
        elif hours >= 24:
            score += 20

    if metrics.active_ticket_age_hours is not None:
        if metrics.active_ticket_age_hours >= 168:
            score += 10
        elif metrics.active_ticket_age_hours >= 48:
            score += 5

    if metrics.is_escalated:
        score += 10
    if metrics.vendor_dispatched:
        score += 8
    if metrics.vendor_escalation:
        score += 10

    if metrics.is_active:
        if metrics.resolution_sla_status == "open_breached" or metrics.resolution_sla_breached is True:
            score += 20
            score = max(score, 45)
        elif metrics.resolution_sla_status == "open_within_target":
            progress = metrics.resolution_sla_progress_pct
            remaining = metrics.resolution_sla_hours_remaining
            if (progress is not None and progress >= 90) or (remaining is not None and remaining <= 2):
                score += 15
            elif (progress is not None and progress >= 75) or (remaining is not None and remaining <= 6):
                score += 10

    if (
        metrics.is_active
        and metrics.customer_followups_after_last_agent >= 2
        and (metrics.hours_since_last_public_agent_reply is None or metrics.hours_since_last_public_agent_reply >= 24)
    ):
        score = max(score, 80)
    if metrics.is_active and metrics.public_customer_comments >= 10 and metrics.last_public_author == "customer":
        score = max(score, 85)
    if (
        metrics.is_active
        and (metrics.resolution_sla_status == "open_breached" or metrics.resolution_sla_breached is True)
        and (metrics.last_public_author == "customer" or metrics.customer_followups_after_last_agent >= 1)
    ):
        score = max(score, 70)

    score = max(0, min(100, int(score)))
    if score >= 70:
        severity = "red"
    elif score >= 45:
        severity = "orange"
    elif score >= 20:
        severity = "yellow"
    else:
        severity = "green"
    return score, severity


def _compose_response_payload(
    ticket: TicketDetail,
    facts: dict[str, Any],
    interpretation: dict[str, Any],
) -> dict[str, Any]:
    metrics: CustomerRiskMetrics = facts["metrics"]
    tone = _normalize_tone(interpretation.get("tone"))
    score = int(facts["score"])
    severity = str(facts["severity"])

    if tone == "escalated" and score >= 45:
        severity = "red"
        score = max(score, 75)
    elif tone == "frustrated" and score >= 55:
        severity = "red"
        score = max(score, 70)
    elif tone in {"frustrated", "escalated"} and score >= 35:
        severity = "orange"
        score = max(score, 45)
    elif tone == "concerned" and score < 20:
        severity = "yellow"
        score = 20

    label = _label_for(severity, interpretation.get("label"))
    reasons = _merge_reasons(facts["deterministic_reasons"], interpretation.get("reasons"))
    next_action = _guard_next_action(str(interpretation.get("next_action") or ""), severity, metrics)
    summary = _clean_sentence(str(interpretation.get("summary") or "Customer risk assessment is available."))

    return {
        "ticket_id": int(ticket.id),
        "severity": severity,
        "score": score,
        "label": label,
        "tone": tone,
        "summary": summary,
        "reasons": reasons,
        "next_action": next_action,
        "metrics": metrics.model_dump(),
        "generated_at": datetime.now(timezone.utc),
        "from_cache": False,
        "prompt_version": PROMPT_VERSION,
        "score_version": SCORE_VERSION,
        "comment_fingerprint": facts["comment_fingerprint"],
    }


def _gemini_input(facts: dict[str, Any]) -> dict[str, Any]:
    ticket: TicketDetail = facts["ticket"]
    store = ticket.store
    return {
        "ticket": {
            "id": ticket.id,
            "subject": ticket.subject,
            "status": ticket.status,
            "store_id": store.store_id if store else None,
            "org_name": store.org_name if store else None,
            "created_at": ticket.created_at.isoformat(),
            "updated_at": ticket.updated_at.isoformat(),
        },
        "deterministic_score": facts["score"],
        "deterministic_severity": facts["severity"],
        "metrics": facts["metrics"].model_dump(),
        "deterministic_reasons": facts["deterministic_reasons"],
        "public_customer_comment_excerpts": facts["customer_comment_excerpts"],
    }


def _deterministic_reasons(metrics: CustomerRiskMetrics) -> list[str]:
    reasons: list[str] = []
    if metrics.public_customer_comments >= 3:
        reasons.append(f"{metrics.public_customer_comments} public customer comments")
    elif metrics.public_customer_comments == 1:
        reasons.append("1 public customer comment")

    if metrics.customer_followups_after_last_agent >= 2:
        reasons.append(f"{metrics.customer_followups_after_last_agent} customer follow-ups after the last public agent reply")
    elif metrics.customer_followups_after_last_agent == 1:
        reasons.append("Customer followed up after the last public agent reply")

    if metrics.last_public_author == "customer":
        reasons.append("Last public comment came from the customer")
    if metrics.is_active and metrics.hours_since_last_public_agent_reply is None:
        reasons.append("No public agent reply found")
    elif metrics.is_active and metrics.hours_since_last_public_agent_reply is not None and metrics.hours_since_last_public_agent_reply >= 24:
        reasons.append(f"Last public agent reply was {int(metrics.hours_since_last_public_agent_reply)} hours ago")

    if metrics.is_active and (metrics.resolution_sla_status == "open_breached" or metrics.resolution_sla_breached is True):
        if metrics.resolution_sla_target_hours is not None and metrics.resolution_sla_elapsed_hours is not None:
            reasons.append(
                f"Resolution SLA breached: {metrics.resolution_sla_elapsed_hours:.1f}h elapsed vs {metrics.resolution_sla_target_hours:g}h target"
            )
        else:
            reasons.append("Resolution SLA is breached")
    elif metrics.is_active and metrics.resolution_sla_status == "open_within_target":
        remaining = metrics.resolution_sla_hours_remaining
        if remaining is not None and remaining <= 6:
            reasons.append(f"Resolution SLA has {remaining:.1f}h remaining")
    if metrics.is_escalated:
        reasons.append("Ticket is internally escalated")
    if metrics.vendor_dispatched:
        reasons.append("Vendor dispatch is marked on the ticket")
    if metrics.vendor_escalation:
        reasons.append("Vendor escalation is marked on the ticket")

    if not reasons:
        reasons.append("No repeated customer follow-up or stale public-reply signal")
    return reasons[:5]


def _merge_reasons(deterministic: list[str], gemini_reasons: object) -> list[str]:
    out: list[str] = []
    for value in [*deterministic, *(gemini_reasons if isinstance(gemini_reasons, list) else [])]:
        text = _clean_sentence(str(value or ""))
        if not text:
            continue
        if text.lower() in {item.lower() for item in out}:
            continue
        out.append(text)
        if len(out) >= 5:
            break
    return out


def _guard_next_action(action: str, severity: str, metrics: CustomerRiskMetrics) -> str:
    clean = _clean_sentence(action)
    sla_breached = metrics.is_active and (
        metrics.resolution_sla_status == "open_breached" or metrics.resolution_sla_breached is True
    )
    sla_near_breach = (
        metrics.is_active
        and metrics.resolution_sla_status == "open_within_target"
        and metrics.resolution_sla_hours_remaining is not None
        and metrics.resolution_sla_hours_remaining <= 6
    )
    delayed_public_reply = (
        metrics.is_active
        and metrics.last_public_author == "customer"
        and (metrics.hours_since_last_public_agent_reply is None or metrics.hours_since_last_public_agent_reply >= 24)
    )
    if sla_breached and delayed_public_reply:
        return "Acknowledge the SLA breach with a public update or call the store before continuing internal-only work."
    if sla_breached:
        return "Review the blocker and post a clear public update with current status and the next resolution step."
    if sla_near_breach and metrics.last_public_author == "customer":
        return "Post a public update before the SLA target is reached and confirm the next resolution step."
    if severity == "red" and delayed_public_reply:
        return "Post a public update or call the store before continuing internal-only work."
    if not clean:
        if metrics.last_public_author == "customer":
            return "Post a clear public update with current status and next steps."
        return "Continue monitoring and keep the next public update clear."
    return clean


def _label_for(severity: str, model_label: object) -> str:
    labels = {
        "green": "Normal customer risk",
        "yellow": "Watch customer risk",
        "orange": "Elevated customer risk",
        "red": "High customer frustration risk",
    }
    label = _clean_sentence(str(model_label or ""))
    if (
        label
        and len(label) <= 80
        and label.lower() not in {"green", "yellow", "orange", "red"}
        and not (severity != "green" and "normal" in label.lower())
    ):
        return label
    return labels.get(severity, "Customer risk")


def _normalize_tone(value: object) -> str:
    tone = str(value or "").strip().lower()
    if tone in {"calm", "concerned", "frustrated", "escalated"}:
        return tone
    return "concerned"


def _customer_comment_excerpts(customer_comments: list[dict[str, Any]]) -> list[dict[str, str]]:
    selected = customer_comments[-6:]
    return [
        {
            "created_at": _format_et(row["created_at"]),
            "author_name": str(row.get("author_name") or "Customer"),
            "excerpt": row["body"][:500],
        }
        for row in selected
        if row.get("body")
    ]


def _comment_fingerprint(ticket: TicketDetail, comments: list[dict[str, Any]]) -> str:
    raw = "|".join(
        [
            str(ticket.id),
            ticket.updated_at.isoformat(),
            str(ticket.resolution_sla_target_hours),
            str(ticket.resolution_sla_elapsed_hours),
            str(ticket.resolution_sla_breached),
            str(ticket.resolution_sla_status),
            *[
                f"{c['id']}:{c['created_at'].isoformat()}:{c.get('author_role')}:{c.get('author_is_agent')}"
                for c in comments
            ],
        ]
    )
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _is_customer_public_comment(row: dict[str, Any]) -> bool:
    role = str(row.get("author_role") or "").lower()
    if role == "end-user":
        return True
    return row.get("author_is_agent") is False


def _is_agent_public_comment(row: dict[str, Any]) -> bool:
    role = str(row.get("author_role") or "").lower()
    if role in {"agent", "admin"}:
        return True
    return row.get("author_is_agent") is True


def _hours_between(start: datetime, end: datetime) -> float:
    return round(max(0.0, (_ensure_utc(end) - _ensure_utc(start)).total_seconds() / 3600.0), 2)


def _sla_hours_remaining(ticket: TicketDetail) -> float | None:
    target = ticket.resolution_sla_target_hours
    elapsed = ticket.resolution_sla_elapsed_hours
    if target is None or elapsed is None:
        return None
    return round(float(target) - float(elapsed), 2)


def _sla_progress_pct(ticket: TicketDetail) -> float | None:
    target = ticket.resolution_sla_target_hours
    elapsed = ticket.resolution_sla_elapsed_hours
    if target is None or elapsed is None or target <= 0:
        return None
    return round(max(0.0, float(elapsed) / float(target) * 100.0), 1)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_et(value: datetime) -> str:
    return _ensure_utc(value).astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %H:%M ET")


def _clean_comment_text(value: str) -> str:
    text = re.sub(r"https?://\S+", "", value or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_sentence(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:500]
