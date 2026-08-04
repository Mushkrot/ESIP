#!/usr/bin/env python3
"""Build the ESIP architecture documentation artifact bundle.

The bundle is intentionally static: rendered SVG diagrams, a manifest, a
latest pointer payload, and the existing D2 gallery HTML as an optional export
artifact. ESIP runtime reads these files later; it does not render D2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


REPO_ROOT = Path(__file__).resolve().parents[1]
ARCH_ROOT = REPO_ROOT / "Architectural documentation" / "docs" / "architecture"
D2_ROOT = ARCH_ROOT / "d2"
D2_OUT = D2_ROOT / "out"
RUNBOOK_SOURCE = ARCH_ROOT / "operator-runbook.json"
RUNBOOK_ASSET = "operator-runbook.json"
OVERVIEW_SOURCE = ARCH_ROOT / "platform-overview.json"
OVERVIEW_ASSET = "platform-overview.json"
SourceKind = Literal["d2", "generated_svg"]

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RUNBOOK_RISKS = {"safe", "caution", "approval_required", "emergency"}
_FORBIDDEN_ACTION_KEYS = {"action", "command", "endpoint", "http_method", "method", "mutation", "request"}
_SAFE_RUNBOOK_URL_PREFIXES = ("https://",)


@dataclass(frozen=True)
class DiagramSpec:
    id: str
    title: str
    source: str
    kind: str
    description: str
    source_kind: SourceKind = "d2"
    generated_asset: str | None = None

    @property
    def asset(self) -> str:
        return f"{self.id}.svg"


DIAGRAMS: tuple[DiagramSpec, ...] = (
    DiagramSpec(
        "presentation-overview",
        "Current GCP System Overview",
        "presentation/current-gcp-system/build_presentation_architecture_diagram.py",
        "presentation",
        "Executive one-page overview of the current ESIP platform architecture, shown first for presentation and orientation.",
        source_kind="generated_svg",
        generated_asset="presentation/current-gcp-system/esip_gcp_system_architecture_current.svg",
    ),
    DiagramSpec("infrastructure-overview", "Infrastructure Overview", "d2/infrastructure-overview.d2", "overview", "Big-picture map of the ESIP platform, managed cloud services, external access, integrations, and operational controls."),
    DiagramSpec("runtime-request-flow", "Runtime Request Flow", "d2/runtime-request-flow.d2", "flow", "How a browser or Zendesk request moves through frontend, backend, auth, semantic contracts, deterministic executors, search, AI, and logs."),
    DiagramSpec("access-dns-flow", "External Access & DNS Flow", "d2/access-dns-flow.d2", "access", "Public and office/VPN access paths for esip.example.com, with external DNS/CDN items marked for manual verification."),
    DiagramSpec("gcp-operations-map", "GCP Operations Map", "d2/gcp-operations-map.d2", "operations", "Where platform resources live in the GCP project and which console areas operators should use."),
    DiagramSpec("cloud-run-backend", "Cloud Run Backend", "d2/cloud-run-backend.d2", "operations", "Operational view of esip-backend, scaling, runtime identity, health checks, logs, and safe manual actions."),
    DiagramSpec("firebase-firestore-auth", "Firebase, Firestore & Auth", "d2/firebase-firestore-auth.d2", "security", "How Firebase Hosting/Auth and Firestore roles/state work together with backend authorization."),
    DiagramSpec("storage-search-datastores", "Storage, Search & Datastores", "d2/storage-search-datastores.d2", "data", "GCS, BigQuery, Vertex AI Search, embeddings, and how documentation/ticket data is stored and searched."),
    DiagramSpec("scheduler-nightly-jobs", "Nightly Jobs & Scheduler", "d2/scheduler-nightly-jobs.d2", "operations", "Cloud Scheduler jobs, schedules, endpoints, risk level, logs, and what each nightly process changes."),
    DiagramSpec("emergency-stop", "Emergency Stop & Cost Incident", "d2/emergency-stop.d2", "emergency", "Practical sequence for stopping runaway scans, AI spend, or repeated processing without making the situation worse."),
    DiagramSpec("zendesk-integration", "Zendesk Integration", "d2/zendesk-integration.d2", "integration", "Zendesk sidebar/modal app, ZAF auth, ESIP backend calls, and how the embedded app connects to the platform."),
    DiagramSpec("notifications-alerting", "Notifications & Alerting", "d2/notifications-alerting.d2", "operations", "Current Gmail API alerting, explicit legacy fallback, and future BetterStack/Slack escalation paths."),
    DiagramSpec("credentials-access", "Credentials & Access Ownership", "d2/credentials-access.d2", "security", "Where operators should find access, which portals require credentials, and what must never be stored in diagrams."),
    DiagramSpec("ai-logical-flow", "AI Logical Flow", "d2/ai-logical-flow.d2", "flow", "Logical view of semantic interpretation, bounded dispatch/policy, result memory, deterministic retrieval, validation, repair, and answer generation."),
)


def _require_nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value.strip()


def _validate_string_list(value: object, path: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "non-empty " if not allow_empty else ""
        raise ValueError(f"{path} must be a {qualifier}list")
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_require_nonempty_string(item, f"{path}[{index}]"))
    return result


def _validate_runbook_url(value: object, path: str) -> str:
    url = _require_nonempty_string(value, path)
    if not url.startswith(_SAFE_RUNBOOK_URL_PREFIXES):
        raise ValueError(f"{path} must be an https URL")
    return url


def _validate_browser_path(value: object, path: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    _require_nonempty_string(value.get("label"), f"{path}.label")
    _validate_runbook_url(value.get("url"), f"{path}.url")
    _require_nonempty_string(value.get("navigation"), f"{path}.navigation")


def _reject_action_fields(value: object, path: str = "runbook") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_ACTION_KEYS:
                raise ValueError(f"{path}.{key} is an unsafe mutation-action field")
            _reject_action_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_action_fields(child, f"{path}[{index}]")


def _validate_runbook(runbook: object, diagram_ids: set[str]) -> dict:
    if not isinstance(runbook, dict):
        raise ValueError("runbook must be an object")
    _reject_action_fields(runbook)
    if runbook.get("schema_version") != 1:
        raise ValueError("runbook.schema_version must be 1")
    _require_nonempty_string(runbook.get("title"), "runbook.title")
    _require_nonempty_string(runbook.get("description"), "runbook.description")
    access = runbook.get("access")
    if not isinstance(access, dict):
        raise ValueError("runbook.access must be an object")
    _require_nonempty_string(access.get("title"), "runbook.access.title")
    _require_nonempty_string(access.get("instruction"), "runbook.access.instruction")
    _require_nonempty_string(access.get("access_note"), "runbook.access.access_note")

    categories = runbook.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ValueError("runbook.categories must be a non-empty list")
    category_ids: set[str] = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise ValueError(f"runbook.categories[{index}] must be an object")
        category_id = _require_nonempty_string(category.get("id"), f"runbook.categories[{index}].id")
        if not _ID_RE.fullmatch(category_id):
            raise ValueError(f"Invalid category id: {category_id}")
        if category_id in category_ids:
            raise ValueError(f"Duplicate category id: {category_id}")
        category_ids.add(category_id)
        _require_nonempty_string(category.get("label"), f"runbook.categories[{index}].label")
        _require_nonempty_string(category.get("description"), f"runbook.categories[{index}].description")

    procedures = runbook.get("procedures")
    if not isinstance(procedures, list) or not procedures:
        raise ValueError("runbook.procedures must be a non-empty list")
    procedure_ids: set[str] = set()
    for index, procedure in enumerate(procedures):
        path = f"runbook.procedures[{index}]"
        if not isinstance(procedure, dict):
            raise ValueError(f"{path} must be an object")
        procedure_id = _require_nonempty_string(procedure.get("id"), f"{path}.id")
        if not _ID_RE.fullmatch(procedure_id):
            raise ValueError(f"Invalid procedure id: {procedure_id}")
        if procedure_id in procedure_ids:
            raise ValueError(f"Duplicate procedure id: {procedure_id}")
        procedure_ids.add(procedure_id)
        _require_nonempty_string(procedure.get("title"), f"{path}.title")
        _require_nonempty_string(procedure.get("summary"), f"{path}.summary")
        category_id = _require_nonempty_string(procedure.get("category"), f"{path}.category")
        if category_id not in category_ids:
            raise ValueError(f"Unknown category id in {path}: {category_id}")
        risk = _require_nonempty_string(procedure.get("risk"), f"{path}.risk")
        if risk not in _RUNBOOK_RISKS:
            raise ValueError(f"Unsupported risk level in {path}: {risk}")
        _validate_string_list(procedure.get("systems"), f"{path}.systems", allow_empty=False)
        _require_nonempty_string(procedure.get("estimated_time"), f"{path}.estimated_time")
        _validate_string_list(procedure.get("prerequisites"), f"{path}.prerequisites")
        steps = procedure.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{path}.steps must be a non-empty list")
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"{path}.steps[{step_index}] must be an object")
            _require_nonempty_string(step.get("title"), f"{path}.steps[{step_index}].title")
            _require_nonempty_string(step.get("detail"), f"{path}.steps[{step_index}].detail")
            for route_key in ("dashboard_path", "manual_path"):
                route = step.get(route_key)
                if route is not None:
                    _validate_browser_path(route, f"{path}.steps[{step_index}].{route_key}")
        _validate_string_list(procedure.get("verify"), f"{path}.verify", allow_empty=False)
        _validate_string_list(procedure.get("stop_conditions"), f"{path}.stop_conditions")
        _validate_string_list(procedure.get("rollback"), f"{path}.rollback")
        portals = procedure.get("portals")
        if not isinstance(portals, list):
            raise ValueError(f"{path}.portals must be a list")
        for portal_index, portal in enumerate(portals):
            if not isinstance(portal, dict):
                raise ValueError(f"{path}.portals[{portal_index}] must be an object")
            _require_nonempty_string(portal.get("label"), f"{path}.portals[{portal_index}].label")
            _require_nonempty_string(portal.get("location"), f"{path}.portals[{portal_index}].location")
            _validate_runbook_url(portal.get("url"), f"{path}.portals[{portal_index}].url")
        related = _validate_string_list(procedure.get("related_diagrams"), f"{path}.related_diagrams")
        unknown = sorted(set(related) - diagram_ids)
        if unknown:
            raise ValueError(f"Unknown related diagram ids in {path}: {', '.join(unknown)}")
    return runbook


def _load_runbook() -> dict:
    if not RUNBOOK_SOURCE.exists():
        raise FileNotFoundError(f"Missing structured runbook: {RUNBOOK_SOURCE}")
    try:
        payload = json.loads(RUNBOOK_SOURCE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid structured runbook JSON: {exc}") from exc
    return _validate_runbook(payload, {diagram.id for diagram in DIAGRAMS})


def _validate_platform_overview(overview: object, diagram_ids: set[str]) -> dict:
    if not isinstance(overview, dict):
        raise ValueError("overview must be an object")
    _reject_action_fields(overview, "overview")
    if overview.get("schema_version") != 1:
        raise ValueError("overview.schema_version must be 1")
    _require_nonempty_string(overview.get("title"), "overview.title")
    _require_nonempty_string(overview.get("description"), "overview.description")
    access = overview.get("access")
    if not isinstance(access, dict):
        raise ValueError("overview.access must be an object")
    _require_nonempty_string(access.get("title"), "overview.access.title")
    _require_nonempty_string(access.get("instruction"), "overview.access.instruction")
    _require_nonempty_string(access.get("access_note"), "overview.access.access_note")

    groups = overview.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("overview.groups must be a non-empty list")
    group_ids: set[str] = set()
    system_ids: set[str] = set()
    systems: list[tuple[dict, str]] = []
    for group_index, group in enumerate(groups):
        path = f"overview.groups[{group_index}]"
        if not isinstance(group, dict):
            raise ValueError(f"{path} must be an object")
        group_id = _require_nonempty_string(group.get("id"), f"{path}.id")
        if not _ID_RE.fullmatch(group_id):
            raise ValueError(f"Invalid group id: {group_id}")
        if group_id in group_ids:
            raise ValueError(f"Duplicate group id: {group_id}")
        group_ids.add(group_id)
        _require_nonempty_string(group.get("title"), f"{path}.title")
        _require_nonempty_string(group.get("description"), f"{path}.description")
        group_systems = group.get("systems")
        if not isinstance(group_systems, list) or not group_systems:
            raise ValueError(f"{path}.systems must be a non-empty list")
        for system_index, system in enumerate(group_systems):
            system_path = f"{path}.systems[{system_index}]"
            if not isinstance(system, dict):
                raise ValueError(f"{system_path} must be an object")
            system_id = _require_nonempty_string(system.get("id"), f"{system_path}.id")
            if not _ID_RE.fullmatch(system_id):
                raise ValueError(f"Invalid system id: {system_id}")
            if system_id in system_ids:
                raise ValueError(f"Duplicate system id: {system_id}")
            system_ids.add(system_id)
            systems.append((system, system_path))
            _require_nonempty_string(system.get("name"), f"{system_path}.name")
            _require_nonempty_string(system.get("purpose"), f"{system_path}.purpose")
            portal = system.get("portal")
            if not isinstance(portal, dict):
                raise ValueError(f"{system_path}.portal must be an object")
            _require_nonempty_string(portal.get("label"), f"{system_path}.portal.label")
            _require_nonempty_string(portal.get("location"), f"{system_path}.portal.location")
            _validate_runbook_url(portal.get("url"), f"{system_path}.portal.url")
            map_id = system.get("map_id")
            if map_id is not None and _require_nonempty_string(map_id, f"{system_path}.map_id") not in diagram_ids:
                raise ValueError(f"Unknown related diagram id in {system_path}: {map_id}")

    for system, path in systems:
        connections = _validate_string_list(system.get("connects_to"), f"{path}.connects_to", allow_empty=False)
        unknown = sorted(set(connections) - system_ids)
        if unknown:
            raise ValueError(f"Unknown connected system ids in {path}: {', '.join(unknown)}")

    processes = overview.get("processes")
    if not isinstance(processes, list) or not processes:
        raise ValueError("overview.processes must be a non-empty list")
    process_ids: set[str] = set()
    for index, process in enumerate(processes):
        path = f"overview.processes[{index}]"
        if not isinstance(process, dict):
            raise ValueError(f"{path} must be an object")
        process_id = _require_nonempty_string(process.get("id"), f"{path}.id")
        if not _ID_RE.fullmatch(process_id):
            raise ValueError(f"Invalid process id: {process_id}")
        if process_id in process_ids:
            raise ValueError(f"Duplicate process id: {process_id}")
        process_ids.add(process_id)
        _require_nonempty_string(process.get("title"), f"{path}.title")
        _require_nonempty_string(process.get("description"), f"{path}.description")
        related_systems = _validate_string_list(process.get("system_ids"), f"{path}.system_ids", allow_empty=False)
        unknown = sorted(set(related_systems) - system_ids)
        if unknown:
            raise ValueError(f"Unknown process system ids in {path}: {', '.join(unknown)}")
    return overview


def _load_platform_overview() -> dict:
    if not OVERVIEW_SOURCE.exists():
        raise FileNotFoundError(f"Missing structured platform overview: {OVERVIEW_SOURCE}")
    try:
        payload = json.loads(OVERVIEW_SOURCE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid structured platform overview JSON: {exc}") from exc
    return _validate_platform_overview(payload, {diagram.id for diagram in DIAGRAMS})


def _build_gallery_html(manifest: dict) -> str:
    diagrams = manifest["diagrams"]
    buttons = "\n".join(
        f'<button type="button" data-asset="{item["asset"]}">{item["title"]}</button>'
        for item in diagrams
    )
    first_asset = diagrams[0]["asset"] if diagrams else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{manifest["title"]}</title>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fb; color: #111827; }}
    header {{ display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid #d7dce5; background: white; }}
    header h1 {{ margin: 0; font-size: 16px; }}
    header span {{ color: #6b7280; font-size: 12px; }}
    main {{ display: grid; grid-template-columns: 280px 1fr; height: calc(100vh - 54px); }}
    nav {{ overflow: auto; border-right: 1px solid #d7dce5; background: #fff; padding: 12px; }}
    button {{ display: block; width: 100%; text-align: left; border: 1px solid #d7dce5; background: #fff; border-radius: 6px; padding: 9px 10px; margin-bottom: 8px; cursor: pointer; }}
    button.active {{ border-color: #0d32b2; color: #0d32b2; background: #eef3ff; }}
    .stage {{ position: relative; overflow: hidden; cursor: grab; }}
    .stage:active {{ cursor: grabbing; }}
    img {{ position: absolute; left: 50%; top: 50%; max-width: none; max-height: none; transform-origin: center center; background: white; box-shadow: 0 1px 6px rgba(15, 23, 42, .16); }}
  </style>
</head>
<body>
  <header>
    <h1>{manifest["title"]}</h1>
    <span>{manifest["version"]} · {manifest["source_tool"]}</span>
  </header>
  <main>
    <nav>{buttons}</nav>
    <section class="stage" id="stage"><img id="diagram" alt="Architecture diagram" src="{first_asset}" /></section>
  </main>
  <script>
    const img = document.getElementById('diagram');
    const stage = document.getElementById('stage');
    const buttons = Array.from(document.querySelectorAll('button[data-asset]'));
    let scale = 0.72, x = 0, y = 0, drag = null;
    function apply() {{ img.style.transform = `translate(calc(-50% + ${{x}}px), calc(-50% + ${{y}}px)) scale(${{scale}})`; }}
    function select(asset) {{
      buttons.forEach(b => b.classList.toggle('active', b.dataset.asset === asset));
      img.src = asset; scale = 0.72; x = 0; y = 0; apply();
    }}
    buttons.forEach(b => b.addEventListener('click', () => select(b.dataset.asset)));
    stage.addEventListener('wheel', e => {{ e.preventDefault(); scale = Math.min(3, Math.max(.18, scale + (e.deltaY > 0 ? -.08 : .08))); apply(); }}, {{ passive: false }});
    stage.addEventListener('pointerdown', e => {{ stage.setPointerCapture(e.pointerId); drag = {{ id: e.pointerId, x: e.clientX, y: e.clientY, ox: x, oy: y }}; }});
    stage.addEventListener('pointermove', e => {{ if (!drag || drag.id !== e.pointerId) return; x = drag.ox + e.clientX - drag.x; y = drag.oy + e.clientY - drag.y; apply(); }});
    stage.addEventListener('pointerup', () => drag = null);
    select('{first_asset}');
  </script>
</body>
</html>
"""


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT) -> str:
    return subprocess.check_output(cmd, cwd=str(cwd), text=True).strip()


def _git_short_sha() -> str:
    env_value = os.getenv("ARCH_SOURCE_COMMIT")
    if env_value:
        return env_value
    try:
        return _run(["git", "rev-parse", "--short", "HEAD"])
    except Exception:
        return "unknown"


def _git_full_sha() -> str:
    env_value = os.getenv("ARCH_SOURCE_COMMIT_FULL")
    if env_value:
        return env_value
    try:
        return _run(["git", "rev-parse", "HEAD"])
    except Exception:
        return "unknown"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _render_d2(diagram: DiagramSpec, output_dir: Path) -> None:
    source = ARCH_ROOT / diagram.source
    target = output_dir / diagram.asset
    if not source.exists():
        raise FileNotFoundError(f"Missing D2 source: {source}")
    subprocess.run(
        ["d2", "--layout", "elk", str(source), str(target)],
        cwd=str(REPO_ROOT),
        check=True,
    )


def _render_generated_svg(diagram: DiagramSpec, output_dir: Path) -> None:
    generator = ARCH_ROOT / diagram.source
    generated_asset = ARCH_ROOT / (diagram.generated_asset or "")
    if not generator.exists():
        raise FileNotFoundError(f"Missing presentation generator: {generator}")
    if not diagram.generated_asset:
        raise RuntimeError(f"Generated SVG diagram {diagram.id} is missing generated_asset")

    subprocess.run([sys.executable, str(generator)], cwd=str(REPO_ROOT), check=True)
    if not generated_asset.exists():
        raise FileNotFoundError(f"Presentation generator did not produce: {generated_asset}")
    shutil.copy2(generated_asset, output_dir / diagram.asset)


def build_bundle(output_dir: Path, *, version: str | None = None, skip_render: bool = False) -> dict:
    short_sha = _git_short_sha()
    full_sha = _git_full_sha()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    version_id = version or f"{_utc_stamp()}_{short_sha}"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    runbook = _load_runbook()
    overview = _load_platform_overview()

    diagrams_payload: list[dict] = []
    for diagram in DIAGRAMS:
        if diagram.source_kind == "generated_svg":
            if skip_render:
                generated_asset = ARCH_ROOT / (diagram.generated_asset or "")
                if not generated_asset.exists():
                    raise FileNotFoundError(f"Missing pre-rendered presentation SVG: {generated_asset}")
                shutil.copy2(generated_asset, output_dir / diagram.asset)
            else:
                _render_generated_svg(diagram, output_dir)
        elif skip_render:
            source_svg = D2_OUT / diagram.asset
            if not source_svg.exists():
                raise FileNotFoundError(f"Missing pre-rendered SVG: {source_svg}")
            shutil.copy2(source_svg, output_dir / diagram.asset)
        else:
            _render_d2(diagram, output_dir)

        asset_path = output_dir / diagram.asset
        if asset_path.stat().st_size <= 0:
            raise RuntimeError(f"Rendered SVG is empty: {asset_path}")
        diagrams_payload.append({
            "id": diagram.id,
            "title": diagram.title,
            "asset": diagram.asset,
            "source": diagram.source,
            "kind": diagram.kind,
            "description": diagram.description,
            "content_type": "image/svg+xml",
        })
        if diagram.generated_asset:
            diagrams_payload[-1]["generated_asset"] = diagram.generated_asset

    shutil.copy2(RUNBOOK_SOURCE, output_dir / RUNBOOK_ASSET)
    shutil.copy2(OVERVIEW_SOURCE, output_dir / OVERVIEW_ASSET)

    manifest = {
        "schema_version": 3,
        "format": "static-architecture-hub",
        "source_tool": "presentation-svg + d2",
        "d2_layout": "elk",
        "version": version_id,
        "source_commit": short_sha,
        "source_commit_full": full_sha,
        "generated_at": generated_at,
        "title": "ESIP Architecture",
        "diagrams": diagrams_payload,
        "runbook": {
            "asset": RUNBOOK_ASSET,
            "content_type": "application/json",
            "schema_version": runbook["schema_version"],
            "title": runbook["title"],
            "description": runbook["description"],
            "category_count": len(runbook["categories"]),
            "procedure_count": len(runbook["procedures"]),
        },
        "overview": {
            "asset": OVERVIEW_ASSET,
            "content_type": "application/json",
            "schema_version": overview["schema_version"],
            "title": overview["title"],
            "description": overview["description"],
            "group_count": len(overview["groups"]),
            "system_count": sum(len(group["systems"]) for group in overview["groups"]),
            "process_count": len(overview["processes"]),
        },
        "gallery": {
            "asset": "index.html",
            "content_type": "text/html; charset=utf-8",
            "embedded_viewer_enabled": False,
        },
    }

    (output_dir / "index.html").write_text(_build_gallery_html(manifest), encoding="utf-8")

    latest = {
        "current_version": version_id,
        "source_commit": short_sha,
        "source_commit_full": full_sha,
        "published_at": generated_at,
        "manifest": "manifest.json",
    }

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output_dir / "latest.json").write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")

    return {
        "version": version_id,
        "output_dir": str(output_dir),
        "diagram_count": len(diagrams_payload),
        "runbook_procedure_count": len(runbook["procedures"]),
        "overview_system_count": sum(len(group["systems"]) for group in overview["groups"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ESIP architecture static artifact bundle")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / ".architecture-build", help="Output directory")
    parser.add_argument("--version", default=None, help="Override bundle version id")
    parser.add_argument(
        "--skip-render",
        action="store_true",
        help="Copy existing d2/out SVGs instead of invoking d2. Use only for local debugging.",
    )
    args = parser.parse_args()

    result = build_bundle(args.output.resolve(), version=args.version, skip_render=args.skip_render)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
