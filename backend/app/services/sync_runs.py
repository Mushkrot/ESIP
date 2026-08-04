"""Operational history for scheduled and manual sync runs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.firestore import get_db, get_data_sources

_COLLECTION = "sync_runs"
_MAX_EVENTS_PER_RUN = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def source_settings_snapshot() -> dict[str, dict[str, Any]]:
    """Capture source Search/Sync flags at run start."""
    snapshot: dict[str, dict[str, Any]] = {}
    for source_id, cfg in get_data_sources().items():
        snapshot[source_id] = {
            "display_name": cfg.get("display_name", source_id),
            "type": cfg.get("type"),
            "enabled": bool(cfg.get("enabled", False)),
            "sync_enabled": bool(cfg.get("sync_enabled", True)),
        }
    return snapshot


def start_run(
    *,
    job_id: str,
    trigger: str,
    request: dict[str, Any] | None = None,
    started_by: str | None = None,
    source_settings: dict[str, Any] | None = None,
) -> str:
    run_id = f"{job_id}_{_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    payload = {
        "run_id": run_id,
        "job_id": job_id,
        "trigger": trigger,
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "duration_sec": None,
        "started_by": started_by,
        "request": _clean(request or {}),
        "source_settings": _clean(source_settings or source_settings_snapshot()),
        "summary": {},
        "source_results": {},
        "cost_summary": {},
        "cursor_before": None,
        "cursor_after": None,
        "error_count": 0,
        "updated_at": _now(),
    }
    get_db().collection(_COLLECTION).document(run_id).set(payload)
    add_event(run_id, stage="run", message="Run started", details={"job_id": job_id, "trigger": trigger})
    return run_id


def add_event(
    run_id: str | None,
    *,
    stage: str,
    message: str,
    level: str = "INFO",
    source_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if not run_id:
        return
    doc = get_db().collection(_COLLECTION).document(run_id)
    now = _now()
    event_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:6]}"
    event = {
        "timestamp": now,
        "level": level,
        "stage": stage,
        "source_id": source_id,
        "message": message[:2000],
        "details": _clean(details or {}),
    }
    doc.collection("events").document(event_id).set(event)
    doc.set({"updated_at": now, "last_event": event}, merge=True)


def finish_run(
    run_id: str | None,
    *,
    status: str,
    summary: dict[str, Any] | None = None,
    source_results: dict[str, Any] | None = None,
    cost_summary: dict[str, Any] | None = None,
    cursor_before: int | None = None,
    cursor_after: int | None = None,
    error_count: int = 0,
    error_message: str | None = None,
) -> None:
    if not run_id:
        return
    ref = get_db().collection(_COLLECTION).document(run_id)
    doc = ref.get()
    data = doc.to_dict() or {}
    started_at = data.get("started_at")
    finished_at = _now()
    duration_sec = None
    if isinstance(started_at, datetime):
        duration_sec = int((finished_at - started_at).total_seconds())
    payload = {
        "status": status,
        "finished_at": finished_at,
        "duration_sec": duration_sec,
        "summary": _clean(summary or {}),
        "source_results": _clean(source_results or {}),
        "cost_summary": _clean(cost_summary or {}),
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "error_count": int(error_count or 0),
        "error_message": error_message,
        "updated_at": finished_at,
    }
    ref.set(payload, merge=True)
    add_event(
        run_id,
        stage="run",
        message=f"Run {status}",
        level="ERROR" if status in {"failed", "blocked"} else "INFO",
        details={"error_count": error_count, "error_message": error_message},
    )
    try:
        from app.services.sync_run_mirror import mirror_sync_run  # noqa: PLC0415

        final_doc = ref.get().to_dict() or {}
        mirror_sync_run(run_id, {"id": run_id, **final_doc})
    except Exception:
        pass


def list_runs(*, job_id: str | None = None, status: str | None = None, days: int = 7, limit: int = 50) -> list[dict[str, Any]]:
    since = _now() - timedelta(days=max(1, min(days, 90)))
    query = get_db().collection(_COLLECTION).where("started_at", ">=", since)
    docs = query.order_by("started_at", direction="DESCENDING").limit(300).stream()
    result: list[dict[str, Any]] = []
    for d in docs:
        item = {"id": d.id, **(d.to_dict() or {})}
        if job_id and item.get("job_id") != job_id:
            continue
        if status and item.get("status") != status:
            continue
        result.append(item)
        if len(result) >= max(1, min(limit, 200)):
            break
    return result


def get_run(run_id: str) -> dict[str, Any] | None:
    ref = get_db().collection(_COLLECTION).document(run_id)
    doc = ref.get()
    if not doc.exists:
        return None
    events = [
        {"id": e.id, **(e.to_dict() or {})}
        for e in ref.collection("events")
        .order_by("timestamp")
        .limit(_MAX_EVENTS_PER_RUN)
        .stream()
    ]
    return {"id": doc.id, **(doc.to_dict() or {}), "events": events}
