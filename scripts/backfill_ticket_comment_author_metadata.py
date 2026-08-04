#!/usr/bin/env python3
"""Metadata-only backfill of Zendesk comment author fields into BQ.

This updates only ticket_comments author metadata:
- author_id
- author_email
- author_role
- author_is_agent
- via_channel

It does not touch tickets.updated_at, ticket full_text, comments body,
attachments, OCR rows, embeddings, GCS documents, Vertex indexes, or sync
cursor state.
"""

from __future__ import annotations

import argparse
import json
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
from google.cloud import bigquery  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402
from app.services import bigquery as bq_service  # noqa: E402


MAX_RETRIES = 3
SHOW_MANY_USERS_BATCH = 100
AUTHOR_COLUMNS = {"author_id", "author_email", "author_role", "author_is_agent", "via_channel"}
TEMP_SCHEMA = [
    bigquery.SchemaField("ticket_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("comment_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("author_id", "INT64"),
    bigquery.SchemaField("author_name", "STRING"),
    bigquery.SchemaField("author_email", "STRING"),
    bigquery.SchemaField("author_role", "STRING"),
    bigquery.SchemaField("author_is_agent", "BOOL"),
    bigquery.SchemaField("via_channel", "STRING"),
    bigquery.SchemaField("fetched_at", "TIMESTAMP"),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _zd_get(path_or_url: str, params: dict | None = None) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"{settings.zendesk_base_url}{path_or_url}"
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(url, params=params, auth=settings.zendesk_auth, timeout=30)
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            print(json.dumps({"phase": "rate_limit", "wait_sec": wait, "url": url}), flush=True)
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Zendesk request failed after {MAX_RETRIES} retries: {url}")


def _existing_comment_columns() -> set[str]:
    client = bq_service._get_client()
    table = client.get_table(f"{settings.google_cloud_project_id}.esip_reference.ticket_comments")
    return {field.name for field in table.schema}


def require_author_columns() -> None:
    missing = AUTHOR_COLUMNS - _existing_comment_columns()
    if missing:
        raise SystemExit(
            "Missing BQ ticket_comments columns: "
            f"{sorted(missing)}. Run scripts/backend_python.sh "
            "scripts/migrate_ticket_comment_author_fields.py --apply before an apply backfill."
        )


def _ids_from_csv(value: str | None) -> list[int]:
    if not value:
        return []
    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def _candidate_filter(scope: str) -> str:
    if scope == "active":
        return "LOWER(COALESCE(t.status, '')) IN ('new', 'open', 'pending', 'hold')"
    if scope == "unresolved":
        return "LOWER(COALESCE(t.status, '')) NOT IN ('solved', 'closed')"
    return "TRUE"


def fetch_candidate_ticket_ids(scope: str, limit: int | None, include_existing: bool) -> tuple[list[int], int]:
    client = bq_service._get_client()
    metadata_filter = (
        "TRUE"
        if include_existing
        else "(c.author_id IS NULL AND c.author_role IS NULL AND c.via_channel IS NULL)"
    )
    base_sql = f"""
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets` AS t
        JOIN `{settings.google_cloud_project_id}.esip_reference.ticket_comments` AS c
          ON c.ticket_id = t.id
        WHERE {_candidate_filter(scope)}
          AND {metadata_filter}
    """
    total_sql = f"SELECT COUNT(DISTINCT t.id) AS n {base_sql}"
    total = int(next(iter(client.query(total_sql).result())).n or 0)
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    ids_sql = f"""
        SELECT DISTINCT t.id
        {base_sql}
        ORDER BY t.id
        {limit_sql}
    """
    ids = [int(row.id) for row in client.query(ids_sql).result()]
    return ids, total


def fetch_ticket_comments(ticket_id: int) -> list[dict]:
    comments: list[dict] = []
    url: str | None = f"/tickets/{ticket_id}/comments.json"
    params: dict | None = {"include": "attachments"}
    while url:
        data = _zd_get(url, params=params)
        comments.extend(data.get("comments", []))
        url = data.get("next_page")
        params = None
    return comments


def fetch_users(author_ids: set[int]) -> dict[int, dict]:
    users: dict[int, dict] = {}
    ordered = sorted(author_ids)
    for index in range(0, len(ordered), SHOW_MANY_USERS_BATCH):
        batch = ordered[index:index + SHOW_MANY_USERS_BATCH]
        if not batch:
            continue
        data = _zd_get("/users/show_many.json", params={"ids": ",".join(str(value) for value in batch)})
        for user in data.get("users", []):
            user_id = user.get("id")
            if user_id is not None:
                users[int(user_id)] = user
        time.sleep(0.1)
    return users


def _zendesk_user_is_agent(user: dict | None) -> bool | None:
    if not user:
        return None
    role = str(user.get("role") or "").lower()
    if role in {"agent", "admin"}:
        return True
    if role == "end-user":
        return False
    return None


def _comment_author_name(comment: dict, user: dict | None = None) -> str | None:
    return (
        comment.get("author_name")
        or (user or {}).get("name")
        or comment.get("via", {}).get("source", {}).get("from", {}).get("name")
    )


def build_author_rows(ticket_comments: dict[int, list[dict]], users: dict[int, dict]) -> list[dict]:
    fetched_at = _utc_now().isoformat()
    rows: list[dict] = []
    for ticket_id, comments in ticket_comments.items():
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id is None:
                continue
            author_id = comment.get("author_id")
            author = users.get(int(author_id)) if author_id else None
            rows.append({
                "ticket_id": int(ticket_id),
                "comment_id": int(comment_id),
                "author_id": int(author_id) if author_id else None,
                "author_name": _comment_author_name(comment, author),
                "author_email": (author or {}).get("email"),
                "author_role": (author or {}).get("role"),
                "author_is_agent": _zendesk_user_is_agent(author),
                "via_channel": comment.get("via", {}).get("channel"),
                "fetched_at": fetched_at,
            })
    return rows


def apply_rows(rows: list[dict]) -> None:
    if not rows:
        return
    client = bq_service._get_client()
    temp_table = (
        f"{settings.google_cloud_project_id}.esip_reference._ticket_comment_author_backfill_"
        f"{_utc_now().strftime('%Y%m%d%H%M%S')}"
    )
    job = client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=TEMP_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    )
    job.result(timeout=600)
    merge_sql = f"""
        MERGE `{settings.google_cloud_project_id}.esip_reference.ticket_comments` T
        USING `{temp_table}` S
        ON T.ticket_id = S.ticket_id AND T.comment_id = S.comment_id
        WHEN MATCHED THEN UPDATE SET
          author_id = S.author_id,
          author_name = COALESCE(S.author_name, T.author_name),
          author_email = S.author_email,
          author_role = S.author_role,
          author_is_agent = S.author_is_agent,
          via_channel = S.via_channel
    """
    try:
        client.query(merge_sql).result(timeout=600)
    finally:
        client.delete_table(temp_table, not_found_ok=True)


def _load_checkpoint(path: Path | None) -> dict:
    if not path or not path.exists():
        return {"completed_ticket_ids": [], "errors": []}
    return json.loads(path.read_text())


def _save_checkpoint(path: Path | None, checkpoint: dict) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))


def _chunks(values: list[int], size: int) -> list[list[int]]:
    size = max(1, int(size or 1))
    return [values[index:index + size] for index in range(0, len(values), size)]


def _summarize_rows(rows: list[dict]) -> dict:
    return {
        "rows": len(rows),
        "with_author_id": sum(1 for row in rows if row.get("author_id") is not None),
        "with_author_role": sum(1 for row in rows if row.get("author_role")),
        "agent_authored": sum(1 for row in rows if row.get("author_is_agent") is True),
        "end_user_authored": sum(1 for row in rows if row.get("author_is_agent") is False),
        "with_via_channel": sum(1 for row in rows if row.get("via_channel")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Zendesk comment author metadata into BQ.")
    parser.add_argument("--scope", choices=["unresolved", "active", "all"], default="unresolved")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ticket-ids", help="Comma-separated explicit ticket IDs for a targeted pilot.")
    parser.add_argument("--chunk-size", type=int, default=25)
    parser.add_argument("--checkpoint-file", type=Path)
    parser.add_argument("--include-existing", action="store_true", help="Refresh rows even if author metadata is present.")
    parser.add_argument("--apply", action="store_true", help="Apply BQ MERGE. Omit for dry run.")
    parser.add_argument("--sleep", type=float, default=0.1, help="Sleep after each ticket comment fetch.")
    parser.add_argument("--stop-after-chunks", type=int)
    args = parser.parse_args()

    explicit_ids = _ids_from_csv(args.ticket_ids)
    if args.apply or not explicit_ids:
        require_author_columns()
    if explicit_ids:
        selected_ids = explicit_ids[: args.limit] if args.limit else explicit_ids
        total_candidates = len(explicit_ids)
    else:
        selected_ids, total_candidates = fetch_candidate_ticket_ids(
            args.scope,
            args.limit,
            include_existing=args.include_existing,
        )

    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed = {int(value) for value in checkpoint.get("completed_ticket_ids", [])}
    pending_ids = [ticket_id for ticket_id in selected_ids if ticket_id not in completed]
    print(json.dumps({
        "phase": "selected",
        "apply": args.apply,
        "scope": args.scope,
        "explicit_ticket_ids": bool(explicit_ids),
        "total_candidates": total_candidates,
        "selected": len(selected_ids),
        "already_completed": len(completed),
        "pending": len(pending_ids),
    }), flush=True)
    if not pending_ids:
        return 0

    started = time.monotonic()
    processed_tickets = 0
    total_rows = 0
    total_zendesk_requests = 0
    sample_rows: list[dict] = []
    errors: list[dict] = list(checkpoint.get("errors", []))
    chunks = _chunks(pending_ids, args.chunk_size)
    for chunk_index, ticket_ids in enumerate(chunks, 1):
        if args.stop_after_chunks and chunk_index > args.stop_after_chunks:
            break
        ticket_comments: dict[int, list[dict]] = {}
        author_ids: set[int] = set()
        chunk_errors: list[dict] = []
        chunk_started = time.monotonic()
        for ticket_id in ticket_ids:
            try:
                comments = fetch_ticket_comments(ticket_id)
                total_zendesk_requests += 1
            except Exception as exc:
                error = {"ticket_id": ticket_id, "error": str(exc)}
                chunk_errors.append(error)
                errors.append(error)
                continue
            ticket_comments[ticket_id] = comments
            for comment in comments:
                author_id = comment.get("author_id")
                if author_id:
                    author_ids.add(int(author_id))
            time.sleep(max(0.0, args.sleep))

        users = fetch_users(author_ids)
        total_zendesk_requests += (len(author_ids) + SHOW_MANY_USERS_BATCH - 1) // SHOW_MANY_USERS_BATCH
        rows = build_author_rows(ticket_comments, users)
        if not sample_rows:
            sample_rows = rows[:5]
        if args.apply:
            apply_rows(rows)
        processed_tickets += len(ticket_comments)
        total_rows += len(rows)
        completed.update(ticket_comments)
        checkpoint.update({
            "updated_at": _utc_now().isoformat(),
            "completed_ticket_ids": sorted(completed),
            "errors": errors,
        })
        _save_checkpoint(args.checkpoint_file, checkpoint)
        elapsed = time.monotonic() - started
        rate = processed_tickets / elapsed if elapsed > 0 else 0
        projected_hours = (total_candidates / rate / 3600) if rate > 0 and total_candidates else None
        print(json.dumps({
            "phase": "chunk_done",
            "chunk": chunk_index,
            "chunks": len(chunks),
            "tickets": len(ticket_comments),
            "rows_summary": _summarize_rows(rows),
            "unique_author_ids": len(author_ids),
            "zendesk_requests_total": total_zendesk_requests,
            "chunk_errors": chunk_errors[:5],
            "chunk_elapsed_sec": round(time.monotonic() - chunk_started, 2),
            "tickets_per_min": round(rate * 60, 2),
            "projected_hours_for_scope": round(projected_hours, 2) if projected_hours else None,
        }), flush=True)

    elapsed = time.monotonic() - started
    print(json.dumps({
        "phase": "complete",
        "apply": args.apply,
        "processed_tickets": processed_tickets,
        "rows": total_rows,
        "errors": len(errors),
        "elapsed_sec": round(elapsed, 2),
        "tickets_per_min": round((processed_tickets / elapsed) * 60, 2) if elapsed > 0 else None,
        "zendesk_requests_total": total_zendesk_requests,
        "sample": sample_rows[:5],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
