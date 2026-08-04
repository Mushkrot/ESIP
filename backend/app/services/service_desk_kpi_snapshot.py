"""Cloud-side Service Desk KPI Excel snapshot storage."""

from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from google.cloud import storage

from app.config import settings
from app.services import service_desk_kpi, service_desk_kpi_export

_DEFAULT_BUCKET = "esip-reference-data"
_DEFAULT_PREFIX = "reference_kpi_reports"
_SNAPSHOT_NAME_RE = re.compile(r"service-desk-kpi-(\d{4}-\d{2}-\d{2})\.xlsx$")
_DEFAULT_LIST_LIMIT = 31
_MAX_LIST_LIMIT = 180


@dataclass(frozen=True)
class SnapshotResult:
    status: str
    report_date: str
    bucket: str
    blob_name: str
    gcs_uri: str
    filename: str
    media_type: str
    size_bytes: int
    metadata: dict[str, str] | None = None
    telephony_status: dict[str, Any] | None = None


@dataclass(frozen=True)
class SnapshotArtifact:
    report_date: str
    bucket: str
    blob_name: str
    gcs_uri: str
    filename: str
    media_type: str
    size_bytes: int
    updated_at: str | None


@dataclass(frozen=True)
class SnapshotDownload:
    filename: str
    media_type: str
    content: bytes


class SnapshotNotFoundError(FileNotFoundError):
    """Raised when a requested KPI snapshot blob does not exist."""


class SnapshotStaleTelephonyError(RuntimeError):
    """Raised when a snapshot would be written with non-final CXone data."""


def default_report_date() -> str:
    today = datetime.now(service_desk_kpi.BUSINESS_TIMEZONE).date()
    return (today - timedelta(days=1)).isoformat()


def normalize_report_date(report_date: str | None) -> str:
    value = str(report_date or default_report_date())[:10]
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def resolve_bucket_name(bucket_name: str | None = None) -> str:
    value = (bucket_name or settings.reference_kpi_reports_bucket or _DEFAULT_BUCKET).strip()
    return value or _DEFAULT_BUCKET


def resolve_prefix(prefix: str | None = None) -> str:
    value = (prefix or settings.reference_kpi_reports_prefix or _DEFAULT_PREFIX).strip().strip("/")
    return value or _DEFAULT_PREFIX


def build_blob_name(report_date: str, *, prefix: str | None = None) -> str:
    clean_prefix = resolve_prefix(prefix)
    return f"{clean_prefix}/{report_date[:7]}/service-desk-kpi-{report_date}.xlsx"


def build_gcs_uri(bucket_name: str, blob_name: str) -> str:
    return f"gs://{bucket_name}/{blob_name}"


def _get_storage_client() -> storage.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return storage.Client(project=settings.google_cloud_project_id)


def _source_revision() -> str:
    return os.environ.get("K_REVISION") or os.environ.get("SOURCE_REVISION") or "local"


def _metadata_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _reload_blob_metadata(blob: storage.Blob, client: storage.Client) -> None:
    try:
        blob.reload(client=client)
    except TypeError:
        blob.reload()
    except AttributeError:
        return


def _existing_snapshot_is_fresh(blob: storage.Blob, *, client: storage.Client, report_date: str) -> bool:
    _reload_blob_metadata(blob, client)
    metadata = blob.metadata or {}
    return metadata.get("report_date") == report_date and _metadata_bool(metadata.get("cxone_cache_fresh"))


def _snapshot_metadata(report_date: str, export: service_desk_kpi_export.KpiExportFile) -> dict[str, str]:
    generated_at = export.metadata.get("generated_at") or datetime.now(service_desk_kpi.BUSINESS_TIMEZONE).isoformat()
    telephony_status = export.metadata.get("telephony_status") or {}
    return {
        "report_date": report_date,
        "generated_at": str(generated_at),
        "generated_by": "esip-service-desk-kpi-snapshot",
        "media_type": export.media_type,
        "telephony_status": str(telephony_status.get("status") or ""),
        "telephony_status_json": json.dumps(telephony_status, sort_keys=True, default=str),
        "cxone_run_started_at": str(export.metadata.get("cxone_run_started_at") or ""),
        "cxone_run_completed_at": str(export.metadata.get("cxone_run_completed_at") or ""),
        "cxone_cache_fresh": str(bool(export.metadata.get("cxone_cache_fresh"))).lower(),
        "source_revision": _source_revision(),
    }


def _export_uses_fresh_telephony(export: service_desk_kpi_export.KpiExportFile) -> bool:
    return bool(export.metadata.get("cxone_cache_fresh"))


def _normalize_list_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIST_LIMIT
    return max(1, min(int(limit), _MAX_LIST_LIMIT))


def _blob_to_artifact(bucket_name: str, blob: storage.Blob) -> SnapshotArtifact | None:
    match = _SNAPSHOT_NAME_RE.search(blob.name or "")
    if not match:
        return None
    report_date = match.group(1)
    updated_at = blob.updated.isoformat() if blob.updated is not None else None
    return SnapshotArtifact(
        report_date=report_date,
        bucket=bucket_name,
        blob_name=blob.name,
        gcs_uri=build_gcs_uri(bucket_name, blob.name),
        filename=f"service-desk-kpi-{report_date}.xlsx",
        media_type=blob.content_type or service_desk_kpi_export.XLSX_MEDIA_TYPE,
        size_bytes=int(blob.size or 0),
        updated_at=updated_at,
    )


def list_snapshots(
    *,
    bucket_name: str | None = None,
    prefix: str | None = None,
    limit: int | None = None,
) -> list[SnapshotArtifact]:
    bucket = resolve_bucket_name(bucket_name)
    prefix_value = resolve_prefix(prefix)
    client = _get_storage_client()
    blobs: Iterable[storage.Blob] = client.list_blobs(bucket, prefix=f"{prefix_value}/")
    artifacts = [artifact for blob in blobs if (artifact := _blob_to_artifact(bucket, blob)) is not None]
    artifacts.sort(key=lambda item: (item.report_date, item.updated_at or ""), reverse=True)
    return artifacts[: _normalize_list_limit(limit)]


def download_snapshot(
    report_date: str,
    *,
    bucket_name: str | None = None,
    prefix: str | None = None,
) -> SnapshotDownload:
    report_day = normalize_report_date(report_date)
    bucket = resolve_bucket_name(bucket_name)
    blob_name = build_blob_name(report_day, prefix=prefix)
    client = _get_storage_client()
    blob = client.bucket(bucket).blob(blob_name)
    if not blob.exists(client):
        raise SnapshotNotFoundError(f"Service Desk KPI snapshot not found for {report_day}")
    return SnapshotDownload(
        filename=f"service-desk-kpi-{report_day}.xlsx",
        media_type=blob.content_type or service_desk_kpi_export.XLSX_MEDIA_TYPE,
        content=blob.download_as_bytes(),
    )


def write_daily_snapshot(
    *,
    report_date: str | None = None,
    bucket_name: str | None = None,
    prefix: str | None = None,
    overwrite: bool = False,
    allow_stale_test_output: bool = False,
) -> SnapshotResult:
    report_day = normalize_report_date(report_date)
    bucket = resolve_bucket_name(bucket_name)
    blob_name = build_blob_name(report_day, prefix=prefix)
    gcs_uri = build_gcs_uri(bucket, blob_name)
    client = _get_storage_client()
    blob = client.bucket(bucket).blob(blob_name)

    exists = blob.exists(client)
    existing_is_fresh = exists and _existing_snapshot_is_fresh(blob, client=client, report_date=report_day)
    if exists and existing_is_fresh and not overwrite:
        return SnapshotResult(
            status="skip",
            report_date=report_day,
            bucket=bucket,
            blob_name=blob_name,
            gcs_uri=gcs_uri,
            filename=f"service-desk-kpi-{report_day}.xlsx",
            media_type=service_desk_kpi_export.XLSX_MEDIA_TYPE,
            size_bytes=0,
            metadata=blob.metadata or {},
        )

    export = service_desk_kpi_export.get_daily_excel_export(report_day)
    metadata = _snapshot_metadata(report_day, export)
    if not _export_uses_fresh_telephony(export):
        if allow_stale_test_output and resolve_prefix(prefix) != resolve_prefix(None):
            metadata["stale_test_output"] = "true"
        else:
            raise SnapshotStaleTelephonyError(
                f"Refusing to write {gcs_uri}: CXone telephony cache is not fresh for {report_day}."
            )
    blob.cache_control = "no-store"
    blob.content_disposition = f'attachment; filename="{export.filename}"'
    blob.metadata = metadata
    blob.upload_from_string(export.content, content_type=export.media_type)

    return SnapshotResult(
        status="refreshed" if exists and not existing_is_fresh and not overwrite else "wrote",
        report_date=report_day,
        bucket=bucket,
        blob_name=blob_name,
        gcs_uri=gcs_uri,
        filename=export.filename,
        media_type=export.media_type,
        size_bytes=len(export.content),
        metadata=metadata,
        telephony_status=export.metadata.get("telephony_status") or {},
    )
