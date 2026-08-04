"""Regression coverage for resilient customer-risk model interpretation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
import requests
from fastapi import HTTPException

from app.models.ticket import StoreContext, TicketComment, TicketDetail
from app.routers import tickets
from app.services import error_provenance, ticket_customer_risk
from app.services.zendesk import ZendeskClient


def _ticket() -> TicketDetail:
    now = datetime.now(timezone.utc)
    return TicketDetail(
        id=950056,
        subject="Store needs an update",
        status="open",
        priority=None,
        ticket_type=None,
        ticket_form_name=None,
        created_at=now - timedelta(days=3),
        updated_at=now - timedelta(hours=2),
        sla_policy_name=None,
        resolution_sla_target_hours=None,
        resolution_sla_elapsed_hours=None,
        resolution_sla_breached=None,
        resolution_sla_status=None,
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
        comments=[
            TicketComment(
                id=1,
                author_name="Engineer",
                body="We are investigating.",
                is_internal=False,
                created_at=now - timedelta(hours=30),
            ),
            TicketComment(
                id=2,
                author_name="Store contact",
                body="Can you share an update?",
                is_internal=False,
                created_at=now - timedelta(hours=2),
            ),
        ],
    )


def _bq_data() -> dict:
    return {
        "ticket": {"escalated": False, "vendor_dispatched": False, "vendor_escalation": False},
        "comments": {
            1: {"author_role": "agent", "author_is_agent": True, "via_channel": "email"},
            2: {"author_role": "end-user", "author_is_agent": False, "via_channel": "email"},
        },
    }


def _get_risk(model_result):
    model = Mock(side_effect=model_result) if isinstance(model_result, Exception) else Mock(return_value=model_result)
    with (
        patch.object(ticket_customer_risk.zendesk, "get_ticket", return_value=_ticket()),
        patch.object(ticket_customer_risk, "_fetch_bq_risk_data", return_value=_bq_data()),
        patch.object(ticket_customer_risk.firestore, "get_cached_customer_risk", return_value=None),
        patch.object(ticket_customer_risk.firestore, "set_cached_customer_risk"),
        patch.object(ticket_customer_risk.gemini, "generate_customer_risk_assessment", model),
        patch.object(ticket_customer_risk.error_provenance, "record_event_best_effort") as provenance,
    ):
        risk = ticket_customer_risk.get_customer_risk(950056)
    return risk, model, provenance


def test_valid_model_interpretation_remains_available():
    risk, model, provenance = _get_risk(
        {
            "tone": "frustrated",
            "label": "Customer needs a response",
            "summary": "The customer is waiting for an update.",
            "reasons": ["Customer requested an update."],
            "next_action": "Post a public update.",
        }
    )

    assert model.call_count == 1
    assert provenance.call_count == 0
    assert risk.tone == "frustrated"
    assert risk.label == "Customer needs a response"
    assert "Customer requested an update." in risk.reasons


@pytest.mark.parametrize(
    "model_result",
    [
        pytest.param(json.JSONDecodeError("Unterminated string", "{", 1), id="truncated-json"),
        pytest.param(ValueError("schema validation failed"), id="value-error"),
        pytest.param(RuntimeError("model transport failure"), id="model-exception"),
        pytest.param({}, id="empty-object"),
        pytest.param([], id="non-object"),
    ],
)
def test_invalid_model_interpretation_uses_deterministic_fallback(model_result, caplog):
    facts = ticket_customer_risk._build_customer_risk_facts(_ticket(), _bq_data())
    with caplog.at_level("WARNING"):
        risk, model, provenance = _get_risk(model_result)

    assert model.call_count == 1
    provenance.assert_called_once()
    event = provenance.call_args.args[0]
    assert event["outcome"] == "esip_fallback_used"
    assert event["fallback_used"] is True
    assert risk.severity == facts["severity"]
    assert risk.score == facts["score"]
    assert risk.metrics.model_dump() == facts["metrics"].model_dump()
    assert risk.reasons == facts["deterministic_reasons"]
    assert risk.summary == "The available ticket activity shows customer-risk signals that need attention."
    assert "Customer risk model fallback:" in caplog.text
    assert "Can you share an update" not in caplog.text


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response._content = b'{"token":"SECRET","customer":"RAW CUSTOMER TEXT"}'
    response.headers["Authorization"] = "Bearer SECRET"
    return requests.HTTPError("SECRET exception text", response=response)


def test_error_provenance_outcome_matrix_is_compact_and_raw_free():
    events = [
        error_provenance.classify_exception(_http_error(404), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(_http_error(403), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(_http_error(410), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(_http_error(503), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(requests.Timeout("SECRET timeout"), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(requests.ConnectionError("SECRET connection"), endpoint_class="ticket_detail", source_hint="zendesk"),
        error_provenance.classify_exception(RuntimeError("SECRET internal"), endpoint_class="customer_risk_analytics"),
        error_provenance.fallback_event(
            endpoint_class="customer_risk_model",
            reason_code="customer_risk_model_invalid_payload",
        ),
    ]

    assert [event["outcome"] for event in events] == [
        "not_found",
        "access_denied_as_not_found",
        "gone_as_not_found",
        "upstream_http_error",
        "upstream_timeout",
        "upstream_connection_error",
        "esip_failure",
        "esip_fallback_used",
    ]
    assert events[3]["status_code"] == 503
    assert events[-1]["fallback_used"] is True
    rendered = repr(events)
    for forbidden in ("SECRET", "RAW CUSTOMER", "Authorization", "Bearer", "token"):
        assert forbidden not in rendered
    assert all(set(event) == {
        "source", "outcome", "endpoint_class", "status_code", "fallback_used", "reason_code"
    } for event in events)


@pytest.mark.parametrize(
    ("zendesk_status", "expected_http_status"),
    [(404, 404), (403, 404), (410, 404), (503, 502)],
)
def test_ticket_router_preserves_intended_zendesk_http_contract(zendesk_status, expected_http_status):
    with (
        patch.object(error_provenance, "record_event_best_effort"),
        pytest.raises(HTTPException) as raised,
    ):
        tickets._handle_zendesk_error(_http_error(zendesk_status), "ticket #950056")

    assert raised.value.status_code == expected_http_status


def test_provenance_persistence_failure_is_fail_open():
    event = error_provenance.fallback_event(
        endpoint_class="customer_risk_model",
        reason_code="customer_risk_model_presentation_error",
    )
    with patch("app.services.firestore.save_system_provenance_event", side_effect=RuntimeError("storage failed")):
        error_provenance.record_event_best_effort(event)


def test_zendesk_client_records_network_class_without_changing_exception():
    client = ZendeskClient()
    client._session.get = Mock(side_effect=requests.Timeout("RAW SECRET"))
    with (
        patch.object(error_provenance, "record_exception_best_effort") as recorder,
        pytest.raises(requests.Timeout),
    ):
        client._get("/tickets/950013.json")

    recorder.assert_called_once()
    assert recorder.call_args.kwargs == {
        "endpoint_class": "zendesk_ticket",
        "source_hint": "zendesk",
    }


def test_internal_ticket_failure_keeps_500_contract():
    with (
        patch.object(error_provenance, "record_event_best_effort"),
        pytest.raises(HTTPException) as raised,
    ):
        tickets._handle_zendesk_error(RuntimeError("existing internal detail"), "ticket #950056")

    assert raised.value.status_code == 500
    assert raised.value.detail == "existing internal detail"
