#!/usr/bin/env python3
"""Compile a private, conservative verified corpus across ESIP model surfaces.

This reader never calls a model or writes to production. Activity-only telemetry is retained as a
real `ambiguous` observation so coverage cannot silently collapse to Universal Chat.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / "export" / "model_replacement_test"
LABELS = ("verified_good", "known_bad", "ambiguous")
TICKET_RE = re.compile(r"(?:ticket|tickets|case|#)\s*#?\s*(\d{4,})", re.IGNORECASE)
LOOKUP_RE = re.compile(r"\b(look up|find|show|list|compare|which tickets|details for)\b", re.IGNORECASE)

# Every current cost-tracked generation surface belongs in one coverage row.
SURFACE_CALL_TYPES = {
    "universal_chat_interpretation": ("universal_semantic",),
    "universal_chat_planner": ("universal_p1",),
    "universal_chat_answer": ("universal_p2",),
    "universal_chat_translation": ("universal_translate",),
    "universal_chat_review_validator": ("universal_review",),
    "universal_dispatcher_planner": ("universal_dispatcher_planner",),
    "universal_output_critic": ("universal_output_critic",),
    "analytics_decision": ("analytics_decision",),
    "analytics_answer": ("analytics_answer",),
    "docs_gap_report": ("docs_gap",),
    "ai_summary": ("summary",),
    "ticket_attachment_ocr": ("ocr_image",),
    "drive_document_ocr": ("drive_ocr",),
    "customer_risk": ("customer_risk",),
    "legacy_ticket_chat": ("chat_phase0", "chat_phase1", "chat_phase2"),
}
EVALUATOR_ONLY_KEYS = {
    "label", "rationale", "assertion", "reference_answer", "quality_events",
    "phase1_decision", "policy_flags", "assistant_answer", "route_metadata",
}


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ticket_ids(question: str) -> list[str]:
    values = [match.group(1) for match in TICKET_RE.finditer(question or "")]
    if re.search(r"ticket\s+ids?\s*:", question or "", re.IGNORECASE):
        tail = re.split(r"ticket\s+ids?\s*:", question, maxsplit=1, flags=re.IGNORECASE)[-1]
        values.extend(re.findall(r"\b\d{4,}\b", tail))
    return list(dict.fromkeys(values))


def _evidence_text(case: dict[str, Any]) -> str:
    return json.dumps(
        {key: case.get(key) for key in ("evidence_snapshot", "sources", "tickets", "target_ticket_id")},
        ensure_ascii=False, sort_keys=True, default=str,
    )


def classify_universal_trace(case: dict[str, Any]) -> dict[str, Any]:
    """Classify only deterministic, independently checkable ticket-lookup facts."""
    question = str(case.get("latest_question") or "")
    answer = str(case.get("assistant_answer") or "")
    requested = _ticket_ids(question)
    evidence_ids = set(re.findall(r"\b\d{4,}\b", _evidence_text(case)))
    answer_ids = set(re.findall(r"\b\d{4,}\b", answer))
    if requested and LOOKUP_RE.search(question):
        missing_evidence = [item for item in requested if item not in evidence_ids]
        missing_answer = [item for item in requested if item not in answer_ids]
        if not missing_evidence and not missing_answer:
            return {
                "label": "verified_good",
                "rationale": "explicit_requested_ticket_ids_match_frozen_evidence_and_answer",
                "assertion": {"requested_ticket_ids": requested, "must_appear_in_answer": requested},
            }
        if not missing_evidence and missing_answer:
            return {
                "label": "known_bad",
                "rationale": "requested_ticket_ids_are_in_frozen_evidence_but_missing_from_answer",
                "assertion": {"missing_from_answer": missing_answer},
            }
    return {
        "label": "ambiguous",
        "rationale": "no_independent_deterministic_evidence_for_full_business_correctness",
        "assertion": {"requires_independent_review": True},
    }


def candidate_packet(case: dict[str, Any]) -> dict[str, Any]:
    """Return production-visible input/evidence only, with all evaluator material excluded."""
    return {
        "case_id": case["case_id"],
        "surface": "universal_chat_answer",
        "current_question": case.get("latest_question"),
        "conversation_history": case.get("messages"),
        "frozen_evidence": case.get("evidence_snapshot"),
        "sources": case.get("sources"),
        "tickets": case.get("tickets"),
        "target_ticket_id": case.get("target_ticket_id"),
        "counts": case.get("counts"),
        "schema_version": case.get("schema_version"),
    }


def build_trace_cases(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    evaluator: list[dict[str, Any]] = []
    packets: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=1):
        case = dict(raw)
        case["case_id"] = f"UC-{index:04d}"
        result = classify_universal_trace(case)
        evaluator.append({
            "case_id": case["case_id"], "surface": "universal_chat_answer", **result,
            "reference_answer": case.get("assistant_answer"),
            "route_metadata": {"phase1_decision": case.get("phase1_decision"), "policy_flags": case.get("policy_flags")},
            "quality_events": [],
        })
        packets.append(candidate_packet(case))
        coverage.append({
            "case_id": case["case_id"], "surface": "universal_chat_answer",
            "source_kind": "durable_turn_log", "label": result["label"], "ranking_eligible": result["label"] != "ambiguous",
        })
    return evaluator, packets, coverage


def select_trace_rows(rows: list[dict[str, Any]], *, per_label: int = 40) -> list[dict[str, Any]]:
    """Keep all label classes represented without making a huge private replay corpus."""
    buckets: dict[str, list[dict[str, Any]]] = {label: [] for label in LABELS}
    for row in rows:
        label = classify_universal_trace(row)["label"]
        if len(buckets[label]) < per_label:
            buckets[label].append(row)
    return [row for label in ("known_bad", "verified_good", "ambiguous") for row in buckets[label]]


def activity_observations(activity: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep genuine activity without claiming a missing prompt/output pair is a quality case."""
    observations = []
    for surface, call_types in SURFACE_CALL_TYPES.items():
        detail = [activity.get(call_type, {}) for call_type in call_types]
        count = sum(int(item.get("actual_calls") or 0) for item in detail)
        observations.append({
            "surface": surface,
            "call_types": list(call_types),
            "actual_calls": count,
            "first_seen": min((item.get("first_seen") for item in detail if item.get("first_seen")), default=None),
            "last_seen": max((item.get("last_seen") for item in detail if item.get("last_seen")), default=None),
            "source_kind": "cost_activity",
            "label": "ambiguous",
            "ranking_eligible": False,
            "rationale": "real_model_activity_without_durable_prompt_output_and_independent_evidence",
        })
    return observations


def fetch_cost_activity(start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
    from google.cloud import bigquery
    from app.config import settings

    table = f"{settings.google_cloud_project_id}.esip_reference.cost_events"
    sql = f"""
      SELECT call_type, COUNT(*) AS actual_calls, MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen
      FROM `{table}`
      WHERE event_type = 'ACTUAL' AND timestamp >= @start_at AND timestamp < @end_at
      GROUP BY call_type
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start),
            bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end),
        ],
        maximum_bytes_billed=100_000_000,
        use_query_cache=True,
    )
    rows = bigquery.Client(project=settings.google_cloud_project_id).query(sql, job_config=config).result()
    return {
        str(row.call_type): {
            "actual_calls": int(row.actual_calls or 0),
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        }
        for row in rows
        if row.call_type
    }


def fetch_ocr_contract_failures(start: datetime, end: datetime, limit: int = 40) -> tuple[list[dict[str, Any]], str | None]:
    """Read a narrow real failure cohort without copying OCR text or attachment URLs."""
    try:
        from google.cloud import bigquery
        from app.config import settings

        table = f"{settings.google_cloud_project_id}.esip_reference.ticket_attachment_ocr"
        sql = f"""
          SELECT ticket_id, processed_at
          FROM `{table}`
          WHERE processing_status = 'success'
            AND processed_at >= @start_at AND processed_at < @end_at
            AND NOT CONTAINS_SUBSTR(IFNULL(ocr_text, ''), '[IMAGE DESCRIPTION:')
          ORDER BY processed_at DESC
          LIMIT @limit
        """
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start),
                bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end),
                bigquery.ScalarQueryParameter("limit", "INT64", max(1, min(limit, 100))),
            ],
            maximum_bytes_billed=100_000_000,
            use_query_cache=True,
        )
        rows = bigquery.Client(project=settings.google_cloud_project_id).query(sql, job_config=config).result()
        return [
            {"ticket_id": int(row.ticket_id) if row.ticket_id is not None else None, "processed_at": row.processed_at.isoformat() if row.processed_at else None}
            for row in rows
        ], None
    except Exception as exc:  # pragma: no cover - live credential/data boundary
        return [], type(exc).__name__


def ocr_contract_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A required OCR output header gives an independent, non-semantic failure proof."""
    return [
        {
            "case_id": f"OCR-{index:04d}",
            "surface": "ticket_attachment_ocr",
            "label": "known_bad",
            "ranking_eligible": False,
            "rationale": "persisted_success_row_violates_required_image_description_header",
            "assertion": {"required_prefix": "[IMAGE DESCRIPTION:]", "observed_prefix": "absent"},
            "source_kind": "persisted_ocr_contract_failure",
            # Identifiers are intentionally private evaluator-only fields.
            "ticket_id": row.get("ticket_id"),
            "processed_at": row.get("processed_at"),
        }
        for index, row in enumerate(rows, start=1)
    ]


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n" for value in values), encoding="utf-8")


def write_corpus(
    output: Path,
    evaluator: list[dict[str, Any]],
    packets: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    ocr_cases: list[dict[str, Any]],
    ocr_gap: str | None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "evaluator.jsonl": (output / "evaluator.jsonl", evaluator),
        "candidate_packets.jsonl": (output / "candidate_packets.jsonl", packets),
        "surface_activity.jsonl": (output / "surface_activity.jsonl", observations),
        "ocr_contract_cases.jsonl": (output / "ocr_contract_cases.jsonl", ocr_cases),
    }
    for path, values in paths.values():
        _write_jsonl(path, values)
    labels = Counter(item["label"] for item in evaluator + ocr_cases)
    summary = {
        "schema_version": "esip.model_transition.phase3.verified_trace_corpus.v1",
        "trace_cases": len(evaluator),
        "persisted_contract_cases": len(ocr_cases),
        "labels": {label: labels.get(label, 0) for label in LABELS},
        "ranking_eligible": sum(item["label"] != "ambiguous" for item in evaluator),
        "ambiguous_excluded_from_ranking": labels.get("ambiguous", 0),
        "surface_coverage": {
            "total": len(observations),
            "with_actual_activity": sum(item["actual_calls"] > 0 for item in observations),
            "telemetry_only": sum(item["actual_calls"] > 0 for item in observations if item["surface"] != "universal_chat_answer"),
        },
        "ocr_contract_source_gap": ocr_gap,
        "privacy": "raw trace content is private; tracked report carries only this summary and hashes",
    }
    manifest = {
        "schema_version": "esip.model_transition.phase3.private_manifest.v1",
        "summary": summary,
        "files": {name: {"rows": len(values), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for name, (path, values) in paths.items()},
    }
    manifest["manifest_sha256"] = _hash(manifest)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universal-input", type=Path, required=True, help="Private Phase 3 Universal snapshot directory")
    parser.add_argument("--output", type=Path, default=PRIVATE_ROOT / "phase3-verified-corpus")
    parser.add_argument("--start", required=True, help="Inclusive UTC timestamp")
    parser.add_argument("--end", required=True, help="Exclusive UTC timestamp")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).astimezone(timezone.utc)
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).astimezone(timezone.utc)
    if start >= end:
        raise SystemExit("start must be before end")
    source_path = args.universal_input / "scanned_rows.jsonl"
    rows = _read_jsonl(source_path if source_path.exists() else args.universal_input / "baseline_cases.jsonl")
    rows = select_trace_rows(rows)
    evaluator, packets, _ = build_trace_cases(rows)
    observations = activity_observations(fetch_cost_activity(start, end))
    ocr_rows, ocr_gap = fetch_ocr_contract_failures(start, end)
    manifest = write_corpus(args.output, evaluator, packets, observations, ocr_contract_cases(ocr_rows), ocr_gap)
    print(json.dumps({"output": str(args.output), "manifest_sha256": manifest["manifest_sha256"], **manifest["summary"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
