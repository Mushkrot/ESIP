#!/usr/bin/env python3
"""QA for Zendesk ticket comment author enrichment prep."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.services import bq_sync, bigquery as bq_service, universal_chat  # noqa: E402
import scripts.backfill_ticket_comment_author_metadata as backfill  # noqa: E402
import scripts.export_tickets_bq as export_tickets_bq  # noqa: E402
import scripts.migrate_ticket_comment_author_fields as migration  # noqa: E402


PASS = "PASS"
FAIL = "FAIL"


def _check(condition: bool, name: str, message: str) -> bool:
    print(f"{PASS if condition else FAIL} {name}: {message}")
    return condition


def _sample_comments() -> list[dict]:
    return [
        {
            "id": 9001,
            "created_at": "2026-05-20T10:00:00Z",
            "public": True,
            "author_id": 101,
            "body": "Public reply from an agent.",
            "via": {"channel": "web", "source": {"from": {"name": "Fallback Agent"}}},
        },
        {
            "id": 9002,
            "created_at": "2026-05-20T10:05:00Z",
            "public": False,
            "author_id": 202,
            "body": "Internal note from an end user should still be internal by flag.",
            "via": {"channel": "email", "source": {"from": {"name": "Fallback User"}}},
        },
        {
            "id": 9003,
            "created_at": "2026-05-20T10:10:00Z",
            "public": True,
            "body": "Anonymous public comment.",
            "via": {"channel": "api", "source": {"from": {"name": "Anonymous"}}},
        },
    ]


def _sample_users() -> dict[int, dict]:
    return {
        101: {"id": 101, "name": "Agent One", "email": "casey.nguyen@northstar.example.com", "role": "agent"},
        202: {"id": 202, "name": "Customer One", "email": "morgan.chen@northstar.example.com", "role": "end-user"},
    }


def case_bq_sync_builder() -> bool:
    original = bq_sync._fetch_user
    try:
        bq_sync._fetch_user = lambda user_id, cache: _sample_users().get(int(user_id))
        rows = bq_sync._build_comment_rows(123, _sample_comments(), datetime.now(timezone.utc), {})
    finally:
        bq_sync._fetch_user = original
    first, second, third = rows
    ok = (
        first["author_id"] == 101
        and first["author_email"] == "casey.nguyen@northstar.example.com"
        and first["author_role"] == "agent"
        and first["author_is_agent"] is True
        and first["via_channel"] == "web"
        and second["is_internal"] is True
        and second["author_is_agent"] is False
        and third["author_id"] is None
        and third["author_name"] == "Anonymous"
    )
    return _check(ok, "COMMENT-AUTH-001", f"daily sync builder enriches comment rows; rows={rows}")


def case_export_builder() -> bool:
    original = export_tickets_bq.fetch_user
    try:
        export_tickets_bq.fetch_user = lambda user_id, cache: _sample_users().get(int(user_id))
        rows = export_tickets_bq.build_comment_rows(
            123,
            _sample_comments(),
            datetime.now(timezone.utc),
            {},
        )
    finally:
        export_tickets_bq.fetch_user = original
    ok = (
        rows[0]["author_role"] == "agent"
        and rows[0]["author_is_agent"] is True
        and rows[1]["author_role"] == "end-user"
        and rows[1]["author_is_agent"] is False
        and rows[2]["via_channel"] == "api"
    )
    return _check(ok, "COMMENT-AUTH-002", f"full export builder enriches comment rows; rows={rows}")


def case_backfill_rows() -> bool:
    rows = backfill.build_author_rows({123: _sample_comments()}, _sample_users())
    ok = (
        len(rows) == 3
        and rows[0]["ticket_id"] == 123
        and rows[0]["comment_id"] == 9001
        and rows[0]["author_role"] == "agent"
        and rows[1]["author_role"] == "end-user"
        and rows[2]["author_name"] == "Anonymous"
        and set(migration.COMMENT_AUTHOR_COLUMNS) == backfill.AUTHOR_COLUMNS
    )
    return _check(ok, "COMMENT-AUTH-003", f"backfill rows match migration columns; rows={rows}")


def case_prompts_include_roles() -> bool:
    schema = bq_service.SCHEMA_DESCRIPTION
    hint = universal_chat._BQ_SCHEMA_HINT
    ok = (
        "author_id" in schema
        and "author_role" in schema
        and "author_is_agent" in schema
        and "author_is_agent" in hint
        and "ticket_comments(ticket_id, body, author_name, created_at, is_public)" not in hint
        and "There is no `is_public` column" in hint
    )
    return _check(ok, "COMMENT-AUTH-004", "analytics schema hints include author role fields and avoid is_public")


def main() -> int:
    results = [
        case_bq_sync_builder(),
        case_export_builder(),
        case_backfill_rows(),
        case_prompts_include_roles(),
    ]
    print(f"RESULTS: {sum(1 for ok in results if ok)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
