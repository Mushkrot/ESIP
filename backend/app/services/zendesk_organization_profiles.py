"""Zendesk organization profile extraction for store-level analytics."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import requests
from google.cloud import bigquery

from app.config import settings
from app.reference_data.fixtures import BRAND_TAG_MAP as REFERENCE_BRAND_TAG_MAP
from app.services import bigquery as bq_service

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
BIGQUERY_JOB_TIMEOUT_SECONDS = 300
INSERT_CHUNK_SIZE = 500

_ORG_NUMBER_RE = re.compile(r"\b(\d{4})\b")
_KNOWN_POS_TYPES = {
    "simphony": "Simphony",
    "micros": "Micros",
    "micros 5.5": "Micros 5.5",
    "micros 5_5": "Micros 5.5",
    "aloha": "Aloha",
}
_TRUE_VALUES = {"1", "true", "yes", "y", "checked", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "unchecked", "off"}
_BRAND_TAG_MAP = REFERENCE_BRAND_TAG_MAP


@dataclass(frozen=True)
class OrganizationProfileSyncResult:
    dry_run: bool
    fetched_count: int
    prepared_count: int
    written_count: int
    pos_type_distribution: dict[str, int]
    simphony_store_count: int
    target_table: str
    simphony_samples: tuple[dict[str, Any], ...] = ()

    @property
    def write_executed(self) -> bool:
        return not self.dry_run


def _clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_pos_type(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    return _KNOWN_POS_TYPES.get(text.casefold(), text)


def _parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def _parse_timestamp(value: Any) -> str | None:
    text = _clean_string(value)
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, default=str)


def _field(fields: dict[str, Any], name: str) -> Any:
    return fields.get(name)


def _tags(org: dict[str, Any]) -> list[str]:
    return [
        tag
        for tag in (_clean_string(item) for item in (org.get("tags") or []))
        if tag
    ]


def _brand_from_tags(tags: list[str]) -> str | None:
    for tag in tags:
        mapped = _BRAND_TAG_MAP.get(tag)
        if mapped:
            return mapped
    return None


def _store_id_from_org(org: dict[str, Any], fields: dict[str, Any]) -> str | None:
    store_id = _clean_string(_field(fields, "store_id"))
    if store_id:
        return store_id
    match = _ORG_NUMBER_RE.search(str(org.get("name") or ""))
    return match.group(1) if match else None


def _org_status(org: dict[str, Any], fields: dict[str, Any]) -> str:
    status = _clean_string(_field(fields, "status") or org.get("status"))
    if status:
        return status
    return "suspended" if org.get("suspended") else "active"


def build_profile_row(
    org: dict[str, Any],
    *,
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    fields = org.get("organization_fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    tags = _tags(org)
    synced_at = synced_at or datetime.now(timezone.utc)

    row = {
        "org_id": int(org["id"]),
        "org_name": _clean_string(org.get("name")),
        "store_id": _store_id_from_org(org, fields),
        "brand": _clean_string(_field(fields, "brand")) or _brand_from_tags(tags),
        "status": _org_status(org, fields),
        "pos_type": normalize_pos_type(_field(fields, "pos_type")),
        "simphony_store": _parse_bool(_field(fields, "simphony_store")),
        "simphony_install_date": _parse_timestamp(_field(fields, "simphony_install_date")),
        "pos_server_ip": _clean_string(_field(fields, "pos_server_ip")),
        "kds": _parse_bool(_field(fields, "kds")),
        "dmb": _parse_bool(_field(fields, "dmb")),
        "guest_wifi": _parse_bool(_field(fields, "guest_wifi")),
        "phone_type": _clean_string(_field(fields, "phone_type")),
        "internet_connection": _clean_string(_field(fields, "internet_connection")),
        "tags": tags,
        "source_updated_at": _parse_timestamp(org.get("updated_at")),
        "synced_at": synced_at.astimezone(timezone.utc).isoformat(),
        "raw_fields_json": _json_dump(fields),
        "raw_profile_json": _json_dump(org),
    }
    return row


def _session() -> requests.Session:
    session = requests.Session()
    session.auth = settings.zendesk_auth
    session.headers.update({"Content-Type": "application/json"})
    return session


def _zendesk_get(
    path_or_url: str,
    *,
    params: dict[str, Any] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    url = (
        path_or_url
        if path_or_url.startswith("http")
        else f"{settings.zendesk_base_url}{path_or_url}"
    )
    request_session = session or _session()
    for attempt in range(1, MAX_RETRIES + 1):
        response = request_session.get(url, params=params, timeout=30)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", RETRY_BACKOFF_SECONDS))
            logger.warning("Zendesk rate limit while fetching organizations; sleeping %ds", wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Zendesk organization request failed after {MAX_RETRIES} retries: {url}")


def iter_zendesk_organizations(
    *,
    limit: int | None = None,
    page_size: int = PAGE_SIZE,
    session: requests.Session | None = None,
) -> Iterator[dict[str, Any]]:
    page_size = min(max(1, int(page_size)), PAGE_SIZE)
    remaining = None if limit is None else max(0, int(limit))
    url: str | None = "/organizations.json"
    params: dict[str, Any] | None = {"per_page": page_size}
    request_session = session or _session()

    while url and (remaining is None or remaining > 0):
        data = _zendesk_get(url, params=params, session=request_session)
        for org in data.get("organizations") or []:
            if remaining is not None and remaining <= 0:
                break
            yield org
            if remaining is not None:
                remaining -= 1
        url = data.get("next_page")
        params = None


def _dedupe_by_org_id(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[int, dict[str, Any]] = {}
    for row in rows:
        deduped[int(row["org_id"])] = row
    return list(deduped.values())


def _profile_rows(
    organizations: Iterable[dict[str, Any]],
    *,
    limit: int | None = None,
    synced_at: datetime | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    max_items = None if limit is None else max(0, int(limit))
    synced_at = synced_at or datetime.now(timezone.utc)
    for org in organizations:
        if max_items is not None and len(rows) >= max_items:
            break
        rows.append(build_profile_row(org, synced_at=synced_at))
    return _dedupe_by_org_id(rows)


def _chunked(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for idx in range(0, len(rows), size):
        yield rows[idx : idx + size]


def _sample_simphony_rows(rows: list[dict[str, Any]], limit: int = 5) -> tuple[dict[str, Any], ...]:
    samples: list[dict[str, Any]] = []
    for row in rows:
        if row.get("pos_type") != "Simphony" and row.get("simphony_store") is not True:
            continue
        samples.append(
            {
                "org_id": row.get("org_id"),
                "org_name": row.get("org_name"),
                "store_id": row.get("store_id"),
                "brand": row.get("brand"),
                "pos_type": row.get("pos_type"),
            }
        )
        if len(samples) >= limit:
            break
    return tuple(samples)


def upsert_profile_rows(
    client: bigquery.Client,
    rows: list[dict[str, Any]],
) -> int:
    bq_service.ensure_zendesk_organization_profiles_table(client)
    rows = _dedupe_by_org_id(rows)
    if not rows:
        return 0

    target_table = bq_service.zendesk_organization_profiles_table_id()
    org_ids = sorted({int(row["org_id"]) for row in rows})
    delete_job = client.query(
        f"DELETE FROM `{target_table}` WHERE org_id IN UNNEST(@org_ids)",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("org_ids", "INT64", org_ids)
            ]
        ),
    )
    delete_job.result(timeout=BIGQUERY_JOB_TIMEOUT_SECONDS)

    for chunk in _chunked(rows, INSERT_CHUNK_SIZE):
        errors = client.insert_rows_json(target_table, chunk)
        if errors:
            raise RuntimeError(f"Failed to insert organization profile rows: {errors[:2]}")
    return len(rows)


def sync_zendesk_organization_profiles(
    *,
    dry_run: bool = True,
    limit: int | None = None,
    page_size: int = PAGE_SIZE,
    organizations: Iterable[dict[str, Any]] | None = None,
    client: bigquery.Client | None = None,
) -> OrganizationProfileSyncResult:
    source = organizations
    if source is None:
        source = iter_zendesk_organizations(limit=limit, page_size=page_size)
        row_limit = None
    else:
        row_limit = limit

    rows = _profile_rows(source, limit=row_limit)
    distribution = Counter(row.get("pos_type") or "(blank)" for row in rows)
    written_count = 0
    if not dry_run:
        write_client = client or bq_service._get_client()
        written_count = upsert_profile_rows(write_client, rows)

    return OrganizationProfileSyncResult(
        dry_run=dry_run,
        fetched_count=len(rows),
        prepared_count=len(rows),
        written_count=written_count,
        pos_type_distribution=dict(sorted(distribution.items())),
        simphony_store_count=sum(1 for row in rows if row.get("simphony_store") is True),
        target_table=bq_service.zendesk_organization_profiles_table_id(),
        simphony_samples=_sample_simphony_rows(rows),
    )
