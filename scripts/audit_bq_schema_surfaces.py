#!/usr/bin/env python3
"""Read-only audit for BigQuery schema drift across prompt/schema surfaces.

The script compares the live BigQuery table schema with the fields advertised
to LLM SQL generation surfaces. It does not modify prompts, BigQuery, or app
state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from google.api_core import exceptions as google_exceptions

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.services import bigquery as bq_service  # noqa: E402
from app.services import universal_chat  # noqa: E402
from app.services.bigquery import BQ_DATASET, GCP_PROJECT, _get_client  # noqa: E402


FULL_SURFACES = {
    "bigquery.SCHEMA_DESCRIPTION": bq_service.SCHEMA_DESCRIPTION,
}

COMPACT_SURFACES = {
    "universal_chat._BQ_SCHEMA_HINT": universal_chat._BQ_SCHEMA_HINT,
}

TABLES = ("tickets", "ticket_comments", "zendesk_organization_profiles")
LOCAL_SCHEMA_TABLES = {
    bq_service.ZENDESK_ORGANIZATION_PROFILES_TABLE: [
        field.name for field in bq_service.ZENDESK_ORGANIZATION_PROFILES_SCHEMA
    ],
}
DANGEROUS_ALIASES = ("is_public", "average_sla_minutes")
SUPPORTED_SLA_FIELDS = {
    "sla_policy_name",
    "resolution_sla_target_hours",
    "resolution_sla_elapsed_hours",
    "resolution_sla_breached",
    "resolution_sla_status",
}


@dataclass
class SurfaceAudit:
    surface: str
    table: str
    actual_count: int
    advertised_count: int
    missing_from_surface: list[str]
    extra_in_surface: list[str]


def _live_schema() -> dict[str, list[str]]:
    client = _get_client()
    out: dict[str, list[str]] = {}
    for table in TABLES:
        table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.{table}"
        try:
            out[table] = [field.name for field in client.get_table(table_ref).schema]
        except google_exceptions.NotFound:
            if table not in LOCAL_SCHEMA_TABLES:
                raise
            out[table] = LOCAL_SCHEMA_TABLES[table]
    return out


def _extract_schema_description_fields(text: str, table: str) -> set[str]:
    pattern = rf"TABLE:\s+`?{re.escape(table)}`?.*?(?=\nTABLE:\s+`|\Z)"
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return set()
    fields: set[str] = set()
    for line in match.group(0).splitlines()[1:]:
        stripped = line.strip()
        field_match = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)\s+(?:ARRAY<[^>]+>|[A-Z0-9_]+)\b", stripped)
        if field_match:
            fields.add(field_match.group(1))
    return fields


def _extract_compact_hint_fields(text: str, table: str) -> set[str]:
    pattern = rf"-\s+{re.escape(table)}\((.*?)\)"
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        return set()
    raw = match.group(1)
    fields: set[str] = set()
    for token in re.split(r"[,\s]+", raw):
        token = token.strip("` \n\t")
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", token):
            fields.add(token)
    return fields


def _surface_fields(surface_name: str, text: str, table: str, *, compact: bool) -> set[str]:
    if compact:
        return _extract_compact_hint_fields(text, table)
    return _extract_schema_description_fields(text, table)


def _audit_surface(surface_name: str, text: str, live: dict[str, list[str]], *, compact: bool) -> list[SurfaceAudit]:
    rows: list[SurfaceAudit] = []
    for table in TABLES:
        actual = set(live[table])
        advertised = _surface_fields(surface_name, text, table, compact=compact)
        rows.append(
            SurfaceAudit(
                surface=surface_name,
                table=table,
                actual_count=len(actual),
                advertised_count=len(advertised),
                missing_from_surface=sorted(actual - advertised),
                extra_in_surface=sorted(advertised - actual),
            )
        )
    return rows


def _dangerous_alias_mentions() -> dict[str, list[dict[str, str]]]:
    surfaces = {**FULL_SURFACES, **COMPACT_SURFACES}
    hits: dict[str, list[dict[str, str]]] = {}
    for surface, text in surfaces.items():
        found = []
        for alias in DANGEROUS_ALIASES:
            match = re.search(rf"\b{re.escape(alias)}\b", text)
            if not match:
                continue
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            context = re.sub(r"\s+", " ", text[start:end]).strip()
            found.append({"alias": alias, "context": context})
        if found:
            hits[surface] = found
    return hits


def _guard_notes(live: dict[str, list[str]]) -> dict[str, Any]:
    ticket_fields = set(live["tickets"])
    return {
        "supported_sla_fields_present": sorted(SUPPORTED_SLA_FIELDS & ticket_fields),
        "supported_sla_fields_missing": sorted(SUPPORTED_SLA_FIELDS - ticket_fields),
        "priority_present": "priority" in ticket_fields,
        "is_public_present": "is_public" in set(live["ticket_comments"]),
        "organization_profile_schema_source": (
            "live BigQuery table"
            if bq_service.ZENDESK_ORGANIZATION_PROFILES_TABLE not in LOCAL_SCHEMA_TABLES
            else "local planned schema if live table is not created yet"
        ),
    }


def _print_human(rows: list[SurfaceAudit], dangerous: dict[str, list[dict[str, str]]], notes: dict[str, Any]) -> None:
    print("BQ schema surface audit (read-only)")
    print(f"Project/dataset: {GCP_PROJECT}.{BQ_DATASET}")
    print("")
    for row in rows:
        print(f"== {row.surface} / {row.table} ==")
        print(f"actual fields: {row.actual_count}; advertised fields: {row.advertised_count}")
        if row.missing_from_surface:
            print("missing from surface:")
            for field in row.missing_from_surface:
                print(f"  - {field}")
        else:
            print("missing from surface: none")
        if row.extra_in_surface:
            print("extra in surface:")
            for field in row.extra_in_surface:
                print(f"  - {field}")
        else:
            print("extra in surface: none")
        print("")

    print("Dangerous alias mentions:")
    print("  Review context. Negated mentions such as 'do not use is_public' are expected.")
    if dangerous:
        for surface, aliases in dangerous.items():
            print(f"  - {surface}:")
            for item in aliases:
                print(f"      {item['alias']}: {item['context']}")
    else:
        print("  none")
    print("")

    print("Guard notes:")
    for key, value in notes.items():
        print(f"  - {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit BQ schema drift across prompt/schema surfaces.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--fail-on-extra",
        action="store_true",
        help="Exit non-zero when a surface advertises fields absent from live BQ.",
    )
    args = parser.parse_args()

    live = _live_schema()
    rows: list[SurfaceAudit] = []
    for name, text in FULL_SURFACES.items():
        rows.extend(_audit_surface(name, text, live, compact=False))
    for name, text in COMPACT_SURFACES.items():
        rows.extend(_audit_surface(name, text, live, compact=True))

    dangerous = _dangerous_alias_mentions()
    notes = _guard_notes(live)
    payload = {
        "project": GCP_PROJECT,
        "dataset": BQ_DATASET,
        "tables": live,
        "surfaces": [asdict(row) for row in rows],
        "dangerous_alias_mentions": dangerous,
        "guard_notes": notes,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_human(rows, dangerous, notes)

    if args.fail_on_extra and any(row.extra_in_surface for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
