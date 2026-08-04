#!/usr/bin/env python3
"""Audit and optionally remove unpublished Zendesk KB articles from ESIP indexes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402
from app.services.zendesk_article_cleanup import (  # noqa: E402
    article_publication_state,
    cleanup_unpublished_articles,
    is_zendesk_article_published,
)


def _zendesk_get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(1, 4):
        resp = requests.get(url, params=params, auth=settings.zendesk_auth, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(5 * attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Zendesk request failed after retries: {url}")


def fetch_sections_and_categories() -> dict[int, dict[str, str]]:
    sections: dict[int, dict[str, str]] = {}
    url = f"{settings.zendesk_base_url}/help_center/sections"
    while url:
        data = _zendesk_get(url, {"per_page": 100})
        for section in data.get("sections", []):
            sections[int(section["id"])] = {
                "section": section.get("name", ""),
                "category_id": str(section.get("category_id") or ""),
                "category": "",
            }
        url = data.get("next_page")
        if url:
            time.sleep(0.2)

    categories: dict[str, str] = {}
    url = f"{settings.zendesk_base_url}/help_center/categories"
    while url:
        data = _zendesk_get(url, {"per_page": 100})
        for category in data.get("categories", []):
            categories[str(category.get("id") or "")] = category.get("name", "")
        url = data.get("next_page")
        if url:
            time.sleep(0.2)

    for section in sections.values():
        section["category"] = categories.get(section["category_id"], "")
    return sections


def fetch_articles() -> list[dict[str, Any]]:
    sections = fetch_sections_and_categories()
    articles: list[dict[str, Any]] = []
    url = f"{settings.zendesk_base_url}/help_center/articles"
    while url:
        data = _zendesk_get(url, {"per_page": 100, "sort_by": "created_at"})
        for article in data.get("articles", []):
            section = sections.get(int(article.get("section_id") or 0), {})
            articles.append({
                "id": int(article["id"]),
                "title": article.get("title", ""),
                "html_url": article.get("html_url", ""),
                "section_id": article.get("section_id"),
                "section": section.get("section", ""),
                "category": section.get("category", ""),
                "locale": article.get("locale", ""),
                "draft": article.get("draft"),
                "state": article_publication_state(article),
                "created_at": article.get("created_at"),
                "updated_at": article.get("updated_at"),
            })
        url = data.get("next_page")
        if url:
            time.sleep(0.2)
    return articles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete unpublished indexed articles")
    parser.add_argument("--json-out", type=Path, help="Optional path for the full audit JSON")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()

    articles = fetch_articles()
    unpublished = [article for article in articles if not is_zendesk_article_published(article)]
    published = [article for article in articles if is_zendesk_article_published(article)]
    cleanup = cleanup_unpublished_articles(unpublished, dry_run=not args.apply)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "applied": args.apply,
        "zendesk": {
            "total_articles": len(articles),
            "published_articles": len(published),
            "unpublished_articles": len(unpublished),
        },
        "cleanup": cleanup,
        "unpublished_samples": unpublished[: args.sample_limit],
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "applied": args.apply,
        "zendesk": report["zendesk"],
        "cleanup": {
            key: cleanup[key]
            for key in (
                "dry_run",
                "indexed_unpublished_articles",
                "bq_rows",
                "gcs_blobs",
                "vertex_documents",
                "deleted_bq_rows",
                "deleted_gcs_blobs",
                "deleted_vertex_documents",
            )
        },
        "samples": cleanup.get("samples", [])[: args.sample_limit],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
