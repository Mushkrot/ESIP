#!/usr/bin/env python3
"""Calibrate the local model-transition replay against the recorded ESIP baseline.

This is a control-plane comparator only. It reads the private Phase 1, Phase 2
and Phase 3 artifacts, never calls a model, and never imports a production
transport. The output is aggregate metadata; raw prompts, answers, tickets and
evidence remain in the ignored private export directory.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
TICKET_ID_RE = re.compile(r"\b\d{4,}\b")
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
RAW_PROMPT_KEYS = {
    "prompt",
    "raw_prompt",
    "prompt_bytes",
    "serialized_prompt",
    "production_prompt",
    "production_prompt_bytes",
}
EXPECTED_LABELS = {"verified_good", "known_bad", "ambiguous"}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load calibration dependency: {path.name}")
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PHASE2 = _load_module("model_transition_phase2_reference_for_calibration", ROOT / "scripts" / "model_transition_phase2_reference.py")
PHASE3 = _load_module("model_transition_phase3_replay_for_calibration", ROOT / "scripts" / "model_transition_phase3_replay.py")


class CalibrationError(Exception):
    """Base class for a fail-closed calibration mismatch."""


class CalibrationDriftError(CalibrationError):
    """Raised when an independent artifact or frozen contract drifts."""


class EvaluatorBoundaryError(CalibrationError):
    """Raised when evaluator-only data enters a candidate-visible packet."""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _unique_case_map(rows: Iterable[Mapping[str, Any]], artifact: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        if not case_id:
            raise CalibrationError(f"{artifact} contains a row without case_id")
        if case_id in result:
            raise CalibrationError(f"{artifact} contains duplicate case_id")
        result[case_id] = row
    return result


def _route_projection(row: Mapping[str, Any], *, route_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    metadata = route_metadata or row
    decision = _as_dict(metadata.get("phase1_decision"))
    policy = _as_dict(metadata.get("policy_flags"))
    semantic = _as_dict(policy.get("semantic_frame"))
    task = _as_dict(semantic.get("task"))
    tools = decision.get("tools")
    if not isinstance(tools, list):
        tools = [] if tools is None else [str(tools)]
    return {
        "tools": [str(tool) for tool in tools],
        "selected_executor": str(policy.get("selected_executor") or ""),
        "semantic_intent": str(semantic.get("intent") or ""),
        "semantic_task": str(task.get("task_type") or ""),
        "target_context": str(semantic.get("target_context") or ""),
    }


def _ticket_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(TICKET_ID_RE.findall(value))
    return set(TICKET_ID_RE.findall(_canonical(value)))


def _language_signal(value: str) -> str:
    return "ru" if CYRILLIC_RE.search(value or "") else "en"


def _normalise_language(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("ru", "рус")):
        return "ru"
    if text.startswith(("en", "анг")):
        return "en"
    return "unknown"


def _prompt_reconstruction(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    observed_keys = set().union(*(set(case.keys()) for case in cases))
    serialized_keys = sorted(RAW_PROMPT_KEYS.intersection(observed_keys))
    available = bool(serialized_keys)
    return {
        "serialized_production_prompt_available": available,
        "serialized_prompt_fields_observed": serialized_keys,
        "status": "available" if available else "reconstruction_gap",
        "model_variance_claim_permitted": False,
        "reason": (
            "historical Phase 1 rows contain candidate-visible context and source references, "
            "but not serialized production prompt bytes"
            if not available
            else "prompt fields are present but byte-level builder comparison remains a separate proof"
        ),
        "live_baseline_required": False,
        "live_baseline_executed": False,
    }


def _assert_equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise CalibrationDriftError(message)


def _calibrate_verified_good(
    baseline_by_id: Mapping[str, Mapping[str, Any]],
    evaluator_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    selected = [case_id for case_id, row in evaluator_by_id.items() if row.get("label") == "verified_good"]
    scope_pass = 0
    evidence_pass = 0
    language_pass = 0
    schema_pass = 0
    for case_id in selected:
        case = baseline_by_id[case_id]
        question = str(case.get("latest_question") or "")
        answer = str(case.get("assistant_answer") or "")
        requested = set(PHASE2.requested_ticket_ids(question))
        answer_ids = _ticket_ids(answer)
        evidence_ids = _ticket_ids({
            "evidence_snapshot": case.get("evidence_snapshot"),
            "sources": case.get("sources"),
            "tickets": case.get("tickets"),
            "target_ticket_id": case.get("target_ticket_id"),
        })
        semantic = _as_dict(_as_dict(case.get("policy_flags")).get("semantic_frame"))
        expected_language = _language_signal(question)
        answer_language = _language_signal(answer)
        route_language = _normalise_language(semantic.get("answer_language"))
        if requested and requested.issubset(answer_ids):
            scope_pass += 1
        if requested and requested.issubset(evidence_ids):
            evidence_pass += 1
        if answer_language == expected_language and route_language in {expected_language, "unknown"}:
            language_pass += 1
        if isinstance(answer, str) and answer.strip() and case.get("schema_version"):
            schema_pass += 1
    return {
        "selected": len(selected),
        "scored": len(selected),
        "scope_pass": scope_pass,
        "evidence_pass": evidence_pass,
        "language_pass": language_pass,
        "schema_pass": schema_pass,
        "ambiguous_excluded": sum(row.get("label") == "ambiguous" for row in evaluator_by_id.values()),
        "known_bad_excluded": sum(row.get("label") == "known_bad" for row in evaluator_by_id.values()),
    }


def calibrate_records(
    phase1_cases: list[Mapping[str, Any]],
    phase2_packets: list[Mapping[str, Any]],
    phase2_evaluator: list[Mapping[str, Any]],
    phase3_attempts: list[Mapping[str, Any]],
    *,
    phase3_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare independent phase artifacts without generating or exposing output."""
    baseline_by_id = _unique_case_map(phase1_cases, "phase1 baseline")
    packet_by_id = _unique_case_map(phase2_packets, "phase2 candidate packets")
    evaluator_by_id = _unique_case_map(phase2_evaluator, "phase2 evaluator")
    attempt_by_id = _unique_case_map(phase3_attempts, "phase3 attempts")
    expected_ids = set(baseline_by_id)
    for name, values in (("phase2 candidate packets", packet_by_id), ("phase2 evaluator", evaluator_by_id), ("phase3 attempts", attempt_by_id)):
        _assert_equal(set(values), expected_ids, f"case-id lineage mismatch in {name}")
    if not expected_ids:
        raise CalibrationError("calibration corpus is empty")

    labels = [str(row.get("label") or "") for row in phase2_evaluator]
    if not set(labels).issubset(EXPECTED_LABELS):
        raise CalibrationError("phase2 evaluator contains an unknown label")

    packet_hash_matches = 0
    route_matches = 0
    fingerprint_matches = 0
    schema_matches = 0
    for case_id in sorted(expected_ids):
        baseline = baseline_by_id[case_id]
        packet = packet_by_id[case_id]
        evaluator = evaluator_by_id[case_id]
        attempt = attempt_by_id[case_id]
        expected_packet = PHASE2.candidate_packet(dict(baseline))
        try:
            PHASE3.validate_candidate_packet(packet)
        except PHASE3.EvaluatorBoundaryError as exc:
            raise EvaluatorBoundaryError("evaluator metadata entered candidate packet") from exc
        except PHASE3.ReplayError as exc:
            raise CalibrationDriftError("candidate packet failed replay validation") from exc
        _assert_equal(_hash(packet), _hash(expected_packet), "phase2 candidate packet drift")
        packet_hash_matches += 1

        contract = PHASE3.ReplayContract.for_lane(str(packet.get("lane") or ""))
        expected_fingerprints = PHASE3.freeze_fingerprints(packet, contract)
        _assert_equal(_route_projection(baseline), _route_projection(baseline, route_metadata=_as_dict(evaluator.get("route_metadata"))), "route/tool projection drift")
        route_matches += 1
        _assert_equal(str(packet.get("schema_version") or ""), str(baseline.get("schema_version") or ""), "schema version drift")
        schema_matches += 1
        if attempt.get("status") != "ok" or attempt.get("attempt_index") != 1 or attempt.get("retry_of") is not None:
            raise CalibrationDriftError("phase3 attempt is not a single successful bounded attempt")
        try:
            PHASE3.assert_fingerprint(expected_fingerprints, _as_dict(attempt.get("fingerprints")))
        except PHASE3.ReplayDriftError as exc:
            raise CalibrationDriftError("phase3 fingerprint drift") from exc
        fingerprint_matches += 1

    if phase3_manifest:
        _assert_equal(phase3_manifest.get("packet_count"), len(phase2_packets), "phase3 manifest packet count drift")
        _assert_equal(phase3_manifest.get("evaluator_count"), len(phase2_evaluator), "phase3 manifest evaluator count drift")

    verified = _calibrate_verified_good(baseline_by_id, evaluator_by_id)
    if verified["selected"] and verified["scored"] != verified["scope_pass"]:
        raise CalibrationDriftError("verified_good ticket scope check failed")
    if verified["selected"] and verified["scored"] != verified["evidence_pass"]:
        raise CalibrationDriftError("verified_good evidence check failed")
    if verified["selected"] and verified["scored"] != verified["language_pass"]:
        raise CalibrationDriftError("verified_good language check failed")
    if verified["selected"] and verified["scored"] != verified["schema_pass"]:
        raise CalibrationDriftError("verified_good schema check failed")

    prompt_reconstruction = _prompt_reconstruction(phase1_cases)
    summary = {
        "schema": "esip.model_transition.phase4.calibration.v1",
        "status": "pass_with_known_reconstruction_gap" if prompt_reconstruction["status"] == "reconstruction_gap" else "pass",
        "case_count": len(expected_ids),
        "artifact_counts": {
            "phase1_baseline_cases": len(phase1_cases),
            "phase2_candidate_packets": len(phase2_packets),
            "phase2_evaluator_rows": len(phase2_evaluator),
            "phase3_attempts": len(phase3_attempts),
        },
        "labels": dict(sorted(Counter(labels).items())),
        "lineage": {
            "phase1_to_phase2_packet_hash_matches": packet_hash_matches,
            "phase1_to_phase2_route_matches": route_matches,
            "phase2_to_phase3_fingerprint_matches": fingerprint_matches,
            "schema_matches": schema_matches,
            "mismatches": 0,
        },
        "verified_good": verified,
        "prompt_reconstruction": prompt_reconstruction,
        "candidate_request_boundary": "evaluator_blind",
        "live_transport_used": False,
        "paid_cost_usd": 0.0,
        "candidate_quality_claims_authorized": False,
        "production_state_changed": False,
    }
    return summary


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def calibrate_artifacts(phase1_dir: Path, phase2_dir: Path, phase3_dir: Path) -> dict[str, Any]:
    phase1_cases = read_jsonl(phase1_dir / "baseline_cases.jsonl")
    phase2_packets = read_jsonl(phase2_dir / "candidate_packets.jsonl")
    phase2_evaluator = read_jsonl(phase2_dir / "reference_evaluator.jsonl")
    phase3_attempts = read_jsonl(phase3_dir / "attempts.jsonl")
    manifest = _load_json(phase3_dir / "manifest.json") if (phase3_dir / "manifest.json").exists() else None
    return calibrate_records(
        phase1_cases,
        phase2_packets,
        phase2_evaluator,
        phase3_attempts,
        phase3_manifest=manifest,
    )


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    temporary = path / "calibration.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path / "calibration.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase1-dir", type=Path, required=True)
    parser.add_argument("--phase2-dir", type=Path, required=True)
    parser.add_argument("--phase3-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = calibrate_artifacts(args.phase1_dir, args.phase2_dir, args.phase3_dir)
    except CalibrationError as exc:
        sys.stderr.write(f"calibration_failed: {type(exc).__name__}\n")
        return 2
    write_json(args.output, result)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
