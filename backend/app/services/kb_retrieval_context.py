"""
Shared KB retrieval status for prompt injection (ticket chat + universal chat).

Lets the model and operators distinguish:
- search not run (routing / disabled);
- search ran, zero hits (indexing or query mismatch vs “topic not in corpus”);
- search error (infrastructure).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def format_kb_status_for_prompt(kb_status: dict[str, Any] | None) -> str:
    """
    Long-form line(s) in English for the Phase-2 system context.
    kb_status is produced by chat routers; shape:
      state: "executed" | "not_selected" | "unavailable" | "skipped_missing_query" | "error"
      query, result_count, reason, error — optional
    """
    if not kb_status:
        return ""

    state = kb_status.get("state", "")
    lines: list[str] = [
        "",
        "=== KNOWLEDGE BASE (Vertex) — RETRIEVAL STATUS (authoritative) ===",
    ]

    if state == "executed":
        q = kb_status.get("query") or ""
        n = kb_status.get("result_count")
        if n is None:
            n = 0
        err = kb_status.get("error")
        lines.append(f"search_performed: yes")
        lines.append(f"query: {q!r}")
        lines.append(f"articles_returned: {n}")
        if int(n) == 0 and not err:
            lines.append(
                "Zero articles: the automated index search returned no matches. "
                "This does not prove the topic is undocumented — it may be a query phrasing / ranking issue "
                "or a gap in indexing. Do NOT ask the user to 'go to the knowledge base' or 'check documentation'; "
                "state clearly that *this* search returned nothing."
            )
        if err:
            lines.append(f"search_error: {err}")
        return "\n".join(lines) + "\n"

    if state == "error":
        q = kb_status.get("query")
        err = kb_status.get("error") or "unknown error"
        lines.append("search_performed: attempted; request failed (see error).")
        if q is not None:
            lines.append(f"intended_query: {q!r}")
        lines.append(f"search_error: {err}")
        lines.append(
            "Do NOT ask the user to look up the KB themselves; explain the lookup failed and suggest retry or alternate wording."
        )
        return "\n".join(lines) + "\n"

    if state == "unavailable":
        reason = kb_status.get("reason") or "KB search not available for this request."
        lines.append("search_performed: no (KB not offered for this turn).")
        lines.append(f"reason: {reason}")
        return "\n".join(lines) + "\n"

    if state == "skipped_missing_query":
        reason = kb_status.get("reason") or "search_knowledge_base was selected but no query was provided by routing."
        lines.append("search_performed: no (routing error).")
        lines.append(f"reason: {reason}")
        return "\n".join(lines) + "\n"

    if state == "not_selected":
        reason = kb_status.get("reason") or "The tool router did not run search_knowledge_base for this turn."
        lines.append("search_performed: no (not selected by router).")
        lines.append(f"reason: {reason}")
        return "\n".join(lines) + "\n"

    lines.append(f"search_performed: unknown (state={state!r})")
    if kb_status.get("reason"):
        lines.append(f"reason: {kb_status['reason']}")
    return "\n".join(lines) + "\n"


def log_kb_status(endpoint: str, kb_status: dict[str, Any] | None) -> None:
    if not kb_status:
        return
    st = kb_status.get("state")
    q = kb_status.get("query")
    n = kb_status.get("result_count")
    err = kb_status.get("error")
    logger.info(
        "kb_retrieval %s state=%s query=%r result_count=%s err=%s",
        endpoint, st, q, n, err,
    )
