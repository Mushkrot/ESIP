#!/usr/bin/env python3
"""Shadow-evaluate candidate model routing without affecting production answers.

Default mode is a dry run and cannot call Gemini. Live mode is intentionally
explicit: `--live --candidate-model MODEL --limit N`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from app.config import settings  # noqa: E402
from app.services import cost_tracker, model_routing  # noqa: E402


OUTPUT_DIR = ROOT / "export" / "model_routing_shadow"


@dataclass(frozen=True)
class ShadowCase:
    case_id: str
    surface: model_routing.ModelSurface
    label: str
    input_tokens: int
    output_tokens: int


SHADOW_CASES: tuple[ShadowCase, ...] = (
    ShadowCase(
        case_id="CHAT-077",
        surface=model_routing.ModelSurface.ANSWER_VALIDATOR,
        label="routine_supported_answer_validator",
        input_tokens=1600,
        output_tokens=500,
    ),
    ShadowCase(
        case_id="CHAT-080",
        surface=model_routing.ModelSurface.CHAT_QUALITY_REVIEW,
        label="scope_mismatch_review",
        input_tokens=2600,
        output_tokens=900,
    ),
    ShadowCase(
        case_id="CHAT-086",
        surface=model_routing.ModelSurface.REPAIR_ESCALATION,
        label="allowlisted_repair_escalation_candidate",
        input_tokens=3200,
        output_tokens=1200,
    ),
)


def _candidate_model(args: argparse.Namespace) -> str:
    if args.candidate_model:
        return args.candidate_model
    return "gemini-3.1-flash-lite"


def _plan_case(case: ShadowCase, candidate_model: str) -> dict[str, Any]:
    baseline_profile = model_routing.resolve_surface_model_profile(settings, case.surface)
    candidate_profile = model_routing.resolve_surface_model_profile(
        settings,
        case.surface,
        override_model_id=candidate_model,
    )
    baseline_cost = cost_tracker.estimate_model_cost_cad(
        model_id=baseline_profile.model_id,
        input_tokens=case.input_tokens,
        output_tokens=case.output_tokens,
    )
    candidate_cost = cost_tracker.estimate_model_cost_cad(
        model_id=candidate_profile.model_id,
        input_tokens=case.input_tokens,
        output_tokens=case.output_tokens,
    )
    return {
        "case_id": case.case_id,
        "label": case.label,
        "surface": case.surface.value,
        "baseline": model_routing.telemetry_payload(baseline_profile, candidate_mode="baseline"),
        "candidate": model_routing.telemetry_payload(candidate_profile, candidate_mode="shadow_candidate"),
        "baseline_expected_cost_cad": round(baseline_cost.expected_cost_cad, 8),
        "candidate_expected_cost_cad": round(candidate_cost.expected_cost_cad, 8),
        "candidate_reserved_cost_cad": round(candidate_cost.reserved_cost_cad, 8),
        "checks": {
            "schema_parse": "planned",
            "quality_marker": "planned",
            "latency_ms": "planned",
            "token_cost_telemetry": "planned",
            "fallback_reason": candidate_profile.fallback_reason,
        },
    }


def _write_live_stub(rows: list[dict[str, Any]], candidate_model: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"shadow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{candidate_model}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            live_row = dict(row)
            live_row["shadow_status"] = "candidate_not_called"
            live_row["shadow_failure"] = (
                "live execution hook is intentionally disabled until an explicit "
                "candidate-call implementation is reviewed"
            )
            handle.write(json.dumps(live_row, sort_keys=True) + "\n")
    return path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print planned shadow comparisons only. This is the default.")
    parser.add_argument("--live", action="store_true", help="Write guarded live-mode shadow records under export/model_routing_shadow/.")
    parser.add_argument("--candidate-model", default="", help="Candidate model id, e.g. gemini-3.1-flash-lite or gemini-3.5-flash.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of redacted cases to evaluate in live mode.")
    args = parser.parse_args(argv)

    if args.live:
        if not args.candidate_model:
            parser.error("--live requires --candidate-model")
        if args.limit <= 0:
            parser.error("--live requires --limit > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    candidate_model = _candidate_model(args)
    limit = args.limit if args.live else len(SHADOW_CASES)
    cases = list(SHADOW_CASES[:limit])
    rows = [_plan_case(case, candidate_model) for case in cases]

    print("MODEL_ROUTING_SHADOW")
    print(f"mode={'live' if args.live else 'dry-run'}")
    print(f"candidate_model={candidate_model}")
    print(f"output_dir={OUTPUT_DIR.relative_to(ROOT)}")
    print(f"cases={len(rows)}")
    for row in rows:
        print(
            "case={case_id} surface={surface} baseline={baseline} candidate={candidate} "
            "candidate_cost_cad={candidate_expected_cost_cad}".format(
                case_id=row["case_id"],
                surface=row["surface"],
                baseline=row["baseline"]["model_id"],
                candidate=row["candidate"]["model_id"],
                candidate_expected_cost_cad=row["candidate_expected_cost_cad"],
            )
        )

    if args.live:
        output_path = _write_live_stub(rows, candidate_model)
        print(f"wrote={output_path.relative_to(ROOT)}")
    else:
        print("dry_run_no_gemini_calls=true")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
