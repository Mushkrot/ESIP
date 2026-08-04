#!/usr/bin/env python3
"""Add and backfill derived ticket SLA fields in BigQuery.

Default behavior only migrates the `tickets` schema. Use `--ticket-id` for a
small targeted backfill, or `--since-days` for a controlled recent cohort.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Any

import requests
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.services.bigquery import _get_client  # noqa: E402
from app.services.sla import build_sla_fields  # noqa: E402


SLA_COLUMNS: tuple[bigquery.SchemaField, ...] = (
    bigquery.SchemaField("priority", "STRING"),
    bigquery.SchemaField("ticket_type", "STRING"),
    bigquery.SchemaField("ticket_form_id", "INT64"),
    bigquery.SchemaField("ticket_form_name", "STRING"),
    bigquery.SchemaField("sla_policy_name", "STRING"),
    bigquery.SchemaField("resolution_sla_target_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_elapsed_hours", "FLOAT64"),
    bigquery.SchemaField("resolution_sla_breached", "BOOL"),
    bigquery.SchemaField("resolution_sla_status", "STRING"),
)


def ensure_columns(client: bigquery.Client) -> list[str]:
    table_id = f"{settings.google_cloud_project_id}.esip_reference.tickets"
    table = client.get_table(table_id)
    existing = {field.name for field in table.schema}
    missing = [field for field in SLA_COLUMNS if field.name not in existing]
    if not missing:
        return []
    table.schema = [*table.schema, *missing]
    client.update_table(table, ["schema"])
    return [field.name for field in missing]


def _zd_get(path: str) -> dict[str, Any]:
    url = f"{settings.zendesk_base_url}{path}"
    response = requests.get(url, auth=settings.zendesk_auth, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_ticket(ticket_id: int) -> dict[str, Any]:
    return _zd_get(f"/tickets/{ticket_id}.json").get("ticket") or {}


def fetch_ticket_form_name(form_id: int | None, cache: dict[int, str | None]) -> str | None:
    if not form_id:
        return None
    if form_id in cache:
        return cache[form_id]
    try:
        form = _zd_get(f"/ticket_forms/{form_id}.json").get("ticket_form") or {}
        cache[form_id] = form.get("display_name") or form.get("name")
    except Exception:
        cache[form_id] = None
    return cache[form_id]


def recent_ticket_ids(client: bigquery.Client, since_days: int) -> list[int]:
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    query = f"""
    SELECT id
    FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
    WHERE updated_at >= @since
    ORDER BY updated_at DESC
    """
    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("since", "TIMESTAMP", since)]
        ),
    )
    return [int(row.id) for row in job.result()]


def update_ticket_sla(
    client: bigquery.Client,
    ticket_id: int,
    form_cache: dict[int, str | None],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    ticket = fetch_ticket(ticket_id)
    if not ticket:
        raise RuntimeError(f"Zendesk ticket {ticket_id} not found")
    form_name = fetch_ticket_form_name(ticket.get("ticket_form_id"), form_cache)
    fields = build_sla_fields(ticket, ticket_form_name=form_name)
    params = [
        bigquery.ScalarQueryParameter("ticket_id", "INT64", ticket_id),
        bigquery.ScalarQueryParameter("priority", "STRING", fields["priority"]),
        bigquery.ScalarQueryParameter("ticket_type", "STRING", fields["ticket_type"]),
        bigquery.ScalarQueryParameter("ticket_form_id", "INT64", fields["ticket_form_id"]),
        bigquery.ScalarQueryParameter("ticket_form_name", "STRING", fields["ticket_form_name"]),
        bigquery.ScalarQueryParameter("sla_policy_name", "STRING", fields["sla_policy_name"]),
        bigquery.ScalarQueryParameter("target_hours", "FLOAT64", fields["resolution_sla_target_hours"]),
        bigquery.ScalarQueryParameter("elapsed_hours", "FLOAT64", fields["resolution_sla_elapsed_hours"]),
        bigquery.ScalarQueryParameter("breached", "BOOL", fields["resolution_sla_breached"]),
        bigquery.ScalarQueryParameter("sla_status", "STRING", fields["resolution_sla_status"]),
    ]
    sql = f"""
    UPDATE `{settings.google_cloud_project_id}.esip_reference.tickets`
    SET
      priority = @priority,
      ticket_type = @ticket_type,
      ticket_form_id = @ticket_form_id,
      ticket_form_name = @ticket_form_name,
      sla_policy_name = @sla_policy_name,
      resolution_sla_target_hours = @target_hours,
      resolution_sla_elapsed_hours = @elapsed_hours,
      resolution_sla_breached = @breached,
      resolution_sla_status = @sla_status
    WHERE id = @ticket_id
    """
    if not dry_run:
        client.query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate/backfill derived ticket SLA fields")
    parser.add_argument("--ticket-id", type=int, action="append", default=[])
    parser.add_argument("--since-days", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = _get_client()
    added = ensure_columns(client)
    print(f"schema_added={added}")

    ids = list(dict.fromkeys(args.ticket_id))
    if args.since_days:
        ids.extend(recent_ticket_ids(client, args.since_days))
        ids = list(dict.fromkeys(ids))

    form_cache: dict[int, str | None] = {}
    for ticket_id in ids:
        fields = update_ticket_sla(client, ticket_id, form_cache, dry_run=args.dry_run)
        elapsed = fields["resolution_sla_elapsed_hours"]
        elapsed_text = f"{elapsed:.1f}" if elapsed is not None else "None"
        print(
            f"ticket={ticket_id} priority={fields['priority']} "
            f"target={fields['resolution_sla_target_hours']} "
            f"elapsed={elapsed_text} "
            f"status={fields['resolution_sla_status']} dry_run={args.dry_run}"
        )


if __name__ == "__main__":
    main()
