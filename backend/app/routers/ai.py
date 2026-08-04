"""
AI endpoints — ticket summary generation and KB suggestions.

Legacy ticket-detail streaming chat was removed. Product chat is
POST /api/universal/stream.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.config import settings
from app.dependencies import AnyUser
from app.models.ai import SummaryRequest, SummaryResponse
from app.services import bigquery, firestore, gemini, vertex_search, reference_runtime
from app.services.zendesk import zendesk

logger = logging.getLogger(__name__)
router = APIRouter()

DETAILED_SUMMARY_GENERATION = "structured_sections_v1"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@router.post("/summary", response_model=SummaryResponse)
def get_summary(req: SummaryRequest, user: AnyUser):
    """
    Return an AI summary for the given ticket.
    English is the canonical summary. Non-English summaries are translated from
    the English cache and then cached per language.
    """
    language = (req.language or "en").strip().lower() or "en"

    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        summary = reference_runtime.summary(req.ticket_id, req.mode)
        if summary is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Ticket #{req.ticket_id} was not found.")
        return SummaryResponse(
            ticket_id=req.ticket_id,
            mode=req.mode,
            summary=summary,
            generated_at=datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc),
            from_cache=not req.refresh,
        )

    if req.refresh:
        _invalidate_summary_family(req.ticket_id, req.mode, language)

    cached = firestore.get_cached_summary(req.ticket_id, req.mode, language)
    if _usable_summary_cache(cached, req.mode, language):
        return SummaryResponse(
            ticket_id=req.ticket_id,
            mode=req.mode,
            summary=cached["summary"],
            generated_at=cached["generated_at"],
            from_cache=True,
        )

    try:
        ticket = zendesk.get_ticket(req.ticket_id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    english_cache = firestore.get_cached_summary(req.ticket_id, req.mode, "en")
    if _usable_summary_cache(english_cache, req.mode, "en"):
        english_summary = english_cache["summary"]
        english_generated_at = english_cache["generated_at"]
    else:
        english_summary = _generate_english_summary(req.ticket_id, req.mode, ticket)
        now = datetime.now(timezone.utc)
        english_generated_at = now
        firestore.set_cached_summary(
            req.ticket_id,
            req.mode,
            {
                "summary": english_summary,
                "canonical_language": "en",
                "summary_generation": _summary_generation(req.mode),
            },
            "en",
            ticket.status,
        )

    if language == "en":
        return SummaryResponse(
            ticket_id=req.ticket_id,
            mode=req.mode,
            summary=english_summary,
            generated_at=english_generated_at,
            from_cache=False,
        )

    try:
        translated_summary = gemini.translate_summary(english_summary, req.mode, language)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Gemini translation error: {exc}")

    firestore.set_cached_summary(
        req.ticket_id,
        req.mode,
        {
            "summary": translated_summary,
            "translated_from_language": "en",
            "source_summary_generated_at": english_generated_at,
            "source_summary_generation": _summary_generation(req.mode),
        },
        language,
        ticket.status,
    )

    return SummaryResponse(
        ticket_id=req.ticket_id,
        mode=req.mode,
        summary=translated_summary,
        generated_at=datetime.now(timezone.utc),
        from_cache=False,
    )


def _usable_summary_cache(cached: dict | None, mode: str, language: str) -> bool:
    if not cached:
        return False
    summary = cached.get("summary")
    if not isinstance(summary, dict):
        return False
    # Auto-invalidate stale detailed summary formats:
    # - Pre-2026-03-22 old schema had "overview" key (very old)
    # - Pre-2026-04-10 schema had "participants" but no "markdown_text" (now Variant B)
    if mode == "detailed" and "markdown_text" not in summary:
        return False
    if mode == "detailed":
        if language == "en" and cached.get("summary_generation") != DETAILED_SUMMARY_GENERATION:
            return False
        if language != "en" and cached.get("source_summary_generation") != DETAILED_SUMMARY_GENERATION:
            return False
    # Old non-English entries were independently re-summarized. New entries must
    # be translations derived from the canonical English summary.
    if language != "en" and cached.get("translated_from_language") != "en":
        return False
    return True


def _summary_generation(mode: str) -> str:
    return DETAILED_SUMMARY_GENERATION if mode == "detailed" else "brief_v1"


def _invalidate_summary_family(ticket_id: int, mode: str, language: str) -> None:
    firestore.invalidate_summary_cache(ticket_id, mode, "en")
    if language != "en":
        firestore.invalidate_summary_cache(ticket_id, mode, language)
        return
    for lang in ("fr", "es", "zh", "ru", "hi"):
        firestore.invalidate_summary_cache(ticket_id, mode, lang)


def _generate_english_summary(ticket_id: int, mode: str, ticket) -> dict:
    kb_results = []
    try:
        kb_results = vertex_search.search_kb(ticket.subject, page_size=3 if mode == "brief" else 5)
    except Exception as exc:
        logger.warning("KB search failed for ticket %s: %s", ticket_id, exc)

    images_by_comment: dict = {}
    try:
        images_by_comment = bigquery.fetch_ticket_images_by_comment(ticket_id)
    except Exception as exc:
        logger.warning("Images fetch failed for ticket %s: %s", ticket_id, exc)

    try:
        return gemini.generate_summary(
            ticket,
            mode,
            kb_results,
            language="en",
            images_by_comment=images_by_comment,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Gemini error: {exc}")


# ---------------------------------------------------------------------------
# KB Suggestions — auto-load for ticket detail panel
# ---------------------------------------------------------------------------

class KBSuggestionsRequest(BaseModel):
    query: str
    page_size: int = 5


@router.post("/kb-suggestions")
def get_kb_suggestions(req: KBSuggestionsRequest, user: AnyUser):
    """
    Semantic KB + Drive search for a free-text query.
    Used by the AI Chat left panel to auto-load relevant articles when a ticket is opened.
    """
    if settings.runtime_profile == "reference" and user.get("reference_runtime"):
        return {
            "articles": [
                article.model_dump()
                for article in reference_runtime.knowledge_suggestions(req.query.strip(), req.page_size)
            ]
        }
    try:
        results = vertex_search.search_kb(req.query.strip(), page_size=req.page_size)
        return {"articles": [r.model_dump() for r in results]}
    except Exception as exc:
        logger.warning("kb-suggestions failed: %s", exc)
        return {"articles": []}
