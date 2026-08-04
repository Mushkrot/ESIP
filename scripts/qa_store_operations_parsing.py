#!/usr/bin/env python3
"""
Repeatable QA checks for the Store Operations Job Aids Drive corpus.

Default mode uses BQ-backed Drive exact search only, so it does not call Gemini
and does not mutate data. Use --mode semantic to exercise Vertex AI semantic
search as well; that records the normal Vertex Search flat cost.

Run from repo root:
  scripts/backend_python.sh scripts/qa_store_operations_parsing.py --mode exact
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


SOURCE_ID = "gdrive_operations_job_aids"


@dataclass(frozen=True)
class QACase:
    label: str
    file_type: str
    query: str
    expected_titles: tuple[str, ...]
    max_rank: int = 3


QA_CASES = [
    QACase(
        label="Manager POS clock-in",
        file_type="Google Doc",
        query="manager can clock in",
        expected_titles=("Store Operations - PC Clock - How a Manager Can Clock in a Teammate",),
    ),
    QACase(
        label="Password resets",
        file_type="PDF / Google Doc",
        query="PASSWORD RESETS",
        expected_titles=("Store Operations - General - Password Resets", "Store Operations Full Guide - 2019.pdf"),
    ),
    QACase(
        label="Operations Portal formats",
        file_type="Google Slides",
        query="In the world of Operations Portal today we see it in two different formats",
        expected_titles=("OPERATIONS_SUITE Scheduling  + Chat Webinar",),
    ),
    QACase(
        label="French scheduling/chat deck",
        file_type="PPTX / Google Slides",
        query="Horaires et clavardage",
        expected_titles=("Webinaire sur les horaires + le clavardage OPERATIONS_SUITE",),
    ),
    QACase(
        label="ProcedureView refresh time",
        file_type="DOCX / Google Doc",
        query="portal is refreshed daily between 8 and 9:30 AM",
        expected_titles=("ProcedureView Job Aid 2021",),
    ),
    QACase(
        label="Coverage definition",
        file_type="XLSX / Google Sheet",
        query="Correct Coverage",
        expected_titles=("Store Operations 2020.2 Translations", "Store Operations - Labour - Building a Schedule"),
    ),
    QACase(
        label="Food forecasting sheet change",
        file_type="Google Sheet",
        query='Change column from "Hold Time" to "Shelf Life"',
        expected_titles=("Store Operations Forecasting",),
    ),
    QACase(
        label="Inventory adjustment warning",
        file_type="VTT captions / Google Doc",
        query="count adjustment and a price adjustment on the same entry",
        expected_titles=("Store Operations - Inventory - Inventory Adjustments", "[SubtitleTools.com] Store Operations - Inventory"),
    ),
    QACase(
        label="French chat screenshot",
        file_type="PNG OCR",
        query="Bonjour!",
        expected_titles=("IMG_7319.PNG",),
    ),
    QACase(
        label="Store Operations logo OCR",
        file_type="JPEG OCR",
        query="fork combined with a bar chart",
        expected_titles=("store_operations_v2", "store_operations logos"),
    ),
]


def _title_matches(title: str, expected_titles: tuple[str, ...]) -> bool:
    title_lower = title.lower()
    return any(expected.lower() in title_lower for expected in expected_titles)


def _run_exact(case: QACase, page_size: int) -> tuple[bool, list[dict]]:
    from app.services.bigquery import search_drive_exact

    rows = search_drive_exact(
        case.query,
        page_size=page_size,
        source_ids=[SOURCE_ID],
    )
    for idx, row in enumerate(rows, start=1):
        if idx > case.max_rank:
            break
        if _title_matches(str(row.get("file_name", "")), case.expected_titles):
            return True, rows
    return False, rows


def _run_semantic(case: QACase, page_size: int) -> tuple[bool, list[dict]]:
    from app.services.vertex_search import search_kb

    results = search_kb(case.query, page_size=page_size, sources=[SOURCE_ID])
    rows = [{"file_name": r.title, "chunk_text": r.snippet, "drive_url": r.url} for r in results]
    for idx, row in enumerate(rows, start=1):
        if idx > case.max_rank:
            break
        if _title_matches(str(row.get("file_name", "")), case.expected_titles):
            return True, rows
    return False, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="QA Drive parsing/retrieval for Store Operations Job Aids.")
    parser.add_argument(
        "--mode",
        choices=["exact", "semantic"],
        default="exact",
        help="exact uses BQ Drive exact search; semantic uses Vertex AI semantic search and records normal search cost.",
    )
    parser.add_argument("--page-size", type=int, default=8)
    parser.add_argument("--show-snippets", action="store_true")
    args = parser.parse_args()

    runner = _run_semantic if args.mode == "semantic" else _run_exact
    failures = 0
    print(f"Store Operations QA mode={args.mode} source={SOURCE_ID}")
    for case in QA_CASES:
        ok, rows = runner(case, args.page_size)
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"\n[{status}] {case.file_type}: {case.label}")
        print(f"Query: {case.query}")
        for idx, row in enumerate(rows[:3], start=1):
            title = str(row.get("file_name", ""))
            path = str(row.get("source_path", ""))
            print(f"  {idx}. {title}" + (f" | {path}" if path else ""))
            if args.show_snippets:
                snippet = str(row.get("chunk_text", "")).replace("\n", " ")[:240]
                print(f"     {snippet}")

    print(f"\nResult: {len(QA_CASES) - failures}/{len(QA_CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
