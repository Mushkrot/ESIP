"""Compact failure/fallback provenance with fail-open persistence."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

SOURCES = {"upstream_zendesk", "esip_model", "esip"}
OUTCOMES = {
    "not_found",
    "access_denied_as_not_found",
    "gone_as_not_found",
    "upstream_http_error",
    "upstream_timeout",
    "upstream_connection_error",
    "esip_fallback_used",
    "esip_failure",
}
ENDPOINT_CLASSES = {
    "zendesk_ticket",
    "zendesk_search",
    "zendesk_user",
    "zendesk_organization",
    "zendesk_ticket_form",
    "zendesk_incremental",
    "zendesk_other",
    "ticket_detail",
    "ticket_customer_risk",
    "ticket_history",
    "ticket_store",
    "customer_risk_model",
    "customer_risk_analytics",
    "unknown",
}
REASON_CODES = {
    "zendesk_http_404",
    "zendesk_http_403",
    "zendesk_http_410",
    "zendesk_http_5xx",
    "zendesk_http_other",
    "zendesk_timeout",
    "zendesk_connection_error",
    "zendesk_client_unclassified",
    "customer_risk_model_invalid_payload",
    "customer_risk_model_presentation_error",
    "esip_internal_exception",
    "unknown_failure",
}


def zendesk_endpoint_class(path: str) -> str:
    """Map a Zendesk path to a bounded class without preserving IDs or query text."""
    clean = str(path or "").split("?", 1)[0].strip("/")
    root = clean.split("/", 1)[0]
    return {
        "tickets": "zendesk_ticket",
        "search.json": "zendesk_search",
        "users": "zendesk_user",
        "organizations": "zendesk_organization",
        "ticket_forms": "zendesk_ticket_form",
        "incremental": "zendesk_incremental",
    }.get(root, "zendesk_other")


def compact_event(
    *,
    source: str,
    outcome: str,
    endpoint_class: str,
    reason_code: str,
    status_code: int | None = None,
    fallback_used: bool = False,
) -> dict[str, Any]:
    """Return the complete allowlisted event schema; free text is never accepted."""
    safe_source = source if source in SOURCES else "esip"
    safe_outcome = outcome if outcome in OUTCOMES else "esip_failure"
    try:
        safe_status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        safe_status = None
    if safe_status is not None and not 100 <= safe_status <= 599:
        safe_status = None
    return {
        "source": safe_source,
        "outcome": safe_outcome,
        "endpoint_class": endpoint_class if endpoint_class in ENDPOINT_CLASSES else "unknown",
        "status_code": safe_status,
        "fallback_used": bool(fallback_used),
        "reason_code": reason_code if reason_code in REASON_CODES else "unknown_failure",
    }


def classify_exception(
    exc: Exception,
    *,
    endpoint_class: str,
    source_hint: str = "esip",
) -> dict[str, Any]:
    """Classify known Zendesk/network failures without inspecting response content."""
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        mapping = {
            404: ("not_found", "zendesk_http_404"),
            403: ("access_denied_as_not_found", "zendesk_http_403"),
            410: ("gone_as_not_found", "zendesk_http_410"),
        }
        outcome, reason = mapping.get(status_code, ("upstream_http_error", "zendesk_http_5xx"))
        if not isinstance(status_code, int) or status_code < 500:
            reason = reason if status_code in mapping else "zendesk_http_other"
        return compact_event(
            source="upstream_zendesk",
            outcome=outcome,
            endpoint_class=endpoint_class,
            status_code=status_code,
            fallback_used=False,
            reason_code=reason,
        )
    if isinstance(exc, requests.Timeout):
        return compact_event(
            source="upstream_zendesk",
            outcome="upstream_timeout",
            endpoint_class=endpoint_class,
            reason_code="zendesk_timeout",
        )
    if isinstance(exc, requests.ConnectionError):
        return compact_event(
            source="upstream_zendesk",
            outcome="upstream_connection_error",
            endpoint_class=endpoint_class,
            reason_code="zendesk_connection_error",
        )
    return compact_event(
        source="upstream_zendesk" if source_hint == "zendesk" else "esip",
        outcome="esip_failure",
        endpoint_class=endpoint_class,
        reason_code="zendesk_client_unclassified" if source_hint == "zendesk" else "esip_internal_exception",
    )


def fallback_event(*, endpoint_class: str, reason_code: str) -> dict[str, Any]:
    return compact_event(
        source="esip_model",
        outcome="esip_fallback_used",
        endpoint_class=endpoint_class,
        fallback_used=True,
        reason_code=reason_code,
    )


def record_event_best_effort(event: dict[str, Any]) -> None:
    """Persist/log compact metadata only; telemetry must never affect behavior."""
    safe_event = compact_event(**{
        key: event.get(key)
        for key in ("source", "outcome", "endpoint_class", "reason_code", "status_code", "fallback_used")
    })
    try:
        from app.services import firestore  # Local import avoids service initialization cycles.

        firestore.save_system_provenance_event(safe_event)
    except Exception:
        logger.warning("system_provenance persistence_failed event=%s", json.dumps(safe_event, sort_keys=True))
        return
    logger.info("system_provenance event=%s", json.dumps(safe_event, sort_keys=True))


def record_exception_best_effort(
    exc: Exception,
    *,
    endpoint_class: str,
    source_hint: str = "esip",
) -> dict[str, Any]:
    event = classify_exception(exc, endpoint_class=endpoint_class, source_hint=source_hint)
    if not bool(getattr(exc, "_esip_provenance_recorded", False)):
        record_event_best_effort(event)
        try:
            setattr(exc, "_esip_provenance_recorded", True)
        except Exception:
            pass
    return event
