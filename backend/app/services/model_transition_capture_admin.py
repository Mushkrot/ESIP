"""Fail-closed Super Admin control for temporary exact-request capture."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from app.config import settings
from app.reference_data.fixtures import IDENTITY_DOMAIN
from app.services import firestore


CONFIG_COLLECTION = "system"
CONFIG_DOCUMENT = "model_transition_request_capture"
MAX_ALLOWED_EMAILS = 25
CACHE_SECONDS = 15.0
_CORPORATE_EMAIL = re.compile(
    rf"^[a-z0-9._%+-]+@{re.escape(IDENTITY_DOMAIN)}$",
    re.IGNORECASE,
)
_cached_config: dict[str, Any] | None = None
_cached_until = 0.0


class CaptureConfigValidationError(ValueError):
    """Raised when a capture-control patch is not safe."""


def default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "allowed_emails": [],
        "max_allowed_emails": MAX_ALLOWED_EMAILS,
        "config_document": f"{CONFIG_COLLECTION}/{CONFIG_DOCUMENT}",
        "safe_defaults": True,
        "updated_at": None,
        "updated_by": None,
    }


def _normalize(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    default = default_config()
    values: list[str] = []
    seen: set[str] = set()
    for item in (raw or {}).get("allowed_emails") or []:
        email = str(item or "").strip().lower()
        if email and email not in seen and _CORPORATE_EMAIL.fullmatch(email):
            seen.add(email)
            values.append(email)
    enabled = bool((raw or {}).get("enabled")) and bool(values)
    return {
        **default,
        "enabled": enabled,
        "allowed_emails": values[:MAX_ALLOWED_EMAILS],
        "updated_at": (raw or {}).get("updated_at"),
        "updated_by": (raw or {}).get("updated_by"),
    }


def _set_cache(config: Mapping[str, Any]) -> dict[str, Any]:
    global _cached_config, _cached_until
    _cached_config = dict(config)
    _cached_until = time.monotonic() + CACHE_SECONDS
    return dict(_cached_config)


def read_config(*, db: Any | None = None) -> dict[str, Any]:
    if db is None and settings.runtime_profile == "reference":
        return _normalize(_cached_config)
    client = db or firestore.get_db()
    try:
        doc = client.collection(CONFIG_COLLECTION).document(CONFIG_DOCUMENT).get()
        raw = doc.to_dict() or {} if doc.exists else {}
    except Exception:
        raw = {}
    return _normalize(raw)


def get_runtime_config(*, db: Any | None = None, now: float | None = None) -> dict[str, Any]:
    """Return a short-lived cached config; read failures disable capture."""
    current = time.monotonic() if now is None else now
    if _cached_config is not None and current < _cached_until:
        return dict(_cached_config)
    return _set_cache(read_config(db=db))


def validate_config_patch(patch: Mapping[str, Any], *, actor_email: str = "") -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise CaptureConfigValidationError("Capture config patch must be an object.")
    unknown = sorted(set(patch) - {"enabled", "allowed_emails"})
    if unknown:
        raise CaptureConfigValidationError(f"Unsupported capture config field: {', '.join(unknown)}")
    if "enabled" not in patch or not isinstance(patch.get("enabled"), bool):
        raise CaptureConfigValidationError("enabled must be a boolean.")
    raw_emails = patch.get("allowed_emails")
    if not isinstance(raw_emails, list):
        raise CaptureConfigValidationError("allowed_emails must be a list.")

    emails: list[str] = []
    seen: set[str] = set()
    for item in raw_emails:
        email = str(item or "").strip().lower()
        if not _CORPORATE_EMAIL.fullmatch(email):
            raise CaptureConfigValidationError(
                f"Each allowed email must be a {IDENTITY_DOMAIN} address."
            )
        if email not in seen:
            seen.add(email)
            emails.append(email)
    if len(emails) > MAX_ALLOWED_EMAILS:
        raise CaptureConfigValidationError(f"allowed_emails must contain at most {MAX_ALLOWED_EMAILS} addresses.")
    if patch["enabled"] and not emails:
        raise CaptureConfigValidationError("Select at least one email before enabling request capture.")
    return {
        "enabled": bool(patch["enabled"]),
        "allowed_emails": emails,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(actor_email or "").strip().lower(),
    }


def update_config(patch: Mapping[str, Any], *, actor_email: str, db: Any | None = None) -> dict[str, Any]:
    update = validate_config_patch(patch, actor_email=actor_email)
    if db is None and settings.runtime_profile == "reference":
        return _set_cache(_normalize(update))
    client = db or firestore.get_db()
    client.collection(CONFIG_COLLECTION).document(CONFIG_DOCUMENT).set(update, merge=True)
    return _set_cache(_normalize(update))


def reset_runtime_cache() -> None:
    """Test helper; production callers never need cache invalidation."""
    global _cached_config, _cached_until
    _cached_config = None
    _cached_until = 0.0
