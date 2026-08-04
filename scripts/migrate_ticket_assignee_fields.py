#!/usr/bin/env python3
"""Add metadata-only assignee identity columns to esip_reference.tickets."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from google.cloud import bigquery  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> None:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    client = bigquery.Client(project=settings.google_cloud_project_id)
    table_id = f"{settings.google_cloud_project_id}.esip_reference.tickets"
    table = client.get_table(table_id)
    existing = {field.name for field in table.schema}
    additions = []
    if "assignee_id" not in existing:
        additions.append(bigquery.SchemaField("assignee_id", "INT64"))
    if "assignee_email" not in existing:
        additions.append(bigquery.SchemaField("assignee_email", "STRING"))
    if not additions:
        print("tickets already has assignee_id and assignee_email")
        return
    table.schema = list(table.schema) + additions
    client.update_table(table, ["schema"])
    print("added columns:", ", ".join(field.name for field in additions))


if __name__ == "__main__":
    main()
