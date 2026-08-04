#!/usr/bin/env python3
"""Targeted QA for Universal Chat pre-routing semantic interpretation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services import semantic_interpreter  # noqa: E402


OPEN_TICKET_950032 = {
    "ticket_id": 950032,
    "subject": "Network connectivity issues - degraded speed",
    "org_name": "City Fries 2009",
    "brand": "City Fries",
    "store_id": "2009",
    "issue_category": "Network > Wifi > Unable To Connect",
    "status": "open",
    "created_at": "2026-04-19",
}


CASES = [
    {
        "id": "SEM-001",
        "messages": [{"role": "user", "content": "Посмотри контекст этого тикета."}],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "target_context": "open_ticket",
        "answer_language": "Russian",
        "intent_any": {"current_ticket_context", "ticket_details"},
        "normalized_any": {"current", "open", "ticket"},
    },
    {
        "id": "SEM-002",
        "messages": [{"role": "user", "content": "Правильно ли я понимаю, что в ресторане случился пожар некоторое время назад?"}],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "target_context": "open_ticket",
        "answer_language": "Russian",
        "intent_any": {"event_fact_check"},
        "normalized_any": {"fire", "restaurant"},
    },
    {
        "id": "SEM-003",
        "messages": [
            {"role": "user", "content": "Посмотри контекст этого тикета."},
            {"role": "assistant", "content": "Этот тикет касается деградации скорости интернета в City Fries 2009."},
            {"role": "user", "content": "А что проверять дальше?"},
        ],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "target_context": "open_ticket",
        "answer_language": "Russian",
        "intent_any": {"next_steps", "current_ticket_context"},
        "normalized_any": {"next", "check"},
    },
    {
        "id": "SEM-004",
        "messages": [{"role": "user", "content": "Can you give me the details and top issues from store 2011 from the past 3 months?"}],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "target_context": "store",
        "answer_language": "English",
        "intent_any": {"store_analytics"},
        "normalized_any": {"store 2011", "past 3 months"},
    },
    {
        "id": "SEM-005",
        "messages": [{"role": "user", "content": "who do I escalate DispatchFlow issues to?"}],
        "open_ticket_id": None,
        "open_ticket_metadata": {},
        "target_context_any": {"global", "unclear"},
        "answer_language": "English",
        "intent_any": {"procedure_lookup"},
        "normalized_any": {"escalate", "DispatchFlow"},
    },
    {
        "id": "SEM-006",
        "messages": [{"role": "user", "content": "How n)may tickets were opened withon last 5 days?"}],
        "open_ticket_id": None,
        "open_ticket_metadata": {},
        "target_context_any": {"global", "unclear"},
        "answer_language": "English",
        "intent_any": {"temporal_analytics"},
        "normalized_any": {"how many", "last 5 days"},
    },
    {
        "id": "SEM-007",
        "messages": [
            {"role": "user", "content": "How many times has this category of issue been reported?"},
            {"role": "assistant", "content": "The issue category Server --> Hard Drive has been reported 476 times in total."},
            {"role": "user", "content": "What about in the last 90 days?"},
            {"role": "assistant", "content": "In the last 90 days, Server --> Hard Drive has been reported 216 times."},
            {"role": "user", "content": "Which stores had it most?"},
        ],
        "open_ticket_id": 950034,
        "open_ticket_metadata": {
            "ticket_id": 950034,
            "subject": "Server hard drive failed",
            "org_name": "Intel Rapid Storage Technology",
            "issue_category": "Server --> Hard Drive",
            "status": "open",
        },
        "target_context_any": {"prior_turn", "global"},
        "answer_language": "English",
        "intent_any": {"store_analytics"},
        "normalized_any": {"Server Hard Drive", "last 90 days"},
    },
    {
        "id": "SEM-008",
        "messages": [
            {
                "role": "user",
                "content": "Сумарищируй очень кратко на английсокм какого рода жалобы были от рестарана в течение с 2026 года связанные с сетью или подклбчением",
            },
            {
                "role": "assistant",
                "content": (
                    "Since the beginning of 2026, City Fries 2009 has consistently reported issues including "
                    "degraded internet speed, lagging Wi-Fi, dropping and inconsistent connections, and "
                    "complete primary internet outages or WAN 2 failures."
                ),
            },
            {"role": "user", "content": "Сколько тикетов по каждой из перечисленных категорий за 2026 год"},
        ],
        "open_ticket_id": 950032,
        "open_ticket_metadata": OPEN_TICKET_950032,
        "target_context_any": {"prior_turn", "open_ticket", "store"},
        "answer_language": "Russian",
        "intent_any": {"temporal_analytics", "store_analytics"},
        "normalized_any": {"listed categories", "2026"},
        "task": {
            "task_type": "count_by_dimension",
            "dimension_type": "semantic_labels",
            "label_any": {"degraded internet speed", "lagging Wi-Fi", "dropping connections"},
        },
    },
]


def _contains_any(text: str, needles: set[str]) -> bool:
    lower = text.lower()
    return any(needle.lower() in lower for needle in needles)


def _run_case(case: dict) -> tuple[bool, str]:
    result = semantic_interpreter.interpret_latest_turn(
        case["messages"],
        open_ticket_id=case.get("open_ticket_id"),
        open_ticket_metadata=case.get("open_ticket_metadata"),
    )
    frame = result.to_dict()
    failures: list[str] = []

    expected_target = case.get("target_context")
    expected_targets = case.get("target_context_any")
    if expected_target and result.target_context != expected_target:
        failures.append(f"target_context={result.target_context!r}, expected {expected_target!r}")
    if expected_targets and result.target_context not in expected_targets:
        failures.append(f"target_context={result.target_context!r}, expected one of {sorted(expected_targets)}")

    if result.answer_language != case["answer_language"]:
        failures.append(f"answer_language={result.answer_language!r}, expected {case['answer_language']!r}")
    if result.intent not in case["intent_any"]:
        failures.append(f"intent={result.intent!r}, expected one of {sorted(case['intent_any'])}")
    if not _contains_any(result.normalized_question_en, case["normalized_any"]):
        failures.append(
            f"normalized_question_en={result.normalized_question_en!r} missing any of {sorted(case['normalized_any'])}"
        )
    if result.status != "ok":
        failures.append(f"status={result.status!r}, fallback={result.fallback_reason!r}")
    expected_task = case.get("task")
    if expected_task:
        task = frame.get("task") or {}
        if task.get("task_type") != expected_task["task_type"]:
            failures.append(f"task_type={task.get('task_type')!r}, expected {expected_task['task_type']!r}")
        if task.get("dimension_type") != expected_task["dimension_type"]:
            failures.append(
                f"dimension_type={task.get('dimension_type')!r}, expected {expected_task['dimension_type']!r}"
            )
        labels = [str(label).lower() for label in (task.get("labels") or [])]
        if expected_task.get("label_any") and not any(
            expected.lower() in labels for expected in expected_task["label_any"]
        ):
            failures.append(f"task.labels={task.get('labels')!r} missing any of {sorted(expected_task['label_any'])}")

    summary = (
        f"{case['id']} intent={frame['intent']} target={frame['target_context']} "
        f"lang={frame['answer_language']} confidence={frame['confidence']} "
        f"normalized={frame['normalized_question_en']!r} task={(frame.get('task') or {}).get('task_type')!r}"
    )
    return not failures, summary + ("" if not failures else " FAIL: " + "; ".join(failures))


def main() -> int:
    passed = 0
    for case in CASES:
        ok, summary = _run_case(case)
        print(("PASS " if ok else "FAIL ") + summary)
        if ok:
            passed += 1
    print(f"RESULTS: {passed}/{len(CASES)} passed")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
