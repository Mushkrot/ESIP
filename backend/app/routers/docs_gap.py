"""
Knowledge Gap Analysis endpoint.

POST /api/docs-gap/stream
  — Phase 1: fetch top categories from BQ + KB coverage via Vertex AI
  — Phase 2: streaming Gemini gap report
  — Returns SSE events identical to other chat endpoints
"""

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.dependencies import CurrentUser
from app.config import settings
from app.models.knowledge_gap import (
    DraftRequest,
    DraftResult,
    KnowledgeGapAnalysisResult,
    KnowledgeGapCriteria,
)
from app.services import docs_gap, vertex_search
from app.services import firestore
from app.services import docs_gap_v2
from app.services import reference_runtime

logger = logging.getLogger(__name__)
router = APIRouter()


class GapChatMessage(BaseModel):
    role: str
    content: str


class GapRequest(BaseModel):
    messages: list[GapChatMessage]
    days: int = 90
    min_tickets: int = 50
    sort_by: str = "resolution_time"  # "volume" | "resolution_time" | "open_time"


@router.post("/stream")
async def gap_stream(req: GapRequest, user: CurrentUser):
    if settings.runtime_profile == "reference":
        async def reference_events():
            report = "## Knowledge coverage review\n\nThe strongest opportunity is to add the final validation and evidence step to the payment recovery procedure. Network failover guidance should also be retitled with the terms engineers use in tickets."
            yield f"data: {json.dumps({'delta': report})}\n\n"
            yield f"data: {json.dumps({'done': True, 'sources': [{'title': 'Support recovery checklist', 'url': 'https://docs.example.com/support-recovery'}], 'categories': []})}\n\n"
        return StreamingResponse(reference_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    messages = [m.model_dump() for m in req.messages]
    loop = asyncio.get_event_loop()

    # Phase 1: fetch BQ categories
    try:
        categories = await loop.run_in_executor(
            None,
            lambda: docs_gap.fetch_top_categories(req.days, req.min_tickets, req.sort_by),
        )
    except Exception as exc:
        logger.error("docs_gap BQ fetch failed: %s", exc)
        categories = []

    # Fetch KB coverage for each category in parallel
    async def _search_kb(category: dict) -> tuple[str, list]:
        try:
            results = await loop.run_in_executor(
                None,
                lambda: vertex_search.search_kb(category["display_name"], page_size=3),
            )
            return category["issue_category"], results
        except Exception as exc:
            logger.warning("KB search for %s failed: %s", category["issue_category"], exc)
            return category["issue_category"], []

    kb_coverage: dict = {}
    if categories:
        coverage_results = await asyncio.gather(*[_search_kb(c) for c in categories])
        kb_coverage = dict(coverage_results)

    async def event_stream():
        full_text = ""
        try:
            def _sync_gen():
                return list(docs_gap.gap_stream(
                    messages=messages,
                    categories=categories,
                    kb_coverage=kb_coverage,
                    days=req.days,
                    sort_by=req.sort_by,
                ))
            chunks = await loop.run_in_executor(None, _sync_gen)
            for chunk in chunks:
                full_text += chunk
                yield f"data: {json.dumps({'delta': chunk})}\n\n"
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("docs_gap stream error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        sources_json = _extract_json_footer(full_text, "SOURCES_JSON:")
        sources = sources_json.get("sources", []) if isinstance(sources_json, dict) else []
        yield f"data: {json.dumps({'done': True, 'sources': sources, 'categories': categories})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v2/analyze", response_model=KnowledgeGapAnalysisResult)
async def analyze_v2(req: KnowledgeGapCriteria, user: CurrentUser):
    if settings.runtime_profile == "reference":
        return reference_runtime.knowledge_gap_analysis(req.model_dump(mode="json"))
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: docs_gap_v2.analyze(req))

    def _save_run() -> KnowledgeGapAnalysisResult:
        metadata = firestore.save_knowledge_gap_run(
            result=result.model_dump(mode="json"),
            owner_uid=user.get("uid") or user.get("email"),
            owner_email=user.get("email"),
        )
        return result.model_copy(
            update={
                "run_id": metadata["run_id"],
                "created_at": metadata["created_at"],
            }
        )

    try:
        return await loop.run_in_executor(None, _save_run)
    except Exception as exc:
        logger.warning("Knowledge Gaps run persistence failed: %s", exc)
        return result


@router.post("/v2/draft", response_model=DraftResult)
async def draft_v2(req: DraftRequest, user: CurrentUser):
    if settings.runtime_profile == "reference":
        return reference_runtime.knowledge_gap_draft(req.model_dump(mode="json"))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: docs_gap_v2.draft(req))


@router.get("/v2/runs")
async def list_runs_v2(user: CurrentUser):
    if settings.runtime_profile == "reference":
        return {"runs": reference_runtime.knowledge_gap_runs()}
    loop = asyncio.get_event_loop()
    owner_uid = user.get("uid") or user.get("email")
    return await loop.run_in_executor(
        None,
        lambda: {"runs": firestore.list_knowledge_gap_runs(owner_uid=owner_uid)},
    )


@router.get("/v2/runs/{run_id}")
async def get_run_v2(run_id: str, user: CurrentUser):
    if settings.runtime_profile == "reference":
        record = reference_runtime.knowledge_gap_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Knowledge Gaps run not found")
        return record
    loop = asyncio.get_event_loop()
    owner_uid = user.get("uid") or user.get("email")
    record = await loop.run_in_executor(
        None,
        lambda: firestore.get_knowledge_gap_run(run_id=run_id, owner_uid=owner_uid),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Knowledge Gaps run not found")
    return record


def _extract_json_footer(text: str, marker: str):
    idx = text.rfind(marker)
    if idx == -1:
        return None
    try:
        return json.loads(text[idx + len(marker):].strip().split("\n")[0])
    except Exception:
        return None
