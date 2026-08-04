"""Run or preflight the approval-gated current Gemini 2.5 control wave."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# This script is launched from ``scripts/`` while the test-lab package lives at
# the repository root. The backend runner already provides ``app`` on PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_transition.current_baseline import (
    BaselineApproval,
    build_preflight,
    run_approved_baseline,
    vertex_baseline_transport,
)
from model_transition.staged_waves import WAVE_PLAN_PATH, load_wave_plan, next_executable_wave, validate_wave_plan


def _approval_from_file(path: Path) -> BaselineApproval:
    data = json.loads(path.read_text(encoding="utf-8"))
    return BaselineApproval(
        case_ids=tuple(data["case_ids"]),
        corpus_sha256=data["corpus_sha256"],
        model_id=data["model_id"],
        location=data["location"],
        profile_id=data["profile_id"],
        hard_cap_usd=float(data["hard_cap_usd"]),
        hard_cap_cad=float(data["hard_cap_cad"]),
        max_calls=int(data["max_calls"]),
        one_attempt_per_case=bool(data["one_attempt_per_case"]),
        timeout_seconds=int(data["timeout_seconds"]),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically save private progress before a later provider call can begin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_campaign_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"waves": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("waves"), dict):
        raise ValueError("Campaign state must contain a waves object.")
    return data


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_packet(result: dict[str, Any], wave: dict[str, Any]) -> dict[str, Any]:
    corpus = json.loads((ROOT / "model_transition" / "stress_corpus.json").read_text(encoding="utf-8"))
    expected = {case["id"]: case["evaluator_only"] for case in corpus["cases"]}
    return {
        "wave_id": wave["id"],
        "purpose": wave["purpose"],
        "decision": "pending_human_or_agent_review",
        "cases": [
            {
                "case_id": row["case_id"],
                "answer": row["answer"],
                "usage": row["usage"],
                "latency_ms": row["latency_ms"],
                "actual_cost_usd": row["actual_cost_usd"],
                "evaluator_only": expected[row["case_id"]],
            }
            for row in result["results"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true", help="Offline only; never calls Vertex.")
    parser.add_argument("--execute", action="store_true", help="Run only the next owner-approved staged wave.")
    parser.add_argument("--record-qa", choices=("pass", "fail"), help="Record the review decision for a completed wave.")
    parser.add_argument("--approval", type=Path, help="Private owner-approved JSON envelope.")
    parser.add_argument("--output", type=Path, help="Ignored private receipt path for --execute.")
    parser.add_argument("--campaign-state", type=Path, help="Ignored private state file that gates later waves.")
    parser.add_argument("--wave-plan", type=Path, default=WAVE_PLAN_PATH, help="Tracked staged-wave plan.")
    parser.add_argument("--wave-id", help="Completed wave to mark after review.")
    parser.add_argument("--review-note", help="Short evidence-backed QA decision note.")
    args = parser.parse_args()
    selected_actions = sum(bool(value) for value in (args.preflight, args.execute, args.record_qa))
    if selected_actions != 1:
        parser.error("Choose exactly one of --preflight, --execute, or --record-qa.")
    if args.preflight:
        plan = load_wave_plan(args.wave_plan)
        print(json.dumps({"all_cases": build_preflight(), "staged_plan": validate_wave_plan(plan)}, indent=2, sort_keys=True))
        return
    if args.campaign_state is None:
        parser.error("--execute and --record-qa require --campaign-state.")
    plan = load_wave_plan(args.wave_plan)
    validate_wave_plan(plan)
    campaign_state = _load_campaign_state(args.campaign_state)
    if args.record_qa:
        if not args.wave_id or not args.review_note:
            parser.error("--record-qa requires --wave-id and --review-note.")
        record = campaign_state["waves"].get(args.wave_id)
        if not record or record.get("status") != "completed":
            parser.error("Only a completed wave with its private review packet can receive a QA decision.")
        record["status"] = "qa_passed" if args.record_qa == "pass" else "qa_failed"
        record["qa_note"] = args.review_note
        _write_json(args.campaign_state, campaign_state)
        print(json.dumps({"wave_id": args.wave_id, "status": record["status"]}, indent=2))
        return
    if args.approval is None or args.output is None:
        parser.error("--execute requires both --approval and --output.")
    wave = next_executable_wave(plan, campaign_state["waves"])
    if wave is None:
        parser.error("Every staged wave already has a recorded QA pass.")
    approval = _approval_from_file(args.approval)
    if tuple(wave["case_ids"]) != approval.case_ids:
        parser.error(f"Approval cases do not match the next required wave {wave['id']}.")
    preflight = build_preflight(case_ids=approval.case_ids)
    journal_path = args.output.with_suffix(args.output.suffix + ".journal.json")
    journal: dict[str, Any] = {
        "status": "starting",
        "corpus_sha256": preflight["corpus_sha256"],
        "profile_id": preflight["profile"]["id"],
        "approved_case_count": approval.max_calls,
        "attempted_case_ids": [],
        "completed_case_ids": [],
    }
    _write_json(journal_path, journal)

    def record_attempt(case_id: str) -> None:
        journal["status"] = "running"
        journal["attempted_case_ids"].append(case_id)
        _write_json(journal_path, journal)

    def record_result(result_row: dict[str, Any]) -> None:
        journal["completed_case_ids"].append(result_row["case_id"])
        _write_json(journal_path, journal)

    try:
        result = run_approved_baseline(
            approval,
            vertex_baseline_transport(),
            on_attempt=record_attempt,
            on_result=record_result,
        )
    except Exception as exc:
        journal["status"] = "stopped"
        journal["error_type"] = type(exc).__name__
        journal["error"] = str(exc)
        _write_json(journal_path, journal)
        raise
    _write_json(args.output, result)
    review_path = args.output.with_suffix(args.output.suffix + ".review.json")
    _write_json(review_path, _review_packet(result, wave))
    campaign_state["waves"][wave["id"]] = {
        "status": "completed",
        "receipt_sha256": _sha256(args.output),
        "review_path": str(review_path),
    }
    _write_json(args.campaign_state, campaign_state)
    journal["status"] = "complete"
    _write_json(journal_path, journal)
    print(json.dumps({key: result[key] for key in ("case_count", "actual_cost_usd", "actual_cost_cad")}, indent=2))


if __name__ == "__main__":
    main()
