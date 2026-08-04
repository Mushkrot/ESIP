#!/usr/bin/env python3
"""
Re-run Gemini OCR on a small sample from the BQ cohort:
  ticket_attachment_ocr.processing_status = 'success'
  AND ocr_text does NOT contain '[IMAGE DESCRIPTION:'

Purpose: smoke-test whether a *fresh* ocr_image() call returns the required description line.

NOT for production BQ refresh — results are discarded (no INSERT/UPDATE). For saving new OCR
to BigQuery after deploy, use scripts/reprocess_failed_ocr.py (or ocr_attachments / sync path).

Safety (by design):
  - Does NOT update BigQuery
  - Does NOT modify scripts/.ocr_reprocess_done.txt
  - Does NOT modify scripts/.ocr_reprocess_failures.json
  - Does NOT touch abandon counters

Note: ocr_image() still runs the normal cost gate + CIS estimate/actual — you pay for N calls.
Prefer small N (e.g. <=10) unless the user explicitly wants a larger throwaway probe.

Usage:
  python3 scripts/probe_ocr_no_description.py [--limit 10]
  python3 scripts/probe_ocr_no_description.py [--limit 10] --reversed-order
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, "backend")

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import requests
from vertexai.generative_models import GenerationConfig, Part

from app.services.bigquery import _get_client, GCP_PROJECT, BQ_DATASET
from app.services.cost_tracker import (
    CallType,
    CostDivergenceError,
    CostGateError,
    OCR_MEASURED_EXPECTED_CAD,
    check_cost_gate,
    check_divergence,
    get_gate_reserve,
    new_correlation_id,
    record_estimate,
    record_usage,
    flush_bq_events,
)
import app.services.gemini as gemini_mod
from app.services.gemini import ocr_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe_ocr_no_description")

MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}
MARKER = "[IMAGE DESCRIPTION:"

_REVERSED_OCR_PROMPT = (
    "You are processing an image attached to an IT support ticket "
    "from a multi-site support operation. Images may show: "
    "error dialogs, application interfaces, network configuration screens, "
    "terminal/log output, Slack or email conversations, remote desktop sessions, "
    "POS system screens, physical equipment, cables, hardware, or damage.\n\n"
    "OUTPUT ORDER — follow strictly:\n\n"
    "PART 1 (must be the VERY FIRST line of your entire response, before any other text):\n"
    "[IMAGE DESCRIPTION: one sentence describing what the image shows — "
    "type of equipment, physical state, visible damage, screen content, "
    "cable types, hardware components, or any other relevant visual detail]\n\n"
    "Then a single blank line.\n\n"
    "PART 2 — Extract ALL visible text from the image after that blank line:\n"
    "- Preserve exact wording of error messages, codes, and technical values\n"
    "- Keep IP addresses, hostnames, port numbers, version numbers exactly as shown\n"
    "- Preserve line breaks and structure in logs, stack traces, and config output\n"
    "- Include all UI labels, button text, menu items, and window titles\n"
    "- Include all chat/email text — sender names, timestamps, message content\n"
    "- Do NOT use markdown — plain text only\n\n"
    "If you must truncate due to length, keep PART 1 complete and truncate from the end of PART 2."
)


def ocr_image_reversed_order(image_bytes: bytes, mime_type: str) -> tuple[str | None, str]:
    """
    Same CIS pattern as ocr_image(), but prompt asks for [IMAGE DESCRIPTION:] FIRST.
    Probe-only — not used in production sync path.
    """
    try:
        from google.api_core.exceptions import (
            DeadlineExceeded,
            InternalServerError as GcpInternalError,
            ServiceUnavailable,
        )
    except ImportError:
        DeadlineExceeded = ServiceUnavailable = GcpInternalError = Exception

    _ocr_expected = OCR_MEASURED_EXPECTED_CAD
    _ocr_reserved = get_gate_reserve(CallType.OCR_IMAGE, _ocr_expected)
    _ocr_corr_id = new_correlation_id()
    try:
        check_cost_gate(CallType.OCR_IMAGE, projected_additional_cost=_ocr_reserved, fail_closed=True)
    except CostGateError:
        raise
    record_estimate(
        CallType.OCR_IMAGE,
        _ocr_expected,
        correlation_id=_ocr_corr_id,
        est_basis="measured_avg",
        expected_cost_cad=_ocr_expected,
        reserved_cost_cad=_ocr_reserved,
    )

    image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
    gen_cfg = GenerationConfig.from_dict(
        {
            "temperature": 0.0,
            "max_output_tokens": 2048,
            "thinking_config": {"thinking_budget": 0},
        }
    )

    for attempt in range(1, 4):
        try:
            response = gemini_mod._model.generate_content(
                [image_part, _REVERSED_OCR_PROMPT],
                generation_config=gen_cfg,
            )
            usage = getattr(response, "usage_metadata", None)
            if usage:
                t_in = getattr(usage, "prompt_token_count", 0) or 0
                t_out = getattr(usage, "candidates_token_count", 0) or 0
                t_think = getattr(usage, "thoughts_token_count", 0) or 0
                actual_cost = record_usage(
                    CallType.OCR_IMAGE,
                    t_in,
                    t_out,
                    t_think,
                    correlation_id=_ocr_corr_id,
                    est_cost_usd=_ocr_expected,
                    expected_cost_cad=_ocr_expected,
                )
                check_divergence(
                    estimated_cost=_ocr_expected,
                    actual_cost=actual_cost,
                    call_type=CallType.OCR_IMAGE,
                    tokens_thinking=t_think,
                    thinking_budget_was_zero=True,
                )
            text = (response.text or "").strip()
            return (text if text else None), ""
        except (CostGateError, CostDivergenceError):
            raise
        except (DeadlineExceeded, ServiceUnavailable, GcpInternalError) as exc:
            if attempt < 3:
                import time as _time

                _time.sleep(5.0 * attempt)
                continue
            return None, f"gemini_error:{exc}"
        except Exception as exc:
            return None, f"gemini_error:{exc}"
    return None, "ocr_failed"


def fetch_sample(client, limit: int) -> list[dict]:
    sql = f"""
        SELECT
            o.ticket_id,
            o.content_url AS content_url,
            IFNULL(o.ocr_text, '') AS old_ocr_text,
            COALESCE(a.content_type, 'image/jpeg') AS content_type
        FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` o
        LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` a
            ON a.content_url = o.content_url
        WHERE o.processing_status = 'success'
          AND NOT CONTAINS_SUBSTR(IFNULL(o.ocr_text, ''), '{MARKER}')
        ORDER BY o.ticket_id
        LIMIT {int(limit)}
    """
    return [
        {
            "ticket_id": r.ticket_id,
            "content_url": r.content_url,
            "old_len": len(r.old_ocr_text or ""),
            "content_type": r.content_type or "image/jpeg",
        }
        for r in client.query(sql).result()
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="How many cohort rows to probe (default 10, max 50; prefer <=10 for smoke — see AGENTS.md §9)",
    )
    ap.add_argument(
        "--reversed-order",
        action="store_true",
        help="Prompt: require [IMAGE DESCRIPTION:] as FIRST line, then extracted text (probe only)",
    )
    args = ap.parse_args()
    limit = max(1, min(args.limit, 50))

    client = _get_client()
    rows = fetch_sample(client, limit)
    if not rows:
        logger.info("Cohort is empty — nothing to probe.")
        return

    mode = "REVERSED (description first)" if args.reversed_order else "production ocr_image()"
    logger.info("Probing %d image(s) [%s]. No BQ / done / failure JSON writes.", len(rows), mode)

    with_desc = 0
    without_desc = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        url = row["content_url"]
        tid = row["ticket_id"]
        tail = url[-56:] if len(url) > 56 else url
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                logger.warning("[%d/%d] ticket=%s HTTP %s …%s", i, len(rows), tid, r.status_code, tail)
                errors += 1
                continue
            if len(r.content) > MAX_IMAGE_BYTES:
                logger.warning("[%d/%d] ticket=%s too_large …%s", i, len(rows), tid, tail)
                errors += 1
                continue
        except Exception as exc:
            logger.warning("[%d/%d] ticket=%s download_error %s …%s", i, len(rows), tid, exc, tail)
            errors += 1
            continue

        ct = r.headers.get("Content-Type", row["content_type"]).split(";")[0].strip()
        if ct not in SUPPORTED_MIME:
            ct = "image/jpeg"

        try:
            if args.reversed_order:
                new_text, err = ocr_image_reversed_order(r.content, ct)
            else:
                new_text, err = ocr_image(r.content, ct)
        except Exception as exc:
            logger.error("[%d/%d] ticket=%s ocr_image raised: %s", i, len(rows), tid, exc)
            errors += 1
            continue

        if err:
            logger.warning("[%d/%d] ticket=%s ocr_image err=%s …%s", i, len(rows), tid, err[:120], tail)
            errors += 1
            continue

        text = (new_text or "").strip()
        has = MARKER in text
        if has:
            with_desc += 1
        else:
            without_desc += 1

        preview = text.replace("\n", " ")[:160]
        logger.info(
            "[%d/%d] ticket=%s has_marker=%s new_len=%d old_len=%d preview=%r",
            i,
            len(rows),
            tid,
            has,
            len(text),
            row["old_len"],
            preview,
        )

    logger.info("=" * 60)
    logger.info(
        "SUMMARY: with %s: %d | without: %d | download/ocr errors: %d",
        MARKER,
        with_desc,
        without_desc,
        errors,
    )
    logger.info("No database or resume files were modified.")
    flush_bq_events(timeout=15.0)


if __name__ == "__main__":
    main()
