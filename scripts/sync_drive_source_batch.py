#!/usr/bin/env python3
"""Controlled partial sync for one Google Drive source.

Unlike the admin endpoint, this stops Drive traversal after it finds enough
new/changed files for the requested batch. It never performs deletion cleanup.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import google.auth.transport.requests as google_requests_transport  # noqa: E402

from app.services.bigquery import (  # noqa: E402
    build_drive_chunk_rows,
    build_drive_spreadsheet_row_rows,
    delete_drive_spreadsheet_rows,
    get_indexed_drive_files,
    replace_drive_chunks_for_file,
    replace_drive_spreadsheet_rows_for_file,
)
from app.services.drive_sync import (  # noqa: E402
    DRIVE_HTTP_MAX_RETRIES,
    DRIVE_HTTP_RETRY_STATUS_CODES,
    DRIVE_SOURCES,
    GOOGLE_SHEETS_MIME,
    SKIP_MIMETYPES,
    chunk_text,
    download_file,
    _extract_document_payload,
    _extract_google_sheet_payload,
    _file_sort_key,
    _get_cost_abort_reason,
    _get_drive_creds,
    _get_gcp_token,
    _get_doc_client,
    _normalize_path,
    _path_to_slug,
    _should_skip_file_by_name,
    upload_to_gcs,
    upsert_vertex_doc,
)

NON_DOCUMENT_EXTENSIONS = {
    ".bin",
    ".bat",
    ".7z",
    ".cab",
    ".cfg",
    ".cmd",
    ".config",
    ".cnt",
    ".dat",
    ".dcb",
    ".db",
    ".dll",
    ".doc",
    ".dmg",
    ".exe",
    ".hash",
    ".hlp",
    ".ico",
    ".ini",
    ".job",
    ".manifest",
    ".iso",
    ".iss",
    ".lnk",
    ".lng",
    ".log",
    ".msi",
    ".mst",
    ".msu",
    ".pak",
    ".pdb",
    ".pec",
    ".pem",
    ".properties",
    ".reg",
    ".sql",
    ".svy",
    ".tibx",
    ".vbs",
    ".xml",
}
NON_DOCUMENT_FILENAMES = {"thumbs.db"}
NON_DOCUMENT_PATH_PREFIXES = {
    "applications",
    "documents/micros_integrated_logon",
    "micros_integrated_logon",
    "ecomm_shortcuts",
    "epos image",
    "hardwaredrivers",
    "micros files",
    "officeprinterdrivers",
    "patt/build 364_1 - all in one installer",
    "patt/catr 4.12.0.2446",
    "patt/cctags - multi 364 and newer",
    "patt/multi364_11",
    "patt/multi364_12",
    "patt/old",
    "patt/old/build 364 - all in one installer",
    "scripts/micros_integrated_logon",
}


def _source_config(source_id: str) -> dict[str, str]:
    for source in DRIVE_SOURCES:
        if source["source_id"] == source_id:
            return source
    raise SystemExit(f"Unknown Drive source: {source_id}")


def _drive_list_folder(creds, folder_id: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {creds.token}"}
    url = "https://www.googleapis.com/drive/v3/files"
    params: dict[str, Any] = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
        "pageSize": 100,
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
        "orderBy": "folder,name_natural",
    }
    items: list[dict[str, Any]] = []
    while True:
        for attempt in range(1, DRIVE_HTTP_MAX_RETRIES + 1):
            try:
                response = requests.get(url, params=params, headers=headers, timeout=30)
                if response.status_code == 401:
                    creds.refresh(google_requests_transport.Request())
                    headers["Authorization"] = f"Bearer {creds.token}"
                    if attempt < DRIVE_HTTP_MAX_RETRIES:
                        time.sleep(min(8, 2 ** (attempt - 1)))
                        continue
                if response.status_code in DRIVE_HTTP_RETRY_STATUS_CODES and attempt < DRIVE_HTTP_MAX_RETRIES:
                    time.sleep(min(8, 2 ** (attempt - 1)))
                    continue
                response.raise_for_status()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError):
                if attempt >= DRIVE_HTTP_MAX_RETRIES:
                    raise
                time.sleep(min(8, 2 ** (attempt - 1)))
        data = response.json()
        items.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return items
        params["pageToken"] = page_token


def _find_pending_files(
    *,
    creds,
    root_folder_id: str,
    indexed: dict[str, str],
    limit: int,
    max_list_seconds: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.monotonic()
    stack: list[tuple[str, str]] = [(root_folder_id, "")]
    pending: list[dict[str, Any]] = []
    scanned_files = 0
    scanned_folders = 0
    skipped_unchanged = 0
    skipped_mime = 0
    skipped_name = 0

    while stack and len(pending) < limit:
        if time.monotonic() - started > max_list_seconds:
            return pending, {
                "listing_complete": False,
                "reason": "max_list_seconds",
                "scanned_files": scanned_files,
                "scanned_folders": scanned_folders,
                "skipped_unchanged": skipped_unchanged,
                "skipped_mime": skipped_mime,
                "skipped_name": skipped_name,
                "remaining_folders": len(stack),
            }
        folder_id, current_path = stack.pop()
        normalized_current_path = _normalize_path(current_path).lower()
        if any(
            normalized_current_path == prefix or normalized_current_path.startswith(prefix + "/")
            for prefix in NON_DOCUMENT_PATH_PREFIXES
        ):
            continue
        scanned_folders += 1
        items = sorted(_drive_list_folder(creds, folder_id), key=_file_sort_key)
        child_folders: list[tuple[str, str]] = []
        for item in items:
            mime_type = str(item.get("mimeType", "")).lower()
            if mime_type == "application/vnd.google-apps.folder":
                child_path = _normalize_path(f"{current_path}/{item.get('name', '')}")
                child_folders.append((item["id"], child_path))
                continue
            scanned_files += 1
            if mime_type in SKIP_MIMETYPES:
                skipped_mime += 1
                continue
            item_name = str(item.get("name", ""))
            lower_name = item_name.lower()
            if (
                _should_skip_file_by_name(item_name)
                or lower_name in NON_DOCUMENT_FILENAMES
                or any(lower_name.endswith(ext) for ext in NON_DOCUMENT_EXTENSIONS)
            ):
                skipped_name += 1
                continue
            modified = str(item.get("modifiedTime", ""))
            existing_modified = indexed.get(item["id"])
            if existing_modified and existing_modified[:19] >= modified[:19]:
                skipped_unchanged += 1
                continue
            item["source_path"] = _normalize_path(current_path)
            pending.append(item)
            if len(pending) >= limit:
                break
        stack.extend(reversed(child_folders))

    return pending, {
        "listing_complete": not stack,
        "scanned_files": scanned_files,
        "scanned_folders": scanned_folders,
        "skipped_unchanged": skipped_unchanged,
        "skipped_mime": skipped_mime,
        "skipped_name": skipped_name,
        "remaining_folders": len(stack),
    }


def sync_batch(source_id: str, limit: int, max_list_seconds: int) -> dict[str, Any]:
    source = _source_config(source_id)
    drive_creds = _get_drive_creds()
    gcp_token = _get_gcp_token()
    _get_doc_client()
    indexed = get_indexed_drive_files(source_id)
    pending_files, listing = _find_pending_files(
        creds=drive_creds,
        root_folder_id=source["folder_id"],
        indexed=indexed,
        limit=limit,
        max_list_seconds=max_list_seconds,
    )
    print({"selected": len(pending_files), "listing": listing}, flush=True)

    added = updated = skipped = errors = 0
    processed: list[str] = []
    for i, file in enumerate(pending_files, 1):
        abort_reason = _get_cost_abort_reason()
        if abort_reason:
            raise RuntimeError(f"Drive sync stopped by cost guard: {abort_reason}")

        file_id = file["id"]
        file_name = file["name"]
        mime_type = file["mimeType"]
        modified = file.get("modifiedTime", "")
        source_path = file.get("source_path", "")
        source_path_slug = _path_to_slug(source_path)
        drive_url = f"https://drive.google.com/file/d/{file_id}/view"
        is_new = file_id not in indexed
        print(f"[{i}/{len(pending_files)}] {'Adding' if is_new else 'Updating'}: {source_path}/{file_name}", flush=True)
        try:
            if mime_type == GOOGLE_SHEETS_MIME:
                payload = _extract_google_sheet_payload(file_id, file_name, drive_creds)
            else:
                content = download_file(file_id, mime_type, drive_creds)
                payload = _extract_document_payload(content, mime_type, file_name)
            text = payload["text"]
            if not text.strip():
                skipped += 1
                continue
            full_text = (
                f"FILE: {file_name}\n"
                f"SOURCE: {source['display_name']}\n"
                f"SOURCE PATH: {source_path or 'root'}\n\n"
                f"{text}"
            )
            gcs_uri = upload_to_gcs(file_id, full_text, gcp_token, source_id=source_id)
            upsert_vertex_doc(
                file_id=file_id,
                file_name=file_name,
                gcs_uri=gcs_uri,
                drive_url=drive_url,
                source_id=source_id,
                source_path=source_path,
                source_path_slug=source_path_slug,
            )
            chunk_units = payload["chunks"] or chunk_text(full_text)
            rows = build_drive_chunk_rows(
                file_id=file_id,
                file_name=file_name,
                source_id=source_id,
                source_path=source_path,
                source_path_slug=source_path_slug,
                drive_url=drive_url,
                modified_at=modified,
                chunks=chunk_units,
                mime_type=mime_type,
            )
            replace_drive_chunks_for_file(file_id, rows)
            spreadsheet_rows = payload["spreadsheet_rows"]
            if spreadsheet_rows:
                structured_rows = build_drive_spreadsheet_row_rows(
                    file_id=file_id,
                    file_name=file_name,
                    source_id=source_id,
                    source_path=source_path,
                    source_path_slug=source_path_slug,
                    drive_url=drive_url,
                    modified_at=modified,
                    source_format=payload["source_format"] or "xlsx",
                    row_records=spreadsheet_rows,
                    mime_type=mime_type,
                )
                replace_drive_spreadsheet_rows_for_file(file_id, structured_rows)
            else:
                delete_drive_spreadsheet_rows(file_id)
            if is_new:
                added += 1
            else:
                updated += 1
            processed.append(file_id)
        except Exception as exc:
            errors += 1
            print(f"ERROR processing {file_name}: {exc}", flush=True)

    return {
        "source_id": source_id,
        "selected": len(pending_files),
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "processed_file_ids": processed,
        "listing": listing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-list-seconds", type=int, default=90)
    args = parser.parse_args()
    print(sync_batch(args.source_id, args.limit, args.max_list_seconds))


if __name__ == "__main__":
    main()
