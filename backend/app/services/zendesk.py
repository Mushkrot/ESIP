"""
Zendesk REST API client.

All calls use the backend service account (email/token Basic Auth).
Engineers' personal Zendesk credentials are never used.

Session cache (in-process dict):
  - user_cache[user_id]  → {name, email}
  - org_cache[org_id]    → {name, store_id, brand}

This eliminates N+1 API calls when resolving author names across a ticket's comments.
Cache lives for the lifetime of the Cloud Run instance (min-instances=1).
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from app.config import settings
from app.reference_data.fixtures import BRAND_TAG_MAP as REFERENCE_BRAND_TAG_MAP
from app.models.ticket import (
    TicketComment,
    TicketDetail,
    TicketListItem,
    StoreContext,
)
from app.services.sla import build_sla_fields
from app.services import error_provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zendesk custom field IDs
# ---------------------------------------------------------------------------
FIELD_STORE_ID = 2017
FIELD_ISSUE_CATEGORY = 930100000009
FIELD_SYMPTOMS = 930100000011
FIELD_WORKAROUND = 930100000012
FIELD_NEXT_STEPS = 930100000013
FIELD_RESOLUTION = 930100000008
FIELD_ESCALATED = 930100000006
FIELD_NEXT_FOLLOW_UP = 930100000007
FIELD_REQUESTER_NAME = 91000001

# Brand detection: Zendesk org tags → human-readable brand name
BRAND_TAG_MAP = REFERENCE_BRAND_TAG_MAP

_ORG_NUMBER_RE = re.compile(r"\b(\d{4})\b")


class ZendeskClient:
    """
    Stateful Zendesk client with in-process session caches for user/org lookups.
    One instance lives for the lifetime of the Cloud Run process.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.auth = settings.zendesk_auth
        self._session.headers.update({"Content-Type": "application/json"})
        self._base = settings.zendesk_base_url

        # In-process session caches (N+1 prevention)
        self._user_cache: dict[int, dict] = {}   # user_id → {name, email}
        self._org_cache: dict[int, dict] = {}    # org_id  → {name, store_id, brand}
        self._form_cache: dict[int, str | None] = {}

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self._base}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            error_provenance.record_exception_best_effort(
                exc,
                endpoint_class=error_provenance.zendesk_endpoint_class(path),
                source_hint="zendesk",
            )
            raise

    def _resolve_user(self, user_id: Optional[int]) -> Optional[str]:
        """Return display name for a Zendesk user ID. Cached."""
        if user_id is None:
            return None
        if user_id not in self._user_cache:
            try:
                data = self._get(f"/users/{user_id}.json")
                self._user_cache[user_id] = {
                    "name": data["user"]["name"],
                    "email": data["user"].get("email", ""),
                }
            except Exception:
                logger.warning("Failed to resolve user %s", user_id)
                self._user_cache[user_id] = {"name": f"User #{user_id}", "email": ""}
        return self._user_cache[user_id]["name"]

    def _resolve_org(self, org_id: Optional[int]) -> Optional[dict]:
        """Return {name, store_id, brand} for a Zendesk org ID. Cached."""
        if org_id is None:
            return None
        if org_id not in self._org_cache:
            try:
                data = self._get(f"/organizations/{org_id}.json")
                org = data["organization"]
                name: str = org.get("name", "")
                tags: list[str] = org.get("tags", [])

                # Store ID: from org custom fields (same field as ticket)
                org_fields: list[dict] = org.get("organization_fields", {})
                store_id: Optional[str] = None

                # Try ticket-style custom_fields if present on org
                for f in org.get("custom_fields", {}).values() if isinstance(org.get("custom_fields"), dict) else []:
                    pass  # org fields use different structure; rely on org name below

                # Parse 4-digit number from org name as fallback
                m = _ORG_NUMBER_RE.search(name)
                if m:
                    store_id = m.group(1)

                # Brand from tags
                brand = "Unknown"
                for tag in tags:
                    if tag in BRAND_TAG_MAP:
                        brand = BRAND_TAG_MAP[tag]
                        break
                # Fallback: extract brand from org name (everything before the number)
                if brand == "Unknown" and m:
                    brand = name[: m.start()].strip()

                self._org_cache[org_id] = {"name": name, "store_id": store_id, "brand": brand}
            except Exception:
                logger.warning("Failed to resolve org %s", org_id)
                self._org_cache[org_id] = {"name": "", "store_id": None, "brand": "Unknown"}

        return self._org_cache[org_id]

    def _resolve_ticket_form_name(self, form_id: Optional[int]) -> Optional[str]:
        """Return Zendesk ticket form display name. Cached."""
        if form_id is None:
            return None
        if form_id not in self._form_cache:
            try:
                data = self._get(f"/ticket_forms/{form_id}.json")
                form = data.get("ticket_form") or {}
                self._form_cache[form_id] = form.get("display_name") or form.get("name")
            except Exception:
                logger.warning("Failed to resolve ticket form %s", form_id)
                self._form_cache[form_id] = None
        return self._form_cache[form_id]

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get_ticket(self, ticket_id: int) -> TicketDetail:
        """
        Fetch a single ticket with all comments, enriched with user names and org context.
        Applies tiered compression if comments are unusually large (>300 comments).
        """
        ticket_data = self._get(f"/tickets/{ticket_id}.json")["ticket"]
        comments_data = self._get(f"/tickets/{ticket_id}/comments.json")["comments"]
        ticket_form_name = self._resolve_ticket_form_name(ticket_data.get("ticket_form_id"))
        sla_fields = build_sla_fields(ticket_data, ticket_form_name=ticket_form_name)

        # Extract custom fields into a lookup dict
        fields: dict[int, object] = {
            f["id"]: f["value"]
            for f in ticket_data.get("custom_fields", [])
        }

        # Store ID comes from the ticket custom field (authoritative source)
        raw_store_id = fields.get(FIELD_STORE_ID)
        store_id = str(raw_store_id) if raw_store_id else None

        # Resolve org context
        org_context = self._resolve_org(ticket_data.get("organization_id"))
        store_context: Optional[StoreContext] = None
        if org_context and (store_id or org_context.get("store_id")):
            store_context = StoreContext(
                store_id=store_id or org_context["store_id"] or "",
                org_name=org_context["name"],
                brand=org_context["brand"],
                org_id=ticket_data.get("organization_id", 0),
            )

        # Build comments list
        comments: list[TicketComment] = []
        for c in comments_data:
            author_name = self._resolve_user(c.get("author_id")) or "Unknown"
            comments.append(TicketComment(
                id=c["id"],
                author_name=author_name,
                body=c.get("body") or c.get("html_body", ""),
                is_internal=not c.get("public", True),
                created_at=datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")),
            ))

        return TicketDetail(
            id=ticket_data["id"],
            subject=ticket_data.get("subject", ""),
            status=ticket_data.get("status", ""),
            priority=ticket_data.get("priority"),
            ticket_type=sla_fields["ticket_type"],
            ticket_form_id=sla_fields["ticket_form_id"],
            ticket_form_name=sla_fields["ticket_form_name"],
            created_at=datetime.fromisoformat(ticket_data["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(ticket_data["updated_at"].replace("Z", "+00:00")),
            sla_policy_name=sla_fields["sla_policy_name"],
            resolution_sla_target_hours=sla_fields["resolution_sla_target_hours"],
            resolution_sla_elapsed_hours=sla_fields["resolution_sla_elapsed_hours"],
            resolution_sla_breached=sla_fields["resolution_sla_breached"],
            resolution_sla_status=sla_fields["resolution_sla_status"],
            assignee_name=self._resolve_user(ticket_data.get("assignee_id")),
            issue_category=fields.get(FIELD_ISSUE_CATEGORY),  # type: ignore
            symptoms=fields.get(FIELD_SYMPTOMS),  # type: ignore
            workaround=fields.get(FIELD_WORKAROUND),  # type: ignore
            next_steps=fields.get(FIELD_NEXT_STEPS),  # type: ignore
            resolution=fields.get(FIELD_RESOLUTION),  # type: ignore
            is_escalated=bool(fields.get(FIELD_ESCALATED, False)),
            next_follow_up=str(fields.get(FIELD_NEXT_FOLLOW_UP)) if fields.get(FIELD_NEXT_FOLLOW_UP) else None,
            requester_name=fields.get(FIELD_REQUESTER_NAME),  # type: ignore
            store=store_context,
            comments=sorted(comments, key=lambda c: c.created_at),
        )

    def get_tickets_by_store(
        self,
        store_number: str,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[TicketListItem], int, Optional[str]]:
        """
        Return paginated tickets for a store identified by store_number.

        Strategy (in order):
        1. Search orgs where custom field 2017 = store_number (authoritative)
        2. Search orgs where name contains the 4-digit number (fallback)

        Returns (tickets, total_count, org_name).
        """
        org_id = self._find_org_by_store_number(store_number)
        if org_id is None:
            return [], 0, None

        org_context = self._resolve_org(org_id)
        org_name = org_context["name"] if org_context else None

        # Zendesk search: tickets by org
        search_resp = self._get(
            "/search.json",
            params={
                "query": f"type:ticket organization_id:{org_id}",
                "sort_by": "updated_at",
                "sort_order": "desc",
                "per_page": per_page,
                "page": page,
            },
        )

        total = search_resp.get("count", 0)
        items: list[TicketListItem] = []
        for t in search_resp.get("results", []):
            fields = {f["id"]: f["value"] for f in t.get("custom_fields", [])}
            items.append(TicketListItem(
                id=t["id"],
                subject=t.get("subject", ""),
                status=t.get("status", ""),
                priority=t.get("priority"),
                updated_at=datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00")),
                assignee_name=self._resolve_user(t.get("assignee_id")),
                issue_category=fields.get(FIELD_ISSUE_CATEGORY),  # type: ignore
                store_id=str(fields.get(FIELD_STORE_ID)) if fields.get(FIELD_STORE_ID) else store_number,
                org_name=org_name,
            ))

        return items, total, org_name

    def get_store_history(
        self,
        org_id: int,
        exclude_ticket_id: int,
        limit: int = 20,
    ) -> list[TicketListItem]:
        """
        Return the most recent closed/solved tickets for a store (for context panel).
        Excludes the currently open ticket.
        """
        search_resp = self._get(
            "/search.json",
            params={
                "query": f"type:ticket organization_id:{org_id} status:solved status:closed",
                "sort_by": "updated_at",
                "sort_order": "desc",
                "per_page": limit + 1,  # fetch one extra to account for exclusion
                "page": 1,
            },
        )

        items: list[TicketListItem] = []
        for t in search_resp.get("results", []):
            if t["id"] == exclude_ticket_id:
                continue
            if len(items) >= limit:
                break
            fields = {f["id"]: f["value"] for f in t.get("custom_fields", [])}
            org_context = self._resolve_org(t.get("organization_id"))
            items.append(TicketListItem(
                id=t["id"],
                subject=t.get("subject", ""),
                status=t.get("status", ""),
                priority=t.get("priority"),
                updated_at=datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00")),
                assignee_name=self._resolve_user(t.get("assignee_id")),
                issue_category=fields.get(FIELD_ISSUE_CATEGORY),  # type: ignore
                store_id=str(fields.get(FIELD_STORE_ID)) if fields.get(FIELD_STORE_ID) else None,
                org_name=org_context["name"] if org_context else None,
            ))

        return items

    def search_all_tickets(
        self,
        query: str,
        days: Optional[int] = None,
        limit: int = 100,
    ) -> list[TicketListItem]:
        """
        Full-text ticket search across all stores via Zendesk Search API.
        Optionally restricted to the last `days` days.
        Returns up to `limit` results sorted by created_at desc.
        Uses sideloading to fetch all org data in one request (avoids N+1 calls).
        """
        search_query = f"type:ticket {query}"
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            search_query += f" created>{since}"

        items: list[TicketListItem] = []
        page = 1
        per_page = min(limit, 100)  # Zendesk search max is 100 per page

        while len(items) < limit:
            search_resp = self._get(
                "/search.json",
                params={
                    "query": search_query,
                    "sort_by": "created_at",
                    "sort_order": "desc",
                    "per_page": per_page,
                    "page": page,
                    "include": "organizations",  # sideload orgs to avoid N+1 API calls
                },
            )

            # Pre-populate org cache from sideloaded data
            for org in search_resp.get("organizations", []):
                org_id = org.get("id")
                if org_id and org_id not in self._org_cache:
                    name = org.get("name", "")
                    tags = org.get("tags", [])
                    m = _ORG_NUMBER_RE.search(name)
                    store_id = m.group(1) if m else None
                    brand = "Unknown"
                    for tag in tags:
                        if tag in BRAND_TAG_MAP:
                            brand = BRAND_TAG_MAP[tag]
                            break
                    if brand == "Unknown" and m:
                        brand = name[: m.start()].strip()
                    self._org_cache[org_id] = {"name": name, "store_id": store_id, "brand": brand}

            results = search_resp.get("results", [])
            if not results:
                break

            for t in results:
                if len(items) >= limit:
                    break
                fields = {f["id"]: f["value"] for f in t.get("custom_fields", [])}
                org_context = self._resolve_org(t.get("organization_id"))
                items.append(TicketListItem(
                    id=t["id"],
                    subject=t.get("subject", ""),
                    status=t.get("status", ""),
                    priority=t.get("priority"),
                    updated_at=datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00")),
                    assignee_name=self._resolve_user(t.get("assignee_id")),
                    issue_category=fields.get(FIELD_ISSUE_CATEGORY),  # type: ignore
                    store_id=str(fields.get(FIELD_STORE_ID)) if fields.get(FIELD_STORE_ID) else None,
                    org_name=org_context["name"] if org_context else None,
                ))

            # Stop if there are no more pages
            if not search_resp.get("next_page") or len(results) < per_page:
                break
            page += 1

        return items

    def get_unsolved_tickets_assigned_to_email(
        self,
        email: str,
        limit: int = 20,
    ) -> list[TicketListItem]:
        """
        Return the current Zendesk unsolved tickets assigned to one engineer.

        This is the live-refresh path for the ESIP "My Unsolved Tickets" sidebar
        view. It is read-only and intentionally bounded.
        """
        safe_email = (email or "").strip().lower().replace('"', "")
        if not safe_email:
            return []

        per_page = min(max(limit, 1), 100)
        search_resp = self._get(
            "/search.json",
            params={
                "query": (
                    f'type:ticket assignee:"{safe_email}" '
                    "status:new status:open status:pending status:hold"
                ),
                "sort_by": "updated_at",
                "sort_order": "desc",
                "per_page": per_page,
                "page": 1,
                "include": "organizations",
            },
        )

        for org in search_resp.get("organizations", []):
            org_id = org.get("id")
            if org_id and org_id not in self._org_cache:
                name = org.get("name", "")
                tags = org.get("tags", [])
                m = _ORG_NUMBER_RE.search(name)
                store_id = m.group(1) if m else None
                brand = "Unknown"
                for tag in tags:
                    if tag in BRAND_TAG_MAP:
                        brand = BRAND_TAG_MAP[tag]
                        break
                if brand == "Unknown" and m:
                    brand = name[: m.start()].strip()
                self._org_cache[org_id] = {"name": name, "store_id": store_id, "brand": brand}

        items: list[TicketListItem] = []
        for t in search_resp.get("results", []):
            status = str(t.get("status") or "").lower()
            if status not in {"new", "open", "pending", "hold"}:
                continue
            fields = {f["id"]: f["value"] for f in t.get("custom_fields", [])}
            org_context = self._resolve_org(t.get("organization_id"))
            items.append(TicketListItem(
                id=t["id"],
                subject=t.get("subject", ""),
                status=status,
                priority=t.get("priority"),
                updated_at=datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00")),
                assignee_name=self._resolve_user(t.get("assignee_id")),
                issue_category=fields.get(FIELD_ISSUE_CATEGORY),  # type: ignore
                store_id=str(fields.get(FIELD_STORE_ID)) if fields.get(FIELD_STORE_ID) else None,
                org_name=org_context["name"] if org_context else None,
            ))
            if len(items) >= limit:
                break

        return items

    def search_tickets_created_between(
        self,
        start_at: datetime,
        end_at: datetime,
        limit: int = 300,
    ) -> list[dict]:
        """
        Return live Zendesk tickets created within [start_at, end_at).

        This is intentionally bounded for same-day / near-real-time Universal Chat
        analytics overlays. It is not a replacement for BigQuery trend analytics.
        Zendesk Search date filters are coarse, so results are filtered exactly in
        Python after retrieval.
        """
        return self._search_tickets_between("created", start_at, end_at, limit)

    def search_tickets_updated_between(
        self,
        start_at: datetime,
        end_at: datetime,
        limit: int = 300,
    ) -> list[dict]:
        """
        Return live Zendesk tickets updated within [start_at, end_at).

        This mirrors the created-date helper but uses Zendesk updated filters
        and exact Python filtering on updated_at after retrieval.
        """
        return self._search_tickets_between("updated", start_at, end_at, limit)

    def search_store_tickets_created_between(
        self,
        store_number: str,
        start_at: datetime,
        end_at: datetime,
        limit: int = 300,
    ) -> list[dict]:
        """
        Return live Zendesk tickets for one store created within [start_at, end_at).

        This is a store-scoped historical fallback for Universal Chat when BQ
        cannot provide the requested same-store date slice.
        """
        org_id = self._find_org_by_store_number(str(store_number))
        if org_id is None:
            return []
        return self._search_tickets_between(
            "created",
            start_at,
            end_at,
            limit,
            organization_id=org_id,
        )

    def _search_tickets_between(
        self,
        date_field: str,
        start_at: datetime,
        end_at: datetime,
        limit: int = 300,
        organization_id: int | None = None,
    ) -> list[dict]:
        if date_field not in {"created", "updated"}:
            raise ValueError("date_field must be created or updated")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("start_at and end_at must be timezone-aware")
        if end_at <= start_at:
            return []

        cap = max(1, min(int(limit), 500))
        per_page = min(cap, 100)
        # Search one calendar day earlier and filter exactly below. This avoids
        # losing Eastern early-morning tickets if Zendesk interprets date terms
        # in UTC.
        query_start = (start_at.astimezone(timezone.utc) - timedelta(days=1)).date().isoformat()
        query_end = (end_at.astimezone(timezone.utc) + timedelta(days=1)).date().isoformat()
        org_filter = f" organization_id:{int(organization_id)}" if organization_id else ""
        search_query = f"type:ticket{org_filter} {date_field}>{query_start} {date_field}<{query_end}"

        items: list[dict] = []
        page = 1
        while len(items) < cap:
            search_resp = self._get(
                "/search.json",
                params={
                    "query": search_query,
                    "sort_by": f"{date_field}_at",
                    "sort_order": "desc",
                    "per_page": per_page,
                    "page": page,
                    "include": "organizations",
                },
            )

            for org in search_resp.get("organizations", []):
                org_id = org.get("id")
                if org_id and org_id not in self._org_cache:
                    name = org.get("name", "")
                    tags = org.get("tags", [])
                    m = _ORG_NUMBER_RE.search(name)
                    store_id = m.group(1) if m else None
                    brand = "Unknown"
                    for tag in tags:
                        if tag in BRAND_TAG_MAP:
                            brand = BRAND_TAG_MAP[tag]
                            break
                    if brand == "Unknown" and m:
                        brand = name[: m.start()].strip()
                    self._org_cache[org_id] = {"name": name, "store_id": store_id, "brand": brand}

            results = search_resp.get("results", [])
            if not results:
                break

            oldest_seen: datetime | None = None
            for t in results:
                created_at = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                updated_at = datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00"))
                selected_at = updated_at if date_field == "updated" else created_at
                oldest_seen = selected_at if oldest_seen is None or selected_at < oldest_seen else oldest_seen
                if not (start_at <= selected_at < end_at):
                    continue
                if len(items) >= cap:
                    break
                fields = {f["id"]: f["value"] for f in t.get("custom_fields", [])}
                org_context = self._resolve_org(t.get("organization_id"))
                items.append({
                    "id": t["id"],
                    "subject": t.get("subject", ""),
                    "status": t.get("status", ""),
                    "priority": t.get("priority"),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "assignee_name": self._resolve_user(t.get("assignee_id")),
                    "issue_category": fields.get(FIELD_ISSUE_CATEGORY),
                    "store_id": str(fields.get(FIELD_STORE_ID)) if fields.get(FIELD_STORE_ID) else (
                        org_context.get("store_id") if org_context else None
                    ),
                    "org_name": org_context["name"] if org_context else None,
                    "brand": org_context["brand"] if org_context else "Unknown",
                })

            if len(items) >= cap or not search_resp.get("next_page") or len(results) < per_page:
                break
            if oldest_seen and oldest_seen < start_at:
                break
            page += 1

        return items

    # -----------------------------------------------------------------------
    # Private: org lookup
    # -----------------------------------------------------------------------

    def _find_org_by_store_number(self, store_number: str) -> Optional[int]:
        """
        Locate the Zendesk organization for a given store number.
        Step 1: search by custom field 2017 (authoritative).
        Step 2: search by org name containing the number (fallback).
        """
        # Step 1: search by org custom field (Zendesk org search by external_id or custom field)
        # Zendesk search syntax for org: type:organization custom_field_<id>:<value>
        try:
            resp = self._get(
                "/search.json",
                params={"query": f"type:organization custom_field_{FIELD_STORE_ID}:{store_number}"},
            )
            results = resp.get("results", [])
            if results:
                return results[0]["id"]
        except Exception as e:
            logger.warning("Store ID field search failed: %s", e)

        # Step 2: search by name containing the store number
        try:
            resp = self._get(
                "/search.json",
                params={"query": f"type:organization {store_number}"},
            )
            for org in resp.get("results", []):
                if _ORG_NUMBER_RE.search(org.get("name", "")) and store_number in org.get("name", ""):
                    return org["id"]
        except Exception as e:
            logger.warning("Org name search failed: %s", e)

        return None


# Module-level singleton — one client per Cloud Run instance
zendesk = ZendeskClient()
