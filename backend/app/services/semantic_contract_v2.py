"""Semantic task contracts for Universal Chat routing.

The v2 contract converts the existing AI semantic frame plus already-computed
deterministic guard signals into a single traceable routing contract. Most
uses are still observational; narrow routing gates can opt into the contract
only after regression coverage is in place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SemanticContractV2:
    version: str
    mode: str
    intent: str
    data_domain: str
    scope: str
    executor: str
    output: str
    time_range: dict[str, Any] | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"
    source: str = "semantic_frame_shadow"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["filters"] = {key: value for key, value in self.filters.items() if value not in (None, "", [])}
        return payload


def build_shadow_contract(
    *,
    latest_question: str,
    routing_question: str,
    semantic_frame: dict[str, Any],
    explicit_ticket_id: int | None = None,
    open_ticket_id: int | None = None,
    target_ticket_id: int | None = None,
    explicit_store_context: str | None = None,
    explicit_store_ids: list[str] | tuple[str, ...] | None = None,
    ticket_list_analytics: bool = False,
    public_update_staleness: bool = False,
    force_context_only: bool = False,
    semantic_context_only: bool = False,
    task_contract: Any | None = None,
    customer_risk_request: Any | None = None,
    global_event_request: dict[str, Any] | None = None,
    temporal_resolution: Any | None = None,
    selected_executor: str | None = None,
) -> SemanticContractV2:
    """Build an observational contract from the current semantic/router signals."""
    entities = _entities(semantic_frame)
    task = _task(semantic_frame)
    retrieval = _retrieval(semantic_frame)
    text = f"{latest_question}\n{routing_question}".lower()
    explicit_store_list = _clean_store_id_list(explicit_store_ids)
    entity_store_list = _clean_store_id_list(entities.get("store_ids"))
    if not entity_store_list:
        entity_store_list = _clean_store_id_list(entities.get("store_id"))
    store_ids = explicit_store_list or entity_store_list
    single_store_id = (
        _clean(explicit_store_context)
        or (store_ids[0] if len(store_ids) == 1 else None)
        or (_clean(entities.get("store_id")) if not store_ids else None)
    )
    base_filters = {
        "explicit_ticket_id": explicit_ticket_id,
        "open_ticket_id": open_ticket_id,
        "target_ticket_id": target_ticket_id,
        "store_id": single_store_id,
        "store_ids": store_ids,
        "semantic_issue_or_category": entities.get("issue_or_category"),
    }
    time_range = _time_range(entities, temporal_resolution)
    confidence = _clean(semantic_frame.get("confidence")) or "low"
    scope = _scope(semantic_frame, explicit_store_context, store_ids, explicit_ticket_id, target_ticket_id)

    if customer_risk_request:
        filters = {
            **base_filters,
            "customer_risk_scope": getattr(customer_risk_request, "scope", None),
            "customer_public_followups_min": getattr(customer_risk_request, "followup_min", None),
            "require_after_agent_followups": getattr(customer_risk_request, "require_after_agent_followups", None),
            "since_days": getattr(customer_risk_request, "since_days", None),
            "limit": getattr(customer_risk_request, "limit", None),
        }
        return _contract(
            intent="analyze_customer_risk",
            data_domain="zendesk_tickets",
            scope=_customer_risk_scope(customer_risk_request, scope),
            executor="customer_risk_analytics",
            output="risk_summary" if getattr(customer_risk_request, "scope", None) == "ticket" else "risk_list",
            time_range=time_range,
            filters=filters,
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Customer Risk detector matched public-comment/frustration-risk wording.",
        )

    if task_contract and getattr(task_contract, "dimension_type", None) == "semantic_labels":
        return _contract(
            intent="count_by_dimension",
            data_domain="zendesk_tickets",
            scope="store",
            executor="semantic_label_count",
            output="label_counts_with_ticket_ids",
            time_range=_period_contract_time_range(getattr(task_contract, "period", None)) or time_range,
            filters={
                **base_filters,
                "store_id": getattr(task_contract, "store_id", None) or base_filters.get("store_id"),
                "dimension_type": "semantic_labels",
                "labels": list(getattr(task_contract, "labels", None) or []),
                "overlap_allowed": bool(getattr(task_contract, "overlap_allowed", False)),
            },
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Existing semantic-label task contract is executable.",
        )

    if public_update_staleness:
        return _contract(
            intent="list_tickets",
            data_domain="zendesk_ticket_comments",
            scope="global",
            executor="public_update_analytics",
            output="ticket_list",
            time_range=time_range,
            filters={**base_filters, "status": "not_closed", "public_update_stale": True},
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Question asks for unresolved tickets missing a recent public update.",
        )

    if ticket_list_analytics:
        count_request = _asks_for_ticket_count(text)
        return _contract(
            intent="count_tickets" if count_request else "list_tickets",
            data_domain="zendesk_tickets",
            scope=scope if scope in {"store", "global", "prior_turn"} else "global",
            executor="ticket_analytics",
            output="ticket_count" if count_request else ("ticket_numbers" if _asks_for_ticket_numbers(text) else "ticket_list"),
            time_range=time_range,
            filters=base_filters,
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Question asks to list Zendesk tickets; event words remain filters/topics, not router triggers.",
        )

    if global_event_request:
        return _contract(
            intent="discover_event",
            data_domain="zendesk_tickets",
            scope="global",
            executor="event_discovery",
            output="event_candidate_stores",
            time_range=_period_dict_time_range(global_event_request.get("period")) or time_range,
            filters={**base_filters, "event_terms": list(global_event_request.get("terms") or [])},
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Global event discovery detector matched an event/fact-discovery ask.",
        )

    if target_ticket_id and (force_context_only or semantic_context_only):
        return _contract(
            intent="ticket_details",
            data_domain="zendesk_tickets",
            scope="explicit_ticket" if explicit_ticket_id else "open_ticket",
            executor="direct_ticket_context",
            output="ticket_answer",
            time_range=time_range,
            filters={**base_filters, "ticket_id": target_ticket_id},
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Question can be answered from direct ticket context.",
        )

    task_type = _clean(task.get("task_type")) or "general_answer"
    if task_type == "count_by_dimension":
        return _contract(
            intent="count_by_dimension",
            data_domain="zendesk_tickets",
            scope=scope,
            executor="ticket_analytics",
            output="aggregate_counts",
            time_range=time_range,
            filters={
                **base_filters,
                "dimension_type": task.get("dimension_type"),
                "dimension_source": task.get("dimension_source"),
                "labels": task.get("labels") if isinstance(task.get("labels"), list) else [],
            },
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Semantic frame requested a count/grouping task.",
        )

    intent = _clean(semantic_frame.get("intent")) or "general"
    if intent == "ticket_retrieval" or _has_retrieval_concepts(retrieval):
        retrieval_filters = {
            **base_filters,
            "search_goal": retrieval.get("search_goal"),
            "required_concepts": _clean_list(retrieval.get("required_concepts")),
            "optional_concepts": _clean_list(retrieval.get("optional_concepts")),
            "vendors": _clean_list(retrieval.get("vendors")),
            "devices": _clean_list(retrieval.get("devices")),
            "actions": _clean_list(retrieval.get("actions")),
        }
        retrieval_filters = _enrich_ticket_retrieval_filters(retrieval_filters, text)
        if not _retrieval_goal_allows_ticket_search(retrieval_filters):
            return _contract(
                intent="document_retrieval",
                data_domain="documents",
                scope="global",
                executor="phase1_tools",
                output="document_answer",
                time_range=time_range,
                filters=retrieval_filters,
                confidence=confidence,
                selected_executor=selected_executor,
                reason="Semantic frame requested retrieval, but document markers require document/knowledge tools rather than ticket search.",
            )
        return _contract(
            intent="ticket_retrieval",
            data_domain="zendesk_tickets",
            scope=_retrieval_scope(scope, retrieval_filters),
            executor="ticket_semantic_search",
            output=_clean(retrieval.get("output")) or "ticket_examples",
            time_range=time_range,
            filters=retrieval_filters,
            confidence=confidence,
            selected_executor=selected_executor,
            reason="Semantic frame requested scenario-based ticket retrieval.",
        )

    executor = _executor_from_intent(intent, target_ticket_id)
    return _contract(
        intent=_contract_intent_from_semantic(intent),
        data_domain=_domain_from_executor(executor),
        scope=scope,
        executor=executor,
        output=_output_from_executor(executor),
        time_range=time_range,
        filters=base_filters,
        confidence=confidence,
        selected_executor=selected_executor,
        reason="Default contract compiled from semantic frame.",
    )


def selected_executor_label(
    *,
    force_semantic_label_count: bool = False,
    force_customer_risk: bool = False,
    force_global_event_discovery: bool = False,
    force_ticket_semantic_search: bool = False,
    public_update_staleness: bool = False,
    temporal_ticket_analytics: bool = False,
    force_context_only: bool = False,
    semantic_context_only: bool = False,
    schema_limitation: bool = False,
) -> str:
    """Return a trace label for the current production routing path."""
    if force_semantic_label_count:
        return "semantic_label_count"
    if force_customer_risk:
        return "customer_risk_analytics"
    if force_global_event_discovery:
        return "event_discovery"
    if force_ticket_semantic_search:
        return "ticket_semantic_search"
    if schema_limitation:
        return "schema_limited_analytics"
    if public_update_staleness:
        return "public_update_analytics"
    if temporal_ticket_analytics:
        return "ticket_analytics"
    if force_context_only or semantic_context_only:
        return "direct_ticket_context"
    return "phase1_tools"


def authorizes_global_event_discovery(
    contract: SemanticContractV2,
    *,
    global_event_request: dict[str, Any] | None,
    force_customer_risk: bool,
) -> bool:
    """Return whether contract-v2 allows the event-discovery force route."""
    return bool(
        global_event_request
        and not force_customer_risk
        and contract.intent == "discover_event"
        and contract.executor == "event_discovery"
        and contract.scope == "global"
    )


def authorizes_customer_risk_analytics(
    contract: SemanticContractV2,
    *,
    customer_risk_request: Any | None,
) -> bool:
    """Return whether contract-v2 allows the Customer Risk analytics force route."""
    return bool(
        customer_risk_request
        and contract.intent == "analyze_customer_risk"
        and contract.executor == "customer_risk_analytics"
        and contract.data_domain == "zendesk_tickets"
        and contract.scope in {"ticket", "store", "global"}
    )


def authorizes_public_update_analytics(
    contract: SemanticContractV2,
    *,
    public_update_staleness: bool,
    force_customer_risk: bool,
    force_global_event_discovery: bool,
) -> bool:
    """Return whether contract-v2 allows the public-update analytics force route."""
    return bool(
        public_update_staleness
        and not force_customer_risk
        and not force_global_event_discovery
        and contract.intent == "list_tickets"
        and contract.executor == "public_update_analytics"
        and contract.data_domain == "zendesk_ticket_comments"
        and contract.scope == "global"
    )


def authorizes_ticket_semantic_search(
    contract: SemanticContractV2,
    *,
    force_customer_risk: bool,
    force_global_event_discovery: bool,
    force_context_only: bool,
    semantic_context_only: bool,
    schema_limitation: bool,
) -> bool:
    """Return whether contract-v2 allows deterministic scenario-ticket retrieval."""
    return bool(
        not force_customer_risk
        and not force_global_event_discovery
        and not force_context_only
        and not semantic_context_only
        and not schema_limitation
        and contract.intent == "ticket_retrieval"
        and contract.executor == "ticket_semantic_search"
        and contract.data_domain == "zendesk_tickets"
        and contract.scope in {"global", "store", "prior_turn"}
        and contract.confidence in {"high", "medium"}
        and _has_retrieval_concepts(contract.filters)
        and _retrieval_goal_allows_ticket_search(contract.filters)
    )


def _contract(
    *,
    intent: str,
    data_domain: str,
    scope: str,
    executor: str,
    output: str,
    time_range: dict[str, Any] | None,
    filters: dict[str, Any],
    confidence: str,
    selected_executor: str | None,
    reason: str,
) -> SemanticContractV2:
    if selected_executor and selected_executor != executor:
        reason = f"{reason} Shadow executor={executor}; current selected executor={selected_executor}."
    return SemanticContractV2(
        version="2.0-shadow",
        mode="shadow",
        intent=intent,
        data_domain=data_domain,
        scope=scope,
        executor=executor,
        output=output,
        time_range=time_range,
        filters=filters,
        confidence=confidence,
        reason=reason,
    )


def _entities(semantic_frame: dict[str, Any]) -> dict[str, Any]:
    entities = semantic_frame.get("entities") if isinstance(semantic_frame, dict) else None
    return entities if isinstance(entities, dict) else {}


def _task(semantic_frame: dict[str, Any]) -> dict[str, Any]:
    task = semantic_frame.get("task") if isinstance(semantic_frame, dict) else None
    return task if isinstance(task, dict) else {}


def _retrieval(semantic_frame: dict[str, Any]) -> dict[str, Any]:
    retrieval = semantic_frame.get("retrieval") if isinstance(semantic_frame, dict) else None
    return retrieval if isinstance(retrieval, dict) else {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item)
        if text:
            out.append(text)
    return out


def _clean_store_id_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = str(value or "").replace("[", " ").replace("]", " ").replace("'", " ").split(",")

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        for store_id in str(item or "").split():
            text = store_id.strip()
            if text and text.isdigit() and 3 <= len(text) <= 6 and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def _has_retrieval_concepts(retrieval: dict[str, Any]) -> bool:
    return any(
        _clean_list(retrieval.get(key))
        for key in ("required_concepts", "optional_concepts", "vendors", "devices", "actions")
    )


def _retrieval_goal_allows_ticket_search(filters: dict[str, Any]) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            filters.get("search_goal"),
            *(_clean_list(filters.get("required_concepts"))),
            *(_clean_list(filters.get("optional_concepts"))),
        ]
    ).lower()
    document_markers = {
        "article",
        "document",
        "documents",
        "drive",
        "file",
        "files",
        "job aid",
        "kb",
        "knowledge base",
        "sheet",
        "spreadsheet",
        "table",
    }
    ticket_markers = {"ticket", "tickets", "zendesk", "case", "cases", "incident", "incidents"}
    if any(marker in text for marker in document_markers) and not any(marker in text for marker in ticket_markers):
        return False
    return True


def _enrich_ticket_retrieval_filters(filters: dict[str, Any], text: str) -> dict[str, Any]:
    out = dict(filters)
    lowered = text.lower()
    filter_text = " ".join(
        str(item or "")
        for key in ("required_concepts", "optional_concepts", "vendors", "devices", "actions")
        for item in _clean_list(out.get(key))
    ).lower()
    if "network" not in filter_text and "modem" in lowered and any(
        marker in lowered
        for marker in ("connect", "connected", "connection", "connectivity", "network", "internet", "wan")
    ):
        out["required_concepts"] = _append_unique(_clean_list(out.get("required_concepts")), "network")
    return out


def _append_unique(values: list[str], value: str) -> list[str]:
    seen = {item.casefold() for item in values}
    if value.casefold() not in seen:
        return [*values, value]
    return values


def _retrieval_scope(scope: str, filters: dict[str, Any]) -> str:
    if scope == "store" and not (filters.get("store_id") or filters.get("store_ids")):
        return "global"
    if scope in {"store", "global", "prior_turn", "open_ticket", "explicit_ticket"}:
        return scope
    return "global"


def _scope(
    semantic_frame: dict[str, Any],
    explicit_store_context: str | None,
    explicit_store_ids: list[str],
    explicit_ticket_id: int | None,
    target_ticket_id: int | None,
) -> str:
    if explicit_ticket_id:
        return "explicit_ticket"
    if target_ticket_id:
        return "open_ticket"
    if explicit_store_context or explicit_store_ids:
        return "store"
    target = _clean(semantic_frame.get("target_context"))
    if target in {"open_ticket", "explicit_ticket", "store", "global", "prior_turn", "unclear"}:
        return target
    return "unclear"


def _time_range(entities: dict[str, Any], temporal_resolution: Any | None) -> dict[str, Any] | None:
    if temporal_resolution:
        return {
            "display_label": getattr(temporal_resolution, "display_label", None),
            "start_at": getattr(getattr(temporal_resolution, "start_at", None), "isoformat", lambda: None)(),
            "end_at": getattr(getattr(temporal_resolution, "end_at", None), "isoformat", lambda: None)(),
            "source": "temporal_resolver",
        }
    date_phrase = _clean(entities.get("date_phrase"))
    if date_phrase:
        resolved = _resolve_date_phrase(date_phrase)
        if resolved:
            return {
                "display_label": getattr(resolved, "display_label", None),
                "start_at": getattr(getattr(resolved, "start_at", None), "isoformat", lambda: None)(),
                "end_at": getattr(getattr(resolved, "end_at", None), "isoformat", lambda: None)(),
                "date_phrase": date_phrase,
                "source": "semantic_frame_temporal_resolver",
            }
        return {"date_phrase": date_phrase, "source": "semantic_frame"}
    return None


def _resolve_date_phrase(date_phrase: str) -> Any | None:
    try:
        from app.services import temporal_resolver

        return temporal_resolver.resolve_period_text(date_phrase)
    except Exception:
        return None


def _period_contract_time_range(period: Any | None) -> dict[str, Any] | None:
    if not period:
        return None
    return {
        "display_label": getattr(period, "display_label", None),
        "start_date": getattr(period, "start_date", None),
        "end_date_exclusive": getattr(period, "end_date_exclusive", None),
        "source": "task_contract",
    }


def _period_dict_time_range(period: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(period, dict) or not period:
        return None
    return {**period, "source": "event_discovery_period"}


def _asks_for_ticket_numbers(text: str) -> bool:
    return "ticket number" in text or "ticket numbers" in text or "numbers for tickets" in text


def _asks_for_ticket_count(text: str) -> bool:
    return "how many" in text or "count " in text or "number of tickets" in text


def _customer_risk_scope(request: Any, fallback_scope: str) -> str:
    scope = getattr(request, "scope", None)
    if scope == "ticket":
        return "explicit_ticket"
    if scope == "store_tickets":
        return "store"
    if scope in {"ticket_list", "store_ranking"}:
        return "global"
    return fallback_scope


def _executor_from_intent(intent: str, target_ticket_id: int | None) -> str:
    if target_ticket_id and intent in {"current_ticket_context", "ticket_details", "event_fact_check", "next_steps"}:
        return "direct_ticket_context"
    if intent in {"store_analytics", "temporal_analytics"}:
        return "ticket_analytics"
    if intent == "procedure_lookup":
        return "knowledge_retrieval"
    if intent == "similar_cases":
        return "similar_ticket_search"
    return "phase1_tools"


def _contract_intent_from_semantic(intent: str) -> str:
    if intent in {"store_analytics", "temporal_analytics"}:
        return "analyze_tickets"
    if intent in {"current_ticket_context", "ticket_details", "event_fact_check", "next_steps"}:
        return "ticket_details"
    if intent == "procedure_lookup":
        return "retrieve_procedure"
    return intent or "general"


def _domain_from_executor(executor: str) -> str:
    if executor in {"ticket_analytics", "similar_ticket_search", "direct_ticket_context"}:
        return "zendesk_tickets"
    if executor == "knowledge_retrieval":
        return "knowledge_base"
    return "mixed"


def _output_from_executor(executor: str) -> str:
    if executor == "ticket_analytics":
        return "analytics_answer"
    if executor == "direct_ticket_context":
        return "ticket_answer"
    if executor == "knowledge_retrieval":
        return "procedure_answer"
    if executor == "similar_ticket_search":
        return "similar_cases"
    return "answer"
