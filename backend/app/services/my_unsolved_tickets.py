"""Hybrid My Unsolved Tickets view for the AI Chat sidebar."""

from __future__ import annotations

import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Callable

from google.cloud import bigquery

from app.models.profile import MyUnsolvedTicketsResponse
from app.models.ticket import TicketListItem
from app.services import firestore
from app.services.bigquery import BQ_DATASET, GCP_PROJECT, _get_client
from app.services.zendesk import zendesk

logger = logging.getLogger(__name__)

UNSOLVED_STATUSES = ("new", "open", "pending", "hold")
BIGQUERY_TIMEOUT_SECONDS = 8


def get_my_unsolved_tickets(
    *,
    uid: str,
    email: str,
    limit: int = 20,
    enqueue_refresh: Callable[[str, str, int], None] | None = None,
) -> MyUnsolvedTicketsResponse:
    """
    Return a fast per-user unsolved-ticket list.

    Fresh live cache wins. Otherwise the UI gets a BQ snapshot immediately while
    a bounded Zendesk live refresh is queued in the background when possible.
    """
    safe_email = _normalize_email(email)
    now = _now()
    cache = firestore.get_my_unsolved_tickets_cache(uid)

    if _cache_is_fresh(cache, now):
        return _response_from_cache(cache, refresh_status="fresh")

    refresh_status = _queue_refresh_if_possible(
        uid=uid,
        email=safe_email,
        limit=limit,
        enqueue_refresh=enqueue_refresh,
    )

    try:
        bq_items = query_my_unsolved_tickets_from_bq(safe_email, limit=limit)
        return MyUnsolvedTicketsResponse(
            items=bq_items,
            count=len(bq_items),
            source="bq_snapshot",
            fetched_at=now,
            expires_at=None,
            refresh_status=refresh_status,
            warning=_background_warning(refresh_status),
        )
    except Exception as exc:
        logger.warning("my_unsolved_tickets: BQ fallback failed for %s: %s", safe_email, exc)
        if cache:
            return _response_from_cache(
                cache,
                refresh_status=refresh_status if refresh_status != "fresh" else "failed",
                warning="Showing the last cached Zendesk list because the BigQuery snapshot was unavailable.",
            )
        return MyUnsolvedTicketsResponse(
            items=[],
            count=0,
            source="bq_snapshot",
            fetched_at=now,
            expires_at=None,
            refresh_status="failed",
            warning="Could not load your assigned unsolved tickets right now.",
        )


def refresh_my_unsolved_tickets(
    *,
    uid: str,
    email: str,
    limit: int = 20,
) -> MyUnsolvedTicketsResponse:
    """Force a current-user Zendesk live refresh, with BQ/cache fallback on failure."""
    safe_email = _normalize_email(email)
    try:
        cache = refresh_my_unsolved_tickets_cache(uid, safe_email, limit)
        return _response_from_cache(cache, refresh_status="fresh")
    except Exception as exc:
        logger.warning("my_unsolved_tickets: manual refresh failed for %s: %s", safe_email, exc)
        cache = firestore.get_my_unsolved_tickets_cache(uid)
        try:
            bq_items = query_my_unsolved_tickets_from_bq(safe_email, limit=limit)
            return MyUnsolvedTicketsResponse(
                items=bq_items,
                count=len(bq_items),
                source="bq_snapshot",
                fetched_at=_now(),
                expires_at=None,
                refresh_status="failed",
                warning="Live Zendesk refresh failed; showing the latest BigQuery snapshot.",
            )
        except Exception:
            if cache:
                return _response_from_cache(
                    cache,
                    refresh_status="failed",
                    warning="Live Zendesk refresh failed; showing the last cached Zendesk list.",
                )
            return MyUnsolvedTicketsResponse(
                items=[],
                count=0,
                source="bq_snapshot",
                fetched_at=_now(),
                expires_at=None,
                refresh_status="failed",
                warning="Live Zendesk refresh failed and no cached list is available.",
            )


def refresh_my_unsolved_tickets_cache(uid: str, email: str, limit: int = 20) -> dict:
    """
    Fetch live Zendesk tickets and update Firestore cache.

    Intended for both explicit Refresh and FastAPI BackgroundTasks.
    """
    safe_email = _normalize_email(email)
    try:
        items = zendesk.get_unsolved_tickets_assigned_to_email(safe_email, limit=limit)
        ticket_dicts = [item.model_dump(mode="python") for item in items]
        return firestore.set_my_unsolved_tickets_cache(
            uid,
            email=safe_email,
            tickets=ticket_dicts,
        )
    except Exception as exc:
        firestore.mark_my_unsolved_tickets_refresh_failed(uid, _error_summary(exc))
        raise


def query_my_unsolved_tickets_from_bq(email: str, *, limit: int = 20) -> list[TicketListItem]:
    """Read the current BigQuery ticket snapshot for this user's unsolved tickets."""
    safe_email = _normalize_email(email)
    if not safe_email:
        return []

    client = _get_client()
    table = f"`{GCP_PROJECT}.{BQ_DATASET}.tickets`"
    sql = f"""
    SELECT
      id,
      subject,
      status,
      priority,
      updated_at,
      assignee_name,
      issue_category,
      store_id,
      org_name
    FROM {table}
    WHERE LOWER(TRIM(COALESCE(assignee_email, ''))) = @email
      AND LOWER(TRIM(COALESCE(status, ''))) IN UNNEST(@statuses)
    ORDER BY updated_at DESC
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("email", "STRING", safe_email),
            bigquery.ArrayQueryParameter("statuses", "STRING", list(UNSOLVED_STATUSES)),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ]
    )
    try:
        job = client.query(
            sql,
            job_config=job_config,
            retry=None,
            job_retry=None,
            timeout=BIGQUERY_TIMEOUT_SECONDS,
        )
        rows = job.result(timeout=BIGQUERY_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        raise RuntimeError(f"BigQuery timed out after {BIGQUERY_TIMEOUT_SECONDS}s") from exc

    return [
        TicketListItem(
            id=int(row.id),
            subject=str(row.subject or ""),
            status=str(row.status or ""),
            priority=row.priority,
            updated_at=row.updated_at,
            assignee_name=row.assignee_name,
            issue_category=row.issue_category,
            store_id=row.store_id,
            org_name=row.org_name,
        )
        for row in rows
    ]


def _queue_refresh_if_possible(
    *,
    uid: str,
    email: str,
    limit: int,
    enqueue_refresh: Callable[[str, str, int], None] | None,
) -> str:
    try:
        acquired = firestore.try_acquire_my_unsolved_tickets_refresh_lock(uid)
    except Exception as exc:
        logger.warning("my_unsolved_tickets: could not acquire refresh lock for %s: %s", email, exc)
        return "failed"
    if not acquired:
        return "refreshing"
    if enqueue_refresh:
        enqueue_refresh(uid, email, limit)
        return "queued"
    return "refreshing"


def _response_from_cache(
    cache: dict,
    *,
    refresh_status: str,
    warning: str | None = None,
) -> MyUnsolvedTicketsResponse:
    items = [_ticket_from_cache(item) for item in cache.get("tickets") or []]
    return MyUnsolvedTicketsResponse(
        items=items,
        count=len(items),
        source="zendesk_live_cache",
        fetched_at=_ensure_datetime(cache.get("fetched_at")),
        expires_at=_ensure_datetime(cache.get("expires_at")),
        refresh_status=refresh_status,  # type: ignore[arg-type]
        warning=warning,
    )


def _ticket_from_cache(data: dict) -> TicketListItem:
    return TicketListItem(
        id=int(data.get("id") or 0),
        subject=str(data.get("subject") or ""),
        status=str(data.get("status") or ""),
        priority=data.get("priority"),
        updated_at=_ensure_datetime(data.get("updated_at")) or _now(),
        assignee_name=data.get("assignee_name"),
        issue_category=data.get("issue_category"),
        store_id=data.get("store_id"),
        org_name=data.get("org_name"),
    )


def _cache_is_fresh(cache: dict | None, now: datetime) -> bool:
    if not cache:
        return False
    expires_at = _ensure_datetime(cache.get("expires_at"))
    return bool(expires_at and expires_at > now)


def _background_warning(refresh_status: str) -> str | None:
    if refresh_status == "queued":
        return "Refreshing your live Zendesk list in the background."
    if refresh_status == "refreshing":
        return "A live Zendesk refresh is already in progress."
    if refresh_status == "failed":
        return "Could not start a live Zendesk refresh; showing the BigQuery snapshot."
    return None


def _ensure_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_summary(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
