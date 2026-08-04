#!/usr/bin/env python3
"""Read and verify private Phase 2 capture receipts without exporting raw chat data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from google.cloud import storage

from app.config import settings
from app.services import model_transition_request_capture as capture


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"GCS URI required: {uri}")
    bucket_and_name = uri[5:].split("/", 1)
    if len(bucket_and_name) != 2 or not all(bucket_and_name):
        raise ValueError(f"bucket and object name required: {uri}")
    return bucket_and_name[0], bucket_and_name[1]


def summarize_payload(payload: dict[str, Any], expected_model: str) -> dict[str, Any]:
    """Validate capture hashes and return a receipt-only summary."""
    captures = payload.get("model_transition_request_captures") or []
    results = []
    for envelope in captures:
        request = envelope.get("request") or {}
        rebuilt = capture.build_envelope(
            surface=str(envelope.get("surface") or ""),
            model=str(envelope.get("model") or ""),
            input_kind=str(request.get("input_kind") or ""),
            input_value=request.get("input_value"),
            generation_config=request.get("generation_config") or {},
            tool_config=request.get("tool_config"),
            stream=bool(request.get("stream")),
        )
        receipt = capture.compact_receipt(envelope)
        results.append({
            **receipt,
            "hash_valid": receipt["request_sha256"] == rebuilt["request_sha256"],
            "expected_model": receipt["model"] == expected_model,
        })
    return {
        "schema_version": "model_transition_phase2_collection_summary.v1",
        "trace_id": payload.get("trace_id"),
        "turn_id": payload.get("turn_id"),
        "capture_count": len(results),
        "captures": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcs-uri", action="append", required=True, help="Private chat-log GCS object URI; repeatable")
    parser.add_argument("--expected-model", default="gemini-2.5-flash")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("export/model_replacement_test/phase2_capture_summary.json"),
        help="Ignored local receipt-only report path",
    )
    args = parser.parse_args()

    client = storage.Client(project=settings.google_cloud_project_id)
    summaries = []
    for uri in args.gcs_uri:
        bucket, name = _parse_gcs_uri(uri)
        payload = json.loads(client.bucket(bucket).blob(name).download_as_bytes())
        summary = summarize_payload(payload, args.expected_model)
        summary["gcs_payload_uri"] = uri
        summaries.append(summary)

    result = {
        "schema_version": "model_transition_phase2_collection_report.v1",
        "expected_model": args.expected_model,
        "payload_count": len(summaries),
        "capture_count": sum(item["capture_count"] for item in summaries),
        "hashes_valid": all(
            capture_row["hash_valid"]
            for item in summaries
            for capture_row in item["captures"]
        ),
        "models_match": all(
            capture_row["expected_model"]
            for item in summaries
            for capture_row in item["captures"]
        ),
        "payloads": summaries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("payload_count", "capture_count", "hashes_valid", "models_match")}))
    return 0 if result["hashes_valid"] and result["models_match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
