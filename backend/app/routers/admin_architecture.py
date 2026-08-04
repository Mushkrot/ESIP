"""Admin-only architecture documentation artifact proxy."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from google.api_core.exceptions import Forbidden, GoogleAPIError, NotFound
from google.cloud import storage

from app.config import settings
from app.dependencies import CurrentUser, require_admin


logger = logging.getLogger(__name__)
router = APIRouter()

_REFERENCE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<rect width="1280" height="720" fill="#f6f8f7"/><style>text{font-family:Inter,Arial,sans-serif;fill:#18322a}.t{font-size:24px;font-weight:700}.s{font-size:15px}.b{fill:#fff;stroke:#2d6a58;stroke-width:2}.a{stroke:#2d6a58;stroke-width:3;marker-end:url(#m)}</style>
<defs><marker id="m" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#2d6a58"/></marker></defs>
<text x="55" y="60" class="t">ESIP evidence and operations flow</text>
<rect class="b" x="55" y="135" rx="14" width="220" height="105"/><text x="83" y="180" class="t">Support intake</text><text x="83" y="210" class="s">Tickets and comments</text>
<rect class="b" x="365" y="135" rx="14" width="220" height="105"/><text x="397" y="180" class="t">Normalization</text><text x="397" y="210" class="s">Safe domain records</text>
<rect class="b" x="675" y="135" rx="14" width="220" height="105"/><text x="714" y="180" class="t">Intelligence</text><text x="714" y="210" class="s">Search and analysis</text>
<rect class="b" x="985" y="135" rx="14" width="220" height="105"/><text x="1022" y="180" class="t">Workspace</text><text x="1022" y="210" class="s">Evidence-backed action</text>
<path class="a" d="M275 188 H355"/><path class="a" d="M585 188 H665"/><path class="a" d="M895 188 H975"/>
<rect class="b" x="365" y="385" rx="14" width="220" height="105"/><text x="399" y="430" class="t">Governance</text><text x="399" y="460" class="s">Roles, cost, quality</text>
<rect class="b" x="675" y="385" rx="14" width="220" height="105"/><text x="711" y="430" class="t">Operations</text><text x="711" y="460" class="s">Incremental sync</text>
<path class="a" d="M475 375 V260"/><path class="a" d="M785 375 V260"/>
<text x="55" y="650" class="s">Reference runtime · deterministic fixtures · provider boundaries preserved</text></svg>'''.encode("utf-8")


def _reference_manifest() -> dict[str, Any]:
    return {
        "schema_version": 2, "format": "static-svg-gallery", "source_tool": "D2-compatible reference map",
        "d2_layout": "dagre", "version": "reference-2026.08", "source_commit": "reference",
        "generated_at": "2026-08-01T13:00:00+00:00", "title": "ESIP Architecture",
        "diagrams": [{"id": "platform-flow", "title": "Platform flow", "asset": "platform-flow.svg", "asset_url": "/api/admin/architecture/assets/platform-flow.svg", "kind": "system", "description": "Support intake, evidence processing, governance, and operations."}],
        "runbook": {"title": "Operator Runbook", "description": "Bounded operating procedures", "url": "/api/admin/architecture/runbook", "procedure_count": 2},
        "overview": {"title": "Platform Overview", "description": "Systems and operating flows", "url": "/api/admin/architecture/overview", "system_count": 6},
        "gallery": {"default_diagram": "platform-flow"},
    }


def _reference_overview() -> dict[str, Any]:
    systems = [
        {"id": "workspace", "name": "Signal Desk", "purpose": "Ticket context, evidence, and guided action.", "connects_to": ["api"], "portal": {"label": "Open workspace", "location": "ESIP web application", "url": "/chat"}, "map_id": "platform-flow"},
        {"id": "api", "name": "Domain API", "purpose": "Policy, orchestration, and stable application contracts.", "connects_to": ["tickets", "knowledge", "analytics"], "portal": {"label": "API health", "location": "Local reference endpoint", "url": "/health"}, "map_id": "platform-flow"},
        {"id": "tickets", "name": "Ticket store", "purpose": "Normalized ticket, comment, and store records.", "connects_to": ["api"], "portal": {"label": "Open sources", "location": "Admin > Sources", "url": "/admin/sources"}, "map_id": "platform-flow"},
        {"id": "knowledge", "name": "Knowledge index", "purpose": "Procedures and evidence available to retrieval workflows.", "connects_to": ["api"], "portal": {"label": "Open knowledge gaps", "location": "Tools > Knowledge Gaps", "url": "/knowledge-gap"}, "map_id": "platform-flow"},
        {"id": "analytics", "name": "Analytics engine", "purpose": "Bounded trend and cohort analysis over support records.", "connects_to": ["api"], "portal": {"label": "Open analytics", "location": "AI Chat analytics workflow", "url": "/analytics"}, "map_id": "platform-flow"},
        {"id": "governance", "name": "Governance controls", "purpose": "Roles, cost signals, quality telemetry, and audit surfaces.", "connects_to": ["api"], "portal": {"label": "Open administration", "location": "Admin Dashboard", "url": "/admin/panel"}, "map_id": "platform-flow"},
    ]
    return {
        "schema_version": 2, "title": "Enterprise Support Intelligence Platform",
        "description": "A layered support platform that connects ticket context, governed retrieval, operational analytics, and administration.",
        "access": {"title": "Reference access", "instruction": "The included reference workspace signs in automatically with a synthetic platform administrator.", "access_note": "Connected-provider credentials are optional and are never required for the reference runtime."},
        "processes": [
            {"id": "assist", "title": "Evidence-assisted support", "description": "Open a ticket, retrieve relevant procedures and resolved cases, then capture the next action.", "system_ids": ["workspace", "api", "tickets", "knowledge"]},
            {"id": "learn", "title": "Operational learning", "description": "Aggregate recurring issues and expose gaps that need better guidance.", "system_ids": ["tickets", "analytics", "governance"]},
        ],
        "groups": [
            {"id": "experience", "title": "Experience and application", "description": "Surfaces used by support engineers and administrators.", "systems": systems[:2]},
            {"id": "data", "title": "Evidence and insight", "description": "Normalized evidence, retrieval, analytics, and governance.", "systems": systems[2:]},
        ],
    }


def _reference_runbook() -> dict[str, Any]:
    return {
        "schema_version": 2, "title": "ESIP Operator Runbook", "description": "Task-first procedures for inspection and bounded recovery.",
        "access": {"title": "Reference operations", "instruction": "Use the in-product dashboards for inspection and simulated controls.", "access_note": "The reference runtime contains no production credentials."},
        "categories": [{"id": "health", "label": "Health and evidence", "description": "Inspect platform state and validate evidence paths."}],
        "procedures": [
            {"id": "verify-ticket-flow", "title": "Verify the ticket evidence flow", "summary": "Confirm that ticket, knowledge, and related-case evidence remain linked.", "category": "health", "risk": "safe", "systems": ["Signal Desk", "Domain API", "Knowledge index"], "estimated_time": "5 minutes", "prerequisites": ["Reference workspace is open"], "steps": [{"title": "Open a ticket", "detail": "Select an assigned ticket and review its store, comments, risk, summary, and knowledge cards.", "dashboard_path": {"label": "Signal Desk", "url": "/chat?ticket=950001", "navigation": "AI Chat > ticket #950001"}}, {"title": "Ask a scoped question", "detail": "Use the ticket conversation and confirm the answer includes evidence links."}], "verify": ["Ticket context and next action are visible", "Knowledge sources and related cases are linked"], "stop_conditions": ["Any request attempts to contact a connected provider in reference mode"], "rollback": ["Reset process-local reference state and reload the workspace"], "portals": [{"label": "Signal Desk", "location": "Local workspace", "url": "/chat?ticket=950001"}], "related_diagrams": ["platform-flow"]},
            {"id": "review-incremental-sync", "title": "Review incremental sync state", "summary": "Inspect source controls, schedules, and completed checkpoint history.", "category": "health", "risk": "safe", "systems": ["Governance controls", "Ticket store", "Knowledge index"], "estimated_time": "3 minutes", "prerequisites": ["Platform administrator role"], "steps": [{"title": "Open Data Sources", "detail": "Review enabled sources, schedule state, and recent successful runs.", "dashboard_path": {"label": "Data Sources", "url": "/admin/sources", "navigation": "Admin > Sources"}}], "verify": ["Every source has an explicit search and sync state", "Recent runs include a bounded summary"], "stop_conditions": ["A connected-provider write is requested from reference mode"], "rollback": ["Reload reference fixtures"], "portals": [{"label": "Sources", "location": "Admin workspace", "url": "/admin/sources"}], "related_diagrams": ["platform-flow"]},
        ],
    }


def _require_admin(user: dict) -> None:
    require_admin(user)


def _bucket_name() -> str:
    return (settings.architecture_docs_bucket or "").strip()


def _storage_client() -> storage.Client:
    return storage.Client(project=settings.google_cloud_project_id)


def _read_json_blob(bucket: storage.Bucket, blob_name: str) -> dict[str, Any]:
    try:
        raw = bucket.blob(blob_name).download_as_text(encoding="utf-8")
    except NotFound as exc:
        raise HTTPException(status_code=503, detail="Architecture documentation is not published yet") from exc
    except Forbidden as exc:
        logger.warning("architecture docs GCS forbidden for %s: %s", blob_name, exc)
        raise HTTPException(status_code=503, detail="Architecture documentation storage is not accessible") from exc
    except GoogleAPIError as exc:
        logger.warning("architecture docs GCS error for %s: %s", blob_name, exc)
        raise HTTPException(status_code=503, detail="Architecture documentation storage error") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("architecture docs invalid JSON in %s: %s", blob_name, exc)
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    return parsed


def _load_published_bundle() -> tuple[storage.Bucket, str, dict[str, Any]]:
    if not settings.architecture_docs_enabled:
        raise HTTPException(status_code=503, detail="Architecture documentation is disabled")
    bucket_name = _bucket_name()
    if not bucket_name:
        raise HTTPException(status_code=503, detail="Architecture documentation bucket is not configured")

    bucket = _storage_client().bucket(bucket_name)
    latest = _read_json_blob(bucket, "latest.json")
    version = str(latest.get("current_version") or "").strip()
    if not version or "/" in version or ".." in version:
        raise HTTPException(status_code=503, detail="Architecture documentation pointer is invalid")

    manifest = _read_json_blob(bucket, f"versions/{version}/manifest.json")
    diagrams = manifest.get("diagrams")
    if not isinstance(diagrams, list):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    return bucket, version, manifest


def _manifest_asset_names(manifest: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in manifest.get("diagrams") or []:
        if isinstance(item, dict):
            asset = item.get("asset")
            if isinstance(asset, str):
                names.add(asset)
    return names


def _manifest_runbook_asset(manifest: dict[str, Any]) -> str | None:
    runbook = manifest.get("runbook")
    if runbook is None:
        return None
    if not isinstance(runbook, dict):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    asset = runbook.get("asset")
    content_type = runbook.get("content_type")
    if (
        not isinstance(asset, str)
        or not asset
        or asset.startswith("/")
        or "://" in asset
        or "\\" in asset
        or "/" in asset
        or ".." in asset
        or not asset.endswith(".json")
        or content_type != "application/json"
    ):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    return asset


def _manifest_overview_asset(manifest: dict[str, Any]) -> str | None:
    overview = manifest.get("overview")
    if overview is None:
        return None
    if not isinstance(overview, dict):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    asset = overview.get("asset")
    content_type = overview.get("content_type")
    if (
        not isinstance(asset, str)
        or not asset
        or asset.startswith("/")
        or "://" in asset
        or "\\" in asset
        or "/" in asset
        or ".." in asset
        or not asset.endswith(".json")
        or content_type != "application/json"
    ):
        raise HTTPException(status_code=503, detail="Architecture documentation manifest is invalid")
    return asset


def _validate_asset_path(asset_path: str, allowed_assets: set[str]) -> str:
    asset = asset_path.strip()
    if (
        not asset
        or asset.startswith("/")
        or "://" in asset
        or "\\" in asset
        or "/" in asset
        or ".." in asset
        or not asset.endswith(".svg")
        or asset not in allowed_assets
    ):
        raise HTTPException(status_code=404, detail="Architecture asset not found")
    return asset


@router.get("/manifest")
def get_architecture_manifest(user: CurrentUser) -> dict[str, Any]:
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return _reference_manifest()
    _bucket, version, manifest = _load_published_bundle()

    diagrams_out: list[dict[str, Any]] = []
    for item in manifest.get("diagrams") or []:
        if not isinstance(item, dict):
            continue
        asset = item.get("asset")
        if not isinstance(asset, str) or not asset.endswith(".svg"):
            continue
        enriched = dict(item)
        enriched["asset_url"] = f"/api/admin/architecture/assets/{asset}"
        diagrams_out.append(enriched)

    runbook_meta = manifest.get("runbook")
    runbook_out: dict[str, Any] | None = None
    runbook_asset = _manifest_runbook_asset(manifest)
    if runbook_asset and isinstance(runbook_meta, dict):
        runbook_out = dict(runbook_meta)
        runbook_out["url"] = "/api/admin/architecture/runbook"

    overview_meta = manifest.get("overview")
    overview_out: dict[str, Any] | None = None
    overview_asset = _manifest_overview_asset(manifest)
    if overview_asset and isinstance(overview_meta, dict):
        overview_out = dict(overview_meta)
        overview_out["url"] = "/api/admin/architecture/overview"

    return {
        "schema_version": manifest.get("schema_version", 1),
        "format": manifest.get("format", "static-svg-gallery"),
        "source_tool": manifest.get("source_tool"),
        "d2_layout": manifest.get("d2_layout"),
        "version": manifest.get("version") or version,
        "source_commit": manifest.get("source_commit"),
        "source_commit_full": manifest.get("source_commit_full"),
        "generated_at": manifest.get("generated_at"),
        "title": manifest.get("title", "ESIP Architecture"),
        "diagrams": diagrams_out,
        "runbook": runbook_out,
        "overview": overview_out,
        "gallery": manifest.get("gallery"),
    }


def _download_asset_bytes(bucket: storage.Bucket, blob_name: str, *, missing_detail: str) -> bytes:
    try:
        return bucket.blob(blob_name).download_as_bytes()
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=missing_detail) from exc
    except Forbidden as exc:
        logger.warning("architecture docs asset forbidden for %s: %s", blob_name, exc)
        raise HTTPException(status_code=503, detail="Architecture documentation storage is not accessible") from exc
    except GoogleAPIError as exc:
        logger.warning("architecture docs asset GCS error for %s: %s", blob_name, exc)
        raise HTTPException(status_code=503, detail="Architecture documentation storage error") from exc


@router.get("/runbook")
def get_architecture_runbook(user: CurrentUser) -> Response:
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return Response(content=json.dumps(_reference_runbook()).encode("utf-8"), media_type="application/json")
    bucket, version, manifest = _load_published_bundle()
    asset = _manifest_runbook_asset(manifest)
    if asset is None:
        raise HTTPException(status_code=404, detail="Operator Runbook is not published yet")
    body = _download_asset_bytes(
        bucket,
        f"versions/{version}/{asset}",
        missing_detail="Operator Runbook is not published yet",
    )
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Operator Runbook is invalid") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("procedures"), list):
        raise HTTPException(status_code=503, detail="Operator Runbook is invalid")
    return Response(
        content=json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/overview")
def get_architecture_overview(user: CurrentUser) -> Response:
    _require_admin(user)
    if settings.runtime_profile == "reference":
        return Response(content=json.dumps(_reference_overview()).encode("utf-8"), media_type="application/json")
    bucket, version, manifest = _load_published_bundle()
    asset = _manifest_overview_asset(manifest)
    if asset is None:
        raise HTTPException(status_code=404, detail="Platform Overview is not published yet")
    body = _download_asset_bytes(
        bucket,
        f"versions/{version}/{asset}",
        missing_detail="Platform Overview is not published yet",
    )
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Platform Overview is invalid") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("groups"), list) or not isinstance(parsed.get("processes"), list):
        raise HTTPException(status_code=503, detail="Platform Overview is invalid")
    return Response(
        content=json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/assets/{asset_path:path}")
def get_architecture_asset(asset_path: str, user: CurrentUser) -> Response:
    _require_admin(user)
    if settings.runtime_profile == "reference":
        if asset_path != "platform-flow.svg":
            raise HTTPException(status_code=404, detail="Architecture asset not found")
        return Response(content=_REFERENCE_SVG, media_type="image/svg+xml")
    bucket, version, manifest = _load_published_bundle()
    asset = _validate_asset_path(asset_path, _manifest_asset_names(manifest))
    blob_name = f"versions/{version}/{asset}"
    body = _download_asset_bytes(bucket, blob_name, missing_detail="Architecture asset not found")

    return Response(
        content=body,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )
