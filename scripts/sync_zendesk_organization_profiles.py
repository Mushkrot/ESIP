#!/usr/bin/env python3
"""Sync Zendesk organization profile fields into BigQuery.

Dry-run is the default. Use --apply to write only esip_reference.zendesk_organization_profiles.
"""

from __future__ import annotations

import argparse
import sys

from app.services import zendesk_organization_profiles as profiles


def _line(value: str) -> None:
    sys.stdout.write(f"{value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Zendesk organization profile fields.")
    parser.add_argument("--apply", action="store_true", help="Write the profile table. Omit for dry run.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry run; this is the default.")
    parser.add_argument("--limit", type=int, default=None, help="Limit organizations fetched for proof runs.")
    parser.add_argument("--page-size", type=int, default=profiles.PAGE_SIZE, help="Zendesk page size, max 100.")
    args = parser.parse_args()

    dry_run = not args.apply
    result = profiles.sync_zendesk_organization_profiles(
        dry_run=dry_run,
        limit=args.limit,
        page_size=args.page_size,
    )

    mode = "DRY_RUN" if result.dry_run else "APPLY"
    distribution = ", ".join(
        f"{key}={value}" for key, value in result.pos_type_distribution.items()
    ) or "(none)"
    _line(f"MODE {mode}")
    _line(f"TARGET {result.target_table}")
    _line(f"FETCHED organizations={result.fetched_count}")
    _line(f"PREPARED rows={result.prepared_count}")
    _line(f"POS_TYPE distribution: {distribution}")
    _line(f"SIMPHONY_STORE true={result.simphony_store_count}")
    if result.simphony_samples:
        sample_text = "; ".join(
            (
                f"org_id={sample.get('org_id')} store_id={sample.get('store_id') or '-'} "
                f"name={sample.get('org_name') or '-'} brand={sample.get('brand') or '-'}"
            )
            for sample in result.simphony_samples
        )
    else:
        sample_text = "none in this fetched sample"
    _line(f"SAMPLE_SIMPHONY_ORGS {sample_text}")
    if result.dry_run:
        _line("NO_WRITE dry run: BigQuery profile table was not modified.")
    else:
        _line(f"WROTE rows={result.written_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
