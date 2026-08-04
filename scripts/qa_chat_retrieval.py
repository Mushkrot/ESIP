#!/usr/bin/env python3
"""Retrieval-level regression checks for Universal Chat.

These checks avoid Gemini calls. They validate the deterministic retrieval
contracts that protect Universal Chat from answering with polluted table
context before the final model response is generated.

Run from repo root:
  scripts/backend_python.sh scripts/qa_chat_retrieval.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from app.services.bigquery import search_drive_exact  # noqa: E402


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    query: str
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...] = ()


CASES = (
    RetrievalCase(
        name="CHAT-004 generic DispatchFlow escalation owner",
        query="who do I escalate DispatchFlow issues to?",
        must_contain=("Row: 14", "Section Owner / Team: BA"),
        must_not_contain=("Row: 19", "Failed Orders", "Legacy Queue"),
    ),
    RetrievalCase(
        name="CHAT-004 spelling variant with DispatchFlow",
        query="who do I escalate DispatchFlow issues to?",
        must_contain=("Row: 14", "Section Owner / Team: BA"),
        must_not_contain=("Row: 19", "Failed Orders", "Legacy Queue"),
    ),
    RetrievalCase(
        name="DispatchFlow task-specific Failed Orders",
        query="DispatchFlow Failed Orders escalation",
        must_contain=("Row: 14", "Row: 19", "Failed Orders"),
        must_not_contain=("Row: 20", "Not Integrating"),
    ),
    RetrievalCase(
        name="DispatchFlow task-specific Not Integrating",
        query="DispatchFlow Not Integrating escalation",
        must_contain=("Row: 14", "Row: 20", "Not Integrating"),
        must_not_contain=("Row: 19", "Failed Orders", "Row: 22", "Row: 35"),
    ),
    RetrievalCase(
        name="TeamViewer exact store label",
        query="TeamViewer access code for City Fries 2009 Stonehaven",
        must_contain=("City Fries 2009", "cityfries2009"),
        must_not_contain=("City Fries 2999", "Foundry Burger 2010"),
    ),
    RetrievalCase(
        name="Contract/customer exact store number",
        query="customer name for store 2009",
        must_contain=("Store No.: 2009", "City Fries 2009"),
        must_not_contain=("Store No.: 2010", "Foundry Burger 2010"),
    ),
    RetrievalCase(
        name="CHAT-054 Tempo Systems vendor contact stays entity-scoped",
        query="Tempo Systems vendor contact phone number",
        must_contain=("Tempo Systems",),
        must_not_contain=("Payroll export in Operations Portal", "Accounts Payable FAQ", "Phone number in deployment roster"),
    ),
    RetrievalCase(
        name="CHAT-055 indexed Drive URL resolves by file id",
        query="https://drive.google.com/file/d/REFERENCE_SUPPORT_MATRIX_FILE_000001/view",
        must_contain=("Foundry Burger 2010 Support Matrix",),
        must_not_contain=("Operations Portal Login", "Accounts Payable FAQ", "Team Schedule"),
    ),
)


def main() -> int:
    failures: list[str] = []
    for case in CASES:
        rows = search_drive_exact(case.query, page_size=5)
        text = "\n".join(str(row.get("chunk_text", "")) for row in rows)
        ok = True
        for needle in case.must_contain:
            if needle not in text:
                failures.append(f"{case.name}: missing {needle!r}")
                ok = False
        for needle in case.must_not_contain:
            if needle in text:
                failures.append(f"{case.name}: forbidden {needle!r}")
                ok = False
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case.name}")
        print(f"  Query: {case.query}")
        for idx, row in enumerate(rows, 1):
            print(
                "  "
                f"{idx}. {row.get('file_name')} "
                f"row={row.get('row_number')} type={row.get('row_type')}"
            )

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"\nResult: {len(CASES)}/{len(CASES)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
