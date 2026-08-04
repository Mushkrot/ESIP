#!/usr/bin/env python3
"""Create and verify the immutable official Google guide vault for model tests."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


SOURCES = (
    {
        "id": "model-card-gemini-2-5-flash",
        "title": "Gemini 2.5 Flash model card",
        "url": "https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-2-5-Flash-Model-Card.pdf",
        "filename": "model-card-gemini-2-5-flash.pdf",
        "category": "model_card",
    },
    {
        "id": "model-card-gemini-3-5-flash-lite",
        "title": "Gemini 3.5 Flash-Lite model card",
        "url": "https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/",
        "filename": "model-card-gemini-3-5-flash-lite.html",
        "category": "model_card",
    },
    {
        "id": "model-card-gemini-3-5-flash",
        "title": "Gemini 3.5 Flash model card",
        "url": "https://deepmind.google/models/model-cards/gemini-3-5-flash/",
        "filename": "model-card-gemini-3-5-flash.html",
        "category": "model_card",
    },
    {
        "id": "model-card-gemini-3-6-flash",
        "title": "Gemini 3.6 Flash model card",
        "url": "https://deepmind.google/models/model-cards/gemini-3-6-flash/",
        "filename": "model-card-gemini-3-6-flash.html",
        "category": "model_card",
    },
    {
        "id": "gemini-3-developer-guide",
        "title": "Gemini 3 developer guide",
        "url": "https://ai.google.dev/gemini-api/docs/gemini-3",
        "filename": "gemini-3-developer-guide.html",
        "category": "family_guide",
    },
    {
        "id": "google-genai-sdk-overview",
        "title": "Google Gen AI SDK overview",
        "url": "https://cloud.google.com/vertex-ai/generative-ai/docs/sdks/overview",
        "filename": "google-genai-sdk-overview.html",
        "category": "sdk",
    },
    {
        "id": "vertex-sdk-migration",
        "title": "Vertex AI generative SDK migration",
        "url": "https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk",
        "filename": "vertex-sdk-migration.html",
        "category": "sdk",
    },
    {
        "id": "gemini-thinking",
        "title": "Gemini thinking guide",
        "url": "https://ai.google.dev/gemini-api/docs/thinking",
        "filename": "gemini-thinking.html",
        "category": "thinking",
    },
    {
        "id": "gemini-function-calling",
        "title": "Gemini function calling guide",
        "url": "https://ai.google.dev/gemini-api/docs/function-calling",
        "filename": "gemini-function-calling.html",
        "category": "tools",
    },
    {
        "id": "gemini-structured-output",
        "title": "Gemini structured output guide",
        "url": "https://ai.google.dev/gemini-api/docs/structured-output",
        "filename": "gemini-structured-output.html",
        "category": "tools",
    },
    {
        "id": "vertex-generative-ai-pricing",
        "title": "Vertex AI generative AI pricing",
        "url": "https://cloud.google.com/vertex-ai/generative-ai/pricing",
        "filename": "vertex-generative-ai-pricing.html",
        "category": "pricing",
    },
)

MANIFEST_NAME = "MANIFEST.json"
USER_AGENT = "ESIP-Model-Transition-Guide-Vault/1.0"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def validate_manifest(vault: Path) -> list[str]:
    manifest_path = vault / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"missing {MANIFEST_NAME}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != len(SOURCES):
        return ["manifest source count does not match the pinned source registry"]
    expected_ids = {source["id"] for source in SOURCES}
    found_ids = {entry.get("id") for entry in entries}
    if found_ids != expected_ids:
        errors.append("manifest source IDs do not match the pinned source registry")
    for entry in entries:
        filename = entry.get("filename")
        if not isinstance(filename, str):
            errors.append("manifest entry without a filename")
            continue
        snapshot = vault / filename
        if not snapshot.is_file():
            errors.append(f"missing snapshot: {filename}")
            continue
        actual = sha256_bytes(snapshot.read_bytes())
        if actual != entry.get("sha256"):
            errors.append(f"sha256 mismatch: {filename}")
        if not entry.get("source_url") or not entry.get("retrieved_at"):
            errors.append(f"missing provenance: {filename}")
    return errors


def fetch_source(source: dict[str, str], destination: Path) -> dict[str, object]:
    request = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - URLs come from the fixed HTTPS registry.
            payload = response.read()
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
            status = response.status
    except urllib.error.URLError as error:
        raise RuntimeError(f"{source['id']}: {error}") from error
    if status != 200 or not payload:
        raise RuntimeError(f"{source['id']}: HTTP {status} with empty or unsuccessful payload")
    (destination / source["filename"]).write_bytes(payload)
    return {
        **source,
        "source_url": source["url"],
        "final_url": final_url,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "content_type": content_type,
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def write_readme(vault: Path, manifest: dict[str, object]) -> None:
    lines = [
        "# Official Google Guide Vault",
        "",
        "This directory is an immutable source snapshot for ESIP model-transition planning.",
        "It is not production configuration, model-quality evidence, or an authorization to call a model.",
        "Use `MANIFEST.json` for source URL, final URL, retrieval time, content type, byte count, and SHA-256.",
        "",
        "## Included sources",
        "",
    ]
    for entry in manifest["entries"]:
        lines.append(f"- `{entry['filename']}` - {entry['title']} ({entry['category']})")
    lines.extend(
        [
            "",
            "Verify a checked-out vault without network access:",
            "",
            "```sh",
            "scripts/backend_python.sh scripts/model_transition_phase1_guide_vault.py --verify official_guides/google/2026-07-23",
            "```",
        ]
    )
    (vault / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_vault(target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"target already exists: {target}; snapshots are immutable")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        entries = [fetch_source(source, temporary) for source in SOURCES]
        manifest = {
            "schema_version": 1,
            "vault_purpose": "official Google documentation provenance for ESIP model transition",
            "entries": entries,
        }
        (temporary / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_readme(temporary, manifest)
        errors = validate_manifest(temporary)
        if errors:
            raise RuntimeError("; ".join(errors))
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="new immutable vault directory")
    parser.add_argument("--verify", type=Path, help="existing vault directory to verify")
    args = parser.parse_args()
    if bool(args.target) == bool(args.verify):
        parser.error("provide exactly one of --target or --verify")
    if args.verify:
        errors = validate_manifest(args.verify)
        if errors:
            parser.error("; ".join(errors))
        print(f"verified {len(SOURCES)} official snapshots in {args.verify}")
        return 0
    build_vault(args.target)
    print(f"created {len(SOURCES)} official snapshots in {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
