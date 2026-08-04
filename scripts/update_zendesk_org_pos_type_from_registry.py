#!/usr/bin/env python3
"""Build or apply a reviewed Simphony-only Zendesk organization POS Type manifest.

Dry-run is the default and performs no Zendesk writes.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import settings
from app.services import bigquery as bq_service
from app.services import simphony_pos_backfill as backfill
from app.services import zendesk_organization_profiles

REGISTRY_SPREADSHEET_ID = os.getenv("OPERATIONS_REGISTRY_SPREADSHEET_ID", "")
REGISTRY_RANGE = os.getenv("OPERATIONS_REGISTRY_RANGE", "Registry!A1:Z5000")
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_OUTPUT = Path("export/simphony_pos_type_backfill_candidates_dry_run.csv")


def _line(value: str, *, stderr: bool = False) -> None:
    stream = sys.stderr if stderr else sys.stdout
    stream.write(f"{value}\n")


def _candidate_counts(candidates: Iterable[backfill.SimphonyPosCandidate]) -> str:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.reason] = counts.get(candidate.reason, 0) + 1
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "(none)"


def _read_registry_records_from_csv(path: Path) -> tuple[backfill.RegistryPosRecord, ...]:
    rows: list[backfill.RegistryPosRecord] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_number, row in enumerate(reader, start=2):
            rows.append(backfill.registry_record_from_row(row, row_number=row_number))
    return tuple(rows)


def _read_registry_records_from_sheet(*, oauth_token_file: Path) -> tuple[backfill.RegistryPosRecord, ...]:
    if not REGISTRY_SPREADSHEET_ID:
        raise RuntimeError(
            "OPERATIONS_REGISTRY_SPREADSHEET_ID is required for live Google Sheets reads. "
            "Use --registry-csv for the offline portfolio workflow."
        )
    if not oauth_token_file.exists():
        raise RuntimeError(
            f"Missing OAuth token file: {oauth_token_file}. Pass --oauth-token-file only for "
            "an explicitly configured Google Sheets integration."
        )
    creds = Credentials.from_authorized_user_file(str(oauth_token_file), scopes=[SHEETS_SCOPE])
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            oauth_token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(f"OAuth token is invalid and cannot refresh: {oauth_token_file}")
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=REGISTRY_SPREADSHEET_ID, range=REGISTRY_RANGE)
        .execute()
        .get("values")
        or []
    )
    if not values:
        raise RuntimeError("Operations registry sheet returned no rows")
    headers = values[0]
    rows: list[backfill.RegistryPosRecord] = []
    for row_number, row in enumerate(values[1:], start=2):
        mapped = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
        rows.append(backfill.registry_record_from_row(mapped, row_number=row_number))
    return tuple(rows)


def _read_profile_records_from_csv(path: Path) -> tuple[backfill.ZendeskProfileRecord, ...]:
    rows: list[backfill.ZendeskProfileRecord] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(backfill.profile_record_from_row(row))
    return tuple(rows)


def _read_profile_records_from_bigquery() -> tuple[backfill.ZendeskProfileRecord, ...]:
    client = bq_service._get_client()
    profile_table = bq_service.zendesk_organization_profiles_table_id()
    tickets_table = f"{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets"
    sql = f"""
    SELECT
      p.org_id,
      p.org_name,
      p.store_id,
      p.brand,
      p.pos_type,
      p.simphony_store,
      COUNT(t.id) AS ticket_count,
      COUNTIF(LOWER(t.status) IN ("new", "open", "pending", "hold")) AS open_ticket_count
    FROM `{profile_table}` p
    LEFT JOIN `{tickets_table}` t
      ON t.org_id = p.org_id
    GROUP BY p.org_id, p.org_name, p.store_id, p.brand, p.pos_type, p.simphony_store
    ORDER BY p.store_id, p.org_id
    """
    return tuple(
        backfill.profile_record_from_row(dict(row))
        for row in client.query(sql).result(timeout=120)
    )


def _default_unmatched_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_unmatched_registry_rows{output_path.suffix}")


def _write_unmatched_csv(path: Path, records: Iterable[backfill.RegistryPosRecord]) -> int:
    rows = list(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "source_location_id",
                "store_id",
                "pos_system_name",
                "pos_type",
                "source_row_number",
            ],
        )
        writer.writeheader()
        for record in rows:
            writer.writerow(
                {
                    "source_location_id": record.source_location_id,
                    "store_id": record.store_id,
                    "pos_system_name": record.pos_system_name,
                    "pos_type": record.pos_type,
                    "source_row_number": record.source_row_number or "",
                }
            )
    return len(rows)


def _build_manifest(args: argparse.Namespace) -> int:
    if args.registry_csv:
        registry_records = _read_registry_records_from_csv(Path(args.registry_csv))
        registry_source = f"csv:{args.registry_csv}"
    else:
        token = Path(args.oauth_token_file or os.environ.get("OAUTH_TOKEN_FILE", ""))
        registry_records = _read_registry_records_from_sheet(oauth_token_file=token)
        registry_source = f"google_sheets:{REGISTRY_SPREADSHEET_ID}:{REGISTRY_RANGE}"

    if args.profiles_csv:
        profile_records = _read_profile_records_from_csv(Path(args.profiles_csv))
        profile_source = f"csv:{args.profiles_csv}"
    else:
        profile_records = _read_profile_records_from_bigquery()
        profile_source = "bigquery:esip_reference.zendesk_organization_profiles"

    result = backfill.build_simphony_pos_candidates(
        registry_records=registry_records,
        profile_records=profile_records,
        zendesk_base_url=settings.zendesk_base_url,
    )
    output_path = Path(args.output or DEFAULT_OUTPUT)
    unmatched_path = Path(args.unmatched_output) if args.unmatched_output else _default_unmatched_path(output_path)
    written = backfill.write_candidate_csv(output_path, result.candidates)
    unmatched_written = _write_unmatched_csv(unmatched_path, result.unmatched_registry_records)

    _line("MODE DRY_RUN")
    _line(f"REGISTRY_SOURCE {registry_source}")
    _line(f"PROFILE_SOURCE {profile_source}")
    _line(f"CANDIDATES total={len(result.candidates)}")
    _line(f"REASON_COUNTS {_candidate_counts(result.candidates)}")
    _line(f"ALREADY_CANONICAL profiles={result.already_canonical_profiles}")
    _line(f"UNMATCHED_REGISTRY_RECORDS total={len(result.unmatched_registry_records)}")
    _line(f"EXCLUDED_REGISTRY_RECORDS total={len(result.excluded_registry_records)}")
    _line(f"WROTE_MANIFEST path={output_path} rows={written}")
    _line(f"WROTE_UNMATCHED path={unmatched_path} rows={unmatched_written}")
    _line("NO_WRITE dry run: Zendesk organizations were not modified.")
    return 0


def _apply_reviewed_manifest(args: argparse.Namespace) -> int:
    if not args.reviewed_manifest:
        _line("REFUSED apply requires --reviewed-manifest PATH", stderr=True)
        return 2
    if not args.confirm_reviewed_manifest:
        _line("REFUSED apply requires --confirm-reviewed-manifest", stderr=True)
        return 2

    candidates = backfill.load_candidate_manifest(args.reviewed_manifest)
    try:
        backfill.validate_manifest_for_apply(candidates)
    except backfill.ManifestValidationError as exc:
        _line(f"REFUSED {exc}", stderr=True)
        return 2

    session = zendesk_organization_profiles._session()
    updated = 0
    for candidate in candidates:
        backfill.update_zendesk_org_pos_type(
            session=session,
            zendesk_base_url=settings.zendesk_base_url,
            candidate=candidate,
        )
        updated += 1
        _line(f"UPDATED org_id={candidate.org_id} store_id={candidate.store_id}")
    _line(f"WROTE_ZENDESK organizations={updated}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Build manifest only; default mode.")
    parser.add_argument("--apply", action="store_true", help="Apply an explicitly reviewed manifest.")
    parser.add_argument("--reviewed-manifest", help="Reviewed candidate CSV to apply.")
    parser.add_argument(
        "--confirm-reviewed-manifest",
        action="store_true",
        help="Required extra confirmation for --apply.",
    )
    parser.add_argument("--registry-csv", help="Optional operations-registry CSV for tests/offline dry-runs.")
    parser.add_argument("--profiles-csv", help="Optional profile CSV for tests/offline dry-runs.")
    parser.add_argument("--oauth-token-file", help="Optional Google OAuth token file for a configured live registry.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Dry-run candidate manifest path.")
    parser.add_argument("--unmatched-output", help="Dry-run unmatched registry rows output path.")
    args = parser.parse_args(argv)

    if args.apply:
        return _apply_reviewed_manifest(args)
    return _build_manifest(args)


if __name__ == "__main__":
    raise SystemExit(main())
