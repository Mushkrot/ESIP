#!/usr/bin/env python3
"""Run a local-only, evaluator-blind replay of the Phase 2 candidate corpus.

The runner deliberately has no model, network, or production-service adapter.
It freezes candidate-visible inputs and control-plane fingerprints, then uses
fake/recorded adapters to prove the replay mechanics before Phase 4 calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "esip.model_transition.phase3.replay.v1"
REQUEST_SCHEMA = "esip.model_transition.candidate_request.v1"
KNOWN_MODEL_PROFILES = {"gemini-2.5-flash", "fake-recorded-v1"}
ALLOWED_TRANSPORTS = {"fake", "recorded"}
LANES = {
    "A_bounded_structured",
    "B_grounded_answer",
    "C_contextual_tool_workflow",
    "D_multimodal",
}
REQUIRED_PACKET_KEYS = {
    "case_id",
    "lane",
    "current_question",
    "conversation_history",
    "frozen_evidence",
    "sources",
    "tickets",
    "target_ticket_id",
    "counts",
    "schema_version",
}
EVALUATOR_ONLY_KEYS = {
    "assistant_answer",
    "label",
    "rationale",
    "assertion",
    "rubric",
    "reference_answer",
    "evaluator_notes",
    "quality_events",
    "phase1_decision",
    "policy_flags",
    "risk_signals",
    "route_metadata",
    "expected_route",
    "route_expectation",
    "reference_route",
    "review_quality",
}
DATA_BLOB_KEYS = {"conversation_history", "frozen_evidence", "sources", "tickets", "counts"}
HASH_KEYS = (
    "prompt_sha256",
    "context_sha256",
    "evidence_sha256",
    "tool_results_sha256",
    "schema_sha256",
    "validator_sha256",
    "generation_config_sha256",
    "route_profile_sha256",
    "request_sha256",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PRODUCTION_BUILDER_REFS = {
    "A_bounded_structured": (
        "backend.app.services.universal_chat:decide_tools",
        "backend.app.services.ai_dispatcher_runtime:build_planner_prompt",
        "backend.app.services.model_routing:generation_config_payload",
    ),
    "B_grounded_answer": (
        "backend.app.services.universal_chat:chat_stream",
        "backend.app.services.model_routing:generation_config_payload",
    ),
    "C_contextual_tool_workflow": (
        "backend.app.services.universal_chat:chat_stream",
        "backend.app.services.ai_dispatcher_runtime:build_planner_prompt",
        "backend.app.services.model_routing:generation_config_payload",
    ),
    "D_multimodal": (
        "backend.app.services.gemini:ocr_image",
        "backend.app.services.model_routing:generation_config_payload",
    ),
}
VALIDATOR_REFS = (
    "backend.app.services.chat_answer_validator",
    "backend.app.services.output_critic_shadow",
    "backend.app.services.ai_dispatcher_runtime",
)


class ReplayError(Exception):
    """Base class for safe, local replay failures."""


class PacketValidationError(ReplayError):
    pass


class EvaluatorBoundaryError(ReplayError):
    pass


class ReplaySafetyError(ReplayError):
    pass


class ReplayDriftError(ReplayError):
    pass


class JournalCorruptionError(ReplayError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_evaluator_keys(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    """Find evaluator keys outside recorded data blobs.

    Source and ticket payloads are opaque evidence. Their own business fields
    may contain names such as ``label``; evaluator metadata at the packet or
    control-envelope level must still fail closed.
    """
    found: list[str] = []
    if isinstance(value, Mapping):
        in_data_blob = bool(path) and path[0] in DATA_BLOB_KEYS
        for key, child in value.items():
            key_text = str(key)
            if key_text in EVALUATOR_ONLY_KEYS and not in_data_blob:
                found.append(".".join((*path, key_text)))
            found.extend(_scan_evaluator_keys(child, (*path, key_text)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_scan_evaluator_keys(child, (*path, str(index))))
    return found


def validate_candidate_packet(packet: Mapping[str, Any]) -> None:
    if not isinstance(packet, Mapping):
        raise PacketValidationError("candidate packet must be an object")
    missing = sorted(REQUIRED_PACKET_KEYS.difference(packet))
    if missing:
        raise PacketValidationError("candidate packet is missing required fields")
    if not str(packet.get("case_id") or "").strip():
        raise PacketValidationError("candidate packet requires case_id")
    if str(packet.get("lane") or "") not in LANES:
        raise PacketValidationError("candidate packet has an unknown lane")
    if not isinstance(packet.get("current_question"), str):
        raise PacketValidationError("candidate packet question must be text")
    if not isinstance(packet.get("conversation_history"), list):
        raise PacketValidationError("candidate packet history must be a list, including [] for a first turn")
    if packet.get("frozen_evidence") is None:
        raise PacketValidationError("candidate packet must preserve frozen evidence, including an explicit empty object")
    paths = _scan_evaluator_keys(packet)
    if paths:
        raise EvaluatorBoundaryError("evaluator metadata is outside the candidate request boundary")
    try:
        canonical_json(packet)
    except (TypeError, ValueError) as exc:
        raise PacketValidationError("candidate packet is not JSON serializable") from exc


def _generation_config_for_lane(lane: str) -> dict[str, Any]:
    if lane == "D_multimodal":
        return {"max_output_tokens": 2048, "thinking_config": {"thinking_budget": 0}, "temperature": 0.0}
    if lane == "A_bounded_structured":
        return {"max_output_tokens": 1024, "thinking_config": {"thinking_budget": 0}, "temperature": 0.0}
    return {"max_output_tokens": 8192, "thinking_config": {"thinking_budget": 1024}, "temperature": 0.2}


@dataclass(frozen=True)
class ReplayContract:
    lane: str = "B_grounded_answer"
    model_id: str = "gemini-2.5-flash"
    route_profile_id: str = "production-baseline"
    generation_config: Mapping[str, Any] | None = None
    production_builder_refs: tuple[str, ...] = ()
    validator_refs: tuple[str, ...] = VALIDATOR_REFS
    allowed_tools: tuple[str, ...] = ()
    automatic_tools: bool = False
    built_in_tools: tuple[str, ...] = ()
    max_retries: int = 0
    cost_cap_usd: float = 0.0
    transport_kind: str = "recorded"

    def __post_init__(self) -> None:
        if self.lane not in LANES:
            raise ReplaySafetyError("unknown replay lane")
        if self.model_id not in KNOWN_MODEL_PROFILES:
            raise ReplaySafetyError("unknown model profile; replay fails closed")
        if self.transport_kind not in ALLOWED_TRANSPORTS:
            raise ReplaySafetyError("live or unknown transport is not allowed in Phase 3")
        if self.max_retries != 0:
            raise ReplaySafetyError("Phase 3 allows one bounded attempt and no hidden retries")
        if self.cost_cap_usd != 0.0:
            raise ReplaySafetyError("Phase 3 paid budget must remain exactly zero")
        if self.automatic_tools or self.built_in_tools:
            raise ReplaySafetyError("automatic and built-in tools are disabled")
        config = dict(self.generation_config or _generation_config_for_lane(self.lane))
        max_output = config.get("max_output_tokens")
        thinking = config.get("thinking_config")
        if not isinstance(max_output, int) or max_output <= 0 or max_output > 16384:
            raise ReplaySafetyError("generation config requires a bounded max_output_tokens")
        if not isinstance(thinking, Mapping) or set(thinking) != {"thinking_budget"}:
            raise ReplaySafetyError("Gemini 2.5 replay profile requires exactly one thinking_budget control")
        if not isinstance(thinking["thinking_budget"], int) or thinking["thinking_budget"] < 0:
            raise ReplaySafetyError("thinking_budget must be a nonnegative integer")
        object.__setattr__(self, "generation_config", config)
        object.__setattr__(self, "production_builder_refs", tuple(self.production_builder_refs or PRODUCTION_BUILDER_REFS[self.lane]))
        object.__setattr__(self, "validator_refs", tuple(self.validator_refs))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))

    @classmethod
    def for_lane(cls, lane: str, *, route_profile_id: str = "production-baseline") -> "ReplayContract":
        return cls(lane=lane, route_profile_id=route_profile_id)

    def route_descriptor(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "route_profile_id": self.route_profile_id,
            "production_builder_refs": list(self.production_builder_refs),
            "validator_refs": list(self.validator_refs),
            "allowed_tools": list(self.allowed_tools),
            "fallback": "legacy_universal_chat",
        }


def _candidate_input(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "lane": packet["lane"],
        "current_question": packet["current_question"],
        "conversation_history": packet["conversation_history"],
        "frozen_evidence": packet["frozen_evidence"],
        "sources": packet["sources"],
        "tickets": packet["tickets"],
        "target_ticket_id": packet["target_ticket_id"],
        "counts": packet["counts"],
        "schema_version": packet["schema_version"],
    }


def build_candidate_request(packet: Mapping[str, Any], contract: ReplayContract) -> dict[str, Any]:
    """Build the only object an executor may receive.

    The prompt contains candidate-visible input only. Route/evaluator metadata
    remains in the control-plane fingerprint record and is never serialized into
    ``prompt`` or passed to an adapter.
    """
    validate_candidate_packet(packet)
    if packet["lane"] != contract.lane:
        raise ReplayDriftError("packet lane does not match the frozen replay contract")
    candidate_input = _candidate_input(packet)
    prompt = canonical_json(candidate_input)
    request = {
        "schema": REQUEST_SCHEMA,
        "case_id": str(packet["case_id"]),
        "prompt": prompt,
        "input": candidate_input,
        "generation_config": dict(contract.generation_config or {}),
    }
    request["request_hash"] = sha256_value(request)
    return request


def freeze_fingerprints(packet: Mapping[str, Any], contract: ReplayContract) -> dict[str, str]:
    request = build_candidate_request(packet, contract)
    context = {"question": packet["current_question"], "history": packet["conversation_history"]}
    tool_results = {
        "sources": packet["sources"],
        "tickets": packet["tickets"],
        "counts": packet["counts"],
    }
    return {
        "prompt_sha256": sha256_text(request["prompt"]),
        "context_sha256": sha256_value(context),
        "evidence_sha256": sha256_value(packet["frozen_evidence"]),
        "tool_results_sha256": sha256_value(tool_results),
        "schema_sha256": sha256_value({"packet": packet["schema_version"], "request": REQUEST_SCHEMA}),
        "validator_sha256": sha256_value(list(contract.validator_refs)),
        "generation_config_sha256": sha256_value(contract.generation_config),
        "route_profile_sha256": sha256_value(contract.route_descriptor()),
        "request_sha256": request["request_hash"],
    }


def assert_fingerprint(expected: Mapping[str, str], actual: Mapping[str, str]) -> None:
    for key in HASH_KEYS:
        if expected.get(key) != actual.get(key):
            raise ReplayDriftError("frozen replay fingerprint changed")


@dataclass(frozen=True)
class AdapterResult:
    status: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cost_usd: float
    output_hash: str | None = None
    error_code: str | None = None
    retry_index: int = 0


class DeterministicRecordedAdapter:
    """A local adapter that records mechanics, never generates an answer."""

    transport_kind = "recorded"

    def execute(self, request: Mapping[str, Any]) -> AdapterResult:
        output_hash = sha256_text("recorded-output:" + str(request["request_hash"]))
        return AdapterResult(
            status="ok",
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            thought_tokens=0,
            cost_usd=0.0,
            output_hash=output_hash,
        )


class FakeRecordedAdapter(DeterministicRecordedAdapter):
    transport_kind = "fake"


def _validate_adapter_result(result: AdapterResult, contract: ReplayContract) -> None:
    if result.status not in {"ok", "error"}:
        raise ReplaySafetyError("adapter returned an unknown status")
    if result.retry_index != 0:
        raise ReplaySafetyError("adapter attempted an unapproved retry")
    if min(result.latency_ms, result.input_tokens, result.output_tokens, result.thought_tokens) < 0:
        raise ReplaySafetyError("adapter returned negative telemetry")
    if result.cost_usd != 0.0 or result.cost_usd > contract.cost_cap_usd:
        raise ReplaySafetyError("adapter exceeded the zero Phase 3 budget")
    max_output = int(dict(contract.generation_config or {})["max_output_tokens"])
    if result.output_tokens > max_output:
        raise ReplaySafetyError("adapter exceeded max_output_tokens")
    if result.output_hash is not None and not SHA256_RE.fullmatch(result.output_hash):
        raise ReplaySafetyError("adapter output must be represented by a SHA-256 digest")


class AttemptJournal:
    """Append-only metadata journal with safe recovery of an incomplete last line."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recovered_partial_bytes = 0
        self._records = self._load_and_recover()

    def _load_and_recover(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if not raw:
            return []
        records: list[dict[str, Any]] = []
        offset = 0
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            complete = line.endswith((b"\n", b"\r"))
            body = line.rstrip(b"\r\n")
            try:
                record = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if index == len(lines) - 1 and not complete:
                    self.recovered_partial_bytes = len(line)
                    self.path.write_bytes(raw[:offset])
                    break
                raise JournalCorruptionError("attempt journal contains an invalid complete record") from exc
            if not isinstance(record, dict):
                raise JournalCorruptionError("attempt journal record must be an object")
            records.append(record)
            offset += len(line)
        return records

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def append(self, record: Mapping[str, Any]) -> None:
        payload = (canonical_json(dict(record)) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            if self.path.stat().st_size and not self.path.read_bytes().endswith(b"\n"):
                handle.write(b"\n")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._records.append(dict(record))


class ReplayRunner:
    def __init__(self, contract: ReplayContract, adapter: Any, journal_path: Path, *, run_id: str = "phase3"):
        if getattr(adapter, "transport_kind", None) not in ALLOWED_TRANSPORTS:
            raise ReplaySafetyError("ReplayRunner accepts fake or recorded adapters only")
        self.contract = contract
        self.adapter = adapter
        self.journal = AttemptJournal(Path(journal_path))
        self.run_id = run_id

    def run(
        self,
        packets: Iterable[Mapping[str, Any]],
        *,
        evaluator_rows: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # Evaluator rows are indexed only for parity/audit. Their values never
        # enter build_candidate_request, adapter.execute, or attempt records.
        evaluator_ids = {str(row.get("case_id")) for row in (evaluator_rows or []) if row.get("case_id")}
        records = self.journal.records()
        completed_case_ids = {str(row.get("case_id")) for row in records if row.get("status") == "ok"}
        completed_attempt_ids = {str(row.get("attempt_id")) for row in records}
        completed = 0
        skipped = 0
        errors = 0
        for packet in packets:
            request = build_candidate_request(packet, self.contract)
            case_id = str(packet["case_id"])
            attempt_id = f"{self.run_id}:{case_id}:1"
            if case_id in completed_case_ids or attempt_id in completed_attempt_ids:
                skipped += 1
                continue
            fingerprints = freeze_fingerprints(packet, self.contract)
            try:
                result = self.adapter.execute(request)
                if isinstance(result, Mapping):
                    result = AdapterResult(**dict(result))
                _validate_adapter_result(result, self.contract)
                record = {
                    "schema": "esip.model_transition.phase3.attempt.v1",
                    "run_id": self.run_id,
                    "case_id": case_id,
                    "attempt_id": attempt_id,
                    "attempt_index": 1,
                    "retry_of": None,
                    "status": result.status,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "thought_tokens": result.thought_tokens,
                    "cost_usd": result.cost_usd,
                    "output_hash": result.output_hash,
                    "error_code": result.error_code,
                    "fingerprints": fingerprints,
                }
            except ReplayError:
                raise
            except Exception as exc:  # adapter errors are sanitized into metadata
                record = {
                    "schema": "esip.model_transition.phase3.attempt.v1",
                    "run_id": self.run_id,
                    "case_id": case_id,
                    "attempt_id": attempt_id,
                    "attempt_index": 1,
                    "retry_of": None,
                    "status": "error",
                    "latency_ms": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thought_tokens": 0,
                    "cost_usd": 0.0,
                    "output_hash": None,
                    "error_code": type(exc).__name__,
                    "fingerprints": fingerprints,
                }
            self.journal.append(record)
            if record["status"] == "ok":
                completed += 1
                completed_case_ids.add(case_id)
            else:
                errors += 1
            completed_attempt_ids.add(attempt_id)
        return {
            "schema": SCHEMA_VERSION,
            "run_id": self.run_id,
            "total_packets": completed + skipped + errors,
            "completed": completed,
            "skipped": skipped,
            "errors": errors,
            "attempt_records": len(self.journal.records()),
            "recovered_partial_bytes": self.journal.recovered_partial_bytes,
            "evaluator_rows_indexed": len(evaluator_ids),
            "candidate_request_boundary": "evaluator_blind",
            "transport": getattr(self.adapter, "transport_kind", "unknown"),
            "paid_cost_usd": 0.0,
        }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_private_replay(packets_path: Path, evaluator_path: Path | None, output: Path, *, adapter_name: str, run_id: str) -> dict[str, Any]:
    packets = read_jsonl(packets_path)
    evaluator = read_jsonl(evaluator_path) if evaluator_path else []
    packets_by_lane: dict[str, list[dict[str, Any]]] = {}
    for item in packets:
        packets_by_lane.setdefault(str(item.get("lane")), []).append(item)
    if not packets_by_lane or any(lane not in LANES for lane in packets_by_lane):
        raise ReplaySafetyError("private replay contains an unknown or empty lane")
    journal_path = output / "attempts.jsonl"
    lane_summaries: list[dict[str, Any]] = []
    for lane, lane_packets in sorted(packets_by_lane.items()):
        contract = ReplayContract.for_lane(lane)
        adapter = FakeRecordedAdapter() if adapter_name == "fake" else DeterministicRecordedAdapter()
        lane_summaries.append(
            ReplayRunner(contract, adapter, journal_path, run_id=f"{run_id}:{lane}").run(
                lane_packets,
                evaluator_rows=evaluator,
            )
        )
    summary = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "total_packets": len(packets),
        "completed": sum(item["completed"] for item in lane_summaries),
        "skipped": sum(item["skipped"] for item in lane_summaries),
        "errors": sum(item["errors"] for item in lane_summaries),
        "attempt_records": len(AttemptJournal(journal_path).records()),
        "recovered_partial_bytes": sum(item["recovered_partial_bytes"] for item in lane_summaries),
        "evaluator_rows_indexed": len(evaluator),
        "candidate_request_boundary": "evaluator_blind",
        "transport": adapter_name,
        "paid_cost_usd": 0.0,
        "lanes": {lane: len(rows) for lane, rows in sorted(packets_by_lane.items())},
    }
    manifest = {
        "schema": SCHEMA_VERSION,
        "packets_file_sha256": sha256_file(packets_path),
        "evaluator_file_sha256": sha256_file(evaluator_path) if evaluator_path else None,
        "packet_count": len(packets),
        "evaluator_count": len(evaluator),
        "contract": {
            "lane_contracts": {
                lane: {
                    "model_id": ReplayContract.for_lane(lane).model_id,
                    "transport_kind": ReplayContract.for_lane(lane).transport_kind,
                    "generation_config_sha256": sha256_value(ReplayContract.for_lane(lane).generation_config),
                    "route_profile_sha256": sha256_value(ReplayContract.for_lane(lane).route_descriptor()),
                }
                for lane in sorted(packets_by_lane)
            },
        },
        "summary": summary,
        "private_artifact_policy": "raw packets, answers, evidence and outputs remain outside Git and Recallant",
    }
    write_json_atomic(output / "manifest.json", manifest)
    write_json_atomic(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--evaluator", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adapter", choices=("fake", "recorded"), default="recorded")
    parser.add_argument("--run-id", default="phase3-local")
    args = parser.parse_args()
    summary = run_private_replay(args.packets, args.evaluator, args.output, adapter_name=args.adapter, run_id=args.run_id)
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
