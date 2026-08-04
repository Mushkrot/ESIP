"""
Firestore operations for ESIP.

Collections:
  users/{email}                          — allowlist (managed by admin)
  profiles/{uid}                         — per-user profile (theme, tabs, history)
  cache/summaries/{ticket_id}/{key}      — AI summary cache (status-aware TTL)
  cache/history/{org_id}/data            — Store history cache (2-hour TTL)
  system/data_sources                    — data source toggle state
  chat_answer_traces/{trace_id}          — compact Universal Chat answer traces
  universal_chat_turn_controls/{turn_id} — short-lived active-turn control flags
  universal_chat_conversation_entities/{conversation_id}
                                          — short-lived per-conversation routing entities
  chat_quality_reviews/{review_id}       — Improve Answer review telemetry
  system_provenance_events/{event_id}    — compact failure/fallback provenance
  knowledge_gap_runs/{run_id}            — persisted Knowledge Gaps analysis runs
"""

import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping, Sequence
from typing import Any, Optional

import threading

import firebase_admin
from firebase_admin import credentials as fb_credentials
from firebase_admin import firestore as admin_firestore
from google.cloud import firestore

from app.config import settings
from app.models.profile import UserProfile
from app.services import universal_answer_now

_firebase_init_lock = threading.Lock()


def _ensure_firebase_app() -> None:
    """Initialize Firebase Admin app if not already done. Thread-safe."""
    with _firebase_init_lock:
        try:
            firebase_admin.get_app()
        except ValueError:
            cred_path = str(settings.google_application_credentials or "").strip()
            if cred_path:
                cred = fb_credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})
            else:
                firebase_admin.initialize_app(options={"projectId": settings.firebase_project_id})

logger = logging.getLogger(__name__)

# --- PII redaction patterns (applied before writing summary to Firestore) ---
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# ---------------------------------------------------------------------------
# Cache TTL constants
# ---------------------------------------------------------------------------

# Solved/closed tickets are essentially frozen — cache for 7 days to minimise Gemini costs.
# Open/pending tickets are active and may receive new comments — cache for 4 hours.
SUMMARY_TTL_SOLVED_HOURS = 7 * 24   # 168 h = 7 days
SUMMARY_TTL_OPEN_HOURS = 4          # 4 hours

# Store history changes slowly — 2-hour TTL is a good balance.
HISTORY_TTL_HOURS = 2

_TERMINAL_STATUSES = {"solved", "closed"}


def _redact_pii(text: str) -> str:
    """Replace phone numbers and email addresses in text before caching."""
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    return text


def _redact_trace_value(value):
    """Recursively redact common PII before writing chat traces."""
    if isinstance(value, str):
        return _redact_pii(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_trace_value(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_trace_value(v) for v in value]
    return value


# Module-level client (one per Cloud Run instance — reused across requests)
_db: Optional[firestore.Client] = None


def get_db() -> firestore.Client:
    """Return a Firestore client via Firebase Admin SDK (bypasses Security Rules)."""
    global _db
    if _db is None:
        _ensure_firebase_app()
        _db = admin_firestore.client()
    return _db


def save_chat_answer_trace(trace_id: str, trace: dict) -> None:
    """
    Store a compact Universal Chat answer trace for later quality review.

    Trace capture is best-effort and must never break chat responses.
    """
    now = datetime.now(timezone.utc)
    safe_trace = _redact_trace_value(trace)
    safe_trace["trace_id"] = trace_id
    safe_trace["created_at"] = now.isoformat()
    safe_trace["expires_at"] = (now + timedelta(days=7)).isoformat()
    get_db().collection("chat_answer_traces").document(trace_id).set(safe_trace)


def get_chat_answer_trace(trace_id: str) -> dict | None:
    """Return a saved Universal Chat answer trace, or None when it is unavailable."""
    safe_id = (trace_id or "").strip()
    if not safe_id:
        return None
    doc = get_db().collection("chat_answer_traces").document(safe_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return data if isinstance(data, dict) else None


def _conversation_entities_doc_id(conversation_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(conversation_id or "").strip())
    return safe_id[:180] or "unknown"


def _universal_turn_control_doc_id(turn_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(turn_id or "").strip())
    return safe_id[:180] or "unknown"


def register_universal_chat_turn_control(
    *,
    turn_id: str,
    conversation_id: str,
    owner_uid: str | None,
    owner_email: str | None,
    ttl_seconds: int = 1800,
) -> dict:
    """Register an active Universal Chat turn for short-lived side-channel control."""
    safe_id = _universal_turn_control_doc_id(turn_id)
    if safe_id == "unknown":
        return {"status": "invalid_turn", "available": False}
    payload = universal_answer_now.build_turn_registration_payload(
        turn_id=turn_id,
        conversation_id=conversation_id,
        owner_uid=owner_uid,
        owner_email=owner_email,
        ttl_seconds=ttl_seconds,
    )
    get_db().collection("universal_chat_turn_controls").document(safe_id).set(payload)
    return {"status": "registered", "available": True, "turn_id": payload["turn_id"]}


def request_universal_chat_answer_now(
    *,
    turn_id: str,
    conversation_id: str | None,
    owner_uid: str | None,
    owner_email: str | None,
) -> dict:
    """Set `answer_now_requested` for an active turn owned by the current user."""
    safe_id = _universal_turn_control_doc_id(turn_id)
    if safe_id == "unknown":
        return {"status": "missing", "available": False, "action": universal_answer_now.ANSWER_NOW_ACTION}

    ref = get_db().collection("universal_chat_turn_controls").document(safe_id)
    doc = ref.get()
    data = doc.to_dict() if doc.exists else None
    evaluation = universal_answer_now.evaluate_active_turn_document(
        data if isinstance(data, dict) else None,
        owner_uid=owner_uid,
        owner_email=owner_email,
        conversation_id=conversation_id,
    )
    if not evaluation.available:
        return {
            "status": evaluation.status,
            "available": False,
            "action": universal_answer_now.ANSWER_NOW_ACTION,
            "reason": evaluation.reason,
        }

    now = datetime.now(timezone.utc)
    update = {
        "answer_now_requested": True,
        "requested_action": universal_answer_now.ANSWER_NOW_ACTION,
        "action": universal_answer_now.ANSWER_NOW_ACTION,
        "requested_at": now,
        "updated_at": now,
    }
    ref.set(update, merge=True)
    return {
        "status": "accepted",
        "available": True,
        "action": universal_answer_now.ANSWER_NOW_ACTION,
        "turn_id": str((data or {}).get("turn_id") or turn_id),
        "conversation_id": str((data or {}).get("conversation_id") or conversation_id or ""),
        "requested_at": now.isoformat(),
    }


def get_universal_chat_turn_control(
    *,
    turn_id: str,
    conversation_id: str | None,
    owner_uid: str | None,
    owner_email: str | None,
) -> dict:
    """Return current active-turn control status for stream polling."""
    safe_id = _universal_turn_control_doc_id(turn_id)
    if safe_id == "unknown":
        return {"status": "missing", "available": False, "answer_now_requested": False}
    doc = get_db().collection("universal_chat_turn_controls").document(safe_id).get()
    data = doc.to_dict() if doc.exists else None
    evaluation = universal_answer_now.evaluate_active_turn_document(
        data if isinstance(data, dict) else None,
        owner_uid=owner_uid,
        owner_email=owner_email,
        conversation_id=conversation_id,
    )
    if not evaluation.available:
        return {"status": evaluation.status, "available": False, "answer_now_requested": False}
    return {
        "status": "active",
        "available": True,
        "action": str((data or {}).get("requested_action") or (data or {}).get("action") or ""),
        "answer_now_requested": bool((data or {}).get("answer_now_requested")),
        "turn_id": str((data or {}).get("turn_id") or turn_id),
        "conversation_id": str((data or {}).get("conversation_id") or conversation_id or ""),
    }


def mark_universal_chat_turn_control_completed(turn_id: str) -> None:
    """Best-effort mark of a Universal Chat turn control document as completed."""
    safe_id = _universal_turn_control_doc_id(turn_id)
    if safe_id == "unknown":
        return
    now = datetime.now(timezone.utc)
    get_db().collection("universal_chat_turn_controls").document(safe_id).set(
        {
            "status": "completed",
            "completed_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(minutes=5),
        },
        merge=True,
    )


def get_universal_conversation_entities(conversation_id: str) -> dict | None:
    """Return short-lived Universal Chat routing entities for a conversation."""
    safe_id = _conversation_entities_doc_id(conversation_id)
    if safe_id == "unknown":
        return None
    doc = get_db().collection("universal_chat_conversation_entities").document(safe_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    expires_at = data.get("expires_at")
    if expires_at is None:
        return None
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return None
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        return None
    entities = data.get("entities")
    return entities if isinstance(entities, dict) else None


def save_universal_conversation_entities(conversation_id: str, entities: dict) -> None:
    """
    Save lightweight Universal Chat routing entities.

    This is per-conversation context, not user personalization memory. It is
    short-lived and intentionally stores structured hints instead of transcripts.
    """
    safe_id = _conversation_entities_doc_id(conversation_id)
    if safe_id == "unknown" or not isinstance(entities, dict):
        return
    now = datetime.now(timezone.utc)
    payload = {
        "conversation_id": safe_id,
        "entities": _redact_trace_value(entities),
        "updated_at": now,
        "expires_at": now + timedelta(hours=48),
    }
    get_db().collection("universal_chat_conversation_entities").document(safe_id).set(payload)


def _prepare_chat_quality_review_event(event: dict) -> tuple[str, dict]:
    """Return `(review_id, safe_event)` for Improve Answer review telemetry."""
    now = datetime.now(timezone.utc)
    review_id = str(event.get("review_id") or uuid.uuid4().hex)
    safe_event = _redact_trace_value(event)

    # Preserve selected metadata emails for Super Admin usage/adoption analysis.
    # Free-form text fields remain PII-redacted by _redact_trace_value().
    for key in ("requester_email", "trace_owner_email"):
        value = str(event.get(key) or "").strip().lower()
        if value:
            safe_event[key] = value

    safe_event["review_id"] = review_id
    safe_event["created_at"] = now.isoformat()
    safe_event.setdefault("expires_at", (now + timedelta(days=90)).isoformat())
    return review_id, safe_event


def save_chat_quality_review_event(event: dict) -> str:
    """
    Store Improve Answer review telemetry.

    This stores usage and quality metadata only; raw question/answer text should
    remain in the short-lived answer trace or durable Super Admin chat logs.
    """
    review_id, safe_event = _prepare_chat_quality_review_event(event)
    get_db().collection("chat_quality_reviews").document(review_id).set(safe_event)
    return review_id


def save_system_provenance_event(event: dict) -> str:
    """Store allowlisted failure provenance without exception or customer text."""
    event_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    safe_event = {
        key: event.get(key)
        for key in (
            "source",
            "outcome",
            "endpoint_class",
            "status_code",
            "fallback_used",
            "reason_code",
        )
    }
    safe_event.update({
        "event_id": event_id,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=90)).isoformat(),
    })
    get_db().collection("system_provenance_events").document(event_id).set(safe_event)
    return event_id


def is_universal_chat_logging_disabled(email: str) -> bool:
    """Return whether durable Universal Chat logging is disabled for this user."""
    safe_email = (email or "").strip().lower()
    if not safe_email:
        return False
    try:
        doc = get_db().collection("users").document(safe_email).get()
        if not doc.exists:
            return False
        data = doc.to_dict() or {}
        return bool(data.get("universal_chat_logging_disabled", False))
    except Exception as exc:
        logger.warning("Could not read Universal Chat logging flag for %s: %s", safe_email, exc)
        return False


def save_knowledge_gap_run(
    *,
    result: dict,
    owner_uid: str | None,
    owner_email: str | None,
) -> dict:
    """Persist a Knowledge Gaps analysis run and return its metadata."""
    now = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex
    criteria = result.get("criteria") or {}
    findings = result.get("findings") or []
    title = _knowledge_gap_run_title(criteria, findings, now)
    safe_result = _redact_trace_value(result)
    safe_result["run_id"] = run_id
    safe_result["created_at"] = now.isoformat()
    record = {
        "run_id": run_id,
        "title": title,
        "created_at": now.isoformat(),
        "owner_uid": owner_uid,
        "owner_email": owner_email,
        "period_days": int(criteria.get("period_days") or 0),
        "min_tickets": int(criteria.get("min_tickets") or 0),
        "mode": criteria.get("mode") or "tickets_vs_all",
        "sources": list(criteria.get("sources") or []),
        "finding_count": len(findings),
        "top_findings": [
            str(finding.get("title") or "") for finding in findings[:3]
            if isinstance(finding, Mapping)
        ],
        "result": safe_result,
    }
    get_db().collection("knowledge_gap_runs").document(run_id).set(record)
    return {
        "run_id": run_id,
        "title": title,
        "created_at": record["created_at"],
    }


def list_knowledge_gap_runs(
    *,
    owner_uid: str | None,
    limit: int = 20,
) -> list[dict]:
    """List recent Knowledge Gaps runs for the current user."""
    # Filter in Python to avoid requiring a Firestore composite index for
    # owner_uid + created_at while this feature is still small-volume.
    docs = (
        get_db().collection("knowledge_gap_runs")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(max(limit * 3, 30))
        .stream()
    )
    out: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if owner_uid and data.get("owner_uid") != owner_uid:
            continue
        out.append({
            "run_id": data.get("run_id") or doc.id,
            "title": data.get("title") or "Knowledge Gaps analysis",
            "created_at": data.get("created_at") or "",
            "period_days": int(data.get("period_days") or 0),
            "min_tickets": int(data.get("min_tickets") or 0),
            "mode": data.get("mode") or "tickets_vs_all",
            "sources": list(data.get("sources") or []),
            "finding_count": int(data.get("finding_count") or 0),
            "top_findings": list(data.get("top_findings") or []),
        })
        if len(out) >= limit:
            break
    return out


def get_knowledge_gap_run(
    *,
    run_id: str,
    owner_uid: str | None,
) -> dict | None:
    """Fetch a persisted Knowledge Gaps run if it belongs to the current user."""
    doc = get_db().collection("knowledge_gap_runs").document(run_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    if owner_uid and data.get("owner_uid") != owner_uid:
        return None
    return data


def _knowledge_gap_run_title(criteria: dict, findings: list, created_at: datetime) -> str:
    period = criteria.get("period_days") or 0
    if findings and isinstance(findings[0], Mapping):
        lead = findings[0].get("title") or "Knowledge Gaps analysis"
    else:
        lead = "Knowledge Gaps analysis"
    return f"{created_at.strftime('%b %d')} - last {period} days - {lead}"


# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

ROLE_USER        = "user"
ROLE_ADMIN       = "admin"
ROLE_SUPER_ADMIN = "super_admin"
VALID_ROLES      = {ROLE_USER, ROLE_ADMIN, ROLE_SUPER_ADMIN}


def _norm_user_email(email: Optional[str]) -> str:
    """
    Firestore allowlist keys are lowercased from Admin API / zaf_auth.
    Firebase ID tokens may contain mixed-case email — normalize before document lookup.
    """
    return (email or "").strip().lower()


def clean_auth_display_name(name: Optional[str], email: Optional[str] = None) -> str:
    """
    Return a real auth-provided display name, or "" when the value is just email fallback.

    Admins still provision users by email only. Once a user logs in, Firebase/ZAF may
    provide a real profile name; only that provider-supplied value should become the
    user's display name in management tables.
    """
    text = " ".join(str(name or "").split())
    if not text:
        return ""
    safe_email = _norm_user_email(email)
    lowered = text.lower()
    if safe_email and lowered == safe_email:
        return ""
    if "@" in text:
        return ""
    if "proof" in lowered and any(
        marker in lowered
        for marker in (
            "esip",
            "pilot",
            "phase",
            "shadow",
            "dispatcher",
            "production",
            "all users",
            "all-users",
        )
    ):
        return ""
    if any(marker in lowered for marker in ("test user", "qa user")):
        return ""
    return text


def _role_from_doc(data: dict) -> str:
    """
    Extract role from a Firestore users/{email} document dict.
    Reads the `role` field; falls back to `is_admin` for pre-migration records.
    Returns one of ROLE_USER | ROLE_ADMIN | ROLE_SUPER_ADMIN.
    """
    role = data.get("role")
    if role in VALID_ROLES:
        return role
    # Backward compat: old records written before role system
    if data.get("is_admin", False):
        return ROLE_ADMIN
    return ROLE_USER


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def get_user_role(email: str) -> str:
    """
    Return the effective role for an email.
    Reads users/{email}; returns ROLE_USER if not found or inactive.
    """
    email = _norm_user_email(email)
    if not email:
        return ROLE_USER
    doc = get_db().collection("users").document(email).get()
    if not doc.exists:
        return ROLE_USER
    data = doc.to_dict() or {}
    if not bool(data.get("active", False)):
        return ROLE_USER
    return _role_from_doc(data)


def is_user_allowed(email: str) -> bool:
    """
    Check if the user's email is in the Firestore allowlist.
    Document: users/{email} must exist and have active=True.
    During maintenance mode, only admin/super_admin are allowed.
    """
    email = _norm_user_email(email)
    if not email:
        return False
    doc = get_db().collection("users").document(email).get()
    if not doc.exists:
        return False
    data = doc.to_dict() or {}
    if not bool(data.get("active", False)):
        return False
    if is_maintenance_mode() and _role_from_doc(data) == ROLE_USER:
        return False
    return True


def is_active_user(email: str) -> bool:
    """Return whether an allowlist user exists and is active, ignoring maintenance mode."""
    email = _norm_user_email(email)
    if not email:
        return False
    doc = get_db().collection("users").document(email).get()
    if not doc.exists:
        return False
    return bool((doc.to_dict() or {}).get("active", False))


def resolve_zaf_identity_email(email: str) -> Optional[str]:
    """Resolve only verified legacy Zendesk aliases to their active ESIP identity."""
    from app.reference_data.fixtures import IDENTITY_DOMAIN, LEGACY_IDENTITY_ALIASES, LEGACY_IDENTITY_DOMAIN

    normalized = _norm_user_email(email)
    if normalized.endswith(f"@{IDENTITY_DOMAIN}"):
        return normalized
    if not normalized.endswith(f"@{LEGACY_IDENTITY_DOMAIN}"):
        return normalized
    canonical_email = LEGACY_IDENTITY_ALIASES.get(normalized)
    if not canonical_email:
        return None
    return canonical_email if is_active_user(canonical_email) else None


def is_user_admin(email: str) -> bool:
    """True if role is admin or super_admin (and user is active)."""
    return get_user_role(email) in (ROLE_ADMIN, ROLE_SUPER_ADMIN)


def is_super_admin(email: str) -> bool:
    """True if role is super_admin (and user is active)."""
    return get_user_role(email) == ROLE_SUPER_ADMIN


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

def sync_user_identity_from_auth(
    *,
    email: str,
    name: Optional[str],
    picture: Optional[str] = None,
    source: str,
    uid: Optional[str] = None,
) -> bool:
    """
    Store a real provider display name on the allowlist user document.

    This is best-effort identity enrichment for admin/reporting surfaces. It must
    never block login if Firestore is briefly unavailable.
    """
    safe_email = _norm_user_email(email)
    display_name = clean_auth_display_name(name, safe_email)
    if not safe_email or not display_name:
        return False

    try:
        doc_ref = get_db().collection("users").document(safe_email)
        doc = doc_ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict() or {}
        updates: dict[str, object] = {}
        if str(data.get("display_name") or "").strip() != display_name:
            updates["display_name"] = display_name
            updates["name"] = display_name
        if picture and str(data.get("picture") or "").strip() != picture:
            updates["picture"] = picture
        if uid and str(data.get("firebase_uid") or "").strip() != uid:
            updates["firebase_uid"] = uid
        if source and str(data.get("display_name_source") or "").strip() != source:
            updates["display_name_source"] = source
        if not updates:
            return False
        updates["identity_updated_at"] = datetime.now(timezone.utc)
        doc_ref.set(updates, merge=True)
        return True
    except Exception as exc:
        logger.warning("firestore: could not sync user identity for %s: %s", safe_email, exc)
        return False


def get_or_create_profile(uid: str, email: str, name: str, picture: Optional[str]) -> UserProfile:
    """Return existing profile or create a default one on first login."""
    clean_name = clean_auth_display_name(name, email)
    doc_ref = get_db().collection("profiles").document(uid)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict() or {}
        updates: dict[str, object] = {}
        safe_email = _norm_user_email(email)
        if safe_email and str(data.get("email") or "").strip().lower() != safe_email:
            updates["email"] = safe_email
        if clean_name and str(data.get("name") or "").strip() != clean_name:
            updates["name"] = clean_name
            updates["display_name"] = clean_name
        if picture and str(data.get("picture") or "").strip() != picture:
            updates["picture"] = picture
        if updates:
            doc_ref.set(updates, merge=True)
            data.update(updates)
        return UserProfile(**data)

    # First login — create default profile
    profile = UserProfile(
        uid=uid,
        email=_norm_user_email(email),
        name=clean_name,
        display_name=clean_name or None,
        picture=picture,
    )
    doc_ref.set(profile.model_dump())
    return profile


def update_profile(uid: str, updates: dict) -> None:
    get_db().collection("profiles").document(uid).update(updates)


def add_open_tab(uid: str, tab_info: dict, max_tabs: int = 8) -> tuple[list[dict], Optional[int]]:
    """
    Prepend tab_info to open_tabs, evict oldest if over limit.
    tab_info must have keys: id, subject, status, priority.
    Returns (updated_tabs, evicted_id | None).
    """
    doc_ref = get_db().collection("profiles").document(uid)
    doc = doc_ref.get()
    tabs: list[dict] = (doc.to_dict() or {}).get("open_tabs", [])

    # Remove duplicate entry for this ticket (by id)
    tabs = [t for t in tabs if t.get("id") != tab_info["id"]]

    # Detect eviction: if already at max, the last entry will be dropped
    evicted_id: Optional[int] = None
    if len(tabs) >= max_tabs:
        evicted_id = tabs[-1].get("id")

    tabs.insert(0, tab_info)
    tabs = tabs[:max_tabs]

    doc_ref.update({"open_tabs": tabs})
    return tabs, evicted_id


def remove_open_tab(uid: str, ticket_id: int) -> None:
    doc_ref = get_db().collection("profiles").document(uid)
    doc = doc_ref.get()
    tabs: list[dict] = (doc.to_dict() or {}).get("open_tabs", [])
    tabs = [t for t in tabs if t.get("id") != ticket_id]
    doc_ref.update({"open_tabs": tabs})


def add_search_history(uid: str, query: str, search_type: str, max_items: int = 10) -> None:
    doc_ref = get_db().collection("profiles").document(uid)
    doc = doc_ref.get()
    history: list[dict] = (doc.to_dict() or {}).get("search_history", [])

    # Deduplicate by query+type then prepend
    history = [h for h in history if not (h.get("query") == query and h.get("type") == search_type)]
    history.insert(0, {"query": query, "type": search_type})
    history = history[:max_items]

    doc_ref.update({"search_history": history})


# ---------------------------------------------------------------------------
# Summary cache
# ---------------------------------------------------------------------------

def _summary_doc_ref(ticket_id: int, cache_key: str):
    return (
        get_db()
        .collection("cache")
        .document("summaries")
        .collection(str(ticket_id))
        .document(cache_key)
    )


def _summary_cache_key(mode: str, language: str) -> str:
    return mode if language == "en" else f"{mode}_{language}"


def get_cached_summary(ticket_id: int, mode: str, language: str = "en") -> Optional[dict]:
    """Return cached summary dict if it exists and has not expired, else None."""
    doc = _summary_doc_ref(ticket_id, _summary_cache_key(mode, language)).get()
    if not doc.exists:
        return None

    data = doc.to_dict() or {}

    # All new documents store expires_at; legacy documents (no expires_at) are treated as expired.
    expires_at: Optional[datetime] = data.get("expires_at")
    if expires_at is None:
        return None

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        return None  # Expired

    return data


def set_cached_summary(
    ticket_id: int,
    mode: str,
    summary_data: dict,
    language: str = "en",
    ticket_status: str = "open",
) -> None:
    """
    Write summary to Firestore cache after PII redaction.
    TTL is status-aware:
      solved/closed → 7 days  (ticket is frozen, Gemini call would be wasted)
      open/pending  → 4 hours (ticket is active, may receive new comments)
    """
    safe_data = _redact_summary_fields(summary_data)
    now = datetime.now(timezone.utc)
    ttl_hours = (
        SUMMARY_TTL_SOLVED_HOURS if ticket_status in _TERMINAL_STATUSES
        else SUMMARY_TTL_OPEN_HOURS
    )
    safe_data["generated_at"] = now
    safe_data["expires_at"] = now + timedelta(hours=ttl_hours)

    _summary_doc_ref(ticket_id, _summary_cache_key(mode, language)).set(safe_data)


def invalidate_summary_cache(ticket_id: int, mode: str, language: str = "en") -> None:
    """Delete a summary cache entry so the next request regenerates it."""
    _summary_doc_ref(ticket_id, _summary_cache_key(mode, language)).delete()


def _customer_risk_doc_ref(ticket_id: int):
    return (
        get_db()
        .collection("cache")
        .document("customer_risk")
        .collection(str(ticket_id))
        .document("latest")
    )


def get_cached_customer_risk(ticket_id: int) -> Optional[dict]:
    """Return cached customer risk assessment if it exists and has not expired."""
    doc = _customer_risk_doc_ref(ticket_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    expires_at: Optional[datetime] = data.get("expires_at")
    if expires_at is None:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        return None
    return data


def set_cached_customer_risk(
    ticket_id: int,
    assessment_data: dict,
    ticket_status: str = "open",
) -> None:
    """
    Cache customer risk assessment.

    Active tickets get a short TTL because comments and public reply timing can
    change quickly. Terminal tickets are stable enough to keep longer.
    """
    safe_data = _redact_summary_fields(assessment_data)
    now = datetime.now(timezone.utc)
    ttl_hours = (
        SUMMARY_TTL_SOLVED_HOURS if ticket_status in _TERMINAL_STATUSES
        else 0.5
    )
    safe_data["generated_at"] = now
    safe_data["expires_at"] = now + timedelta(hours=ttl_hours)
    _customer_risk_doc_ref(ticket_id).set(safe_data)


def invalidate_customer_risk_cache(ticket_id: int) -> None:
    """Delete cached customer risk assessment for a ticket."""
    _customer_risk_doc_ref(ticket_id).delete()


def _redact_summary_fields(data: dict) -> dict:
    """Recursively redact PII from all string values in a summary dict."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _redact_pii(value)
        elif isinstance(value, list):
            result[key] = [
                _redact_pii(item) if isinstance(item, str) else item
                for item in value
            ]
        elif isinstance(value, dict):
            result[key] = _redact_summary_fields(value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Store history cache
# ---------------------------------------------------------------------------

def _history_doc_ref(org_id: int):
    """Firestore ref for history cache keyed by organisation ID (not ticket ID).
    Two tickets from the same store share one cache entry — more efficient."""
    return (
        get_db()
        .collection("cache")
        .document("history")
        .collection(str(org_id))
        .document("data")
    )


def get_cached_history(org_id: int) -> Optional[list[dict]]:
    """Return cached history list if not expired, else None."""
    doc = _history_doc_ref(org_id).get()
    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    expires_at: Optional[datetime] = data.get("expires_at")
    if expires_at is None:
        return None

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        return None  # Expired

    return data.get("tickets")


def set_cached_history(org_id: int, tickets: list[dict]) -> None:
    """Cache store history with 2-hour TTL."""
    now = datetime.now(timezone.utc)
    _history_doc_ref(org_id).set({
        "tickets": tickets,
        "generated_at": now,
        "expires_at": now + timedelta(hours=HISTORY_TTL_HOURS),
    })


def invalidate_history_cache(org_id: int) -> None:
    """Delete history cache entry so the next request re-fetches from Zendesk."""
    _history_doc_ref(org_id).delete()


# ---------------------------------------------------------------------------
# My unsolved tickets cache
# ---------------------------------------------------------------------------

MY_UNSOLVED_TICKETS_TTL_MINUTES = 30
MY_UNSOLVED_TICKETS_LOCK_SECONDS = 120


def _my_unsolved_tickets_doc_ref(uid: str):
    return (
        get_db()
        .collection("cache")
        .document("my_unsolved_tickets")
        .collection(str(uid))
        .document("data")
    )


def get_my_unsolved_tickets_cache(uid: str) -> Optional[dict[str, Any]]:
    """Return the raw per-user My Unsolved Tickets cache, fresh or stale."""
    doc = _my_unsolved_tickets_doc_ref(uid).get()
    if not doc.exists:
        return None
    return doc.to_dict() or {}


def set_my_unsolved_tickets_cache(
    uid: str,
    *,
    email: str,
    tickets: list[dict[str, Any]],
    ttl_minutes: int = MY_UNSOLVED_TICKETS_TTL_MINUTES,
) -> dict[str, Any]:
    """Cache a live Zendesk My Unsolved Tickets snapshot for one user."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "email": _norm_user_email(email),
        "tickets": tickets,
        "fetched_at": now,
        "expires_at": now + timedelta(minutes=ttl_minutes),
        "refresh_lock_until": None,
        "refresh_error": None,
        "refresh_failed_at": None,
    }
    _my_unsolved_tickets_doc_ref(uid).set(payload, merge=True)
    return payload


def try_acquire_my_unsolved_tickets_refresh_lock(
    uid: str,
    *,
    lock_seconds: int = MY_UNSOLVED_TICKETS_LOCK_SECONDS,
) -> bool:
    """
    Best-effort anti-stampede lock for one user's live Zendesk refresh.

    The transaction keeps parallel page loads from creating multiple Zendesk
    Search API requests for the same engineer.
    """
    db = get_db()
    doc_ref = _my_unsolved_tickets_doc_ref(uid)
    transaction = db.transaction()
    now = datetime.now(timezone.utc)
    lock_until_next = now + timedelta(seconds=lock_seconds)

    @firestore.transactional
    def _acquire(txn) -> bool:
        snapshot = doc_ref.get(transaction=txn)
        data = snapshot.to_dict() if snapshot.exists else {}
        lock_until = data.get("refresh_lock_until")
        if isinstance(lock_until, datetime):
            if lock_until.tzinfo is None:
                lock_until = lock_until.replace(tzinfo=timezone.utc)
            if lock_until > now:
                return False
        txn.set(
            doc_ref,
            {
                "refresh_lock_until": lock_until_next,
                "refresh_started_at": now,
                "refresh_error": None,
            },
            merge=True,
        )
        return True

    return bool(_acquire(transaction))


def mark_my_unsolved_tickets_refresh_failed(uid: str, error: str) -> None:
    """Record a non-blocking live refresh failure and release the lock."""
    now = datetime.now(timezone.utc)
    _my_unsolved_tickets_doc_ref(uid).set(
        {
            "refresh_lock_until": None,
            "refresh_error": str(error)[:500],
            "refresh_failed_at": now,
        },
        merge=True,
    )


# ---------------------------------------------------------------------------
# BQ sync state (persistent across container restarts)
# ---------------------------------------------------------------------------

_SYNC_DOC = ("system", "bq_sync")
_DRIVE_SYNC_DOC = ("system", "drive_sync")


def get_sync_cursor() -> Optional[int]:
    """
    Return the Zendesk incremental API cursor (Unix timestamp int) from the last
    successful sync, or None if never synced.

    This is the `end_time` value returned by Zendesk at the end of the previous
    incremental export. Passing it as `start_time` to the next call guarantees
    zero gaps and zero duplicate processing — exactly as Zendesk designed.
    """
    doc = get_db().collection(_SYNC_DOC[0]).document(_SYNC_DOC[1]).get()
    if not doc.exists:
        return None
    return (doc.to_dict() or {}).get("sync_cursor")


def set_sync_cursor(cursor: int) -> None:
    """
    Persist the Zendesk incremental API cursor after a fully successful sync
    (tickets fetched + BQ writes complete). Never call this on partial success.
    """
    get_db().collection(_SYNC_DOC[0]).document(_SYNC_DOC[1]).set(
        {"sync_cursor": cursor}, merge=True
    )


def get_bq_sync_state() -> dict:
    """Return persistent Zendesk/BQ sync state from system/bq_sync."""
    doc = get_db().collection(_SYNC_DOC[0]).document(_SYNC_DOC[1]).get()
    if not doc.exists:
        return {
            "sync_cursor": None,
            "running": False,
            "progress": None,
            "last_run": None,
            "last_result": None,
            "last_error": None,
            "last_request": None,
            "heartbeat_at": None,
            "run_owner": None,
        }
    data = doc.to_dict() or {}
    return {
        "sync_cursor": data.get("sync_cursor"),
        "running": bool(data.get("running", False)),
        "progress": data.get("progress"),
        "last_run": data.get("last_run"),
        "last_result": data.get("last_result"),
        "last_error": data.get("last_error"),
        "last_request": data.get("last_request"),
        "heartbeat_at": data.get("heartbeat_at"),
        "run_owner": data.get("run_owner"),
    }


def set_bq_sync_state_fields(fields: dict) -> None:
    """Update persistent Zendesk/BQ sync state without deeply merging result maps."""
    payload = {**fields, "updated_at": datetime.now(timezone.utc)}
    doc_ref = get_db().collection(_SYNC_DOC[0]).document(_SYNC_DOC[1])
    if doc_ref.get().exists:
        doc_ref.update(payload)
    else:
        doc_ref.set(payload, merge=True)


def get_drive_sync_state() -> dict:
    """Return persistent Drive sync state from system/drive_sync."""
    doc = get_db().collection(_DRIVE_SYNC_DOC[0]).document(_DRIVE_SYNC_DOC[1]).get()
    if not doc.exists:
        return {
            "running": False,
            "progress": None,
            "last_run": None,
            "last_result": None,
            "last_error": None,
            "last_request": None,
            "heartbeat_at": None,
            "run_owner": None,
        }
    data = doc.to_dict() or {}
    return {
        "running": bool(data.get("running", False)),
        "progress": data.get("progress"),
        "last_run": data.get("last_run"),
        "last_result": data.get("last_result"),
        "last_error": data.get("last_error"),
        "last_request": data.get("last_request"),
        "heartbeat_at": data.get("heartbeat_at"),
        "run_owner": data.get("run_owner"),
    }


def set_drive_sync_state_fields(fields: dict) -> None:
    """Update persistent Drive sync state without deeply merging result maps."""
    payload = {**fields, "updated_at": datetime.now(timezone.utc)}
    doc_ref = get_db().collection(_DRIVE_SYNC_DOC[0]).document(_DRIVE_SYNC_DOC[1])
    if doc_ref.get().exists:
        doc_ref.update(payload)
    else:
        doc_ref.set(payload, merge=True)


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def get_all_feedback_entries() -> list[dict]:
    """
    Return all feedback entries from all users, newest first. Admin use only.
    Reads feedback/{uid} documents, then fetches entries subcollection for each user.
    Avoids collection_group index requirement.
    """
    db = get_db()
    result = []
    for user_doc in db.collection("feedback").stream():
        for entry_doc in (
            db.collection("feedback").document(user_doc.id)
            .collection("entries")
            .stream()
        ):
            data = entry_doc.to_dict() or {}
            if "text" in data:
                result.append({"id": entry_doc.id, "uid": user_doc.id, **data})
    result.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return result


def add_feedback_entry(uid: str, email: str, name: str, text: str) -> dict:
    """Create a new feedback entry for a user. Returns the created document as a dict."""
    now = datetime.now(timezone.utc)
    entry = {
        "text": text.strip(),
        "created_at": now,
        "updated_at": now,
        "user_email": email,
        "user_name": name,
    }
    db = get_db()
    # Ensure parent document exists so collection("feedback").stream() can find this user.
    # Firestore does not return implicit/virtual parent docs in collection queries.
    db.collection("feedback").document(uid).set(
        {"user_email": email, "user_name": name}, merge=True
    )
    _, doc_ref = (
        db.collection("feedback")
        .document(uid)
        .collection("entries")
        .add(entry)
    )
    return {"id": doc_ref.id, **entry}


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

_DATA_SOURCES_DOC = ("system", "data_sources")

# ---------------------------------------------------------------------------
# Maintenance mode
# ---------------------------------------------------------------------------

_maintenance_cache: Optional[tuple[bool, float]] = None
_MAINTENANCE_CACHE_TTL = 30.0  # seconds


def is_maintenance_mode() -> bool:
    """
    Return True if maintenance mode is currently enabled.
    Checked via Firestore system/maintenance {enabled: bool}.
    Result is cached for 30 seconds to avoid a Firestore read on every request.
    """
    global _maintenance_cache
    now = time.monotonic()
    if _maintenance_cache is not None:
        enabled, fetched_at = _maintenance_cache
        if now - fetched_at < _MAINTENANCE_CACHE_TTL:
            return enabled
    doc = get_db().collection("system").document("maintenance").get()
    enabled = bool((doc.to_dict() or {}).get("enabled", False)) if doc.exists else False
    _maintenance_cache = (enabled, now)
    return enabled


def set_maintenance_enabled(enabled: bool) -> None:
    """Write system/maintenance {enabled} and invalidate the in-process cache."""
    get_db().collection("system").document("maintenance").set({"enabled": enabled}, merge=True)
    global _maintenance_cache
    _maintenance_cache = None


def is_maintenance_blocked(email: str) -> bool:
    """
    Return True if this user should be blocked due to maintenance mode.
    Only admin and super_admin pass through during maintenance.
    Unprovisioned or inactive users are not "maintenance" — they stay 403 not provisioned.
    Returns False immediately (no second Firestore read) when maintenance mode is off.
    """
    if not is_maintenance_mode() or not email:
        return False
    email = _norm_user_email(email)
    if not email:
        return False
    doc = get_db().collection("users").document(email).get()
    if not doc.exists:
        return False
    data = doc.to_dict() or {}
    if not bool(data.get("active", False)):
        return False
    return _role_from_doc(data) == ROLE_USER

# In-process cache: (data, fetched_at_monotonic)
_data_sources_cache: Optional[tuple[dict, float]] = None
_DATA_SOURCES_CACHE_TTL = 30.0  # seconds


# Default state written to Firestore on first access if doc doesn't exist.
_DEFAULT_DATA_SOURCES = {
    "sources": {
        "zendesk_support_articles": {
            "display_name": "Support Articles",
            "type": "vertex_ai",
            "enabled": True,
            "sync_enabled": True,
        },
        "zendesk_operations_articles": {
            "display_name": "Operations Articles",
            "type": "vertex_ai",
            "enabled": False,
            "sync_enabled": True,
        },
        "tickets_bq": {
            "display_name": "BQ Tickets — analytics + semantic search",
            "type": "bigquery",
            "enabled": True,
            "sync_enabled": True,
        },
        "tickets_zendesk": {
            "display_name": "Zendesk API — keyword + store lookup",
            "type": "zendesk_api",
            "enabled": True,
            "sync_enabled": True,
        },
        "reference_support_docs": {
            "display_name": "Support Documentation (Google Drive)",
            "type": "vertex_ai",
            "enabled": True,
            "sync_enabled": True,
            "exclude_path_prefixes": [],
        },
        "gdrive_operations_job_aids": {
            "display_name": "Store Operations Job Aids",
            "type": "vertex_ai",
            "enabled": True,
            "sync_enabled": True,
            "exclude_path_prefixes": [],
        },
        "gdrive_support_repository": {
            "display_name": "Support Knowledge Repository (Google Drive)",
            "type": "vertex_ai",
            "enabled": False,
            "sync_enabled": False,
            "exclude_path_prefixes": [],
        },
    }
}


def get_data_sources() -> dict:
    """
    Return the data_sources dict from Firestore, cached for 30 seconds.
    Returns the 'sources' sub-dict: {source_id: {display_name, type, enabled, ...}}.
    Creates the default document if it doesn't exist yet.
    """
    global _data_sources_cache
    now = time.monotonic()

    if _data_sources_cache is not None:
        data, fetched_at = _data_sources_cache
        if now - fetched_at < _DATA_SOURCES_CACHE_TTL:
            return data

    doc = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1]).get()
    if not doc.exists:
        # First run — write defaults
        get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1]).set(
            _DEFAULT_DATA_SOURCES
        )
        sources = _DEFAULT_DATA_SOURCES["sources"]
    else:
        sources = (doc.to_dict() or {}).get("sources", {})
        # Backfill newly introduced defaults for existing environments.
        missing_updates = {
            f"sources.{sid}": cfg
            for sid, cfg in _DEFAULT_DATA_SOURCES["sources"].items()
            if sid not in sources
        }
        for sid, cfg in sources.items():
            if "sync_enabled" not in cfg:
                missing_updates[f"sources.{sid}.sync_enabled"] = True
        if missing_updates:
            doc_ref = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1])
            doc_ref.update(missing_updates)
            sources = {
                **_DEFAULT_DATA_SOURCES["sources"],
                **sources,
            }
            for sid in sources:
                if "sync_enabled" not in sources[sid]:
                    sources[sid]["sync_enabled"] = True

    _data_sources_cache = (sources, now)
    return sources


def get_enabled_vertex_sources() -> list[str]:
    """
    Return source_ids of all enabled Vertex AI KB sources.
    Used by search_kb() to build the filter expression.
    """
    sources = get_data_sources()
    return [
        sid for sid, cfg in sources.items()
        if cfg.get("type") == "vertex_ai" and cfg.get("enabled", False)
    ]


def is_source_enabled(source_key: str) -> bool:
    """Check if a specific data source toggle is enabled (any type)."""
    sources = get_data_sources()
    return bool(sources.get(source_key, {}).get("enabled", False))


def update_data_source(source_id: str, enabled: bool) -> Optional[dict]:
    """
    Toggle a data source on or off. Returns the updated source config or None if not found.
    Invalidates the in-process cache immediately.
    """
    global _data_sources_cache

    doc_ref = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1])
    doc = doc_ref.get()
    if not doc.exists:
        return None

    sources = (doc.to_dict() or {}).get("sources", {})
    if source_id not in sources:
        return None

    doc_ref.update({f"sources.{source_id}.enabled": enabled})
    _data_sources_cache = None  # invalidate immediately

    updated = {**sources[source_id], "enabled": enabled}
    return updated


def update_data_source_sync(source_id: str, sync_enabled: bool) -> Optional[dict]:
    """
    Toggle whether a data source is eligible for synchronization.
    Returns updated source config or None if source is not found.
    """
    global _data_sources_cache

    doc_ref = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1])
    doc = doc_ref.get()
    if not doc.exists:
        return None

    sources = (doc.to_dict() or {}).get("sources", {})
    if source_id not in sources:
        return None

    doc_ref.update({f"sources.{source_id}.sync_enabled": sync_enabled})
    _data_sources_cache = None

    updated = {**sources[source_id], "sync_enabled": sync_enabled}
    return updated


def update_data_source_paths(
    source_id: str,
    exclude_path_prefixes: list[str],
) -> Optional[dict]:
    """
    Update subfolder exclusion prefixes for a data source.
    Returns updated source config or None if source is not found.
    """
    global _data_sources_cache

    doc_ref = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1])
    doc = doc_ref.get()
    if not doc.exists:
        return None

    sources = (doc.to_dict() or {}).get("sources", {})
    if source_id not in sources:
        return None

    cleaned = []
    seen: set[str] = set()
    for p in exclude_path_prefixes:
        norm = "/".join(part.strip() for part in str(p).strip().strip("/").split("/") if part.strip())
        if not norm:
            continue
        low = norm.lower()
        if low in seen:
            continue
        seen.add(low)
        cleaned.append(norm)

    doc_ref.update({f"sources.{source_id}.exclude_path_prefixes": cleaned})
    _data_sources_cache = None

    updated = {**sources[source_id], "exclude_path_prefixes": cleaned}
    return updated


def get_all_data_sources_raw() -> dict:
    """Return the full data_sources document (for admin UI). Bypasses cache."""
    doc = get_db().collection(_DATA_SOURCES_DOC[0]).document(_DATA_SOURCES_DOC[1]).get()
    if not doc.exists:
        return _DEFAULT_DATA_SOURCES
    return doc.to_dict() or _DEFAULT_DATA_SOURCES


def get_feedback_entries(uid: str) -> list[dict]:
    """Return all feedback entries for a user, newest first."""
    docs = (
        get_db()
        .collection("feedback")
        .document(uid)
        .collection("entries")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .stream()
    )
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in docs]


def update_feedback_entry(uid: str, entry_id: str, text: str) -> Optional[dict]:
    """Update the text of an existing feedback entry. Returns updated dict or None if not found."""
    doc_ref = (
        get_db()
        .collection("feedback")
        .document(uid)
        .collection("entries")
        .document(entry_id)
    )
    doc = doc_ref.get()
    if not doc.exists:
        return None

    now = datetime.now(timezone.utc)
    updates = {"text": text.strip(), "updated_at": now}
    doc_ref.update(updates)

    data = doc.to_dict() or {}
    data.update(updates)
    return {"id": entry_id, **data}


def get_feedback_entry(uid: str, entry_id: str) -> Optional[dict]:
    """Return one feedback entry for uid or None if not found."""
    doc_ref = (
        get_db()
        .collection("feedback")
        .document(uid)
        .collection("entries")
        .document(entry_id)
    )
    doc = doc_ref.get()
    if not doc.exists:
        return None
    return {"id": entry_id, "uid": uid, **(doc.to_dict() or {})}


def get_feedback_replies(uid: str, entry_id: str) -> list[dict]:
    """Return admin/user replies for a feedback entry, oldest first."""
    docs = (
        get_db()
        .collection("feedback")
        .document(uid)
        .collection("entries")
        .document(entry_id)
        .collection("replies")
        .order_by("created_at", direction=firestore.Query.ASCENDING)
        .stream()
    )
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in docs]


def add_feedback_reply(
    uid: str,
    entry_id: str,
    text: str,
    author_email: str,
    author_name: str,
    is_admin_reply: bool = True,
) -> Optional[dict]:
    """
    Add a reply to a feedback entry.
    Returns created reply dict or None if parent entry does not exist.
    """
    parent_ref = (
        get_db()
        .collection("feedback")
        .document(uid)
        .collection("entries")
        .document(entry_id)
    )
    if not parent_ref.get().exists:
        return None

    now = datetime.now(timezone.utc)
    reply = {
        "text": text.strip(),
        "author_email": author_email,
        "author_name": author_name,
        "is_admin_reply": bool(is_admin_reply),
        "created_at": now,
    }
    _, doc_ref = parent_ref.collection("replies").add(reply)
    return {"id": doc_ref.id, **reply}


def add_feedback_inbox_message(
    uid: str,
    entry_id: str,
    reply_id: str,
    preview: str,
) -> dict:
    """
    Create unread inbox notification for feedback reply.
    Collection: users/{uid}/inbox/{message_id}
    """
    now = datetime.now(timezone.utc)
    msg = {
        "type": "feedback_reply",
        "entry_id": entry_id,
        "reply_id": reply_id,
        "preview": preview.strip()[:240],
        "created_at": now,
        "read_at": None,
    }
    _, doc_ref = (
        get_db()
        .collection("users")
        .document(uid)
        .collection("inbox")
        .add(msg)
    )
    return {"id": doc_ref.id, **msg}


def get_feedback_inbox(uid: str, unread_only: bool = True) -> list[dict]:
    """Return inbox feedback-reply notifications, newest first."""
    query = (
        get_db()
        .collection("users")
        .document(uid)
        .collection("inbox")
        .where("type", "==", "feedback_reply")
    )
    docs = [{"id": doc.id, **(doc.to_dict() or {})} for doc in query.stream()]
    if unread_only:
        docs = [d for d in docs if d.get("read_at") is None]
    docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return docs


def mark_feedback_inbox_read(uid: str, message_ids: Optional[list[str]] = None) -> int:
    """
    Mark feedback inbox messages as read.
    If message_ids omitted, marks all unread feedback_reply messages.
    Returns number of updated messages.
    """
    db = get_db()
    base = db.collection("users").document(uid).collection("inbox").where("type", "==", "feedback_reply")
    now = datetime.now(timezone.utc)
    updated = 0

    if message_ids:
        for msg_id in message_ids:
            doc_ref = db.collection("users").document(uid).collection("inbox").document(msg_id)
            snap = doc_ref.get()
            if not snap.exists:
                continue
            data = snap.to_dict() or {}
            if data.get("type") != "feedback_reply":
                continue
            if data.get("read_at") is not None:
                continue
            doc_ref.update({"read_at": now})
            updated += 1
        return updated

    docs = base.stream()
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("read_at") is not None:
            continue
        db.collection("users").document(uid).collection("inbox").document(doc.id).update({"read_at": now})
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# ZAF session tokens
# ---------------------------------------------------------------------------

_ZAF_SESSION_TTL_HOURS = 8


def create_zaf_session(email: str, name: str, role: str) -> str:
    """
    Create a short-lived ZAF session token in Firestore.
    Returns the token string. TTL: 8 hours.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=_ZAF_SESSION_TTL_HOURS)
    get_db().collection("zaf_sessions").document(token).set({
        "email": email,
        "name": name,
        "role": role,
        "expires_at": expires_at,
    })
    return token


def validate_zaf_session(token: str) -> Optional[dict]:
    """
    Validate a ZAF session token. Returns {email, name, role} if valid, None if expired/missing.
    Does NOT check the allowlist — that was checked at login time.
    """
    doc = get_db().collection("zaf_sessions").document(token).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    expires_at = data.get("expires_at")
    if expires_at is None:
        return None
    # Firestore returns datetime objects; compare to now
    now = datetime.now(timezone.utc)
    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now > expires_at:
        # Clean up expired token
        get_db().collection("zaf_sessions").document(token).delete()
        return None
    return {"email": data.get("email", ""), "name": data.get("name", ""), "role": data.get("role", "")}
