"""
Import configured Zendesk articles into Vertex AI with source metadata.

What this script does:
  1. Reads all KB articles from BQ (id, title, section, category)
  2. Checks which articles already have HTML files in GCS
  3. Fetches missing article HTML from Zendesk API and uploads to GCS
  4. Updates existing Vertex AI documents with the canonical source identifier
  5. Creates documents for article files not yet present in the data store
  6. Creates docs for the newly uploaded missing articles with proper source_id

Source ID mapping:
  - section == "Support Knowledge"    → source_id = "zendesk_support_articles"
  - section == "Operations Knowledge" → source_id = "zendesk_operations_articles"

Usage:
  python import_knowledge_vertex.py --dry-run    # analyze gaps, no changes
  python import_knowledge_vertex.py              # full run
  python import_knowledge_vertex.py --skip-fetch # update existing records only
"""

import argparse
import hashlib
import logging
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.cloud import bigquery, discoveryengine_v1beta as discoveryengine, storage
from google.oauth2 import service_account
from google.protobuf import struct_pb2

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GCP_PROJECT      = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "esip-reference-project")
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
DATA_STORE_ID    = os.getenv("VERTEX_AI_DATA_STORE_ID", "reference-support-knowledge")
BUCKET_NAME      = "esip-reference-data"
GCS_PREFIX_KB    = "support_articles_v2"
GCS_PREFIX_CORP  = "operations_articles_v2"

ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "example")
ZENDESK_EMAIL     = os.getenv("ZENDESK_EMAIL")
ZENDESK_TOKEN     = os.getenv("ZENDESK_API_TOKEN")
ZENDESK_AUTH      = (f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

# Vertex AI Discovery Engine parent path
DATASTORE_PARENT = (
    f"projects/{GCP_PROJECT}/locations/global/collections/default_collection"
    f"/dataStores/{DATA_STORE_ID}/branches/default_branch"
)

SOURCE_ID_MAP = {
    "Support Knowledge":    "zendesk_support_articles",
    "Operations Knowledge": "zendesk_operations_articles",
}
GCS_PREFIX_MAP = {
    "zendesk_support_articles":    GCS_PREFIX_KB,
    "zendesk_operations_articles": GCS_PREFIX_CORP,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GCP clients
# ---------------------------------------------------------------------------

def _creds():
    return service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def get_bq_client():
    return bigquery.Client(project=GCP_PROJECT, credentials=_creds())


def get_storage_client():
    return storage.Client(project=GCP_PROJECT, credentials=_creds())


def get_doc_client():
    return discoveryengine.DocumentServiceClient(credentials=_creds())


# ---------------------------------------------------------------------------
# Step 1: Read BQ articles
# ---------------------------------------------------------------------------

def fetch_bq_articles(bq: bigquery.Client) -> dict[int, dict]:
    """Return article metadata keyed by article ID."""
    rows = list(bq.query(
        f"SELECT id, title, url, section, category "
        f"FROM `{GCP_PROJECT}.esip_reference.kb_articles`"
    ).result())
    articles = {}
    for r in rows:
        source_id = SOURCE_ID_MAP.get(r.section)
        if not source_id:
            log.warning("Unknown section '%s' for article %s, skipping", r.section, r.id)
            continue
        articles[r.id] = {
            "title":     r.title,
            "section":   r.section,
            "source_id": source_id,
            "url":       r.url,
        }
    return articles


# ---------------------------------------------------------------------------
# Step 2: Scan GCS
# ---------------------------------------------------------------------------

def scan_gcs(storage_client: storage.Client) -> dict[int, str]:
    """Return {article_id: gcs_uri} for all existing HTML files in both prefixes."""
    bucket = storage_client.bucket(BUCKET_NAME)
    result = {}
    for prefix in (GCS_PREFIX_KB, GCS_PREFIX_CORP):
        for blob in bucket.list_blobs(prefix=f"{prefix}/"):
            fname = blob.name.split("/")[-1]
            if not fname.endswith(".html"):
                continue
            try:
                article_id = int(fname.split("_")[0])
                result[article_id] = f"gs://{BUCKET_NAME}/{blob.name}"
            except (ValueError, IndexError):
                log.warning("Cannot parse article_id from %s", blob.name)
    log.info("GCS: %d articles found", len(result))
    return result


# ---------------------------------------------------------------------------
# Step 3: Fetch missing articles from Zendesk and upload to GCS
# ---------------------------------------------------------------------------

def _zendesk_get(url: str, params: dict = None) -> dict:
    for attempt in range(1, 4):
        r = requests.get(url, params=params, auth=ZENDESK_AUTH, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            log.warning("Rate limited — sleeping %ds", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            log.warning("Server error %d on attempt %d", r.status_code, attempt)
            time.sleep(5 * attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed after 3 attempts: {url}")


def _slugify(title: str) -> str:
    """Create a URL-safe slug from an article title."""
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug[:60].rstrip("-") or "article"


def _gcs_upload_token(storage_client: storage.Client) -> str:
    """Get a short-lived access token for GCS REST uploads."""
    from google.auth.transport.requests import Request
    creds = storage_client._credentials
    creds.refresh(Request())
    return creds.token


def fetch_and_upload_missing(
    missing_ids: list[int],
    articles: dict[int, dict],
    storage_client: storage.Client,
    dry_run: bool,
) -> dict[int, str]:
    """
    Fetch HTML body for each missing article from Zendesk, upload to GCS.
    Returns {article_id: gcs_uri} for newly uploaded articles.
    """
    if not missing_ids:
        log.info("No missing articles to fetch.")
        return {}

    log.info("Fetching %d missing articles from Zendesk...", len(missing_ids))
    token = _gcs_upload_token(storage_client)
    uploaded: dict[int, str] = {}

    for i, article_id in enumerate(missing_ids, 1):
        meta = articles[article_id]
        source_id = meta["source_id"]
        gcs_prefix = GCS_PREFIX_MAP[source_id]

        # Fetch article HTML from Zendesk
        url = f"{ZENDESK_BASE}/help_center/articles/{article_id}/translations/en-us.json"
        try:
            data = _zendesk_get(url)
            body_html = data.get("translation", {}).get("body", "")
            title     = data.get("translation", {}).get("title", meta["title"])
        except Exception as exc:
            log.error("  [%d/%d] article %s: fetch error — %s", i, len(missing_ids), article_id, exc)
            continue

        if not body_html:
            log.warning("  [%d/%d] article %s: empty body — skipping", i, len(missing_ids), article_id)
            continue

        # Build HTML content
        html = (
            f"<!DOCTYPE html><html><head>"
            f"<title>{title}</title>"
            f"<meta charset='utf-8'/>"
            f"<meta name='article_id' content='{article_id}'/>"
            f"</head><body>"
            f"<h1>{title}</h1>"
            f"{body_html}"
            f"</body></html>"
        )

        slug = _slugify(title)
        blob_name = f"{gcs_prefix}/{article_id}_{slug}.html"
        gcs_uri   = f"gs://{BUCKET_NAME}/{blob_name}"

        if dry_run:
            log.info("  [DRY RUN] would upload %s", gcs_uri)
            uploaded[article_id] = gcs_uri
            continue

        # Upload via REST to avoid system-level ADC conflicts.
        # Refresh token every 200 uploads
        if i % 200 == 0:
            token = _gcs_upload_token(storage_client)

        r = requests.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET_NAME}/o",
            params={"uploadType": "media", "name": blob_name},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/html; charset=utf-8",
            },
            data=html.encode("utf-8"),
            timeout=120,
        )
        if r.status_code not in (200, 201):
            log.error("  GCS upload error for %s: %s %s", blob_name, r.status_code, r.text[:200])
            continue

        uploaded[article_id] = gcs_uri
        if i % 20 == 0 or i == len(missing_ids):
            log.info("  Uploaded %d/%d", i, len(missing_ids))

        time.sleep(0.1)  # gentle rate limiting

    log.info("Uploaded %d/%d missing articles to GCS", len(uploaded), len(missing_ids))
    return uploaded


# ---------------------------------------------------------------------------
# Step 4: Vertex AI — update existing docs with source_id
# ---------------------------------------------------------------------------

def update_existing_docs(
    doc_client: discoveryengine.DocumentServiceClient,
    articles: dict[int, dict],
    dry_run: bool,
) -> int:
    """
    List all documents in the data store, extract article_id from GCS URI,
    update struct_data.source_id based on BQ article metadata.
    """
    log.info("Listing all documents in Vertex AI data store...")
    request = discoveryengine.ListDocumentsRequest(
        parent=DATASTORE_PARENT,
        page_size=1000,
    )
    all_docs = list(doc_client.list_documents(request))
    log.info("Found %d documents in data store", len(all_docs))

    updated = 0
    skipped = 0
    for i, doc in enumerate(all_docs, 1):
        # Extract GCS URI
        from google.protobuf.json_format import MessageToDict
        d = MessageToDict(doc._pb)
        gcs_uri = d.get("content", {}).get("uri", "")
        if not gcs_uri:
            log.warning("  doc %s has no content URI — skipping", doc.id)
            skipped += 1
            continue

        # Extract article_id from GCS filename
        fname = gcs_uri.split("/")[-1]
        try:
            article_id = int(fname.split("_")[0])
        except (ValueError, IndexError):
            log.warning("  Cannot parse article_id from %s — skipping", fname)
            skipped += 1
            continue

        # Look up source_id from BQ data
        meta = articles.get(article_id)
        if not meta:
            # Records without metadata use the primary support-article source.
            source_id = "zendesk_support_articles"
        else:
            source_id = meta["source_id"]

        # Check if already set correctly
        existing_struct = d.get("structData", {})
        if existing_struct.get("source_id") == source_id:
            skipped += 1
            continue

        if dry_run:
            if updated < 3:
                log.info("  [DRY RUN] would update doc %s → source_id=%s", doc.id, source_id)
            elif updated == 3:
                log.info("  [DRY RUN] ... (suppressing further output)")
            updated += 1
            continue

        # Build updated document
        struct_data = struct_pb2.Struct()
        struct_data["source_id"] = source_id

        updated_doc = discoveryengine.Document(
            name=doc.name,
            struct_data=struct_data,
        )
        update_req = discoveryengine.UpdateDocumentRequest(
            document=updated_doc,
            update_mask={"paths": ["struct_data"]},
            allow_missing=False,
        )
        try:
            doc_client.update_document(update_req)
            updated += 1
        except Exception as exc:
            log.error("  Update failed for doc %s: %s", doc.id, exc)
            skipped += 1

        if updated % 100 == 0:
            log.info("  Updated %d/%d docs...", updated, len(all_docs))

    log.info("Updated %d docs, skipped %d", updated, skipped)
    return updated


# ---------------------------------------------------------------------------
# Step 5: Create new documents (for corporate + newly uploaded articles)
# ---------------------------------------------------------------------------

def _doc_id_from_gcs_uri(gcs_uri: str) -> str:
    """Generate a stable document ID from GCS URI (MD5 hex, 32 chars)."""
    return hashlib.md5(gcs_uri.encode(), usedforsecurity=False).hexdigest()


def create_new_docs(
    doc_client: discoveryengine.DocumentServiceClient,
    new_articles: dict[int, str],  # {article_id: gcs_uri}
    articles: dict[int, dict],
    existing_gcs_uris: set[str],
    dry_run: bool,
) -> int:
    """
    Create Vertex AI documents for articles not yet in the data store.
    new_articles: {article_id: gcs_uri} — articles with GCS files but no Vertex AI doc.
    """
    if not new_articles:
        log.info("No new documents to create.")
        return 0

    log.info("Creating %d new documents in Vertex AI...", len(new_articles))
    created = 0

    for article_id, gcs_uri in new_articles.items():
        meta = articles.get(article_id)
        source_id = meta["source_id"] if meta else "zendesk_support_articles"

        doc_id = _doc_id_from_gcs_uri(gcs_uri)

        if dry_run:
            if created < 3:
                log.info("  [DRY RUN] would create doc %s → %s (source_id=%s)", doc_id, gcs_uri, source_id)
            created += 1
            continue

        struct_data = struct_pb2.Struct()
        struct_data["source_id"] = source_id

        doc = discoveryengine.Document(
            id=doc_id,
            struct_data=struct_data,
            content=discoveryengine.Document.Content(
                mime_type="text/html",
                uri=gcs_uri,
            ),
        )
        create_req = discoveryengine.CreateDocumentRequest(
            parent=DATASTORE_PARENT,
            document=doc,
            document_id=doc_id,
        )
        try:
            doc_client.create_document(create_req)
            created += 1
        except Exception as exc:
            log.error("  Create failed for article %s: %s", article_id, exc)

        if created % 20 == 0:
            log.info("  Created %d/%d docs...", created, len(new_articles))

    log.info("Created %d new documents", created)
    return created


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool, skip_fetch: bool) -> None:
    log.info("=" * 60)
    log.info("Knowledge article import with source metadata")
    log.info("Dry run: %s | Skip fetch: %s", dry_run, skip_fetch)
    log.info("=" * 60)

    # Init clients
    bq_client      = get_bq_client()
    storage_client = get_storage_client()
    doc_client     = get_doc_client()

    # 1. Read BQ articles
    log.info("\n[1/5] Reading KB articles from BigQuery...")
    articles = fetch_bq_articles(bq_client)
    log.info("BQ articles: %d total (%d support, %d operations)",
             len(articles),
             sum(1 for a in articles.values() if a["source_id"] == "zendesk_support_articles"),
             sum(1 for a in articles.values() if a["source_id"] == "zendesk_operations_articles"))

    # 2. Scan GCS
    log.info("\n[2/5] Scanning GCS...")
    gcs_articles = scan_gcs(storage_client)
    existing_gcs_uris = set(gcs_articles.values())

    # 3. Identify missing articles
    missing_ids = [aid for aid in articles if aid not in gcs_articles]
    log.info("Missing from GCS: %d articles", len(missing_ids))
    missing_kb   = [aid for aid in missing_ids if articles[aid]["source_id"] == "zendesk_support_articles"]
    missing_corp = [aid for aid in missing_ids if articles[aid]["source_id"] == "zendesk_operations_articles"]
    log.info("  support articles missing:    %d", len(missing_kb))
    log.info("  operations articles missing: %d", len(missing_corp))

    # 4. Fetch and upload missing articles
    if not skip_fetch:
        log.info("\n[3/5] Fetching missing articles from Zendesk and uploading to GCS...")
        newly_uploaded = fetch_and_upload_missing(missing_ids, articles, storage_client, dry_run)
    else:
        log.info("\n[3/5] Skipping fetch (--skip-fetch)")
        newly_uploaded = {}

    # 5. Update existing Vertex AI documents with source_id
    log.info("\n[4/5] Updating existing Vertex AI documents with source_id...")
    update_existing_docs(doc_client, articles, dry_run)

    # 6. Create new documents for configured article files and new uploads.
    log.info("\n[5/5] Creating new Vertex AI documents...")
    # Articles with GCS files but presumably not yet in the data store:
    # Operations articles and newly uploaded records are candidates for creation.
    needs_create: dict[int, str] = {}
    # Operations articles in GCS — check whether they already exist in the data store.
    for article_id, gcs_uri in gcs_articles.items():
        meta = articles.get(article_id)
        if meta and meta["source_id"] == "zendesk_operations_articles":
            needs_create[article_id] = gcs_uri
    # Add newly uploaded
    needs_create.update(newly_uploaded)
    log.info("New documents to create: %d", len(needs_create))

    create_new_docs(doc_client, needs_create, articles, existing_gcs_uris, dry_run)

    log.info("\n" + "=" * 60)
    log.info("DONE")
    log.info("  BQ articles:            %d", len(articles))
    log.info("  GCS files (before):     %d", len(gcs_articles))
    log.info("  Missing fetched:        %d", len(newly_uploaded))
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Zendesk articles to Vertex AI with source metadata")
    parser.add_argument("--dry-run",    action="store_true", help="Analyze gaps, no changes made")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip fetching missing articles from Zendesk")
    args = parser.parse_args()
    run(dry_run=args.dry_run, skip_fetch=args.skip_fetch)
