#!/usr/bin/env python3
"""
Local probe for spreadsheet-aware parsing.

Downloads one or more Drive XLSX files and prints the normalized row records.
No database writes. Intended for safe local validation before changing live search.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.services.drive_sync import _get_drive_creds  # noqa: E402
from app.services.spreadsheet_parser import parse_xlsx  # noqa: E402


def download_drive_file(file_id: str) -> bytes:
    creds = _get_drive_creds()
    response = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"alt": "media", "supportsAllDrives": True},
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file_id", nargs="+", help="Google Drive file id(s) for XLSX files")
    parser.add_argument("--name", default="spreadsheet.xlsx", help="Display name override")
    parser.add_argument("--rows", type=int, default=20, help="How many normalized rows to print")
    args = parser.parse_args()

    for index, file_id in enumerate(args.file_id, start=1):
        display_name = args.name if len(args.file_id) == 1 else f"{args.name} #{index}"
        content = download_drive_file(file_id)
        parsed = parse_xlsx(content, display_name)
        print(f"=== FILE {index}: {display_name} ({file_id}) ===")
        print(json.dumps({
            "sheet_count": len(parsed.sheet_names),
            "sheet_names": parsed.sheet_names,
            "row_records": len(parsed.row_records),
        }, ensure_ascii=False, indent=2))
        for row in parsed.row_records[: max(0, args.rows)]:
            print("---")
            print(json.dumps(row.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
