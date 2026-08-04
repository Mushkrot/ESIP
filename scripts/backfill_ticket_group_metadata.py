#!/usr/bin/env python3
"""Metadata-only backfill of Zendesk assignment group fields into BQ tickets.

This updates only:
- tickets.assignee_group_id
- tickets.assignee_group_name

It does not touch updated_at, exported_at, full_text, comments, attachments, OCR,
embeddings, sync cursor, or Vertex indexes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from google.cloud import bigquery  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import bigquery as bq_service  # noqa: E402


MAX_RETRIES = 3
SHOW_MANY_BATCH = 100


def _zd_get(path_or_url: str, params: dict | None = None) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"{settings.zendesk_base_url}{path_or_url}"
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(url, params=params, auth=settings.zendesk_auth, timeout=30)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            print(f"Zendesk rate limit, sleeping {wait}s")
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Zendesk request failed after {MAX_RETRIES} retries: {url}")


def fetch_groups() -> dict[int, str]:
    groups: dict[int, str] = {}
    url = "/groups.json"
    while url:
        data = _zd_get(url)
        for group in data.get("groups", []):
            group_id = group.get("id")
            name = group.get("name") or ""
            if group_id:
                groups[int(group_id)] = name
        url = data.get("next_page")
    return groups


def _require_group_columns() -> None:
    client = bq_service._get_client()
    table = client.get_table(f"{settings.google_cloud_project_id}.esip_reference.tickets")
    columns = {field.name for field in table.schema}
    missing = {"assignee_group_id", "assignee_group_name"} - columns
    if missing:
        raise SystemExit(
            "Missing BQ tickets columns: "
            f"{sorted(missing)}. Run scripts/backend_python.sh "
            "scripts/migrate_ticket_group_fields.py --apply first."
        )


def fetch_ticket_ids(scope: str, limit: int | None, only_missing: bool) -> list[int]:
    client = bq_service._get_client()
    filters = []
    if scope == "active":
        filters.append("status IN ('new', 'open', 'pending', 'hold')")
    if only_missing:
        filters.append("(assignee_group_id IS NULL OR assignee_group_name IS NULL)")
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT id
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
        {where}
        ORDER BY updated_at DESC
        {limit_sql}
    """
    return [int(row.id) for row in client.query(sql).result()]


def fetch_ticket_group_rows(ticket_ids: list[int], groups: dict[int, str]) -> tuple[list[dict], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    for idx in range(0, len(ticket_ids), SHOW_MANY_BATCH):
        batch = ticket_ids[idx:idx + SHOW_MANY_BATCH]
        data = _zd_get("/tickets/show_many.json", params={"ids": ",".join(str(tid) for tid in batch)})
        returned = {int(ticket["id"]): ticket for ticket in data.get("tickets", [])}
        missing = sorted(set(batch) - set(returned))
        if missing:
            warnings.append(f"Zendesk did not return {len(missing)} ticket ids, sample={missing[:5]}")
        for ticket in returned.values():
            group_id = ticket.get("group_id")
            if not group_id:
                continue
            group_id = int(group_id)
            rows.append({
                "ticket_id": int(ticket["id"]),
                "assignee_group_id": group_id,
                "assignee_group_name": groups.get(group_id),
                "fetched_at": fetched_at,
            })
        time.sleep(0.1)
    return rows, warnings


def apply_rows(rows: list[dict]) -> None:
    if not rows:
        return
    client = bq_service._get_client()
    temp_table = (
        f"{settings.google_cloud_project_id}.esip_reference._ticket_group_backfill_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    schema = [
        bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
        bigquery.SchemaField("assignee_group_id", "INT64"),
        bigquery.SchemaField("assignee_group_name", "STRING"),
        bigquery.SchemaField("fetched_at", "TIMESTAMP"),
    ]
    job = client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"),
    )
    job.result(timeout=600)
    merge_sql = f"""
        MERGE `{settings.google_cloud_project_id}.esip_reference.tickets` T
        USING `{temp_table}` S
        ON T.id = S.ticket_id
        WHEN MATCHED THEN UPDATE SET
          assignee_group_id = S.assignee_group_id,
          assignee_group_name = S.assignee_group_name
    """
    try:
        client.query(merge_sql).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def summarize_bq() -> dict:
    client = bq_service._get_client()
    sql = f"""
        SELECT
          COUNT(*) AS total,
          COUNTIF(status IN ('new', 'open', 'pending', 'hold')) AS active_total,
          COUNTIF(assignee_group_id IS NOT NULL) AS with_group_id,
          COUNTIF(assignee_group_name IS NOT NULL) AS with_group_name,
          COUNTIF(LOWER(assignee_group_name) LIKE '%field_services_partner%') AS field_services_partner_total,
          COUNTIF(status IN ('new', 'open', 'pending', 'hold')
                  AND LOWER(assignee_group_name) LIKE '%field_services_partner%') AS field_services_partner_active
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
    """
    return dict(next(iter(client.query(sql).result())).items())


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill BQ ticket assignment group metadata.")
    parser.add_argument("--scope", choices=["active", "all"], default="active")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--include-existing", action="store_true", help="Also refresh rows that already have group fields.")
    parser.add_argument("--apply", action="store_true", help="Apply BQ MERGE. Omit for dry run.")
    args = parser.parse_args()

    _require_group_columns()
    before = summarize_bq()
    ticket_ids = fetch_ticket_ids(args.scope, args.limit, only_missing=not args.include_existing)
    print({"phase": "selected", "scope": args.scope, "apply": args.apply, "ticket_ids": len(ticket_ids), "before": before}, flush=True)
    if not ticket_ids:
        return 0

    groups = fetch_groups()
    all_warnings: list[str] = []
    total_rows = 0
    total_with_group_name = 0
    sample: list[dict] = []
    chunk_size = max(1, int(args.chunk_size or 5000))
    chunks = [ticket_ids[i:i + chunk_size] for i in range(0, len(ticket_ids), chunk_size)]
    for index, chunk in enumerate(chunks, 1):
        rows, warnings = fetch_ticket_group_rows(chunk, groups)
        all_warnings.extend(warnings)
        with_group_name = sum(1 for row in rows if row.get("assignee_group_name"))
        total_rows += len(rows)
        total_with_group_name += with_group_name
        if not sample:
            sample = rows[:5]
        print({
            "phase": "chunk_fetched",
            "chunk": index,
            "chunks": len(chunks),
            "ticket_ids": len(chunk),
            "rows": len(rows),
            "rows_with_group_name": with_group_name,
            "warnings": warnings[:3],
        }, flush=True)
        if args.apply:
            apply_rows(rows)
            print({"phase": "chunk_applied", "chunk": index, "chunks": len(chunks)}, flush=True)

    print({
        "phase": "fetched",
        "groups": len(groups),
        "rows": total_rows,
        "rows_with_group_name": total_with_group_name,
        "warnings": all_warnings[:10],
    }, flush=True)
    if args.apply:
        print({"phase": "applied", "after": summarize_bq()}, flush=True)
    else:
        print({"phase": "dry_run", "sample": sample}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
