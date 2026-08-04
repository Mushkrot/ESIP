"""Zendesk Help Center article publication-state cleanup.

Only published Zendesk Help Center articles should be searchable in ESIP.
Draft/unpublished articles must be removed from BigQuery metadata, GCS HTML
exports, and the Vertex AI data store.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from google.cloud import bigquery, discoveryengine_v1beta as discoveryengine, storage
from google.oauth2 import service_account
from google.protobuf.json_format import MessageToDict

from app.config import settings

logger = logging.getLogger(__name__)

GCP_PROJECT = settings.google_cloud_project_id
BQ_DATASET = "esip_reference"
BQ_KB_TABLE = "kb_articles"
KB_GCS_BUCKET = "esip-reference-data"
KB_GCS_PREFIXES = ("support_articles_v2", "operations_articles_v2")

DATASTORE_PARENT = (
    f"projects/{GCP_PROJECT}/locations/global/collections/default_collection"
    f"/dataStores/{settings.vertex_ai_data_store_id}/branches/default_branch"
)

_ARTICLE_ID_RE = re.compile(r"/(\d+)[_-]|^(\d+)_")


@dataclass
class IndexedArticleRefs:
    article_id: int
    title: str = ""
    bq_present: bool = False
    gcs_blobs: list[str] = field(default_factory=list)
    vertex_documents: list[str] = field(default_factory=list)


def _creds():
    return service_account.Credentials.from_service_account_file(
        settings.google_application_credentials,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def _bq_client() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT, credentials=_creds())


def _storage_client() -> storage.Client:
    return storage.Client(project=GCP_PROJECT, credentials=_creds())


def _document_client() -> discoveryengine.DocumentServiceClient:
    return discoveryengine.DocumentServiceClient(credentials=_creds())


def is_zendesk_article_published(article: dict[str, Any]) -> bool:
    """Return True only for Zendesk Help Center articles that are published."""
    return article.get("draft") is not True


def article_publication_state(article: dict[str, Any]) -> str:
    return "published" if is_zendesk_article_published(article) else "draft"


def extract_article_id_from_uri(uri: str) -> int | None:
    """Extract Zendesk article id from a GCS URI/blob name or Zendesk article URL."""
    text = str(uri or "")
    match = _ARTICLE_ID_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def find_indexed_unpublished_articles(
    unpublished_articles: list[dict[str, Any]],
    *,
    bq_client: bigquery.Client | None = None,
    storage_client: storage.Client | None = None,
    doc_client: discoveryengine.DocumentServiceClient | None = None,
) -> dict[int, IndexedArticleRefs]:
    """Find unpublished Zendesk articles that still exist in ESIP indexes."""
    ids = sorted({
        int(article["id"])
        for article in unpublished_articles
        if article.get("id") is not None
    })
    refs: dict[int, IndexedArticleRefs] = {
        article_id: IndexedArticleRefs(
            article_id=article_id,
            title=str(next((a.get("title") for a in unpublished_articles if int(a["id"]) == article_id), "") or ""),
        )
        for article_id in ids
    }
    if not ids:
        return refs

    bq = bq_client or _bq_client()
    rows = bq.query(
        f"""
        SELECT id
        FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_KB_TABLE}`
        WHERE id IN UNNEST(@article_ids)
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("article_ids", "INT64", ids)]
        ),
    ).result()
    for row in rows:
        refs[int(row.id)].bq_present = True

    storage_cli = storage_client or _storage_client()
    bucket = storage_cli.bucket(KB_GCS_BUCKET)
    ids_set = set(ids)
    for prefix in KB_GCS_PREFIXES:
        for blob in bucket.list_blobs(prefix=f"{prefix}/"):
            article_id = extract_article_id_from_uri(blob.name)
            if article_id in ids_set:
                refs[article_id].gcs_blobs.append(blob.name)

    docs = doc_client or _document_client()
    request = discoveryengine.ListDocumentsRequest(parent=DATASTORE_PARENT, page_size=1000)
    for doc in docs.list_documents(request):
        data = MessageToDict(doc._pb)
        uri = data.get("content", {}).get("uri", "")
        article_id = extract_article_id_from_uri(uri)
        if article_id in ids_set:
            refs[article_id].vertex_documents.append(doc.name)

    return refs


def cleanup_unpublished_articles(
    unpublished_articles: list[dict[str, Any]],
    *,
    dry_run: bool = True,
    bq_client: bigquery.Client | None = None,
    storage_client: storage.Client | None = None,
    doc_client: discoveryengine.DocumentServiceClient | None = None,
) -> dict[str, Any]:
    """Remove unpublished Zendesk articles from BQ, GCS, and Vertex AI."""
    refs = find_indexed_unpublished_articles(
        unpublished_articles,
        bq_client=bq_client,
        storage_client=storage_client,
        doc_client=doc_client,
    )
    indexed = [ref for ref in refs.values() if ref.bq_present or ref.gcs_blobs or ref.vertex_documents]
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "unpublished_articles": len(unpublished_articles),
        "indexed_unpublished_articles": len(indexed),
        "bq_rows": sum(1 for ref in refs.values() if ref.bq_present),
        "gcs_blobs": sum(len(ref.gcs_blobs) for ref in refs.values()),
        "vertex_documents": sum(len(ref.vertex_documents) for ref in refs.values()),
        "deleted_bq_rows": 0,
        "deleted_gcs_blobs": 0,
        "deleted_vertex_documents": 0,
        "samples": [
            {
                "id": ref.article_id,
                "title": ref.title,
                "bq_present": ref.bq_present,
                "gcs_blobs": ref.gcs_blobs[:3],
                "vertex_documents": ref.vertex_documents[:3],
            }
            for ref in indexed[:20]
        ],
    }
    if dry_run or not indexed:
        return summary

    bq = bq_client or _bq_client()
    bq_ids = [ref.article_id for ref in refs.values() if ref.bq_present]
    if bq_ids:
        bq.query(
            f"""
            DELETE FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_KB_TABLE}`
            WHERE id IN UNNEST(@article_ids)
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("article_ids", "INT64", bq_ids)]
            ),
        ).result()
        summary["deleted_bq_rows"] = len(bq_ids)

    storage_cli = storage_client or _storage_client()
    bucket = storage_cli.bucket(KB_GCS_BUCKET)
    for ref in refs.values():
        for blob_name in ref.gcs_blobs:
            bucket.blob(blob_name).delete()
            summary["deleted_gcs_blobs"] += 1

    docs = doc_client or _document_client()
    for ref in refs.values():
        for doc_name in ref.vertex_documents:
            docs.delete_document(name=doc_name)
            summary["deleted_vertex_documents"] += 1
            time.sleep(0.05)

    return summary
