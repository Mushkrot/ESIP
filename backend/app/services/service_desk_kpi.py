"""Daily Service Desk KPI report.

Zendesk remains the source for ticket work, backlog, and reply timing. NICE
CXone completed contacts fill the phone columns when the read-only API/cache is
available. The report still does not invent true FCR or read call media.
"""

from __future__ import annotations

import os
import logging
from collections import defaultdict
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from app.config import settings
from app.reference_data.fixtures import IDENTITY_DOMAIN, LEGACY_IDENTITY_ALIASES
from app.services import cxone_kpi, firestore

logger = logging.getLogger(__name__)

BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
BIGQUERY_QUERY_TIMEOUT_SECONDS = float(os.environ.get("SERVICE_DESK_KPI_BQ_TIMEOUT_SECONDS", "30"))
FIRESTORE_STREAM_TIMEOUT_SECONDS = float(os.environ.get("SERVICE_DESK_KPI_FIRESTORE_TIMEOUT_SECONDS", "10"))
BLANK_OWNER_EMAIL = "__blank__"
BLANK_OWNER_LABEL = "Blank / unassigned"
NAME_OWNER_PREFIX = "__name__:"
QUEUE_DISPLAY_ALIASES = {
    "service desk supervisors": "Service Desk Supervisors",
    "deployment analysts": "DeploymentTeam",
    "operations support": "Operations Support",
    "menu maintenance": "Menu Maintenance",
    "menu maintenance and ba": "Menu Maintenance",
    "store operations support": "TrainingTeam",
    "store operations trainers": "TrainingTeam",
}
SERVICE_DESK_GROUPS = (
    (
        "ServiceDesk",
        (
            "Alex Morgan",
            "Casey Nguyen",
            "Samir Patel",
            "Riley Brooks",
        ),
    ),
    ("Menu Maintenance", ("Sofia Reyes",)),
    ("Technical Services", ("Noah Bennett",)),
    ("Netops", ("Jamie Rivera", "Morgan Chen")),
    ("Operations Support", ("Operations Support",)),
    ("DeploymentTeam", ("Taylor Kim",)),
    ("Field Services Partner", ()),
    ("Business Analysts", ("Sofia Reyes", "Jordan Lee")),
    ("TrainingTeam", ("Olivia Parker",)),
    ("Service Desk Supervisors", ()),
)
GROUP_DISPLAY_ALIASES = {
    "service desk supervisors": "Service Desk Supervisors",
    "field_services_partner": "Field Services Partner",
    "service desk analysts": "ServiceDesk",
    "it service desk": "ServiceDesk",
    "service desk": "ServiceDesk",
    "servicedesk": "ServiceDesk",
    "technical services": "Technical Services",
    "menu maintenance": "Menu Maintenance",
    "menu maintenance and ba": "Menu Maintenance",
    "deployment analysts": "DeploymentTeam",
    "operations support": "Operations Support",
    "store operations support": "TrainingTeam",
    "store operations trainers": "TrainingTeam",
    "trainingteam": "TrainingTeam",
    "the deployment analyst team": "DeploymentTeam",
    "deployment analyst team": "DeploymentTeam",
    "thedeploymentanalystteam": "DeploymentTeam",
}
PERSON_DISPLAY_ALIASES = {
    "sam patel": "Samir Patel",
}
GROUP_MULTI_GROUP_MEMBERS = {
    "sofia reyes": ("Menu Maintenance", "Business Analysts"),
}
GROUP_PERSON_ONLY_FALLBACK = {
    "sofia reyes": "Menu Maintenance",
}
UNMAPPED_GROUP_KEY = "__unmapped__"
UNMAPPED_GROUP_LABEL = "Unmapped / queues"
SERVICE_DESK_CXONE_SKILLS = (
    "SUPPORT_Restaurant Outage EN",
    "SUPPORT_Restaurant Outage FR",
    "SUPPORT_Payment EN",
    "SUPPORT_Payment FR",
    "SUPPORT_General Support EN",
    "SUPPORT_General Support FR",
    "SUPPORT_Technician Queue",
    "SUPPORT_Technician Queue FR",
    "SUPPORT_Special Projects EN",
    "SUPPORT_Special Projects FR",
    "SUPPORT_Security and Privacy EN",
    "SUPPORT_Security and Privacy FR",
    "Support_Voicemail",
)


class ServiceDeskKpiError(RuntimeError):
    """Raised when the KPI report cannot be assembled reliably."""


def get_daily_report(
    *,
    report_date: str | None = None,
    refresh_telephony: bool = False,
    telephony_mode: str = "auto",
    load_profiles: bool = True,
) -> dict[str, Any]:
    day = _parse_report_date(report_date)
    start_at, end_at = _day_bounds_utc(day)
    snapshot_at = _snapshot_at_utc(day)
    outstanding_at = _outstanding_at_utc(day)
    client = _get_client()
    profiles = _load_identity_profiles() if load_profiles else {}
    aliases = _profile_alias_map(profiles)
    mode = str(telephony_mode or "auto").strip().lower()
    if mode == "cached_only":
        telephony_status = cxone_kpi.get_cached_cxone_day_status(
            client,
            report_date=day,
        )
    elif mode == "auto":
        telephony_status = cxone_kpi.ensure_cxone_day_loaded(
            client,
            report_date=day,
            start_at=start_at,
            end_at=end_at,
            force_refresh=refresh_telephony,
        )
    else:
        raise ValueError(f"Unsupported telephony_mode: {telephony_mode}")

    params = _query_params(start_at, end_at, snapshot_at=snapshot_at, outstanding_at=outstanding_at)
    ticket_rows = _query(client, _ticket_kpi_sql(), params)
    first_reply_rows = _query(client, _first_reply_sql(), params)
    customer_reply_rows = _query(client, _customer_reply_sql(), params)
    phone_rows = _query(client, _phone_kpi_sql(), params) if telephony_status.get("available") else []
    coverage_rows = _query(client, _coverage_sql(), params)

    rows = _merge_rows(
        ticket_rows=ticket_rows,
        first_reply_rows=first_reply_rows,
        customer_reply_rows=customer_reply_rows,
        phone_rows=phone_rows,
        profiles=profiles,
        aliases=aliases,
    )
    summary = _summary(rows)
    group_rows = _group_rows(rows, ticket_rows=ticket_rows)
    group_sections = _group_sections(rows, ticket_rows=ticket_rows, group_rows=group_rows)
    coverage = _coverage(coverage_rows)
    coverage["telephony"] = telephony_status
    warnings = _warnings(coverage)
    grouping = _grouping_metadata(group_rows)
    warnings.extend(grouping["warnings"])

    return {
        "date": day.isoformat(),
        "timezone": "America/New_York",
        "generated_at": datetime.now(BUSINESS_TIMEZONE).isoformat(),
        "summary": summary,
        "coverage": coverage,
        "rows": rows,
        "group_rows": group_rows,
        "group_sections": group_sections,
        "grouping": grouping,
        "notes": [],
        "warnings": warnings,
    }


def _get_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=settings.google_cloud_project_id)


def _parse_report_date(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except Exception:
            pass
    return datetime.now(BUSINESS_TIMEZONE).date() - timedelta(days=1)


def _day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, time.min, tzinfo=BUSINESS_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def _snapshot_at_utc(day: date, *, now: datetime | None = None) -> datetime:
    now_local = (now or datetime.now(BUSINESS_TIMEZONE)).astimezone(BUSINESS_TIMEZONE)
    snapshot_local = datetime.combine(day + timedelta(days=1), time(8, 0), tzinfo=BUSINESS_TIMEZONE)
    if snapshot_local > now_local:
        snapshot_local = now_local
    return snapshot_local.astimezone(ZoneInfo("UTC"))


def _outstanding_at_utc(day: date, *, now: datetime | None = None) -> datetime:
    """Return the cutoff for previous-day outstanding backlog."""
    now_local = (now or datetime.now(BUSINESS_TIMEZONE)).astimezone(BUSINESS_TIMEZONE)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=BUSINESS_TIMEZONE)
    if end_local > now_local:
        end_local = now_local
    return end_local.astimezone(ZoneInfo("UTC"))


def _allowed_domains() -> list[str]:
    domains = {
        str(domain or "").strip().lower().lstrip("@")
        for domain in settings.allowed_email_domains_list
        if str(domain or "").strip()
    }
    domains.update({"example.com", "example.com"})
    return sorted(domains)


def _email_domain_condition(expr: str) -> str:
    return f"LOWER(SPLIT(IFNULL({expr}, ''), '@')[SAFE_OFFSET(1)]) IN UNNEST(@allowed_domains)"


def _sql_string_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "\\'") + "'" for value in values)


def _query_params(
    start_at: datetime,
    end_at: datetime,
    *,
    snapshot_at: datetime | None = None,
    outstanding_at: datetime | None = None,
) -> list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]:
    return [
        bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", start_at),
        bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", end_at),
        bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", snapshot_at or end_at),
        bigquery.ScalarQueryParameter("outstanding_at", "TIMESTAMP", outstanding_at or end_at),
        bigquery.ArrayQueryParameter("allowed_domains", "STRING", _allowed_domains()),
    ]


def _query(
    client: bigquery.Client,
    sql: str,
    params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter],
) -> list[dict[str, Any]]:
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    context = "loading Service Desk KPI data from BigQuery"
    try:
        job = client.query(
            sql,
            job_config=job_config,
            retry=None,
            job_retry=None,
            timeout=BIGQUERY_QUERY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ServiceDeskKpiError(
            f"BigQuery request failed while {context}: {_error_summary(exc)}"
        ) from exc
    return _rows_from_job(job, context=context)


def _rows_from_job(
    job: Any,
    *,
    context: str,
) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in job.result(timeout=BIGQUERY_QUERY_TIMEOUT_SECONDS)]
    except FuturesTimeoutError as exc:
        raise ServiceDeskKpiError(
            f"BigQuery timed out after {BIGQUERY_QUERY_TIMEOUT_SECONDS:g}s while {context}."
        ) from exc
    except Exception as exc:
        raise ServiceDeskKpiError(
            f"BigQuery result read failed while {context}: {_error_summary(exc)}"
        ) from exc


def _error_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _ticket_kpi_sql() -> str:
    ticket_table = f"`{settings.google_cloud_project_id}.esip_reference.tickets`"
    audit_table = f"`{settings.google_cloud_project_id}.esip_reference.zendesk_ticket_audit_events`"
    email_ok = _email_domain_condition("COALESCE(identity.assignee_email, tickets.assignee_email)")
    outstanding_email_ok = _email_domain_condition("COALESCE(out_identity.assignee_email, tickets.assignee_email)")
    solve_email_ok = _email_domain_condition("COALESCE(solve_identity.assignee_email, tickets.assignee_email)")
    return f"""
    WITH first_assignee_after_snapshot AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'assignee_id'
          AND created_at > @snapshot_at
      )
      WHERE row_num = 1
    ),
    first_group_after_snapshot AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'group_id'
          AND created_at > @snapshot_at
      )
      WHERE row_num = 1
    ),
    first_status_after_outstanding AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'status'
          AND created_at >= @outstanding_at
      )
      WHERE row_num = 1
    ),
    first_assignee_after_outstanding AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'assignee_id'
          AND created_at >= @outstanding_at
      )
      WHERE row_num = 1
    ),
    first_group_after_outstanding AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'group_id'
          AND created_at >= @outstanding_at
      )
      WHERE row_num = 1
    ),
    assignee_identities AS (
      SELECT
        assignee_id,
        ARRAY_AGG(assignee_name IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS assignee_name,
        ARRAY_AGG(assignee_email IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS assignee_email
      FROM {ticket_table}
      WHERE assignee_id IS NOT NULL
      GROUP BY assignee_id
    ),
    group_identities AS (
      SELECT
        assignee_group_id,
        ARRAY_AGG(assignee_group_name IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS group_name
      FROM {ticket_table}
      WHERE assignee_group_id IS NOT NULL
      GROUP BY assignee_group_id
    ),
    assigned_scoped AS (
      SELECT
        COALESCE(identity.assignee_email, tickets.assignee_email) AS assignee_email_asof,
        COALESCE(identity.assignee_name, tickets.assignee_name) AS assignee_name_asof,
        group_identity.group_name AS group_name_asof,
        CASE
          WHEN {email_ok} THEN LOWER(TRIM(COALESCE(identity.assignee_email, tickets.assignee_email)))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(COALESCE(identity.assignee_name, tickets.assignee_name)), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(group_identity.group_name, '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(group_identity.group_name), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_EMAIL}'
          ELSE NULL
        END AS owner_email,
        CASE
          WHEN NULLIF(TRIM(IFNULL(COALESCE(identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN NULLIF(TRIM(COALESCE(identity.assignee_name, tickets.assignee_name)), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(identity.assignee_email, tickets.assignee_email), '')), '') IS NULL
            AND NULLIF(TRIM(IFNULL(group_identity.group_name, '')), '') IS NOT NULL THEN NULLIF(TRIM(group_identity.group_name), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_LABEL}'
          ELSE NULLIF(TRIM(COALESCE(identity.assignee_name, tickets.assignee_name)), '')
        END AS assignee_name,
        tickets.created_at,
        tickets.updated_at
      FROM {ticket_table} tickets
      LEFT JOIN first_assignee_after_snapshot assignee_after
        ON assignee_after.ticket_id = tickets.id
      LEFT JOIN assignee_identities identity
        ON identity.assignee_id = COALESCE(SAFE_CAST(assignee_after.previous_value AS INT64), tickets.assignee_id)
      LEFT JOIN first_group_after_snapshot group_after
        ON group_after.ticket_id = tickets.id
      LEFT JOIN group_identities group_identity
        ON group_identity.assignee_group_id = COALESCE(SAFE_CAST(group_after.previous_value AS INT64), tickets.assignee_group_id)
      WHERE tickets.created_at >= @start_at
        AND tickets.created_at < @end_at
    ),
    assigned_labeled AS (
      SELECT
        *,
        CASE
          WHEN NULLIF(TRIM(IFNULL(group_name_asof, '')), '') IS NOT NULL
            THEN LOWER(REGEXP_REPLACE(TRIM(group_name_asof), r'\\s+', ' '))
          ELSE ''
        END AS group_key,
        NULLIF(TRIM(group_name_asof), '') AS group_name_normalized
      FROM assigned_scoped
    ),
    assigned_aggregates AS (
      SELECT
        owner_email,
        group_key,
        ARRAY_AGG(group_name_normalized IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS group_name,
        ARRAY_AGG(assignee_name IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS assignee_name,
        COUNT(*) AS new_assigned_tickets
      FROM assigned_labeled
      WHERE owner_email IS NOT NULL
      GROUP BY owner_email, group_key
    ),
    outstanding_scoped AS (
      SELECT
        out_group_identity.group_name AS group_name_asof,
        CASE
          WHEN {outstanding_email_ok} THEN LOWER(TRIM(COALESCE(out_identity.assignee_email, tickets.assignee_email)))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(out_identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(COALESCE(out_identity.assignee_name, tickets.assignee_name)), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(out_group_identity.group_name, '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(out_group_identity.group_name), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(out_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_EMAIL}'
          ELSE NULL
        END AS owner_email,
        CASE
          WHEN NULLIF(TRIM(IFNULL(COALESCE(out_identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN NULLIF(TRIM(COALESCE(out_identity.assignee_name, tickets.assignee_name)), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(out_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL
            AND NULLIF(TRIM(IFNULL(out_group_identity.group_name, '')), '') IS NOT NULL THEN NULLIF(TRIM(out_group_identity.group_name), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(out_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_LABEL}'
          ELSE NULLIF(TRIM(COALESCE(out_identity.assignee_name, tickets.assignee_name)), '')
        END AS assignee_name,
        LOWER(COALESCE(out_status_after.previous_value, tickets.status, '')) AS status_asof_at_day_end,
        tickets.updated_at
      FROM {ticket_table} tickets
      LEFT JOIN first_status_after_outstanding out_status_after
        ON out_status_after.ticket_id = tickets.id
      LEFT JOIN first_assignee_after_outstanding out_assignee_after
        ON out_assignee_after.ticket_id = tickets.id
      LEFT JOIN assignee_identities out_identity
        ON out_identity.assignee_id = COALESCE(SAFE_CAST(out_assignee_after.previous_value AS INT64), tickets.assignee_id)
      LEFT JOIN first_group_after_outstanding out_group_after
        ON out_group_after.ticket_id = tickets.id
      LEFT JOIN group_identities out_group_identity
        ON out_group_identity.assignee_group_id = COALESCE(SAFE_CAST(out_group_after.previous_value AS INT64), tickets.assignee_group_id)
      WHERE tickets.created_at < @outstanding_at
    ),
    outstanding_labeled AS (
      SELECT
        *,
        CASE
          WHEN NULLIF(TRIM(IFNULL(group_name_asof, '')), '') IS NOT NULL
            THEN LOWER(REGEXP_REPLACE(TRIM(group_name_asof), r'\\s+', ' '))
          ELSE ''
        END AS group_key,
        NULLIF(TRIM(group_name_asof), '') AS group_name_normalized
      FROM outstanding_scoped
      WHERE status_asof_at_day_end IN ('new', 'open', 'pending', 'hold')
    ),
    outstanding_aggregates AS (
      SELECT
        owner_email,
        group_key,
        ARRAY_AGG(group_name_normalized IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS group_name,
        ARRAY_AGG(assignee_name IGNORE NULLS ORDER BY updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS assignee_name,
        COUNT(*) AS outstanding_tickets
      FROM outstanding_labeled
      WHERE owner_email IS NOT NULL
      GROUP BY owner_email, group_key
    ),
    solve_events AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          ticket_id,
          created_at AS solved_event_at,
          ROW_NUMBER() OVER (
            PARTITION BY ticket_id, DATE(created_at, 'America/New_York')
            ORDER BY created_at ASC, audit_id ASC, event_id ASC
          ) AS row_num
        FROM {audit_table}
        WHERE field_name = 'status'
          AND LOWER(COALESCE(value, '')) = 'solved'
          AND created_at >= @start_at
          AND created_at < @end_at
      )
      WHERE row_num = 1
    ),
    first_assignee_after_solve AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          solve_events.ticket_id,
          solve_events.solved_event_at,
          audits.previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY solve_events.ticket_id, solve_events.solved_event_at
            ORDER BY audits.created_at ASC, audits.audit_id ASC, audits.event_id ASC
          ) AS row_num
        FROM solve_events
        JOIN {audit_table} audits
          ON audits.ticket_id = solve_events.ticket_id
         AND audits.field_name = 'assignee_id'
         AND audits.created_at > solve_events.solved_event_at
      )
      WHERE row_num = 1
    ),
    first_group_after_solve AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          solve_events.ticket_id,
          solve_events.solved_event_at,
          audits.previous_value,
          ROW_NUMBER() OVER (
            PARTITION BY solve_events.ticket_id, solve_events.solved_event_at
            ORDER BY audits.created_at ASC, audits.audit_id ASC, audits.event_id ASC
          ) AS row_num
        FROM solve_events
        JOIN {audit_table} audits
          ON audits.ticket_id = solve_events.ticket_id
         AND audits.field_name = 'group_id'
         AND audits.created_at > solve_events.solved_event_at
      )
      WHERE row_num = 1
    ),
    solved_scoped AS (
      SELECT
        solve_group_identity.group_name AS group_name_at_solve,
        CASE
          WHEN {solve_email_ok} THEN LOWER(TRIM(COALESCE(solve_identity.assignee_email, tickets.assignee_email)))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(solve_identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(COALESCE(solve_identity.assignee_name, tickets.assignee_name)), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(solve_group_identity.group_name, '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(solve_group_identity.group_name), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(COALESCE(solve_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_EMAIL}'
          ELSE NULL
        END AS owner_email,
        CASE
          WHEN NULLIF(TRIM(IFNULL(COALESCE(solve_identity.assignee_name, tickets.assignee_name), '')), '') IS NOT NULL
            THEN NULLIF(TRIM(COALESCE(solve_identity.assignee_name, tickets.assignee_name)), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(solve_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL
            AND NULLIF(TRIM(IFNULL(solve_group_identity.group_name, '')), '') IS NOT NULL THEN NULLIF(TRIM(solve_group_identity.group_name), '')
          WHEN NULLIF(TRIM(IFNULL(COALESCE(solve_identity.assignee_email, tickets.assignee_email), '')), '') IS NULL THEN '{BLANK_OWNER_LABEL}'
          ELSE NULLIF(TRIM(COALESCE(solve_identity.assignee_name, tickets.assignee_name)), '')
        END AS assignee_name,
        tickets.updated_at,
        solve_events.solved_event_at,
        tickets.resolution_hours,
        tickets.public_comment_count,
        tickets.resolution_sla_breached,
        tickets.resolution_sla_status,
        tickets.resolution_sla_target_hours
      FROM solve_events
      JOIN {ticket_table} tickets
        ON tickets.id = solve_events.ticket_id
      LEFT JOIN first_assignee_after_solve assignee_after_solve
        ON assignee_after_solve.ticket_id = solve_events.ticket_id
       AND assignee_after_solve.solved_event_at = solve_events.solved_event_at
      LEFT JOIN assignee_identities solve_identity
        ON solve_identity.assignee_id = COALESCE(SAFE_CAST(assignee_after_solve.previous_value AS INT64), tickets.assignee_id)
      LEFT JOIN first_group_after_solve group_after_solve
        ON group_after_solve.ticket_id = solve_events.ticket_id
       AND group_after_solve.solved_event_at = solve_events.solved_event_at
      LEFT JOIN group_identities solve_group_identity
        ON solve_group_identity.assignee_group_id = COALESCE(SAFE_CAST(group_after_solve.previous_value AS INT64), tickets.assignee_group_id)
    ),
    solved_labeled AS (
      SELECT
        *,
        CASE
          WHEN NULLIF(TRIM(IFNULL(group_name_at_solve, '')), '') IS NOT NULL
            THEN LOWER(REGEXP_REPLACE(TRIM(group_name_at_solve), r'\\s+', ' '))
          ELSE ''
        END AS group_key,
        NULLIF(TRIM(group_name_at_solve), '') AS group_name_normalized
      FROM solved_scoped
    ),
    solved_aggregates AS (
      SELECT
        owner_email,
        group_key,
        ARRAY_AGG(group_name_normalized IGNORE NULLS ORDER BY solved_event_at DESC, updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS group_name,
        ARRAY_AGG(assignee_name IGNORE NULLS ORDER BY solved_event_at DESC, updated_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS assignee_name,
        COUNT(*) AS solved_tickets,
        APPROX_QUANTILES(resolution_hours, 100 IGNORE NULLS)[OFFSET(50)] AS median_resolution_hours,
        COUNTIF(public_comment_count <= 2) AS single_cycle_solved,
        COUNTIF(resolution_sla_breached = TRUE) AS sla_breached,
        COUNTIF(resolution_sla_status IS NOT NULL OR resolution_sla_target_hours IS NOT NULL) AS sla_known
      FROM solved_labeled
      WHERE owner_email IS NOT NULL
      GROUP BY owner_email, group_key
    )
    SELECT
      owner_email,
      group_key,
      COALESCE(assigned_aggregates.group_name, outstanding_aggregates.group_name, solved_aggregates.group_name) AS group_name,
      COALESCE(assigned_aggregates.assignee_name, outstanding_aggregates.assignee_name, solved_aggregates.assignee_name) AS assignee_name,
      COALESCE(new_assigned_tickets, 0) AS new_assigned_tickets,
      COALESCE(solved_tickets, 0) AS same_day_solved_tickets,
      COALESCE(solved_tickets, 0) AS solved_tickets,
      COALESCE(outstanding_tickets, 0) AS outstanding_tickets,
      median_resolution_hours,
      COALESCE(single_cycle_solved, 0) AS single_cycle_solved,
      COALESCE(sla_breached, 0) AS sla_breached,
      COALESCE(sla_known, 0) AS sla_known
    FROM assigned_aggregates
    FULL OUTER JOIN outstanding_aggregates USING(owner_email, group_key)
    FULL OUTER JOIN solved_aggregates USING(owner_email, group_key)
    """


def _first_reply_sql() -> str:
    ticket_table = f"`{settings.google_cloud_project_id}.esip_reference.tickets`"
    comment_table = f"`{settings.google_cloud_project_id}.esip_reference.ticket_comments`"
    email_ok = _email_domain_condition("t.assignee_email")
    return f"""
    WITH created_tickets AS (
      SELECT
        id AS ticket_id,
        LOWER(TRIM(assignee_email)) AS owner_email,
        created_at
      FROM {ticket_table} t
      WHERE created_at >= @start_at
        AND created_at < @end_at
        AND {email_ok}
    ),
    first_replies AS (
      SELECT
        t.ticket_id,
        t.owner_email,
        t.created_at,
        MIN(c.created_at) AS first_public_reply_at
      FROM created_tickets t
      JOIN {comment_table} c
        ON c.ticket_id = t.ticket_id
       AND c.created_at >= t.created_at
       AND c.is_internal = FALSE
       AND c.author_is_agent = TRUE
      GROUP BY t.ticket_id, t.owner_email, t.created_at
    )
    SELECT
      owner_email,
      COUNT(*) AS first_reply_tickets,
      APPROX_QUANTILES(
        TIMESTAMP_DIFF(first_public_reply_at, created_at, MINUTE) / 60.0,
        100 IGNORE NULLS
      )[OFFSET(50)] AS median_first_public_reply_hours
    FROM first_replies
    GROUP BY owner_email
    """


def _customer_reply_sql() -> str:
    comment_table = f"`{settings.google_cloud_project_id}.esip_reference.ticket_comments`"
    email_ok = _email_domain_condition("next_author_email")
    return f"""
    WITH public_comments AS (
      SELECT
        ticket_id,
        created_at,
        comment_id,
        COALESCE(author_is_agent, FALSE) AS author_is_agent,
        LEAD(created_at) OVER (
          PARTITION BY ticket_id
          ORDER BY created_at, comment_id
        ) AS next_created_at,
        LEAD(COALESCE(author_is_agent, FALSE)) OVER (
          PARTITION BY ticket_id
          ORDER BY created_at, comment_id
        ) AS next_author_is_agent,
        LEAD(LOWER(TRIM(author_email))) OVER (
          PARTITION BY ticket_id
          ORDER BY created_at, comment_id
        ) AS next_author_email
      FROM {comment_table}
      WHERE is_internal = FALSE
        AND created_at >= TIMESTAMP_SUB(@start_at, INTERVAL 30 DAY)
        AND created_at < @end_at
    ),
    reply_cycles AS (
      SELECT
        next_author_email AS owner_email,
        TIMESTAMP_DIFF(next_created_at, created_at, MINUTE) / 60.0 AS reply_latency_hours
      FROM public_comments
      WHERE author_is_agent = FALSE
        AND next_author_is_agent = TRUE
        AND next_created_at >= @start_at
        AND next_created_at < @end_at
        AND {email_ok}
    )
    SELECT
      owner_email,
      COUNT(*) AS customer_reply_count,
      APPROX_QUANTILES(reply_latency_hours, 100 IGNORE NULLS)[OFFSET(50)] AS median_customer_reply_hours
    FROM reply_cycles
    GROUP BY owner_email
    """


def _phone_kpi_sql() -> str:
    contact_table = f"`{settings.google_cloud_project_id}.esip_reference.cxone_completed_contacts`"
    agent_table = f"`{settings.google_cloud_project_id}.esip_reference.cxone_agents`"
    contact_email_ok = _email_domain_condition("COALESCE(c.agent_email, a.email)")
    service_desk_skills = _sql_string_list(SERVICE_DESK_CXONE_SKILLS)
    return f"""
    WITH latest_contacts AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          *,
          ROW_NUMBER() OVER (PARTITION BY report_date, contact_id ORDER BY fetched_at DESC) AS row_num
        FROM {contact_table}
        WHERE report_date = DATE(@start_at, 'America/New_York')
      )
      WHERE row_num = 1
    ),
    latest_agents AS (
      SELECT * EXCEPT(row_num)
      FROM (
        SELECT
          *,
          ROW_NUMBER() OVER (PARTITION BY agent_id ORDER BY fetched_at DESC) AS row_num
        FROM {agent_table}
      )
      WHERE row_num = 1
    ),
    scoped AS (
      SELECT
        CASE
          WHEN {contact_email_ok} THEN LOWER(TRIM(COALESCE(c.agent_email, a.email)))
          WHEN NULLIF(TRIM(CONCAT(IFNULL(c.agent_first_name, ''), ' ', IFNULL(c.agent_last_name, ''))), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(CONCAT(IFNULL(c.agent_first_name, ''), ' ', IFNULL(c.agent_last_name, ''))), r'\\s+', ' ')))
          WHEN NULLIF(TRIM(IFNULL(c.team_name, '')), '') IS NOT NULL
            THEN CONCAT('{NAME_OWNER_PREFIX}', LOWER(REGEXP_REPLACE(TRIM(c.team_name), r'\\s+', ' ')))
          ELSE '{BLANK_OWNER_EMAIL}'
        END AS owner_email,
        COALESCE(
          NULLIF(TRIM(CONCAT(IFNULL(c.agent_first_name, ''), ' ', IFNULL(c.agent_last_name, ''))), ''),
          NULLIF(TRIM(a.display_name), ''),
          NULLIF(TRIM(c.team_name), ''),
          '{BLANK_OWNER_LABEL}'
        ) AS owner_name,
        c.agent_seconds
      FROM latest_contacts c
      LEFT JOIN latest_agents a
        ON a.agent_id = c.agent_id
      WHERE COALESCE(c.is_outbound, FALSE) = FALSE
        AND c.agent_id IS NOT NULL
        AND COALESCE(c.agent_seconds, 0) > 0
        AND c.skill_name IN ({service_desk_skills})
        AND (
          c.media_type_id = 4
          OR LOWER(COALESCE(c.media_type_name, '')) = 'call'
          OR LOWER(COALESCE(c.media_type_name, '')) LIKE '%phone%'
          OR LOWER(COALESCE(c.media_type_name, '')) LIKE '%voice%'
        )
    )
    SELECT
      owner_email,
      ARRAY_AGG(owner_name IGNORE NULLS LIMIT 1)[SAFE_OFFSET(0)] AS assignee_name,
      COUNT(*) AS inbound_calls,
      AVG(agent_seconds) AS avg_call_time_seconds
    FROM scoped
    WHERE owner_email IS NOT NULL
    GROUP BY owner_email
    """


def _coverage_sql() -> str:
    ticket_table = f"`{settings.google_cloud_project_id}.esip_reference.tickets`"
    comment_table = f"`{settings.google_cloud_project_id}.esip_reference.ticket_comments`"
    audit_table = f"`{settings.google_cloud_project_id}.esip_reference.zendesk_ticket_audit_events`"
    assignee_ok = _email_domain_condition("tickets.assignee_email")
    author_ok = _email_domain_condition("author_email")
    return f"""
    WITH solve_events AS (
      SELECT DISTINCT ticket_id
      FROM {audit_table}
      WHERE field_name = 'status'
        AND LOWER(COALESCE(value, '')) = 'solved'
        AND created_at >= @start_at
        AND created_at < @end_at
    ),
    ticket_scope AS (
      SELECT
        COUNT(*) AS relevant_tickets,
        COUNTIF({assignee_ok}) AS tickets_with_internal_assignee_email
      FROM {ticket_table} tickets
      LEFT JOIN solve_events
        ON solve_events.ticket_id = tickets.id
      WHERE
        (tickets.created_at >= @start_at AND tickets.created_at < @end_at)
        OR solve_events.ticket_id IS NOT NULL
        OR LOWER(COALESCE(tickets.status, '')) IN ('new', 'open', 'pending', 'hold')
    ),
    comment_scope AS (
      SELECT
        COUNTIF(is_internal = FALSE AND author_is_agent = TRUE) AS public_agent_replies,
        COUNTIF(is_internal = FALSE AND author_is_agent = TRUE AND {author_ok}) AS public_agent_replies_with_email
      FROM {comment_table}
      WHERE created_at >= @start_at
        AND created_at < @end_at
    )
    SELECT * FROM ticket_scope CROSS JOIN comment_scope
    """


def _load_identity_profiles() -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    try:
        db = firestore.get_db()
    except Exception as exc:
        logger.warning("Service Desk KPI could not initialize Firestore profiles: %s", exc)
        return profiles

    try:
        for doc in db.collection("profiles").stream(timeout=FIRESTORE_STREAM_TIMEOUT_SECONDS):
            data = doc.to_dict() or {}
            email = str(data.get("email") or "").strip().lower()
            if not _is_allowed_email(email):
                continue
            display = firestore.clean_auth_display_name(
                data.get("display_name") or data.get("name") or data.get("full_name") or "",
                email,
            )
            if display:
                profiles[email] = {
                    "email": email,
                    "display_name": display,
                    "active": False,
                    "role": "user",
                }
    except Exception as exc:
        logger.warning("Service Desk KPI could not read Firestore profiles: %s", exc)

    try:
        for doc in db.collection("users").stream(timeout=FIRESTORE_STREAM_TIMEOUT_SECONDS):
            email = str(doc.id or "").strip().lower()
            if not _is_allowed_email(email):
                continue
            data = doc.to_dict() or {}
            display = firestore.clean_auth_display_name(
                data.get("display_name")
                or data.get("name")
                or data.get("full_name")
                or profiles.get(email, {}).get("display_name")
                or "",
                email,
            )
            profiles[email] = {
                "email": email,
                "display_name": display,
                "active": bool(data.get("active", False)),
                "role": firestore._role_from_doc(data),
            }
    except Exception as exc:
        logger.warning("Service Desk KPI could not read Firestore users: %s", exc)

    return profiles


def _is_allowed_email(email: str) -> bool:
    safe = str(email or "").strip().lower()
    if "@" not in safe:
        return False
    if safe in LEGACY_IDENTITY_ALIASES:
        return True
    return safe.rsplit("@", 1)[1] in set(_allowed_domains())


def _profile_alias_map(profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    by_local: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for email, profile in profiles.items():
        if _is_allowed_email(email):
            by_local[email.split("@", 1)[0]].append((email, profile))

    aliases: dict[str, str] = {}
    for items in by_local.values():
        preferred = _preferred_alias_email(items)
        for email, _profile in items:
            aliases[email] = preferred
    for alias, canonical in LEGACY_IDENTITY_ALIASES.items():
        if alias in profiles or canonical in profiles:
            aliases[alias] = canonical
            aliases.setdefault(canonical, canonical)
    return aliases


def _preferred_alias_email(items: list[tuple[str, dict[str, Any]]]) -> str:
    def score(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
        email, profile = item
        domain = email.rsplit("@", 1)[1] if "@" in email else ""
        return (
            0 if profile.get("active") else 1,
            0 if domain == IDENTITY_DOMAIN else 1,
            email,
        )

    return sorted(items, key=score)[0][0]


def _canonical_email(email: str, aliases: dict[str, str]) -> str:
    safe = str(email or "").strip().lower()
    if safe == BLANK_OWNER_EMAIL:
        return BLANK_OWNER_EMAIL
    if safe.startswith(NAME_OWNER_PREFIX):
        return safe
    if safe in LEGACY_IDENTITY_ALIASES:
        return LEGACY_IDENTITY_ALIASES[safe]
    if not _is_allowed_email(safe):
        return ""
    return aliases.get(safe, safe)


def _display_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _queue_display_alias(value: str | None) -> str | None:
    safe = " ".join(str(value or "").strip().split())
    if not safe:
        return None
    return QUEUE_DISPLAY_ALIASES.get(_display_key(safe), safe)


def _group_key(value: str) -> str:
    return _display_key(value)


def _group_members_by_name() -> dict[str, tuple[str, ...]]:
    return {group: tuple(members) for group, members in SERVICE_DESK_GROUPS}


def _known_group_names() -> set[str]:
    return {group for group, _members in SERVICE_DESK_GROUPS}


def _known_person_display_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for _group, members in SERVICE_DESK_GROUPS:
        for member in members:
            names.setdefault(_display_key(member), member)
    for alias, canonical_name in PERSON_DISPLAY_ALIASES.items():
        canonical_key = _display_key(canonical_name)
        names.setdefault(alias, names.get(canonical_key, canonical_name))
    return names


def _group_member_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for group, members in SERVICE_DESK_GROUPS:
        lookup[_display_key(group)] = group
        for member in members:
            member_key = _display_key(member)
            lookup.setdefault(member_key, group)
    lookup.update({member: group for member, group in GROUP_PERSON_ONLY_FALLBACK.items()})
    lookup.update(GROUP_DISPLAY_ALIASES)
    for alias, canonical_name in PERSON_DISPLAY_ALIASES.items():
        group = lookup.get(_display_key(canonical_name))
        if group:
            lookup[alias] = group
    return lookup


def _canonical_group_name(value: str | None) -> str | None:
    display = _queue_display_alias(value)
    if not display:
        return None
    return GROUP_DISPLAY_ALIASES.get(_display_key(display), display)


def _known_group_name(value: str | None) -> str | None:
    group_name = _canonical_group_name(value)
    if group_name in _known_group_names():
        return group_name
    return None


def _group_for_row(row: dict[str, Any]) -> str | None:
    group_name = _known_group_name(str(row.get("group_name") or ""))
    if group_name:
        return group_name
    display = _display_key(str(row.get("display_name") or ""))
    email = _display_key(str(row.get("email") or ""))
    lookup = _group_member_lookup()
    return lookup.get(display) or lookup.get(email)


def _row_has_activity(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return any(
        int(row.get(key) or 0) > 0
        for key in (
            "inbound_calls",
            "new_assigned_tickets",
            "same_day_solved_tickets",
            "solved_tickets",
            "outstanding_tickets",
            "first_reply_tickets",
            "customer_reply_count",
        )
    )


def _group_rows(rows: list[dict[str, Any]], *, ticket_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {group: [] for group, _members in SERVICE_DESK_GROUPS}
    ungrouped: list[dict[str, Any]] = []

    if ticket_rows is None:
        for row in rows:
            group = _group_for_row(row)
            if group in grouped:
                grouped[group].append(row)
            elif _row_has_activity(row):
                ungrouped.append(row)
    else:
        for row in ticket_rows:
            source = _ticket_group_source_row(row)
            group = _group_for_row(source)
            if group in grouped:
                grouped[group].append(source)
            elif _row_has_activity(source):
                ungrouped.append(source)

        for row in rows:
            source = _person_only_group_source_row(row)
            group = _group_for_row(row)
            if group in grouped:
                grouped[group].append(source)
            elif _row_has_activity(source):
                ungrouped.append(source)

    members_by_group = _group_members_by_name()
    out: list[dict[str, Any]] = []
    for group, _members in SERVICE_DESK_GROUPS:
        out.append(_aggregate_group_row(
            group_name=group,
            source_rows=grouped[group],
            member_names=members_by_group[group],
            assignment_notes=[],
            unmapped=False,
        ))

    if ungrouped:
        out.append(_aggregate_group_row(
            group_name=UNMAPPED_GROUP_LABEL,
            source_rows=ungrouped,
            member_names=tuple(_person_name(row) for row in ungrouped),
            assignment_notes=["Rows not present in the provided group list; kept separate so group totals reconcile to the individual report."],
            unmapped=True,
        ))

    return out


def _group_sections(
    rows: list[dict[str, Any]],
    *,
    ticket_rows: list[dict[str, Any]],
    group_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    group_totals = {_display_key(str(row.get("display_name") or "")): row for row in group_rows}
    section_rows: dict[str, dict[str, dict[str, Any]]] = {group: {} for group, _members in SERVICE_DESK_GROUPS}
    section_rows[UNMAPPED_GROUP_LABEL] = {}

    for group, members in SERVICE_DESK_GROUPS:
        for member in members:
            key = _group_detail_key(group, member)
            section_rows[group][key] = _empty_group_source_row(display_name=member, group_name=group)
            section_rows[group][key]["email"] = key
            section_rows[group][key]["row_type"] = "assignee"

    for row in ticket_rows:
        group = _group_for_row(_ticket_group_source_row(row)) or UNMAPPED_GROUP_LABEL
        if group not in section_rows:
            group = UNMAPPED_GROUP_LABEL
        detail_name = _group_detail_display_name(row, group)
        key = _group_detail_key(group, detail_name)
        target = section_rows[group].setdefault(
            key,
            _empty_group_source_row(display_name=detail_name, group_name=group),
        )
        target["email"] = key
        target["row_type"] = "queue" if _group_detail_is_queue(row, group) else "assignee"
        _add_group_metrics(target, _ticket_group_source_row(row))

    for row in rows:
        source = _person_only_group_source_row(row)
        if not _row_has_activity(source):
            continue
        group = _group_for_row(row) or UNMAPPED_GROUP_LABEL
        if group not in section_rows:
            group = UNMAPPED_GROUP_LABEL
        detail_name = _person_name(row)
        key = _group_detail_key(group, detail_name)
        target = section_rows[group].setdefault(
            key,
            _empty_group_source_row(display_name=detail_name, group_name=group),
        )
        target["email"] = key
        target.setdefault("row_type", "assignee")
        _add_group_metrics(target, source)

    sections: list[dict[str, Any]] = []
    group_order = [group for group, _members in SERVICE_DESK_GROUPS]
    if group_totals.get(_display_key(UNMAPPED_GROUP_LABEL)) or section_rows[UNMAPPED_GROUP_LABEL]:
        group_order.append(UNMAPPED_GROUP_LABEL)

    for group in group_order:
        total = group_totals.get(_display_key(group)) or _aggregate_group_row(
            group_name=group,
            source_rows=[],
            member_names=_group_members_by_name().get(group, ()),
            assignment_notes=[],
            unmapped=(group == UNMAPPED_GROUP_LABEL),
        )
        details = list(section_rows.get(group, {}).values())
        details.sort(key=lambda row: (_group_detail_rank(group, _person_name(row)), _person_name(row).lower()))
        sections.append({
            "group_name": group,
            "group_key": _group_key(group),
            "total": total,
            "rows": details,
        })

    return sections


def _group_detail_key(group_name: str, display_name: str) -> str:
    return f"__group_detail__:{_group_key(group_name)}:{_display_key(display_name)}"


def _group_detail_display_name(row: dict[str, Any], group_name: str) -> str:
    group_display = _canonical_group_name(str(row.get("group_name") or "")) or group_name
    assignee = _queue_display_alias(str(row.get("assignee_name") or row.get("display_name") or "")) or ""
    assignee = PERSON_DISPLAY_ALIASES.get(_display_key(assignee), assignee)
    if _group_detail_is_queue(row, group_display):
        if group_display in {"Menu Maintenance", "Operations Support"}:
            return group_display
        return f"{group_display} queue" if group_display == "Field Services Partner" else BLANK_OWNER_LABEL
    return assignee or BLANK_OWNER_LABEL


def _group_detail_is_queue(row: dict[str, Any], group_name: str) -> bool:
    assignee = _display_key(str(row.get("assignee_name") or row.get("display_name") or ""))
    owner_email = str(row.get("owner_email") or row.get("email") or "").strip().lower()
    group_key = _display_key(group_name)
    return (
        owner_email == BLANK_OWNER_EMAIL
        or (owner_email.startswith(NAME_OWNER_PREFIX) and assignee in {group_key, _display_key(_queue_display_alias(group_name) or group_name)})
    )


def _group_detail_rank(group_name: str, display_name: str) -> tuple[int, int]:
    members = list(_group_members_by_name().get(group_name, ()))
    normalized = _display_key(display_name)
    if group_name == "Menu Maintenance" and normalized == "menu maintenance":
        return (0, 0)
    for index, member in enumerate(members):
        if _display_key(member) == normalized:
            return (0, index + (1 if group_name == "Menu Maintenance" else 0))
    if normalized == _display_key(BLANK_OWNER_LABEL) or normalized.endswith(" queue"):
        return (1, 0)
    return (2, len(members) + 1)


def _add_group_metrics(target: dict[str, Any], source: dict[str, Any]) -> None:
    previous_calls = int(target.get("inbound_calls") or 0)
    inbound_calls = int(source.get("inbound_calls") or 0)
    target["inbound_calls"] = previous_calls + inbound_calls
    avg_seconds = _round(source.get("avg_call_time_seconds"))
    if avg_seconds is not None and inbound_calls > 0:
        if target.get("avg_call_time_seconds") is None or previous_calls <= 0:
            target["avg_call_time_seconds"] = avg_seconds
        else:
            target["avg_call_time_seconds"] = round(
                ((float(target["avg_call_time_seconds"]) * previous_calls) + (avg_seconds * inbound_calls))
                / (previous_calls + inbound_calls),
                2,
            )

    for key in (
        "new_assigned_tickets",
        "same_day_solved_tickets",
        "solved_tickets",
        "outstanding_tickets",
        "single_cycle_solved",
        "single_cycle_total",
        "sla_breached",
        "sla_known",
        "first_reply_tickets",
        "customer_reply_count",
    ):
        target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)

    target["median_resolution_hours"] = _coalesce_metric(
        target.get("median_resolution_hours"),
        _round(source.get("median_resolution_hours")),
    )
    target["median_first_public_reply_hours"] = _coalesce_metric(
        target.get("median_first_public_reply_hours"),
        _round(source.get("median_first_public_reply_hours")),
    )
    target["median_customer_reply_hours"] = _coalesce_metric(
        target.get("median_customer_reply_hours"),
        _round(source.get("median_customer_reply_hours")),
    )
    target["single_cycle_solve_rate_pct"] = _pct(target.get("single_cycle_solved"), target.get("single_cycle_total"))
    target["sla_breach_rate_pct"] = _pct(target.get("sla_breached"), target.get("sla_known"))


def _empty_group_source_row(*, display_name: str, group_name: str | None = None) -> dict[str, Any]:
    return {
        "email": f"{NAME_OWNER_PREFIX}{_display_key(display_name)}",
        "display_name": display_name,
        "group_name": group_name,
        "new_assigned_tickets": 0,
        "same_day_solved_tickets": 0,
        "solved_tickets": 0,
        "outstanding_tickets": 0,
        "median_resolution_hours": None,
        "single_cycle_solved": 0,
        "single_cycle_total": 0,
        "single_cycle_solve_rate_pct": 0,
        "sla_breached": 0,
        "sla_known": 0,
        "sla_breach_rate_pct": 0,
        "first_reply_tickets": 0,
        "median_first_public_reply_hours": None,
        "customer_reply_count": 0,
        "median_customer_reply_hours": None,
        "inbound_calls": 0,
        "avg_call_time_seconds": None,
    }


def _ticket_group_source_row(row: dict[str, Any]) -> dict[str, Any]:
    group_name = _known_group_name(str(row.get("group_name") or ""))
    assignee_name = str(row.get("assignee_name") or row.get("display_name") or "").strip()
    assignee_name = PERSON_DISPLAY_ALIASES.get(_display_key(assignee_name), assignee_name)
    display_name = group_name or assignee_name or BLANK_OWNER_LABEL
    source = _empty_group_source_row(display_name=display_name, group_name=group_name)
    source["email"] = str(row.get("owner_email") or row.get("email") or source["email"])
    source["new_assigned_tickets"] = int(row.get("new_assigned_tickets") or 0)
    source["same_day_solved_tickets"] = int(row.get("same_day_solved_tickets") or 0)
    source["solved_tickets"] = int(row.get("solved_tickets") or 0)
    source["outstanding_tickets"] = int(row.get("outstanding_tickets") or 0)
    source["median_resolution_hours"] = _round(row.get("median_resolution_hours"))
    source["single_cycle_solved"] = int(row.get("single_cycle_solved") or 0)
    source["single_cycle_total"] = int(row.get("solved_tickets") or 0)
    source["single_cycle_solve_rate_pct"] = _pct(source["single_cycle_solved"], source["single_cycle_total"])
    source["sla_breached"] = int(row.get("sla_breached") or 0)
    source["sla_known"] = int(row.get("sla_known") or 0)
    source["sla_breach_rate_pct"] = _pct(source["sla_breached"], source["sla_known"])
    return source


def _person_only_group_source_row(row: dict[str, Any]) -> dict[str, Any]:
    source = _empty_group_source_row(display_name=_person_name(row))
    source["email"] = str(row.get("email") or source["email"])
    source["first_reply_tickets"] = int(row.get("first_reply_tickets") or 0)
    source["median_first_public_reply_hours"] = _round(row.get("median_first_public_reply_hours"))
    source["customer_reply_count"] = int(row.get("customer_reply_count") or 0)
    source["median_customer_reply_hours"] = _round(row.get("median_customer_reply_hours"))
    source["inbound_calls"] = int(row.get("inbound_calls") or 0)
    source["avg_call_time_seconds"] = _round(row.get("avg_call_time_seconds"))
    return source


def _aggregate_group_row(
    *,
    group_name: str,
    source_rows: list[dict[str, Any]],
    member_names: tuple[str, ...],
    assignment_notes: list[str],
    unmapped: bool,
) -> dict[str, Any]:
    summary = _summary(source_rows)
    active_rows = [row for row in source_rows if _row_has_activity(row)]
    group_key = UNMAPPED_GROUP_KEY if unmapped else _group_key(group_name)
    return {
        "email": f"__group__:{group_key}",
        "group_key": group_key,
        "display_name": group_name,
        "member_names": list(member_names),
        "mapped_row_names": [_person_name(row) for row in source_rows],
        "active_rows": len(active_rows),
        "unmapped": unmapped,
        "assignment_notes": assignment_notes,
        "inbound_calls": summary["inbound_calls"],
        "avg_call_time_seconds": summary["avg_call_time_seconds"],
        "new_assigned_tickets": summary["new_assigned_tickets"],
        "same_day_solved_tickets": summary["same_day_solved_tickets"],
        "solved_tickets": summary["solved_tickets"],
        "outstanding_tickets": summary["outstanding_tickets"],
        "median_resolution_hours": _weighted_median_like(source_rows, "median_resolution_hours", "solved_tickets"),
        "single_cycle_solved": summary["single_cycle_solved"],
        "single_cycle_total": summary["single_cycle_total"],
        "single_cycle_solve_rate_pct": summary["single_cycle_solve_rate_pct"],
        "sla_breached": summary["sla_breached"],
        "sla_known": summary["sla_known"],
        "sla_breach_rate_pct": summary["sla_breach_rate_pct"],
        "first_reply_tickets": sum(int(row.get("first_reply_tickets") or 0) for row in source_rows),
        "median_first_public_reply_hours": summary["median_first_public_reply_hours"],
        "customer_reply_count": summary["customer_replies"],
        "median_customer_reply_hours": summary["median_customer_reply_hours"],
    }


def _person_name(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("email") or "Unknown").strip() or "Unknown"


def _grouping_metadata(group_rows: list[dict[str, Any]]) -> dict[str, Any]:
    unmapped = next((row for row in group_rows if row.get("unmapped")), None)
    multi_group_members = [
        {
            "name": "Sofia Reyes",
            "groups": list(GROUP_MULTI_GROUP_MEMBERS["sofia reyes"]),
            "rule": "Ticket metrics use the Zendesk group or queue on each ticket.",
        },
    ]
    warnings: list[str] = []
    if unmapped and _row_has_activity(unmapped):
        warnings.append("Some active individual/queue rows are not in the provided group list and are shown under Unmapped / queues.")
    return {
        "source": "fixed_service_desk_group_list_2026_06_02",
        "multi_group_members": multi_group_members,
        "unmapped_active_rows": int((unmapped or {}).get("active_rows") or 0),
        "warnings": warnings,
    }


def _profile_display_alias_map(profiles: dict[str, dict[str, Any]], aliases: dict[str, str]) -> dict[str, str]:
    exact: dict[str, str] = {}
    first_name_candidates: dict[str, set[str]] = defaultdict(set)

    def add(display_name: str, email: str) -> None:
        display = _display_key(display_name)
        if not display:
            return
        canonical = aliases.get(email, email)
        exact[display] = canonical
        first = display.split(" ", 1)[0]
        if len(first) >= 3:
            first_name_candidates[first].add(canonical)

    for email, profile in profiles.items():
        add(str(profile.get("display_name") or ""), email)

    for first, candidates in first_name_candidates.items():
        if len(candidates) == 1 and first not in exact:
            exact[first] = next(iter(candidates))

    return exact


def _row_owner_emails(*row_groups: list[dict[str, Any]]) -> set[str]:
    emails: set[str] = set()
    for rows in row_groups:
        for row in rows:
            email = str(row.get("owner_email") or "").strip().lower()
            if _is_allowed_email(email):
                emails.add(email)
    return emails


def _augment_aliases_with_rows(
    aliases: dict[str, str],
    profiles: dict[str, dict[str, Any]],
    *row_groups: list[dict[str, Any]],
) -> dict[str, str]:
    by_local: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for email, profile in profiles.items():
        if _is_allowed_email(email):
            by_local[email.split("@", 1)[0]][email] = profile
    for email in _row_owner_emails(*row_groups):
        by_local[email.split("@", 1)[0]].setdefault(email, profiles.get(email, {}))

    out = dict(aliases)
    for items_by_email in by_local.values():
        items = list(items_by_email.items())
        if len(items) <= 1:
            continue
        preferred = _preferred_alias_email(items)
        for email, _profile in items:
            out[email] = preferred
    return out


def _ticket_row_display_alias_map(ticket_rows: list[dict[str, Any]], aliases: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    first_name_candidates: dict[str, set[str]] = defaultdict(set)
    for row in ticket_rows:
        owner_email = str(row.get("owner_email") or "").strip().lower()
        if not _is_allowed_email(owner_email):
            continue
        display = _display_key(str(row.get("assignee_name") or ""))
        if display:
            canonical = aliases.get(owner_email, owner_email)
            out[display] = canonical
            first = display.split(" ", 1)[0]
            if len(first) >= 3:
                first_name_candidates[first].add(canonical)
    for first, candidates in first_name_candidates.items():
        if len(candidates) == 1 and first not in out:
            out[first] = next(iter(candidates))
    return out


def _known_person_display_alias_map(existing_aliases: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for alias, canonical_name in PERSON_DISPLAY_ALIASES.items():
        canonical_key = _display_key(canonical_name)
        out[alias] = existing_aliases.get(canonical_key, f"{NAME_OWNER_PREFIX}{canonical_key}")
    return out


def _known_person_name_for_owner(canonical_owner: str) -> str | None:
    if not canonical_owner.startswith(NAME_OWNER_PREFIX):
        return None
    key = canonical_owner.removeprefix(NAME_OWNER_PREFIX)
    return _known_person_display_names().get(key)


def _merge_rows(
    *,
    ticket_rows: list[dict[str, Any]],
    first_reply_rows: list[dict[str, Any]],
    customer_reply_rows: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    aliases: dict[str, str],
    phone_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    users: dict[str, dict[str, Any]] = {}
    aliases = _augment_aliases_with_rows(
        aliases,
        profiles,
        ticket_rows,
        first_reply_rows,
        customer_reply_rows,
        phone_rows or [],
    )
    display_aliases = _profile_display_alias_map(profiles, aliases)
    display_aliases.update(_ticket_row_display_alias_map(ticket_rows, aliases))
    display_aliases.update(_known_person_display_alias_map(display_aliases))

    def ensure(email: str, assignee_name: str | None = None) -> dict[str, Any]:
        canonical = _canonical_email(email, aliases)
        if not canonical:
            return {}
        if canonical.startswith(NAME_OWNER_PREFIX):
            display_key = canonical.removeprefix(NAME_OWNER_PREFIX)
            queue_alias = QUEUE_DISPLAY_ALIASES.get(display_key)
            if queue_alias:
                assignee_name = queue_alias
                canonical = f"{NAME_OWNER_PREFIX}{_display_key(queue_alias)}"
            else:
                canonical = display_aliases.get(display_key, canonical)
        profile = profiles.get(canonical, {})
        queue_alias = QUEUE_DISPLAY_ALIASES.get(_display_key(assignee_name or ""))
        known_person_name = _known_person_name_for_owner(canonical)
        display = (
            BLANK_OWNER_LABEL if canonical == BLANK_OWNER_EMAIL else
            profile.get("display_name")
            or queue_alias
            or known_person_name
            or _queue_display_alias(assignee_name)
            or (canonical.removeprefix(NAME_OWNER_PREFIX).title() if canonical.startswith(NAME_OWNER_PREFIX) else "")
            or firestore.clean_auth_display_name("", canonical)
            or ""
        )
        return users.setdefault(
            canonical,
            {
                "email": canonical,
                "display_name": display,
                "new_assigned_tickets": 0,
                "same_day_solved_tickets": 0,
                "solved_tickets": 0,
                "outstanding_tickets": 0,
                "median_resolution_hours": None,
                "single_cycle_solved": 0,
                "single_cycle_total": 0,
                "single_cycle_solve_rate_pct": 0,
                "sla_breached": 0,
                "sla_known": 0,
                "sla_breach_rate_pct": 0,
                "first_reply_tickets": 0,
                "median_first_public_reply_hours": None,
                "customer_reply_count": 0,
                "median_customer_reply_hours": None,
                "inbound_calls": 0,
                "avg_call_time_seconds": None,
            },
        )

    for row in ticket_rows:
        item = ensure(str(row.get("owner_email") or ""), str(row.get("assignee_name") or ""))
        if not item:
            continue
        item["new_assigned_tickets"] += int(row.get("new_assigned_tickets") or 0)
        item["same_day_solved_tickets"] += int(row.get("same_day_solved_tickets") or 0)
        item["solved_tickets"] += int(row.get("solved_tickets") or 0)
        item["outstanding_tickets"] += int(row.get("outstanding_tickets") or 0)
        item["single_cycle_solved"] += int(row.get("single_cycle_solved") or 0)
        item["single_cycle_total"] = item["solved_tickets"]
        item["sla_breached"] += int(row.get("sla_breached") or 0)
        item["sla_known"] += int(row.get("sla_known") or 0)
        item["median_resolution_hours"] = _coalesce_metric(
            item["median_resolution_hours"],
            _round(row.get("median_resolution_hours")),
        )
        item["single_cycle_solve_rate_pct"] = _pct(item["single_cycle_solved"], item["solved_tickets"])
        item["sla_breach_rate_pct"] = _pct(item["sla_breached"], item["sla_known"])

    for row in first_reply_rows:
        item = ensure(str(row.get("owner_email") or ""))
        if not item:
            continue
        item["first_reply_tickets"] += int(row.get("first_reply_tickets") or 0)
        item["median_first_public_reply_hours"] = _coalesce_metric(
            item["median_first_public_reply_hours"],
            _round(row.get("median_first_public_reply_hours")),
        )

    for row in customer_reply_rows:
        item = ensure(str(row.get("owner_email") or ""))
        if not item:
            continue
        item["customer_reply_count"] += int(row.get("customer_reply_count") or 0)
        item["median_customer_reply_hours"] = _coalesce_metric(
            item["median_customer_reply_hours"],
            _round(row.get("median_customer_reply_hours")),
        )

    for row in phone_rows or []:
        item = ensure(str(row.get("owner_email") or ""), str(row.get("assignee_name") or ""))
        if not item:
            continue
        inbound_calls = int(row.get("inbound_calls") or 0)
        avg_seconds = _round(row.get("avg_call_time_seconds"))
        previous_calls = int(item["inbound_calls"] or 0)
        item["inbound_calls"] += inbound_calls
        if avg_seconds is not None and inbound_calls > 0:
            if item["avg_call_time_seconds"] is None or previous_calls <= 0:
                item["avg_call_time_seconds"] = avg_seconds
            else:
                item["avg_call_time_seconds"] = round(
                    ((float(item["avg_call_time_seconds"]) * previous_calls) + (avg_seconds * inbound_calls))
                    / (previous_calls + inbound_calls),
                    2,
                )

    out = list(users.values())
    out.sort(
        key=lambda item: (
            item["inbound_calls"],
            item["solved_tickets"],
            item["same_day_solved_tickets"],
            item["new_assigned_tickets"],
            item["customer_reply_count"],
            item["outstanding_tickets"],
            item["display_name"] or item["email"],
        ),
        reverse=True,
    )
    return out[:250]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    active = [
        row for row in rows
        if row["new_assigned_tickets"] or row["solved_tickets"] or row["customer_reply_count"] or row["outstanding_tickets"] or row.get("inbound_calls")
    ]
    solved = sum(row["solved_tickets"] for row in rows)
    single_cycle = sum(row["single_cycle_solved"] for row in rows)
    sla_breached = sum(row["sla_breached"] for row in rows)
    sla_known = sum(row["sla_known"] for row in rows)
    inbound_calls = sum(int(row.get("inbound_calls") or 0) for row in rows)
    return {
        "active_engineers": len(active),
        "inbound_calls": inbound_calls,
        "avg_call_time_seconds": _weighted_average(rows, "avg_call_time_seconds", "inbound_calls"),
        "new_assigned_tickets": sum(row["new_assigned_tickets"] for row in rows),
        "same_day_solved_tickets": sum(row["same_day_solved_tickets"] for row in rows),
        "solved_tickets": solved,
        "outstanding_tickets": sum(row["outstanding_tickets"] for row in rows),
        "customer_replies": sum(row["customer_reply_count"] for row in rows),
        "median_first_public_reply_hours": _weighted_median_like(rows, "median_first_public_reply_hours", "first_reply_tickets"),
        "median_customer_reply_hours": _weighted_median_like(rows, "median_customer_reply_hours", "customer_reply_count"),
        "single_cycle_solved": single_cycle,
        "single_cycle_total": solved,
        "single_cycle_solve_rate_pct": _pct(single_cycle, solved),
        "sla_breached": sla_breached,
        "sla_known": sla_known,
        "sla_breach_rate_pct": _pct(sla_breached, sla_known),
    }


def _coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row = rows[0] if rows else {}
    relevant_tickets = int(row.get("relevant_tickets") or 0)
    assignee_covered = int(row.get("tickets_with_internal_assignee_email") or 0)
    public_replies = int(row.get("public_agent_replies") or 0)
    reply_covered = int(row.get("public_agent_replies_with_email") or 0)
    return {
        "relevant_tickets": relevant_tickets,
        "tickets_with_internal_assignee_email": assignee_covered,
        "assignee_email_coverage_pct": _pct(assignee_covered, relevant_tickets),
        "public_agent_replies": public_replies,
        "public_agent_replies_with_email": reply_covered,
        "comment_author_email_coverage_pct": _pct(reply_covered, public_replies),
    }


def _warnings(coverage: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if coverage["relevant_tickets"] and coverage["assignee_email_coverage_pct"] < 90:
        warnings.append("Owner email coverage is limited. Some ticket-owner rows and totals may be incomplete.")
    if coverage["public_agent_replies"] and coverage["comment_author_email_coverage_pct"] < 90:
        warnings.append("Reply author email coverage is limited. Reply-time rows may be incomplete.")
    telephony = coverage.get("telephony") or {}
    if telephony.get("warning"):
        warnings.append(str(telephony["warning"]))
    elif telephony.get("available") and int(telephony.get("contact_count") or 0) == 0:
        warnings.append("CXone telephony data loaded but no completed contacts were found for this date.")
    return warnings


def _pct(part: Any, total: Any) -> int:
    try:
        numerator = float(part or 0)
        denominator = float(total or 0)
    except Exception:
        return 0
    if denominator <= 0:
        return 0
    return int(round((numerator / denominator) * 100))


def _round(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _coalesce_metric(current: float | None, new: float | None) -> float | None:
    return current if current is not None else new


def _weighted_median_like(rows: list[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    values: list[float] = []
    for row in rows:
        value = row.get(value_key)
        weight = int(row.get(weight_key) or 0)
        if value is None or weight <= 0:
            continue
        values.extend([float(value)] * min(weight, 50))
    if not values:
        return None
    values.sort()
    return round(values[len(values) // 2], 2)


def _weighted_average(rows: list[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    total_weight = 0
    weighted_sum = 0.0
    for row in rows:
        value = row.get(value_key)
        weight = int(row.get(weight_key) or 0)
        if value is None or weight <= 0:
            continue
        total_weight += weight
        weighted_sum += float(value) * weight
    if total_weight <= 0:
        return None
    return round(weighted_sum / total_weight, 2)
