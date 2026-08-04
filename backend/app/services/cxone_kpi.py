"""Read-only NICE CXone ingestion for Service Desk phone KPI reporting."""

from __future__ import annotations

import logging
import os
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery

from app.config import settings

logger = logging.getLogger(__name__)

DATASET = "esip_reference"
API_VERSION = "v34.0"
PAGE_SIZE = 10000
BIGQUERY_JOB_TIMEOUT_SECONDS = float(os.environ.get("CXONE_KPI_BQ_TIMEOUT_SECONDS", "30"))
BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
FRESH_CACHE_READY_HOUR_LOCAL = int(os.environ.get("CXONE_KPI_FRESH_CACHE_READY_HOUR_LOCAL", "8"))

CONTACTS_TABLE = f"{settings.google_cloud_project_id}.{DATASET}.cxone_completed_contacts"
AGENTS_TABLE = f"{settings.google_cloud_project_id}.{DATASET}.cxone_agents"
RUNS_TABLE = f"{settings.google_cloud_project_id}.{DATASET}.cxone_kpi_ingest_runs"


CONTACTS_SCHEMA = [
    bigquery.SchemaField("report_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("contact_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("master_contact_id", "INT64"),
    bigquery.SchemaField("contact_start_at", "TIMESTAMP"),
    bigquery.SchemaField("last_update_at", "TIMESTAMP"),
    bigquery.SchemaField("media_type_id", "INT64"),
    bigquery.SchemaField("media_type_name", "STRING"),
    bigquery.SchemaField("is_outbound", "BOOL"),
    bigquery.SchemaField("from_address", "STRING"),
    bigquery.SchemaField("to_address", "STRING"),
    bigquery.SchemaField("agent_id", "INT64"),
    bigquery.SchemaField("agent_email", "STRING"),
    bigquery.SchemaField("agent_first_name", "STRING"),
    bigquery.SchemaField("agent_last_name", "STRING"),
    bigquery.SchemaField("team_id", "INT64"),
    bigquery.SchemaField("team_name", "STRING"),
    bigquery.SchemaField("skill_id", "INT64"),
    bigquery.SchemaField("skill_name", "STRING"),
    bigquery.SchemaField("campaign_id", "INT64"),
    bigquery.SchemaField("campaign_name", "STRING"),
    bigquery.SchemaField("point_of_contact_id", "INT64"),
    bigquery.SchemaField("point_of_contact_name", "STRING"),
    bigquery.SchemaField("total_duration_seconds", "FLOAT64"),
    bigquery.SchemaField("agent_seconds", "FLOAT64"),
    bigquery.SchemaField("in_queue_seconds", "FLOAT64"),
    bigquery.SchemaField("hold_seconds", "FLOAT64"),
    bigquery.SchemaField("acw_seconds", "FLOAT64"),
    bigquery.SchemaField("abandon_seconds", "FLOAT64"),
    bigquery.SchemaField("abandoned", "BOOL"),
    bigquery.SchemaField("is_refused", "BOOL"),
    bigquery.SchemaField("service_level_flag", "INT64"),
    bigquery.SchemaField("primary_disposition_id", "INT64"),
    bigquery.SchemaField("secondary_disposition_id", "INT64"),
    bigquery.SchemaField("disposition_notes", "STRING"),
    bigquery.SchemaField("transfer_indicator_id", "INT64"),
    bigquery.SchemaField("transfer_indicator_name", "STRING"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP", mode="REQUIRED"),
]

AGENTS_SCHEMA = [
    bigquery.SchemaField("agent_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("first_name", "STRING"),
    bigquery.SchemaField("last_name", "STRING"),
    bigquery.SchemaField("display_name", "STRING"),
    bigquery.SchemaField("team_id", "INT64"),
    bigquery.SchemaField("team_name", "STRING"),
    bigquery.SchemaField("profile_id", "INT64"),
    bigquery.SchemaField("profile_name", "STRING"),
    bigquery.SchemaField("is_active", "BOOL"),
    bigquery.SchemaField("last_updated_at", "TIMESTAMP"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP", mode="REQUIRED"),
]

RUNS_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("report_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("completed_at", "TIMESTAMP"),
    bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("contact_count", "INT64"),
    bigquery.SchemaField("agent_count", "INT64"),
    bigquery.SchemaField("api_total_records", "INT64"),
    bigquery.SchemaField("error_message", "STRING"),
    bigquery.SchemaField("force_refresh", "BOOL"),
]


class CxoneKpiError(RuntimeError):
    """Raised for CXone KPI ingestion failures."""


def get_cached_cxone_day_status(
    client: bigquery.Client,
    *,
    report_date: date,
) -> dict[str, Any]:
    """Return cached CXone status for one day without creating or updating BQ tables."""

    try:
        latest = _latest_successful_run_status(client, report_date)
        if latest and latest["is_fresh"]:
            return _status_from_run(latest, status="cached", available=True)
        if latest:
            status = _status_from_run(latest, status="stale", available=False)
            status["warning"] = (
                "CXone telephony cache was loaded before the final daily cutoff; "
                "phone columns are unavailable in read-only mode."
            )
            return status
    except google_exceptions.NotFound:
        pass
    except Exception as exc:
        logger.warning("CXone KPI cached-status check failed for %s: %s", report_date, _error_summary(exc))
        return {
            "status": "failed",
            "available": False,
            "warning": "CXone telephony cache status could not be checked; phone columns are unavailable.",
            "error_message": str(exc)[:300],
        }

    return {
        "status": "cache_missing",
        "available": False,
        "warning": "CXone telephony cache is unavailable for this date in read-only mode; phone columns are omitted.",
    }


def ensure_cxone_day_loaded(
    client: bigquery.Client,
    *,
    report_date: date,
    start_at: datetime,
    end_at: datetime,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Ensure one Eastern report day is available in BigQuery.

    Returns a status dictionary intended for report coverage/warnings. This
    function never exposes credentials and only calls read-only CXone APIs.
    """

    if not _configured():
        return {
            "status": "not_configured",
            "available": False,
            "warning": "CXone telephony credentials are not configured; phone columns are unavailable.",
        }

    try:
        ensure_tables(client)
    except Exception as exc:
        logger.exception("CXone KPI BigQuery table setup failed: %s", exc)
        return {
            "status": "failed",
            "available": False,
            "warning": "CXone telephony cache tables could not be prepared; phone columns are unavailable.",
            "error_message": str(exc)[:300],
        }

    if not force_refresh:
        latest = _latest_successful_run_status(client, report_date)
        if latest and latest["is_fresh"]:
            return _status_from_run(latest, status="cached", available=True)
        if latest:
            logger.info(
                "CXone KPI cache for %s is stale (completed_at=%s, fresh_after=%s); refreshing.",
                report_date,
                latest.get("completed_at"),
                latest.get("fresh_after"),
            )

    started = datetime.now(timezone.utc)
    run_id = f"cxone_{report_date.isoformat()}_{started.strftime('%Y%m%dT%H%M%SZ')}"
    try:
        session = _CxoneSession()
        contacts_payload = session.fetch_completed_contacts(start_at=start_at, end_at=end_at)
        agents_payload = session.fetch_agents()

        contact_rows = [
            _normalize_contact(item, report_date=report_date, fetched_at=started)
            for item in contacts_payload["contacts"]
        ]
        agent_rows = [
            _normalize_agent(item, fetched_at=started)
            for item in agents_payload
            if item.get("agentId") is not None
        ]

        _replace_contacts(client, report_date, contact_rows)
        _replace_agents(client, agent_rows)
        completed = datetime.now(timezone.utc)
        _record_run(
            client,
            run_id=run_id,
            report_date=report_date,
            started_at=started,
            completed_at=completed,
            status="success",
            contact_count=len(contact_rows),
            agent_count=len(agent_rows),
            api_total_records=contacts_payload.get("total_records"),
            force_refresh=force_refresh,
        )
        is_fresh = _completed_after_fresh_cutoff(report_date, completed)
        return {
            "status": "loaded",
            "available": True,
            "contact_count": len(contact_rows),
            "agent_count": len(agent_rows),
            "api_total_records": contacts_payload.get("total_records"),
            "run_id": run_id,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "fresh_after": _fresh_cache_cutoff_utc(report_date).isoformat(),
            "is_fresh": is_fresh,
        }
    except Exception as exc:
        logger.exception("CXone KPI load failed for %s: %s", report_date, exc)
        _record_run(
            client,
            run_id=run_id,
            report_date=report_date,
            started_at=started,
            status="failed",
            error_message=str(exc)[:1000],
            force_refresh=force_refresh,
        )
        return {
            "status": "failed",
            "available": False,
            "warning": "CXone telephony data could not be loaded; phone columns may be incomplete.",
            "error_message": str(exc)[:300],
        }


def ensure_tables(client: bigquery.Client) -> None:
    dataset_ref = bigquery.DatasetReference(settings.google_cloud_project_id, DATASET)
    for table_name, schema in {
        "cxone_completed_contacts": CONTACTS_SCHEMA,
        "cxone_agents": AGENTS_SCHEMA,
        "cxone_kpi_ingest_runs": RUNS_SCHEMA,
    }.items():
        table_ref = dataset_ref.table(table_name)
        try:
            table = client.get_table(table_ref)
        except google_exceptions.NotFound:
            try:
                table = client.create_table(bigquery.Table(table_ref, schema=schema))
            except google_exceptions.Conflict:
                table = client.get_table(table_ref)
        existing = {field.name for field in table.schema}
        additions = [field for field in schema if field.name not in existing]
        if additions:
            table.schema = list(table.schema) + additions
            client.update_table(table, ["schema"])


def _configured() -> bool:
    return bool(settings.cxone_access_key_id and settings.cxone_access_key_secret and settings.cxone_region)


def _fresh_cache_cutoff_utc(report_date: date) -> datetime:
    cutoff_local = datetime.combine(
        report_date + timedelta(days=1),
        time(FRESH_CACHE_READY_HOUR_LOCAL, 0),
        tzinfo=BUSINESS_TIMEZONE,
    )
    return cutoff_local.astimezone(timezone.utc)


def _completed_after_fresh_cutoff(report_date: date, completed_at: datetime | None) -> bool:
    if completed_at is None:
        return False
    return completed_at.astimezone(timezone.utc) >= _fresh_cache_cutoff_utc(report_date)


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _status_from_run(run: dict[str, Any], *, status: str, available: bool) -> dict[str, Any]:
    return {
        "status": status,
        "available": available,
        "contact_count": run.get("contact_count"),
        "agent_count": run.get("agent_count"),
        "api_total_records": run.get("api_total_records"),
        "run_id": run.get("run_id"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "fresh_after": run.get("fresh_after"),
        "is_fresh": bool(run.get("is_fresh")),
    }


def _latest_successful_run_status(client: bigquery.Client, report_date: date) -> dict[str, Any] | None:
    sql = f"""
    SELECT
      run_id,
      started_at,
      completed_at,
      contact_count,
      agent_count,
      api_total_records,
      force_refresh
    FROM `{RUNS_TABLE}`
    WHERE report_date = @report_date
      AND status = 'success'
    ORDER BY COALESCE(completed_at, started_at) DESC
    LIMIT 1
    """
    rows = _query_rows(
        client,
        sql,
        query_parameters=[bigquery.ScalarQueryParameter("report_date", "DATE", report_date)],
        context="checking cached CXone KPI status",
    )
    if not rows:
        return None
    row = rows[0]
    started_at = _as_utc_datetime(row.get("started_at"))
    completed_at = _as_utc_datetime(row.get("completed_at"))
    fresh_after = _fresh_cache_cutoff_utc(report_date)
    return {
        "run_id": row.get("run_id"),
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "fresh_after": fresh_after.isoformat(),
        "contact_count": row.get("contact_count"),
        "agent_count": row.get("agent_count"),
        "api_total_records": row.get("api_total_records"),
        "force_refresh": row.get("force_refresh"),
        "is_fresh": _completed_after_fresh_cutoff(report_date, completed_at),
    }


def _has_successful_run(client: bigquery.Client, report_date: date) -> bool:
    return _latest_successful_run_status(client, report_date) is not None


def _cached_contact_count(client: bigquery.Client, report_date: date) -> int:
    sql = f"SELECT COUNT(DISTINCT contact_id) AS count FROM `{CONTACTS_TABLE}` WHERE report_date = @report_date"
    rows = _query_rows(
        client,
        sql,
        query_parameters=[bigquery.ScalarQueryParameter("report_date", "DATE", report_date)],
        context="counting cached CXone contacts",
    )
    return int(rows[0]["count"] or 0) if rows else 0


def _query_rows(
    client: bigquery.Client,
    sql: str,
    *,
    query_parameters: list[bigquery.ScalarQueryParameter] | None = None,
    context: str,
) -> list[dict[str, Any]]:
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=query_parameters or []),
            retry=None,
            job_retry=None,
            timeout=BIGQUERY_JOB_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise CxoneKpiError(
            f"BigQuery request failed while {context}: {_error_summary(exc)}"
        ) from exc
    try:
        rows = job.result(timeout=BIGQUERY_JOB_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        raise CxoneKpiError(
            f"BigQuery timed out after {BIGQUERY_JOB_TIMEOUT_SECONDS:g}s while {context}."
        ) from exc
    except Exception as exc:
        raise CxoneKpiError(
            f"BigQuery result read failed while {context}: {_error_summary(exc)}"
        ) from exc
    return [dict(row) for row in rows]


def _error_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class _CxoneSession:
    def __init__(self) -> None:
        self.ui_host = _region_host()
        self.session = requests.Session()
        self.token = self._authenticate()
        self.api_endpoint = self._discover_api_endpoint()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _authenticate(self) -> str:
        url = f"https://{self.ui_host}/authentication/v1/token/access-key"
        response = self.session.post(
            url,
            json={
                "accessKeyId": settings.cxone_access_key_id,
                "accessKeySecret": settings.cxone_access_key_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise CxoneKpiError("CXone access-key authentication did not return an access token.")
        return token

    def _discover_api_endpoint(self) -> str:
        url = f"https://{self.ui_host}/.well-known/cxone-configuration"
        response = self.session.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        endpoint = response.json().get("api_endpoint")
        if not endpoint:
            raise CxoneKpiError("CXone discovery did not return api_endpoint.")
        return str(endpoint).rstrip("/")

    def fetch_completed_contacts(self, *, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        contacts: list[dict[str, Any]] = []
        total_records: int | None = None
        skip = 0
        while True:
            params = {
                "startDate": _cxone_timestamp(start_at),
                "endDate": _cxone_timestamp(end_at),
                "top": PAGE_SIZE,
                "skip": skip,
            }
            url = f"{self.api_endpoint}/inContactAPI/services/{API_VERSION}/contacts/completed?{urlencode(params)}"
            response = self.session.get(url, headers=self._headers(), timeout=60)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("completedContacts") or []
            contacts.extend(page)
            total_records = payload.get("totalRecords", total_records)
            if not page or len(page) < PAGE_SIZE:
                break
            if total_records is not None and len(contacts) >= int(total_records):
                break
            skip += PAGE_SIZE
        return {"contacts": contacts, "total_records": total_records}

    def fetch_agents(self) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = []
        skip = 0
        while True:
            params = {"top": PAGE_SIZE, "skip": skip}
            url = f"{self.api_endpoint}/inContactAPI/services/{API_VERSION}/agents?{urlencode(params)}"
            response = self.session.get(url, headers=self._headers(), timeout=60)
            response.raise_for_status()
            payload = response.json()
            page = payload.get("agents") or []
            agents.extend(page)
            total_records = payload.get("totalRecords")
            if not page or len(page) < PAGE_SIZE:
                break
            if total_records is not None and len(agents) >= int(total_records):
                break
            skip += PAGE_SIZE
        return agents


def _region_host() -> str:
    region = str(settings.cxone_region or "na1").strip()
    return region if "." in region else f"{region}.niceincontact.com"


def _cxone_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_contact(item: dict[str, Any], *, report_date: date, fetched_at: datetime) -> dict[str, Any]:
    return {
        "report_date": report_date.isoformat(),
        "contact_id": _int(item.get("contactId")),
        "master_contact_id": _int(item.get("masterContactId")),
        "contact_start_at": item.get("contactStartDate"),
        "last_update_at": item.get("lastUpdateTime"),
        "media_type_id": _int(item.get("mediaTypeId")),
        "media_type_name": item.get("mediaTypeName"),
        "is_outbound": _bool(item.get("isOutbound")),
        "from_address": item.get("fromAddress"),
        "to_address": item.get("toAddress"),
        "agent_id": _int(item.get("agentId")),
        "agent_email": None,
        "agent_first_name": item.get("firstName"),
        "agent_last_name": item.get("lastName"),
        "team_id": _int(item.get("teamId")),
        "team_name": item.get("teamName"),
        "skill_id": _int(item.get("skillId")),
        "skill_name": item.get("skillName"),
        "campaign_id": _int(item.get("campaignId")),
        "campaign_name": item.get("campaignName"),
        "point_of_contact_id": _int(item.get("pointOfContactId")),
        "point_of_contact_name": item.get("pointOfContactName"),
        "total_duration_seconds": _float(item.get("totalDurationSeconds")),
        "agent_seconds": _float(item.get("agentSeconds")),
        "in_queue_seconds": _float(item.get("inQueueSeconds")),
        "hold_seconds": _float(item.get("holdSeconds")),
        "acw_seconds": _float(item.get("acwSeconds")),
        "abandon_seconds": _float(item.get("abandonSeconds")),
        "abandoned": _bool(item.get("abandoned")),
        "is_refused": _bool(item.get("isRefused")),
        "service_level_flag": _int(item.get("serviceLevelFlag")),
        "primary_disposition_id": _int(item.get("primaryDispositionId")),
        "secondary_disposition_id": _int(item.get("secondaryDispositionId")),
        "disposition_notes": item.get("dispositionNotes"),
        "transfer_indicator_id": _int(item.get("transferIndicatorId")),
        "transfer_indicator_name": item.get("transferIndicatorName"),
        "fetched_at": fetched_at.isoformat(),
    }


def _normalize_agent(item: dict[str, Any], *, fetched_at: datetime) -> dict[str, Any]:
    first = str(item.get("firstName") or "").strip()
    last = str(item.get("lastName") or "").strip()
    return {
        "agent_id": _int(item.get("agentId")),
        "user_id": item.get("userId"),
        "email": _lower(item.get("emailAddress")),
        "first_name": first or None,
        "last_name": last or None,
        "display_name": " ".join(part for part in (first, last) if part) or item.get("userName"),
        "team_id": _int(item.get("teamId")),
        "team_name": item.get("teamName"),
        "profile_id": _int(item.get("profileId")),
        "profile_name": item.get("profileName"),
        "is_active": _bool(item.get("isActive")),
        "last_updated_at": item.get("lastUpdated"),
        "fetched_at": fetched_at.isoformat(),
    }


def _replace_contacts(client: bigquery.Client, report_date: date, rows: list[dict[str, Any]]) -> None:
    stage_table = _load_stage_table(client, "cxone_completed_contacts_stage", CONTACTS_SCHEMA, rows)
    columns = [field.name for field in CONTACTS_SCHEMA]
    assignments = ",\n          ".join(f"{column} = S.{column}" for column in columns)
    insert_columns = ", ".join(columns)
    insert_values = ", ".join(f"S.{column}" for column in columns)
    sql = f"""
    MERGE `{CONTACTS_TABLE}` T
    USING `{stage_table}` S
      ON T.report_date = S.report_date
     AND T.contact_id = S.contact_id
    WHEN MATCHED THEN UPDATE SET
          {assignments}
    WHEN NOT MATCHED THEN INSERT ({insert_columns})
      VALUES ({insert_values})
    WHEN NOT MATCHED BY SOURCE AND T.report_date = @report_date THEN DELETE
    """
    try:
        client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("report_date", "DATE", report_date)]
            ),
            retry=None,
            job_retry=None,
            timeout=BIGQUERY_JOB_TIMEOUT_SECONDS,
        ).result(timeout=BIGQUERY_JOB_TIMEOUT_SECONDS)
    finally:
        _delete_stage_table(client, stage_table)


def _replace_agents(client: bigquery.Client, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    stage_table = _load_stage_table(client, "cxone_agents_stage", AGENTS_SCHEMA, rows)
    columns = [field.name for field in AGENTS_SCHEMA]
    assignments = ",\n          ".join(f"{column} = S.{column}" for column in columns)
    insert_columns = ", ".join(columns)
    insert_values = ", ".join(f"S.{column}" for column in columns)
    sql = f"""
    MERGE `{AGENTS_TABLE}` T
    USING `{stage_table}` S
      ON T.agent_id = S.agent_id
    WHEN MATCHED THEN UPDATE SET
          {assignments}
    WHEN NOT MATCHED THEN INSERT ({insert_columns})
      VALUES ({insert_values})
    """
    try:
        client.query(
            sql,
            retry=None,
            job_retry=None,
            timeout=BIGQUERY_JOB_TIMEOUT_SECONDS,
        ).result(timeout=BIGQUERY_JOB_TIMEOUT_SECONDS)
    finally:
        _delete_stage_table(client, stage_table)


def _load_stage_table(
    client: bigquery.Client,
    prefix: str,
    schema: list[bigquery.SchemaField],
    rows: list[dict[str, Any]],
) -> str:
    table_name = f"{prefix}_{uuid4().hex}"
    table_ref = bigquery.DatasetReference(settings.google_cloud_project_id, DATASET).table(table_name)
    table = bigquery.Table(table_ref, schema=schema)
    table.expires = datetime.now(timezone.utc) + timedelta(hours=1)
    client.create_table(table)
    if rows:
        client.load_table_from_json(
            rows,
            table_ref,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            ),
        ).result(timeout=BIGQUERY_JOB_TIMEOUT_SECONDS)
    return f"{settings.google_cloud_project_id}.{DATASET}.{table_name}"


def _delete_stage_table(client: bigquery.Client, table_id: str) -> None:
    try:
        client.delete_table(table_id, not_found_ok=True)
    except Exception:
        logger.warning("Failed to delete CXone KPI stage table %s", table_id, exc_info=True)


def _record_run(
    client: bigquery.Client,
    *,
    run_id: str,
    report_date: date,
    started_at: datetime,
    completed_at: datetime | None = None,
    status: str,
    contact_count: int = 0,
    agent_count: int = 0,
    api_total_records: int | None = None,
    error_message: str | None = None,
    force_refresh: bool = False,
) -> None:
    row = {
        "run_id": run_id,
        "report_date": report_date.isoformat(),
        "started_at": started_at.isoformat(),
        "completed_at": (completed_at or datetime.now(timezone.utc)).isoformat(),
        "status": status,
        "contact_count": contact_count,
        "agent_count": agent_count,
        "api_total_records": api_total_records,
        "error_message": error_message,
        "force_refresh": force_refresh,
    }
    errors = client.insert_rows_json(RUNS_TABLE, [row])
    if errors:
        logger.warning("Failed to record CXone KPI run status: %s", errors[:2])


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _lower(value: Any) -> str | None:
    safe = str(value or "").strip().lower()
    return safe or None
