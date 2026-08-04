"""
Admin endpoints — restricted to users with Firestore users/{email} is_admin=True.

POST  /api/admin/sync                    — trigger daily BQ sync in background
POST  /api/admin/record-cloud-build-sync — daily Cloud Build import from BQ billing (Scheduler + X-Sync-Secret)
GET   /api/admin/sync/status             — return result of last sync run
GET   /api/admin/maintenance             — current maintenance flag
PATCH /api/admin/maintenance             — set maintenance {enabled}
GET   /api/admin/sources                 — list all data sources with toggle state
PATCH /api/admin/sources/{source_id}     — enable or disable a data source
GET   /api/admin/schedulers              — list Cloud Scheduler jobs controlled by ESIP
GET   /api/admin/sync-runs               — list recent sync history
GET   /api/admin/users                   — list all users in allowlist
POST  /api/admin/users                   — add user to allowlist
PATCH /api/admin/users/{email}           — update active / is_admin
DELETE /api/admin/users/{email}          — remove user from allowlist
GET   /api/admin/cost/reconciliation     — billed vs app-metered vs untracked cost
"""

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import settings
from app.reference_data.fixtures import PLATFORM_ADMIN_EMAIL, SUPPORT_ADMIN_EMAIL
from app.dependencies import (
    CurrentUser,
    PREVIEW_ROLE_HEADER,
    attach_role_context,
    get_effective_role,
    is_effective_admin,
    reject_role_preview_write,
    require_admin as require_effective_admin,
    require_super_admin as require_effective_super_admin,
)
from app.services import google_auth, firestore
from app.services import reference_runtime
from app.services import announcements as announcements_service
from app.services import bigquery as bq_service
from app.services import (
    ai_dispatcher_admin,
    model_transition_capture_admin,
    impact_metrics,
    impact_export,
    scheduler_admin,
    service_desk_kpi,
    service_desk_kpi_delivery,
    service_desk_kpi_export,
    service_desk_kpi_snapshot,
    sync_runs,
    quality_telemetry,
    usage_insights,
    usage_insights_export,
)



logger = logging.getLogger(__name__)
router = APIRouter()

_SOURCES_ORDER = [
    "zendesk_operations_articles",
    "zendesk_support_articles",
    "reference_support_docs",
    "gdrive_operations_job_aids",
    "gdrive_support_repository",
    "tickets_zendesk",
    "tickets_bq",
]

# Optional bearer — does NOT raise if Authorization header is absent (auto_error=False).
# Used by trigger_sync so Cloud Scheduler calls (no JWT) still reach the endpoint.
_optional_bearer = HTTPBearer(auto_error=False)


def _get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    x_esip_preview_role: Optional[str] = Header(default=None, alias=PREVIEW_ROLE_HEADER),
) -> dict | None:
    """Return decoded Firebase token claims, or None if no/invalid token provided."""
    if credentials is None:
        return None
    try:
        claims = google_auth.verify_id_token(credentials.credentials)
    except Exception:
        return None
    return attach_role_context(claims, x_esip_preview_role)

# Scheduler authenticates with this secret header instead of Firebase JWT.
# Set SYNC_SECRET in Cloud Run env vars and in Cloud Scheduler custom header.
SYNC_SECRET = getattr(settings, "sync_secret", None)

# In-memory sync state (single instance — Cloud Run min-instances=1)
_sync_state: dict[str, Any] = {
    "running":    False,
    "progress":   None,   # human-readable progress string (updated during run)
    "last_run":   None,   # ISO timestamp
    "last_result": None,  # dict with counts
    "last_error":  None,  # error message if failed
}
_BQ_SYNC_STALE_SECONDS = 15 * 60
_DEFAULT_OCR_BACKLOG_ALERT_THRESHOLD = 500
_DEFAULT_OCR_BACKLOG_ALERT_RUNS = 2
_DRIVE_SYNC_STALE_SECONDS = 15 * 60


def _fmt_et(ts: int) -> str:
    """Format a Unix timestamp as a human-readable Eastern Time string."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    year = dt.year
    # DST start: 2nd Sunday of March at 07:00 UTC (= 2am EST)
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
    # DST end: 1st Sunday of November at 06:00 UTC (= 2am EDT)
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
    offset = timedelta(hours=-4 if dst_start <= dt < dst_end else -5)
    label  = "EDT" if dst_start <= dt < dst_end else "EST"
    return (dt + offset).strftime(f"%Y-%m-%d %H:%M {label}")


def _get_start_time() -> int:
    """
    Return the Unix timestamp to pass as start_time to the Zendesk Incremental API.

    Uses the cursor saved from the previous successful sync (Zendesk's own end_time).
    Falls back to 2 days ago if no cursor exists (first run or Firestore unavailable).
    """
    try:
        cursor = firestore.get_sync_cursor()
    except Exception as exc:
        logger.warning("Could not read sync_cursor from Firestore: %s — defaulting to 2 days ago", exc)
        cursor = None

    if cursor is not None:
        logger.info("Using saved cursor: %d (%s)", cursor,
                    datetime.fromtimestamp(cursor, tz=timezone.utc).isoformat())
        return cursor

    fallback = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    logger.info("No cursor found — using 2-day fallback: %d", fallback)
    return fallback


def _coerce_utc_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _fresh_running_state(state_data: dict, stale_seconds: int) -> tuple[bool, int | None]:
    heartbeat = _coerce_utc_dt(state_data.get("heartbeat_at"))
    if heartbeat is None:
        return bool(state_data.get("running")), None
    age = int((datetime.now(timezone.utc) - heartbeat).total_seconds())
    return bool(state_data.get("running")) and age < stale_seconds, age


def _require_admin(user: dict) -> None:
    require_effective_admin(user)


def _is_reference_user(user: dict) -> bool:
    """Keep provider-free adapters scoped to the authenticated reference session."""
    return settings.runtime_profile == "reference" and bool(user.get("reference_runtime"))


def _sync_config_int(key: str, default: int) -> int:
    try:
        cfg = firestore.get_db().collection("system").document("sync_config").get().to_dict() or {}
        return max(1, int(cfg.get(key, default)))
    except Exception:
        return default


def _recent_ocr_backlog_count(*, current_run_id: str | None, lookback_days: int = 14) -> int:
    """
    Count immediately previous completed BQ sync runs with deferred OCR backlog.

    The current run is not finished yet when this is called, so start from older
    runs and stop as soon as a completed run has no remaining OCR backlog.
    """
    count = 0
    try:
        runs = sync_runs.list_runs(job_id="esip-bq-sync", days=lookback_days, limit=20)
    except Exception as exc:
        logger.warning("Could not inspect recent OCR backlog runs: %s", exc)
        return 0
    for run in runs:
        if run.get("id") == current_run_id or run.get("status") == "running":
            continue
        summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
        try:
            remaining = int(summary.get("ocr_remaining_after_limit") or 0)
        except Exception:
            remaining = 0
        if remaining > 0:
            count += 1
            continue
        break
    return count


def _classify_ocr_backlog(remaining: int, *, current_run_id: str | None) -> dict[str, Any]:
    """
    Return operator-facing classification for deferred OCR backlog.

    A bounded OCR batch can leave backlog by design. That should be visible as
    partial success, not a failed sync. It becomes an alert only when it repeats
    or exceeds a separately configurable threshold.
    """
    if remaining <= 0:
        return {
            "deferred": False,
            "alert": False,
            "consecutive_runs": 0,
            "alert_threshold": _sync_config_int(
                "ticket_ocr_alert_threshold",
                _DEFAULT_OCR_BACKLOG_ALERT_THRESHOLD,
            ),
            "alert_after_runs": _sync_config_int(
                "ticket_ocr_alert_after_runs",
                _DEFAULT_OCR_BACKLOG_ALERT_RUNS,
            ),
        }
    alert_threshold = _sync_config_int(
        "ticket_ocr_alert_threshold",
        _DEFAULT_OCR_BACKLOG_ALERT_THRESHOLD,
    )
    alert_after_runs = _sync_config_int(
        "ticket_ocr_alert_after_runs",
        _DEFAULT_OCR_BACKLOG_ALERT_RUNS,
    )
    consecutive_runs = 1 + _recent_ocr_backlog_count(current_run_id=current_run_id)
    alert = remaining >= alert_threshold or consecutive_runs >= alert_after_runs
    return {
        "deferred": True,
        "alert": alert,
        "consecutive_runs": consecutive_runs,
        "alert_threshold": alert_threshold,
        "alert_after_runs": alert_after_runs,
    }


# ---------------------------------------------------------------------------
# Background sync runner
# ---------------------------------------------------------------------------

async def _run_sync_background(
    start_time: int,
    *,
    run_id: str | None = None,
    source_settings: dict[str, Any] | None = None,
) -> None:
    """
    Run full daily sync pipeline in an executor thread:
      1. Tickets + comments + attachments (incremental, from cursor)
      2. KB articles (full refresh, ~30s)
      3. OCR new attachment images (bounded by system/sync_config.ticket_ocr_limit)
      4. Embeddings for new tickets
    """
    from app.services.bq_sync import (  # noqa: PLC0415
        sync_tickets, sync_kb_articles, vectorize_new_tickets, ocr_new_attachments,
    )

    _sync_state["running"]    = True
    _sync_state["last_error"] = None
    _sync_state["progress"]   = "Starting..."
    started_at = datetime.now(timezone.utc)

    run_owner = f"{settings.google_cloud_project_id}:{int(datetime.now(timezone.utc).timestamp())}"
    last_heartbeat_at = 0.0
    final_status = "failed"
    final_error_count = 1
    final_error_message = "Sync did not complete"

    firestore.set_bq_sync_state_fields({
        "running": True,
        "last_error": None,
        "progress": "Starting...",
        "run_owner": run_owner,
        "heartbeat_at": datetime.now(timezone.utc),
    })
    scheduler_admin.clear_scheduler_dispatch_pending("esip-bq-sync")

    settings_snapshot = source_settings or sync_runs.source_settings_snapshot()

    def _source_sync_enabled(source_id: str) -> bool:
        cfg = settings_snapshot.get(source_id, {})
        return bool(cfg.get("sync_enabled", True))

    zendesk_ticket_sync_enabled = _source_sync_enabled("tickets_zendesk")
    ticket_bq_sync_enabled = _source_sync_enabled("tickets_bq")
    articles_enabled = _source_sync_enabled("zendesk_support_articles") or _source_sync_enabled("zendesk_operations_articles")

    def set_progress(msg: str) -> None:
        nonlocal last_heartbeat_at
        _sync_state["progress"] = msg
        sync_runs.add_event(run_id, stage="progress", message=msg)
        now_mono = time.monotonic()
        if now_mono - last_heartbeat_at >= 5.0:
            last_heartbeat_at = now_mono
            firestore.set_bq_sync_state_fields({
                "progress": msg,
                "heartbeat_at": datetime.now(timezone.utc),
                "run_owner": run_owner,
            })

    try:
        loop = asyncio.get_event_loop()
        since_str = _fmt_et(start_time)
        if zendesk_ticket_sync_enabled:
            logger.info("admin sync: starting ticket sync from %s", since_str)
            sync_runs.add_event(run_id, stage="tickets", message=f"Fetching tickets updated since {since_str}")
            set_progress(f"Fetching tickets updated since {since_str}...")

            ticket_result = await loop.run_in_executor(
                None, lambda: sync_tickets(start_time=start_time, progress_cb=set_progress)
            )
        else:
            ticket_result = {"tickets": 0, "comments": 0, "attachments": 0, "errors": [], "next_cursor": None}
            sync_runs.add_event(
                run_id,
                stage="tickets",
                message="Zendesk ticket ingest skipped because tickets_zendesk Sync is off",
                level="WARNING",
            )

        if articles_enabled:
            logger.info("admin sync: starting KB articles sync")
            sync_runs.add_event(run_id, stage="articles", message="Syncing Zendesk article metadata")
            set_progress("Syncing KB articles...")
            kb_result = await loop.run_in_executor(None, sync_kb_articles)
        else:
            kb_result = {"articles": 0, "errors": []}
            sync_runs.add_event(
                run_id,
                stage="articles",
                message="Zendesk article sync skipped because article source Sync toggles are off",
                level="WARNING",
            )

        if ticket_bq_sync_enabled:
            logger.info("admin sync: starting OCR for new images")
            sync_runs.add_event(run_id, stage="ocr", message="Running OCR on new attachment images")
            set_progress("Running OCR on new attachment images...")
            ocr_result = await loop.run_in_executor(
                None, lambda: ocr_new_attachments(progress_cb=set_progress)
            )

            ocr_remaining = int(ocr_result.get("remaining_after_limit") or 0)
            ocr_backlog = _classify_ocr_backlog(ocr_remaining, current_run_id=run_id)
            if ocr_remaining > 0:
                msg = (
                    f"Ticket OCR backlog deferred: {ocr_remaining} images remain after the "
                    "bounded nightly batch. Embeddings skipped for this run so tickets are not "
                    "indexed without their screenshot/PDF text."
                )
                ocr_result["deferred_backlog"] = True
                ocr_result["operator_alert"] = bool(ocr_backlog["alert"])
                ocr_result["backlog_consecutive_runs"] = ocr_backlog["consecutive_runs"]
                ocr_result["backlog_alert_threshold"] = ocr_backlog["alert_threshold"]
                ocr_result["backlog_alert_after_runs"] = ocr_backlog["alert_after_runs"]
                ocr_result["backlog_message"] = msg
                level = "ERROR" if ocr_backlog["alert"] else "WARNING"
                sync_runs.add_event(run_id, stage="embeddings", message=msg, level=level, details=ocr_backlog)
                set_progress("Partial success — embeddings deferred until ticket OCR backlog is drained.")
                emb_result = {
                    "embedded": 0,
                    "gcs_uploaded": 0,
                    "errors": [],
                    "skipped": True,
                    "skip_reason": "deferred_ocr_backlog",
                }
            else:
                ocr_backlog = _classify_ocr_backlog(0, current_run_id=run_id)
                logger.info("admin sync: starting embedding vectorization")
                sync_runs.add_event(run_id, stage="embeddings", message="Generating ticket embeddings and GCS docs")
                set_progress("Generating embeddings for new tickets...")
                emb_result = await loop.run_in_executor(
                    None, lambda: vectorize_new_tickets(progress_cb=set_progress)
                )
        else:
            ocr_result = {"ocr_processed": 0, "errors": [], "warnings": []}
            emb_result = {"embedded": 0, "gcs_uploaded": 0, "errors": []}
            ocr_backlog = _classify_ocr_backlog(0, current_run_id=run_id)
            sync_runs.add_event(
                run_id,
                stage="embeddings",
                message="Ticket OCR/embedding update skipped because tickets_bq Sync is off",
                level="WARNING",
            )

        all_errors = (
            ticket_result["errors"] + kb_result["errors"]
            + ocr_result["errors"]  + emb_result["errors"]
        )
        all_warnings = ocr_result.get("warnings", [])
        _sync_state["last_result"] = {
            "started_at":    started_at.isoformat(),
            "finished_at":   datetime.now(timezone.utc).isoformat(),
            "tickets":       ticket_result["tickets"],
            "comments":      ticket_result["comments"],
            "attachments":   ticket_result["attachments"],
            "ticket_audit_events": ticket_result.get("ticket_audit_events", 0),
            "ticket_audit_rows": ticket_result.get("ticket_audit_rows", 0),
            "ticket_audit_tickets": ticket_result.get("ticket_audit_tickets", 0),
            "skipped_tickets": ticket_result.get("skipped_tickets", 0),
            "kb_articles":   kb_result["articles"],
            "ocr_processed": ocr_result["ocr_processed"],
            "ocr_pending_total": ocr_result.get("pending_total"),
            "ocr_remaining_after_limit": ocr_result.get("remaining_after_limit"),
            "ocr_backlog_deferred": bool(ocr_result.get("deferred_backlog")),
            "ocr_backlog_operator_alert": bool(ocr_result.get("operator_alert")),
            "ocr_backlog_consecutive_runs": ocr_result.get("backlog_consecutive_runs"),
            "ocr_backlog_message": ocr_result.get("backlog_message"),
            "ocr_warnings":  all_warnings,
            "embedded":      emb_result["embedded"],
            "embeddings_deferred": bool(emb_result.get("skipped")),
            "deferred_reason": emb_result.get("skip_reason"),
            "errors":        all_errors,
        }
        firestore.set_bq_sync_state_fields({
            "last_result": _sync_state["last_result"],
        })
        set_progress(
            f"Done — {ticket_result['tickets']} tickets, "
            f"{kb_result['articles']} KB articles, "
            f"{ocr_result['ocr_processed']} images OCR'd, "
            f"{emb_result['embedded']} embeddings"
        )
        logger.info("admin sync: completed — %s", _sync_state["last_result"])

        # Save Zendesk cursor ONLY if: no errors AND Zendesk returned a valid cursor.
        # This guarantees the next sync starts exactly where this one left off.
        next_cursor = ticket_result.get("next_cursor")
        if next_cursor and not all_errors:
            try:
                firestore.set_sync_cursor(next_cursor)
                logger.info("admin sync: cursor saved → %d", next_cursor)
            except Exception as exc:
                logger.warning("Could not save sync_cursor to Firestore: %s", exc)
        elif not next_cursor:
            logger.warning("admin sync: Zendesk did not return end_time — cursor NOT updated")
            if zendesk_ticket_sync_enabled:
                all_errors.append("Zendesk did not return end_time; cursor was not updated")
                _sync_state["last_result"]["errors"] = all_errors
                firestore.set_bq_sync_state_fields({
                    "last_result": _sync_state["last_result"],
                })
        final_status = "success" if not all_errors else "failed"
        if not all_errors and ocr_backlog.get("deferred"):
            final_status = "partial_success"
        final_error_count = len(all_errors)
        final_error_message = "; ".join(all_errors[:5]) if all_errors else None
        sync_runs.finish_run(
            run_id,
            status=final_status,
            summary=_sync_state["last_result"],
            source_results={
                "tickets": ticket_result,
                "articles": kb_result,
                "ocr": ocr_result,
                "embeddings": emb_result,
            },
            cursor_before=start_time,
            cursor_after=next_cursor if next_cursor and not all_errors else None,
            error_count=final_error_count,
            error_message=final_error_message,
        )

    except Exception as exc:
        _sync_state["last_error"] = str(exc)
        _sync_state["progress"]   = f"Failed: {exc}"
        final_status = "failed"
        final_error_count = 1
        final_error_message = str(exc)
        sync_runs.add_event(run_id, stage="run", message=str(exc), level="ERROR")
        sync_runs.finish_run(
            run_id,
            status="failed",
            cursor_before=start_time,
            error_count=final_error_count,
            error_message=final_error_message,
        )
        firestore.set_bq_sync_state_fields({
            "last_error": str(exc),
            "progress": f"Failed: {exc}",
        })
        logger.error("admin sync: failed — %s", exc)
    finally:
        _sync_state["running"] = False
        _sync_state["last_run"] = datetime.now(timezone.utc).isoformat()
        firestore.set_bq_sync_state_fields({
            "running": False,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "heartbeat_at": datetime.now(timezone.utc),
            "progress": _sync_state["progress"],
            "last_error": _sync_state["last_error"],
            "last_result": _sync_state["last_result"],
            "run_owner": run_owner,
        })
    return {
        "status": final_status,
        "error_count": final_error_count,
        "error_message": final_error_message,
        "last_result": _sync_state["last_result"],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/sync")
async def trigger_sync(
    since_days: Optional[int] = None,
    x_sync_secret: Optional[str] = Header(default=None),
    x_run_trigger: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(_get_optional_user),
):
    """
    Trigger incremental BQ sync in background.

    Auth: X-Sync-Secret header (Cloud Scheduler / curl) OR Firebase JWT from admin user (UI).

    since_days: emergency override — forces sync from N days ago, ignoring the saved cursor.
                Omit in normal use; the cursor from the last successful sync is used automatically.
    """
    secret_ok = SYNC_SECRET and x_sync_secret == SYNC_SECRET
    admin_ok = user is not None and is_effective_admin(user)
    if not secret_ok and not admin_ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid sync secret")
    if admin_ok:
        reject_role_preview_write(user)
    trigger = (
        "run_now"
        if secret_ok and x_run_trigger == "admin_run_now"
        else ("scheduler" if secret_ok and not admin_ok else "manual")
    )
    started_by = (user or {}).get("email")

    if _sync_state["running"]:
        return {"status": "already_running", "message": "Sync is already in progress"}

    persisted = firestore.get_bq_sync_state()
    persisted_running, bq_heartbeat_age = _fresh_running_state(persisted, _BQ_SYNC_STALE_SECONDS)
    if persisted_running:
        run_id = sync_runs.start_run(
            job_id="esip-bq-sync",
            trigger=trigger,
            request={"skipped_before_start": True},
            started_by=started_by,
        )
        sync_runs.finish_run(
            run_id,
            status="skipped",
            summary={"reason": "Zendesk sync already running"},
            error_count=0,
        )
        return {
            "status": "already_running",
            "message": "Zendesk sync is already in progress (persistent lock)",
            "heartbeat_age_sec": bq_heartbeat_age,
        }
    if persisted.get("running"):
        firestore.set_bq_sync_state_fields({
            "running": False,
            "last_error": f"Recovered stale running lock (>{_BQ_SYNC_STALE_SECONDS}s without heartbeat)",
        })

    drive_state = firestore.get_drive_sync_state()
    drive_running, drive_heartbeat_age = _fresh_running_state(drive_state, _DRIVE_SYNC_STALE_SECONDS)
    if drive_running:
        run_id = sync_runs.start_run(
            job_id="esip-bq-sync",
            trigger=trigger,
            request={"skipped_before_start": True},
            started_by=started_by,
        )
        sync_runs.finish_run(
            run_id,
            status="skipped",
            summary={"reason": "Drive sync already running"},
            error_count=0,
        )
        return {
            "status": "blocked_by_active_drive_sync",
            "message": "Zendesk sync skipped because Drive sync is already running",
            "heartbeat_age_sec": drive_heartbeat_age,
        }
    if drive_state.get("running"):
        firestore.set_drive_sync_state_fields({
            "running": False,
            "last_error": f"Recovered stale running lock before Zendesk sync (>{_DRIVE_SYNC_STALE_SECONDS}s without heartbeat)",
        })

    if since_days is not None:
        start_time = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
        logger.info("trigger_sync: emergency override — syncing last %d days", since_days)
    else:
        start_time = _get_start_time()

    source_settings = sync_runs.source_settings_snapshot()
    run_id = sync_runs.start_run(
        job_id="esip-bq-sync",
        trigger=trigger,
        request={"since_days": since_days, "start_time": start_time},
        started_by=started_by,
        source_settings=source_settings,
    )
    since_str = _fmt_et(start_time)
    if secret_ok and not admin_ok:
        result = await _run_sync_background(start_time=start_time, run_id=run_id, source_settings=source_settings)
        if result.get("status") != "success":
            raise HTTPException(
                status_code=500,
                detail=result.get("error_message") or "Scheduler sync failed",
            )
        return {
            "status": "completed",
            "message": f"Sync completed from {since_str} + KB articles",
            "run_id": run_id,
        }

    asyncio.create_task(_run_sync_background(start_time=start_time, run_id=run_id, source_settings=source_settings))

    return {
        "status":  "started",
        "message": f"Sync started from {since_str} + KB articles",
        "run_id": run_id,
    }


@router.get("/sync/status")
async def sync_status(user: CurrentUser):
    """Return current sync state and result of the last completed run."""
    _require_admin(user)
    persisted = firestore.get_bq_sync_state()
    heartbeat = _coerce_utc_dt(persisted.get("heartbeat_at"))
    heartbeat_age_sec = None
    if heartbeat is not None:
        heartbeat_age_sec = int((datetime.now(timezone.utc) - heartbeat).total_seconds())
    return {
        "running": bool(persisted.get("running", _sync_state["running"])),
        "progress": persisted.get("progress") or _sync_state["progress"],
        "last_run": persisted.get("last_run") or _sync_state["last_run"],
        "last_result": persisted.get("last_result") or _sync_state["last_result"],
        "last_error": persisted.get("last_error") or _sync_state["last_error"],
        "heartbeat_at": persisted.get("heartbeat_at"),
        "heartbeat_age_sec": heartbeat_age_sec,
        "run_owner": persisted.get("run_owner"),
        "sync_cursor": persisted.get("sync_cursor"),
    }


# ---------------------------------------------------------------------------
# Google Drive sync
# ---------------------------------------------------------------------------

# Drive sync state (separate from BQ sync state)
_drive_sync_state: dict[str, Any] = {
    "running":     False,
    "progress":    None,
    "last_run":    None,
    "last_result": None,
    "last_error":  None,
    "last_request": None,
}


class DriveSyncRequest(BaseModel):
    source_ids: list[str] = Field(default_factory=list)
    include_path_prefixes: list[str] = Field(default_factory=list)
    include_mime_types: list[str] = Field(default_factory=list)
    max_files_per_source: int | None = Field(default=None, ge=1, le=200)
    stop_on_cost_alert: bool = True
    force_reindex: bool = False


class SourceSyncNowRequest(BaseModel):
    include_path_prefixes: list[str] = Field(default_factory=list)
    include_mime_types: list[str] = Field(default_factory=list)
    max_files_per_source: int | None = Field(default=10, ge=1, le=25)
    force_reindex: bool = False


class SchedulerUpdateRequest(BaseModel):
    enabled: bool | None = None
    schedule_time: str | None = None


class ChangeCandidateCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=2000)
    change_type: str = "other"
    impact: str = "medium"
    source: str = "manual"
    related_files: list[str] = Field(default_factory=list)
    audience_roles: list[str] = Field(default_factory=lambda: ["user", "admin"])
    surfaces: list[str] = Field(default_factory=lambda: ["web"])
    severity: str = "info"


class ChangeCandidatePublishRequest(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    body_markdown: str | None = Field(default=None, max_length=4000)
    audience_roles: list[str] = Field(default_factory=lambda: ["user", "admin"])
    surfaces: list[str] = Field(default_factory=lambda: ["web"])
    severity: str = "info"
    active_until: datetime | None = None


class ProductAnnouncementUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body_markdown: str = Field(min_length=1, max_length=4000)
    severity: str = "info"
    audience_roles: list[str] | None = None
    surfaces: list[str] | None = None


def _require_super_admin(user: dict) -> None:
    require_effective_super_admin(user)


async def _run_drive_sync_background(
    request: Optional[DriveSyncRequest] = None,
    *,
    run_id: str | None = None,
) -> None:
    """Run Google Drive sync pipeline in an executor thread."""
    from app.services.drive_sync import DriveSyncOptions, sync_drive  # noqa: PLC0415

    run_owner = f"{settings.google_cloud_project_id}:{int(datetime.now(timezone.utc).timestamp())}"
    request_payload = (request or DriveSyncRequest()).model_dump()
    sync_options = DriveSyncOptions(**request_payload)
    _drive_sync_state["running"]    = True
    _drive_sync_state["last_error"] = None
    _drive_sync_state["progress"]   = "Starting Drive sync..."
    _drive_sync_state["last_request"] = request_payload
    firestore.set_drive_sync_state_fields({
        "running": True,
        "last_error": None,
        "progress": "Starting Drive sync...",
        "last_request": request_payload,
        "run_owner": run_owner,
        "heartbeat_at": datetime.now(timezone.utc),
    })
    scheduler_admin.clear_scheduler_dispatch_pending("esip-drive-sync")

    last_heartbeat_at = 0.0

    def set_progress(msg: str) -> None:
        nonlocal last_heartbeat_at
        _drive_sync_state["progress"] = msg
        sync_runs.add_event(run_id, stage="progress", message=msg)
        now_mono = time.monotonic()
        if now_mono - last_heartbeat_at >= 5.0:
            last_heartbeat_at = now_mono
            firestore.set_drive_sync_state_fields({
                "progress": msg,
                "heartbeat_at": datetime.now(timezone.utc),
                "run_owner": run_owner,
            })

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: sync_drive(progress_cb=set_progress, options=sync_options)
        )
        _drive_sync_state["last_result"] = result
        set_progress(
            f"Done — {result['added']} added, {result['updated']} updated, "
            f"{result['deleted']} deleted, {result['errors']} errors"
        )
        firestore.set_drive_sync_state_fields({
            "last_result": result,
        })
        sync_runs.finish_run(
            run_id,
            status="success" if not result.get("errors") else "failed",
            summary={
                "added": result.get("added", 0),
                "updated": result.get("updated", 0),
                "deleted": result.get("deleted", 0),
                "skipped": result.get("skipped", 0),
                "errors": result.get("errors", 0),
                "total_drive_files": result.get("total_drive_files", 0),
            },
            source_results=result.get("sources", {}),
            error_count=int(result.get("errors") or 0),
            error_message="Drive sync completed with source errors" if result.get("errors") else None,
        )
        logger.info("drive sync: completed — %s", result)
    except Exception as exc:
        _drive_sync_state["last_error"] = str(exc)
        _drive_sync_state["progress"]   = f"Failed: {exc}"
        sync_runs.add_event(run_id, stage="run", message=str(exc), level="ERROR")
        sync_runs.finish_run(run_id, status="failed", error_count=1, error_message=str(exc))
        firestore.set_drive_sync_state_fields({
            "last_error": str(exc),
            "progress": f"Failed: {exc}",
            "last_request": request_payload,
        })
        logger.error("drive sync: failed — %s", exc)
    finally:
        _drive_sync_state["running"] = False
        _drive_sync_state["last_run"] = datetime.now(timezone.utc).isoformat()
        firestore.set_drive_sync_state_fields({
            "running": False,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "heartbeat_at": datetime.now(timezone.utc),
            "progress": _drive_sync_state["progress"],
            "last_error": _drive_sync_state["last_error"],
            "last_result": _drive_sync_state["last_result"],
            "last_request": _drive_sync_state["last_request"],
            "run_owner": run_owner,
        })


@router.post("/sync-drive")
async def trigger_drive_sync(
    body: Optional[DriveSyncRequest] = None,
    x_sync_secret: Optional[str] = Header(default=None),
    x_run_trigger: Optional[str] = Header(default=None),
    user: Optional[dict] = Depends(_get_optional_user),
):
    """
    Trigger Google Drive sync inline in the active HTTP request.

    Auth: X-Sync-Secret header (Cloud Scheduler) OR Firebase JWT from admin user.
    Supports controlled partial runs by source/path/mime/limit.
    """
    secret_ok = SYNC_SECRET and x_sync_secret == SYNC_SECRET
    admin_ok = user is not None and is_effective_admin(user)
    if not secret_ok and not admin_ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid sync secret")
    if admin_ok:
        reject_role_preview_write(user)
    trigger = (
        "run_now"
        if secret_ok and x_run_trigger == "admin_run_now"
        else ("scheduler" if secret_ok and not admin_ok else "manual")
    )
    started_by = (user or {}).get("email")

    if _drive_sync_state["running"]:
        return {"status": "already_running", "message": "Drive sync is already in progress"}

    bq_state = firestore.get_bq_sync_state()
    bq_running, bq_heartbeat_age = _fresh_running_state(bq_state, _BQ_SYNC_STALE_SECONDS)
    if bq_running:
        run_id = sync_runs.start_run(
            job_id="esip-drive-sync",
            trigger=trigger,
            request={"skipped_before_start": True},
            started_by=started_by,
        )
        sync_runs.finish_run(run_id, status="skipped", summary={"reason": "Zendesk sync already running"})
        return {
            "status": "blocked_by_active_zendesk_sync",
            "message": "Drive sync skipped because Zendesk sync is already running",
            "heartbeat_age_sec": bq_heartbeat_age,
        }
    if bq_state.get("running"):
        firestore.set_bq_sync_state_fields({
            "running": False,
            "last_error": f"Recovered stale running lock before Drive sync (>{_BQ_SYNC_STALE_SECONDS}s without heartbeat)",
        })

    persisted = firestore.get_drive_sync_state()
    persisted_hb = _coerce_utc_dt(persisted.get("heartbeat_at"))
    persisted_running = bool(persisted.get("running", False))
    if persisted_running and persisted_hb is not None:
        age = (datetime.now(timezone.utc) - persisted_hb).total_seconds()
        if age < _DRIVE_SYNC_STALE_SECONDS:
            run_id = sync_runs.start_run(
                job_id="esip-drive-sync",
                trigger=trigger,
                request={"skipped_before_start": True},
                started_by=started_by,
            )
            sync_runs.finish_run(run_id, status="skipped", summary={"reason": "Drive sync already running"})
            return {
                "status": "already_running",
                "message": "Drive sync is already in progress (persistent lock)",
                "heartbeat_age_sec": int(age),
            }

    if persisted_running:
        firestore.set_drive_sync_state_fields({
            "running": False,
            "last_error": f"Recovered stale running lock (>{_DRIVE_SYNC_STALE_SECONDS}s without heartbeat)",
        })

    request_payload = (body or DriveSyncRequest()).model_dump()
    run_id = sync_runs.start_run(
        job_id="esip-drive-sync",
        trigger=trigger,
        request=request_payload,
        started_by=started_by,
    )
    await _run_drive_sync_background(body, run_id=run_id)
    return {
        "status": "completed",
        "message": "Drive sync completed",
        "request": request_payload,
        "run_id": run_id,
    }


@router.get("/sync-drive/status")
async def drive_sync_status(user: CurrentUser):
    """Return current Drive sync state and result of the last completed run."""
    _require_admin(user)
    persisted = firestore.get_drive_sync_state()
    heartbeat = _coerce_utc_dt(persisted.get("heartbeat_at"))
    heartbeat_age_sec = None
    if heartbeat is not None:
        heartbeat_age_sec = int((datetime.now(timezone.utc) - heartbeat).total_seconds())
    return {
        "running":     bool(persisted.get("running", _drive_sync_state["running"])),
        "progress":    persisted.get("progress") or _drive_sync_state["progress"],
        "last_run":    persisted.get("last_run") or _drive_sync_state["last_run"],
        "last_result": persisted.get("last_result") or _drive_sync_state["last_result"],
        "last_error":  persisted.get("last_error") or _drive_sync_state["last_error"],
        "last_request": persisted.get("last_request") or _drive_sync_state["last_request"],
        "heartbeat_at": persisted.get("heartbeat_at"),
        "heartbeat_age_sec": heartbeat_age_sec,
        "run_owner": persisted.get("run_owner"),
    }


# ---------------------------------------------------------------------------
# Maintenance mode (Admin Panel — System tab)
# ---------------------------------------------------------------------------


@router.get("/maintenance")
def get_maintenance(user: CurrentUser):
    """Return current maintenance flag (admin only)."""
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return {"enabled": reference_runtime.maintenance_enabled()}
    return {"enabled": firestore.is_maintenance_mode()}


@router.patch("/maintenance")
def set_maintenance(body: dict, user: CurrentUser):
    """
    Set system/maintenance {enabled}. Non-admin users are blocked from the app when enabled.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Body must be {"enabled": true|false}',
        )
    if settings.runtime_profile == "reference":
        return {"enabled": reference_runtime.set_maintenance(enabled)}
    firestore.set_maintenance_enabled(enabled)
    logger.info("admin: maintenance enabled=%s", enabled)
    return {"enabled": enabled}


# ---------------------------------------------------------------------------
# Data source toggles
# ---------------------------------------------------------------------------

@router.get("/sources")
async def list_sources(user: CurrentUser):
    """
    Return all data sources with their current toggle state.
    """
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.sources()
    raw = firestore.get_all_data_sources_raw()
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        return {}

    ordered: dict[str, Any] = {}
    for source_id in _SOURCES_ORDER:
        if source_id in sources:
            ordered[source_id] = sources[source_id]
    for source_id, cfg in sources.items():
        if source_id not in ordered:
            ordered[source_id] = cfg
    return ordered


@router.patch("/sources/{source_id}")
async def toggle_source(
    source_id: str,
    body: dict,
    user: CurrentUser,
):
    """
    Enable or disable a data source.
    Body: {"enabled": true|false}
    Returns the updated source config.
    """
    _require_admin(user)
    reject_role_preview_write(user)

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Body must be {\"enabled\": true|false}",
        )
    if settings.runtime_profile == "reference":
        updated = reference_runtime.update_source(source_id, enabled=enabled)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
        return {"source_id": source_id, **updated}

    updated = firestore.update_data_source(source_id, enabled)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source '{source_id}' not found",
        )

    logger.info("admin: source %s set enabled=%s", source_id, enabled)
    return {"source_id": source_id, **updated}


@router.patch("/sources/{source_id}/sync")
async def toggle_source_sync(
    source_id: str,
    body: dict,
    user: CurrentUser,
):
    """
    Enable or disable synchronization for a data source.
    Body: {"sync_enabled": true|false}
    """
    _require_admin(user)
    reject_role_preview_write(user)

    sync_enabled = body.get("sync_enabled")
    if not isinstance(sync_enabled, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Body must be {"sync_enabled": true|false}',
        )
    if settings.runtime_profile == "reference":
        updated = reference_runtime.update_source(source_id, sync_enabled=sync_enabled)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
        return {"source_id": source_id, **updated}

    updated = firestore.update_data_source_sync(source_id, sync_enabled)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source '{source_id}' not found",
        )

    logger.info("admin: source %s set sync_enabled=%s", source_id, sync_enabled)
    return {"source_id": source_id, **updated}


@router.patch("/sources/{source_id}/paths")
async def update_source_paths(
    source_id: str,
    body: dict,
    user: CurrentUser,
):
    """
    Update path-level exclusion rules for a source.
    Body: {"exclude_path_prefixes": ["2. General/Archived", "..."]}
    """
    _require_admin(user)
    reject_role_preview_write(user)

    raw = body.get("exclude_path_prefixes")
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Body must include "exclude_path_prefixes": []',
        )
    if not isinstance(raw, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='"exclude_path_prefixes" must be a list of strings',
        )
    if not all(isinstance(x, str) for x in raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='"exclude_path_prefixes" must contain only strings',
        )
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        updated = reference_runtime.update_source(source_id, exclude_path_prefixes=raw)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
        return {"source_id": source_id, **updated}

    updated = firestore.update_data_source_paths(source_id, raw)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source '{source_id}' not found",
        )

    logger.info(
        "admin: source %s updated exclude_path_prefixes=%s",
        source_id,
        updated.get("exclude_path_prefixes", []),
    )
    return {"source_id": source_id, **updated}


@router.post("/sources/{source_id}/sync-now")
async def sync_source_now(
    source_id: str,
    user: CurrentUser,
    body: Optional[SourceSyncNowRequest] = None,
):
    """Run one Drive source without forcing the whole Drive scheduler corpus."""
    _require_admin(user)
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference":
        if reference_runtime.update_source(source_id, last_sync=datetime.now(timezone.utc).isoformat()) is None:
            raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
        return {"accepted": True, "source_id": source_id, "run_id": f"reference-{source_id}"}
    from app.services.drive_sync import DRIVE_SOURCES  # noqa: PLC0415

    drive_source_ids = {source["source_id"] for source in DRIVE_SOURCES}
    if source_id not in drive_source_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source '{source_id}' does not support source-level Drive sync",
        )

    request = body or SourceSyncNowRequest()
    return await trigger_drive_sync(
        body=DriveSyncRequest(
            source_ids=[source_id],
            include_path_prefixes=request.include_path_prefixes,
            include_mime_types=request.include_mime_types,
            max_files_per_source=request.max_files_per_source,
            stop_on_cost_alert=True,
            force_reindex=request.force_reindex,
        ),
        x_sync_secret=None,
        x_run_trigger=None,
        user=user,
    )


# ---------------------------------------------------------------------------
# Scheduler controls + sync history
# ---------------------------------------------------------------------------

@router.get("/schedulers")
async def list_scheduler_jobs(user: CurrentUser):
    """Return Cloud Scheduler jobs shown in Admin → Data Sources."""
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.schedulers()
    try:
        jobs = scheduler_admin.list_schedulers()
        for job in jobs:
            job_id = job.get("job_id")
            if job_id == "esip-drive-sync":
                state = firestore.get_drive_sync_state()
                active, heartbeat_age = _fresh_running_state(state, _DRIVE_SYNC_STALE_SECONDS)
                pending = scheduler_admin.get_scheduler_dispatch_pending(job_id)
                job["active_run_running"] = active
                if pending and not active:
                    job["active_run_running"] = True
                    job["active_run_progress"] = "Scheduler dispatch requested; waiting for Drive sync heartbeat"
                else:
                    job["active_run_progress"] = state.get("progress")
                job["active_run_heartbeat_age_sec"] = heartbeat_age
            elif job_id == "esip-bq-sync":
                state = firestore.get_bq_sync_state()
                active, heartbeat_age = _fresh_running_state(state, _BQ_SYNC_STALE_SECONDS)
                pending = scheduler_admin.get_scheduler_dispatch_pending(job_id)
                job["active_run_running"] = active
                if pending and not active:
                    job["active_run_running"] = True
                    job["active_run_progress"] = "Scheduler dispatch requested; waiting for Zendesk sync heartbeat"
                else:
                    job["active_run_progress"] = state.get("progress")
                job["active_run_heartbeat_age_sec"] = heartbeat_age
            else:
                job["active_run_running"] = False
                job["active_run_progress"] = None
                job["active_run_heartbeat_age_sec"] = None
        return jobs
    except Exception as exc:
        logger.exception("admin: scheduler list failed")
        raise HTTPException(status_code=502, detail=f"Could not read Cloud Scheduler jobs: {exc}") from exc


@router.patch("/schedulers/{job_id}")
async def update_scheduler_job(
    job_id: str,
    body: SchedulerUpdateRequest,
    user: CurrentUser,
):
    """Pause/resume a scheduler job and/or update its daily Eastern start time."""
    _require_admin(user)
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference":
        updated = reference_runtime.update_scheduler(job_id, enabled=body.enabled, schedule_time=body.schedule_time)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Scheduler job '{job_id}' not found")
        return updated
    email = user.get("email") or ""
    if job_id not in scheduler_admin.JOBS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scheduler job '{job_id}' not found")

    before = scheduler_admin.get_scheduler(job_id)
    current = before
    try:
        if body.schedule_time is not None:
            current = scheduler_admin.update_scheduler_time(job_id, body.schedule_time)
        if body.enabled is not None:
            current = scheduler_admin.resume_scheduler(job_id) if body.enabled else scheduler_admin.pause_scheduler(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("admin: scheduler update failed for %s", job_id)
        raise HTTPException(status_code=502, detail=f"Could not update scheduler job: {exc}") from exc

    firestore.get_db().collection("system").document("scheduler_audit").collection("events").add({
        "job_id": job_id,
        "updated_by": email,
        "updated_at": datetime.now(timezone.utc),
        "before": before,
        "after": current,
    })
    return current


@router.post("/schedulers/{job_id}/run-now")
async def run_scheduler_job_now(job_id: str, user: CurrentUser):
    """Trigger a Cloud Scheduler job immediately."""
    _require_admin(user)
    reject_role_preview_write(user)
    if settings.runtime_profile == "reference":
        if reference_runtime.update_scheduler(job_id) is None:
            raise HTTPException(status_code=404, detail=f"Scheduler job '{job_id}' not found")
        return {"accepted": True, "job_id": job_id, "run_id": f"reference-{job_id}"}
    if job_id not in scheduler_admin.JOBS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Scheduler job '{job_id}' not found")
    try:
        result = scheduler_admin.run_scheduler_now(job_id)
    except Exception as exc:
        logger.exception("admin: scheduler run-now failed for %s", job_id)
        raise HTTPException(status_code=502, detail=f"Could not run scheduler job: {exc}") from exc
    firestore.get_db().collection("system").document("scheduler_audit").collection("events").add({
        "job_id": job_id,
        "action": "run_now",
        "updated_by": user.get("email") or "",
        "updated_at": datetime.now(timezone.utc),
        "result": result,
    })
    return result


@router.get("/sync-runs")
async def list_sync_runs(
    user: CurrentUser,
    job_id: str | None = None,
    status_filter: str | None = None,
    days: int = 7,
    limit: int = 50,
):
    """Return recent sync run summaries."""
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.sync_runs_list()[:limit]
    return sync_runs.list_runs(job_id=job_id, status=status_filter, days=days, limit=limit)


@router.get("/sync-runs/{run_id}")
async def get_sync_run(run_id: str, user: CurrentUser):
    """Return a sync run with bounded event log."""
    _require_admin(user)
    if settings.runtime_profile == "reference":
        run = reference_runtime.sync_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Sync run '{run_id}' not found")
        return run
    run = sync_runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Sync run '{run_id}' not found")
    return run


# ---------------------------------------------------------------------------
# Product change announcements (Super Admin)
# ---------------------------------------------------------------------------

@router.get("/change-candidates")
def list_change_candidates(
    user: CurrentUser,
    status_filter: str | None = None,
    limit: int = 100,
):
    """Return Super Admin review queue for UI/functionality change announcements."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.change_candidates(status_filter, limit)
    return announcements_service.list_change_candidates(status=status_filter, limit=limit)


@router.post("/change-candidates")
def create_change_candidate(body: ChangeCandidateCreateRequest, user: CurrentUser):
    """Create a draft change candidate. Drafts are not shown to users."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        return reference_runtime.create_change_candidate(
            body.model_dump(mode="json"), user.get("email") or ""
        )
    try:
        return announcements_service.create_change_candidate(
            title=body.title,
            summary=body.summary,
            change_type=body.change_type,
            impact=body.impact,
            source=body.source,
            related_files=body.related_files,
            audience_roles=body.audience_roles,
            surfaces=body.surfaces,
            severity=body.severity,
            created_by=user.get("email") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/change-candidates/{candidate_id}/publish")
def publish_change_candidate(candidate_id: str, body: ChangeCandidatePublishRequest, user: CurrentUser):
    """Publish a draft candidate as a one-time user-visible announcement."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        result = reference_runtime.publish_change_candidate(
            candidate_id, body.model_dump(mode="json"), user.get("email") or ""
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change candidate not found")
        return result
    try:
        result = announcements_service.publish_change_candidate(
            candidate_id,
            title=body.title,
            body_markdown=body.body_markdown,
            audience_roles=body.audience_roles,
            surfaces=body.surfaces,
            severity=body.severity,
            active_until=body.active_until,
            published_by=user.get("email") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change candidate not found")
    return result


@router.post("/change-candidates/{candidate_id}/ignore")
def ignore_change_candidate(candidate_id: str, user: CurrentUser):
    """Mark a draft candidate ignored. It is retained for audit but not shown to users."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        result = reference_runtime.ignore_change_candidate(candidate_id, user.get("email") or "")
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change candidate not found")
        return result
    result = announcements_service.ignore_change_candidate(candidate_id, ignored_by=user.get("email") or "")
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Change candidate not found")
    return result


@router.get("/announcements")
def list_product_announcements(user: CurrentUser, limit: int = 100):
    """Return announcements for Super Admin review, including soft-unpublished items."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.product_announcements(limit)
    return announcements_service.list_published_announcements(limit=limit, include_unpublished=True)


@router.get("/announcements/{announcement_id}/stats")
def get_product_announcement_stats(announcement_id: str, user: CurrentUser):
    """Return read/dismiss tracking for a published announcement."""
    _require_super_admin(user)
    if _is_reference_user(user):
        result = reference_runtime.announcement_stats(announcement_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return result
    result = announcements_service.get_announcement_stats(announcement_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result


@router.patch("/announcements/{announcement_id}")
def update_product_announcement(announcement_id: str, body: ProductAnnouncementUpdateRequest, user: CurrentUser):
    """Edit an existing announcement in place while preserving read/stat records."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        result = reference_runtime.update_product_announcement(
            announcement_id, body.model_dump(mode="json"), user.get("email") or ""
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return result
    try:
        result = announcements_service.update_announcement(
            announcement_id,
            title=body.title,
            body_markdown=body.body_markdown,
            severity=body.severity,
            audience_roles=body.audience_roles,
            surfaces=body.surfaces,
            edited_by=user.get("email") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result


@router.post("/announcements/{announcement_id}/unpublish")
def unpublish_product_announcement(announcement_id: str, user: CurrentUser):
    """Soft-unpublish an announcement while retaining audit/statistics."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        result = reference_runtime.set_announcement_published(
            announcement_id, False, user.get("email") or ""
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return result
    result = announcements_service.unpublish_announcement(
        announcement_id,
        unpublished_by=user.get("email") or "",
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result


@router.post("/announcements/{announcement_id}/republish")
def republish_product_announcement(announcement_id: str, user: CurrentUser):
    """Republish a soft-unpublished announcement while retaining audit/statistics."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        result = reference_runtime.set_announcement_published(
            announcement_id, True, user.get("email") or ""
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return result
    result = announcements_service.republish_announcement(
        announcement_id,
        republished_by=user.get("email") or "",
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result


@router.delete("/announcements/{announcement_id}")
def delete_product_announcement(announcement_id: str, user: CurrentUser):
    """Permanently delete an announcement and its read records from the review list."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        if not reference_runtime.delete_product_announcement(announcement_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
        return {"deleted": announcement_id}
    result = announcements_service.delete_announcement(
        announcement_id,
        deleted_by=user.get("email") or "",
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")
    return result


# ---------------------------------------------------------------------------
# Cost monitoring — snapshot, budget, alerts, notifications
# ---------------------------------------------------------------------------

def _alias_budget_cad(budget: dict) -> dict:
    """Add non-breaking CAD aliases to legacy *_usd budget keys."""
    out = dict(budget)
    if "daily_limit_usd" in out and "daily_limit_cad" not in out:
        out["daily_limit_cad"] = out["daily_limit_usd"]
    if "monthly_limit_usd" in out and "monthly_limit_cad" not in out:
        out["monthly_limit_cad"] = out["monthly_limit_usd"]
    return out

@router.get("/cost/snapshot")
def get_cost_snapshot(user: CurrentUser):
    """
    Return today's estimated cost (Firestore counters) + active alert status.
    Also returns last 7 days of daily totals for the history chart.
    Amounts are in CAD (billing currency); legacy *_usd keys remain for compatibility.
    """
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.cost_snapshot()
    from app.services.cost_tracker import get_daily_summary, get_budget  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
    today = datetime.now(BUSINESS_TIMEZONE)

    # Today's summary
    today_key = today.strftime("%Y-%m-%d")
    today_data = get_daily_summary(today_key)

    # Last 7 days
    history = []
    for i in range(7):
        day = today - timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        day_data = get_daily_summary(key)
        history.append({
            "date": key,
            "cost_usd": round(day_data.get("total_cost_usd", 0.0), 6),
            "cost_cad": round(day_data.get("total_cost_cad", day_data.get("total_cost_usd", 0.0)), 6),
            "calls": day_data.get("total_calls", 0),
        })

    # Active alert
    alert = None
    try:
        doc = firestore.get_db().document("system/cost_alerts").get()
        if doc.exists:
            d = doc.to_dict() or {}
            if d.get("has_active_alert"):
                alert = {
                    "type": d.get("last_alert_type"),
                    "message": d.get("last_alert_message"),
                    "at": d.get("last_alert_at"),
                    "hard_cap_active": d.get("hard_cap_active", False),
                }
    except Exception as exc:
        logger.warning("cost/snapshot: could not read cost_alerts: %s", exc)

    budget = _alias_budget_cad(get_budget())
    daily_limit = budget.get("daily_limit_usd", 10.0)
    today_cost = today_data.get("total_cost_usd", 0.0)

    return {
        "currency": "CAD",
        "today": {
            "date": today_key,
            "cost_usd": round(today_cost, 6),
            "cost_cad": round(today_data.get("total_cost_cad", today_cost), 6),
            "calls": today_data.get("total_calls", 0),
            "by_type": today_data.get("by_type", {}),
            "pct_of_daily_limit": round(today_cost / daily_limit * 100, 1) if daily_limit else 0,
        },
        "history": history,
        "budget": budget,
        "alert": alert,
        "note": (
            "Estimated: in-app Gemini/embeddings/search + flat lines (idle, Vertex query, "
            "Cloud Build via manual POST /cost/cloud-build or automatic POST /cost/sync-cloud-build-billing "
            "which reads GCP Billing Export in BQ). BQ billing (Billed) is full GCP — 1–24h lag."
        ),
    }


@router.get("/cost/range")
def get_cost_range(user: CurrentUser, from_dt: str, to_dt: str):
    """
    Return aggregated cost counters for an arbitrary time range.

    Query params (Eastern time, ISO format YYYY-MM-DDTHH:MM):
      from_dt — range start (inclusive)
      to_dt   — range end   (inclusive)

    Granularity: hourly if range ≤ 48h, daily otherwise.
    Max range: 90 days.
    """
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.cost_range(from_dt, to_dt)
    from app.services.cost_tracker import get_range_summary  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
    try:
        from_parsed = datetime.fromisoformat(from_dt).replace(tzinfo=BUSINESS_TIMEZONE)
        to_parsed   = datetime.fromisoformat(to_dt).replace(tzinfo=BUSINESS_TIMEZONE)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid datetime format. Use ISO: YYYY-MM-DDTHH:MM",
        )

    if to_parsed <= from_parsed:
        raise HTTPException(status_code=422, detail="to_dt must be after from_dt")

    max_days = 90
    if (to_parsed - from_parsed).total_seconds() > max_days * 24 * 3600:
        raise HTTPException(status_code=422, detail=f"Range cannot exceed {max_days} days")

    return get_range_summary(from_parsed, to_parsed)


@router.get("/cost/budget")
def get_cost_budget(user: CurrentUser):
    """Return current budget configuration from system/cost_budget."""
    _require_admin(user)
    from app.services.cost_tracker import get_budget  # noqa: PLC0415
    return _alias_budget_cad(get_budget())


@router.patch("/cost/budget")
def update_cost_budget(body: dict, user: CurrentUser):
    """
    Update budget limits and cap thresholds.
    Accepted fields: daily_limit_usd, monthly_limit_usd, soft_cap_pct,
                     hard_cap_pct, divergence_threshold.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    import app.services.cost_tracker as ct  # noqa: PLC0415

    allowed = {"daily_limit_usd", "monthly_limit_usd", "soft_cap_pct",
               "hard_cap_pct", "divergence_threshold"}
    update = {k: v for k, v in body.items() if k in allowed}
    if not update:
        raise HTTPException(status_code=422, detail=f"No valid fields. Allowed: {allowed}")

    # Validate numeric ranges
    if "soft_cap_pct" in update and not (0 < update["soft_cap_pct"] < 100):
        raise HTTPException(status_code=422, detail="soft_cap_pct must be between 1 and 99")
    if "hard_cap_pct" in update and not (0 < update["hard_cap_pct"] <= 100):
        raise HTTPException(status_code=422, detail="hard_cap_pct must be between 1 and 100")
    if "divergence_threshold" in update and update["divergence_threshold"] < 1.1:
        raise HTTPException(status_code=422, detail="divergence_threshold must be >= 1.1")

    firestore.get_db().document("system/cost_budget").set(update, merge=True)

    # Invalidate in-memory budget cache so new limits take effect immediately
    ct._budget_cache = None
    ct._budget_cache_ts = 0.0

    logger.info("admin: cost_budget updated: %s", update)
    return _alias_budget_cad(ct.get_budget())


@router.post("/cost/alerts/clear")
def clear_cost_alert(user: CurrentUser):
    """
    Clear active cost alert (e.g. after admin investigates and decides to resume).
    Does NOT automatically resume schedulers — admin must do that manually.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    firestore.get_db().document("system/cost_alerts").set({
        "has_active_alert": False,
        "hard_cap_active":  False,
        "platform_blocked": False,
        "cleared_at":       datetime.now(timezone.utc).isoformat(),
        "cleared_by":       user.get("email", "unknown"),
    }, merge=True)
    logger.info("admin: cost alert cleared by %s", user.get("email"))
    return {"cleared": True}


class CloudBuildCostBody(BaseModel):
    """Log a Cloud Build charge (e.g. after `gcloud run deploy`) into estimated counters."""

    cost_usd: float = Field(
        default=0.04,
        ge=0.0,
        le=500.0,
        description="Amount from GCP console / billing (project currency, typically CAD ~0.04/build)",
    )
    units: int = Field(default=1, ge=1, le=50, description="Number of builds to record at this price each")


@router.post("/cost/cloud-build")
def record_cloud_build_cost(body: CloudBuildCostBody, user: CurrentUser):
    """
    Record Cloud Build / deploy cost into Firestore cost_counters (today + hourly bucket).

    Gemini and other in-app calls are metered automatically; Cloud Build is billed separately
    in GCP and does not hit the backend — it never appeared in By call type until this endpoint.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    from app.services.cost_tracker import CallType, record_flat_cost  # noqa: PLC0415

    total_cost = round(body.cost_usd * body.units, 6)
    recorded = record_flat_cost(
        CallType.CLOUD_BUILD,
        cost_usd=total_cost,
        units=body.units,
    )
    logger.info(
        "admin: recorded cloud_build unit=$%.4f × %d by %s (total $%.4f)",
        body.cost_usd,
        body.units,
        user.get("email"),
        recorded,
    )
    return {"recorded": True, "currency": "CAD", "cost_usd": recorded, "cost_cad": recorded, "units": body.units}


@router.post("/cost/sync-cloud-build-billing")
def sync_cloud_build_billing_admin(user: CurrentUser):
    """
    Pull Cloud Build costs from GCP Billing Export (BQ) into Firestore estimated counters.
    Idempotent per day (ledger in ``system/cost_cloud_build_bq_ledger``).
    """
    _require_admin(user)
    reject_role_preview_write(user)
    try:
        from app.services.cloud_build_billing_sync import sync_cloud_build_from_billing  # noqa: PLC0415

        return sync_cloud_build_from_billing()
    except Exception as exc:
        logger.exception("admin: sync-cloud-build-billing failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/record-cloud-build-sync")
def record_cloud_build_sync(x_sync_secret: Optional[str] = Header(default=None)):
    """
    Same as POST /cost/sync-cloud-build-billing — for Cloud Scheduler + ``X-Sync-Secret``.
    Recommended: daily after billing export lag (e.g. 10:00 Eastern).
    """
    secret_ok = SYNC_SECRET and x_sync_secret == SYNC_SECRET
    if not secret_ok:
        raise HTTPException(status_code=403, detail="Invalid or missing sync secret")
    try:
        from app.services.cloud_build_billing_sync import sync_cloud_build_from_billing  # noqa: PLC0415

        return sync_cloud_build_from_billing()
    except Exception as exc:
        logger.exception("admin: record-cloud-build-sync failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Notification recipients management
# ---------------------------------------------------------------------------

_SAFE_TEST_NOTIFICATION_RECIPIENTS = {
    SUPPORT_ADMIN_EMAIL,
    PLATFORM_ADMIN_EMAIL,
}


class NotificationTestBody(BaseModel):
    recipient: str | None = Field(
        default=None,
        description="Optional safe test recipient. Full alert recipients are never used for tests.",
    )


class KpiDeliverySlotBody(BaseModel):
    id: str | None = None
    time: str
    enabled: bool = True
    label: str | None = None


class KpiDeliveryConfigBody(BaseModel):
    enabled: bool | None = None
    timezone: str | None = None
    recipients: list[str] | None = None
    cc_recipients: list[str] | None = None
    slots: list[KpiDeliverySlotBody] | None = None


class KpiDeliveryRecipientBody(BaseModel):
    email: str


class KpiDeliveryTestBody(BaseModel):
    recipient: str
    report_date: str | None = None


def _resolve_notification_test_recipient(body: NotificationTestBody | None, user: CurrentUser) -> str:
    requested = str((body.recipient if body else "") or "").strip().lower()
    if requested:
        if requested not in _SAFE_TEST_NOTIFICATION_RECIPIENTS:
            raise HTTPException(
                status_code=422,
                detail=f"Test notifications can only be sent to {SUPPORT_ADMIN_EMAIL} or {PLATFORM_ADMIN_EMAIL}",
            )
        return requested

    caller_email = str(user.get("email") or "").strip().lower()
    if caller_email in _SAFE_TEST_NOTIFICATION_RECIPIENTS:
        return caller_email
    return PLATFORM_ADMIN_EMAIL


@router.get("/notifications")
def get_notifications(user: CurrentUser):
    """Return current notification config (recipients list)."""
    _require_admin(user)
    if _is_reference_user(user):
        return {"recipients": reference_runtime.notification_recipients()}
    try:
        doc = firestore.get_db().document("system/notification_config").get()
        data = doc.to_dict() if doc.exists else {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "recipients": data.get("recipients", [
            SUPPORT_ADMIN_EMAIL,
            PLATFORM_ADMIN_EMAIL,
        ])
    }


@router.post("/notifications/recipients")
def add_recipient(body: dict, user: CurrentUser):
    """Add an email to the alert recipients list."""
    _require_admin(user)
    reject_role_preview_write(user)
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Invalid email")

    if _is_reference_user(user):
        reference_runtime.add_notification_recipient(email)
        return {"added": email}

    from google.cloud.firestore import ArrayUnion  # noqa: PLC0415
    firestore.get_db().document("system/notification_config").set(
        {"recipients": ArrayUnion([email])}, merge=True
    )
    # Invalidate email_service cache
    import app.services.email_service as es  # noqa: PLC0415
    es._recipients_cache = None

    logger.info("admin: notification recipient added: %s", email)
    return {"added": email}


@router.delete("/notifications/recipients/{email}")
def remove_recipient(email: str, user: CurrentUser):
    """Remove an email from the alert recipients list."""
    _require_admin(user)
    reject_role_preview_write(user)
    email = email.strip().lower()
    if _is_reference_user(user):
        reference_runtime.remove_notification_recipient(email)
        return {"removed": email}
    from google.cloud.firestore import ArrayRemove  # noqa: PLC0415
    firestore.get_db().document("system/notification_config").set(
        {"recipients": ArrayRemove([email])}, merge=True
    )
    import app.services.email_service as es  # noqa: PLC0415
    es._recipients_cache = None

    logger.info("admin: notification recipient removed: %s", email)
    return {"removed": email}


@router.get("/cost/billing")
def get_cost_billing(user: CurrentUser, from_dt: str = "", to_dt: str = ""):
    """
    Return daily cost totals from BQ Billing export (actual billed cost, 1–24h lag).
    Amounts are in CAD (billing account currency); legacy *_usd keys remain for compatibility.

    Query params (Eastern date, YYYY-MM-DD or YYYY-MM-DDTHH:MM):
      from_dt — range start (default: 30 days ago)
      to_dt   — range end   (default: today)

    Returns daily buckets with cost (before credits).
    Table: settings.gcp_billing_export_table
    """
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.cost_billing(from_dt, to_dt)
    from google.cloud import bigquery as bq  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    from app.services.bigquery import _get_client  # noqa: PLC0415

    BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
    now_business_time = datetime.now(BUSINESS_TIMEZONE)

    # Parse date range
    if from_dt:
        try:
            from_date = datetime.fromisoformat(from_dt.split("T")[0]).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid from_dt format. Use YYYY-MM-DD")
    else:
        from_date = (now_business_time - timedelta(days=30)).date()

    if to_dt:
        try:
            to_date = datetime.fromisoformat(to_dt.split("T")[0]).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid to_dt format. Use YYYY-MM-DD")
    else:
        to_date = now_business_time.date()

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to_dt must be >= from_dt")

    max_days = 365
    if (to_date - from_date).days > max_days:
        raise HTTPException(status_code=422, detail=f"Range cannot exceed {max_days} days")

    query = f"""
        SELECT
            FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'America/New_York')) AS day,
            SUM(cost) AS cost_cad
        FROM `{settings.gcp_billing_export_table}`
        WHERE
            project.id = 'esip-reference-project'
            AND DATE(usage_start_time, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day
        ORDER BY day
    """

    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("from_date", "DATE", from_date.isoformat()),
            bq.ScalarQueryParameter("to_date",   "DATE", to_date.isoformat()),
        ]
    )

    try:
        client = _get_client()
        rows = list(client.query(query, job_config=job_config).result())
    except Exception as exc:
        logger.error("cost/billing: BQ query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"BQ billing query failed: {exc}")

    # Build daily buckets map
    cost_by_day: dict[str, float] = {row.day: float(row.cost_cad) for row in rows}

    # Fill all days in range (include zero-cost days)
    buckets = []
    current = from_date
    import datetime as dt_mod  # noqa: PLC0415
    while current <= to_date:
        key = current.isoformat()
        buckets.append({
            "key":      key,
            "label":    key[5:],   # MM-DD
            "cost_usd": round(cost_by_day.get(key, 0.0), 6),
            "cost_cad": round(cost_by_day.get(key, 0.0), 6),
        })
        current += dt_mod.timedelta(days=1)

    total_cost = sum(b["cost_usd"] for b in buckets)

    # Latest data point timestamp (last day with nonzero cost)
    nonzero = [b["key"] for b in buckets if b["cost_usd"] > 0]
    latest_day = nonzero[-1] if nonzero else None

    return {
        "from_dt":      from_date.isoformat(),
        "to_dt":        to_date.isoformat(),
        "currency":      "CAD",
        "total_cost_usd": round(total_cost, 6),
        "total_cost_cad": round(total_cost, 6),
        "buckets":      buckets,
        "latest_day":   latest_day,
        "lag_note":     f"Actual billed cost in CAD (before credits). Latest data: {latest_day or 'none'}. BQ billing export has 1–24h lag.",
    }


@router.get("/cost/reconciliation")
def get_cost_reconciliation(user: CurrentUser, from_dt: str = "", to_dt: str = ""):
    """
    Return full cost coverage for a date range:
      - GCP billed cost from Billing Export (source of truth, delayed)
      - app-metered ACTUAL rows from esip_reference.cost_events
      - untracked billed cost = billed - app_metered, by day/service/SKU

    Billing export and esip_reference can live in different BigQuery locations,
    so this intentionally runs separate queries and joins in Python.
    """
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return reference_runtime.cost_reconciliation(from_dt, to_dt)
    from google.cloud import bigquery as bq  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    from app.services.bigquery import _get_client  # noqa: PLC0415

    BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
    now_business_time = datetime.now(BUSINESS_TIMEZONE)

    def parse_date(value: str, default):
        if not value:
            return default
        try:
            return datetime.fromisoformat(value.split("T")[0]).date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format. Use YYYY-MM-DD")

    from_date = parse_date(from_dt, (now_business_time - timedelta(days=30)).date())
    to_date = parse_date(to_dt, now_business_time.date())
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to_dt must be >= from_dt")
    if (to_date - from_date).days > 365:
        raise HTTPException(status_code=422, detail="Range cannot exceed 365 days")

    params = [
        bq.ScalarQueryParameter("from_date", "DATE", from_date.isoformat()),
        bq.ScalarQueryParameter("to_date", "DATE", to_date.isoformat()),
    ]
    client = _get_client()

    billing_service_sql = f"""
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'America/New_York')) AS day,
          service.description AS service,
          SUM(cost) AS billed_cad,
          SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits_cad,
          SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_billed_cad,
          COUNT(*) AS billing_rows
        FROM `{settings.gcp_billing_export_table}`
        WHERE
          project.id = 'esip-reference-project'
          AND DATE(usage_start_time, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day, service
        ORDER BY day DESC, billed_cad DESC
    """
    billing_sku_sql = f"""
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'America/New_York')) AS day,
          service.description AS service,
          sku.id AS sku_id,
          sku.description AS sku_description,
          SUM(cost) AS billed_cad,
          SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits_cad,
          SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_billed_cad,
          COUNT(*) AS billing_rows
        FROM `{settings.gcp_billing_export_table}`
        WHERE
          project.id = 'esip-reference-project'
          AND DATE(usage_start_time, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day, service, sku_id, sku_description
        ORDER BY day DESC, billed_cad DESC
    """
    app_service_sql = """
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(timestamp, 'America/New_York')) AS day,
          CASE
            WHEN call_type = 'vertex_search' THEN 'Vertex AI Search'
            WHEN call_type = 'cloud_run_idle' THEN 'Cloud Run'
            WHEN call_type = 'cloud_build' THEN 'Cloud Build'
            WHEN call_type IN (
              'ocr_image', 'drive_ocr',
              'embed_ticket', 'embed_query', 'embed_drive',
              'chat_phase0', 'chat_phase1', 'chat_phase2',
              'analytics_sql', 'analytics_decision', 'analytics_answer',
              'summary', 'docs_gap',
              'universal_p1', 'universal_p2', 'universal_translate', 'universal_review',
              'main_chat_p1', 'main_chat_p2'
            ) THEN 'Vertex AI'
            ELSE 'Other / Unmapped'
          END AS service,
          SUM(CASE WHEN event_type = 'ACTUAL' THEN COALESCE(actual_cost_cad, act_cost_usd, 0) ELSE 0 END) AS app_actual_cad,
          SUM(CASE WHEN event_type = 'ESTIMATE' THEN COALESCE(expected_cost_cad, est_cost_usd, 0) ELSE 0 END) AS app_estimated_cad,
          COUNTIF(event_type = 'ACTUAL') AS app_actual_events,
          STRING_AGG(DISTINCT IF(event_type = 'ACTUAL', call_type, NULL), ', ') AS call_types
        FROM `esip-reference-project.esip_reference.cost_events`
        WHERE DATE(timestamp, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day, service
        ORDER BY day DESC, app_actual_cad DESC
    """

    try:
        job_config = bq.QueryJobConfig(query_parameters=params)
        billing_service_rows = list(client.query(billing_service_sql, job_config=job_config).result())
        billing_sku_rows = list(client.query(billing_sku_sql, job_config=job_config).result())
        app_service_rows = list(client.query(app_service_sql, job_config=job_config).result())
    except Exception as exc:
        logger.error("cost/reconciliation: BQ query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"BQ reconciliation query failed: {exc}")

    billing_by_service: dict[tuple[str, str], dict[str, Any]] = {}
    for row in billing_service_rows:
        billing_by_service[(str(row.day), str(row.service))] = {
            "billed_cad": float(row.billed_cad or 0.0),
            "credits_cad": float(row.credits_cad or 0.0),
            "net_billed_cad": float(row.net_billed_cad or 0.0),
            "billing_rows": int(row.billing_rows or 0),
        }

    app_by_service: dict[tuple[str, str], dict[str, Any]] = {}
    for row in app_service_rows:
        app_by_service[(str(row.day), str(row.service))] = {
            "app_actual_cad": float(row.app_actual_cad or 0.0),
            "app_estimated_cad": float(row.app_estimated_cad or 0.0),
            "app_actual_events": int(row.app_actual_events or 0),
            "call_types": str(row.call_types or ""),
        }

    def service_item(day: str, service: str, billed: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
        billed_cad = float(billed.get("billed_cad", 0.0))
        credits_cad = float(billed.get("credits_cad", 0.0))
        net_billed_cad = float(billed.get("net_billed_cad", 0.0))
        app_actual_cad = float(app.get("app_actual_cad", 0.0))
        app_estimated_cad = float(app.get("app_estimated_cad", 0.0))
        return {
            "day": day,
            "service": service,
            "billed_cad": round(billed_cad, 6),
            "credits_cad": round(credits_cad, 6),
            "net_billed_cad": round(net_billed_cad, 6),
            "app_actual_cad": round(app_actual_cad, 6),
            "app_estimated_cad": round(app_estimated_cad, 6),
            "untracked_cad": round(net_billed_cad - app_actual_cad, 6),
            "coverage_pct": round(app_actual_cad / net_billed_cad * 100, 1) if net_billed_cad > 0 else None,
            "app_actual_events": int(app.get("app_actual_events", 0)),
            "call_types": app.get("call_types") or None,
            "billing_rows": int(billed.get("billing_rows", 0)),
        }

    service_rows: list[dict[str, Any]] = []
    day_totals: dict[str, dict[str, float]] = {}
    totals = {"billed_cad": 0.0, "credits_cad": 0.0, "net_billed_cad": 0.0, "app_actual_cad": 0.0, "app_estimated_cad": 0.0}
    for day, service in sorted(set(billing_by_service) | set(app_by_service), reverse=True):
        item = service_item(day, service, billing_by_service.get((day, service), {}), app_by_service.get((day, service), {}))
        service_rows.append(item)
        d = day_totals.setdefault(day, {"billed_cad": 0.0, "credits_cad": 0.0, "net_billed_cad": 0.0, "app_actual_cad": 0.0, "app_estimated_cad": 0.0})
        for key in d:
            d[key] += float(item[key])
            totals[key] += float(item[key])

    sku_rows: list[dict[str, Any]] = []
    for row in billing_sku_rows:
        billed_cad = float(row.billed_cad or 0.0)
        credits_cad = float(row.credits_cad or 0.0)
        net_billed_cad = float(row.net_billed_cad or 0.0)
        sku_rows.append({
            "day": str(row.day),
            "service": str(row.service),
            "sku_id": row.sku_id,
            "sku_description": row.sku_description,
            "billed_cad": round(billed_cad, 6),
            "credits_cad": round(credits_cad, 6),
            "net_billed_cad": round(net_billed_cad, 6),
            "billing_rows": int(row.billing_rows or 0),
        })

    buckets = []
    current = from_date
    import datetime as dt_mod  # noqa: PLC0415
    while current <= to_date:
        key = current.isoformat()
        d = day_totals.get(key, {})
        net_billed = float(d.get("net_billed_cad", 0.0))
        app_actual = float(d.get("app_actual_cad", 0.0))
        buckets.append({
            "key": key,
            "label": key[5:],
            "billed_cad": round(float(d.get("billed_cad", 0.0)), 6),
            "credits_cad": round(float(d.get("credits_cad", 0.0)), 6),
            "net_billed_cad": round(net_billed, 6),
            "app_actual_cad": round(app_actual, 6),
            "app_estimated_cad": round(float(d.get("app_estimated_cad", 0.0)), 6),
            "untracked_cad": round(net_billed - app_actual, 6),
            "coverage_pct": round(app_actual / net_billed * 100, 1) if net_billed > 0 else None,
        })
        current += dt_mod.timedelta(days=1)

    total_net = totals["net_billed_cad"]
    total_app = totals["app_actual_cad"]
    top_untracked_services = sorted(
        [r for r in service_rows if abs(float(r["untracked_cad"])) > 0.000001],
        key=lambda r: float(r["untracked_cad"]),
        reverse=True,
    )[:10]

    return {
        "from_dt": from_date.isoformat(),
        "to_dt": to_date.isoformat(),
        "currency": "CAD",
        "totals": {
            "billed_cad": round(totals["billed_cad"], 6),
            "credits_cad": round(totals["credits_cad"], 6),
            "net_billed_cad": round(total_net, 6),
            "app_actual_cad": round(total_app, 6),
            "app_estimated_cad": round(totals["app_estimated_cad"], 6),
            "untracked_cad": round(total_net - total_app, 6),
            "coverage_pct": round(total_app / total_net * 100, 1) if total_net > 0 else None,
        },
        "buckets": buckets,
        "service_rows": service_rows,
        "sku_rows": sku_rows,
        "top_untracked_services": top_untracked_services,
        "lag_note": "Billing export is delayed 1-24h. App-metered actuals cover controllable runtime calls; untracked is billed infrastructure or unmapped SKU cost.",
    }


@router.post("/notifications/test")
def send_test_notification(user: CurrentUser, body: NotificationTestBody | None = None):
    """Send a test alert email to one safe test recipient, never the full alert list."""
    _require_admin(user)
    reject_role_preview_write(user)
    recipient = _resolve_notification_test_recipient(body, user)
    if _is_reference_user(user):
        return {"sent": True, "recipient": recipient, "transport": "reference"}
    from app.services.email_service import send_alert  # noqa: PLC0415
    result = send_alert(
        subject="Test notification from Admin Panel",
        body=f"This is a test alert sent manually by {user.get('email', 'admin')}.\n"
             "If you received this, email notifications are working correctly.",
        level="info",
        recipients=[recipient],
    )
    return {"sent": result, "recipient": recipient, "transport": settings.email_transport}


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

# Protected accounts: role and active status cannot be changed by anyone via the platform.
_PROTECTED_ADMINS = {PLATFORM_ADMIN_EMAIL, SUPPORT_ADMIN_EMAIL}


def _caller_role(user: dict) -> str:
    """Return the effective role of the API caller."""
    return get_effective_role(user)


def _require_super_admin(user: dict) -> None:
    require_effective_super_admin(user)


@router.get("/chat-logs/recent")
def recent_universal_chat_logs(user: CurrentUser, limit: int = 25):
    """Return recent Universal Chat durable logs. Super Admin only."""
    _require_super_admin(user)
    safe_limit = max(1, min(int(limit or 25), 100))
    if _is_reference_user(user):
        return {"logs": reference_runtime.chat_logs(safe_limit), "limit": safe_limit}
    client = bq_service._get_client()
    profile_names = _profile_names_by_email()
    sql = f"""
        SELECT
          created_at,
          conversation_id,
          turn_id,
          trace_id,
          owner_email,
          owner_role,
          ticket_id,
          target_ticket_id,
          latest_question,
          assistant_answer,
          JSON_VALUE(counts, '$.sources') AS sources_count,
          JSON_VALUE(counts, '$.tickets') AS tickets_count,
          JSON_VALUE(counts, '$.answer_chars') AS answer_chars,
          latency_ms,
          gcs_payload_uri
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        ORDER BY created_at DESC
        LIMIT @limit
    """
    job_config = bq_service.bigquery.QueryJobConfig(
        query_parameters=[
            bq_service.bigquery.ScalarQueryParameter("limit", "INT64", safe_limit),
        ]
    )
    rows = []
    for row in client.query(sql, job_config=job_config).result():
        owner_email = str(row.owner_email or "").strip().lower()
        rows.append({
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "conversation_id": row.conversation_id,
            "turn_id": row.turn_id,
            "trace_id": row.trace_id,
            "owner_email": owner_email or None,
            "owner_display_name": profile_names.get(owner_email, ""),
            "owner_role": row.owner_role,
            "ticket_id": row.ticket_id,
            "target_ticket_id": row.target_ticket_id,
            "question": row.latest_question,
            "answer": row.assistant_answer,
            "sources_count": int(row.sources_count or 0),
            "tickets_count": int(row.tickets_count or 0),
            "answer_chars": int(row.answer_chars or 0),
            "latency_ms": row.latency_ms,
            "gcs_payload_uri": row.gcs_payload_uri,
        })
    return {"logs": rows, "limit": safe_limit}


@router.get("/quality/dashboard")
def quality_dashboard(
    user: CurrentUser,
    start_date: str = "",
    end_date: str = "",
    event_type: str = "",
    quality: str = "",
    risk: str = "",
    validator_action: str = "",
    repair_action: str = "",
    stop_reason: str = "",
    model_surface: str = "",
    requester_email: str = "",
    reason_tag: str = "",
    limit: int = 25,
):
    """Return Super Admin-only Universal Chat quality telemetry."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.quality_dashboard(
            start_date=start_date or None,
            end_date=end_date or None,
            event_type=event_type or None,
            quality=quality or None,
            risk=risk or None,
            validator_action=validator_action or None,
            repair_action=repair_action or None,
            stop_reason=stop_reason or None,
            model_surface=model_surface or None,
            requester_email=requester_email or None,
            reason_tag=reason_tag or None,
            limit=limit,
        )
    try:
        return quality_telemetry.get_quality_dashboard(
            start_date=start_date or None,
            end_date=end_date or None,
            event_type=event_type or None,
            quality=quality or None,
            risk=risk or None,
            validator_action=validator_action or None,
            repair_action=repair_action or None,
            stop_reason=stop_reason or None,
            model_surface=model_surface or None,
            requester_email=requester_email or None,
            reason_tag=reason_tag or None,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("Quality telemetry dashboard failed: %s", exc)
        raise HTTPException(status_code=500, detail="Quality telemetry dashboard query failed") from exc


@router.get("/ai-dispatcher/config")
def ai_dispatcher_config(user: CurrentUser):
    """Return Super Admin-only AI dispatcher runtime config."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.dispatcher_config()
    return ai_dispatcher_admin.read_config()


@router.patch("/ai-dispatcher/config")
def update_ai_dispatcher_config(body: dict, user: CurrentUser):
    """Update bounded AI dispatcher config. Super Admin only."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        return reference_runtime.update_dispatcher_config(body or {}, str(user.get("email") or ""))
    try:
        return ai_dispatcher_admin.update_config(body or {}, actor_email=str(user.get("email") or ""))
    except ai_dispatcher_admin.DispatcherAdminValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ai-dispatcher/emergency-disable")
def ai_dispatcher_emergency_disable(user: CurrentUser):
    """Immediately return AI dispatcher to disabled mode without a deploy."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        return reference_runtime.emergency_disable_dispatcher(str(user.get("email") or ""))
    return ai_dispatcher_admin.emergency_disable(actor_email=str(user.get("email") or ""))


@router.get("/model-transition-capture/config")
def model_transition_capture_config(user: CurrentUser):
    """Return the Super Admin-only temporary exact-request capture setting."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.capture_config()
    return model_transition_capture_admin.read_config()


@router.put("/model-transition-capture/config")
def update_model_transition_capture_config(body: dict, user: CurrentUser):
    """Set the disabled-by-default, named-user capture scope without a deploy."""
    _require_super_admin(user)
    reject_role_preview_write(user)
    if _is_reference_user(user):
        enabled = bool((body or {}).get("enabled", False))
        allowed_emails = [str(item).strip().lower() for item in (body or {}).get("allowed_emails", []) if str(item).strip()]
        if enabled and not allowed_emails:
            raise HTTPException(status_code=400, detail="At least one allowed email is required when capture is enabled")
        if len(set(allowed_emails)) > reference_runtime.capture_config()["max_allowed_emails"]:
            raise HTTPException(status_code=400, detail="Too many allowed emails")
        return reference_runtime.update_capture_config(enabled, allowed_emails, str(user.get("email") or ""))
    try:
        return model_transition_capture_admin.update_config(
            body or {}, actor_email=str(user.get("email") or "")
        )
    except model_transition_capture_admin.CaptureConfigValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai-dispatcher/dashboard")
def ai_dispatcher_dashboard(
    user: CurrentUser,
    start_date: str = "",
    end_date: str = "",
    limit: int = 50,
):
    """Return compact Super Admin-only AI dispatcher telemetry."""
    _require_super_admin(user)
    if _is_reference_user(user):
        return reference_runtime.dispatcher_dashboard(start_date or None, end_date or None, limit)
    try:
        return ai_dispatcher_admin.get_dashboard(
            start_date=start_date or None,
            end_date=end_date or None,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("AI dispatcher dashboard failed: %s", exc)
        raise HTTPException(status_code=500, detail="AI dispatcher dashboard query failed") from exc


@router.get("/usage/dashboard")
def usage_dashboard(
    user: CurrentUser,
    range: str = "to_date",  # noqa: A002 - API query parameter name
    start_date: str = "",
    end_date: str = "",
    user_email: str = "",
    question_type: str = "",
    compare_previous: bool = True,
    group_by: str = "user",
    min_activity: int = 0,
    problem_signal: str = "all",
    category_focus: str = "all",
):
    """Return sanitized Universal Chat usage insights for Admin Panel."""
    _require_admin(user)
    if _is_reference_user(user):
        return reference_runtime.usage_dashboard(
            range_mode=range,
            start_date=start_date or None,
            end_date=end_date or None,
            user_email=user_email or None,
            question_type=question_type or None,
            compare_previous=compare_previous,
            group_by=group_by,
            min_activity=min_activity,
            problem_signal=problem_signal,
            category_focus=category_focus,
            technical_quality_enabled=_caller_role(user) == firestore.ROLE_SUPER_ADMIN,
        )
    try:
        caller_role = _caller_role(user)
        return usage_insights.get_usage_dashboard(
            range_mode=range,
            start_date=start_date or None,
            end_date=end_date or None,
            user_email=user_email or None,
            question_type=question_type or None,
            include_translation_turns=False,
            include_super_admin_usage=False,
            include_technical_quality=caller_role == firestore.ROLE_SUPER_ADMIN,
            compare_previous=compare_previous,
            group_by=group_by,
            min_activity=min_activity,
            problem_signal=problem_signal,
            category_focus=category_focus,
        )
    except Exception as exc:
        logger.exception("Usage Insights dashboard failed: %s", exc)
        raise HTTPException(status_code=500, detail="Usage Insights query failed") from exc


@router.get("/usage/export")
def usage_export(
    user: CurrentUser,
    range: str = "custom",  # noqa: A002 - API query parameter name
    start_date: str = "",
    end_date: str = "",
    user_email: str = "",
    question_type: str = "",
    compare_previous: bool = True,
    group_by: str = "user",
    min_activity: int = 0,
    problem_signal: str = "all",
    category_focus: str = "all",
    format: str = "xlsx",
):
    """Return a management-safe ESIP Usage Excel file."""
    _require_admin(user)
    if _is_reference_user(user):
        if format.strip().lower() not in {"xlsx", "excel"}:
            raise HTTPException(status_code=400, detail="Excel only")
        data = reference_runtime.usage_dashboard(
            range_mode=range,
            start_date=start_date or None,
            end_date=end_date or None,
            user_email=user_email or None,
            question_type=question_type or None,
            compare_previous=compare_previous,
            group_by=group_by,
            min_activity=min_activity,
            problem_signal=problem_signal,
            category_focus=category_focus,
            technical_quality_enabled=_caller_role(user) == firestore.ROLE_SUPER_ADMIN,
        )
        content = usage_insights_export.build_excel_export(data)
        filename = f"esip-usage-{start_date or 'reference'}_to_{end_date or 'reference'}.xlsx"
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
        )
    try:
        caller_role = _caller_role(user)
        export = usage_insights_export.get_export_file(
            range_mode=range,
            start_date=start_date or None,
            end_date=end_date or None,
            user_email=user_email or None,
            question_type=question_type or None,
            compare_previous=compare_previous,
            group_by=group_by,
            min_activity=min_activity,
            problem_signal=problem_signal,
            category_focus=category_focus,
            include_super_admin_usage=False,
            include_technical_quality=caller_role == firestore.ROLE_SUPER_ADMIN,
            file_format=format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Usage Insights export failed: %s", exc)
        raise HTTPException(status_code=500, detail="Usage Insights export failed") from exc

    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/usage/service-desk-kpi")
def usage_service_desk_kpi(
    user: CurrentUser,
    date: str = "",
    refresh_telephony: bool = False,
):
    """Return a daily Service Desk KPI report for Admin Panel."""
    _require_admin(user)
    if _is_reference_user(user):
        return reference_runtime.service_desk_kpi_report(date or None)
    try:
        return service_desk_kpi.get_daily_report(
            report_date=date or None,
            refresh_telephony=refresh_telephony,
        )
    except Exception as exc:
        logger.exception("Service Desk KPI report failed: %s", exc)
        raise HTTPException(status_code=500, detail="Service Desk KPI report query failed") from exc


@router.get("/usage/service-desk-kpi/export")
def usage_service_desk_kpi_export(
    user: CurrentUser,
    date: str = "",
    start_date: str = "",
    end_date: str = "",
    format: str = "pdf",
):
    """Return a server-generated Service Desk KPI PDF or Excel file."""
    _require_admin(user)
    if _is_reference_user(user):
        if start_date and end_date:
            start = datetime.fromisoformat(start_date[:10]).date()
            end = datetime.fromisoformat(end_date[:10]).date()
            reports = [reference_runtime.service_desk_kpi_report((start + timedelta(days=index)).isoformat()) for index in range((end - start).days + 1)]
        else:
            reports = [reference_runtime.service_desk_kpi_report((date or start_date or end_date) or None)]
        requested = (format or "pdf").strip().lower()
        if requested == "pdf":
            content = service_desk_kpi_export.build_pdf_export_for_reports(reports)
            extension = "pdf"
            media_type = "application/pdf"
        elif requested in {"xlsx", "excel"}:
            content = service_desk_kpi_export.build_excel_export_for_reports(reports)
            extension = "xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise HTTPException(status_code=400, detail="Unsupported KPI export format")
        safe_range = reports[0]["date"] if len(reports) == 1 else f"{reports[0]['date']}_to_{reports[-1]['date']}"
        return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="service-desk-kpi-{safe_range}.{extension}"', "Cache-Control": "no-store"})
    try:
        requested_format = (format or "pdf").strip().lower()
        same_day_range = bool(start_date and end_date and start_date[:10] == end_date[:10])
        if requested_format in {"xlsx", "excel"} and (same_day_range or (date and not start_date and not end_date)):
            export = service_desk_kpi_export.get_daily_excel_export((date or start_date or end_date) or None)
        else:
            export = service_desk_kpi_export.get_export_file(
                report_date=date or None,
                start_date=start_date or None,
                end_date=end_date or None,
                file_format=requested_format,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Service Desk KPI export failed: %s", exc)
        raise HTTPException(status_code=500, detail="Service Desk KPI export failed") from exc

    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/usage/service-desk-kpi/snapshots")
def usage_service_desk_kpi_snapshots(
    user: CurrentUser,
    limit: int = 31,
):
    """Return recent cloud-side daily Service Desk KPI snapshot files."""
    _require_admin(user)
    if _is_reference_user(user):
        return {"bucket": "esip-reference-reports", "prefix": "service-desk-kpi/", "items": reference_runtime.service_desk_kpi_snapshots(limit)}
    try:
        items = service_desk_kpi_snapshot.list_snapshots(limit=limit)
    except Exception as exc:
        logger.exception("Service Desk KPI snapshot list failed: %s", exc)
        raise HTTPException(status_code=500, detail="Service Desk KPI snapshot list failed") from exc
    return {
        "bucket": service_desk_kpi_snapshot.resolve_bucket_name(),
        "prefix": service_desk_kpi_snapshot.resolve_prefix(),
        "items": [item.__dict__ for item in items],
    }


@router.get("/usage/service-desk-kpi/snapshots/{report_date}/download")
def usage_service_desk_kpi_snapshot_download(
    user: CurrentUser,
    report_date: str,
):
    """Download a cloud-side daily Service Desk KPI snapshot file."""
    _require_admin(user)
    if _is_reference_user(user):
        report = reference_runtime.service_desk_kpi_report(report_date)
        content = service_desk_kpi_export.build_pdf_export_for_reports([report])
        return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="service-desk-kpi-{report_date}.pdf"', "Cache-Control": "no-store"})
    try:
        export = service_desk_kpi_snapshot.download_snapshot(report_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service_desk_kpi_snapshot.SnapshotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Service Desk KPI snapshot download failed: %s", exc)
        raise HTTPException(status_code=500, detail="Service Desk KPI snapshot download failed") from exc

    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )


KPI_DELIVERY_SCHEDULER_JOB_ID = "esip-service-desk-kpi-snapshot-daily"


def _kpi_primary_slot(config: service_desk_kpi_delivery.DeliveryConfig) -> service_desk_kpi_delivery.DeliverySlot:
    for slot in config.slots:
        if slot.enabled:
            return slot
    if config.slots:
        return config.slots[0]
    return service_desk_kpi_delivery.DeliverySlot(id="slot-1030", time=service_desk_kpi_delivery.DEFAULT_SLOT_TIME)


def _get_kpi_delivery_scheduler_state() -> dict[str, Any] | None:
    try:
        return scheduler_admin.get_scheduler(KPI_DELIVERY_SCHEDULER_JOB_ID)
    except Exception as exc:
        logger.warning("Service Desk KPI delivery scheduler state read failed: %s", exc)
        return None


def _sync_kpi_delivery_scheduler(config: service_desk_kpi_delivery.DeliveryConfig) -> dict[str, Any]:
    slot = _kpi_primary_slot(config)
    scheduler_state = scheduler_admin.get_scheduler(KPI_DELIVERY_SCHEDULER_JOB_ID)
    if scheduler_state.get("schedule_time") != slot.time or scheduler_state.get("time_zone") != scheduler_admin.TIME_ZONE:
        scheduler_state = scheduler_admin.update_scheduler_time(KPI_DELIVERY_SCHEDULER_JOB_ID, slot.time)
    if config.enabled and not scheduler_state.get("enabled"):
        return scheduler_admin.resume_scheduler(KPI_DELIVERY_SCHEDULER_JOB_ID)
    if not config.enabled and scheduler_state.get("enabled"):
        return scheduler_admin.pause_scheduler(KPI_DELIVERY_SCHEDULER_JOB_ID)
    return scheduler_state


def _kpi_delivery_response(config: service_desk_kpi_delivery.DeliveryConfig, scheduler_state: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = service_desk_kpi_delivery.normalize_config(config)
    scheduler_payload = scheduler_state if scheduler_state is not None else _get_kpi_delivery_scheduler_state()
    return {
        "enabled": normalized.enabled,
        "timezone": normalized.timezone,
        "recipients": list(normalized.recipients),
        "cc_recipients": list(normalized.cc_recipients),
        "slots": [asdict(slot) for slot in normalized.slots],
        "last_delivery": normalized.last_delivery,
        "last_test": normalized.last_test,
        "transport": settings.email_transport,
        "safe_test_recipients": sorted(service_desk_kpi_delivery.SAFE_TEST_RECIPIENTS),
        "scheduler": scheduler_payload,
    }


def _require_kpi_delivery_write(user: CurrentUser) -> None:
    # KPI delivery writes can trigger automated production email distribution,
    # so they are intentionally restricted to effective Super Admin users.
    reject_role_preview_write(user)
    _require_super_admin(user)


def _delivery_validation_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _dedupe_or_422(raw_recipients: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_recipients:
        email = service_desk_kpi_delivery.normalize_email(raw)
        if email in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate recipient: {email}")
        seen.add(email)
        normalized.append(email)
    return tuple(normalized)


def _reject_to_cc_overlap(recipients: tuple[str, ...], cc_recipients: tuple[str, ...]) -> None:
    overlap = sorted(set(recipients).intersection(cc_recipients))
    if overlap:
        raise HTTPException(status_code=422, detail=f"Recipient cannot be both To and CC: {overlap[0]}")


@router.get("/usage/service-desk-kpi/delivery")
def usage_service_desk_kpi_delivery(user: CurrentUser):
    """Return Service Desk KPI email delivery config for the Admin KPI panel."""
    _require_admin(user)
    if _is_reference_user(user):
        return reference_runtime.kpi_delivery_config()
    try:
        config = service_desk_kpi_delivery.get_delivery_config()
    except Exception as exc:
        logger.exception("Service Desk KPI delivery config read failed: %s", exc)
        raise HTTPException(status_code=500, detail="Service Desk KPI delivery config read failed") from exc
    return _kpi_delivery_response(config)


@router.patch("/usage/service-desk-kpi/delivery")
def update_usage_service_desk_kpi_delivery(body: KpiDeliveryConfigBody, user: CurrentUser):
    """Update KPI delivery config. Production delivery writes require Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        return reference_runtime.update_kpi_delivery_config(body.model_dump(exclude_none=True, mode="json"))
    try:
        current = service_desk_kpi_delivery.get_delivery_config()
        recipients = current.recipients if body.recipients is None else _dedupe_or_422(body.recipients)
        cc_recipients = current.cc_recipients if body.cc_recipients is None else _dedupe_or_422(body.cc_recipients)
        _reject_to_cc_overlap(recipients, cc_recipients)
        slots = (
            current.slots
            if body.slots is None
            else tuple(
                service_desk_kpi_delivery.normalize_slot(
                    {
                        "id": slot.id,
                        "time": slot.time,
                        "enabled": slot.enabled,
                        "label": slot.label,
                    }
                )
                for slot in body.slots
            )
        )
        updated = service_desk_kpi_delivery.normalize_config(
            {
                "enabled": current.enabled if body.enabled is None else body.enabled,
                "timezone": current.timezone if body.timezone is None else body.timezone,
                "recipients": list(recipients),
                "cc_recipients": list(cc_recipients),
                "slots": [asdict(slot) for slot in slots],
                "last_delivery": current.last_delivery,
                "last_test": current.last_test,
            }
        )
        if body.slots is not None and len({slot.id for slot in updated.slots}) != len(updated.slots):
            raise HTTPException(status_code=422, detail="Duplicate slot id")
        scheduler_state = None
        if body.enabled is not None or body.slots is not None:
            try:
                scheduler_state = _sync_kpi_delivery_scheduler(updated)
            except Exception as exc:
                logger.exception("Service Desk KPI delivery scheduler sync failed")
                raise HTTPException(status_code=502, detail=f"Could not update KPI delivery scheduler: {exc}") from exc
        saved = service_desk_kpi_delivery.save_delivery_config(updated)
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    return _kpi_delivery_response(saved, scheduler_state)


@router.post("/usage/service-desk-kpi/delivery/recipients")
def add_usage_service_desk_kpi_delivery_recipient(body: KpiDeliveryRecipientBody, user: CurrentUser):
    """Add one production KPI delivery recipient. Requires Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        email = body.email.strip().lower()
        current = reference_runtime.kpi_delivery_config()
        if email in current["recipients"] or email in current["cc_recipients"]:
            raise HTTPException(status_code=422, detail=f"Duplicate recipient: {email}")
        return reference_runtime.update_kpi_delivery_config({"recipients": [*current["recipients"], email]})
    try:
        email = service_desk_kpi_delivery.normalize_email(body.email)
        current = service_desk_kpi_delivery.get_delivery_config()
        if email in current.recipients or email in current.cc_recipients:
            raise HTTPException(status_code=422, detail=f"Duplicate recipient: {email}")
        updated = service_desk_kpi_delivery.save_delivery_config(
            service_desk_kpi_delivery.DeliveryConfig(
                enabled=current.enabled,
                timezone=current.timezone,
                recipients=(*current.recipients, email),
                cc_recipients=current.cc_recipients,
                slots=current.slots,
                last_delivery=current.last_delivery,
                last_test=current.last_test,
            )
        )
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    return _kpi_delivery_response(updated)


@router.delete("/usage/service-desk-kpi/delivery/recipients/{email}")
def remove_usage_service_desk_kpi_delivery_recipient(email: str, user: CurrentUser):
    """Remove one production KPI delivery recipient. Requires Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        target = email.strip().lower()
        current = reference_runtime.kpi_delivery_config()
        return reference_runtime.update_kpi_delivery_config({"recipients": [item for item in current["recipients"] if item != target]})
    try:
        target = service_desk_kpi_delivery.normalize_email(email)
        current = service_desk_kpi_delivery.get_delivery_config()
        updated = service_desk_kpi_delivery.save_delivery_config(
            service_desk_kpi_delivery.DeliveryConfig(
                enabled=current.enabled,
                timezone=current.timezone,
                recipients=tuple(item for item in current.recipients if item != target),
                cc_recipients=current.cc_recipients,
                slots=current.slots,
                last_delivery=current.last_delivery,
                last_test=current.last_test,
            )
        )
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    return _kpi_delivery_response(updated)


@router.post("/usage/service-desk-kpi/delivery/slots")
def add_usage_service_desk_kpi_delivery_slot(body: KpiDeliverySlotBody, user: CurrentUser):
    """Add one KPI delivery slot. Requires Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        current = reference_runtime.kpi_delivery_config()
        slot = body.model_dump(mode="json")
        slot["id"] = slot.get("id") or f"slot-{slot['time'].replace(':', '')}"
        if any(item["id"] == slot["id"] for item in current["slots"]):
            raise HTTPException(status_code=422, detail=f"Duplicate slot id: {slot['id']}")
        return reference_runtime.update_kpi_delivery_config({"slots": [*current["slots"], slot]})
    try:
        slot = service_desk_kpi_delivery.normalize_slot(
            {"id": body.id, "time": body.time, "enabled": body.enabled, "label": body.label}
        )
        current = service_desk_kpi_delivery.get_delivery_config()
        if any(existing.id == slot.id for existing in current.slots):
            raise HTTPException(status_code=422, detail=f"Duplicate slot id: {slot.id}")
        next_config = service_desk_kpi_delivery.DeliveryConfig(
            enabled=current.enabled,
            timezone=current.timezone,
            recipients=current.recipients,
            cc_recipients=current.cc_recipients,
            slots=(*current.slots, slot),
            last_delivery=current.last_delivery,
            last_test=current.last_test,
        )
        scheduler_state = _sync_kpi_delivery_scheduler(next_config)
        updated = service_desk_kpi_delivery.save_delivery_config(next_config)
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Service Desk KPI delivery scheduler sync failed")
        raise HTTPException(status_code=502, detail=f"Could not update KPI delivery scheduler: {exc}") from exc
    return _kpi_delivery_response(updated, scheduler_state)


@router.put("/usage/service-desk-kpi/delivery/slots/{slot_id}")
def update_usage_service_desk_kpi_delivery_slot(slot_id: str, body: KpiDeliverySlotBody, user: CurrentUser):
    """Update one KPI delivery slot. Requires Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        current = reference_runtime.kpi_delivery_config()
        if not any(item["id"] == slot_id for item in current["slots"]):
            raise HTTPException(status_code=404, detail=f"Slot not found: {slot_id}")
        replacement = {**body.model_dump(mode="json"), "id": slot_id}
        slots = [replacement if item["id"] == slot_id else item for item in current["slots"]]
        return reference_runtime.update_kpi_delivery_config({"slots": slots})
    try:
        current = service_desk_kpi_delivery.get_delivery_config()
        found = False
        replacement = service_desk_kpi_delivery.normalize_slot(
            {"id": slot_id, "time": body.time, "enabled": body.enabled, "label": body.label}
        )
        slots: list[service_desk_kpi_delivery.DeliverySlot] = []
        for existing in current.slots:
            if existing.id == slot_id:
                slots.append(replacement)
                found = True
            else:
                slots.append(existing)
        if not found:
            raise HTTPException(status_code=404, detail=f"Slot not found: {slot_id}")
        next_config = service_desk_kpi_delivery.DeliveryConfig(
            enabled=current.enabled,
            timezone=current.timezone,
            recipients=current.recipients,
            cc_recipients=current.cc_recipients,
            slots=tuple(slots),
            last_delivery=current.last_delivery,
            last_test=current.last_test,
        )
        scheduler_state = _sync_kpi_delivery_scheduler(next_config)
        updated = service_desk_kpi_delivery.save_delivery_config(next_config)
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Service Desk KPI delivery scheduler sync failed")
        raise HTTPException(status_code=502, detail=f"Could not update KPI delivery scheduler: {exc}") from exc
    return _kpi_delivery_response(updated, scheduler_state)


@router.delete("/usage/service-desk-kpi/delivery/slots/{slot_id}")
def remove_usage_service_desk_kpi_delivery_slot(slot_id: str, user: CurrentUser):
    """Remove one KPI delivery slot. Requires Super Admin."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        current = reference_runtime.kpi_delivery_config()
        return reference_runtime.update_kpi_delivery_config({"slots": [item for item in current["slots"] if item["id"] != slot_id]})
    current = service_desk_kpi_delivery.get_delivery_config()
    next_config = service_desk_kpi_delivery.DeliveryConfig(
        enabled=current.enabled,
        timezone=current.timezone,
        recipients=current.recipients,
        cc_recipients=current.cc_recipients,
        slots=tuple(slot for slot in current.slots if slot.id != slot_id),
        last_delivery=current.last_delivery,
        last_test=current.last_test,
    )
    try:
        scheduler_state = _sync_kpi_delivery_scheduler(next_config)
        updated = service_desk_kpi_delivery.save_delivery_config(next_config)
    except Exception as exc:
        logger.exception("Service Desk KPI delivery scheduler sync failed")
        raise HTTPException(status_code=502, detail=f"Could not update KPI delivery scheduler: {exc}") from exc
    return _kpi_delivery_response(updated, scheduler_state)


@router.post("/usage/service-desk-kpi/delivery/test")
def send_usage_service_desk_kpi_delivery_test(body: KpiDeliveryTestBody, user: CurrentUser):
    """Send one KPI delivery test email to an allowlisted safe mailbox only."""
    _require_kpi_delivery_write(user)
    if _is_reference_user(user):
        recipient = body.recipient.strip().lower()
        current = reference_runtime.update_kpi_delivery_config({"last_test": {"status": "sent", "recipient": recipient, "report_date": body.report_date or "2026-08-03", "requested_by": str(user.get("email") or "").strip().lower(), "requested_at": datetime.now(timezone.utc).isoformat()}})
        return {**current, "test": {"sent": True, "recipient": recipient, "report_date": body.report_date or "2026-08-03", "gcs_uri": None}}
    try:
        outcome = service_desk_kpi_delivery.send_test_delivery(
            recipient=body.recipient,
            report_date=body.report_date,
        )
        current = service_desk_kpi_delivery.get_delivery_config()
        saved = service_desk_kpi_delivery.save_delivery_config(
            service_desk_kpi_delivery.DeliveryConfig(
                enabled=current.enabled,
                timezone=current.timezone,
                recipients=current.recipients,
                cc_recipients=current.cc_recipients,
                slots=current.slots,
                last_delivery=current.last_delivery,
                last_test={
                    **asdict(outcome),
                    "recipient": service_desk_kpi_delivery.normalize_email(body.recipient),
                    "requested_by": str(user.get("email") or "").strip().lower(),
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        )
    except service_desk_kpi_delivery.DeliveryValidationError as exc:
        raise _delivery_validation_error(exc) from exc
    except service_desk_kpi_delivery.DeliverySendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {**_kpi_delivery_response(saved), "test": asdict(outcome)}


@router.get("/impact/dashboard")
def impact_dashboard(
    user: CurrentUser,
    rollout_date: str = "2026-05-26",
    end_date: str = "",
):
    """Return cautious ESIP adoption, efficiency, outcome, and quality impact metrics."""
    _require_admin(user)
    if _is_reference_user(user):
        return reference_runtime.impact_dashboard(rollout_date or None, end_date or None)
    try:
        return impact_metrics.get_impact_dashboard(
            rollout_date=rollout_date or None,
            end_date=end_date or None,
        )
    except Exception as exc:
        logger.exception("Impact dashboard failed: %s", exc)
        raise HTTPException(status_code=500, detail="Impact dashboard query failed") from exc


@router.get("/impact/report")
def impact_report(
    user: CurrentUser,
    rollout_date: str = "2026-05-26",
    end_date: str = "",
):
    """Return a deterministic printable executive impact report payload."""
    _require_admin(user)
    if _is_reference_user(user):
        return reference_runtime.impact_dashboard(rollout_date or None, end_date or None)
    try:
        return impact_metrics.get_impact_report(
            rollout_date=rollout_date or None,
            end_date=end_date or None,
        )
    except Exception as exc:
        logger.exception("Impact report failed: %s", exc)
        raise HTTPException(status_code=500, detail="Impact report query failed") from exc


@router.get("/impact/export")
def impact_report_export(
    user: CurrentUser,
    rollout_date: str = "2026-05-26",
    end_date: str = "",
    format: str = "pdf",
):
    """Return a server-generated Impact PDF or Excel file."""
    _require_admin(user)
    if _is_reference_user(user):
        report = reference_runtime.impact_dashboard(rollout_date or None, end_date or None)
        requested = (format or "pdf").strip().lower()
        if requested == "pdf":
            content = impact_export.build_pdf_export(report)
            extension = "pdf"
            media_type = "application/pdf"
        elif requested in {"xlsx", "excel"}:
            content = impact_export.build_excel_export(report)
            extension = "xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            raise HTTPException(status_code=400, detail="Unsupported Impact export format")
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="esip-impact-{rollout_date}.{extension}"', "Cache-Control": "no-store"},
        )
    try:
        export = impact_export.get_export_file(
            rollout_date=rollout_date or None,
            end_date=end_date or None,
            file_format=format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Impact export failed: %s", exc)
        raise HTTPException(status_code=500, detail="Impact export failed") from exc

    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )


def _target_role_from_doc(doc_data: dict) -> str:
    return firestore._role_from_doc(doc_data)


def _profile_names_by_email() -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        for doc in firestore.get_db().collection("profiles").stream():
            data = doc.to_dict() or {}
            email = str(data.get("email") or "").strip().lower()
            name = firestore.clean_auth_display_name(
                data.get("display_name")
                or data.get("name")
                or data.get("full_name")
                or "",
                email,
            )
            if email and name:
                names[email] = name
    except Exception as exc:
        logger.warning("admin: could not load profile display names: %s", exc)
    return names


def _user_payload(
    email: str,
    data: dict,
    *,
    include_chat_logging_flag: bool = False,
    profile_names: dict[str, str] | None = None,
) -> dict:
    safe_email = (email or "").strip().lower()
    display_name = firestore.clean_auth_display_name(
        data.get("display_name")
        or data.get("name")
        or data.get("full_name")
        or (profile_names or {}).get(safe_email)
        or "",
        safe_email,
    )
    payload = {
        "email": safe_email,
        "display_name": display_name,
        "active": bool(data.get("active", False)),
        "role": firestore._role_from_doc(data),
        "esip_access_granted_at": data.get("esip_access_granted_at") or "2026-05-26",
    }
    if include_chat_logging_flag:
        payload["universal_chat_logging_disabled"] = bool(
            data.get("universal_chat_logging_disabled", False)
        )
    return payload


@router.get("/users")
def list_users(user: CurrentUser):
    """Return all documents from the users/ collection."""
    _require_admin(user)
    include_chat_logging_flag = _caller_role(user) == firestore.ROLE_SUPER_ADMIN
    if _is_reference_user(user):
        users = reference_runtime.admin_users()
        if not include_chat_logging_flag:
            for item in users:
                item.pop("universal_chat_logging_disabled", None)
        return {"users": users}
    docs = firestore.get_db().collection("users").stream()
    profile_names = _profile_names_by_email()
    users = []
    for doc in docs:
        data = doc.to_dict() or {}
        users.append(
            _user_payload(
                doc.id,
                data,
                include_chat_logging_flag=include_chat_logging_flag,
                profile_names=profile_names,
            )
        )
    users.sort(key=lambda u: u["email"])
    return {"users": users}


@router.post("/users")
def add_user(body: dict, user: CurrentUser):
    """
    Add a user to the allowlist.
    - admin can only add role=user.
    - super_admin can add any role.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    email: str = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")

    role: str = (body.get("role") or firestore.ROLE_USER).strip()
    if role not in firestore.VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid role. Must be one of: {sorted(firestore.VALID_ROLES)}")

    caller_role = _caller_role(user)
    if caller_role == firestore.ROLE_ADMIN and role != firestore.ROLE_USER:
        raise HTTPException(status_code=403, detail="Admins can only add users with role=user")
    if (
        "universal_chat_logging_disabled" in body
        and caller_role != firestore.ROLE_SUPER_ADMIN
    ):
        raise HTTPException(status_code=403, detail="Super-admin access required for chat logging settings")

    if _is_reference_user(user):
        return reference_runtime.add_admin_user(
            email,
            role,
            chat_logging_disabled=bool(body.get("universal_chat_logging_disabled", False)),
        )

    new_user = {
        "active": True,
        "role": role,
        "esip_access_granted_at": datetime.now(timezone.utc).date().isoformat(),
    }
    if "universal_chat_logging_disabled" in body:
        new_user["universal_chat_logging_disabled"] = bool(body["universal_chat_logging_disabled"])

    firestore.get_db().collection("users").document(email).set(
        new_user, merge=True
    )
    logger.info("admin: user added email=%s role=%s by %s", email, role, user.get("email"))
    return _user_payload(
        email,
        new_user,
        include_chat_logging_flag=caller_role == firestore.ROLE_SUPER_ADMIN,
    )


@router.patch("/users/{email}")
def update_user(email: str, body: dict, user: CurrentUser):
    """
    Update active and/or role for a user.
    Permission matrix:
      - admin: can only modify role=user accounts (toggle active, promote to admin)
      - super_admin: can modify anyone except protected accounts
    """
    _require_admin(user)
    reject_role_preview_write(user)
    email = email.strip().lower()

    reference_target = reference_runtime.admin_user(email) if _is_reference_user(user) else None
    if _is_reference_user(user):
        if reference_target is None:
            raise HTTPException(status_code=404, detail="User not found")
        target_role = str(reference_target.get("role") or firestore.ROLE_USER)
        doc_ref = None
    else:
        doc_ref = firestore.get_db().collection("users").document(email)
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        target_role = _target_role_from_doc(doc.to_dict() or {})
    caller_role = _caller_role(user)
    body_keys = set(body.keys())
    protected_identity_fields = {"active", "role"}

    # Protected accounts keep immutable role/active status, but Super Admin may
    # still manage their durable Universal Chat logging exclusion flag.
    if email in _PROTECTED_ADMINS and body_keys & protected_identity_fields:
        raise HTTPException(status_code=403, detail=f"{email} is a protected account")

    # admin cannot touch other admins or super_admins
    if caller_role == firestore.ROLE_ADMIN and target_role != firestore.ROLE_USER:
        raise HTTPException(status_code=403, detail="Admins can only manage users with role=user")
    if (
        "universal_chat_logging_disabled" in body
        and caller_role != firestore.ROLE_SUPER_ADMIN
    ):
        raise HTTPException(status_code=403, detail="Super-admin access required for chat logging settings")

    # Prevent removing own admin/super_admin role
    if user.get("email", "").lower() == email and "role" in body:
        new_role = body.get("role", "")
        if new_role == firestore.ROLE_USER:
            raise HTTPException(status_code=400, detail="Cannot downgrade your own role")

    update: dict = {}
    if "active" in body:
        update["active"] = bool(body["active"])
    if "role" in body:
        new_role = (body["role"] or "").strip()
        if new_role not in firestore.VALID_ROLES:
            raise HTTPException(status_code=422, detail=f"Invalid role: {new_role}")
        # admin can promote a user to admin, but cannot grant super_admin.
        # Existing admin/super_admin targets are blocked above.
        if caller_role == firestore.ROLE_ADMIN and new_role not in {firestore.ROLE_USER, firestore.ROLE_ADMIN}:
            raise HTTPException(status_code=403, detail="Admins cannot promote users to super_admin")
        update["role"] = new_role
    if "universal_chat_logging_disabled" in body:
        update["universal_chat_logging_disabled"] = bool(body["universal_chat_logging_disabled"])

    if not update:
        raise HTTPException(status_code=422, detail="Provide active, role, or universal_chat_logging_disabled")

    if _is_reference_user(user):
        data = reference_runtime.update_admin_user(email, update)
        if data is None:
            raise HTTPException(status_code=404, detail="User not found")
    else:
        assert doc_ref is not None
        doc_ref.update(update)
        data = doc_ref.get().to_dict() or {}
    logger.info("admin: user updated email=%s patch=%s by %s", email, update, user.get("email"))
    return _user_payload(
        email,
        data,
        include_chat_logging_flag=caller_role == firestore.ROLE_SUPER_ADMIN,
    )


@router.delete("/users/{email}")
def delete_user(email: str, user: CurrentUser):
    """
    Remove a user from the allowlist.
    - admin can only delete role=user accounts.
    - super_admin can delete anyone except protected accounts and themselves.
    """
    _require_admin(user)
    reject_role_preview_write(user)
    email = email.strip().lower()

    if user.get("email", "").lower() == email:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if email in _PROTECTED_ADMINS:
        raise HTTPException(status_code=403, detail=f"{email} is a protected account")

    reference_target = reference_runtime.admin_user(email) if _is_reference_user(user) else None
    if _is_reference_user(user):
        if reference_target is None:
            raise HTTPException(status_code=404, detail="User not found")
        target_role = str(reference_target.get("role") or firestore.ROLE_USER)
        doc_ref = None
    else:
        doc_ref = firestore.get_db().collection("users").document(email)
        doc = doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="User not found")
        target_role = _target_role_from_doc(doc.to_dict() or {})
    caller_role = _caller_role(user)
    if caller_role == firestore.ROLE_ADMIN and target_role != firestore.ROLE_USER:
        raise HTTPException(status_code=403, detail="Admins can only delete users with role=user")

    if _is_reference_user(user):
        reference_runtime.delete_admin_user(email)
    else:
        assert doc_ref is not None
        doc_ref.delete()
    logger.info("admin: user deleted email=%s by %s", email, user.get("email"))
    return {"deleted": email}


# ---------------------------------------------------------------------------
# Infrastructure cost recording (called by Cloud Scheduler hourly)
# ---------------------------------------------------------------------------

def _get_cloud_run_min_instances() -> int:
    """
    Query Cloud Run Admin API for current min-instances of esip-backend.
    Returns 0 on any error (safe default — don't record cost if unsure).
    """
    import requests as _req  # noqa: PLC0415
    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests  # noqa: PLC0415
    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        project = settings.google_cloud_project_id
        url = (
            f"https://run.googleapis.com/v2/projects/{project}"
            f"/locations/us-central1/services/esip-backend"
        )
        resp = _req.get(
            url,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # v2 API: scaling.minInstanceCount (may be absent = 0)
        return int(data.get("scaling", {}).get("minInstanceCount", 0))
    except Exception as exc:
        logger.warning("record-idle: could not query Cloud Run min-instances: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# OCR failures — images without [IMAGE DESCRIPTION:] tag
# ---------------------------------------------------------------------------

@router.get("/ocr-failures")
def get_ocr_failures(user: CurrentUser, limit: int = 20):
    """
    Return up to `limit` ticket attachment images that were processed by OCR
    but do not contain [IMAGE DESCRIPTION:] — indicating the new prompt was
    not applied (likely expired URL or Gemini error during reprocess).
    """
    _require_admin(user)
    from app.services.bigquery import _bq_client, GCP_PROJECT, BQ_DATASET
    sql = f"""
        SELECT content_url, ticket_id, ocr_text, processed_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr`
        WHERE processing_status = 'success'
          AND NOT CONTAINS_SUBSTR(IFNULL(ocr_text, ''), '[IMAGE DESCRIPTION:')
        ORDER BY RAND()
        LIMIT {int(limit)}
    """
    try:
        rows = list(_bq_client.query(sql).result())
        return [
            {
                "ticket_id": r.ticket_id,
                "content_url": r.content_url,
                "ocr_text": (r.ocr_text or "")[:300],
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
                "zendesk_url": f"https://support.example.com/agent/tickets/{r.ticket_id}",
            }
            for r in rows
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/record-idle")
def record_idle_cost(x_sync_secret: Optional[str] = Header(default=None)):
    """
    Record one hour of Cloud Run idle cost into cost_tracker.
    Called hourly by Cloud Scheduler via X-Sync-Secret header auth.
    Checks actual min-instances via Cloud Run API — skips if platform is paused (min=0).
    """
    secret_ok = SYNC_SECRET and x_sync_secret == SYNC_SECRET
    if not secret_ok:
        raise HTTPException(status_code=403, detail="Invalid or missing sync secret")

    min_instances = _get_cloud_run_min_instances()
    if min_instances < 1:
        logger.info("admin: record-idle skipped — min-instances=%d (platform paused)", min_instances)
        return {"recorded": False, "reason": "platform paused (min-instances=0)", "min_instances": min_instances}

    from app.services.cost_tracker import CallType, cloud_run_idle_cost_per_hour_cad, record_flat_cost  # noqa: PLC0415
    cost = cloud_run_idle_cost_per_hour_cad()
    recorded = record_flat_cost(CallType.CLOUD_RUN_IDLE, cost_usd=cost, units=1)
    logger.info("admin: recorded cloud_run_idle cost=$%.4f (min-instances=%d)", recorded, min_instances)
    return {"recorded": True, "currency": "CAD", "cost_usd": recorded, "cost_cad": recorded, "min_instances": min_instances}
