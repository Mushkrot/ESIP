#!/usr/bin/env python3
"""Read-only audit for Universal Chat Semantic Contract v2 traces.

The script inspects durable BigQuery chat logs and reports whether the v2
contract, the route gate, and the runtime executor agree on recent turns.
It does not modify Firestore, BigQuery, or production routing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402
from google.cloud import bigquery  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402


_TABLE = "esip_reference.universal_chat_turn_logs"
_PROMOTED_EVENT_EXECUTOR = "event_discovery"
_TICKET_ANALYTICS_EXECUTORS = {
    "ticket_analytics",
    "public_update_analytics",
    "customer_risk_analytics",
    "semantic_label_count",
}
_NON_EVENT_SEMANTIC_INTENTS = {
    "general",
    "temporal_analytics",
    "store_analytics",
    "similar_cases",
    "procedure_lookup",
    "row_provenance",
}
_GENERIC_EVENT_OVERREACH_TERMS = {
    "find",
    "from",
    "log",
    "check",
    "ticket",
    "tickets",
    "network",
    "connection",
    "issue",
    "line",
    "technician",
    "dispatched",
    "modem",
    "fiber",
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _short(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _gate_authorized(gate: dict[str, Any]) -> bool | None:
    if "authorized" not in gate:
        return None
    value = gate.get("authorized")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _query_rows(start: datetime, end: datetime, limit: int) -> list[bigquery.table.Row]:
    client = bigquery.Client(project=settings.google_cloud_project_id)
    sql = f"""
        SELECT
          created_at,
          trace_id,
          conversation_id,
          turn_id,
          owner_email,
          surface,
          ticket_id,
          target_ticket_id,
          latest_question,
          assistant_answer,
          policy_flags,
          backend_revision
        FROM `{settings.google_cloud_project_id}.{_TABLE}`
        WHERE created_at >= @start_at
          AND created_at < @end_at
        ORDER BY created_at DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start),
            bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    return list(client.query(sql, job_config=job_config).result())


def _base_example(
    row: bigquery.table.Row,
    contract: dict[str, Any],
    gate: dict[str, Any],
    public_update_gate: dict[str, Any] | None = None,
    customer_risk_gate: dict[str, Any] | None = None,
    ticket_semantic_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "trace_id": row.trace_id,
        "conversation_id": row.conversation_id,
        "turn_id": row.turn_id,
        "owner_email": row.owner_email,
        "backend_revision": row.backend_revision,
        "question": _short(row.latest_question),
        "answer_excerpt": _short(row.assistant_answer),
        "contract": {
            "intent": contract.get("intent"),
            "executor": contract.get("executor"),
            "scope": contract.get("scope"),
            "output": contract.get("output"),
            "confidence": contract.get("confidence"),
        },
        "route_gate": {
            "authorized": gate.get("authorized"),
            "global_event_request": bool(gate.get("global_event_request")),
            "force_customer_risk": bool(gate.get("force_customer_risk")),
        },
        "public_update_gate": (
            {
                "authorized": public_update_gate.get("authorized"),
            }
            if public_update_gate
            else None
        ),
        "customer_risk_gate": (
            {
                "authorized": customer_risk_gate.get("authorized"),
            }
            if customer_risk_gate
            else None
        ),
        "ticket_semantic_gate": (
            {
                "authorized": ticket_semantic_gate.get("authorized"),
            }
            if ticket_semantic_gate
            else None
        ),
    }


def _contract_event_terms(contract: dict[str, Any], flags: dict[str, Any]) -> set[str]:
    filters = contract.get("filters") if isinstance(contract.get("filters"), dict) else {}
    terms = filters.get("event_terms")
    if not terms:
        event_request = _json_dict(flags.get("global_event_discovery"))
        terms = event_request.get("terms")
    if not isinstance(terms, list):
        return set()
    return {str(term or "").strip().lower() for term in terms if str(term or "").strip()}


def _semantic_intent(flags: dict[str, Any]) -> str:
    semantic = _json_dict(flags.get("semantic_frame"))
    return str(semantic.get("intent") or "").strip()


def _semantic_issue(flags: dict[str, Any]) -> str:
    semantic = _json_dict(flags.get("semantic_frame"))
    entities = semantic.get("entities") if isinstance(semantic.get("entities"), dict) else {}
    return str(entities.get("issue_or_category") or "").strip()


def _normalized_question(flags: dict[str, Any]) -> str:
    semantic = _json_dict(flags.get("semantic_frame"))
    return str(semantic.get("normalized_question_en") or "")


def _has_explicit_event_shape(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(
            r"\b(?:which|what)\s+(?:restaurants?|stores?)\b.{0,100}\b"
            r"(?:had|have|reported|recorded|experienced|were\s+affected\s+by)\b",
            lowered,
        )
        or re.search(
            r"\bwhere\b.{0,100}\b(?:did\s+it\s+)?(?:happen|occur|happened|occurred|was|were|reported|recorded)\b",
            lowered,
        )
        or re.search(
            r"\b(?:recorded|reported|confirmed)\b.{0,50}\b(?:cases?|incidents?)\b",
            lowered,
        )
        or re.search(
            r"\b(?:в\s+каком\s+ресторане|каком\s+ресторане|какие\s+рестораны|какой\s+ресторан)\b",
            lowered,
        )
        or re.search(
            r"\b(?:зафикс\w+|случа[ийе])\b.{0,100}\b(?:рестор|рестар|магазин|store|restaurant)",
            lowered,
        )
    )


def _has_fire_word(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("fire", "пожар", "поджар"))


def build_report(start: datetime, end: datetime, rows: list[bigquery.table.Row]) -> dict[str, Any]:
    contract_executor_counts: Counter[str] = Counter()
    contract_intent_counts: Counter[str] = Counter()
    selected_executor_counts: Counter[str] = Counter()
    gate_counts: Counter[str] = Counter()
    public_update_gate_counts: Counter[str] = Counter()
    customer_risk_gate_counts: Counter[str] = Counter()
    ticket_semantic_gate_counts: Counter[str] = Counter()
    revision_counts: Counter[str] = Counter()
    missing_contract_revision_counts: Counter[str] = Counter()
    mismatch_counts: Counter[str] = Counter()

    rows_with_contract = 0
    rows_without_contract = 0
    rows_with_route_gate = 0
    serious_anomalies: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    missing_contract_examples: list[dict[str, Any]] = []
    expected_drift_examples: list[dict[str, Any]] = []

    def add_anomaly(
        reason: str,
        row: bigquery.table.Row,
        contract: dict[str, Any],
        gate: dict[str, Any],
        public_update_gate: dict[str, Any] | None = None,
        customer_risk_gate: dict[str, Any] | None = None,
        ticket_semantic_gate: dict[str, Any] | None = None,
    ) -> None:
        if len(serious_anomalies) < 25:
            item = _base_example(row, contract, gate, public_update_gate, customer_risk_gate, ticket_semantic_gate)
            item["reason"] = reason
            serious_anomalies.append(item)

    def add_warning(
        reason: str,
        row: bigquery.table.Row,
        contract: dict[str, Any],
        gate: dict[str, Any],
        public_update_gate: dict[str, Any] | None = None,
        customer_risk_gate: dict[str, Any] | None = None,
        ticket_semantic_gate: dict[str, Any] | None = None,
    ) -> None:
        if len(warnings) < 25:
            item = _base_example(row, contract, gate, public_update_gate, customer_risk_gate, ticket_semantic_gate)
            item["reason"] = reason
            warnings.append(item)

    for row in rows:
        if row.backend_revision:
            revision_counts[str(row.backend_revision)] += 1

        flags = _json_dict(row.policy_flags)
        contract = _json_dict(flags.get("semantic_contract_v2"))
        gate = _json_dict(flags.get("semantic_contract_v2_route_gate"))
        public_update_gate = _json_dict(flags.get("semantic_contract_v2_public_update_gate"))
        customer_risk_gate = _json_dict(flags.get("semantic_contract_v2_customer_risk_gate"))
        ticket_semantic_gate = _json_dict(flags.get("semantic_contract_v2_ticket_semantic_search_gate"))
        selected_executor = str(flags.get("selected_executor") or "").strip() or "missing"
        semantic_intent = _semantic_intent(flags)
        semantic_text = f"{_normalized_question(flags)}\n{_semantic_issue(flags)}"

        selected_executor_counts[selected_executor] += 1

        if not contract:
            rows_without_contract += 1
            missing_contract_revision_counts[str(row.backend_revision or "missing")] += 1
            if len(missing_contract_examples) < 25:
                missing_contract_examples.append(
                    {
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "trace_id": row.trace_id,
                        "conversation_id": row.conversation_id,
                        "turn_id": row.turn_id,
                        "owner_email": row.owner_email,
                        "backend_revision": row.backend_revision,
                        "question": _short(row.latest_question),
                    }
                )
            continue

        rows_with_contract += 1
        contract_executor = str(contract.get("executor") or "missing")
        contract_intent = str(contract.get("intent") or "missing")
        contract_scope = str(contract.get("scope") or "missing")
        contract_executor_counts[contract_executor] += 1
        contract_intent_counts[contract_intent] += 1

        if gate:
            rows_with_route_gate += 1
            authorized = _gate_authorized(gate)
            gate_key = "authorized_true" if authorized is True else "authorized_false" if authorized is False else "authorized_missing"
        else:
            authorized = None
            gate_key = "route_gate_missing"
        gate_counts[gate_key] += 1

        if public_update_gate:
            public_update_authorized = _gate_authorized(public_update_gate)
            public_update_gate_key = (
                "authorized_true"
                if public_update_authorized is True
                else "authorized_false"
                if public_update_authorized is False
                else "authorized_missing"
            )
        else:
            public_update_authorized = None
            public_update_gate_key = "route_gate_missing"
        public_update_gate_counts[public_update_gate_key] += 1

        if customer_risk_gate:
            customer_risk_authorized = _gate_authorized(customer_risk_gate)
            customer_risk_gate_key = (
                "authorized_true"
                if customer_risk_authorized is True
                else "authorized_false"
                if customer_risk_authorized is False
                else "authorized_missing"
            )
        else:
            customer_risk_authorized = None
            customer_risk_gate_key = "route_gate_missing"
        customer_risk_gate_counts[customer_risk_gate_key] += 1

        if ticket_semantic_gate:
            ticket_semantic_authorized = _gate_authorized(ticket_semantic_gate)
            ticket_semantic_gate_key = (
                "authorized_true"
                if ticket_semantic_authorized is True
                else "authorized_false"
                if ticket_semantic_authorized is False
                else "authorized_missing"
            )
        else:
            ticket_semantic_authorized = None
            ticket_semantic_gate_key = "route_gate_missing"
        ticket_semantic_gate_counts[ticket_semantic_gate_key] += 1

        if contract_executor != selected_executor:
            key = f"{contract_executor} -> {selected_executor}"
            mismatch_counts[key] += 1
            if len(expected_drift_examples) < 20:
                item = _base_example(row, contract, gate, public_update_gate, customer_risk_gate, ticket_semantic_gate)
                item["selected_executor"] = selected_executor
                item["drift"] = key
                expected_drift_examples.append(item)

        if authorized is True:
            if (
                contract_executor != _PROMOTED_EVENT_EXECUTOR
                or contract_intent != "discover_event"
                or contract_scope != "global"
            ):
                add_anomaly(
                    "route gate authorized a non-global-event contract",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if selected_executor != _PROMOTED_EVENT_EXECUTOR:
                add_anomaly(
                    "route gate authorized event discovery but selected_executor is different",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if contract_executor in _TICKET_ANALYTICS_EXECUTORS:
                add_anomaly(
                    "route gate authorized while contract is a ticket analytics class",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )

        if selected_executor == _PROMOTED_EVENT_EXECUTOR and authorized is False:
            add_anomaly(
                "selected event_discovery even though contract route gate denied it",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
            )

        if selected_executor == _PROMOTED_EVENT_EXECUTOR:
            event_terms = _contract_event_terms(contract, flags)
            question_text = f"{row.latest_question or ''}\n{semantic_text}"
            explicit_event_shape = _has_explicit_event_shape(question_text)
            if semantic_intent in _NON_EVENT_SEMANTIC_INTENTS and not explicit_event_shape:
                add_anomaly(
                    "event_discovery selected for non-event semantic frame",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if event_terms and event_terms.issubset(_GENERIC_EVENT_OVERREACH_TERMS) and not explicit_event_shape:
                add_anomaly(
                    f"event_discovery terms look like generic ticket-search terms: {sorted(event_terms)}",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if (
                ("пожар ресторана" in str(row.assistant_answer or "").lower()
                or "restaurant fire" in str(row.assistant_answer or "").lower())
                and "fire" not in event_terms
                and not _has_fire_word(question_text)
            ):
                add_anomaly(
                    "fire-specific answer template leaked into a non-fire question",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )

        if contract_executor in {"ticket_analytics", "public_update_analytics"} and selected_executor == _PROMOTED_EVENT_EXECUTOR:
            add_anomaly(
                "ticket/list contract was routed to event_discovery",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
            )

        if contract_executor == _PROMOTED_EVENT_EXECUTOR and authorized is False:
            add_warning(
                "event_discovery contract present but route gate denied it",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
            )

        if public_update_authorized is True:
            if (
                contract_executor != "public_update_analytics"
                or contract_intent != "list_tickets"
                or contract_scope != "global"
            ):
                add_anomaly(
                    "public-update gate authorized a non-public-update contract",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if selected_executor != "public_update_analytics":
                add_anomaly(
                    "public-update gate authorized but selected_executor is different",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )

        if selected_executor == "public_update_analytics" and public_update_gate and public_update_authorized is False:
            add_anomaly(
                "selected public_update_analytics even though contract gate denied it",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
            )

        if customer_risk_authorized is True:
            if (
                contract_executor != "customer_risk_analytics"
                or contract_intent != "analyze_customer_risk"
                or contract_scope not in {"ticket", "store", "global"}
            ):
                add_anomaly(
                    "Customer Risk gate authorized a non-Customer-Risk contract",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )
            if selected_executor != "customer_risk_analytics":
                add_anomaly(
                    "Customer Risk gate authorized but selected_executor is different",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                )

        if selected_executor == "customer_risk_analytics" and customer_risk_gate and customer_risk_authorized is False:
            add_anomaly(
                "selected customer_risk_analytics even though contract gate denied it",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
            )

        if ticket_semantic_authorized is True:
            if (
                contract_executor != "ticket_semantic_search"
                or contract_intent != "ticket_retrieval"
                or contract_scope not in {"global", "store", "prior_turn"}
            ):
                add_anomaly(
                    "ticket semantic gate authorized a non-ticket-retrieval contract",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                    ticket_semantic_gate,
                )
            if selected_executor != "ticket_semantic_search":
                add_anomaly(
                    "ticket semantic gate authorized but selected_executor is different",
                    row,
                    contract,
                    gate,
                    public_update_gate,
                    customer_risk_gate,
                    ticket_semantic_gate,
                )

        if selected_executor == "ticket_semantic_search" and ticket_semantic_gate and ticket_semantic_authorized is False:
            add_anomaly(
                "selected ticket_semantic_search even though contract gate denied it",
                row,
                contract,
                gate,
                public_update_gate,
                customer_risk_gate,
                ticket_semantic_gate,
            )

    return {
        "window": {
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
        },
        "total_rows": len(rows),
        "rows_with_contract": rows_with_contract,
        "rows_without_contract": rows_without_contract,
        "rows_with_route_gate": rows_with_route_gate,
        "counts": {
            "contract_executors": dict(contract_executor_counts.most_common()),
            "contract_intents": dict(contract_intent_counts.most_common()),
            "selected_executors": dict(selected_executor_counts.most_common()),
            "route_gate": dict(gate_counts.most_common()),
            "public_update_gate": dict(public_update_gate_counts.most_common()),
            "customer_risk_gate": dict(customer_risk_gate_counts.most_common()),
            "ticket_semantic_gate": dict(ticket_semantic_gate_counts.most_common()),
            "executor_drift": dict(mismatch_counts.most_common()),
            "backend_revisions": dict(revision_counts.most_common()),
            "missing_contract_revisions": dict(missing_contract_revision_counts.most_common()),
        },
        "serious_anomaly_count": len(serious_anomalies),
        "warning_count": len(warnings),
        "examples": {
            "serious_anomalies": serious_anomalies,
            "warnings": warnings,
            "missing_contract": missing_contract_examples,
            "executor_drift": expected_drift_examples,
        },
    }


def _print_examples(title: str, examples: list[dict[str, Any]], limit: int) -> None:
    if not examples:
        return
    print()
    print(f"## {title}")
    for item in examples[:limit]:
        trace = item.get("trace_id") or "no-trace"
        print(f"- `{trace}` {item.get('created_at')} rev={item.get('backend_revision')}")
        if item.get("reason"):
            print(f"  Reason: {item['reason']}")
        if item.get("drift"):
            print(f"  Drift: {item['drift']}")
        contract = item.get("contract") or {}
        if contract:
            print(
                "  Contract: "
                f"{contract.get('intent')} / {contract.get('executor')} / "
                f"{contract.get('scope')} / {contract.get('output')}"
            )
        route_gate = item.get("route_gate") or {}
        if route_gate:
            print(f"  Gate: authorized={route_gate.get('authorized')}")
        public_update_gate = item.get("public_update_gate") or {}
        if public_update_gate:
            print(f"  Public update gate: authorized={public_update_gate.get('authorized')}")
        customer_risk_gate = item.get("customer_risk_gate") or {}
        if customer_risk_gate:
            print(f"  Customer Risk gate: authorized={customer_risk_gate.get('authorized')}")
        ticket_semantic_gate = item.get("ticket_semantic_gate") or {}
        if ticket_semantic_gate:
            print(f"  Ticket semantic gate: authorized={ticket_semantic_gate.get('authorized')}")
        print(f"  Q: {item.get('question')}")


def _print_markdown(report: dict[str, Any], example_limit: int) -> None:
    window = report["window"]
    counts = report["counts"]
    print("# Semantic Contract v2 Audit")
    print()
    print(f"- Window: `{window['start_at']}` to `{window['end_at']}`")
    print(f"- Rows scanned: {report['total_rows']}")
    print(f"- Rows with contract: {report['rows_with_contract']}")
    print(f"- Rows without contract: {report['rows_without_contract']}")
    print(f"- Rows with route gate: {report['rows_with_route_gate']}")
    print(f"- Serious anomalies: {report['serious_anomaly_count']}")
    print(f"- Warnings: {report['warning_count']}")
    print()
    print("## Distribution")
    for key in [
        "backend_revisions",
        "contract_executors",
        "contract_intents",
        "selected_executors",
        "route_gate",
        "public_update_gate",
        "customer_risk_gate",
        "ticket_semantic_gate",
        "executor_drift",
        "missing_contract_revisions",
    ]:
        values = counts.get(key) or {}
        print(f"- `{key}`: {values if values else '{}'}")

    _print_examples("Serious Anomalies", report["examples"]["serious_anomalies"], example_limit)
    _print_examples("Warnings", report["examples"]["warnings"], example_limit)
    _print_examples("Rows Missing Contract", report["examples"]["missing_contract"], example_limit)
    _print_examples("Executor Drift Examples", report["examples"]["executor_drift"], example_limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Semantic Contract v2 traces in Universal Chat logs.")
    parser.add_argument("--start", help="Inclusive UTC timestamp, e.g. 2026-05-20T00:00:00Z")
    parser.add_argument("--end", help="Exclusive UTC timestamp. Defaults to now.")
    parser.add_argument("--hours", type=int, default=24, help="Use the last N hours when --start is omitted.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum rows to inspect.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    parser.add_argument("--json-out", help="Write the report JSON to this path.")
    parser.add_argument("--example-limit", type=int, default=10, help="Examples to print per section.")
    parser.add_argument("--fail-on-anomaly", action="store_true", help="Exit non-zero when serious anomalies are found.")
    args = parser.parse_args()

    end = _parse_ts(args.end) or datetime.now(timezone.utc)
    start = _parse_ts(args.start)
    if start is None:
        hours = max(1, min(int(args.hours or 24), 24 * 30))
        start = end - timedelta(hours=hours)
    if start >= end:
        raise SystemExit("Invalid audit window: start must be before end")

    rows = _query_rows(start, end, max(1, min(int(args.limit or 500), 10000)))
    report = build_report(start, end, rows)

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_markdown(report, max(1, int(args.example_limit or 10)))

    if args.fail_on_anomaly and report["serious_anomaly_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
