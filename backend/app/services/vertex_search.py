"""
Vertex AI Agent Builder (Discovery Engine) search service.

Uses the REST API directly with explicit service account credentials
to avoid Google Cloud SDK credential conflicts (see PROJECT_LOG.md rules).

Data store: reference-support-knowledge (all knowledge records with source_id metadata).
  source_id = "zendesk_support_articles"    → maintained support articles
  source_id = "zendesk_operations_articles" → maintained operations articles
  source_id = "gdrive_*"                    → configured Google Drive sources

Source filtering note:
  Vertex AI Discovery Engine standard edition does not support filtering on
  struct_data fields. Source filtering is done in Python after receiving
  results — disabled sources are excluded from the returned list.
  The data store is searched with a larger page_size to account for filtering.
"""

import logging
import re
from typing import Optional

import google.auth.transport.requests
import requests
from google.oauth2 import service_account

from app.config import settings
from app.models.ai import KBArticleSource
from app.services.cost_tracker import CallType, record_flat_cost, vertex_search_cost_per_query_cad

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags from Vertex AI snippet text."""
    return _HTML_TAG_RE.sub("", text).strip()


_DISCOVERY_ENGINE_BASE = "https://discoveryengine.googleapis.com/v1"
_PROJECT = settings.google_cloud_project_id
_LOCATION = settings.vertex_ai_location
_DATA_STORE = settings.vertex_ai_data_store_id
_SERVING_CONFIG = (
    f"projects/{_PROJECT}/locations/{_LOCATION}"
    f"/collections/default_collection/dataStores/{_DATA_STORE}"
    f"/servingConfigs/default_config"
)


def _get_access_token() -> str:
    """Obtain a short-lived Bearer token. Tries service account file first, falls back to ADC."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            settings.google_application_credentials,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    except Exception:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token  # type: ignore[return-value]


def search_kb(
    query: str,
    page_size: int = 5,
    sources: Optional[list[str]] = None,
) -> list[KBArticleSource]:
    """
    Run a semantic search against the KB data store.

    Args:
        query:     Natural language search query.
        page_size: Max results to return (after source filtering).
        sources:   Source IDs to include. If None, fetches enabled sources from
                   Firestore (30s cache). Pass [] to return empty immediately.
                   Pass a list to override (e.g. for testing).

    Returns list of KBArticleSource with title, snippet, and Zendesk article URL.

    Note on source filtering: Vertex AI standard edition does not support API-level
    filtering on struct_data fields. We fetch page_size * 3 results and filter
    post-retrieval. If only one source is enabled, some retrieved results may be
    discarded, but the returned list will still have up to page_size items.
    """
    # Resolve sources
    if sources is None:
        from app.services.firestore import get_enabled_vertex_sources
        sources = get_enabled_vertex_sources()

    if not sources:
        logger.info("search_kb: no enabled Vertex AI sources — returning empty")
        return []

    sources_set = set(sources)
    # Fetch more than needed so post-filtering still yields page_size results
    fetch_size = min(page_size * 3, 50)

    token = _get_access_token()
    url = f"{_DISCOVERY_ENGINE_BASE}/{_SERVING_CONFIG}:search"

    payload = {
        "query": query,
        "pageSize": fetch_size,
        "queryExpansionSpec": {"condition": "AUTO"},
        "spellCorrectionSpec": {"mode": "AUTO"},
        "contentSearchSpec": {
            "snippetSpec": {"returnSnippet": True},
        },
    }

    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[KBArticleSource] = []
    seen_urls: set[str] = set()

    for item in data.get("results", []):
        doc = item.get("document", {})
        derived = doc.get("derivedStructData", {})
        struct  = doc.get("structData", {})

        # Backend-side source filtering
        doc_source_id = struct.get("source_id", "")
        if doc_source_id and doc_source_id not in sources_set:
            continue  # document belongs to a disabled source

        # Use file_name from struct_data for Drive results (title from derivedStructData may be
        # the GCS blob name, not the original Drive file name)
        if doc_source_id.startswith("gdrive_"):
            title: str = struct.get("file_name") or derived.get("title") or "Google Drive documentation"
        else:
            title = derived.get("title") or doc.get("name", "KB Article")

        snippets: list[dict] = derived.get("snippets", [])
        snippet_text = _strip_html(snippets[0].get("snippet", "") if snippets else "")

        # Resolve URL:
        # - Drive results: use drive_url from struct_data
        # - KB articles: convert GCS path to Zendesk article URL
        if doc_source_id.startswith("gdrive_"):
            link: str = struct.get("drive_url") or ""
        else:
            link = derived.get("link") or ""

        if not link:
            continue  # skip results without a usable URL

        # Convert GCS path to Zendesk article URL.
        # GCS filename format: {article_id}_{slug}.html
        # e.g. gs://esip-reference-data/support_articles_v2/930000000001_replacing-a-workstation.html
        if link.startswith("gs://"):
            filename = link.rsplit("/", 1)[-1]
            m = re.match(r"^(\d+)_", filename)
            if m:
                link = f"https://support.example.com/hc/en-us/articles/{m.group(1)}"

        if link in seen_urls:
            continue
        seen_urls.add(link)

        results.append(KBArticleSource(
            title=title,
            snippet=snippet_text,
            url=link,
        ))

        if len(results) >= page_size:
            break

    try:
        record_flat_cost(CallType.VERTEX_SEARCH, vertex_search_cost_per_query_cad(), units=1)
    except Exception as _e:
        logger.warning("search_kb: cost tracking failed (non-blocking): %s", _e)

    return results


def search_gdrive(
    query: str,
    page_size: int = 5,
) -> list[KBArticleSource]:
    """
    Search the Google Drive data store (SD Documentation).

    Queries the SD Documentation data store which indexes files from the
    "SD Documentation" folder in "Northstar Support Knowledge" Shared Drive
    via the native Google Drive connector.

    ⚠️  KNOWN BLOCKER (2026-03-27): Service accounts are NOT supported for
    GOOGLE_WORKSPACE data store search (403: "Search using service account
    credentials is not supported for workspace datastores"). Requires DWD
    impersonation with cloud-platform scope — pending Marvin adding that scope
    AND Google Drive API being enabled in the GCP project.

    Returns list of KBArticleSource with title, snippet, and Google Drive URL.
    Returns empty list if reference_support_docs source is disabled in Firestore.
    """
    from app.services.firestore import is_source_enabled
    if not is_source_enabled("reference_support_docs"):
        logger.info("search_gdrive: reference_support_docs disabled — skipping")
        return []

    _GDRIVE_DATA_STORE = settings.vertex_ai_gdrive_data_store_id
    # Drive data stores live in default_collection (confirmed via API listing 2026-03-27).
    # Earlier assumption that they use a separate collection was WRONG.
    gdrive_serving_config = (
        f"projects/{_PROJECT}/locations/{_LOCATION}"
        f"/collections/default_collection/dataStores/{_GDRIVE_DATA_STORE}"
        f"/servingConfigs/default_config"
    )

    token = _get_access_token()
    url = f"{_DISCOVERY_ENGINE_BASE}/{gdrive_serving_config}:search"

    payload = {
        "query": query,
        "pageSize": page_size,
        "queryExpansionSpec": {"condition": "AUTO"},
        "spellCorrectionSpec": {"mode": "AUTO"},
        "contentSearchSpec": {
            "snippetSpec": {"returnSnippet": True},
        },
    }

    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[KBArticleSource] = []
    seen_urls: set[str] = set()

    for item in data.get("results", []):
        doc = item.get("document", {})
        derived = doc.get("derivedStructData", {})

        title: str = derived.get("title") or doc.get("name", "SD Documentation")
        link: str = derived.get("link") or ""
        snippets: list[dict] = derived.get("snippets", [])
        snippet_text = _strip_html(snippets[0].get("snippet", "") if snippets else "")

        if not link or link in seen_urls:
            continue
        seen_urls.add(link)

        results.append(KBArticleSource(
            title=title,
            snippet=snippet_text,
            url=link,
        ))

        if len(results) >= page_size:
            break

    return results


def search_tickets(query: str, page_size: int = 10) -> list[dict]:
    """
    Search the tickets Vertex AI Agent Builder data store for semantically relevant tickets.

    Uses the tickets data store, which contains full-content ticket documents:
    all fields + all comments + OCR text, uploaded as gs://esip-reference-data/tickets/{id}.txt.

    Args:
        query:     Natural language search query.
        page_size: Max results to return.

    Returns:
        List of dicts with keys: ticket_id (int), snippet (str).
        ticket_id is extracted from the GCS filename (e.g. "950003.txt" → 950003).
    """
    from app.services.firestore import is_source_enabled

    if not is_source_enabled("tickets_bq"):
        logger.info("search_tickets: tickets_bq Search is disabled — returning empty")
        return []

    _TICKETS_DATA_STORE = settings.vertex_ai_tickets_data_store_id
    tickets_serving_config = (
        f"projects/{_PROJECT}/locations/{_LOCATION}"
        f"/collections/default_collection/dataStores/{_TICKETS_DATA_STORE}"
        f"/servingConfigs/default_config"
    )

    token = _get_access_token()
    url = f"{_DISCOVERY_ENGINE_BASE}/{tickets_serving_config}:search"

    payload = {
        "query": query,
        "pageSize": page_size,
        "queryExpansionSpec": {"condition": "AUTO"},
        "spellCorrectionSpec": {"mode": "AUTO"},
        "contentSearchSpec": {
            "snippetSpec": {"returnSnippet": True},
        },
    }

    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for item in data.get("results", []):
        doc = item.get("document", {})
        derived = doc.get("derivedStructData", {})

        # Extract ticket_id from the GCS link (e.g. gs://esip-reference-data/tickets/950003.txt)
        link: str = derived.get("link") or ""
        ticket_id: int | None = None
        if link:
            filename = link.rsplit("/", 1)[-1]
            stem = filename.rsplit(".", 1)[0]
            if stem.isdigit():
                ticket_id = int(stem)

        if ticket_id is None:
            # Fall back: try extracting from document name field
            doc_name: str = doc.get("name", "")
            name_part = doc_name.rsplit("/", 1)[-1]
            if name_part.isdigit():
                ticket_id = int(name_part)

        if ticket_id is None:
            continue

        snippets: list[dict] = derived.get("snippets", [])
        snippet_text = _strip_html(snippets[0].get("snippet", "") if snippets else "")

        results.append({"ticket_id": ticket_id, "snippet": snippet_text})

    try:
        record_flat_cost(CallType.VERTEX_SEARCH, vertex_search_cost_per_query_cad(), units=1)
    except Exception as _e:
        logger.warning("search_all_tickets: cost tracking failed (non-blocking): %s", _e)

    return results
