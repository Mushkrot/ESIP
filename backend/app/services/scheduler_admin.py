"""Cloud Scheduler management helpers for Admin Data Sources UI."""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import google.auth
import google.auth.transport.requests
import requests

from app.config import settings

LOCATION = "us-central1"
TIME_ZONE = "America/New_York"
_CLOUD_PLATFORM_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

JOBS: dict[str, dict[str, str]] = {
    "esip-drive-sync": {
        "display_name": "Google Drive document sync",
        "purpose": "Updates enabled Google Drive folders into ESIP knowledge search.",
        "kind": "sync",
    },
    "esip-bq-sync": {
        "display_name": "Zendesk sync",
        "purpose": "Updates Zendesk tickets, OCR/embeddings, and enabled Zendesk article metadata.",
        "kind": "sync",
    },
    "esip-cloud-build-cost-sync": {
        "display_name": "Cloud Build cost sync",
        "purpose": "Imports Cloud Build costs from Billing Export into Admin Costs.",
        "kind": "cost",
    },
    "esip-record-idle": {
        "display_name": "Cloud Run idle cost recorder",
        "purpose": "Records hourly always-on Cloud Run idle cost.",
        "kind": "cost",
    },
    "esip-service-desk-kpi-snapshot-daily": {
        "display_name": "Service Desk KPI email delivery",
        "purpose": "Runs the daily Service Desk KPI Excel generation and Gmail distribution job.",
        "kind": "kpi",
    },
}

_DAILY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _token() -> str:
    try:
        if os.environ.get("K_SERVICE"):
            from google.auth import compute_engine

            creds = compute_engine.Credentials(scopes=_CLOUD_PLATFORM_SCOPES)
        else:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=_CLOUD_PLATFORM_SCOPES,
            )
    except Exception:
        creds, _ = google.auth.default(scopes=_CLOUD_PLATFORM_SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _job_name(job_id: str) -> str:
    if job_id not in JOBS:
        raise ValueError(f"Unknown scheduler job: {job_id}")
    return f"projects/{settings.google_cloud_project_id}/locations/{LOCATION}/jobs/{job_id}"


def _url(job_id: str, suffix: str = "") -> str:
    return f"https://cloudscheduler.googleapis.com/v1/{_job_name(job_id)}{suffix}"


def _schedule_to_time(schedule: str) -> str | None:
    parts = schedule.split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"] and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[1]):02d}:{int(parts[0]):02d}"
    return None


def _time_to_schedule(time_hhmm: str) -> str:
    match = _DAILY_TIME_RE.match(time_hhmm)
    if not match:
        raise ValueError("Schedule time must be HH:mm in 24-hour Eastern time")
    hour, minute = match.groups()
    return f"{int(minute)} {int(hour)} * * *"


def _parse_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    schedule = payload.get("schedule", "")
    return {
        "job_id": job_id,
        **JOBS[job_id],
        "state": payload.get("state", "UNKNOWN"),
        "enabled": payload.get("state") == "ENABLED",
        "schedule": schedule,
        "schedule_time": _schedule_to_time(schedule),
        "time_zone": payload.get("timeZone", TIME_ZONE),
        "uri": (payload.get("httpTarget") or {}).get("uri"),
        "last_attempt_time": payload.get("status", {}).get("lastAttemptTime"),
        "user_update_time": payload.get("userUpdateTime"),
    }


def get_scheduler(job_id: str) -> dict[str, Any]:
    resp = requests.get(_url(job_id), headers=_headers(), timeout=20)
    resp.raise_for_status()
    return _parse_job(job_id, resp.json())


def list_schedulers() -> list[dict[str, Any]]:
    return [get_scheduler(job_id) for job_id in JOBS]


def pause_scheduler(job_id: str) -> dict[str, Any]:
    resp = requests.post(_url(job_id, ":pause"), headers=_headers(), json={}, timeout=20)
    resp.raise_for_status()
    return get_scheduler(job_id)


def resume_scheduler(job_id: str) -> dict[str, Any]:
    resp = requests.post(_url(job_id, ":resume"), headers=_headers(), json={}, timeout=20)
    resp.raise_for_status()
    return get_scheduler(job_id)


def run_scheduler_now(job_id: str) -> dict[str, Any]:
    job = get_scheduler(job_id)
    was_paused = job.get("state") == "PAUSED"
    previous_attempt_time = job.get("last_attempt_time")
    if was_paused:
        resume_resp = requests.post(_url(job_id, ":resume"), headers=_headers(), json={}, timeout=20)
        resume_resp.raise_for_status()
    mark_scheduler_dispatch_pending(job_id)
    dispatched = False
    run_accepted = False
    try:
        resp = requests.post(_url(job_id, ":run"), headers=_headers(), json={}, timeout=20)
        if not resp.ok:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text[:1000]
            clear_scheduler_dispatch_pending(job_id)
            raise RuntimeError(f"Cloud Scheduler jobs.run failed ({resp.status_code}): {detail}")
        run_accepted = True
        response_body = resp.json()
        dispatched = _wait_for_manual_dispatch(job_id, previous_attempt_time)
    except Exception:
        if not run_accepted:
            clear_scheduler_dispatch_pending(job_id)
        raise
    finally:
        if was_paused:
            pause_resp = requests.post(_url(job_id, ":pause"), headers=_headers(), json={}, timeout=20)
            pause_resp.raise_for_status()
    return {
        "job_id": job_id,
        "status": "triggered",
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "uri": job.get("uri"),
        "http_status": resp.status_code,
        "job_state": response_body.get("state"),
        "last_attempt_time": (response_body.get("status") or {}).get("lastAttemptTime"),
        "dispatch_observed": dispatched,
        "restored_paused": was_paused,
    }


def mark_scheduler_dispatch_pending(job_id: str, ttl_seconds: int = 120) -> None:
    """Record a short-lived manual Scheduler dispatch window for Admin UI locking."""
    from app.services import firestore  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    firestore.get_db().collection("system").document(f"scheduler_dispatch_{job_id}").set(
        {
            "job_id": job_id,
            "status": "pending",
            "requested_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
            "updated_at": now,
        },
        merge=True,
    )


def clear_scheduler_dispatch_pending(job_id: str) -> None:
    """Clear the short-lived manual Scheduler dispatch marker once the target job starts."""
    from app.services import firestore  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    firestore.get_db().collection("system").document(f"scheduler_dispatch_{job_id}").set(
        {
            "job_id": job_id,
            "status": "started",
            "updated_at": now,
            "expires_at": now,
        },
        merge=True,
    )


def get_scheduler_dispatch_pending(job_id: str) -> dict[str, Any] | None:
    """Return an unexpired manual dispatch marker, if one exists."""
    from app.services import firestore  # noqa: PLC0415

    doc = firestore.get_db().collection("system").document(f"scheduler_dispatch_{job_id}").get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    expires_at = data.get("expires_at")
    if data.get("status") != "pending" or not expires_at:
        return None
    if expires_at <= datetime.now(timezone.utc):
        return None
    return data


def _wait_for_manual_dispatch(job_id: str, previous_attempt_time: str | None) -> bool:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            current = get_scheduler(job_id)
            attempt_time = current.get("last_attempt_time")
            if attempt_time and attempt_time != previous_attempt_time:
                return True
        except Exception:
            pass

        if job_id == "esip-drive-sync":
            try:
                from app.services import firestore  # noqa: PLC0415

                state = firestore.get_drive_sync_state()
                if state.get("running") or "sync-drive" in str(state.get("run_owner") or ""):
                    return True
            except Exception:
                pass
        elif job_id == "esip-bq-sync":
            try:
                from app.services import firestore  # noqa: PLC0415

                state = firestore.get_bq_sync_state()
                if state.get("running") or "bq-sync" in str(state.get("run_owner") or ""):
                    return True
            except Exception:
                pass

        time.sleep(1.0)
    return False


def update_scheduler_time(job_id: str, schedule_time: str) -> dict[str, Any]:
    schedule = _time_to_schedule(schedule_time)
    payload = {"schedule": schedule, "timeZone": TIME_ZONE}
    resp = requests.patch(
        _url(job_id) + "?updateMask=schedule,timeZone",
        headers=_headers(),
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_job(job_id, resp.json())
