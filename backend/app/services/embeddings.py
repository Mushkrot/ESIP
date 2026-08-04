"""
Vertex AI text embedding service.

Uses gemini-embedding-001 (3072 dimensions, 8192 token limit) for semantic similarity.
Stable GA model released May 2025. Requires us-central1 region.
Task types:
  RETRIEVAL_DOCUMENT — for ticket content being indexed
  RETRIEVAL_QUERY    — for user queries at search time
"""

import logging
from typing import Callable

import vertexai
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

from app.config import settings
from app.services.cost_tracker import (
    CallType,
    calc_cost,
    check_cost_gate,
    get_gate_reserve,
    new_correlation_id,
    record_estimate,
    record_usage,
)

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "gemini-embedding-001"
_EMBEDDING_DIMS = 3072
_BATCH_SIZE = 5
_EMBED_MAX_TOKENS_EST = 8192  # model context limit — cap per string for gate projection

_model: TextEmbeddingModel | None = None


def _approx_embed_tokens(text: str) -> int:
    """Rough input tokens for one string; capped for gate projection safety."""
    return min(_EMBED_MAX_TOKENS_EST, max(1, len(text) // 4))


def _projected_embed_batch_cad(texts: list[str]) -> float:
    """CAD cost upper bound for embedding this batch (used by check_cost_gate)."""
    approx_in = sum(_approx_embed_tokens(t) for t in texts)
    return calc_cost(approx_in, 0, is_embedding=True)


def _get_model() -> TextEmbeddingModel:
    global _model
    if _model is None:
        # text-embedding-004 requires a regional endpoint — use gemini_location (us-central1).
        # vertex_ai_location is "global" which is unsupported for embedding models.
        vertexai.init(
            project=settings.google_cloud_project_id,
            location=settings.gemini_location,
        )
        _model = TextEmbeddingModel.from_pretrained(_EMBEDDING_MODEL)
    return _model


def embed_texts(
    texts: list[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    progress_cb: Callable[[str], None] | None = None,
    call_type: CallType = CallType.EMBED_TICKET,
) -> list[list[float]]:
    """
    Generate embeddings for a list of texts.

    Args:
        texts: Input strings (model supports up to 8192 tokens each).
        task_type: "RETRIEVAL_DOCUMENT" for content, "RETRIEVAL_QUERY" for queries.
        progress_cb: Optional callback for progress reporting.
        call_type: Cost tracker call type (default EMBED_TICKET; use EMBED_QUERY for queries).

    Returns:
        List of embedding vectors (list[float], length 3072) in same order as input.
    """
    if not texts:
        return []

    job_expected = _projected_embed_batch_cad(texts)
    job_reserved = get_gate_reserve(call_type, job_expected)
    check_cost_gate(call_type, job_reserved, fail_closed=True)

    model = _get_model()
    results: list[list[float]] = []

    for batch_start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[batch_start: batch_start + _BATCH_SIZE]

        batch_expected = _projected_embed_batch_cad(batch)
        batch_reserved = get_gate_reserve(call_type, batch_expected)
        check_cost_gate(call_type, batch_reserved, fail_closed=True)

        corr = new_correlation_id()
        record_estimate(
            call_type,
            batch_expected,
            corr,
            est_basis="formula",
            est_input_tokens=sum(_approx_embed_tokens(t) for t in batch),
            est_output_tokens=0,
            expected_cost_cad=batch_expected,
            reserved_cost_cad=batch_reserved,
        )

        inputs = [
            TextEmbeddingInput(
                text=t if t else "",
                task_type=task_type,
            )
            for t in batch
        ]
        embeddings = model.get_embeddings(inputs)
        results.extend(e.values for e in embeddings)

        # Embedding model doesn't expose usage_metadata — approximate from text length.
        batch_chars = sum(len(t) for t in batch)
        approx_tokens = max(1, batch_chars // 4)
        record_usage(
            call_type, approx_tokens, 0, is_embedding=True,
            correlation_id=corr,
            est_cost_usd=batch_expected,
            expected_cost_cad=batch_expected,
        )

        if progress_cb:
            progress_cb(f"Embedded {min(batch_start + _BATCH_SIZE, len(texts))}/{len(texts)}")

    return results


def embed_query(text: str) -> list[float]:
    """Generate a single query embedding (RETRIEVAL_QUERY task type)."""
    return embed_texts([text], task_type="RETRIEVAL_QUERY", call_type=CallType.EMBED_QUERY)[0]
