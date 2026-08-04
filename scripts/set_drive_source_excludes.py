"""
Set exclude_path_prefixes for a Drive source in Firestore system/data_sources.

Examples:
    python scripts/set_drive_source_excludes.py \
      --source-id gdrive_operations_job_aids \
      --exclude "2. General/Archived"

    python scripts/set_drive_source_excludes.py \
      --source-id gdrive_operations_job_aids \
      --exclude "2. General/Archived" \
      --exclude "French/7. Other items needed for translation"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dotenv import load_dotenv
load_dotenv(override=True)

import firebase_admin
from firebase_admin import credentials, firestore

from app.config import settings


def _normalize(path: str) -> str:
    return "/".join(part.strip() for part in path.strip().strip("/").split("/") if part.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Set Drive source exclude_path_prefixes")
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Path prefix to exclude (repeatable)",
    )
    args = parser.parse_args()

    cred = credentials.Certificate(settings.google_application_credentials)
    firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})
    db = firestore.client()
    doc_ref = db.collection("system").document("data_sources")

    doc = doc_ref.get()
    data = doc.to_dict() or {}
    sources = data.get("sources", {})
    if args.source_id not in sources:
        print(f"ERROR: source_id not found: {args.source_id}")
        return 1

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in args.exclude:
        norm = _normalize(raw)
        if not norm:
            continue
        low = norm.lower()
        if low in seen:
            continue
        seen.add(low)
        cleaned.append(norm)

    doc_ref.update({f"sources.{args.source_id}.exclude_path_prefixes": cleaned})
    print(f"Updated exclude_path_prefixes for {args.source_id}:")
    for p in cleaned:
        print(f"  - {p}")
    if not cleaned:
        print("  (empty)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
