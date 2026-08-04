#!/usr/bin/env python3
"""QA for Universal Chat Semantic Contract v2 shadow routing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.routers import universal  # noqa: E402
from app.services import (  # noqa: E402
    customer_risk_analytics,
    semantic_contract_v2,
    semantic_interpreter,
    semantic_task_contracts,
    temporal_resolver,
)


OPEN_TICKET_950032 = {
    "ticket_id": 950032,
    "subject": "Network connectivity issues - degraded speed",
    "org_name": "City Fries 2009",
    "brand": "City Fries",
    "store_id": "2009",
    "issue_category": "Network > Wifi > Unable To Connect",
    "status": "open",
    "created_at": "2026-04-19",
}


def _latest_assistant_text(messages: list[dict]) -> str:
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def _previous_user_question(messages: list[dict]) -> str:
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _build_contract(
    messages: list[dict],
    *,
    open_ticket_id: int | None = None,
    open_ticket_metadata: dict | None = None,
    conversation_entities: dict | None = None,
) -> dict:
    latest_question = str(messages[-1]["content"])
    semantic = semantic_interpreter.interpret_latest_turn(
        messages,
        open_ticket_id=open_ticket_id,
        open_ticket_metadata=open_ticket_metadata,
    )
    semantic_frame = semantic.to_dict()
    routing_messages = semantic_interpreter.build_routing_messages(messages, semantic)
    routing_question = str(routing_messages[-1]["content"])

    raw_explicit_ticket_id = universal._extract_explicit_ticket_id(latest_question)
    routing_explicit_ticket_id = universal._extract_explicit_ticket_id(routing_question)
    if (
        routing_explicit_ticket_id
        and open_ticket_id
        and semantic.target_context == "open_ticket"
        and int(routing_explicit_ticket_id) == int(open_ticket_id)
        and not raw_explicit_ticket_id
    ):
        routing_explicit_ticket_id = None
    explicit_ticket_id = raw_explicit_ticket_id or routing_explicit_ticket_id
    raw_store_ids = universal._extract_store_ids(latest_question)
    routing_store_ids = universal._extract_store_ids(routing_question)
    explicit_store_ids = raw_store_ids or (
        routing_store_ids if semantic.target_context != "open_ticket" else []
    )
    explicit_store_context = explicit_store_ids[0] if len(explicit_store_ids) == 1 else None
    target_ticket_id = explicit_ticket_id
    force_context_only = False
    semantic_context_only = False
    if not target_ticket_id and open_ticket_id and semantic.target_context == "open_ticket":
        target_ticket_id = open_ticket_id
        semantic_context_only = semantic.intent in {"current_ticket_context", "ticket_details"}

    previous_assistant_text = _latest_assistant_text(messages)
    task_contract = semantic_task_contracts.build_task_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        previous_assistant_text=previous_assistant_text,
        semantic_frame=semantic_frame,
        conversation_entities=conversation_entities,
        explicit_store_context=explicit_store_context,
        open_ticket_store_id=str((open_ticket_metadata or {}).get("store_id") or ""),
        messages=routing_messages,
    )
    force_semantic_label_count = bool(
        task_contract
        and task_contract.task_type == "count_by_dimension"
        and task_contract.dimension_type == "semantic_labels"
    )
    customer_risk_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question=latest_question,
        routing_question=routing_question,
        explicit_ticket_id=explicit_ticket_id,
        target_ticket_id=target_ticket_id,
        explicit_store_id=explicit_store_context,
    )
    global_event_request = universal._looks_like_global_event_discovery(
        latest_question,
        routing_question,
        semantic,
        explicit_store_context=explicit_store_context,
        explicit_ticket_id=explicit_ticket_id,
        prior_event_text=_previous_user_question(messages),
    )
    public_update_sql = (
        universal._public_update_staleness_sql(routing_question)
        or universal._public_update_staleness_sql(latest_question)
    )
    temporal_resolution = temporal_resolver.resolve_temporal_context(routing_messages)
    semantic_issue_value = (semantic.entities or {}).get("issue_or_category")
    semantic_issue_filter = bool(semantic_issue_value)
    if semantic_issue_filter and (
        universal._issue_filter_is_generic_ticket_noun(routing_question, semantic_issue_value)
        or universal._issue_filter_is_generic_ticket_noun(latest_question, semantic_issue_value)
    ):
        semantic_issue_filter = False
    temporal_bq_sql = temporal_resolver.build_bq_sql(temporal_resolution) if temporal_resolution else None
    selected_executor = semantic_contract_v2.selected_executor_label(
        force_semantic_label_count=force_semantic_label_count,
        force_customer_risk=bool(customer_risk_request),
        force_global_event_discovery=bool(global_event_request),
        public_update_staleness=bool(public_update_sql),
        temporal_ticket_analytics=bool(temporal_bq_sql and not semantic_issue_filter and not force_semantic_label_count),
        force_context_only=force_context_only,
        semantic_context_only=semantic_context_only,
        schema_limitation=False,
    )
    contract = semantic_contract_v2.build_shadow_contract(
        latest_question=latest_question,
        routing_question=routing_question,
        semantic_frame=semantic_frame,
        explicit_ticket_id=explicit_ticket_id,
        open_ticket_id=open_ticket_id,
        target_ticket_id=target_ticket_id,
        explicit_store_context=explicit_store_context,
        explicit_store_ids=explicit_store_ids,
        ticket_list_analytics=universal._looks_like_ticket_list_analytics(latest_question)
        or universal._looks_like_ticket_list_analytics(routing_question),
        public_update_staleness=bool(public_update_sql),
        force_context_only=force_context_only,
        semantic_context_only=semantic_context_only,
        task_contract=task_contract,
        customer_risk_request=customer_risk_request,
        global_event_request=global_event_request,
        temporal_resolution=temporal_resolution,
        selected_executor=selected_executor,
    )
    return contract.to_dict()


CASES = [
    {
        "id": "SCV2-001",
        "messages": [{"role": "user", "content": "List all ticket numbers for tickets logged today that are incidents"}],
        "expect": {
            "intent": "list_tickets",
            "executor": "ticket_analytics",
            "data_domain": "zendesk_tickets",
            "output": "ticket_numbers",
        },
    },
    {
        "id": "SCV2-002",
        "messages": [{"role": "user", "content": "в каком ресторане у нас был пожар?"}],
        "expect": {
            "intent": "discover_event",
            "executor": "event_discovery",
            "data_domain": "zendesk_tickets",
            "output": "event_candidate_stores",
            "filter_key": "event_terms",
        },
    },
    {
        "id": "SCV2-003",
        "messages": [
            {"role": "user", "content": "в каком ресторане у нас был пожар?"},
            {"role": "assistant", "content": "Я нашел несколько кандидатов по пожару."},
            {"role": "user", "content": "Проверь за последние пару лет"},
        ],
        "expect": {
            "intent": "discover_event",
            "executor": "event_discovery",
            "data_domain": "zendesk_tickets",
            "filter_key": "event_terms",
            "time_key_any": {"date_phrase", "start_at", "start_date"},
        },
    },
    {
        "id": "SCV2-004",
        "messages": [
            {
                "role": "user",
                "content": "For store 2002, a ticket had 4+ customer public follow-ups. What is the general Customer Risk / sentiment of the store?",
            }
        ],
        "expect": {
            "intent": "analyze_customer_risk",
            "executor": "customer_risk_analytics",
            "scope": "store",
            "data_domain": "zendesk_tickets",
        },
    },
    {
        "id": "SCV2-005",
        "messages": [
            {
                "role": "user",
                "content": "show me all tickets not closed that have not had a public update in the last 24 hours",
            }
        ],
        "expect": {
            "intent": "list_tickets",
            "executor": "public_update_analytics",
            "data_domain": "zendesk_ticket_comments",
            "filter_key": "public_update_stale",
        },
    },
    {
        "id": "SCV2-006",
        "messages": [
            {
                "role": "user",
                "content": "Сумарищируй очень кратко на английсокм какого рода жалобы были от рестарана в течение с 2026 года связанные с сетью или подклбчением",
            },
            {
                "role": "assistant",
                "content": (
                    "Since the beginning of 2026, City Fries 2009 has consistently reported issues including "
                    "degraded internet speed, lagging Wi-Fi, dropping and inconsistent connections, and "
                    "complete primary internet outages or WAN 2 failures."
                ),
            },
            {"role": "user", "content": "Сколько тикетов по каждой из перечисленных категорий за 2026 год"},
        ],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "expect": {
            "intent": "count_by_dimension",
            "executor": "semantic_label_count",
            "scope": "store",
            "data_domain": "zendesk_tickets",
            "filter_key": "labels",
        },
    },
    {
        "id": "SCV2-007",
        "messages": [{"role": "user", "content": "How many incident tickets were opened today?"}],
        "expect": {
            "intent": "count_tickets",
            "executor": "ticket_analytics",
            "data_domain": "zendesk_tickets",
            "output": "ticket_count",
        },
    },
    {
        "id": "SCV2-008",
        "messages": [{"role": "user", "content": "Show all outage tickets logged today"}],
        "expect": {
            "intent": "list_tickets",
            "executor": "ticket_analytics",
            "data_domain": "zendesk_tickets",
            "output": "ticket_list",
        },
    },
    {
        "id": "SCV2-009",
        "messages": [{"role": "user", "content": "Which stores had outage incidents over the last two years?"}],
        "expect": {
            "intent": "discover_event",
            "executor": "event_discovery",
            "data_domain": "zendesk_tickets",
            "output": "event_candidate_stores",
            "filter_key": "event_terms",
        },
    },
    {
        "id": "SCV2-019",
        "messages": [
            {
                "role": "user",
                "content": (
                    "найди мне любой тикет из лога ресторана, где мы направляем в ресторан техника "
                    "из codgeco для того, чтобы тот подключил новый моем к сети."
                ),
            }
        ],
        "expect": {
            "intent": "ticket_retrieval",
            "executor": "ticket_semantic_search",
            "data_domain": "zendesk_tickets",
            "scope": "global",
            "forbid_filter_key": "event_terms",
            "filter_text_contains": ["cog", "modem", "network"],
        },
    },
    {
        "id": "SCV2-020",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Проверь все тикеты, начиная от 2023 и по сегодня, связанные с сетью или подключением. "
                    "Были ли упоминания upgrade на Fiber линию от Telecom Partner?"
                ),
            }
        ],
        "expect": {
            "intent": "ticket_retrieval",
            "executor": "ticket_semantic_search",
            "data_domain": "zendesk_tickets",
            "forbid_filter_key": "event_terms",
            "filter_text_contains": ["telecom_partner", "fiber"],
            "time_start_date": "2023-01-01",
        },
    },
]


def _passes(contract: dict, expected: dict) -> list[str]:
    failures: list[str] = []
    for key in ("intent", "executor", "scope", "data_domain", "output"):
        if key in expected and contract.get(key) != expected[key]:
            failures.append(f"{key}={contract.get(key)!r}, expected {expected[key]!r}")
    for key in ("intent", "executor"):
        forbidden = expected.get(f"not_{key}")
        if forbidden and contract.get(key) == forbidden:
            failures.append(f"{key}={contract.get(key)!r}, forbidden {forbidden!r}")
    filter_key = expected.get("filter_key")
    if filter_key and filter_key not in (contract.get("filters") or {}):
        failures.append(f"filters missing {filter_key!r}: {contract.get('filters')!r}")
    forbidden_filter_key = expected.get("forbid_filter_key")
    if forbidden_filter_key and forbidden_filter_key in (contract.get("filters") or {}):
        failures.append(f"filters contain forbidden {forbidden_filter_key!r}: {contract.get('filters')!r}")
    filter_text_contains = expected.get("filter_text_contains") or []
    if filter_text_contains:
        filters = contract.get("filters") or {}
        filter_text = " ".join(
            str(value)
            for key, value in filters.items()
            if key in {"search_goal", "required_concepts", "optional_concepts", "vendors", "devices", "actions"}
        ).lower()
        for needle in filter_text_contains:
            if str(needle).lower() not in filter_text:
                failures.append(f"filters text missing {needle!r}: {filters!r}")
    time_key_any = expected.get("time_key_any")
    if time_key_any:
        time_range = contract.get("time_range") or {}
        if not any(key in time_range and time_range.get(key) for key in time_key_any):
            failures.append(f"time_range missing any of {sorted(time_key_any)}: {time_range!r}")
    expected_time_start = expected.get("time_start_date")
    if expected_time_start:
        time_range = contract.get("time_range") or {}
        start_value = str(time_range.get("start_date") or time_range.get("start_at") or "")
        if not start_value.startswith(str(expected_time_start)):
            failures.append(f"time_range start={start_value!r}, expected date {expected_time_start!r}: {time_range!r}")
    if contract.get("mode") != "shadow":
        failures.append(f"mode={contract.get('mode')!r}, expected 'shadow'")
    return failures


def main() -> int:
    passed = 0
    total = len(CASES) + 13
    for case in CASES:
        contract = _build_contract(
            case["messages"],
            open_ticket_id=case.get("open_ticket_id"),
            open_ticket_metadata=case.get("open_ticket_metadata"),
        )
        failures = _passes(contract, case["expect"])
        ok = not failures
        print(
            ("PASS " if ok else "FAIL ")
            + f"{case['id']}: intent={contract.get('intent')} executor={contract.get('executor')} "
            + f"scope={contract.get('scope')} output={contract.get('output')}"
            + ("" if ok else " FAIL: " + "; ".join(failures))
        )
        if ok:
            passed += 1

    conflict_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="List all ticket numbers for tickets logged today that are incidents",
        routing_question="List all ticket numbers for tickets logged today that are incidents.",
        semantic_frame={
            "intent": "temporal_analytics",
            "target_context": "global",
            "confidence": "high",
            "entities": {"date_phrase": "today", "issue_or_category": "incidents"},
            "task": {"task_type": "retrieve_context"},
        },
        ticket_list_analytics=True,
        global_event_request={"terms": ["incident"], "period": {"display_label": "Today"}},
        temporal_resolution=None,
        selected_executor="ticket_analytics",
    ).to_dict()
    conflict_failures = _passes(
        conflict_contract,
        {
            "intent": "list_tickets",
            "executor": "ticket_analytics",
            "data_domain": "zendesk_tickets",
            "output": "ticket_numbers",
        },
    )
    conflict_ok = not conflict_failures
    print(
        ("PASS " if conflict_ok else "FAIL ")
        + "SCV2-010: synthetic ticket-list/event-discovery conflict keeps ticket_analytics precedence"
        + ("" if conflict_ok else " FAIL: " + "; ".join(conflict_failures))
    )
    if conflict_ok:
        passed += 1

    conflict_authorized = semantic_contract_v2.authorizes_global_event_discovery(
        semantic_contract_v2.SemanticContractV2(**conflict_contract),
        global_event_request={"terms": ["incident"]},
        force_customer_risk=False,
    )
    conflict_gate_ok = conflict_authorized is False
    print(
        ("PASS " if conflict_gate_ok else "FAIL ")
        + "SCV2-011: contract gate blocks event-discovery when ticket-list contract wins"
    )
    if conflict_gate_ok:
        passed += 1

    event_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="в каком ресторане у нас был пожар?",
        routing_question="Which restaurant had a fire?",
        semantic_frame={
            "intent": "store_analytics",
            "target_context": "global",
            "confidence": "high",
            "entities": {},
            "task": {"task_type": "general_answer"},
        },
        global_event_request={"terms": ["fire"], "period": {}},
        selected_executor="event_discovery",
    )
    event_authorized = semantic_contract_v2.authorizes_global_event_discovery(
        event_contract,
        global_event_request={"terms": ["fire"]},
        force_customer_risk=False,
    )
    event_gate_ok = event_authorized is True
    print(
        ("PASS " if event_gate_ok else "FAIL ")
        + "SCV2-012: contract gate allows real global event-discovery contract"
    )
    if event_gate_ok:
        passed += 1

    public_update_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="Show open tickets with no public update in the last 24 hours",
        routing_question="Show open tickets with no public update in the last 24 hours.",
        semantic_frame={
            "intent": "temporal_analytics",
            "target_context": "global",
            "confidence": "high",
            "entities": {},
            "task": {"task_type": "retrieve_context"},
        },
        public_update_staleness=True,
        selected_executor="public_update_analytics",
    )
    public_update_authorized = semantic_contract_v2.authorizes_public_update_analytics(
        public_update_contract,
        public_update_staleness=True,
        force_customer_risk=False,
        force_global_event_discovery=False,
    )
    public_update_gate_ok = public_update_authorized is True
    print(
        ("PASS " if public_update_gate_ok else "FAIL ")
        + "SCV2-013: contract gate allows public-update analytics contract"
    )
    if public_update_gate_ok:
        passed += 1

    public_update_conflict_authorized = semantic_contract_v2.authorizes_public_update_analytics(
        semantic_contract_v2.SemanticContractV2(**conflict_contract),
        public_update_staleness=True,
        force_customer_risk=False,
        force_global_event_discovery=False,
    )
    public_update_conflict_ok = public_update_conflict_authorized is False
    print(
        ("PASS " if public_update_conflict_ok else "FAIL ")
        + "SCV2-014: public-update gate blocks plain ticket-list contracts"
    )
    if public_update_conflict_ok:
        passed += 1

    public_update_precedence_authorized = semantic_contract_v2.authorizes_public_update_analytics(
        public_update_contract,
        public_update_staleness=True,
        force_customer_risk=True,
        force_global_event_discovery=False,
    ) or semantic_contract_v2.authorizes_public_update_analytics(
        public_update_contract,
        public_update_staleness=True,
        force_customer_risk=False,
        force_global_event_discovery=True,
    )
    public_update_precedence_ok = public_update_precedence_authorized is False
    print(
        ("PASS " if public_update_precedence_ok else "FAIL ")
        + "SCV2-015: public-update gate yields to Customer Risk and event-discovery routes"
    )
    if public_update_precedence_ok:
        passed += 1

    customer_risk_request = customer_risk_analytics.detect_customer_risk_request(
        latest_question="For store 2002, summarize Customer Risk from public comments.",
        routing_question="For store 2002, summarize Customer Risk from public comments.",
        explicit_ticket_id=None,
        target_ticket_id=None,
        explicit_store_id="2004",
    )
    customer_risk_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="For store 2002, summarize Customer Risk from public comments.",
        routing_question="For store 2002, summarize Customer Risk from public comments.",
        semantic_frame={
            "intent": "store_analytics",
            "target_context": "store",
            "confidence": "high",
            "entities": {"store_id": "2004"},
            "task": {"task_type": "general_answer"},
        },
        explicit_store_context="2004",
        customer_risk_request=customer_risk_request,
        selected_executor="customer_risk_analytics",
    )
    customer_risk_authorized = semantic_contract_v2.authorizes_customer_risk_analytics(
        customer_risk_contract,
        customer_risk_request=customer_risk_request,
    )
    customer_risk_gate_ok = customer_risk_authorized is True
    print(
        ("PASS " if customer_risk_gate_ok else "FAIL ")
        + "SCV2-016: contract gate allows Customer Risk analytics contract"
    )
    if customer_risk_gate_ok:
        passed += 1

    customer_risk_conflict_authorized = semantic_contract_v2.authorizes_customer_risk_analytics(
        semantic_contract_v2.SemanticContractV2(**conflict_contract),
        customer_risk_request=customer_risk_request,
    )
    customer_risk_conflict_ok = customer_risk_conflict_authorized is False
    print(
        ("PASS " if customer_risk_conflict_ok else "FAIL ")
        + "SCV2-017: Customer Risk gate blocks plain ticket-list contracts"
    )
    if customer_risk_conflict_ok:
        passed += 1

    customer_risk_event_authorized = semantic_contract_v2.authorizes_global_event_discovery(
        customer_risk_contract,
        global_event_request={"terms": ["follow-up"]},
        force_customer_risk=True,
    )
    customer_risk_event_precedence_ok = customer_risk_event_authorized is False
    print(
        ("PASS " if customer_risk_event_precedence_ok else "FAIL ")
        + "SCV2-018: Customer Risk route blocks event-discovery promotion"
    )
    if customer_risk_event_precedence_ok:
        passed += 1

    ticket_retrieval_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="Find a ticket where FiberLink sent a technician to connect a new modem.",
        routing_question="Find a ticket where FiberLink sent a technician to connect a new modem.",
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "global",
            "confidence": "high",
            "entities": {},
            "task": {"task_type": "general_answer"},
            "retrieval": {
                "search_goal": "find tickets matching a described scenario",
                "required_concepts": ["sent technician", "connect new modem"],
                "vendors": ["FiberLink"],
                "devices": ["modem"],
                "actions": ["connect"],
            },
        },
        selected_executor="ticket_semantic_search",
    )
    ticket_retrieval_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        ticket_retrieval_contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    ticket_retrieval_gate_ok = ticket_retrieval_authorized is True
    print(
        ("PASS " if ticket_retrieval_gate_ok else "FAIL ")
        + "SCV2-021: contract gate allows high-confidence ticket semantic search"
    )
    if ticket_retrieval_gate_ok:
        passed += 1

    multi_store_ticket_contract = semantic_contract_v2.build_shadow_contract(
        latest_question=(
            "For every store 2012 2011 2002 2003 2004 2006 find all "
            "internet/speed/connection interruption related tickets for the last 365 days"
        ),
        routing_question=(
            "For every store 2012 2011 2002 2003 2004 2006 find all "
            "internet, speed, or connection interruption related tickets for the last 365 days."
        ),
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "store",
            "confidence": "high",
            "entities": {
                "store_ids": ["2012", "2011", "2002", "2003", "2004", "2006"],
                "date_phrase": "last 365 days",
            },
            "task": {"task_type": "general_answer"},
            "retrieval": {
                "search_goal": "find internet, speed, or connection interruption related tickets",
                "required_concepts": ["internet", "speed", "connection interruption"],
                "output": "ticket_examples",
            },
        },
        explicit_store_ids=["2012", "2011", "2002", "2003", "2004", "2006"],
        selected_executor="ticket_semantic_search",
    )
    multi_store_filters = multi_store_ticket_contract.filters
    multi_store_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        multi_store_ticket_contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    multi_store_ok = (
        multi_store_ticket_contract.scope == "store"
        and multi_store_filters.get("store_id") in (None, "")
        and multi_store_filters.get("store_ids") == ["2012", "2011", "2002", "2003", "2004", "2006"]
        and multi_store_authorized is True
    )
    print(
        ("PASS " if multi_store_ok else "FAIL ")
        + "SCV2-024: multi-store ticket retrieval preserves all store_ids without collapsing to first store"
    )
    if multi_store_ok:
        passed += 1

    low_confidence_ticket_contract = semantic_contract_v2.build_shadow_contract(
        latest_question="Find an old ticket like this.",
        routing_question="Find an old ticket like this.",
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "global",
            "confidence": "low",
            "entities": {},
            "task": {"task_type": "general_answer"},
            "retrieval": {"required_concepts": ["old ticket"]},
        },
        selected_executor="phase1_tools",
    )
    low_confidence_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        low_confidence_ticket_contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    ticket_retrieval_block_ok = low_confidence_authorized is False
    print(
        ("PASS " if ticket_retrieval_block_ok else "FAIL ")
        + "SCV2-022: ticket semantic search gate blocks low-confidence/unsupported contracts"
    )
    if ticket_retrieval_block_ok:
        passed += 1

    document_retrieval_contract = semantic_contract_v2.build_shadow_contract(
        latest_question='Find document that contains: "Oracle POS and Cash Drawers"',
        routing_question='Find documents that contain the phrase "Oracle POS and Cash Drawers".',
        semantic_frame={
            "intent": "ticket_retrieval",
            "target_context": "global",
            "confidence": "high",
            "entities": {},
            "task": {"task_type": "general_answer"},
            "retrieval": {
                "search_goal": "documents",
                "required_concepts": ["Oracle POS and Cash Drawers"],
                "vendors": ["Oracle"],
                "devices": ["POS", "Cash Drawers"],
                "output": "evidence_summary",
            },
        },
        selected_executor="phase1_tools",
    )
    document_retrieval_authorized = semantic_contract_v2.authorizes_ticket_semantic_search(
        document_retrieval_contract,
        force_customer_risk=False,
        force_global_event_discovery=False,
        force_context_only=False,
        semantic_context_only=False,
        schema_limitation=False,
    )
    document_retrieval_block_ok = (
        document_retrieval_authorized is False
        and document_retrieval_contract.intent == "document_retrieval"
        and document_retrieval_contract.data_domain == "documents"
        and document_retrieval_contract.executor == "phase1_tools"
    )
    print(
        ("PASS " if document_retrieval_block_ok else "FAIL ")
        + "SCV2-023: document/Drive retrieval compiles out of ticket semantic search"
    )
    if document_retrieval_block_ok:
        passed += 1

    print(f"RESULTS: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
