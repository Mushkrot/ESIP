#!/usr/bin/env python3
"""Add Zendesk assignment group columns to BigQuery tickets."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402
from app.services import bigquery as bq_service  # noqa: E402


def _existing_columns(client) -> set[str]:
    table = client.get_table(f"{settings.google_cloud_project_id}.esip_reference.tickets")
    return {field.name for field in table.schema}


def migrate(*, dry_run: bool) -> dict:
    client = bq_service._get_client()
    before = _existing_columns(client)
    statements = []
    if "assignee_group_id" not in before:
        statements.append(
            "ALTER TABLE "
            f"`{settings.google_cloud_project_id}.esip_reference.tickets` "
            "ADD COLUMN assignee_group_id INT64"
        )
    if "assignee_group_name" not in before:
        statements.append(
            "ALTER TABLE "
            f"`{settings.google_cloud_project_id}.esip_reference.tickets` "
            "ADD COLUMN assignee_group_name STRING"
        )

    for sql in statements:
        print(sql)
        if not dry_run:
            client.query(sql).result()

    after = before if dry_run else _existing_columns(client)
    return {
        "dry_run": dry_run,
        "statements": len(statements),
        "before_has_assignee_group_id": "assignee_group_id" in before,
        "before_has_assignee_group_name": "assignee_group_name" in before,
        "after_has_assignee_group_id": "assignee_group_id" in after,
        "after_has_assignee_group_name": "assignee_group_name" in after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Add assignment group columns to BQ tickets.")
    parser.add_argument("--apply", action="store_true", help="Apply migration. Omit for dry run.")
    args = parser.parse_args()
    result = migrate(dry_run=not args.apply)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
