"""
Google Drive sync service.

Syncs files from SD Documentation folder to:
  1. GCS: gs://esip-reference-data/gdrive/reference_support_docs/{file_id}.txt
  2. Vertex AI data store: reference-support-knowledge (same as KB articles)
     - Document ID = Drive file_id
     - struct_data: source_id, drive_url, file_name
  3. BigQuery: esip_reference.drive_chunks (paragraph-level verbatim text)

Primary Drive folder: SD Documentation (configured reference folder ID)
Parent Shared Drive: Northstar Support Knowledge (configured reference drive ID)
DWD subject: the configured portfolio service identity

Triggered by: POST /api/admin/sync-drive
Schedule: Cloud Scheduler daily at 03:00 ET (separate job from BQ sync)
"""

import io
import logging
import multiprocessing
import queue
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from google.oauth2 import service_account
from google.protobuf import struct_pb2
import google.auth.transport.requests as google_requests_transport

from app.config import settings
from app.reference_data.fixtures import PLATFORM_ADMIN_EMAIL
from app.services.bigquery import (
    ensure_drive_chunks_table,
    ensure_drive_spreadsheet_rows_table,
    get_indexed_drive_files,
    build_drive_chunk_rows,
    build_drive_spreadsheet_row_rows,
    delete_drive_chunks,
    delete_drive_spreadsheet_rows,
    replace_drive_chunks_for_file,
    replace_drive_spreadsheet_rows_for_file,
)
from app.services.spreadsheet_parser import (
    parse_google_sheet_grids,
    parse_xlsx,
    serialize_headers,
    serialize_values,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DRIVE_FOLDER_ID = "REFERENCE_SUPPORT_DOCS_FOLDER_ID"
DRIVE_SUBJECT   = PLATFORM_ADMIN_EMAIL
SOURCE_ID       = "reference_support_docs"
GCP_PROJECT     = settings.google_cloud_project_id
GCS_BUCKET      = "esip-reference-data"
GCS_PREFIX      = f"gdrive/{SOURCE_ID}"
DATA_STORE_ID   = "reference-support-knowledge"
DATASTORE_PARENT = (
    f"projects/{GCP_PROJECT}/locations/global/collections/default_collection"
    f"/dataStores/{DATA_STORE_ID}/branches/default_branch"
)

# mimeTypes to skip entirely (no text content)
SKIP_MIMETYPES = {
    "application/story",  # Articulate Storyline project; no stable text extraction path
    "application/x-zip",  # Storyline packages often appear as generic zip-like binaries
    "application/x-iso9660-image",
    "application/octet-stream",
    "application/x-msdownload",
    "application/x-msdos-program",
    "application/x-dosexec",
    "application/x-msi",
    "application/msword",  # Legacy binary .doc; no safe parser in current Cloud Run image
    "application/vnd.microsoft.portable-executable",
    "video/mp4",
    "application/x-zip-compressed",
    "application/zip",
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.form",      # Google Forms are operational forms, not KB content
    "application/vnd.google-apps.shortcut",  # Do not follow shortcuts during controlled ingestion
}

GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"

# mimeTypes exported as text/plain from Drive API
GOOGLE_DOC_EXPORT_TYPES = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
}

MAX_CHUNK_CHARS = 1000
MIN_CHUNK_CHARS = 20
DRIVE_HTTP_MAX_RETRIES = 5
DRIVE_HTTP_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
PDF_TEXT_EXTRACT_TIMEOUT_SECONDS = 60
DRIVE_LISTING_SAFETY_MULTIPLIER = 20
DRIVE_LISTING_MIN_SAFETY_LIMIT = 250
DRIVE_LISTING_MAX_SAFETY_LIMIT = 5000
SUBTITLE_FILE_EXTENSIONS = (
    ".vtt",
    ".srt",
    ".sbv",
    ".ass",
    ".ssa",
    ".dfxp",
    ".ttml",
    ".smi",
    ".sami",
)
SKIP_FILE_EXTENSIONS = {
    ".story",  # Articulate Storyline package; binary archive, no direct text extraction path
    ".7z",
    ".bin",
    ".cab",
    ".cmd",
    ".com",
    ".doc",
    ".dll",
    ".dmg",
    ".exe",
    ".img",
    ".iso",
    ".msi",
    ".ps1",
    ".reg",
    ".sys",
    ".zip",
}

# Drive sources processed by sync_drive() in one run.
# Keep legacy SOURCE_ID/DRIVE_FOLDER_ID constants above for backward-compatible imports.
DRIVE_SOURCES: list[dict[str, str]] = [
    {
        "source_id": "reference_support_docs",
        "folder_id": "REFERENCE_SUPPORT_DOCS_FOLDER_ID",
        "display_name": "SD Documentation (Google Drive)",
    },
    {
        "source_id": "gdrive_operations_job_aids",
        "folder_id": "REFERENCE_OPERATIONS_JOB_AIDS_FOLDER_ID",
        "display_name": "Store Operations Job Aids",
    },
    {
        "source_id": "gdrive_support_repository",
        "folder_id": "REFERENCE_SUPPORT_REPOSITORY_FOLDER_ID",
        "display_name": "Support Knowledge Repository (Google Drive)",
    },
]


@dataclass
class DriveSyncOptions:
    source_ids: list[str] = field(default_factory=list)
    include_path_prefixes: list[str] = field(default_factory=list)
    include_mime_types: list[str] = field(default_factory=list)
    max_files_per_source: Optional[int] = None
    stop_on_cost_alert: bool = True
    force_reindex: bool = False

    def listing_safety_limit(self) -> Optional[int]:
        if self.max_files_per_source is None:
            return None
        return min(
            DRIVE_LISTING_MAX_SAFETY_LIMIT,
            max(
                DRIVE_LISTING_MIN_SAFETY_LIMIT,
                self.max_files_per_source * DRIVE_LISTING_SAFETY_MULTIPLIER,
            ),
        )

    def normalized_source_ids(self) -> set[str]:
        return {
            str(source_id).strip()
            for source_id in self.source_ids
            if str(source_id).strip()
        }

    def normalized_path_prefixes(self) -> list[str]:
        return [
            _normalize_path(prefix)
            for prefix in self.include_path_prefixes
            if _normalize_path(prefix)
        ]

    def normalized_mime_types(self) -> set[str]:
        return {
            str(mime).strip().lower()
            for mime in self.include_mime_types
            if str(mime).strip()
        }

    def deletions_enabled(self) -> bool:
        return (
            self.max_files_per_source is None
            and not self.normalized_path_prefixes()
            and not self.normalized_mime_types()
            and not self.force_reindex
        )


def _normalize_path(path: str) -> str:
    p = (path or "").strip().strip("/")
    return "/".join(part.strip() for part in p.split("/") if part.strip())


def _path_to_slug(path: str) -> str:
    norm = _normalize_path(path).lower()
    if not norm:
        return "root"
    slug = re.sub(r"[^a-z0-9/_-]+", "_", norm)
    slug = re.sub(r"_+", "_", slug)
    slug = re.sub(r"/+", "/", slug)
    return slug.strip("_/") or "root"


def _is_excluded_path(source_path: str, exclude_prefixes: list[str]) -> bool:
    norm_path = _normalize_path(source_path).lower()
    for prefix in exclude_prefixes:
        norm_prefix = _normalize_path(prefix).lower()
        if not norm_prefix:
            continue
        if norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/"):
            return True
    return False


def _matches_included_path(source_path: str, include_prefixes: list[str]) -> bool:
    if not include_prefixes:
        return True
    norm_path = _normalize_path(source_path).lower()
    for prefix in include_prefixes:
        norm_prefix = _normalize_path(prefix).lower()
        if not norm_prefix:
            continue
        if norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/"):
            return True
    return False


def _should_skip_file_by_name(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return any(lower.endswith(ext) for ext in SKIP_FILE_EXTENSIONS)


def _is_subtitle_file(file_name: str) -> bool:
    lower = (file_name or "").lower()
    return lower.endswith(SUBTITLE_FILE_EXTENSIONS)


def _should_skip_file_by_mime(mime_type: str, file_name: str) -> bool:
    if _is_subtitle_file(file_name):
        return False
    return str(mime_type or "").lower() in SKIP_MIMETYPES


def _file_sort_key(item: dict) -> tuple[str, str, str]:
    return (
        _normalize_path(item.get("source_path", "")).lower(),
        str(item.get("name", "")).lower(),
        str(item.get("id", "")),
    )


def _get_cost_abort_reason() -> Optional[str]:
    """
    Stop long-running Drive sync quickly if the platform cost guard is active.
    """
    from app.services.firestore import get_db  # noqa: PLC0415

    try:
        doc = get_db().document("system/cost_alerts").get()
    except Exception as exc:
        logger.warning("drive_sync: could not read cost_alerts during guard check: %s", exc)
        return None

    if not doc.exists:
        return None

    data = doc.to_dict() or {}
    if bool(data.get("platform_blocked")):
        return "platform_blocked=true"
    if bool(data.get("hard_cap_active")):
        return "hard_cap_active=true"
    if bool(data.get("has_active_alert")):
        alert_type = data.get("alert_type") or data.get("last_alert_type") or "unknown"
        return f"has_active_alert=true ({alert_type})"
    return None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _make_service_creds(scopes: list[str]) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=scopes,
    )


def _get_drive_creds() -> service_account.Credentials:
    """Return refreshed DWD credentials for Drive read access."""
    creds = _make_service_creds(["https://www.googleapis.com/auth/drive.readonly"])
    creds = creds.with_subject(DRIVE_SUBJECT)
    creds.refresh(google_requests_transport.Request())
    return creds


def _get_gcp_token() -> str:
    """Return a fresh GCP access token for GCS REST uploads."""
    creds = _make_service_creds(["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google_requests_transport.Request())
    return creds.token


def _get_doc_client():
    from google.cloud import discoveryengine_v1beta as discoveryengine
    creds = _make_service_creds(["https://www.googleapis.com/auth/cloud-platform"])
    return discoveryengine.DocumentServiceClient(credentials=creds)


# ---------------------------------------------------------------------------
# Drive API helpers
# ---------------------------------------------------------------------------

def list_all_drive_files(
    drive_creds: service_account.Credentials,
    folder_id: Optional[str] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    progress_label: str = "Drive",
    include_path_prefixes: Optional[list[str]] = None,
    exclude_path_prefixes: Optional[list[str]] = None,
    max_processable_files: Optional[int] = None,
    stop_on_cost_alert: bool = False,
) -> list[dict]:
    """
    Recursively list all non-folder, non-skipped files in folder_id.
    Returns list of {id, name, mimeType, modifiedTime, source_path}.
    """
    target_folder_id = folder_id or DRIVE_FOLDER_ID
    headers = {"Authorization": f"Bearer {drive_creds.token}"}
    include_prefixes = [
        _normalize_path(prefix).lower()
        for prefix in (include_path_prefixes or [])
        if _normalize_path(prefix)
    ]
    exclude_prefixes = [
        _normalize_path(prefix).lower()
        for prefix in (exclude_path_prefixes or [])
        if _normalize_path(prefix)
    ]

    def progress(message: str) -> None:
        if progress_cb:
            progress_cb(message)

    def check_abort() -> None:
        if not stop_on_cost_alert:
            return
        abort_reason = _get_cost_abort_reason()
        if abort_reason:
            raise RuntimeError(f"Drive sync stopped by cost guard during Drive listing: {abort_reason}")

    def path_can_match_include(path: str) -> bool:
        if not include_prefixes:
            return True
        norm_path = _normalize_path(path).lower()
        if not norm_path:
            return True
        for prefix in include_prefixes:
            if norm_path == prefix or norm_path.startswith(prefix + "/") or prefix.startswith(norm_path + "/"):
                return True
        return False

    def path_is_excluded(path: str) -> bool:
        norm_path = _normalize_path(path).lower()
        for prefix in exclude_prefixes:
            if norm_path == prefix or norm_path.startswith(prefix + "/"):
                return True
        return False

    def drive_get_with_retry(
        url: str,
        params: dict,
        timeout_seconds: int,
        context: str,
    ) -> requests.Response:
        for attempt in range(1, DRIVE_HTTP_MAX_RETRIES + 1):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
                if r.status_code == 401:
                    # Token can expire during long sync runs; refresh and retry.
                    drive_creds.refresh(google_requests_transport.Request())
                    headers["Authorization"] = f"Bearer {drive_creds.token}"
                    if attempt < DRIVE_HTTP_MAX_RETRIES:
                        time.sleep(min(8, 2 ** (attempt - 1)))
                        continue
                if r.status_code in DRIVE_HTTP_RETRY_STATUS_CODES:
                    if attempt < DRIVE_HTTP_MAX_RETRIES:
                        logger.warning(
                            "drive_sync: transient Drive API status %s for %s (attempt %d/%d)",
                            r.status_code,
                            context,
                            attempt,
                            DRIVE_HTTP_MAX_RETRIES,
                        )
                        time.sleep(min(8, 2 ** (attempt - 1)))
                        continue
                r.raise_for_status()
                return r
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
                if attempt >= DRIVE_HTTP_MAX_RETRIES:
                    raise
                logger.warning(
                    "drive_sync: transient Drive API transport error for %s (attempt %d/%d): %s",
                    context,
                    attempt,
                    DRIVE_HTTP_MAX_RETRIES,
                    exc,
                )
                time.sleep(min(8, 2 ** (attempt - 1)))
        raise RuntimeError(f"drive_sync: exhausted Drive API retries for {context}")

    def list_folder(folder_id: str, current_path: str, stats: dict[str, int]) -> list[dict]:
        url = "https://www.googleapis.com/drive/v3/files"
        params: dict = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
            "pageSize": 100,
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }
        items: list[dict] = []
        while True:
            check_abort()
            r = drive_get_with_retry(
                url=url,
                params=params,
                timeout_seconds=30,
                context=f"list folder {folder_id}",
            )
            data = r.json()
            page_items = data.get("files", [])
            items.extend(page_items)
            stats["items_seen"] += len(page_items)
            progress(
                f"{progress_label} listing: {stats['folders_visited']} folders, "
                f"{stats['items_seen']} items seen, {stats['processable_files']} processable files"
                + (f", current path: {current_path or 'root'}" if current_path else "")
            )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token
        return items

    def recurse(folder_id: str, current_path: str, stats: dict[str, int]) -> list[dict]:
        check_abort()
        stats["folders_visited"] += 1
        all_files: list[dict] = []
        for item in list_folder(folder_id, current_path, stats):
            mime = item["mimeType"]
            if mime == "application/vnd.google-apps.folder":
                child_path = _normalize_path(f"{current_path}/{item['name']}")
                if not path_is_excluded(child_path) and path_can_match_include(child_path):
                    all_files.extend(recurse(item["id"], child_path, stats))
                    if max_processable_files is not None and stats["processable_files"] >= max_processable_files:
                        return all_files
            elif (
                not _should_skip_file_by_mime(mime, item.get("name", ""))
                and not _should_skip_file_by_name(item.get("name", ""))
            ):
                item["source_path"] = _normalize_path(current_path)
                all_files.append(item)
                stats["processable_files"] += 1
                if max_processable_files is not None and stats["processable_files"] >= max_processable_files:
                    progress(
                        f"{progress_label} listing limit reached: "
                        f"{stats['processable_files']} processable files"
                    )
                    return all_files
        return all_files

    stats = {"folders_visited": 0, "items_seen": 0, "processable_files": 0}
    files = recurse(target_folder_id, "", stats)
    logger.info(
        "drive_sync: found %d processable files in Drive folder %s after visiting %d folders",
        len(files),
        target_folder_id,
        stats["folders_visited"],
    )
    return files


def download_file(file_id: str, mime_type: str, drive_creds: service_account.Credentials) -> bytes:
    """Download file bytes from Drive. Exports Google Workspace files to plain text."""
    headers = {"Authorization": f"Bearer {drive_creds.token}"}
    if mime_type in GOOGLE_DOC_EXPORT_TYPES:
        export_mime = GOOGLE_DOC_EXPORT_TYPES[mime_type]
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
        params: dict = {"mimeType": export_mime, "supportsAllDrives": True}
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        params = {"alt": "media", "supportsAllDrives": True}

    for attempt in range(1, DRIVE_HTTP_MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=120)
            if r.status_code == 401:
                drive_creds.refresh(google_requests_transport.Request())
                headers["Authorization"] = f"Bearer {drive_creds.token}"
                if attempt < DRIVE_HTTP_MAX_RETRIES:
                    time.sleep(min(8, 2 ** (attempt - 1)))
                    continue
            if r.status_code in DRIVE_HTTP_RETRY_STATUS_CODES:
                if attempt < DRIVE_HTTP_MAX_RETRIES:
                    logger.warning(
                        "drive_sync: transient download status %s for file %s (attempt %d/%d)",
                        r.status_code,
                        file_id,
                        attempt,
                        DRIVE_HTTP_MAX_RETRIES,
                    )
                    time.sleep(min(8, 2 ** (attempt - 1)))
                    continue
            r.raise_for_status()
            return r.content
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
            if attempt >= DRIVE_HTTP_MAX_RETRIES:
                raise
            logger.warning(
                "drive_sync: transient download transport error for %s (attempt %d/%d): %s",
                file_id,
                attempt,
                DRIVE_HTTP_MAX_RETRIES,
                exc,
            )
            time.sleep(min(8, 2 ** (attempt - 1)))
    raise RuntimeError(f"drive_sync: exhausted download retries for {file_id}")


def read_google_sheet_grids(
    file_id: str,
    drive_creds: service_account.Credentials,
) -> list[tuple[str, list[list[str]]]]:
    """
    Read a Google Sheet through the native Sheets API.

    Values are pulled from `formattedValue` so dates, currencies, percentages,
    formulas, and locale-specific display text match what engineers see in the
    sheet. Merged ranges are intentionally not expanded across all covered
    cells; the top-left value is enough for section/title detection and avoids
    polluting rows with many duplicated columns.
    """
    headers = {"Authorization": f"Bearer {drive_creds.token}"}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}"
    params = {
        "includeGridData": "true",
        "fields": (
            "sheets(properties(sheetId,title,index,hidden),"
            "merges(startRowIndex,endRowIndex,startColumnIndex,endColumnIndex),"
            "data(startRow,startColumn,rowData(values(formattedValue))))"
        ),
    }

    for attempt in range(1, DRIVE_HTTP_MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=120)
            if r.status_code == 401:
                drive_creds.refresh(google_requests_transport.Request())
                headers["Authorization"] = f"Bearer {drive_creds.token}"
                if attempt < DRIVE_HTTP_MAX_RETRIES:
                    time.sleep(min(8, 2 ** (attempt - 1)))
                    continue
            if r.status_code in DRIVE_HTTP_RETRY_STATUS_CODES:
                if attempt < DRIVE_HTTP_MAX_RETRIES:
                    logger.warning(
                        "drive_sync: transient Sheets API status %s for file %s (attempt %d/%d)",
                        r.status_code,
                        file_id,
                        attempt,
                        DRIVE_HTTP_MAX_RETRIES,
                    )
                    time.sleep(min(8, 2 ** (attempt - 1)))
                    continue
            r.raise_for_status()
            data = r.json()
            sheets = []
            for sheet in sorted(
                data.get("sheets", []),
                key=lambda s: int(s.get("properties", {}).get("index", 0)),
            ):
                props = sheet.get("properties", {})
                title = str(props.get("title") or "Sheet")
                rows = _extract_google_sheet_rows(sheet)
                sheets.append((title, rows))
            return sheets
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
            if attempt >= DRIVE_HTTP_MAX_RETRIES:
                raise
            logger.warning(
                "drive_sync: transient Sheets API transport error for %s (attempt %d/%d): %s",
                file_id,
                attempt,
                DRIVE_HTTP_MAX_RETRIES,
                exc,
            )
            time.sleep(min(8, 2 ** (attempt - 1)))
    raise RuntimeError(f"drive_sync: exhausted Sheets API retries for {file_id}")


def _extract_google_sheet_rows(sheet: dict) -> list[list[str]]:
    rows: list[list[str]] = []

    def ensure_size(row_idx: int, col_idx: int) -> None:
        while len(rows) <= row_idx:
            rows.append([])
        while len(rows[row_idx]) <= col_idx:
            rows[row_idx].append("")

    for grid in sheet.get("data", []) or []:
        start_row = int(grid.get("startRow") or 0)
        start_col = int(grid.get("startColumn") or 0)
        for row_offset, row_data in enumerate(grid.get("rowData", []) or []):
            row_idx = start_row + row_offset
            for col_offset, cell in enumerate(row_data.get("values", []) or []):
                value = str(cell.get("formattedValue") or "").strip()
                if not value:
                    continue
                col_idx = start_col + col_offset
                ensure_size(row_idx, col_idx)
                rows[row_idx][col_idx] = value

    trimmed_rows: list[list[str]] = []
    for row in rows:
        cleaned = [str(cell).strip() for cell in row]
        while cleaned and not cleaned[-1]:
            cleaned.pop()
        trimmed_rows.append(cleaned)
    return trimmed_rows


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(content: bytes, mime_type: str, file_name: str) -> str:
    """
    Extract plain text from file content based on mimeType.
    Returns empty string on failure or if no text found.
    """
    try:
        lower_name = (file_name or "").lower()

        # Some subtitle/text assets from Drive Shared Folders are returned as
        # application/octet-stream even though they are plain UTF-8 text.
        # Keep them indexable instead of skipping as unsupported binaries.
        if lower_name.endswith(SUBTITLE_FILE_EXTENSIONS):
            return content.decode("utf-8", errors="replace")

        # Google Workspace exports come as text/plain or CSV already
        if mime_type in GOOGLE_DOC_EXPORT_TYPES:
            return content.decode("utf-8", errors="replace")

        # Generic text fallback (covers text/plain, text/csv and similar).
        if mime_type.startswith("text/"):
            return content.decode("utf-8", errors="replace")

        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            return _extract_docx(content)

        if mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            return _extract_pptx(content)

        if mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            return _extract_xlsx_flat(content)

        if mime_type == "application/pdf":
            return _extract_pdf(content, file_name)

        if mime_type in ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"):
            return _ocr_bytes(content, mime_type, file_name)

        logger.warning("drive_sync: unsupported mimeType '%s' for %s — skipping", mime_type, file_name)
        return ""

    except Exception as exc:
        logger.error("drive_sync: extract_text error for %s (%s): %s", file_name, mime_type, exc)
        return ""


def _extract_docx(content: bytes) -> str:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(content))
    lines: list[str] = []

    def add_text(text: str) -> None:
        normalized = re.sub(r"[ \t]+", " ", text).strip()
        if normalized:
            lines.append(normalized)

    def table_lines(table: Table) -> list[str]:
        table_text: list[str] = []
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                cell_text = " ".join(
                    p.text.strip()
                    for p in cell.paragraphs
                    if p.text and p.text.strip()
                )
                for nested_table in cell.tables:
                    nested_text = " ".join(table_lines(nested_table))
                    if nested_text:
                        cell_text = f"{cell_text} {nested_text}".strip()
                cells.append(re.sub(r"[ \t]+", " ", cell_text).strip())
            row_text = " | ".join(c for c in cells if c)
            if row_text:
                table_text.append(row_text)
        return table_text

    # python-docx's Document.paragraphs excludes content stored in Word tables.
    # Many job aids are table-based, so preserve body order across paragraphs and tables.
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            add_text(Paragraph(child, doc).text)
        elif child.tag == qn("w:tbl"):
            for line in table_lines(Table(child, doc)):
                add_text(line)

    for section in doc.sections:
        for container in (section.header, section.footer):
            for paragraph in container.paragraphs:
                add_text(paragraph.text)
            for table in container.tables:
                for line in table_lines(table):
                    add_text(line)

    return "\n\n".join(lines)


def _extract_pptx(content: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(content))
    lines: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                lines.append(shape.text.strip())
        lines.append("")
    return "\n".join(lines)


def _extract_xlsx_flat(content: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows_text: list[str] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows_text.append(f"[Sheet: {sheet_name}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(c.strip() for c in cells):
                rows_text.append("\t".join(cells).strip())
    return "\n".join(rows_text)


def _extract_document_payload(content: bytes, mime_type: str, file_name: str) -> dict:
    """
    Return normalized ingestion payload.

    text: full document text for GCS / Vertex semantic indexing
    chunks: row- or paragraph-level units for drive_chunks exact search
    spreadsheet_rows: structured row-level records for spreadsheet-specific search
    """
    if mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        parsed = parse_xlsx(content, file_name)
        if parsed.row_records:
            records = []
            for row in parsed.row_records:
                records.append(
                    {
                        **row.to_dict(),
                        "headers_json": serialize_headers(row.headers),
                        "values_json": serialize_values(row.values_by_column),
                    }
                )
            return {
                "text": parsed.full_text,
                "chunks": parsed.chunk_texts(),
                "spreadsheet_rows": records,
                "source_format": parsed.source_format,
            }

    text = extract_text(content, mime_type, file_name)
    return {
        "text": text,
        "chunks": chunk_text(text) if text.strip() else [],
        "spreadsheet_rows": [],
        "source_format": "",
    }


def _extract_google_sheet_payload(
    file_id: str,
    file_name: str,
    drive_creds: service_account.Credentials,
) -> dict:
    sheets = read_google_sheet_grids(file_id, drive_creds)
    parsed = parse_google_sheet_grids(sheets, file_name)
    records = []
    for row in parsed.row_records:
        records.append(
            {
                **row.to_dict(),
                "headers_json": serialize_headers(row.headers),
                "values_json": serialize_values(row.values_by_column),
            }
        )
    return {
        "text": parsed.full_text,
        "chunks": parsed.chunk_texts(),
        "spreadsheet_rows": records,
        "source_format": parsed.source_format,
    }


def _pypdf_extract_worker(content: bytes, result_queue: multiprocessing.Queue) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        result_queue.put(("ok", "\n\n".join(pages).strip()))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def _extract_pdf_with_timeout(content: bytes, file_name: str) -> str:
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_pypdf_extract_worker, args=(content, result_queue), daemon=True)
    proc.start()
    proc.join(PDF_TEXT_EXTRACT_TIMEOUT_SECONDS)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        logger.warning(
            "drive_sync: pypdf timed out after %ss for %s",
            PDF_TEXT_EXTRACT_TIMEOUT_SECONDS,
            file_name,
        )
        return ""

    try:
        status, value = result_queue.get_nowait()
    except queue.Empty:
        logger.warning("drive_sync: pypdf returned no result for %s", file_name)
        return ""
    if status == "error":
        logger.warning("drive_sync: pypdf failed for %s: %s", file_name, value)
        return ""
    return str(value or "")


def _extract_pdf(content: bytes, file_name: str) -> str:
    """Extract text from PDF. Falls back to Gemini Vision OCR for scanned PDFs."""
    import re as _re
    raw = _extract_pdf_with_timeout(content, file_name)
    # pypdf sometimes produces double-spaced text (e.g. "Oracle  POS  and  Cash  Drawers")
    # for PDFs with certain encodings. Normalize runs of spaces to a single space,
    # but preserve newlines (which carry structural meaning).
    text = _re.sub(r"[ \t]{2,}", " ", raw)

    if len(text) >= 100:
        return text

    abort_reason = _get_cost_abort_reason()
    if abort_reason:
        raise RuntimeError(f"Drive sync stopped by cost guard before PDF OCR fallback: {abort_reason}")

    # Scanned PDF — Gemini Vision supports application/pdf natively
    logger.info("drive_sync: %s has <100 chars extracted — using Gemini Vision OCR", file_name)
    return _ocr_bytes(content, "application/pdf", file_name)


def _ocr_bytes(content: bytes, mime_type: str, file_name: str) -> str:
    """Use Gemini Vision OCR to extract text from an image or scanned PDF."""
    from app.services.gemini import ocr_image
    text, err = ocr_image(content, mime_type)
    if err:
        logger.warning("drive_sync: OCR error for %s (mime=%s): %s", file_name, mime_type, err)
        return ""
    return text or ""


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str) -> list[str]:
    """
    Split text into paragraph-level chunks for BQ drive_chunks.
    Split on double newlines; further split on single newlines if chunk > MAX_CHUNK_CHARS.
    """
    paragraphs = [p.strip() for p in text.split("\n\n")]
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) < MIN_CHUNK_CHARS:
            continue
        if len(para) <= MAX_CHUNK_CHARS:
            chunks.append(para)
        else:
            # Long paragraph: split on single newlines
            current = ""
            for line in para.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if current and len(current) + len(line) + 1 > MAX_CHUNK_CHARS:
                    if len(current) >= MIN_CHUNK_CHARS:
                        chunks.append(current)
                    current = line
                else:
                    current = f"{current}\n{line}" if current else line
            if len(current) >= MIN_CHUNK_CHARS:
                chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# GCS operations
# ---------------------------------------------------------------------------

def upload_to_gcs(file_id: str, text: str, gcp_token: str, source_id: str = SOURCE_ID) -> str:
    """Upload text as UTF-8 to GCS. Returns GCS URI. Uses REST to avoid ADC conflicts."""
    blob_name = f"gdrive/{source_id}/{file_id}.txt"
    gcs_uri = f"gs://{GCS_BUCKET}/{blob_name}"
    token = gcp_token
    payload = text.encode("utf-8")
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET}/o"
    params = {"uploadType": "media", "name": blob_name}

    for attempt in range(1, DRIVE_HTTP_MAX_RETRIES + 1):
        try:
            r = requests.post(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain; charset=utf-8",
                },
                data=payload,
                timeout=30,
            )
            if r.status_code == 401 and attempt < DRIVE_HTTP_MAX_RETRIES:
                logger.warning(
                    "drive_sync: GCS upload token rejected for %s; refreshing token (attempt %d/%d)",
                    blob_name,
                    attempt,
                    DRIVE_HTTP_MAX_RETRIES,
                )
                token = _get_gcp_token()
                time.sleep(min(8, 2 ** (attempt - 1)))
                continue
            if r.status_code in DRIVE_HTTP_RETRY_STATUS_CODES and attempt < DRIVE_HTTP_MAX_RETRIES:
                logger.warning(
                    "drive_sync: transient GCS upload status %s for %s (attempt %d/%d)",
                    r.status_code,
                    blob_name,
                    attempt,
                    DRIVE_HTTP_MAX_RETRIES,
                )
                time.sleep(min(8, 2 ** (attempt - 1)))
                continue
            r.raise_for_status()
            return gcs_uri
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
            if attempt >= DRIVE_HTTP_MAX_RETRIES:
                raise
            logger.warning(
                "drive_sync: transient GCS upload transport error for %s (attempt %d/%d): %s",
                blob_name,
                attempt,
                DRIVE_HTTP_MAX_RETRIES,
                exc,
            )
            if attempt >= 2:
                token = _get_gcp_token()
            time.sleep(min(8, 2 ** (attempt - 1)))
    raise RuntimeError(f"drive_sync: exhausted GCS upload retries for {blob_name}")


def delete_from_gcs(file_id: str, gcp_token: str, source_id: str = SOURCE_ID) -> None:
    """Delete a file from GCS. 404 is treated as success (already gone)."""
    blob_name = f"gdrive/{source_id}/{file_id}.txt"
    encoded = requests.utils.quote(blob_name, safe="")
    r = requests.delete(
        f"https://storage.googleapis.com/storage/v1/b/{GCS_BUCKET}/o/{encoded}",
        headers={"Authorization": f"Bearer {gcp_token}"},
        timeout=15,
    )
    if r.status_code not in (200, 204, 404):
        logger.warning("drive_sync: GCS delete returned %d for %s", r.status_code, blob_name)


# ---------------------------------------------------------------------------
# Vertex AI document management
# ---------------------------------------------------------------------------

def upsert_vertex_doc(
    file_id: str,
    file_name: str,
    gcs_uri: str,
    drive_url: str,
    source_id: str = SOURCE_ID,
    source_path: str = "",
    source_path_slug: str = "root",
) -> None:
    """
    Create or update a document in Vertex AI data store.
    Uses allow_missing=True so update_document acts as upsert.
    """
    from google.cloud import discoveryengine_v1beta as discoveryengine

    doc_client = _get_doc_client()
    doc_name = f"{DATASTORE_PARENT}/documents/{file_id}"

    struct_data = struct_pb2.Struct()
    struct_data["source_id"] = source_id
    struct_data["drive_url"] = drive_url
    struct_data["file_name"] = file_name
    struct_data["source_path"] = source_path
    struct_data["source_path_slug"] = source_path_slug

    content = discoveryengine.Document.Content(
        mime_type="text/plain",
        uri=gcs_uri,
    )

    # allow_missing=True creates the document if it doesn't exist
    updated_doc = discoveryengine.Document(
        name=doc_name,
        struct_data=struct_data,
        content=content,
    )
    try:
        doc_client.update_document(
            discoveryengine.UpdateDocumentRequest(
                document=updated_doc,
                allow_missing=True,
            ),
            timeout=120,
        )
        logger.debug("drive_sync: upserted Vertex AI doc for %s (%s)", file_name, file_id)
    except Exception as exc:
        logger.error("drive_sync: upsert_vertex_doc failed for %s: %s", file_name, exc)
        raise


def delete_vertex_doc(file_id: str) -> None:
    """Delete a document from Vertex AI data store. Logs warning if not found."""
    from google.cloud import discoveryengine_v1beta as discoveryengine

    doc_client = _get_doc_client()
    doc_name = f"{DATASTORE_PARENT}/documents/{file_id}"
    try:
        doc_client.delete_document(
            discoveryengine.DeleteDocumentRequest(name=doc_name),
            timeout=60,
        )
        logger.info("drive_sync: deleted Vertex AI doc %s", file_id)
    except Exception as exc:
        logger.warning("drive_sync: delete_vertex_doc skipped for %s: %s", file_id, exc)


# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------

def sync_drive(
    progress_cb: Optional[Callable[[str], None]] = None,
    options: Optional[DriveSyncOptions] = None,
) -> dict:
    """
    Full sync of SD Documentation Drive folder.

    Steps:
    1. Ensure BQ drive_chunks table exists
    2. List all files recursively in DRIVE_FOLDER_ID
    3. Get currently indexed file_ids from BQ drive_chunks
    4. For each new/modified file: parse → upload GCS → upsert Vertex AI → replace BQ chunks for file
    5. For deleted files: delete from GCS + Vertex AI + BQ chunks

    Returns: {added, updated, deleted, skipped, errors, total_drive_files}
    """
    options = options or DriveSyncOptions()
    selected_source_ids = options.normalized_source_ids()
    include_path_prefixes = options.normalized_path_prefixes()
    include_mime_types = options.normalized_mime_types()

    def progress(msg: str) -> None:
        logger.info("drive_sync: %s", msg)
        if progress_cb:
            progress_cb(msg)

    progress("Ensuring drive_chunks BQ table exists...")
    ensure_drive_chunks_table()
    ensure_drive_spreadsheet_rows_table()

    progress("Fetching Drive/GCP credentials...")
    drive_creds = _get_drive_creds()
    gcp_token = _get_gcp_token()

    added = updated = deleted = skipped = errors = 0
    total_drive_files = 0
    per_source: dict[str, dict] = {}

    from app.services.firestore import get_data_sources

    source_cfg = get_data_sources()

    for source in DRIVE_SOURCES:
        source_id = source["source_id"]
        if selected_source_ids and source_id not in selected_source_ids:
            continue

        folder_id = source["folder_id"]
        display_name = source["display_name"]
        cfg = source_cfg.get(source_id, {})
        sync_enabled = bool(cfg.get("sync_enabled", True)) if isinstance(cfg, dict) else True
        if cfg and not sync_enabled:
            per_source[source_id] = {
                "display_name": display_name,
                "total_drive_files": 0,
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "skipped": 0,
                "errors": 0,
                "excluded": 0,
                "status": "sync_disabled",
            }
            progress(f"[{source_id}] Source sync is disabled — skipping sync.")
            continue
        exclude_prefixes = cfg.get("exclude_path_prefixes") if isinstance(cfg, dict) else []
        if not isinstance(exclude_prefixes, list):
            exclude_prefixes = []
        exclude_prefixes = [str(p) for p in exclude_prefixes if str(p).strip()]

        progress(f"[{source_id}] Listing Drive files...")
        try:
            listing_limit = options.listing_safety_limit()
            drive_files = list_all_drive_files(
                drive_creds,
                folder_id=folder_id,
                progress_cb=lambda msg, sid=source_id: progress(f"[{sid}] {msg}"),
                progress_label=source_id,
                include_path_prefixes=include_path_prefixes,
                exclude_path_prefixes=exclude_prefixes,
                max_processable_files=listing_limit,
                stop_on_cost_alert=options.stop_on_cost_alert,
            )
        except Exception as exc:
            logger.error(
                "drive_sync: [%s] failed while listing files: %s",
                source_id,
                exc,
                exc_info=True,
            )
            per_source[source_id] = {
                "display_name": display_name,
                "total_drive_files": 0,
                "added": 0,
                "updated": 0,
                "deleted": 0,
                "skipped": 0,
                "errors": 1,
                "excluded": 0,
                "exclude_path_prefixes": exclude_prefixes,
                "status": "listing_failed",
            }
            errors += 1
            continue
        excluded_files = [f for f in drive_files if _is_excluded_path(f.get("source_path", ""), exclude_prefixes)]
        drive_files = [f for f in drive_files if not _is_excluded_path(f.get("source_path", ""), exclude_prefixes)]
        path_filtered_out = 0
        if include_path_prefixes:
            before = len(drive_files)
            drive_files = [
                f for f in drive_files
                if _matches_included_path(f.get("source_path", ""), include_path_prefixes)
            ]
            path_filtered_out = before - len(drive_files)
        drive_files = sorted(drive_files, key=_file_sort_key)
        total_drive_files += len(drive_files)

        progress(f"[{source_id}] Checking BQ indexed state...")
        indexed = get_indexed_drive_files(source_id)  # {file_id: modified_at_iso}
        drive_ids = {f["id"] for f in drive_files}
        deleted_ids = set(indexed.keys()) - drive_ids if options.deletions_enabled() else set()

        pending_files: list[dict] = []
        s_added = s_updated = s_deleted = s_skipped = s_errors = 0
        skipped_unchanged = skipped_name = skipped_mime = skipped_no_text = 0
        file_warnings: list[dict[str, str]] = []
        file_errors: list[dict[str, str]] = []

        def remember_issue(target: list[dict[str, str]], file: dict | str, reason: str) -> None:
            if len(target) >= 25:
                return
            if isinstance(file, dict):
                target.append({
                    "file_id": str(file.get("id", "")),
                    "file_name": str(file.get("name", "")),
                    "source_path": str(file.get("source_path", "")),
                    "mime_type": str(file.get("mimeType", "")),
                    "reason": reason[:500],
                })
            else:
                target.append({"file_id": str(file), "reason": reason[:500]})

        for file in drive_files:
            file_name = file["name"]
            mime_type = str(file["mimeType"]).lower()
            modified = file.get("modifiedTime", "")
            existing_modified = indexed.get(file["id"])
            modified_sec = modified[:19]

            if _should_skip_file_by_name(file_name):
                skipped_name += 1
                continue

            if include_mime_types and mime_type not in include_mime_types:
                skipped_mime += 1
                continue

            if existing_modified and existing_modified[:19] >= modified_sec and not options.force_reindex:
                skipped_unchanged += 1
                continue

            pending_files.append(file)

        if options.max_files_per_source is not None:
            run_files = pending_files[: max(0, options.max_files_per_source)]
        else:
            run_files = pending_files

        s_skipped += skipped_unchanged + skipped_name + skipped_mime
        remaining_after_limit = max(0, len(pending_files) - len(run_files))

        for i, file in enumerate(run_files, 1):
            if options.stop_on_cost_alert:
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

            existing_modified = indexed.get(file_id)
            modified_sec = modified[:19]
            if existing_modified and existing_modified[:19] >= modified_sec and not options.force_reindex:
                s_skipped += 1
                continue

            if _should_skip_file_by_name(file_name):
                logger.info(
                    "drive_sync: skip non-indexable extension for %s (%s)",
                    file_name,
                    source_id,
                )
                s_skipped += 1
                continue

            is_new = file_id not in indexed
            progress(
                f"[{source_id}] [{i}/{len(run_files)}] "
                f"{'Adding' if is_new else 'Updating'}: {file_name}"
            )

            try:
                if mime_type == GOOGLE_SHEETS_MIME:
                    progress(f"[{source_id}] [{i}/{len(run_files)}] Reading sheet: {file_name}")
                    payload = _extract_google_sheet_payload(file_id, file_name, drive_creds)
                else:
                    progress(f"[{source_id}] [{i}/{len(run_files)}] Downloading: {file_name}")
                    content = download_file(file_id, mime_type, drive_creds)
                    progress(f"[{source_id}] [{i}/{len(run_files)}] Extracting text: {file_name}")
                    payload = _extract_document_payload(content, mime_type, file_name)
                text = payload["text"]
                if not text.strip():
                    logger.warning("drive_sync: no text extracted from %s (%s) — skipping", file_name, source_id)
                    skipped_no_text += 1
                    remember_issue(file_warnings, file, "no_text_extracted")
                    s_skipped += 1
                    continue

                full_text = (
                    f"FILE: {file_name}\n"
                    f"SOURCE: {display_name}\n"
                    f"SOURCE PATH: {source_path or 'root'}\n\n"
                    f"{text}"
                )
                progress(f"[{source_id}] [{i}/{len(run_files)}] Uploading GCS text: {file_name}")
                gcs_uri = upload_to_gcs(file_id, full_text, gcp_token, source_id=source_id)
                progress(f"[{source_id}] [{i}/{len(run_files)}] Upserting Vertex doc: {file_name}")
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
                progress(
                    f"[{source_id}] [{i}/{len(run_files)}] Writing BQ chunks "
                    f"({len(chunk_units)}): {file_name}"
                )
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
                    progress(
                        f"[{source_id}] [{i}/{len(run_files)}] Writing BQ spreadsheet rows "
                        f"({len(spreadsheet_rows)}): {file_name}"
                    )
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
                    s_added += 1
                else:
                    s_updated += 1
            except Exception as exc:
                logger.error("drive_sync: error processing %s (%s): %s", file_name, source_id, exc, exc_info=True)
                remember_issue(file_errors, file, str(exc))
                s_errors += 1

            time.sleep(0.1)

        for file_id in deleted_ids:
            progress(f"[{source_id}] Deleting removed file: {file_id}")
            try:
                delete_from_gcs(file_id, gcp_token, source_id=source_id)
                delete_vertex_doc(file_id)
                delete_drive_chunks(file_id)
                delete_drive_spreadsheet_rows(file_id)
                s_deleted += 1
            except Exception as exc:
                logger.error("drive_sync: error deleting %s (%s): %s", file_id, source_id, exc)
                remember_issue(file_errors, file_id, f"delete_failed:{exc}")
                s_errors += 1

        per_source[source_id] = {
            "display_name": display_name,
            "total_drive_files": len(drive_files),
            "pending_candidates": len(pending_files),
            "selected_for_run": len(run_files),
            "remaining_after_limit": remaining_after_limit,
            "added": s_added,
            "updated": s_updated,
            "deleted": s_deleted,
            "skipped": s_skipped,
            "errors": s_errors,
            "excluded": len(excluded_files),
            "path_filtered_out": path_filtered_out,
            "skipped_unchanged": skipped_unchanged,
            "skipped_name": skipped_name,
            "skipped_mime": skipped_mime,
            "skipped_no_text": skipped_no_text,
            "file_warnings": file_warnings,
            "file_errors": file_errors,
            "exclude_path_prefixes": exclude_prefixes,
            "include_path_prefixes": include_path_prefixes,
            "include_mime_types": sorted(include_mime_types),
            "force_reindex": options.force_reindex,
            "deletes_enabled": options.deletions_enabled(),
        }

        added += s_added
        updated += s_updated
        deleted += s_deleted
        skipped += s_skipped
        errors += s_errors

    result = {
        "added": added,
        "updated": updated,
        "deleted": deleted,
        "skipped": skipped,
        "errors": errors,
        "total_drive_files": total_drive_files,
        "options": {
            "source_ids": sorted(selected_source_ids),
            "include_path_prefixes": include_path_prefixes,
            "include_mime_types": sorted(include_mime_types),
            "max_files_per_source": options.max_files_per_source,
            "stop_on_cost_alert": options.stop_on_cost_alert,
            "force_reindex": options.force_reindex,
            "deletes_enabled": options.deletions_enabled(),
        },
        "sources": per_source,
    }
    logger.info("drive_sync: complete — %s", result)
    return result
