"""Private, answer-neutral request-envelope capture for model-transition calibration."""

from __future__ import annotations

import copy
import hashlib
import json
from math import isfinite
from typing import Any

CAPTURE_SCHEMA_VERSION = "model_transition_request_envelope.v1"


class CaptureValueError(ValueError):
    """Raised when a transport payload is not safe to canonicalize exactly."""


def _json_value(value: Any, path: str = "$") -> Any:
    """Deep-copy only JSON values so hashing never depends on object identity."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise CaptureValueError(f"non-finite float at {path}")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CaptureValueError(f"non-string key at {path}")
            copied[key] = _json_value(item, f"{path}.{key}")
        return copied
    raise CaptureValueError(f"unsupported value at {path}: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 without silently stringifying unknown objects."""
    return json.dumps(
        _json_value(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def build_envelope(
    *, surface: str, model: str, input_kind: str, input_value: Any,
    generation_config: dict[str, Any], tool_config: dict[str, Any] | None, stream: bool,
) -> dict[str, Any]:
    """Freeze the exact JSON-compatible request arguments at the provider boundary."""
    if not surface or not model or not input_kind:
        raise CaptureValueError("surface, model, and input_kind are required")
    envelope = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "surface": str(surface),
        "model": str(model),
        "request": {
            "input_kind": str(input_kind),
            "input_value": _json_value(input_value, "$.request.input_value"),
            "generation_config": _json_value(generation_config, "$.request.generation_config"),
            "tool_config": _json_value(tool_config, "$.request.tool_config"),
            "stream": bool(stream),
        },
    }
    raw = canonical_json_bytes(envelope)
    envelope["request_sha256"] = hashlib.sha256(raw).hexdigest()
    envelope["request_bytes"] = len(raw)
    return copy.deepcopy(envelope)


def compact_receipt(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return BQ/Firestore-safe metadata; raw request content is intentionally excluded."""
    request = envelope.get("request") or {}
    return {
        "schema_version": envelope.get("schema_version"),
        "surface": envelope.get("surface"),
        "model": envelope.get("model"),
        "input_kind": request.get("input_kind"),
        "stream": request.get("stream"),
        "request_sha256": envelope.get("request_sha256"),
        "request_bytes": envelope.get("request_bytes"),
    }
