"""
Ticket endpoints.

GET /api/tickets/{ticket_id}
    — Fetch a single ticket with all comments and store context.

GET /api/tickets/by-store/{store_number}?page=1&per_page=20
    — Fetch paginated tickets for a store by its 4-digit store number.

GET /api/tickets/{ticket_id}/history?refresh=false
    — Fetch recent solved/closed tickets from the same store (for context panel).
      Results are cached in Firestore for 2 hours per organisation.
      Pass ?refresh=true to bypass cache and re-fetch from Zendesk.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status

from app.config import settings
from app.dependencies import AnyUser, CurrentUser
from app.models.customer_risk import CustomerRiskResponse
from app.models.ticket import TicketDetail, TicketListResponse
from app.services import error_provenance, firestore, reference_runtime
from app.services.bigquery import _BRAND_DISPLAY, _slug_to_title
from app.services.ticket_customer_risk import get_customer_risk
from app.services.zendesk import zendesk

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: int, user: CurrentUser):
    """Fetch a single ticket, enriched with user names and org context."""
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        ticket = reference_runtime.ticket_detail(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket #{ticket_id} was not found.")
        return ticket
    try:
        return zendesk.get_ticket(ticket_id)
    except Exception as exc:
        _handle_zendesk_error(exc, f"ticket #{ticket_id}")


@router.get("/{ticket_id}/customer-risk", response_model=CustomerRiskResponse)
def get_ticket_customer_risk(
    ticket_id: int,
    user: AnyUser,
    refresh: bool = Query(default=False),
):
    """Return color-coded customer frustration risk for a ticket."""
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        risk = reference_runtime.customer_risk(ticket_id)
        if risk is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket #{ticket_id} was not found.")
        return risk
    try:
        return get_customer_risk(ticket_id, refresh=refresh)
    except Exception as exc:
        _handle_zendesk_error(exc, f"customer risk for ticket #{ticket_id}")


@router.get("/by-store/{store_number}", response_model=TicketListResponse)
def get_tickets_by_store(
    store_number: str,
    user: CurrentUser,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
):
    """Return paginated tickets for a store identified by its 4-digit store number."""
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        tickets, total, org_name = reference_runtime.tickets_by_store(store_number, page, per_page)
        if total == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No store found with store number {store_number}.",
            )
        return TicketListResponse(
            tickets=tickets,
            total=total,
            page=page,
            per_page=per_page,
            store_id=store_number,
            org_name=org_name,
        )
    try:
        tickets, total, org_name = zendesk.get_tickets_by_store(store_number, page, per_page)
    except Exception as exc:
        _handle_zendesk_error(exc, f"store #{store_number}")

    if total == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No store found with store number {store_number}.",
        )

    return TicketListResponse(
        tickets=tickets,
        total=total,
        page=page,
        per_page=per_page,
        store_id=store_number,
        org_name=org_name,
    )


@router.get("/{ticket_id}/history", response_model=list)
def get_store_history(
    ticket_id: int,
    user: CurrentUser,
    refresh: bool = Query(default=False),
):
    """
    Return the last 20 solved/closed tickets for the same store as the given ticket.
    Results are cached in Firestore for 2 hours (keyed by organisation ID, shared across
    all tickets from the same store).
    Pass ?refresh=true to bypass cache and re-fetch live data from Zendesk.
    """
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        if reference_runtime.ticket_detail(ticket_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket #{ticket_id} was not found.")
        return reference_runtime.ticket_history(ticket_id)

    try:
        ticket = zendesk.get_ticket(ticket_id)
    except Exception as exc:
        _handle_zendesk_error(exc, f"ticket #{ticket_id}")

    if not ticket.store:
        return []

    org_id = ticket.store.org_id

    # Invalidate cache if engineer explicitly requested a refresh
    if refresh:
        firestore.invalidate_history_cache(org_id)

    # Check Firestore cache (shared across all engineers for this store)
    cached = firestore.get_cached_history(org_id)
    if cached is not None:
        # Exclude the currently viewed ticket from the cached list
        return [t for t in cached if t.get("id") != ticket_id]

    # Cache miss — fetch live from Zendesk (no exclusion so the full list can be cached
    # and reused by any other ticket from the same store)
    try:
        items_all = zendesk.get_store_history(
            org_id=org_id,
            exclude_ticket_id=-1,  # -1 never matches a real ticket ID → no exclusion
            limit=20,
        )
    except Exception as exc:
        _handle_zendesk_error(exc, f"history for ticket #{ticket_id}")

    firestore.set_cached_history(org_id, [t.model_dump(mode="json") for t in items_all])

    # Return filtered: exclude the ticket currently being viewed
    return [t for t in items_all if t.id != ticket_id]


@router.get("/{ticket_id}/similar")
def get_similar_tickets(
    ticket_id: int,
    user: AnyUser,
    limit: int = Query(default=5, ge=1, le=20),
):
    """
    Return semantically similar resolved tickets for the given ticket.
    Used by the ZAF sidebar app. Accessible with both Firebase and ZAF session tokens.
    Search scope is GLOBAL (all stores), with diversity re-ranking so
    results are not dominated by a single store/organization.
    """
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        if reference_runtime.ticket_detail(ticket_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket #{ticket_id} was not found.")
        return {"tickets": reference_runtime.similar_tickets(ticket_id, limit)}

    from app.services import vertex_search

    try:
        ticket = zendesk.get_ticket(ticket_id)
    except Exception as exc:
        _handle_zendesk_error(exc, f"ticket #{ticket_id}")

    query = _build_similar_query(ticket)
    # Pull a larger candidate pool, then diversify and trim to requested limit.
    candidate_pool = min(max(limit * 8, 40), 200)

    try:
        vertex_results = vertex_search.search_tickets(query, page_size=candidate_pool)
        candidate_ids: list[int] = []
        seen_ids: set[int] = set()
        for item in vertex_results:
            raw_id = item.get("ticket_id")
            if raw_id is None:
                continue
            try:
                tid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if tid == ticket_id or tid in seen_ids:
                continue
            seen_ids.add(tid)
            candidate_ids.append(tid)
            if len(candidate_ids) >= candidate_pool:
                break

        candidates = _fetch_ticket_details_from_bq(candidate_ids)
        results = _diversify_ticket_results(candidates, limit=limit, max_per_org=2)
    except Exception as exc:
        logger.warning("similar tickets search failed for #%s: %s", ticket_id, exc)
        results = []

    # Normalize payload for UI consumers (Zendesk app expects `id`).
    # Some search backends return `ticket_id`; keep both for compatibility.
    normalized: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id", item.get("ticket_id"))
        try:
            ticket_id_int = int(raw_id)
        except (TypeError, ValueError):
            continue
        row = dict(item)
        row["id"] = ticket_id_int
        row["ticket_id"] = ticket_id_int
        normalized.append(row)

    return {"tickets": normalized}


# ---------------------------------------------------------------------------
# Similar-ticket helpers (global + diversity)
# ---------------------------------------------------------------------------

def _build_similar_query(ticket: TicketDetail) -> str:
    """
    Build a semantic query from ticket fields.
    Keep it concise so vector search focuses on issue semantics.
    """
    parts: list[str] = []
    if ticket.subject:
        parts.append(ticket.subject.strip())
    if ticket.issue_category:
        parts.append(ticket.issue_category.replace("_", " ").strip())
    if ticket.symptoms:
        parts.append(ticket.symptoms.strip()[:200])
    return " | ".join(p for p in parts if p) or "similar service desk issue"


def _fetch_ticket_details_from_bq(ticket_ids: list[int]) -> list[dict]:
    """
    Fetch ticket metadata for candidate IDs from BigQuery.
    Preserves ticket_ids order (Vertex relevance rank).
    """
    if not ticket_ids:
        return []

    from google.cloud import bigquery as bq
    from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET

    client = _get_client()
    sql = f"""
    SELECT
      id,
      subject,
      org_name,
      org_id,
      brand,
      store_id,
      issue_category,
      resolution,
      status,
      solved_at
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id IN UNNEST(@ticket_ids)
      AND status IN ('solved', 'closed')
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ArrayQueryParameter("ticket_ids", "INT64", ticket_ids)]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    row_map = {int(row.id): row for row in rows}

    results: list[dict] = []
    for tid in ticket_ids:
        row = row_map.get(tid)
        if row is None:
            continue
        solved_at = row.solved_at
        if isinstance(solved_at, datetime):
            solved_str = solved_at.strftime("%Y-%m-%d")
        else:
            solved_str = str(solved_at) if solved_at else None
        results.append(
            {
                "ticket_id": int(row.id),
                "subject": row.subject or "",
                "org_name": row.org_name or "",
                "org_id": row.org_id,
                "brand": _BRAND_DISPLAY.get(row.brand or "", row.brand or ""),
                "store_id": row.store_id or "",
                "issue_category": _slug_to_title(row.issue_category or ""),
                "resolution": (row.resolution or "")[:300],
                "status": row.status or "",
                "solved_at": solved_str,
            }
        )
    return results


def _org_bucket_key(ticket: dict) -> str:
    """Stable org/store bucket used for diversity balancing."""
    org_id = ticket.get("org_id")
    if org_id is not None:
        return f"org:{org_id}"
    store_id = str(ticket.get("store_id", "") or "").strip()
    if store_id:
        return f"store:{store_id}"
    org_name = str(ticket.get("org_name", "") or "").strip().lower()
    if org_name:
        return f"name:{org_name}"
    return f"ticket:{ticket.get('ticket_id')}"


def _diversify_ticket_results(candidates: list[dict], limit: int, max_per_org: int = 2) -> list[dict]:
    """
    Diversity-aware trim:
    1) First pass: keep top-ranked unique org/store tickets.
    2) Second pass: fill remaining slots with cap max_per_org per org/store.
    3) Final pass: if still short, fill by rank without cap.
    """
    if limit <= 0 or not candidates:
        return []

    selected: list[dict] = []
    selected_ids: set[int] = set()
    counts: dict[str, int] = {}
    seen_buckets: set[str] = set()

    def _add(ticket: dict) -> bool:
        tid = int(ticket.get("ticket_id", 0) or 0)
        if tid <= 0 or tid in selected_ids:
            return False
        selected.append(ticket)
        selected_ids.add(tid)
        bucket = _org_bucket_key(ticket)
        counts[bucket] = counts.get(bucket, 0) + 1
        return True

    # Pass 1: maximize store/organization coverage first.
    for ticket in candidates:
        bucket = _org_bucket_key(ticket)
        if bucket in seen_buckets:
            continue
        if _add(ticket):
            seen_buckets.add(bucket)
        if len(selected) >= limit:
            break

    # Pass 2: fill remaining with a per-org cap.
    if len(selected) < limit:
        for ticket in candidates:
            if int(ticket.get("ticket_id", 0) or 0) in selected_ids:
                continue
            bucket = _org_bucket_key(ticket)
            if counts.get(bucket, 0) >= max_per_org:
                continue
            _add(ticket)
            if len(selected) >= limit:
                break

    # Pass 3: if pool is narrow, allow more from same org to satisfy limit.
    if len(selected) < limit:
        for ticket in candidates:
            if _add(ticket) and len(selected) >= limit:
                break

    return selected[:limit]


# ---------------------------------------------------------------------------
# Error handling helper
# ---------------------------------------------------------------------------

def _handle_zendesk_error(exc: Exception, context: str) -> None:
    import requests as req_lib
    context_lower = context.lower()
    if "customer risk" in context_lower:
        endpoint_class = "ticket_customer_risk"
    elif "history" in context_lower:
        endpoint_class = "ticket_history"
    elif "store" in context_lower:
        endpoint_class = "ticket_store"
    else:
        endpoint_class = "ticket_detail"
    event = error_provenance.record_exception_best_effort(
        exc,
        endpoint_class=endpoint_class,
        source_hint="zendesk",
    )
    logger.warning(
        "ticket_endpoint_failure source=%s outcome=%s endpoint_class=%s status_code=%s reason_code=%s",
        event["source"],
        event["outcome"],
        event["endpoint_class"],
        event["status_code"],
        event["reason_code"],
    )
    if isinstance(exc, req_lib.HTTPError):
        code = exc.response.status_code if exc.response is not None else 500
        # Treat 404 (not found), 403 (no access), 410 (deleted) as "not found" for the user
        if code in (404, 403, 410):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Ticket not found or not accessible: {context}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Zendesk API error for {context}")
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
