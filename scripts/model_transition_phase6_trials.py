"""Local-only adaptive wave planning and fail-closed evaluation gates for Phase 6.

The module can read ignored Phase 2 artifacts and produce redacted approval metadata. It has no
provider client, network transport, production-tool adapter or automatic paid execution path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from math import ceil
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.model_transition_phase5_profiles import (
        CandidateProfile,
        estimate_cost_usd,
        profile as get_profile,
        serialize_candidate_request,
    )
except ModuleNotFoundError:  # Direct `python scripts/module.py` execution from the repo root.
    from model_transition_phase5_profiles import (  # type: ignore[no-redef]
        CandidateProfile,
        estimate_cost_usd,
        profile as get_profile,
        serialize_candidate_request,
    )


PHASE6_SCHEMA = "esip.model_transition.phase6.wave.v1"
INVENTORY_SCHEMA = "esip.model_transition.phase6.inventory.v1"
LANE_A = "A_bounded_structured"
LANE_TO_PROFILE = {
    LANE_A: "candidate-gemini-3.5-flash-lite-lane-a-bounded",
    "B_grounded_answer": "candidate-gemini-3.6-flash-lane-b-grounded",
    "C_contextual_tool_workflow": "candidate-gemini-3.6-flash-lane-c-followup",
    "D_multimodal": "control-gemini-2.5-flash-lane-d",
}
MIN_BASE_CASES = 5
MAX_BASE_CASES = 8
DEFAULT_BASE_CASES = 5
DEFAULT_ADVERSARIAL_CASES = 5
DEFAULT_USD_HARD_CAP = 0.32
DEFAULT_CAD_FX_RATE = 1.40
MAX_USD_HARD_CAP = 0.32
ADVERSARIAL_FAMILY_ORDER = (
    "context_loss",
    "date_scope",
    "empty_evidence",
    "bad_identifier",
    "prompt_injection",
    "conflicting_evidence",
    "malformed_tool_output",
    "multilingual_followup",
    "date_range",
    "unsupported_assumption",
)
STOP_RULES = (
    "stop before a call if approval status or receipt is absent",
    "stop before the next call when the USD or CAD hard cap would be exceeded",
    "stop on any retry, second attempt, provider transport error or timeout",
    "stop on schema, tool, evaluator-boundary or protected-case critical failure",
    "stop and classify as inconclusive when evidence, route or product rule is not attributable",
)


class TrialPlanningError(ValueError):
    """Raised when a proposed wave is not safe or cannot be interpreted."""


class TrialAuthorityError(PermissionError):
    """Raised when a paid batch lacks a matching authorization receipt."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise TrialPlanningError(f"missing private artifact: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _redacted_counts(rows: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key) or "unknown") for row in rows).items()))


def _load_phase2(phase2_dir: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    packets = read_jsonl(phase2_dir / "candidate_packets.jsonl")
    evaluator_rows = read_jsonl(phase2_dir / "reference_evaluator.jsonl")
    adversarial = read_jsonl(phase2_dir / "adversarial_packets.jsonl")
    evaluator: dict[str, dict[str, Any]] = {}
    for row in evaluator_rows:
        case_id = str(row.get("case_id") or "")
        if not case_id or case_id in evaluator:
            raise TrialPlanningError("private evaluator has a missing or duplicate case_id")
        evaluator[case_id] = row
    if not packets:
        raise TrialPlanningError("private candidate corpus is empty")
    return packets, evaluator, adversarial


def _select_adversarial(
    base_case_ids: tuple[str, ...],
    adversarial: list[Mapping[str, Any]],
    target_count: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    by_family: dict[str, list[Mapping[str, Any]]] = {}
    base_set = set(base_case_ids)
    for row in adversarial:
        if str(row.get("parent_case_id") or "") not in base_set:
            continue
        family = str(row.get("adversarial_family") or "")
        if family:
            by_family.setdefault(family, []).append(row)
    selected: list[str] = []
    families: list[str] = []
    used_parents: set[str] = set()
    for family in ADVERSARIAL_FAMILY_ORDER:
        rows = by_family.get(family) or []
        if not rows:
            continue
        ordered_rows = sorted(rows, key=lambda item: (str(item.get("parent_case_id") or ""), str(item.get("case_id") or "")))
        row = next((candidate for candidate in ordered_rows if str(candidate.get("parent_case_id") or "") not in used_parents), ordered_rows[0])
        selected.append(str(row.get("case_id")))
        families.append(family)
        used_parents.add(str(row.get("parent_case_id") or ""))
        if len(selected) == target_count:
            break
    if len(selected) < target_count:
        for family in sorted(by_family):
            for row in sorted(by_family[family], key=lambda item: str(item.get("case_id") or "")):
                case_id = str(row.get("case_id") or "")
                if case_id in selected:
                    continue
                selected.append(case_id)
                families.append(family)
                if len(selected) == target_count:
                    break
            if len(selected) == target_count:
                break
    if len(selected) < target_count:
        raise TrialPlanningError("private corpus does not contain enough adversarial siblings")
    return tuple(selected), tuple(families)


def build_inventory(
    phase2_dir: Path,
    *,
    lane: str = LANE_A,
    base_case_count: int = DEFAULT_BASE_CASES,
    adversarial_case_count: int = DEFAULT_ADVERSARIAL_CASES,
) -> dict[str, Any]:
    if not MIN_BASE_CASES <= base_case_count <= MAX_BASE_CASES:
        raise TrialPlanningError("base cohort must contain 5-8 cases")
    if adversarial_case_count < 1:
        raise TrialPlanningError("at least one adversarial sibling is required")
    packets, evaluator, adversarial = _load_phase2(phase2_dir)
    lane_packets = [row for row in packets if str(row.get("lane") or "") == lane]
    if len(lane_packets) < base_case_count:
        raise TrialPlanningError("lane does not have enough frozen candidate packets")
    labels = {str(row.get("case_id")): str(evaluator.get(str(row.get("case_id")), {}).get("label") or "ambiguous") for row in lane_packets}
    protected = sorted(case_id for case_id, label in labels.items() if label == "verified_good")
    known_bad = sorted(case_id for case_id, label in labels.items() if label == "known_bad")
    ambiguous = sorted(case_id for case_id, label in labels.items() if label == "ambiguous")
    ordered = protected + known_bad + ambiguous
    if len(ordered) < base_case_count:
        raise TrialPlanningError("lane selection cannot satisfy the requested base cohort")
    base_case_ids = tuple(ordered[:base_case_count])
    adversarial_ids, adversarial_families = _select_adversarial(
        base_case_ids, adversarial, adversarial_case_count
    )
    lane_counts = _redacted_counts(lane_packets, "lane")
    label_counts = dict(sorted(Counter(labels.values()).items()))
    selection_manifest = {
        "schema": INVENTORY_SCHEMA,
        "lane": lane,
        "base_case_ids": list(base_case_ids),
        "adversarial_case_ids": list(adversarial_ids),
        "adversarial_families": list(adversarial_families),
        "protected_verified_good_ids": protected,
        "known_bad_ids": known_bad,
    }
    return {
        "schema": INVENTORY_SCHEMA,
        "lane": lane,
        "candidate_profile_id": LANE_TO_PROFILE.get(lane),
        "lane_packet_count": len(lane_packets),
        "lane_counts": lane_counts,
        "label_counts": label_counts,
        "protected_verified_good_count": len(protected),
        "known_bad_count": len(known_bad),
        "ambiguous_count": len(ambiguous),
        "selected_base_case_count": len(base_case_ids),
        "selected_base_case_ids": list(base_case_ids),
        "selected_adversarial_case_count": len(adversarial_ids),
        "selected_adversarial_case_ids": list(adversarial_ids),
        "selected_adversarial_families": list(adversarial_families),
        "rankable_protected_lane": bool(protected),
        "selection_manifest_sha256": sha256_value(selection_manifest),
        "raw_artifacts": "ignored private Phase 2 artifacts; not included in this redacted object",
    }


def _token_estimate(value: Any) -> int:
    return max(256, ceil(len(canonical_json(value)) / 4))


def _private_case_map(phase2_dir: Path) -> dict[str, Mapping[str, Any]]:
    packets, _, adversarial = _load_phase2(phase2_dir)
    return {
        **{str(row["case_id"]): row for row in packets},
        **{str(row["case_id"]): row for row in adversarial},
    }


@dataclass(frozen=True)
class WaveSpec:
    schema: str
    wave_id: str
    lane: str
    candidate_profile_id: str
    baseline_profile_id: str
    base_case_ids: tuple[str, ...]
    adversarial_case_ids: tuple[str, ...]
    adversarial_families: tuple[str, ...]
    selection_manifest_sha256: str
    candidate_request_manifest_sha256: str
    sdk_package: str
    sdk_version: str
    api_version: str
    location: str
    endpoint: str
    timeout_seconds: int
    max_attempts: int
    retry_enabled: bool
    transport_status: str
    max_output_tokens: int
    input_token_reservation: int
    output_token_reservation: int
    usd_forecast: float
    usd_hard_cap: float
    cad_fx_rate: float
    cad_forecast: float
    cad_hard_cap: float
    stop_rules: tuple[str, ...]
    approval_status: str
    owner_approval_receipt: str | None


def validate_wave_spec(spec: WaveSpec) -> None:
    if spec.schema != PHASE6_SCHEMA:
        raise TrialPlanningError("unknown wave schema")
    if not spec.wave_id.startswith("MT-P6-"):
        raise TrialPlanningError("wave must have a named MT-P6 identifier")
    if spec.lane != LANE_A:
        raise TrialPlanningError("this first packet is limited to Lane A")
    if spec.candidate_profile_id != LANE_TO_PROFILE[LANE_A]:
        raise TrialPlanningError("candidate profile is not the reviewed Lane A profile")
    candidate = get_profile(spec.candidate_profile_id)
    if candidate.role != "candidate":
        raise TrialPlanningError("control profile cannot be the candidate in this wave")
    if spec.baseline_profile_id != "recorded-production-baseline":
        raise TrialPlanningError("baseline must be the recorded production route")
    if not MIN_BASE_CASES <= len(spec.base_case_ids) <= MAX_BASE_CASES:
        raise TrialPlanningError("base cohort must contain 5-8 cases")
    if len(spec.adversarial_case_ids) < 1:
        raise TrialPlanningError("wave must contain adversarial siblings")
    if len(set(spec.base_case_ids)) != len(spec.base_case_ids):
        raise TrialPlanningError("base case IDs must be unique")
    if spec.max_attempts != 1 or spec.retry_enabled:
        raise TrialPlanningError("wave must use one attempt and no retries")
    if spec.api_version != "v1" or spec.location != "us-central1":
        raise TrialPlanningError("wave API/location must be explicit stable v1/us-central1")
    if spec.endpoint != "vertex-ai":
        raise TrialPlanningError("wave endpoint must be the reviewed Vertex AI endpoint")
    if spec.transport_status != "quarantined":
        raise TrialPlanningError("Phase 6 planner transport must remain quarantined")
    if spec.approval_status not in {"pending_owner_approval", "approved"}:
        raise TrialPlanningError("unknown approval status")
    if spec.approval_status == "approved" and not spec.owner_approval_receipt:
        raise TrialAuthorityError("approved wave requires an owner approval receipt")
    if spec.approval_status == "pending_owner_approval" and spec.owner_approval_receipt:
        raise TrialPlanningError("pending wave cannot carry an approval receipt")
    if spec.usd_hard_cap <= 0 or spec.usd_hard_cap > MAX_USD_HARD_CAP:
        raise TrialPlanningError("USD hard cap is outside the Phase 6 safety ceiling")
    if spec.cad_hard_cap <= 0 or spec.cad_fx_rate <= 0:
        raise TrialPlanningError("CAD cap and FX assumption must be positive")
    if spec.usd_forecast > spec.usd_hard_cap or spec.cad_forecast > spec.cad_hard_cap:
        raise TrialPlanningError("forecast exceeds the wave hard cap")
    if len(spec.stop_rules) != len(STOP_RULES):
        raise TrialPlanningError("complete stop rules are required")


def build_wave_spec(
    phase2_dir: Path,
    *,
    wave_id: str = "MT-P6-A-20250115-W1",
    lane: str = LANE_A,
    usd_hard_cap: float = DEFAULT_USD_HARD_CAP,
    cad_fx_rate: float = DEFAULT_CAD_FX_RATE,
) -> WaveSpec:
    inventory = build_inventory(phase2_dir, lane=lane)
    if not inventory["rankable_protected_lane"]:
        raise TrialPlanningError("lane has no verified_good protected case and cannot be ranked")
    profile = get_profile(str(inventory["candidate_profile_id"]))
    case_map = _private_case_map(phase2_dir)
    all_ids = tuple(inventory["selected_base_case_ids"] + inventory["selected_adversarial_case_ids"])
    raw_tokens = sum(_token_estimate(case_map[case_id]) for case_id in all_ids)
    input_reservation = ceil(raw_tokens * 1.25)
    output_reservation = profile.max_output_tokens * len(all_ids)
    usd_forecast = estimate_cost_usd(profile, input_reservation, output_reservation)
    if usd_forecast > usd_hard_cap:
        raise TrialPlanningError("conservative reservation exceeds proposed USD cap")
    cad_forecast = round(usd_forecast * cad_fx_rate, 2)
    cad_hard_cap = round(usd_hard_cap * cad_fx_rate, 2)
    candidate_request_manifest = {
        "profile_id": profile.profile_id,
        "case_ids": list(all_ids),
        "transport_status": "quarantined",
        "candidate_visible_payload": "ignored private packet data; hash only in redacted report",
    }
    spec = WaveSpec(
        schema=PHASE6_SCHEMA,
        wave_id=wave_id,
        lane=lane,
        candidate_profile_id=profile.profile_id,
        baseline_profile_id="recorded-production-baseline",
        base_case_ids=tuple(inventory["selected_base_case_ids"]),
        adversarial_case_ids=tuple(inventory["selected_adversarial_case_ids"]),
        adversarial_families=tuple(inventory["selected_adversarial_families"]),
        selection_manifest_sha256=str(inventory["selection_manifest_sha256"]),
        candidate_request_manifest_sha256=sha256_value(candidate_request_manifest),
        sdk_package=profile.sdk_package,
        sdk_version=profile.sdk_version,
        api_version=profile.api_version,
        location=profile.location,
        endpoint="vertex-ai",
        timeout_seconds=profile.timeout_seconds,
        max_attempts=1,
        retry_enabled=False,
        transport_status="quarantined",
        max_output_tokens=profile.max_output_tokens,
        input_token_reservation=input_reservation,
        output_token_reservation=output_reservation,
        usd_forecast=usd_forecast,
        usd_hard_cap=round(usd_hard_cap, 2),
        cad_fx_rate=cad_fx_rate,
        cad_forecast=cad_forecast,
        cad_hard_cap=cad_hard_cap,
        stop_rules=STOP_RULES,
        approval_status="pending_owner_approval",
        owner_approval_receipt=None,
    )
    validate_wave_spec(spec)
    return spec


def redacted_wave_packet(spec: WaveSpec) -> dict[str, Any]:
    validate_wave_spec(spec)
    return {
        "schema": spec.schema,
        "wave_id": spec.wave_id,
        "approval_status": spec.approval_status,
        "lane": spec.lane,
        "candidate_profile_id": spec.candidate_profile_id,
        "baseline_profile_id": spec.baseline_profile_id,
        "base_case_ids": list(spec.base_case_ids),
        "base_case_count": len(spec.base_case_ids),
        "adversarial_case_ids": list(spec.adversarial_case_ids),
        "adversarial_case_count": len(spec.adversarial_case_ids),
        "adversarial_families": list(spec.adversarial_families),
        "sdk_package": spec.sdk_package,
        "sdk_version": spec.sdk_version,
        "api_version": spec.api_version,
        "location": spec.location,
        "endpoint": spec.endpoint,
        "timeout_seconds": spec.timeout_seconds,
        "max_attempts": spec.max_attempts,
        "retry_enabled": spec.retry_enabled,
        "transport_status": spec.transport_status,
        "max_output_tokens": spec.max_output_tokens,
        "input_token_reservation": spec.input_token_reservation,
        "output_token_reservation": spec.output_token_reservation,
        "cost_forecast": {
            "usd": spec.usd_forecast,
            "usd_hard_cap": spec.usd_hard_cap,
            "cad_fx_rate_assumption": spec.cad_fx_rate,
            "cad": spec.cad_forecast,
            "cad_hard_cap": spec.cad_hard_cap,
        },
        "stop_rules": list(spec.stop_rules),
        "selection_manifest_sha256": spec.selection_manifest_sha256,
        "candidate_request_manifest_sha256": spec.candidate_request_manifest_sha256,
        "owner_approval_receipt": spec.owner_approval_receipt,
        "raw_content": "not included; remains in ignored private Phase 2 artifacts",
    }


def write_inventory(phase2_dir: Path, output: Path) -> dict[str, Any]:
    payload = build_inventory(phase2_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "inventory.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_wave_packet(phase2_dir: Path, output: Path) -> dict[str, Any]:
    packet = redacted_wave_packet(build_wave_spec(phase2_dir))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return packet


def require_approved(spec: WaveSpec) -> None:
    validate_wave_spec(spec)
    if spec.approval_status != "approved" or not spec.owner_approval_receipt:
        raise TrialAuthorityError("paid execution requires an exact owner approval receipt")
    raise TrialAuthorityError("Phase 6 local planner has no live transport; use the separately reviewed execution adapter")


def candidate_request_fingerprint(
    packet: Mapping[str, Any],
    *,
    profile_id: str = "candidate-gemini-3.5-flash-lite-lane-a-bounded",
) -> dict[str, Any]:
    profile: CandidateProfile = get_profile(profile_id)
    request = serialize_candidate_request(
        profile,
        {"data": dict(packet)},
        input_tokens=_token_estimate(packet),
        output_tokens=profile.max_output_tokens,
    )
    return {
        "profile_id": profile.profile_id,
        "transport_status": request["transport"]["status"],
        "request_sha256": sha256_value(request),
        "input_token_estimate": _token_estimate(packet),
        "output_token_reservation": profile.max_output_tokens,
        "visible_fields": ["data"],
    }


def classify_observation(
    *,
    protected_regressions: int = 0,
    evidence_available: bool = True,
    harness_ok: bool = True,
    product_rule_clear: bool = True,
    transport_ok: bool = True,
    grounding_ok: bool = True,
    context_ok: bool = True,
    schema_ok: bool = True,
) -> dict[str, Any]:
    if not transport_ok:
        return {"classification": "transport_failure", "rankable": False}
    if not harness_ok:
        return {"classification": "harness_or_route_drift", "rankable": False}
    if not product_rule_clear:
        return {"classification": "product_rule_ambiguity", "rankable": False}
    if not evidence_available:
        return {"classification": "evidence_gap", "rankable": False}
    if protected_regressions or not grounding_ok or not context_ok or not schema_ok:
        return {"classification": "model_capability", "rankable": True}
    return {"classification": "candidate_pass", "rankable": True}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "plan-wave"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--phase2-dir", type=Path, required=True)
        sub.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "inventory":
        payload = write_inventory(args.phase2_dir, args.output)
    else:
        payload = write_wave_packet(args.phase2_dir, args.output)
    print(json.dumps({key: value for key, value in payload.items() if key not in {"raw_content", "raw_artifacts"}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
