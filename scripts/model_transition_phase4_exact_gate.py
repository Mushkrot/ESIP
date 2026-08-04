#!/usr/bin/env python3
"""Fail closed when Phase 4 lacks an exact, live Gemini 2.5 baseline packet.

This module deliberately performs no transport. It validates the private packet
that a later, explicitly authorized capture/replay run must supply before any
model-comparison result can be accepted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.services import model_transition_request_capture as capture


EXPECTED_MODEL = "gemini-2.5-flash"
EXPECTED_FINAL_SURFACE = "universal_chat.phase2_final_answer"


@dataclass(frozen=True)
class CalibrationGateResult:
    status: str
    accepted_case_ids: tuple[str, ...]
    gaps: tuple[str, ...]


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_packet(packet: Mapping[str, Any], eligible_case_ids: set[str]) -> list[str]:
    gaps: list[str] = []
    case_id = str(packet.get("case_id") or "")
    if case_id not in eligible_case_ids:
        gaps.append("case is not a Phase 3 ranking-eligible verified_good case")

    if packet.get("source") != "live_phase2_capture":
        gaps.append("packet is not marked as a live Phase 2 capture")
    if not str(packet.get("gcs_payload_uri") or "").startswith("gs://"):
        gaps.append("private GCS payload URI is missing")
    if not packet.get("collector_hash_valid"):
        gaps.append("collector did not validate the captured request hash")

    envelope = packet.get("envelope")
    if not isinstance(envelope, Mapping):
        return gaps + ["raw captured request envelope is missing"]
    request = envelope.get("request")
    if not isinstance(request, Mapping):
        return gaps + ["captured request body is missing"]
    try:
        rebuilt = capture.build_envelope(
            surface=str(envelope.get("surface") or ""),
            model=str(envelope.get("model") or ""),
            input_kind=str(request.get("input_kind") or ""),
            input_value=request.get("input_value"),
            generation_config=dict(request.get("generation_config") or {}),
            tool_config=dict(request["tool_config"]) if isinstance(request.get("tool_config"), Mapping) else request.get("tool_config"),
            stream=bool(request.get("stream")),
        )
    except (capture.CaptureValueError, TypeError, ValueError) as exc:
        return gaps + [f"captured envelope is not canonicalizable: {exc}"]

    if envelope.get("request_sha256") != rebuilt["request_sha256"]:
        gaps.append("captured envelope hash does not reproduce")
    if envelope.get("model") != EXPECTED_MODEL:
        gaps.append("captured model is not gemini-2.5-flash")
    if envelope.get("surface") != EXPECTED_FINAL_SURFACE:
        gaps.append("captured surface is not the final Universal Chat Gemini call")

    answer = packet.get("assistant_answer")
    if not isinstance(answer, str) or not answer.strip():
        gaps.append("recorded final ESIP answer is missing")
    elif packet.get("assistant_answer_sha256") != _text_hash(answer):
        gaps.append("recorded final ESIP answer hash does not reproduce")
    if not isinstance(packet.get("final_behavior"), Mapping):
        gaps.append("recorded final ESIP behavior metadata is missing")
    return gaps


def evaluate_exact_baseline_gate(
    packets: Sequence[Mapping[str, Any]], *, eligible_case_ids: set[str]
) -> CalibrationGateResult:
    """Accept only complete, live packets; any missing condition blocks Phase 4."""
    if not eligible_case_ids:
        return CalibrationGateResult(
            status="blocked", accepted_case_ids=(), gaps=("no Phase 3 verified_good cases are available",)
        )
    if not packets:
        return CalibrationGateResult(
            status="blocked", accepted_case_ids=(), gaps=("no fresh live Phase 2 capture packets are available",)
        )

    accepted: list[str] = []
    gaps: list[str] = []
    for packet in packets:
        packet_gaps = _validate_packet(packet, eligible_case_ids)
        case_id = str(packet.get("case_id") or "unknown")
        if packet_gaps:
            gaps.extend(f"{case_id}: {gap}" for gap in packet_gaps)
        else:
            accepted.append(case_id)
    if gaps:
        return CalibrationGateResult("blocked", tuple(sorted(set(accepted))), tuple(gaps))
    return CalibrationGateResult("ready_for_owner_gated_replay", tuple(sorted(set(accepted))), ())
