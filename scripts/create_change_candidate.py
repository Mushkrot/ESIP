#!/usr/bin/env python3
"""Create a private Super Admin change candidate for the ESIP What’s New queue.

Draft candidates are not shown to engineers until Super Admin publishes them.

Usage:
  scripts/backend_python.sh scripts/create_change_candidate.py \
    --title "Universal Chat input moved" \
    --summary "When a chat is empty, the question field now appears under the guidance text." \
    --type ui \
    --impact medium \
    --file frontend/src/components/UniversalChat.tsx
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.services import announcements  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a ESIP What’s New draft change candidate.")
    parser.add_argument("--title", required=True, help="Short Super Admin/user-facing title.")
    parser.add_argument("--summary", required=True, help="What changed and why engineers may notice it.")
    parser.add_argument(
        "--type",
        default="other",
        choices=sorted(announcements.VALID_CHANGE_TYPES),
        help="Kind of platform change.",
    )
    parser.add_argument(
        "--impact",
        default="medium",
        choices=sorted(announcements.VALID_IMPACTS),
        help="Likely user impact.",
    )
    parser.add_argument("--source", default="agent", help="How this candidate was created.")
    parser.add_argument("--created-by", default="operator", help="Author label for the audit record.")
    parser.add_argument("--file", action="append", default=[], help="Related changed file. May be repeated.")
    parser.add_argument(
        "--audience",
        action="append",
        choices=["user", "admin"],
        default=[],
        help="Default audience for publish review. Repeat for both. Defaults to user+admin.",
    )
    parser.add_argument(
        "--severity",
        default="info",
        choices=sorted(announcements.VALID_SEVERITIES),
        help="Default announcement severity for publish review.",
    )
    args = parser.parse_args()

    result = announcements.create_change_candidate(
        title=args.title,
        summary=args.summary,
        change_type=args.type,
        impact=args.impact,
        source=args.source,
        related_files=args.file,
        audience_roles=args.audience or ["user", "admin"],
        severity=args.severity,
        created_by=args.created_by,
    )
    print(f"created change candidate: {result['id']}")
    print(f"title: {result['title']}")
    print(f"status: {result['status']}")
    print(f"audience: {', '.join(result['audience_roles'])}")


if __name__ == "__main__":
    main()
