"""
Zendesk Help Center → BigQuery KB articles metadata export.

Exports metadata for all published KB articles into BigQuery table `kb_articles`.
Does NOT export article body text — only metadata needed for SQL analytics:
  id, title, url, section, category, locale, created_at, updated_at, exported_at

This enables analytics queries like:
  - "How many KB articles do we have?"
  - "List all articles about POS"
  - "Which sections have the most articles?"
  - "Show me all French articles"

Usage:
  python export_kb_articles_bq.py          # full export (truncates + re-inserts)
  python export_kb_articles_bq.py --dry-run  # fetch but don't write to BQ
"""

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "example")
ZENDESK_EMAIL     = os.getenv("ZENDESK_EMAIL")
ZENDESK_TOKEN     = os.getenv("ZENDESK_API_TOKEN")
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZENDESK_AUTH      = (f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)

GCP_PROJECT      = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "esip-reference-project")
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
BQ_DATASET       = "esip_reference"
BQ_TABLE         = "kb_articles"

MAX_RETRIES  = 3
RETRY_BACKOFF = 5
SLEEP_BETWEEN_PAGES = 0.3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BQ schema
# ---------------------------------------------------------------------------

KB_ARTICLES_SCHEMA = [
    bigquery.SchemaField("id",          "INT64",   mode="REQUIRED"),
    bigquery.SchemaField("title",       "STRING"),
    bigquery.SchemaField("url",         "STRING"),
    bigquery.SchemaField("section",     "STRING"),  # e.g. "POS Terminal"
    bigquery.SchemaField("category",    "STRING"),  # e.g. "Hardware"
    bigquery.SchemaField("locale",      "STRING"),  # e.g. "en-us", "fr"
    bigquery.SchemaField("created_at",  "TIMESTAMP"),
    bigquery.SchemaField("updated_at",  "TIMESTAMP"),
    bigquery.SchemaField("exported_at", "TIMESTAMP"),
]

# ---------------------------------------------------------------------------
# Zendesk helpers
# ---------------------------------------------------------------------------

def _zendesk_get(url: str, params: dict = None) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.get(url, params=params, auth=ZENDESK_AUTH, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", RETRY_BACKOFF))
            log.warning("Rate limited — sleeping %ds", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            log.warning("Server error %d on attempt %d", r.status_code, attempt)
            time.sleep(RETRY_BACKOFF * attempt)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}")


def fetch_sections() -> dict[int, dict]:
    """Fetch all HC sections, return dict: section_id → {name, category_name}."""
    sections: dict[int, dict] = {}
    url = f"{ZENDESK_BASE}/help_center/sections"
    while url:
        data = _zendesk_get(url, params={"per_page": 100})
        for s in data.get("sections", []):
            sections[s["id"]] = {
                "name":     s.get("name", ""),
                "category_id": s.get("category_id"),
            }
        url = data.get("next_page")
        if url:
            time.sleep(SLEEP_BETWEEN_PAGES)

    # Fetch categories to resolve category names
    categories: dict[int, str] = {}
    cat_url = f"{ZENDESK_BASE}/help_center/categories"
    while cat_url:
        data = _zendesk_get(cat_url, params={"per_page": 100})
        for c in data.get("categories", []):
            categories[c["id"]] = c.get("name", "")
        cat_url = data.get("next_page")
        if cat_url:
            time.sleep(SLEEP_BETWEEN_PAGES)

    # Attach category name to each section
    for s in sections.values():
        s["category_name"] = categories.get(s["category_id"], "")

    log.info("Fetched %d sections across %d categories", len(sections), len(categories))
    return sections


def fetch_all_articles(sections: dict) -> list[dict]:
    """Fetch all published articles from Zendesk Help Center, all locales."""
    articles = []
    url = f"{ZENDESK_BASE}/help_center/articles"
    page = 1
    while url:
        log.info("Fetching articles page %d...", page)
        data = _zendesk_get(url, params={"per_page": 100, "sort_by": "created_at"})
        for a in data.get("articles", []):
            section = sections.get(a.get("section_id"), {})
            articles.append({
                "id":          a["id"],
                "title":       a.get("title", ""),
                "url":         a.get("html_url", ""),
                "section":     section.get("name", ""),
                "category":    section.get("category_name", ""),
                "locale":      a.get("locale", ""),
                "created_at":  a.get("created_at"),
                "updated_at":  a.get("updated_at"),
            })
        url = data.get("next_page")
        page += 1
        if url:
            time.sleep(SLEEP_BETWEEN_PAGES)

    log.info("Fetched %d articles total", len(articles))
    return articles

# ---------------------------------------------------------------------------
# BigQuery helpers
# ---------------------------------------------------------------------------

def get_bq_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


def ensure_table(client: bigquery.Client) -> None:
    dataset_ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET)
    table_ref = dataset_ref.table(BQ_TABLE)
    try:
        client.get_table(table_ref)
        log.info("Table %s already exists", BQ_TABLE)
    except Exception:
        table = bigquery.Table(table_ref, schema=KB_ARTICLES_SCHEMA)
        client.create_table(table)
        log.info("Created table %s", BQ_TABLE)


def truncate_table(client: bigquery.Client) -> None:
    table_id = f"`{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}`"
    client.query(f"TRUNCATE TABLE {table_id}").result()
    log.info("Truncated %s", BQ_TABLE)


def insert_articles(client: bigquery.Client, articles: list[dict], now: datetime) -> None:
    rows = []
    for a in articles:
        rows.append({
            "id":          a["id"],
            "title":       a["title"],
            "url":         a["url"],
            "section":     a["section"],
            "category":    a["category"],
            "locale":      a["locale"],
            "created_at":  a["created_at"],
            "updated_at":  a["updated_at"],
            "exported_at": now.isoformat(),
        })
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        log.error("BQ insert errors: %s", errors[:3])
    else:
        log.info("Inserted %d articles into %s", len(rows), BQ_TABLE)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool) -> None:
    now = datetime.now(timezone.utc)

    log.info("Fetching sections and categories from Zendesk Help Center...")
    sections = fetch_sections()

    log.info("Fetching all articles...")
    articles = fetch_all_articles(sections)

    if dry_run:
        log.info("[DRY RUN] Would insert %d articles — sample:", len(articles))
        for a in articles[:5]:
            log.info("  [%s] %s / %s — %s", a["locale"], a["category"], a["section"], a["title"])
        return

    log.info("Connecting to BigQuery...")
    client = get_bq_client()
    ensure_table(client)
    truncate_table(client)
    insert_articles(client, articles, now)

    log.info("=" * 50)
    log.info("DONE — %d KB articles exported to BQ", len(articles))
    log.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zendesk KB articles → BigQuery")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch articles but do not write to BigQuery")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
