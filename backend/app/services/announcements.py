"""Product change candidates and one-time user announcements."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from google.cloud import firestore as gc_firestore

from app.services.firestore import get_db

CHANGE_CANDIDATES = "change_candidates"
PRODUCT_ANNOUNCEMENTS = "product_announcements"
ANNOUNCEMENT_READS = "announcement_reads"

VALID_CHANGE_TYPES = {"ui", "feature", "workflow", "admin", "zendesk_app", "other"}
VALID_IMPACTS = {"low", "medium", "high"}
VALID_SEVERITIES = {"info", "important", "breaking"}
VALID_SURFACES = {"web", "zendesk_app", "admin"}
VALID_AUDIENCE_ROLES = {"all", "user", "admin"}
DEFAULT_AUDIENCE_ROLES = ["user", "admin"]
DEFAULT_SURFACES = ["web"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _doc_id(prefix: str) -> str:
    return f"{prefix}_{_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"


def _as_iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _with_id(doc: Any) -> dict[str, Any]:
    data = doc.to_dict() or {}
    return {"id": doc.id, **{k: _as_iso(v) for k, v in data.items()}}


def _clean_string_list(values: list[str] | None, *, allowed: set[str] | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = str(raw).strip()
        if not value:
            continue
        if allowed is not None and value not in allowed:
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def create_change_candidate(
    *,
    title: str,
    summary: str,
    change_type: str = "other",
    impact: str = "medium",
    source: str = "manual",
    related_files: list[str] | None = None,
    audience_roles: list[str] | None = None,
    surfaces: list[str] | None = None,
    severity: str = "info",
    created_by: str = "",
) -> dict[str, Any]:
    title = title.strip()
    summary = summary.strip()
    if not title:
        raise ValueError("title is required")
    if not summary:
        raise ValueError("summary is required")
    if change_type not in VALID_CHANGE_TYPES:
        raise ValueError(f"change_type must be one of {sorted(VALID_CHANGE_TYPES)}")
    if impact not in VALID_IMPACTS:
        raise ValueError(f"impact must be one of {sorted(VALID_IMPACTS)}")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(VALID_SEVERITIES)}")

    candidate_id = _doc_id("chg")
    now = _now()
    data = {
        "title": title,
        "summary": summary,
        "change_type": change_type,
        "impact": impact,
        "source": source.strip() or "manual",
        "related_files": _clean_string_list(related_files),
        "audience_roles": _clean_audience_roles(audience_roles),
        "surfaces": _clean_surfaces(surfaces),
        "severity": severity,
        "status": "draft",
        "created_by": created_by.strip(),
        "created_at": now,
        "updated_at": now,
        "published_as_announcement_id": None,
    }
    get_db().collection(CHANGE_CANDIDATES).document(candidate_id).set(data)
    return {"id": candidate_id, **{k: _as_iso(v) for k, v in data.items()}}


def list_change_candidates(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    query = (
        get_db()
        .collection(CHANGE_CANDIDATES)
        .order_by("created_at", direction=gc_firestore.Query.DESCENDING)
        .limit(max(1, min(limit, 200)))
    )
    items = [_with_id(doc) for doc in query.stream()]
    if status:
        items = [item for item in items if item.get("status") == status]
    return items


def ignore_change_candidate(candidate_id: str, *, ignored_by: str) -> dict[str, Any] | None:
    ref = get_db().collection(CHANGE_CANDIDATES).document(candidate_id)
    doc = ref.get()
    if not doc.exists:
        return None
    updates = {
        "status": "ignored",
        "ignored_by": ignored_by,
        "ignored_at": _now(),
        "updated_at": _now(),
    }
    ref.set(updates, merge=True)
    return {"id": candidate_id, **{**(doc.to_dict() or {}), **{k: _as_iso(v) for k, v in updates.items()}}}


def publish_change_candidate(
    candidate_id: str,
    *,
    title: str | None = None,
    body_markdown: str | None = None,
    audience_roles: list[str] | None = None,
    surfaces: list[str] | None = None,
    severity: str = "info",
    active_until: datetime | None = None,
    published_by: str,
) -> dict[str, Any] | None:
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(VALID_SEVERITIES)}")

    db = get_db()
    candidate_ref = db.collection(CHANGE_CANDIDATES).document(candidate_id)
    candidate_doc = candidate_ref.get()
    if not candidate_doc.exists:
        return None
    candidate = candidate_doc.to_dict() or {}

    roles = _clean_audience_roles(audience_roles if audience_roles is not None else candidate.get("audience_roles"))
    surface_values = _clean_surfaces(surfaces if surfaces is not None else candidate.get("surfaces"))

    announcement_id = _doc_id("ann")
    now = _now()
    announcement = {
        "title": (title or candidate.get("title") or "").strip(),
        "body_markdown": (body_markdown or candidate.get("summary") or "").strip(),
        "source_candidate_id": candidate_id,
        "audience_roles": roles,
        "surfaces": surface_values,
        "severity": severity,
        "active_from": now,
        "active_until": active_until,
        "published_by": published_by,
        "published_at": now,
        "created_at": now,
        "updated_at": now,
        "status": "published",
    }
    if not announcement["title"]:
        raise ValueError("announcement title is required")
    if not announcement["body_markdown"]:
        raise ValueError("announcement body_markdown is required")

    db.collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id).set(announcement)
    candidate_ref.set(
        {
            "status": "published",
            "published_as_announcement_id": announcement_id,
            "published_by": published_by,
            "published_at": now,
            "updated_at": now,
        },
        merge=True,
    )
    return {"id": announcement_id, **{k: _as_iso(v) for k, v in announcement.items()}}


def list_published_announcements(limit: int = 100, *, include_unpublished: bool = False) -> list[dict[str, Any]]:
    query = (
        get_db()
        .collection(PRODUCT_ANNOUNCEMENTS)
        .order_by("published_at", direction=gc_firestore.Query.DESCENDING)
        .limit(max(1, min(limit, 200)))
    )
    items = [_with_id(doc) for doc in query.stream()]
    if not include_unpublished:
        items = [item for item in items if item.get("status") == "published"]
    return items


def unpublish_announcement(announcement_id: str, *, unpublished_by: str) -> dict[str, Any] | None:
    ref = get_db().collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id)
    doc = ref.get()
    if not doc.exists:
        return None
    now = _now()
    updates = {
        "status": "unpublished",
        "unpublished_by": unpublished_by.strip(),
        "unpublished_at": now,
        "updated_at": now,
    }
    ref.set(updates, merge=True)
    current = doc.to_dict() or {}
    return {"id": announcement_id, **{**current, **{k: _as_iso(v) for k, v in updates.items()}}}


def republish_announcement(announcement_id: str, *, republished_by: str) -> dict[str, Any] | None:
    ref = get_db().collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id)
    doc = ref.get()
    if not doc.exists:
        return None
    now = _now()
    updates = {
        "status": "published",
        "published_at": now,
        "active_from": now,
        "republished_by": republished_by.strip(),
        "republished_at": now,
        "updated_at": now,
    }
    ref.set(updates, merge=True)
    current = doc.to_dict() or {}
    return {"id": announcement_id, **{**current, **{k: _as_iso(v) for k, v in updates.items()}}}


def update_announcement(
    announcement_id: str,
    *,
    title: str,
    body_markdown: str,
    severity: str,
    audience_roles: list[str] | None = None,
    surfaces: list[str] | None = None,
    edited_by: str,
) -> dict[str, Any] | None:
    ref = get_db().collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id)
    doc = ref.get()
    if not doc.exists:
        return None

    title = title.strip()
    body_markdown = body_markdown.strip()
    if not title:
        raise ValueError("announcement title is required")
    if not body_markdown:
        raise ValueError("announcement body_markdown is required")
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(VALID_SEVERITIES)}")

    now = _now()
    updates = {
        "title": title,
        "body_markdown": body_markdown,
        "severity": severity,
        "edited_by": edited_by.strip(),
        "edited_at": now,
        "updated_at": now,
    }
    if audience_roles is not None:
        updates["audience_roles"] = _clean_audience_roles(audience_roles)
    if surfaces is not None:
        updates["surfaces"] = _clean_surfaces(surfaces)
    ref.set(updates, merge=True)
    current = doc.to_dict() or {}
    return {"id": announcement_id, **{**current, **{k: _as_iso(v) for k, v in updates.items()}}}


def delete_announcement(announcement_id: str, *, deleted_by: str) -> dict[str, Any] | None:
    db = get_db()
    ref = db.collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id)
    doc = ref.get()
    if not doc.exists:
        return None

    read_docs = list(
        db.collection(ANNOUNCEMENT_READS)
        .where("announcement_id", "==", announcement_id)
        .stream()
    )

    deleted_reads = 0
    batch = db.batch()
    batch.delete(ref)
    pending = 1
    for read_doc in read_docs:
        batch.delete(read_doc.reference)
        deleted_reads += 1
        pending += 1
        if pending >= 450:
            batch.commit()
            batch = db.batch()
            pending = 0
    if pending:
        batch.commit()

    return {
        "id": announcement_id,
        "deleted": True,
        "deleted_by": deleted_by.strip(),
        "deleted_at": _as_iso(_now()),
        "deleted_read_count": deleted_reads,
    }


def _is_active(announcement: dict[str, Any], now: datetime) -> bool:
    if announcement.get("status") != "published":
        return False
    active_from = announcement.get("active_from")
    active_until = announcement.get("active_until")
    if isinstance(active_from, datetime) and active_from > now:
        return False
    if isinstance(active_until, datetime) and active_until < now:
        return False
    return True


def _matches_user(announcement: dict[str, Any], *, role: str, surface: str) -> bool:
    if role == "super_admin":
        return False
    roles = _audience_roles_for_match(announcement.get("audience_roles"))
    surfaces = _clean_surfaces(announcement.get("surfaces"))
    if surface not in surfaces:
        return False
    return "all" in roles or role in roles


def _clean_audience_roles(values: Any) -> list[str]:
    roles = _clean_string_list(
        values if isinstance(values, list) else None,
        allowed=VALID_AUDIENCE_ROLES,
    )
    return roles or list(DEFAULT_AUDIENCE_ROLES)


def _audience_roles_for_match(values: Any) -> list[str]:
    if not isinstance(values, list):
        return list(DEFAULT_AUDIENCE_ROLES)
    return _clean_string_list(values, allowed=VALID_AUDIENCE_ROLES)


def _clean_surfaces(values: Any) -> list[str]:
    surfaces = _clean_string_list(
        values if isinstance(values, list) else None,
        allowed=VALID_SURFACES,
    )
    return surfaces or list(DEFAULT_SURFACES)


def _read_id(email: str, announcement_id: str) -> str:
    return f"{email.lower()}__{announcement_id}"


def list_active_for_user(
    *,
    email: str,
    role: str,
    surface: str = "web",
    include_dismissed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    now = _now()
    announcements = list_published_announcements(limit=limit)
    read_docs = {}
    if email:
        read_refs = [
            get_db().collection(ANNOUNCEMENT_READS).document(_read_id(email, item["id"]))
            for item in announcements
        ]
        if read_refs:
            read_docs = {
                doc.id: (doc.to_dict() or {})
                for doc in get_db().get_all(read_refs)
                if doc.exists
            }

    out: list[dict[str, Any]] = []
    for item in announcements:
        raw = dict(item)
        read = read_docs.get(_read_id(email, item["id"]), {})
        dismissed = bool(read.get("dismissed_at"))
        if not _is_active(raw, now):
            continue
        if not _matches_user(raw, role=role, surface=surface):
            continue
        if dismissed and not include_dismissed:
            continue
        raw["read_state"] = {k: _as_iso(v) for k, v in read.items()} if read else None
        out.append(raw)
    return out


def list_history_for_user(
    *,
    email: str,
    role: str,
    surface: str = "web",
    limit: int = 100,
) -> list[dict[str, Any]]:
    announcements = [
        item
        for item in list_published_announcements(limit=limit)
        if _matches_user(item, role=role, surface=surface)
    ]
    read_docs = {}
    if email:
        read_refs = [
            get_db().collection(ANNOUNCEMENT_READS).document(_read_id(email, item["id"]))
            for item in announcements
        ]
        if read_refs:
            read_docs = {
                doc.id: (doc.to_dict() or {})
                for doc in get_db().get_all(read_refs)
                if doc.exists
            }
    for item in announcements:
        read = read_docs.get(_read_id(email, item["id"]), {})
        item["read_state"] = {k: _as_iso(v) for k, v in read.items()} if read else None
    return announcements


def dismiss_announcement(*, announcement_id: str, email: str, role: str, action: str = "dismissed") -> dict[str, Any] | None:
    ann_ref = get_db().collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id)
    ann_doc = ann_ref.get()
    if not ann_doc.exists:
        return None
    now = _now()
    read_ref = get_db().collection(ANNOUNCEMENT_READS).document(_read_id(email, announcement_id))
    existing = read_ref.get()
    data = existing.to_dict() if existing.exists else {}
    update = {
        "announcement_id": announcement_id,
        "user_email": email.lower(),
        "user_role": role,
        "action": action,
        "first_seen_at": data.get("first_seen_at") or now,
        "dismissed_at": now,
        "updated_at": now,
    }
    read_ref.set(update, merge=True)
    return {k: _as_iso(v) for k, v in update.items()}


def get_announcement_stats(announcement_id: str) -> dict[str, Any] | None:
    ann_doc = get_db().collection(PRODUCT_ANNOUNCEMENTS).document(announcement_id).get()
    if not ann_doc.exists:
        return None
    reads = (
        get_db()
        .collection(ANNOUNCEMENT_READS)
        .where("announcement_id", "==", announcement_id)
        .stream()
    )
    read_items = [_with_id(doc) for doc in reads]
    dismissed = [item for item in read_items if item.get("dismissed_at")]
    return {
        "announcement": _with_id(ann_doc),
        "read_count": len(read_items),
        "dismissed_count": len(dismissed),
        "reads": read_items,
    }
