#!/usr/bin/env python3
"""Regression checks for unpublished Zendesk KB filtering."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services import bq_sync  # noqa: E402
from app.services.zendesk_article_cleanup import (  # noqa: E402
    extract_article_id_from_uri,
    is_zendesk_article_published,
)


def _check(ok: bool, name: str, detail: str) -> bool:
    print(("PASS" if ok else "FAIL"), name + ":", detail)
    return ok


def case_unpublished_001() -> bool:
    draft = {"id": 123, "title": "Draft", "draft": True}
    published = {"id": 456, "title": "Published", "draft": False}
    missing_flag = {"id": 789, "title": "Legacy payload"}
    ok = (
        is_zendesk_article_published(draft) is False
        and is_zendesk_article_published(published) is True
        and is_zendesk_article_published(missing_flag) is True
    )
    return _check(ok, "UNPUBLISHED-KB-001", "Zendesk draft=true is treated as unpublished")


def case_unpublished_002() -> bool:
    section = {"name": "Support Knowledge", "category_name": "Technology"}
    now = datetime.now(timezone.utc)
    draft_row = bq_sync._kb_article_row(
        {
            "id": 123,
            "title": "Draft article",
            "draft": True,
            "html_url": "https://help.example.com/hc/en-us/articles/91000000000001-Draft",
        },
        section,
        now,
    )
    published_row = bq_sync._kb_article_row(
        {
            "id": 456,
            "title": "Published article",
            "draft": False,
            "html_url": "https://help.example.com/hc/en-us/articles/91000000000002-Published",
        },
        section,
        now,
    )
    ok = draft_row is None and published_row is not None and published_row["id"] == 456
    return _check(ok, "UNPUBLISHED-KB-002", "BQ kb_articles row builder skips draft articles")


def case_unpublished_003() -> bool:
    ok = (
        extract_article_id_from_uri("support_articles_v2/990100001_device-error.html") == 990100001
        and extract_article_id_from_uri("gs://esip-reference-data/operations_articles_v2/930000000002_title.html") == 930000000002
        and extract_article_id_from_uri("https://support.example.com/hc/en-us/articles/91000000000003-Finding-store") == 91000000000003
        and extract_article_id_from_uri("gdrive/reference_support_docs/file.txt") is None
    )
    return _check(ok, "UNPUBLISHED-KB-003", "Cleanup can parse Zendesk article IDs from indexed document URIs")


def main() -> int:
    checks = [
        case_unpublished_001(),
        case_unpublished_002(),
        case_unpublished_003(),
    ]
    passed = sum(1 for item in checks if item)
    print(f"RESULTS: {passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
