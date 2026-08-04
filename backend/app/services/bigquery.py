"""
BigQuery analytics service for ESIP.

Executes read-only SQL queries against the esip_reference dataset.
Used by the analytics chat endpoint to answer questions about ticket trends,
brand statistics, resolution times, recurring issues, etc.

Security guardrails:
  - Only SELECT statements are allowed
  - LIMIT is enforced (max 1000 rows)
  - Query timeout: 30 seconds
  - No DDL/DML (INSERT, UPDATE, DELETE, DROP, CREATE) — rejected immediately
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery
from google.oauth2 import service_account

from app.config import settings
from app.reference_data.fixtures import BRAND_TAG_MAP as REFERENCE_BRAND_TAG_MAP

logger = logging.getLogger(__name__)

GCP_PROJECT = settings.google_cloud_project_id
BQ_DATASET  = "esip_reference"
MAX_ROWS    = 1000
TIMEOUT_SEC = 30
ZENDESK_ORGANIZATION_PROFILES_TABLE = "zendesk_organization_profiles"

ZENDESK_ORGANIZATION_PROFILES_SCHEMA = [
    bigquery.SchemaField("org_id", "INT64", mode="REQUIRED"),
    bigquery.SchemaField("org_name", "STRING"),
    bigquery.SchemaField("store_id", "STRING"),
    bigquery.SchemaField("brand", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("pos_type", "STRING"),
    bigquery.SchemaField("simphony_store", "BOOL"),
    bigquery.SchemaField("simphony_install_date", "TIMESTAMP"),
    bigquery.SchemaField("pos_server_ip", "STRING"),
    bigquery.SchemaField("kds", "BOOL"),
    bigquery.SchemaField("dmb", "BOOL"),
    bigquery.SchemaField("guest_wifi", "BOOL"),
    bigquery.SchemaField("phone_type", "STRING"),
    bigquery.SchemaField("internet_connection", "STRING"),
    bigquery.SchemaField("tags", "STRING", mode="REPEATED"),
    bigquery.SchemaField("source_updated_at", "TIMESTAMP"),
    bigquery.SchemaField("synced_at", "TIMESTAMP"),
    bigquery.SchemaField("raw_fields_json", "STRING"),
    bigquery.SchemaField("raw_profile_json", "STRING"),
]

_ZENDESK_ORG_PROFILE_REQUIRED_FIELDS = {
    field.name for field in ZENDESK_ORGANIZATION_PROFILES_SCHEMA if field.mode == "REQUIRED"
}

# Human-readable schema description injected into the analytics system prompt.
# Gemini uses this to generate correct SQL.
SCHEMA_DESCRIPTION = """
BigQuery dataset: `esip_reference` (project: esip-reference-project)

TABLE: `tickets`  — one row per Zendesk ticket
  id                      INT64       Zendesk ticket ID
  subject                 STRING      Ticket subject line
  status                  STRING      open / pending / solved / closed
  priority                STRING      Zendesk ticket priority: low / normal / high / urgent
  ticket_type             STRING      Zendesk ticket type, e.g. incident
  ticket_form_id          INT64       Zendesk ticket form ID
  ticket_form_name        STRING      Zendesk ticket form display name
  brand                   STRING      Metro Burger / Cedar Grill / Frontier Kitchen / Lakeside Tavern / etc.
  store_id                STRING      4-digit store identifier
  org_id                  INT64       Zendesk organization ID
  org_name                STRING      e.g. "Cedar Grill 2001"
  created_at              TIMESTAMP   When ticket was created
  updated_at              TIMESTAMP   Last update timestamp
  solved_at               TIMESTAMP   When solved/closed (NULL if still open)
  resolution_hours        FLOAT64     Hours from created to solved (NULL if open)
  sla_policy_name         STRING      Derived ESIP SLA policy name when known
  resolution_sla_target_hours FLOAT64 Derived resolution SLA target for known incident policies
  resolution_sla_elapsed_hours FLOAT64 Hours used to compare against the resolution SLA target
  resolution_sla_breached BOOL        True when elapsed hours exceed the derived resolution SLA target
  resolution_sla_status   STRING      met / breached / open_within_target / open_breached / unknown / not_applicable
  time_spent_sec          INT64       Total engineer time on ticket in seconds
  via_channel             STRING      How ticket came in: web / email / phone / api
  source                  STRING      Detailed source: source_phone / source_web / source_email
  assignee_id             INT64       Zendesk assigned user ID
  assignee_email          STRING      Zendesk assigned user email when available
  assignee_name           STRING      Engineer assigned to ticket
  assignee_group_id       INT64       Zendesk assignment group ID
  assignee_group_name     STRING      Zendesk assignment group name (e.g. vendor/queue such as Field Services Partner)
  requester_name          STRING      Store contact name
  issue_category          STRING      Main issue category (e.g. pos_terminal, network, phone)
  product_type            STRING      Device type: POS / KDS / Phone / Wi-Fi / UPS / etc.
  caused_by               STRING      Root cause category
  resolution_type         STRING      How it was resolved
  symptoms                STRING      Free-text description of symptoms
  workaround              STRING      Any workaround applied
  next_steps              STRING      Recommended next steps
  resolution              STRING      How the ticket was resolved (free text)
  root_cause_description  STRING      Detailed root cause description (free text)
  escalated               BOOL        True if internally escalated
  vendor_dispatched       BOOL        True if a vendor was dispatched
  vendor_escalation       BOOL        True if escalated to vendor
  tags                    ARRAY<STRING>  Zendesk tags
  comment_count           INT64       Total comments on ticket
  internal_comment_count  INT64       Internal notes count
  public_comment_count    INT64       Public reply count
  attachment_count        INT64       Number of attachments/screenshots
  full_text               STRING      Concatenated text for semantic search
  exported_at             TIMESTAMP   When row was written by export pipeline

TABLE: `zendesk_organization_profiles` — one row per Zendesk organization/store profile
  org_id                  INT64       Zendesk organization ID; join to tickets.org_id
  org_name                STRING      Zendesk organization/store name
  store_id                STRING      4-digit store identifier when available
  brand                   STRING      Tenant brand when available
  status                  STRING      active/suspended or source profile status
  pos_type                STRING      Organization-level POS Type, e.g. Simphony or Micros 5.5
  simphony_store          BOOL        Source organization flag for Simphony stores
  simphony_install_date   TIMESTAMP   Simphony install date when available
  pos_server_ip           STRING      POS server IP from organization profile
  kds                     BOOL        KDS flag from organization profile
  dmb                     BOOL        DMB flag from organization profile
  guest_wifi              BOOL        Guest Wi-Fi flag from organization profile
  phone_type              STRING      Phone type from organization profile
  internet_connection     STRING      Internet connection/profile notes
  tags                    ARRAY<STRING> Zendesk organization tags
  source_updated_at       TIMESTAMP   Zendesk organization updated_at
  synced_at               TIMESTAMP   When ESIP synced this profile row
  raw_fields_json         STRING      Raw organization_fields JSON
  raw_profile_json        STRING      Raw organization JSON

Use zendesk_organization_profiles for: questions about stores where POS Type = Simphony,
POS Type comparisons, and store-profile hardware flags. POS Type is NOT a tickets column.
Primary join pattern:
  tickets t JOIN zendesk_organization_profiles p ON t.org_id = p.org_id
Only use store_id fallback when explicitly labeled as fallback coverage.

TABLE: `kb_articles`  — metadata for all 948 Zendesk Help Center articles
  id          INT64       Zendesk article ID
  title       STRING      Article title (English or French)
  url         STRING      Full public URL (https://support.example.com/hc/...)
  section     STRING      Section name within the KB
  category    STRING      Category name (top-level grouping)
  locale      STRING      Language: 'en-us' (English) or 'fr' (French)
  created_at  TIMESTAMP   When article was published
  updated_at  TIMESTAMP   Last update timestamp
  exported_at TIMESTAMP   When row was written by export pipeline

Use kb_articles for: counting articles, listing by title/section/category/locale,
finding articles by keyword in title. For full-text semantic search use Vertex AI KB.

TABLE: `ticket_comments`  — one row per comment (public replies + internal notes)
  ticket_id     INT64       FK → tickets.id
  comment_id    INT64       Zendesk comment ID
  created_at    TIMESTAMP   Comment timestamp
  is_internal   BOOL        True = internal note (engineer-to-engineer), False = public reply
  author_id     INT64       Zendesk user ID of the comment author
  author_name   STRING      Comment author name
  author_email  STRING      Comment author email when available
  author_role   STRING      Zendesk role: end-user / agent / admin when available
  author_is_agent BOOL      True for agent/admin authors, False for end-user authors
  via_channel   STRING      Zendesk comment via channel
  body          STRING      Full comment text
  exported_at   TIMESTAMP

TABLE: `ticket_attachments`  — screenshots and file attachments
  ticket_id     INT64       FK → tickets.id
  comment_id    INT64       FK → ticket_comments.comment_id
  attachment_id INT64       Zendesk attachment ID (NULL for inline pasted images)
  file_name     STRING      Original filename
  content_type  STRING      MIME type: image/png, image/jpeg, application/pdf, etc.
  size_bytes    INT64       File size in bytes
  url           STRING      Zendesk API URL
  content_url   STRING      Direct CDN URL — use for AI vision analysis
  is_image      BOOL        True for image/* content types (screenshots, photos)
  created_at    TIMESTAMP
  exported_at   TIMESTAMP

TABLE: `ticket_attachment_ocr`  — OCR text extracted from ticket images
  content_url        STRING      PK — same CDN URL as ticket_attachments.content_url
  ticket_id          INT64       FK → tickets.id
  ocr_text           STRING      Text extracted from the image (NULL if image had no text)
  processing_status  STRING      success / skipped / failed
  skip_reason        STRING      Reason for skipping (unsupported_type, too_large, etc.)
  processed_at       TIMESTAMP   When OCR was run

Use ticket_attachment_ocr to: find tickets with specific text in screenshots,
answer questions about screenshot content, enrich semantic search context.
JOIN pattern: tickets t JOIN ticket_attachment_ocr o ON t.id = o.ticket_id

CRITICAL: When a user asks about content that may appear in screenshots or images (IP addresses,
error codes, configuration values, Slack messages, UI text, etc.), you MUST JOIN ticket_attachment_ocr
to find tickets where that text appears in screenshots — it may not appear in any other ticket field.

Example — find tickets where screenshots mention a specific IP or keyword:
  SELECT DISTINCT t.id, t.subject, t.created_at, t.status, LEFT(o.ocr_text, 300) AS screenshot_text
  FROM `PROJECT.DATASET.tickets` t
  JOIN `PROJECT.DATASET.ticket_attachment_ocr` o ON t.id = o.ticket_id
  WHERE o.processing_status = 'success'
    AND o.ocr_text IS NOT NULL
    AND LOWER(o.ocr_text) LIKE '%whitelist%'
  ORDER BY t.created_at DESC
  LIMIT 20

Always use LOWER(o.ocr_text) LIKE '%keyword%' for text search in OCR results (not full-text search).
When users ask about "IP whitelist", "screenshots", "error messages in images", "configuration in screenshots",
always include a JOIN with ticket_attachment_ocr in the SQL.

IMPORTANT BRAND NOTE: The `brand` column contains NULL and empty string ('') for tickets
where the brand was not filled in. Always normalize brand when grouping:
  COALESCE(NULLIF(brand, ''), 'Unknown brand') AS brand
This ensures NULL and '' are merged into a single "Unknown brand" group.

IMPORTANT SLUG NOTE: The following columns store raw Zendesk tag/slug values (lowercase with
underscores), NOT human-readable display names. Never use display names in WHERE clauses.
  caused_by examples:   'end_user_-_inquiry_', 'rootcause_software', 'rootcause_hardware',
                         'power_outage/failure', 'internet_failure', 'rootcause_cabling',
                         'end_user_-_training_', 'rc_change_-_software'
  issue_category examples: 'infrastructure__internet_connection__primary',
                            'applications__store_operations__end_of_day', 'server__database'
  product_type examples: 'pos', 'kds', 'phone', 'wi-fi'
  resolution_type, source, via_channel: similarly slug-formatted

When filtering these columns from conversation context (e.g. follow-up questions), always use
LOWER() + LIKE pattern matching instead of exact equality:
  -- Find training-related tickets:
  WHERE LOWER(caused_by) LIKE '%training%'
  -- Find software root cause:
  WHERE LOWER(caused_by) LIKE '%software%'
  -- Find Store Operations application tickets:
  WHERE LOWER(issue_category) LIKE '%store_operations%'
  -- Find inquiry tickets:
  WHERE LOWER(caused_by) LIKE '%inquiry%'

USEFUL QUERY PATTERNS:
  -- Tickets by brand last 30 days (with unknown brand normalization):
  SELECT COALESCE(NULLIF(brand, ''), 'Unknown brand') AS brand, COUNT(*) as cnt
  FROM `tickets`
  WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY brand ORDER BY cnt DESC LIMIT 20

  -- Average resolution time by issue category:
  SELECT issue_category, ROUND(AVG(resolution_hours),1) as avg_hours, COUNT(*) as cnt
  FROM `tickets` WHERE status IN ('solved','closed') AND resolution_hours IS NOT NULL
  GROUP BY issue_category ORDER BY avg_hours DESC LIMIT 20

  -- Tickets with attachments (screenshots):
  SELECT t.id, t.subject, COUNT(a.attachment_id) as screenshots
  FROM `tickets` t JOIN `ticket_attachments` a ON t.id = a.ticket_id
  WHERE a.is_image = TRUE GROUP BY t.id, t.subject ORDER BY screenshots DESC LIMIT 20

Always use fully-qualified table names: `esip-reference-project.esip_reference.tickets`

IMPORTANT ASSIGNMENT GROUP NOTE:
  org_name is the store organization, not the assigned vendor or support group.
  For questions about assigned group, assignment queue, vendor queue, or "Field Services Partner tickets",
  use assignee_group_name or assignee_group_id. Do not use org_name for assigned-group filters.
"""


# ---------------------------------------------------------------------------
# BQ client (singleton per process)
# ---------------------------------------------------------------------------

_bq_client: bigquery.Client | None = None


def _get_client() -> bigquery.Client:
    global _bq_client
    if _bq_client is None:
        try:
            creds = service_account.Credentials.from_service_account_file(
                settings.google_application_credentials,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _bq_client = bigquery.Client(project=GCP_PROJECT, credentials=creds)
        except Exception:
            # On GCP VMs: use Application Default Credentials (service account attached to VM)
            _bq_client = bigquery.Client(project=GCP_PROJECT)
    return _bq_client


def zendesk_organization_profiles_table_id() -> str:
    return f"{GCP_PROJECT}.{BQ_DATASET}.{ZENDESK_ORGANIZATION_PROFILES_TABLE}"


def ensure_zendesk_organization_profiles_table(
    client: bigquery.Client | None = None,
) -> bigquery.Table:
    """Create or evolve the Zendesk organization profile table only."""
    client = client or _get_client()
    table_id = zendesk_organization_profiles_table_id()

    try:
        table = client.get_table(table_id)
    except google_exceptions.NotFound:
        table = bigquery.Table(table_id, schema=ZENDESK_ORGANIZATION_PROFILES_SCHEMA)
        table.description = (
            "Zendesk organization/store profile attributes used for ESIP "
            "analytics joins, including POS Type."
        )
        table.clustering_fields = ["org_id", "store_id", "pos_type"]
        try:
            return client.create_table(table)
        except google_exceptions.Conflict:
            table = client.get_table(table_id)

    existing = {field.name for field in table.schema}
    missing_required = sorted(_ZENDESK_ORG_PROFILE_REQUIRED_FIELDS - existing)
    if missing_required:
        raise RuntimeError(
            f"Existing {ZENDESK_ORGANIZATION_PROFILES_TABLE} table is missing "
            f"required field(s): {', '.join(missing_required)}"
        )

    additions = [
        field
        for field in ZENDESK_ORGANIZATION_PROFILES_SCHEMA
        if field.name not in existing
    ]
    if additions:
        table.schema = list(table.schema) + additions
        table = client.update_table(table, ["schema"])

    return table


# ---------------------------------------------------------------------------
# SQL guardrails
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|MERGE|CALL|EXECUTE)\b",
    re.IGNORECASE,
)


def _strip_sql_literals(sql: str) -> str:
    """Remove string literal contents before identifier checks."""
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def _reject_unavailable_ticket_fields(sql: str) -> None:
    """Reject known hallucinated analytics columns before BigQuery execution."""
    searchable = _strip_sql_literals(sql)
    invalid_sla_identifier = next(
        (
            identifier
            for identifier in re.findall(r"\b\w*sla\w*\b", searchable, flags=re.IGNORECASE)
            if "resolution_sla_" not in identifier.lower()
            and identifier.lower() != "sla_policy_name"
        ),
        None,
    )
    if invalid_sla_identifier:
        raise ValueError(
            "Use the supported SLA fields in esip_reference.tickets: "
            "sla_policy_name, resolution_sla_target_hours, "
            "resolution_sla_elapsed_hours, resolution_sla_breached, and "
            "resolution_sla_status."
        )


def _normalize_chat_sql(sql: str) -> str:
    """Narrow compatibility rewrites for common Gemini SQL mistakes."""
    # Gemini may copy prompt examples like STRING_AGG(..., '\n' ORDER BY ...)
    # into a real newline inside a quoted delimiter. BigQuery treats literal
    # newlines inside single-quoted strings as "Unclosed string literal".
    # Only normalize the narrow case where the whole string literal is a newline.
    sql = re.sub(r"'\s*\n\s*'", r"'\\n'", sql)

    # tickets.store_id is STRING. Gemini sometimes emits numeric literals.
    sql = re.sub(
        r"(?P<prefix>\b(?:\w+\.)?store_id\s*=\s*)(?P<store>\d{3,6})(?!\s*['\"])",
        lambda m: f"{m.group('prefix')}'{m.group('store')}'",
        sql,
        flags=re.IGNORECASE,
    )

    sql = re.sub(
        r"(?P<field>\b(?:\w+\.)?store_id)\s+BETWEEN\s+(?P<start>\d{3,6})\s+AND\s+(?P<end>\d{3,6})(?!\s*['\"])",
        lambda m: f"{m.group('field')} BETWEEN '{m.group('start')}' AND '{m.group('end')}'",
        sql,
        flags=re.IGNORECASE,
    )

    sql = re.sub(
        r"(?P<prefix>\b(?:\w+\.)?store_id\s*(?:>=|<=|>|<)\s*)(?P<store>\d{3,6})(?!\s*['\"])",
        lambda m: f"{m.group('prefix')}'{m.group('store')}'",
        sql,
        flags=re.IGNORECASE,
    )

    sql = re.sub(
        r"(?P<prefix>\bSTARTS_WITH\s*\(\s*(?:\w+\.)?store_id\s*,\s*)(?P<prefix_id>\d{1,6})(?P<suffix>\s*\))",
        lambda m: f"{m.group('prefix')}'{m.group('prefix_id')}'{m.group('suffix')}",
        sql,
        flags=re.IGNORECASE,
    )

    sql = _rewrite_assignee_group_equals(sql)

    def _quote_store_in(match: re.Match) -> str:
        prefix = match.group("prefix")
        raw_items = [item.strip() for item in match.group("items").split(",")]
        if not raw_items or not all(re.fullmatch(r"\d{3,6}", item) for item in raw_items):
            return match.group(0)
        quoted = ", ".join(f"'{item}'" for item in raw_items)
        return f"{prefix}({quoted})"

    sql = re.sub(
        r"(?P<prefix>\b(?:\w+\.)?store_id\s+IN\s*)\((?P<items>\s*\d{3,6}(?:\s*,\s*\d{3,6})*\s*)\)",
        _quote_store_in,
        sql,
        flags=re.IGNORECASE,
    )

    # "Today" should mean Eastern business date for operators, not UTC midnight.
    sql = re.sub(
        r"(?P<field>\b(?:\w+\.)?created_at)\s*>=\s*TIMESTAMP_TRUNC\s*\(\s*CURRENT_TIMESTAMP\s*\(\s*\)\s*,\s*DAY\s*\)",
        lambda m: (
            f"DATE({m.group('field')}, 'America/New_York') = "
            "CURRENT_DATE('America/New_York')"
        ),
        sql,
        flags=re.IGNORECASE,
    )
    # Operators often say "on-hold"; Zendesk/BQ status stores this as "hold".
    sql = re.sub(r"(?i)'on-hold'", "'hold'", sql)
    sql = _rewrite_issue_category_literal_equals(sql)
    return sql


def _rewrite_assignee_group_equals(sql: str) -> str:
    """Make assigned-group equality filters case-insensitive."""
    pattern = re.compile(
        r"(?P<field>(?:\b\w+\.)?assignee_group_name)\s*=\s*'(?P<value>[^']+)'",
        flags=re.IGNORECASE,
    )

    def repl(match: re.Match) -> str:
        value = re.sub(r"\s+", " ", match.group("value")).strip().lower()
        if not value:
            return match.group(0)
        return f"LOWER({match.group('field')}) = '{value}'"

    return pattern.sub(repl, sql)


def _rewrite_issue_category_literal_equals(sql: str) -> str:
    """Rewrite model-generated exact readable issue labels into robust LIKE filters."""
    pattern = re.compile(
        r"(?P<field>(?:\b\w+\.)?issue_category)\s*=\s*'(?P<value>[^']+)'",
        flags=re.IGNORECASE,
    )

    def repl(match: re.Match) -> str:
        field = match.group("field")
        value = match.group("value")
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if token not in {"issue", "issues", "category", "categories"}
        ]
        if not tokens:
            return match.group(0)
        clauses = [f"LOWER({field}) LIKE '%{token}%'" for token in tokens]
        return "(" + " AND ".join(clauses) + ")"

    return pattern.sub(repl, sql)


def _sanitize_sql(sql: str) -> str:
    """
    Validate and normalize SQL:
      - Must be a SELECT statement
      - No DDL/DML keywords
      - Enforce LIMIT if missing
    Returns sanitized SQL or raises ValueError.
    """
    sql = sql.strip().rstrip(";")
    sql = _normalize_chat_sql(sql)

    if not sql.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed in analytics.")

    if re.search(r"\b(?:\w+\.)?assignee_group\b", sql, flags=re.IGNORECASE):
        raise ValueError(
            "assignee_group is not available in esip_reference.tickets. "
            "Use assignee_group_name for Zendesk assigned-group filters, assignee_name "
            "for assigned engineer names, or tags/full_text for vendor mentions."
        )
    _reject_unavailable_ticket_fields(sql)

    m = _FORBIDDEN_PATTERNS.search(sql)
    if m:
        raise ValueError(f"Forbidden SQL keyword: {m.group()}")

    # Enforce LIMIT
    if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql = f"{sql}\nLIMIT {MAX_ROWS}"
    else:
        # Cap existing LIMIT at MAX_ROWS
        def _cap_limit(match):
            n = int(match.group(1))
            return f"LIMIT {min(n, MAX_ROWS)}"
        sql = re.sub(r"\bLIMIT\s+(\d+)", _cap_limit, sql, flags=re.IGNORECASE)

    return sql


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def _serialize_cell(v) -> str | int | float | bool | None:
    """Convert a BigQuery cell value to a JSON-serializable form with human-friendly formatting.
    All timestamps are converted to Eastern time (EST/EDT)."""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        if hasattr(v, "hour"):
            # Convert UTC → Eastern Time ( UTC-5 EST / UTC-4 EDT)
            from datetime import timezone, timedelta
            import datetime as _dt
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            # Determine EST vs EDT: second Sunday in March → first Sunday in November
            year = v.year
            # DST starts: 2nd Sunday of March at 2am
            march1 = _dt.datetime(year, 3, 1, tzinfo=timezone.utc)
            dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
            dst_start = dst_start.replace(hour=7)  # 2am EST = 7am UTC
            # DST ends: 1st Sunday of November at 2am
            nov1 = _dt.datetime(year, 11, 1, tzinfo=timezone.utc)
            dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
            dst_end = dst_end.replace(hour=6)  # 2am EDT = 6am UTC
            if dst_start <= v < dst_end:
                eastern = v + timedelta(hours=-4)
            else:
                eastern = v + timedelta(hours=-5)
            return eastern.strftime("%Y-%m-%d %H:%M ET")
        return v.strftime("%Y-%m-%d")
    return v


def execute_query(sql: str) -> dict[str, Any]:
    """
    Execute a validated analytics SQL query against esip_reference.

    Returns:
        {
            "columns": ["col1", "col2", ...],
            "rows":    [[val, val, ...], ...],
            "row_count": int,
            "sql_executed": str,
        }
    Raises ValueError for invalid SQL, RuntimeError for BQ errors.
    """
    sanitized = _sanitize_sql(sql)
    logger.info("Analytics query: %s", sanitized[:200])

    client = _get_client()
    job_config = bigquery.QueryJobConfig(
        use_query_cache=True,
        maximum_bytes_billed=10 * 1024 ** 3,  # 10 GB safety cap
    )

    try:
        job = client.query(sanitized, job_config=job_config)
        rows = list(job.result(timeout=TIMEOUT_SEC))
    except Exception as exc:
        logger.error("BigQuery query failed: %s", exc)
        raise RuntimeError(f"Query failed: {exc}") from exc

    if not rows:
        return {"columns": [], "rows": [], "row_count": 0, "sql_executed": sanitized}

    columns = list(rows[0].keys())
    data = []
    for row in rows:
        data.append([_serialize_cell(v) for v in row.values()])

    return {
        "columns":      columns,
        "rows":         data,
        "row_count":    len(data),
        "sql_executed": sanitized,
    }


_BRAND_DISPLAY = REFERENCE_BRAND_TAG_MAP

# Short technical codes that should be fully uppercased
_UPPERCASE_CODES = {"kds", "pos", "dmb", "ups", "msp", "api", "dt", "ids"}


def _slug_to_title(slug: str) -> str:
    """
    Convert a Zendesk tagger slug to a human-readable title.

    Handles patterns like:
      rootcause_software         → Software
      end_user_-_inquiry_        → End User Inquiry
      applications__store_operations__login   → Applications --> Store Operations --> Login
      source_web_portal          → Web Portal
      zd_automated_resolution    → Automated Resolution
      power_outage/failure       → Power Outage/Failure
    """
    # Remove well-known prefixes
    for prefix in ("rootcause_", "zd_", "source_", "rc_"):
        if slug.lower().startswith(prefix):
            slug = slug[len(prefix):]
            break

    # Hierarchical issue_category: split on double underscore
    if "__" in slug:
        parts = [_slug_to_title(p) for p in slug.split("__") if p]
        return " --> ".join(parts)

    # Preserve " - " separators (Zendesk uses _-_ for grouping like "Vendor - Pinnacle")
    slug = re.sub(r"_-_+", " - ", slug)
    # Split remaining hyphens (e.g. "wired-payment_terminal" → "wired payment_terminal")
    slug = re.sub(r"(?<! )-(?! )", " ", slug)
    # Collapse underscores and extra whitespace to single space
    result = re.sub(r"[_\s]+", " ", slug).strip()

    def _fmt_word(w: str) -> str:
        return w.upper() if w.lower() in _UPPERCASE_CODES else w.capitalize()

    # Handle slash-separated parts (e.g. "outage/failure") — capitalize each side
    parts = result.split("/")
    formatted_parts = []
    for part in parts:
        formatted_parts.append(" ".join(_fmt_word(w) for w in part.split()))
    return "/".join(formatted_parts)


def _is_slug(val: str) -> bool:
    """Return True if the string looks like a Zendesk tagger slug (not free text)."""
    return (
        val == val.lower()
        and " " not in val
        and ("_" in val or "/" in val)
        and len(val) < 120  # ignore long free-text fields
    )


def _display_value(col: str, val) -> str:
    """Format a BQ cell value for human-readable display."""
    # Brand column: explicit mapping
    if col in ("brand",):
        if val is None or val == "":
            return "Unknown brand"
        if isinstance(val, str):
            return _BRAND_DISPLAY.get(val, val)

    if val is None:
        return "NULL"

    # Arrays (e.g. tags ARRAY<STRING>) — join as comma-separated human-readable list
    if isinstance(val, list):
        if not val:
            return ""
        return ", ".join(_slug_to_title(str(v)) if _is_slug(str(v)) else str(v) for v in val)

    # Auto-format Zendesk tagger slugs in known columns + any slug-looking value
    slug_columns = {
        "caused_by", "root_cause",
        "issue_category", "category",
        "product_type",
        "resolution_type",
        "source",
        "via_channel", "channel",
        "status",
    }
    if isinstance(val, str) and (col in slug_columns or _is_slug(val)):
        return _slug_to_title(val)

    return str(val)


def format_result_for_gemini(result: dict) -> str:
    """
    Convert query result to a compact text representation for Gemini context.
    Used to inject query results into the analytics chat prompt.
    """
    if result["row_count"] == 0:
        return "ACTUAL QUERY RESULT: 0 rows returned (no data matches the filter)."

    cols = result["columns"]
    rows = result["rows"]

    lines = [
        f"=== ACTUAL DATA FROM DATABASE ({result['row_count']} rows) ===",
        f"IMPORTANT: These are the REAL numbers. Report them exactly as shown.",
        " | ".join(cols),
        "-" * max(40, len(" | ".join(cols))),
    ]
    for row in rows[:200]:
        lines.append(" | ".join(_display_value(c, v) for c, v in zip(cols, row)))
    if result["row_count"] > 200:
        lines.append(f"... ({result['row_count'] - 200} more rows not shown)")
    lines.append("=== END OF DATA ===")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Semantic (vector) ticket search
# ---------------------------------------------------------------------------

def search_similar_tickets(
    query_embedding: list[float],
    exclude_ticket_id: Optional[int] = None,
    org_id: Optional[int] = None,
    store_id: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """
    Find semantically similar solved/closed tickets for a specific store.

    Fetches all embeddings for the organization from BigQuery,
    then computes cosine similarity in Python using numpy. This avoids the need
    for Vertex AI Agent Builder Enterprise edition (which would be required for
    metadata filtering on struct_data fields).

    Args:
        query_embedding: 3072-dim embedding of the search query (RETRIEVAL_QUERY task).
        exclude_ticket_id: Ticket ID to exclude from results (current ticket).
        org_id: Organization ID to restrict results to the same store. Required for
                same-store search. If None, returns empty (use search_tickets() instead).
        store_id: Unused — kept for API compatibility. Filtering is done by org_id.
        limit: Max results to return.

    Returns:
        List of dicts with keys: ticket_id, subject, org_name, brand, store_id,
        issue_category, resolution, status, solved_at, distance.
    """
    import numpy as np

    if org_id is None:
        # No org filter means this is a general search — caller should use search_tickets() instead.
        logger.warning("search_similar_tickets called without org_id — returning empty")
        return []

    client = _get_client()

    # Fetch all ticket IDs + embeddings for this org (solved/closed only)
    sql = f"""
    SELECT
        t.id AS ticket_id,
        t.subject,
        t.org_name,
        t.brand,
        t.store_id,
        t.issue_category,
        t.resolution,
        t.status,
        t.solved_at,
        te.embedding
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` t
    JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_embeddings` te ON t.id = te.ticket_id
    WHERE t.org_id = @org_id
      AND t.status IN ('solved', 'closed')
    """
    params = [bigquery.ScalarQueryParameter("org_id", "INT64", org_id)]
    if exclude_ticket_id is not None:
        sql += " AND t.id != @exclude_ticket_id"
        params.append(bigquery.ScalarQueryParameter("exclude_ticket_id", "INT64", exclude_ticket_id))

    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = list(client.query(sql, job_config=job_config).result())

    if not rows:
        return []

    # Compute cosine similarity in Python using numpy
    q = np.array(query_embedding, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm
    expected_dims = len(query_embedding)

    scored = []
    skipped_dims = 0
    for row in rows:
        emb = np.array(list(row.embedding), dtype=np.float32)
        if len(emb) != expected_dims:
            # Stale embedding from old model (different dims) — skip until corpus rebuilt
            skipped_dims += 1
            continue
        emb_norm = np.linalg.norm(emb)
        if emb_norm == 0:
            continue
        emb = emb / emb_norm
        similarity = float(np.dot(q, emb))
        scored.append((similarity, row))

    if skipped_dims:
        logger.warning(
            "search_similar_tickets: skipped %d embeddings with wrong dims (expected %d) — "
            "corpus rebuild needed (Step 9)",
            skipped_dims, expected_dims,
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    results = []
    for similarity, row in top:
        solved_at = row.solved_at
        if isinstance(solved_at, datetime):
            solved_str = solved_at.strftime("%Y-%m-%d")
        else:
            solved_str = str(solved_at) if solved_at else None

        results.append({
            "ticket_id": row.ticket_id,
            "distance": round(1.0 - similarity, 4),  # convert similarity → distance for compatibility
            "subject": row.subject or "",
            "org_name": row.org_name or "",
            "brand": _BRAND_DISPLAY.get(row.brand or "", row.brand or ""),
            "store_id": row.store_id or "",
            "issue_category": _slug_to_title(row.issue_category or ""),
            "resolution": (row.resolution or "")[:300],
            "status": row.status or "",
            "solved_at": solved_str,
        })

    return results


def search_tickets_keyword(
    query: str,
    exclude_ticket_id: Optional[int] = None,
    limit: int = 10,
) -> list[dict]:
    """
    Keyword fallback search for tickets with sparse content (short full_text).

    Used when Vertex AI semantic search returns few results because the ticket's
    own full_text is too short to produce a meaningful query embedding.

    Searches subject, issue_category, symptoms, resolution, and full_text
    using BQ LIKE / SEARCH across solved/closed tickets.

    Returns same dict format as search_similar_tickets().
    """
    client = _get_client()

    # Build individual keyword terms (split on spaces, filter short words)
    terms = [t.strip() for t in query.split() if len(t.strip()) >= 3]
    if not terms:
        return []

    # Build a LIKE condition for each term across key text fields
    like_clauses = []
    for term in terms[:6]:  # cap at 6 terms to avoid overly complex query
        escaped = term.replace("'", "''")
        like_clauses.append(
            f"(LOWER(subject) LIKE '%{escaped.lower()}%' OR "
            f"LOWER(issue_category) LIKE '%{escaped.lower()}%' OR "
            f"LOWER(symptoms) LIKE '%{escaped.lower()}%' OR "
            f"LOWER(full_text) LIKE '%{escaped.lower()}%')"
        )

    where_terms = " AND ".join(like_clauses)
    exclude_clause = f"AND id != {exclude_ticket_id}" if exclude_ticket_id is not None else ""

    sql = f"""
    SELECT
        id AS ticket_id,
        subject,
        org_name,
        brand,
        store_id,
        issue_category,
        resolution,
        status,
        solved_at
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE status IN ('solved', 'closed')
      AND ({where_terms})
      {exclude_clause}
    ORDER BY solved_at DESC
    LIMIT {limit}
    """

    rows = list(client.query(sql).result())
    results = []
    for row in rows:
        solved_at = row.solved_at
        if isinstance(solved_at, datetime):
            solved_str = solved_at.strftime("%Y-%m-%d")
        else:
            solved_str = str(solved_at) if solved_at else None

        results.append({
            "ticket_id": row.ticket_id,
            "distance": 0.5,  # neutral distance for keyword results
            "subject": row.subject or "",
            "org_name": row.org_name or "",
            "brand": _BRAND_DISPLAY.get(row.brand or "", row.brand or ""),
            "store_id": row.store_id or "",
            "issue_category": _slug_to_title(row.issue_category or ""),
            "resolution": (row.resolution or "")[:300],
            "status": row.status or "",
            "solved_at": solved_str,
        })

    return results


# ---------------------------------------------------------------------------
# Semantic contract ticket retrieval
# ---------------------------------------------------------------------------

_TICKET_SEMANTIC_STOP_WORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "check",
    "find", "for", "from", "in", "is", "it", "list", "log", "of", "or",
    "restaurant", "restaurants", "search", "store", "stores", "the", "there",
    "set", "site", "ticket", "tickets", "to", "was", "were", "where", "with",
    "line", "new",
}

_TICKET_SEMANTIC_ALIASES = {
    "technician": ["technician", "tech", "vendor technician", "contractor"],
    "tech": ["technician", "tech", "vendor technician", "contractor"],
    "dispatch": ["dispatch", "dispatched", "dispatching", "sent", "send", "scheduled", "onsite", "on site", "site visit"],
    "dispatched": ["dispatch", "dispatched", "dispatching", "sent", "send", "scheduled", "onsite", "on site", "site visit"],
    "sent": ["sent", "send", "dispatch", "dispatched", "scheduled", "onsite", "on site", "site visit"],
    "connect": ["connect", "connected", "connecting", "connection", "hooked", "install", "installed", "setup", "set up"],
    "connected": ["connect", "connected", "connecting", "connection", "hooked", "install", "installed", "setup", "set up"],
    "modem": ["modem"],
    "network": ["network", "internet", "wan", "connection", "connectivity"],
    "internet": ["internet", "network", "wan", "connectivity"],
    "speed": ["speed", "slow", "slowness", "lag", "latency", "bandwidth", "degraded"],
    "slow": ["slow", "slowness", "lag", "latency", "speed"],
    "connection": ["connection", "connectivity", "internet", "network", "wan"],
    "connectivity": ["connectivity", "connection", "internet", "network", "wan"],
    "interruption": ["interruption", "interruptions", "outage", "down", "drop", "dropping", "disconnect", "disconnected", "intermittent"],
    "interruptions": ["interruption", "interruptions", "outage", "down", "drop", "dropping", "disconnect", "disconnected", "intermittent"],
    "outage": ["outage", "down", "interruption", "interruptions", "offline"],
    "dropping": ["dropping", "drop", "disconnect", "disconnected", "intermittent"],
    "wifi": ["wifi", "wi fi", "wireless"],
    "wi": ["wifi", "wi fi", "wireless"],
    "wan": ["wan", "internet", "network", "connectivity"],
    "fiber": ["fiber", "fibre"],
    "fibre": ["fiber", "fibre"],
    "upgrade": ["upgrade", "upgraded", "upgrading", "cutover", "converted", "migration", "migrated"],
    "upgraded": ["upgrade", "upgraded", "upgrading", "cutover", "converted", "migration", "migrated"],
}


def search_tickets_semantic_contract(
    contract: dict[str, Any],
    *,
    limit: int = 20,
    candidate_limit: int = 500,
) -> dict[str, Any]:
    """
    Execute a Semantic Contract v2 ticket_retrieval contract.

    The query collects broad BigQuery candidates by contract concepts, then
    ranks locally by required concept coverage plus vendor/device/action hits.
    """
    if not isinstance(contract, dict):
        return {"rows": [], "summary": {"reason": "invalid_contract"}}

    filters = contract.get("filters") if isinstance(contract.get("filters"), dict) else {}
    required = _semantic_string_list(filters.get("required_concepts"))
    optional = _semantic_string_list(filters.get("optional_concepts"))
    vendors = _semantic_string_list(filters.get("vendors"))
    devices = _semantic_string_list(filters.get("devices"))
    actions = _semantic_string_list(filters.get("actions"))
    issue_or_category = str(filters.get("semantic_issue_or_category") or "").strip()
    if issue_or_category:
        optional.extend(_split_semantic_or_phrase(issue_or_category))

    recall_terms = _semantic_recall_terms([*required, *optional, *vendors, *devices, *actions])
    if not recall_terms:
        return {
            "rows": [],
            "summary": {
                "reason": "empty_recall_terms",
                "required_concepts": required,
                "optional_concepts": optional,
                "vendors": vendors,
                "devices": devices,
                "actions": actions,
            },
        }

    time_range = contract.get("time_range") if isinstance(contract.get("time_range"), dict) else {}
    start_date, end_date = _semantic_contract_date_bounds(time_range)
    store_ids = _semantic_store_id_list(filters.get("store_ids"))
    store_id = str(filters.get("store_id") or "").strip()
    if store_id and store_id not in store_ids:
        store_ids.insert(0, store_id)
    store_id = store_ids[0] if len(store_ids) == 1 else ""
    scope = str(contract.get("scope") or "").strip()

    client = _get_client()
    limit = max(1, min(int(limit), 50))
    candidate_limit = max(limit, min(int(candidate_limit), 900))

    where_parts = ["recall_hit_count > 0"]
    params: list[Any] = [
        bigquery.ArrayQueryParameter("recall_terms", "STRING", recall_terms),
        bigquery.ScalarQueryParameter("candidate_limit", "INT64", candidate_limit),
    ]
    if len(store_ids) > 1:
        where_parts.append("store_id IN UNNEST(@store_ids)")
        params.append(bigquery.ArrayQueryParameter("store_ids", "STRING", store_ids))
    elif store_id:
        where_parts.append("store_id = @store_id")
        params.append(bigquery.ScalarQueryParameter("store_id", "STRING", store_id))
    if start_date:
        where_parts.append("DATE(created_at, 'America/New_York') >= DATE(@start_date)")
        params.append(bigquery.ScalarQueryParameter("start_date", "STRING", start_date))
    if end_date:
        where_parts.append("DATE(created_at, 'America/New_York') < DATE(@end_date)")
        params.append(bigquery.ScalarQueryParameter("end_date", "STRING", end_date))

    where_sql = " AND ".join(where_parts)
    sql = f"""
    WITH searchable AS (
      SELECT
        id,
        subject,
        status,
        org_name,
        brand,
        store_id,
        issue_category,
        symptoms,
        resolution,
        root_cause_description,
        created_at,
        solved_at,
        LOWER(CONCAT(
          COALESCE(subject, ''), ' ',
          COALESCE(issue_category, ''), ' ',
          COALESCE(symptoms, ''), ' ',
          COALESCE(resolution, ''), ' ',
          COALESCE(root_cause_description, ''), ' ',
          COALESCE(full_text, '')
        )) AS search_text,
        SUBSTR(CONCAT(
          COALESCE(subject, ''), ' ',
          COALESCE(symptoms, ''), ' ',
          COALESCE(resolution, ''), ' ',
          COALESCE(root_cause_description, ''), ' ',
          COALESCE(full_text, '')
        ), 1, 18000) AS evidence_text
      FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    ),
    scored AS (
      SELECT
        *,
        (
          SELECT COUNT(DISTINCT term)
          FROM UNNEST(@recall_terms) AS term
          WHERE REGEXP_CONTAINS(search_text, CONCAT(r'\\b', term, r'\\b'))
        ) AS recall_hit_count
      FROM searchable
    )
    SELECT
      id,
      subject,
      status,
      org_name,
      brand,
      store_id,
      issue_category,
      symptoms,
      resolution,
      root_cause_description,
      created_at,
      solved_at,
      evidence_text,
      recall_hit_count
    FROM scored
    WHERE {where_sql}
    ORDER BY recall_hit_count DESC, created_at DESC
    LIMIT @candidate_limit
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=10 * 1024 ** 3,
        use_query_cache=True,
    )
    rows = list(client.query(sql, job_config=cfg).result(timeout=TIMEOUT_SEC))

    ranked: list[dict[str, Any]] = []
    for row in rows:
        evidence_text = str(getattr(row, "evidence_text", "") or "")
        searchable_text = _normalize_semantic_text(evidence_text)
        score, coverage = _score_semantic_ticket(
            searchable_text,
            required=required,
            optional=optional,
            vendors=vendors,
            devices=devices,
            actions=actions,
            recall_hit_count=int(getattr(row, "recall_hit_count", 0) or 0),
        )
        if score <= 0:
            continue
        created_at = getattr(row, "created_at", None)
        solved_at = getattr(row, "solved_at", None)
        match_type = _semantic_ticket_match_type(coverage)
        ranked.append({
            "id": int(getattr(row, "id", 0) or 0),
            "ticket_id": int(getattr(row, "id", 0) or 0),
            "subject": getattr(row, "subject", "") or "",
            "status": getattr(row, "status", "") or "",
            "org_name": getattr(row, "org_name", "") or "",
            "brand": _BRAND_DISPLAY.get(getattr(row, "brand", "") or "", getattr(row, "brand", "") or ""),
            "store_id": getattr(row, "store_id", "") or "",
            "issue_category": _slug_to_title(getattr(row, "issue_category", "") or ""),
            "symptoms": (getattr(row, "symptoms", "") or "")[:300],
            "resolution": (getattr(row, "resolution", "") or "")[:500],
            "root_cause_description": (getattr(row, "root_cause_description", "") or "")[:300],
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "semantic_match_type": match_type,
            "semantic_match_score": score,
            "matched_concepts": coverage["matched_required"],
            "matched_optional_concepts": coverage["matched_optional"],
            "matched_vendors": coverage["matched_vendors"],
            "matched_devices": coverage["matched_devices"],
            "matched_actions": coverage["matched_actions"],
            "missing_required_concepts": coverage["missing_required"],
            "missing_vendors": coverage["missing_vendors"],
            "missing_devices": coverage["missing_devices"],
            "missing_actions": coverage["missing_actions"],
            "evidence_snippet": _semantic_evidence_snippet(evidence_text, recall_terms),
        })

    ranked.sort(
        key=lambda item: (
            _semantic_match_order(str(item.get("semantic_match_type") or "")),
            -int(item.get("semantic_match_score") or 0),
            str(item.get("created_at") or ""),
        ),
        reverse=False,
    )
    result_set_ids = [int(row.get("ticket_id") or row.get("id") or 0) for row in ranked[:100]]
    result_set_ids = [ticket_id for ticket_id in result_set_ids if ticket_id > 0]
    displayed_ids = [int(row.get("ticket_id") or row.get("id") or 0) for row in ranked[:limit]]
    displayed_ids = [ticket_id for ticket_id in displayed_ids if ticket_id > 0]
    return {
        "rows": ranked[:limit],
        "result_set": {
            "ticket_ids": result_set_ids,
            "displayed_ticket_ids": displayed_ids,
            "requested_store_ids": store_ids,
            "criteria": [*required, *vendors, *devices, *actions],
            "source_family": "tickets",
            "row_count": len(ranked),
            "query_fingerprint": _semantic_contract_fingerprint(contract),
            "freshness": {
                "start_date": start_date,
                "end_date": end_date,
                "candidate_rows": len(rows),
                "ranked_rows": len(ranked),
            },
        },
        "summary": {
            "candidate_rows": len(rows),
            "ranked_rows": len(ranked),
            "strong_candidates": sum(1 for row in ranked if row.get("semantic_match_type") == "strong_candidate"),
            "related_candidates": sum(1 for row in ranked if row.get("semantic_match_type") != "strong_candidate"),
            "scope": scope or "global",
            "store_id": store_id or None,
            "store_ids": store_ids,
            "start_date": start_date,
            "end_date": end_date,
            "recall_terms": recall_terms,
            "required_concepts": required,
            "optional_concepts": optional,
            "vendors": vendors,
            "devices": devices,
            "actions": actions,
        },
        "bq_summary": (
            "DETERMINISTIC TICKET SEMANTIC SEARCH: "
            f"scope={scope or 'global'}, store_id={store_id or 'none'}, store_ids={store_ids or 'none'}, "
            f"date_range={start_date or 'any'}..{end_date or 'any'}, "
            f"required={required}, vendors={vendors}, devices={devices}, actions={actions}, "
            f"candidate_rows={len(rows)}, ranked_rows={len(ranked)}."
        ),
    }


def _semantic_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _semantic_contract_fingerprint(contract: dict[str, Any]) -> str:
    filters = contract.get("filters") if isinstance(contract.get("filters"), dict) else {}
    time_range = contract.get("time_range") if isinstance(contract.get("time_range"), dict) else {}
    stable = {
        "intent": contract.get("intent"),
        "executor": contract.get("executor"),
        "scope": contract.get("scope"),
        "output": contract.get("output"),
        "filters": filters,
        "time_range": time_range,
    }
    return hashlib.sha256(repr(stable).encode("utf-8")).hexdigest()


def _semantic_store_id_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.findall(r"\b\d{3,6}\b", str(value or ""))

    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if text and text.isdigit() and 3 <= len(text) <= 6 and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _split_semantic_or_phrase(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\bor\b|/", value, flags=re.IGNORECASE) if part.strip()]


def _semantic_contract_date_bounds(time_range: dict[str, Any]) -> tuple[str | None, str | None]:
    start = str(time_range.get("start_date") or "").strip()
    end = str(time_range.get("end_date_exclusive") or "").strip()
    if not start:
        start_at = str(time_range.get("start_at") or "").strip()
        start = start_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", start_at) else ""
    if not end:
        end_at = str(time_range.get("end_at") or "").strip()
        end = end_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", end_at) else ""
    if (not start or not end) and time_range.get("date_phrase"):
        try:
            from app.services import temporal_resolver

            period = temporal_resolver.resolve_period_text(str(time_range.get("date_phrase") or ""))
            if period:
                start = start or period.start_date.isoformat()
                end = end or period.end_date_exclusive.isoformat()
        except Exception as exc:
            logger.warning("ticket semantic search date phrase resolution failed: %s", exc)
    return (start or None, end or None)


def _semantic_recall_terms(concepts: list[str]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for concept in concepts:
        for group in _semantic_concept_groups(concept):
            for variant in group:
                for token in _semantic_tokens(variant):
                    if token in _TICKET_SEMANTIC_STOP_WORDS:
                        continue
                    if token not in seen:
                        seen.add(token)
                        terms.append(token)
                    if len(terms) >= 40:
                        return terms
    return terms


def _semantic_concept_groups(concept: str) -> list[list[str]]:
    tokens = _semantic_tokens(concept)
    groups: list[list[str]] = []
    for token in tokens:
        if token in _TICKET_SEMANTIC_STOP_WORDS:
            continue
        variants = _TICKET_SEMANTIC_ALIASES.get(token, [token])
        clean_variants = []
        seen: set[str] = set()
        for variant in variants:
            normalized = _normalize_semantic_text(variant)
            if normalized and normalized not in seen:
                seen.add(normalized)
                clean_variants.append(normalized)
        if clean_variants:
            groups.append(clean_variants)
    return groups


def _semantic_tokens(value: str) -> list[str]:
    normalized = _normalize_semantic_text(value)
    return [token for token in normalized.split() if len(token) >= 3]


def _normalize_semantic_text(value: str) -> str:
    text = str(value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _semantic_concept_matches(searchable_text: str, concept: str) -> bool:
    groups = _semantic_concept_groups(concept)
    if not groups:
        return False
    padded = f" {searchable_text} "
    return all(any(f" {variant} " in padded for variant in group) for group in groups)


def _semantic_matches(searchable_text: str, concepts: list[str]) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    for concept in concepts:
        if _semantic_concept_matches(searchable_text, concept):
            matched.append(concept)
        else:
            missing.append(concept)
    return matched, missing


def _score_semantic_ticket(
    searchable_text: str,
    *,
    required: list[str],
    optional: list[str],
    vendors: list[str],
    devices: list[str],
    actions: list[str],
    recall_hit_count: int,
) -> tuple[int, dict[str, list[str]]]:
    matched_required, missing_required = _semantic_matches(searchable_text, required)
    matched_optional, _missing_optional = _semantic_matches(searchable_text, optional)
    matched_vendors, missing_vendors = _semantic_matches(searchable_text, vendors)
    matched_devices, missing_devices = _semantic_matches(searchable_text, devices)
    matched_actions, missing_actions = _semantic_matches(searchable_text, actions)
    score = min(recall_hit_count, 12)
    score += len(matched_required) * 30
    score += len(matched_vendors) * 25
    score += len(matched_devices) * 15
    score += len(matched_actions) * 12
    score += len(matched_optional) * 5
    score -= len(missing_required) * 12
    score -= len(missing_vendors) * 10
    score -= len(missing_devices) * 6
    return (
        score,
        {
            "matched_required": matched_required,
            "missing_required": missing_required,
            "matched_optional": matched_optional,
            "matched_vendors": matched_vendors,
            "missing_vendors": missing_vendors,
            "matched_devices": matched_devices,
            "missing_devices": missing_devices,
            "matched_actions": matched_actions,
            "missing_actions": missing_actions,
        },
    )


def _semantic_ticket_match_type(coverage: dict[str, list[str]]) -> str:
    if (
        not coverage.get("missing_required")
        and not coverage.get("missing_vendors")
        and not coverage.get("missing_devices")
        and not coverage.get("missing_actions")
    ):
        return "strong_candidate"
    if coverage.get("matched_vendors") and not coverage.get("missing_vendors"):
        return "vendor_related"
    return "related_incomplete"


def _semantic_match_order(match_type: str) -> int:
    if match_type == "strong_candidate":
        return 0
    if match_type == "vendor_related":
        return 1
    return 2


def _semantic_evidence_snippet(text: str, recall_terms: list[str], limit: int = 420) -> str:
    compact = _clean_event_evidence_text(text)
    if not compact:
        return ""
    lower = compact.lower()
    for term in recall_terms:
        match = re.search(rf"\b{re.escape(term)}\b", lower)
        if not match:
            continue
        start = max(0, match.start() - 80)
        end = min(len(compact), match.end() + 300)
        if start > 0:
            first_space = compact.find(" ", start, match.start())
            if first_space != -1:
                start = first_space + 1
        return compact[start:end].strip()[:limit]
    return compact[:limit]


_EVENT_STOP_WORDS = {
    "a", "an", "and", "any", "are", "at", "been", "case", "cases",
    "check", "did", "do", "does", "for", "had", "has", "have", "in",
    "incident", "incidents", "last", "of", "our", "over", "recorded",
    "restaurant", "restaurants", "store", "stores", "the", "there", "to",
    "two", "were", "where", "which", "with", "year", "years",
    "был", "была", "были", "где", "года", "есть", "каком", "лет", "любом",
    "любой", "нас", "пару", "последние", "посление", "проверь", "рестаранов",
    "ресторане", "ресторанов", "случаи", "зафиксированные", "зафикисиоованные",
}

_EVENT_ALIASES = {
    "пожар": "fire",
    "пожара": "fire",
    "пожаре": "fire",
    "пожаром": "fire",
    "пожары": "fire",
    "пожаров": "fire",
    "поджаров": "fire",
}

_FIRE_ACTUAL_RE = re.compile(
    r"\b(?:"
    r"restaurant\s+fire|store\s+fire|building\s+fire|fire\s+incident|"
    r"closed\s+due\s+to\s+(?:the\s+)?fire|clos(?:e|ed|ing)\s+(?:.{0,80}\s+)?due\s+to\s+(?:the\s+)?fire|"
    r"after\s+(?:the\s+)?fire|fire\s+above\s+the\s+office|"
    r"fire\s+in\s+(?:the\s+)?restaurant|fire\s+breaks?\s+out|"
    r"arson\s+suspected|fire\s+damage|forest\s+fire\s+damage|"
    r"opens?\s+after\s+fire|until\s+store\s+opens\s+after\s+fire|"
    r"turn\s+off\s+ecomm\s+.*store\s+fire"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)
_FIRE_RELATED_RE = re.compile(
    r"\b(?:fire\s+alarm|fire\s+panel|fire\s+department|fire\s+dept|"
    r"firefighter|fire\s+drill|911\s+calls?|security\s+.*fire)\b",
    re.IGNORECASE | re.DOTALL,
)
_FIRE_NOISE_RE = re.compile(
    r"\b(?:firewall|fire\s+wall|fire\s+orders?|fire\s+items?|"
    r"fire\s+timings?|firing\s+to|kds\s+firing|kds\s+fire|"
    r"fire\s+to\s+kds|did\s+not\s+fire\s+to|not\s+fire\s+to|"
    r"fire\s+to\s+pos|fire\s+to\s+pizza|fire\s+at\s+both|"
    r"items?\s+fire\s+to|fire\s+quicker|fire\s+immediately|"
    r"driver\s+.*fire|order\s+.*fire|smoke\s+did\s+not\s+fire)\b",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_event_terms(event_terms: list[str] | tuple[str, ...]) -> list[str]:
    """Normalize user event words into bounded BQ search terms."""
    out: list[str] = []
    seen: set[str] = set()
    for value in event_terms or []:
        text = str(value or "").lower()
        for source, target in _EVENT_ALIASES.items():
            text = re.sub(rf"\b{re.escape(source)}\b", target, text)
        for token in re.findall(r"[a-z][a-z0-9_-]{2,}|[а-яё]{3,}", text, flags=re.IGNORECASE):
            token = _EVENT_ALIASES.get(token, token)
            if token in _EVENT_STOP_WORDS:
                continue
            if token.endswith("s") and len(token) > 4:
                token = token[:-1]
            if token in _EVENT_STOP_WORDS or token in seen:
                continue
            seen.add(token)
            out.append(token)
            if len(out) >= 5:
                return out
    return out


def _event_snippet(text: str, event_terms: list[str], limit: int = 360) -> str:
    compact = _clean_event_evidence_text(text)
    if not compact:
        return ""
    patterns = [re.escape(term) for term in event_terms if term]
    if patterns:
        match = re.search(rf"\b(?:{'|'.join(patterns)})\b", compact, flags=re.IGNORECASE)
        if match:
            start = max(0, match.start() - 50)
            end = min(len(compact), match.end() + 260)
            if start > 0:
                word_start = compact.find(" ", start, match.start())
                if word_start != -1:
                    start = word_start + 1
            return compact[start:end].strip()[:limit]
    return compact[:limit]


def _clean_event_evidence_text(text: str) -> str:
    compact = str(text or "")
    compact = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", compact)
    compact = re.sub(r"\[[^\]]{0,80}\]\(https?://[^)]+\)", " ", compact)
    compact = re.sub(r"https?://\S+", " ", compact)
    compact = re.sub(r"(^|\s)#{1,6}\s*", " ", compact)
    compact = re.sub(r"\*\*|__|`", "", compact)
    compact = re.sub(r"\bInbound Call Details:\s*---.*", " ", compact, flags=re.IGNORECASE | re.DOTALL)
    compact = re.sub(r"\s+", " ", compact)
    return compact.strip()


def _classify_event_match(text: str, event_terms: list[str]) -> tuple[str, str]:
    """Classify ticket evidence for event discovery without treating keyword hits as proof."""
    lower = (text or "").lower()
    if "fire" in event_terms:
        actual = bool(_FIRE_ACTUAL_RE.search(lower))
        related = bool(_FIRE_RELATED_RE.search(lower))
        noise = bool(_FIRE_NOISE_RE.search(lower))
        if actual:
            return "confirmed_candidate", "ticket text explicitly describes a store/restaurant fire or fire-related closure/damage"
        if related:
            return "related_non_confirming", "ticket mentions fire-related systems/calls but does not confirm a restaurant fire"
        if noise:
            return "noise", "keyword is used for KDS/menu timing, firewall, or another non-incident meaning"
    return "keyword_candidate", "ticket matched the requested event terms; manual review may be needed"


def _event_match_score(text: str, match_type: str) -> int:
    lower = (text or "").lower()
    if match_type == "confirmed_candidate":
        if re.search(
            r"\b(?:restaurant\s+fire|store\s+fire|building\s+fire|"
            r"closed\s+due\s+to\s+(?:a\s+)?fire|store\s+is\s+closed\s+due\s+to\s+fire|"
            r"due\s+to\s+fire\s+store\s+is\s+closed|fire\s+above\s+the\s+office|"
            r"fire\s+in\s+(?:the\s+)?restaurant|fire\s+breaks?\s+out)\b",
            lower,
        ):
            return 100
        if re.search(r"\b(?:fire\s+incident|fire\s+damage|forest\s+fire\s+damage)\b", lower):
            return 90
        if re.search(r"\b(?:after\s+(?:the\s+)?fire|fire\s+recovery|fire\s+reno)\b", lower):
            return 75
        return 70
    if match_type == "related_non_confirming":
        return 40
    if match_type == "keyword_candidate":
        return 30
    return 0


def discover_global_event_tickets(
    event_terms: list[str] | tuple[str, ...],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Retrieve and classify global event candidates from ticket text.

    This is the deterministic executor for questions like "which restaurant had
    event X?" It uses the semantic layer's event terms, searches evidence-rich
    ticket text, then separates likely incident candidates from related/noisy
    keyword uses so final answers do not equate every LIKE hit with an incident.
    """
    clean_terms = _normalize_event_terms(list(event_terms or []))
    if not clean_terms:
        return []

    client = _get_client()
    limit = max(1, min(int(limit), 700))
    regex = r"\b(?:" + "|".join(re.escape(term) for term in clean_terms) + r")\b"

    where_parts = ["REGEXP_CONTAINS(search_text, @term_regex)"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("term_regex", "STRING", regex),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if start_date:
        where_parts.append("DATE(created_at, 'America/New_York') >= DATE(@start_date)")
        params.append(bigquery.ScalarQueryParameter("start_date", "STRING", str(start_date)))
    if end_date:
        where_parts.append("DATE(created_at, 'America/New_York') < DATE(@end_date)")
        params.append(bigquery.ScalarQueryParameter("end_date", "STRING", str(end_date)))

    where_sql = " AND ".join(where_parts)
    sql = f"""
    WITH searchable AS (
      SELECT
        id,
        subject,
        status,
        org_name,
        brand,
        store_id,
        issue_category,
        resolution,
        symptoms,
        created_at,
        solved_at,
        LOWER(CONCAT(
          COALESCE(subject, ''), ' ',
          COALESCE(issue_category, ''), ' ',
          COALESCE(symptoms, ''), ' ',
          COALESCE(resolution, ''), ' ',
          COALESCE(root_cause_description, ''), ' ',
          COALESCE(full_text, '')
        )) AS search_text,
        SUBSTR(CONCAT(
          COALESCE(subject, ''), ' ',
          COALESCE(symptoms, ''), ' ',
          COALESCE(resolution, ''), ' ',
          COALESCE(root_cause_description, ''), ' ',
          COALESCE(full_text, '')
        ), 1, 16000) AS evidence_text
      FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    )
    SELECT
      id,
      subject,
      status,
      org_name,
      brand,
      store_id,
      issue_category,
      resolution,
      symptoms,
      created_at,
      solved_at,
      evidence_text
    FROM searchable
    WHERE {where_sql}
    ORDER BY created_at DESC
    LIMIT @limit
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=10 * 1024 ** 3,
        use_query_cache=True,
    )
    rows = list(client.query(sql, job_config=cfg).result(timeout=TIMEOUT_SEC))

    order = {
        "confirmed_candidate": 0,
        "keyword_candidate": 1,
        "related_non_confirming": 2,
        "noise": 3,
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        text = getattr(row, "evidence_text", "") or ""
        match_type, reason = _classify_event_match(text, clean_terms)
        match_score = _event_match_score(text, match_type)
        created_at = getattr(row, "created_at", None)
        solved_at = getattr(row, "solved_at", None)
        out.append({
            "id": int(getattr(row, "id", 0) or 0),
            "subject": getattr(row, "subject", "") or "",
            "status": getattr(row, "status", "") or "",
            "org_name": getattr(row, "org_name", "") or "",
            "brand": _BRAND_DISPLAY.get(getattr(row, "brand", "") or "", getattr(row, "brand", "") or ""),
            "store_id": getattr(row, "store_id", "") or "",
            "issue_category": _slug_to_title(getattr(row, "issue_category", "") or ""),
            "resolution": (getattr(row, "resolution", "") or "")[:300],
            "symptoms": (getattr(row, "symptoms", "") or "")[:300],
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "event_terms": clean_terms,
            "event_match_type": match_type,
            "event_match_reason": reason,
            "event_match_score": match_score,
            "evidence_snippet": _event_snippet(text, clean_terms),
        })
    out.sort(
        key=lambda item: (
            order.get(str(item.get("event_match_type")), 9),
            str(item.get("created_at") or ""),
        ),
        reverse=False,
    )
    confirmed = [row for row in out if row.get("event_match_type") == "confirmed_candidate"]
    other = [row for row in out if row.get("event_match_type") != "confirmed_candidate"]
    confirmed.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    other.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return confirmed + other


# ---------------------------------------------------------------------------
# OCR context for ticket chat
# ---------------------------------------------------------------------------

_OCR_TABLE_SCHEMA = [
    bigquery.SchemaField("content_url",       "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("ticket_id",         "INT64"),
    bigquery.SchemaField("ocr_text",          "STRING"),
    bigquery.SchemaField("processing_status", "STRING"),
    bigquery.SchemaField("skip_reason",       "STRING"),
    bigquery.SchemaField("processed_at",      "TIMESTAMP"),
]


def ensure_ocr_table() -> None:
    """Create ticket_attachment_ocr if it does not exist. Idempotent."""
    client    = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr"
    try:
        client.get_table(table_ref)
    except Exception:
        table = bigquery.Table(table_ref, schema=_OCR_TABLE_SCHEMA)
        client.create_table(table)
        logger.info("Created table ticket_attachment_ocr")


def fetch_ticket_images_by_comment(ticket_id: int) -> dict[int | None, list[dict]]:
    """
    Return images for a ticket grouped by comment_id.

    Used to interleave screenshots with comments in ticket context (chronological order).
    Images WITH comment_id appear right after their parent comment.
    Images WITHOUT comment_id (key=None) are inserted by created_at among comments.

    Returns:
        {comment_id: [{"filename": str, "ocr_text": str|None, "created_at": datetime}]}
        Key None = images not attached to a specific comment.
    """
    client = _get_client()
    sql = f"""
    SELECT
        ta.comment_id,
        ta.file_name,
        ta.content_url,
        ta.created_at,
        ocr.ocr_text
    FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachments` ta
    LEFT JOIN `{GCP_PROJECT}.{BQ_DATASET}.ticket_attachment_ocr` ocr
        ON ta.content_url = ocr.content_url
    WHERE ta.ticket_id = @ticket_id
      AND ta.is_image = TRUE
      AND (ocr.processing_status IS NULL OR ocr.processing_status = 'success')
    ORDER BY ta.created_at
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("ticket_id", "INT64", ticket_id)]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
        result: dict[int | None, list[dict]] = {}
        for row in rows:
            key = row.comment_id  # int or None
            if key not in result:
                result[key] = []
            result[key].append({
                "filename": row.file_name,
                "ocr_text": row.ocr_text,
                "created_at": row.created_at,
            })
        return result
    except Exception as exc:
        logger.warning("fetch_ticket_images_by_comment failed for ticket %d: %s", ticket_id, exc)
        return {}


# ---------------------------------------------------------------------------
# BigQuery analytics helper for the aggregate-data source class
# ---------------------------------------------------------------------------

_BQ_ANALYTICS_SQL_PROMPT = """You are a SQL generator for a BigQuery ticket analytics database.

Given the engineer's question and the current ticket context, write a SELECT query.

CURRENT TICKET CONTEXT:
{ticket_context}

{schema}

Rules:
- Always use fully-qualified table names: `esip-reference-project.esip_reference.tickets`
- Standard SQL only. SELECT only. No DDL.
- The tickets table DOES include `store_id`. Never claim store_id is unavailable.
- If the question mentions a specific store (e.g. "store 2011"), include `WHERE store_id = '2011'`
  unless the user explicitly asks to compare multiple stores.
- `store_id` is a STRING. For store-id ranges/prefixes, compare strings with quoted literals:
  `store_id BETWEEN '2000' AND '2999'`, `store_id >= '2000'`, `STARTS_WITH(store_id, '20')`.
  Do not use unquoted numeric store_id range values.
- Use the ticket context to understand what "this issue", "this type", "this kind" refers to.
  Match by issue_category, subject keywords, or symptoms from the ticket — whichever is most specific.
- For topical searches: use broad OR across subject, issue_category, full_text
- For assigned group / assignment queue / vendor queue questions, use assignee_group_name.
  Do not use org_name for assigned groups; org_name is the store organization.
  Prefer case-insensitive equality such as `LOWER(assignee_group_name) = 'field_services_partner'`.
- For counts/trends: use GROUP BY + ORDER BY + LIMIT
- Default LIMIT: 20 rows unless the question implies a summary (then 50 is fine)
- For time-based questions without explicit period: use last 365 days
- Status values are new/open/pending/hold/solved/closed. If the user says "on-hold", use 'hold'.
- If the user asks for "current" or "active" tickets without explicitly naming statuses, use
  `status IN ('open', 'pending', 'hold')`. Do not include solved/closed, and do not include
  `new` unless the user explicitly asks for new tickets/status.
- For public/internal comment analytics, use the `ticket_comments` table.
  Public comments/replies are `ticket_comments.is_internal = FALSE`; internal notes are
  `ticket_comments.is_internal = TRUE`. Never use `is_public` because that column does not exist.
- For public agent replies, filter `ticket_comments.is_internal = FALSE AND author_is_agent = TRUE`.
  For customer/end-user public comments, filter `ticket_comments.is_internal = FALSE AND author_role = 'end-user'`.
  These author-role columns may be NULL for historical rows until the comment-author backfill has run.
- For "tickets with no public update/reply/comment in the last N hours", compute
  `MAX(ticket_comments.created_at)` where `is_internal = FALSE` per ticket, LEFT JOIN it to
  `tickets`, and filter tickets where that timestamp is NULL or older than the requested window.
- TIMESTAMP_SUB RULE — CRITICAL: TIMESTAMP_SUB does NOT support MONTH or YEAR intervals.
  Always use DAY: 1 month = 30 DAY, 3 months = 90 DAY, 6 months = 180 DAY, 1 year = 365 DAY.
  CORRECT: created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
  WRONG:   created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 MONTH)
- When grouping by issue_category or any category field: always add WHERE issue_category IS NOT NULL
  to prevent NULL appearing as the top result
- For "top issues by store" requests, if many tickets have missing issue_category, infer issue_topic
  with fallback to subject text instead of returning NULL-only categories:
    CASE
      WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN issue_category
      ELSE CONCAT('__subject__', TRIM(subject))
    END AS issue_topic
  Then GROUP BY issue_topic.
- BRAND vs DEVICE/PRODUCT NAMES: The `brand` column contains TENANT BRANDS only
  (Metro Burger, Cedar Grill, Frontier Kitchen, Lakeside Tavern, City Fries, etc.) — NOT
  device manufacturers, product names, or technology vendors.
  For device/product/vendor queries (network, Zebra, Cisco, Meraki, Oracle, Epson, etc.) ALWAYS use:
    LOWER(full_text) LIKE '%nokia%' OR LOWER(subject) LIKE '%nokia%'
  NEVER use brand = 'nokia' or WHERE brand LIKE '%nokia%' for device/product names.

ENGINEER'S QUESTION: {question}

Respond ONLY with valid JSON:
{{"sql": "SELECT ..."}}"""


def _store_created_date_scope(
    lookback_days: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
    """Return a parameterized created_at scope for store-history helper queries."""
    params: list[bigquery.ScalarQueryParameter] = []
    clauses: list[str] = []
    if start_date:
        clauses.append("DATE(created_at, 'America/New_York') >= DATE(@start_date)")
        params.append(bigquery.ScalarQueryParameter("start_date", "STRING", str(start_date)))
    if end_date:
        clauses.append("DATE(created_at, 'America/New_York') < DATE(@end_date)")
        params.append(bigquery.ScalarQueryParameter("end_date", "STRING", str(end_date)))
    if clauses:
        return " AND ".join(clauses), params
    params.append(bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days))
    return "created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)", params


def infer_store_issue_breakdown_from_content(
    store_id: str,
    lookback_days: int = 90,
    limit: int = 8,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Build a store-level "top issues" breakdown even when issue_category is mostly missing.

    Strategy:
    - Use issue_category when present.
    - Fallback to subject-based themes when issue_category is NULL/empty.
    - Return compact rows suitable for universal chat context injection.
    """
    client = _get_client()
    lookback_days = max(1, min(int(lookback_days), 3650))
    limit = max(1, min(int(limit), 30))
    date_where, date_params = _store_created_date_scope(lookback_days, start_date, end_date)

    summary_sql = f"""
    SELECT
      COUNT(*) AS total_tickets,
      COUNTIF(issue_category IS NULL OR TRIM(issue_category) = '') AS missing_issue_category,
      COUNTIF(NOT (issue_category IS NULL OR TRIM(issue_category) = '')) AS with_issue_category
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE store_id = @store_id
      AND {date_where}
    """
    summary_cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("store_id", "STRING", store_id),
            *date_params,
        ],
    )
    summary_rows = list(client.query(summary_sql, job_config=summary_cfg).result())
    if not summary_rows:
        return {"summary": {"total_tickets": 0, "missing_issue_category": 0, "with_issue_category": 0}, "rows": []}

    srow = summary_rows[0]
    summary = {
        "total_tickets": int(getattr(srow, "total_tickets", 0) or 0),
        "missing_issue_category": int(getattr(srow, "missing_issue_category", 0) or 0),
        "with_issue_category": int(getattr(srow, "with_issue_category", 0) or 0),
    }

    topics_sql = f"""
    WITH scoped AS (
      SELECT id, subject, issue_category, created_at
      FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
      WHERE store_id = @store_id
        AND {date_where}
    )
    SELECT
      CASE
        WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN 'issue_category'
        WHEN subject IS NOT NULL AND TRIM(subject) != '' THEN 'subject_theme'
        ELSE 'uncategorized'
      END AS topic_source,
      CASE
        WHEN issue_category IS NOT NULL AND TRIM(issue_category) != '' THEN issue_category
        WHEN subject IS NOT NULL AND TRIM(subject) != '' THEN TRIM(subject)
        ELSE '(no subject)'
      END AS topic_value,
      COUNT(*) AS ticket_count,
      STRING_AGG(
        CONCAT('#', CAST(id AS STRING), ': ', COALESCE(TRIM(subject), '(no subject)')),
        '; ' ORDER BY created_at DESC LIMIT 3
      ) AS sample_tickets
    FROM scoped
    GROUP BY topic_source, topic_value
    ORDER BY ticket_count DESC, topic_value
    LIMIT @limit
    """
    topics_cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("store_id", "STRING", store_id),
            *date_params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    topic_rows = list(client.query(topics_sql, job_config=topics_cfg).result())

    rows: list[dict[str, Any]] = []
    for row in topic_rows:
        source = str(getattr(row, "topic_source", "") or "")
        raw_value = str(getattr(row, "topic_value", "") or "")
        if source == "issue_category":
            topic = _slug_to_title(raw_value)
            source_label = "issue_category"
        elif source == "subject_theme":
            topic = raw_value
            source_label = "subject/content inference"
        else:
            topic = "Uncategorized (no issue_category and no subject)"
            source_label = "subject/content inference"

        rows.append({
            "topic": topic,
            "ticket_count": int(getattr(row, "ticket_count", 0) or 0),
            "source": source_label,
            "sample_tickets": str(getattr(row, "sample_tickets", "") or ""),
        })

    return {"summary": summary, "rows": rows}


def fetch_store_ticket_examples(
    store_id: str,
    lookback_days: int = 365,
    terms: list[str] | None = None,
    limit: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch store-scoped ticket examples for broad explicit-ticket analysis.

    This is a deterministic safety path for Universal Chat when model-generated
    analytics SQL fails. It prevents false "no same-store history" answers when
    the store id is already known from the ticket context.
    """
    client = _get_client()
    lookback_days = max(1, min(int(lookback_days), 3650))
    limit = max(1, min(int(limit), 50))
    date_where, date_params = _store_created_date_scope(lookback_days, start_date, end_date)
    clean_terms = [
        t.strip().lower()
        for t in (terms or [])
        if isinstance(t, str) and t.strip()
    ][:12]

    sql = f"""
    SELECT
      id,
      subject,
      status,
      org_name,
      brand,
      store_id,
      issue_category,
      created_at,
      solved_at,
      resolution
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE store_id = @store_id
      AND {date_where}
      AND (
        ARRAY_LENGTH(@terms) = 0
        OR EXISTS (
          SELECT 1
          FROM UNNEST(@terms) AS term
          WHERE LOWER(CONCAT(
            COALESCE(subject, ''), ' ',
            COALESCE(issue_category, ''), ' ',
            COALESCE(symptoms, ''), ' ',
            COALESCE(resolution, ''), ' ',
            COALESCE(full_text, '')
          )) LIKE CONCAT('%', term, '%')
        )
      )
    ORDER BY created_at DESC
    LIMIT @limit
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("store_id", "STRING", str(store_id)),
            *date_params,
            bigquery.ArrayQueryParameter("terms", "STRING", clean_terms),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    out: list[dict[str, Any]] = []
    for row in rows:
        created_at = getattr(row, "created_at", None)
        solved_at = getattr(row, "solved_at", None)
        out.append({
            "id": int(getattr(row, "id", 0) or 0),
            "subject": getattr(row, "subject", "") or "",
            "status": getattr(row, "status", "") or "",
            "org_name": getattr(row, "org_name", "") or "",
            "brand": _BRAND_DISPLAY.get(getattr(row, "brand", "") or "", getattr(row, "brand", "") or ""),
            "store_id": getattr(row, "store_id", "") or "",
            "issue_category": _slug_to_title(getattr(row, "issue_category", "") or ""),
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "resolution": (getattr(row, "resolution", "") or "")[:300],
        })
    return out


def fetch_store_tickets_for_semantic_label_count(
    store_id: str,
    lookback_days: int = 365,
    limit: int = 500,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch store-scoped ticket text for deterministic semantic label counts.

    This supports follow-ups like "count each listed complaint type" where the
    labels are descriptive phrases from a prior answer, not Zendesk categories.
    Comments are included because complaint wording often lives there rather
    than in the ticket subject or issue_category.
    """
    client = _get_client()
    lookback_days = max(1, min(int(lookback_days), 3650))
    limit = max(1, min(int(limit), 1000))
    date_where, date_params = _store_created_date_scope(lookback_days, start_date, end_date)

    sql = f"""
    WITH comment_text AS (
      SELECT
        ticket_id,
        STRING_AGG(SUBSTR(COALESCE(body, ''), 1, 4000), ' ' ORDER BY created_at LIMIT 40) AS comments_text
      FROM `{GCP_PROJECT}.{BQ_DATASET}.ticket_comments`
      GROUP BY ticket_id
    )
    SELECT
      t.id,
      t.subject,
      t.status,
      t.org_name,
      t.brand,
      t.store_id,
      t.issue_category,
      t.symptoms,
      t.resolution,
      t.created_at,
      t.solved_at,
      SUBSTR(COALESCE(t.full_text, ''), 1, 8000) AS full_text,
      SUBSTR(COALESCE(c.comments_text, ''), 1, 20000) AS comments_text
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets` t
    LEFT JOIN comment_text c
      ON c.ticket_id = t.id
    WHERE t.store_id = @store_id
      AND {date_where}
    ORDER BY t.created_at DESC
    LIMIT @limit
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("store_id", "STRING", str(store_id)),
            *date_params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    out: list[dict[str, Any]] = []
    for row in rows:
        created_at = getattr(row, "created_at", None)
        solved_at = getattr(row, "solved_at", None)
        out.append({
            "id": int(getattr(row, "id", 0) or 0),
            "subject": getattr(row, "subject", "") or "",
            "status": getattr(row, "status", "") or "",
            "org_name": getattr(row, "org_name", "") or "",
            "brand": _BRAND_DISPLAY.get(getattr(row, "brand", "") or "", getattr(row, "brand", "") or ""),
            "store_id": getattr(row, "store_id", "") or "",
            "issue_category": _slug_to_title(getattr(row, "issue_category", "") or ""),
            "symptoms": getattr(row, "symptoms", "") or "",
            "resolution": getattr(row, "resolution", "") or "",
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "full_text": getattr(row, "full_text", "") or "",
            "comments_text": getattr(row, "comments_text", "") or "",
        })
    return out


def fetch_store_history_snapshot(
    store_id: str,
    lookback_days: int = 1200,
    limit: int = 80,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch a bounded, keyword-free ticket history snapshot for one store.

    This supports model-led contextual analysis without hardcoding event words.
    The model receives scoped rows and can interpret incidents, before/after
    windows, and issue themes from the actual ticket subjects/categories/text.
    """
    client = _get_client()
    lookback_days = max(1, min(int(lookback_days), 3650))
    limit = max(1, min(int(limit), 200))
    date_where, date_params = _store_created_date_scope(lookback_days, start_date, end_date)
    sql = f"""
    SELECT
      id,
      subject,
      status,
      org_name,
      brand,
      store_id,
      issue_category,
      created_at,
      solved_at,
      resolution
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE store_id = @store_id
      AND {date_where}
    ORDER BY created_at DESC
    LIMIT @limit
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("store_id", "STRING", str(store_id)),
            *date_params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    out: list[dict[str, Any]] = []
    for row in rows:
        created_at = getattr(row, "created_at", None)
        solved_at = getattr(row, "solved_at", None)
        out.append({
            "id": int(getattr(row, "id", 0) or 0),
            "subject": getattr(row, "subject", "") or "",
            "status": getattr(row, "status", "") or "",
            "org_name": getattr(row, "org_name", "") or "",
            "brand": _BRAND_DISPLAY.get(getattr(row, "brand", "") or "", getattr(row, "brand", "") or ""),
            "store_id": getattr(row, "store_id", "") or "",
            "issue_category": _slug_to_title(getattr(row, "issue_category", "") or ""),
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "resolution": (getattr(row, "resolution", "") or "")[:300],
        })
    return out


def _fix_timestamp_intervals(sql: str) -> str:
    """
    Post-process generated SQL to replace unsupported TIMESTAMP_SUB intervals.
    BigQuery TIMESTAMP_SUB does NOT support MONTH or YEAR — only DAY and smaller.
    Gemini frequently generates INTERVAL N MONTH/YEAR despite prompt warnings.
    """
    sql = re.sub(
        r'INTERVAL\s+(\d+)\s+MONTH\b',
        lambda m: f'INTERVAL {int(m.group(1)) * 30} DAY',
        sql, flags=re.IGNORECASE,
    )
    sql = re.sub(
        r'INTERVAL\s+(\d+)\s+YEAR\b',
        lambda m: f'INTERVAL {int(m.group(1)) * 365} DAY',
        sql, flags=re.IGNORECASE,
    )
    return sql


def fetch_ticket_details_for_cards(ticket_ids: list[int]) -> list[dict]:
    """Fetch compact ticket card metadata from BQ while preserving input order."""
    if not ticket_ids:
        return []

    client = _get_client()
    ids = [int(ticket_id) for ticket_id in ticket_ids]
    ids_str = ", ".join(str(ticket_id) for ticket_id in ids)
    sql = f"""
    SELECT
      id,
      subject,
      org_name,
      brand,
      store_id,
      issue_category,
      resolution,
      status,
      created_at,
      solved_at,
      LENGTH(COALESCE(full_text, '')) AS full_text_len
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id IN ({ids_str})
    """
    rows = list(client.query(sql).result())
    row_map = {int(row.id): row for row in rows}

    results = []
    for ticket_id in ids:
        row = row_map.get(ticket_id)
        if row is None:
            continue
        created_at = row.created_at
        if isinstance(created_at, datetime):
            created_str = created_at.strftime("%Y-%m-%d")
        else:
            created_str = str(created_at) if created_at else None
        solved_at = row.solved_at
        if isinstance(solved_at, datetime):
            solved_str = solved_at.strftime("%Y-%m-%d")
        else:
            solved_str = str(solved_at) if solved_at else None
        results.append({
            "ticket_id": row.id,
            "distance": 0.0,
            "subject": row.subject or "",
            "org_name": row.org_name or "",
            "brand": _BRAND_DISPLAY.get(row.brand or "", row.brand or ""),
            "store_id": row.store_id or "",
            "issue_category": _slug_to_title(row.issue_category or ""),
            "resolution": (row.resolution or "")[:300],
            "status": row.status or "",
            "created_at": created_str,
            "solved_at": solved_str,
            "full_text_len": int(row.full_text_len or 0),
        })
    return results


def bulk_lookup_ticket_ids(ticket_ids: list[int], *, cap: int = 75) -> dict:
    """Bounded parameterized lookup for explicit ticket IDs.

    Raises provider/query exceptions to the caller; callers must not interpret
    those failures as proof that tickets do not exist.
    """
    if not ticket_ids:
        return {"requested_ids": [], "found_ids": [], "missing_ids": [], "rows": [], "cap": cap, "truncated": False}

    cap = max(1, min(int(cap or 75), 75))
    requested: list[int] = []
    seen: set[int] = set()
    for raw_id in ticket_ids:
        try:
            ticket_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if ticket_id <= 0 or ticket_id in seen:
            continue
        seen.add(ticket_id)
        requested.append(ticket_id)
    truncated = len(requested) > cap
    bounded = requested[:cap]
    if not bounded:
        return {"requested_ids": [], "found_ids": [], "missing_ids": [], "rows": [], "cap": cap, "truncated": truncated}

    client = _get_client()
    sql = f"""
    SELECT
      id,
      subject,
      status,
      org_name,
      brand,
      store_id,
      issue_category,
      created_at,
      solved_at,
      resolution,
      SUBSTR(CONCAT(
        COALESCE(subject, ''), ' ',
        COALESCE(symptoms, ''), ' ',
        COALESCE(resolution, ''), ' ',
        COALESCE(root_cause_description, '')
      ), 1, 500) AS evidence_snippet,
      LENGTH(COALESCE(full_text, '')) AS full_text_len
    FROM `{GCP_PROJECT}.{BQ_DATASET}.tickets`
    WHERE id IN UNNEST(@ticket_ids)
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ticket_ids", "INT64", bounded)],
        maximum_bytes_billed=2 * 1024 ** 3,
        use_query_cache=True,
    )
    rows = list(client.query(sql, job_config=cfg).result(timeout=TIMEOUT_SEC))
    row_map = {int(row.id): row for row in rows}
    out_rows: list[dict] = []
    for ticket_id in bounded:
        row = row_map.get(ticket_id)
        if row is None:
            continue
        created_at = row.created_at
        solved_at = row.solved_at
        out_rows.append({
            "ticket_id": int(row.id),
            "id": int(row.id),
            "subject": row.subject or "",
            "status": row.status or "",
            "org_name": row.org_name or "",
            "brand": _BRAND_DISPLAY.get(row.brand or "", row.brand or ""),
            "store_id": row.store_id or "",
            "issue_category": _slug_to_title(row.issue_category or ""),
            "created_at": created_at.strftime("%Y-%m-%d") if isinstance(created_at, datetime) else str(created_at or ""),
            "solved_at": solved_at.strftime("%Y-%m-%d") if isinstance(solved_at, datetime) else str(solved_at or ""),
            "resolution": (row.resolution or "")[:500],
            "evidence_snippet": (row.evidence_snippet or "")[:500],
            "full_text_len": int(row.full_text_len or 0),
        })
    found_ids = [int(row["ticket_id"]) for row in out_rows]
    return {
        "requested_ids": bounded,
        "found_ids": found_ids,
        "missing_ids": [ticket_id for ticket_id in bounded if ticket_id not in set(found_ids)],
        "rows": out_rows,
        "cap": cap,
        "truncated": truncated,
        "bq_summary": (
            "BOUNDED BULK TICKET LOOKUP: "
            f"requested={len(requested)}, bounded={len(bounded)}, found={len(found_ids)}, "
            f"missing={len(bounded) - len(found_ids)}, cap={cap}, parameterized=true."
        ),
    }


def run_bq_for_chat(question: str, ticket_context: str = "") -> str:
    """
    Generate and execute a BQ SQL query for an analytics question in ticket chat context.

    Args:
        question: The engineer's question.
        ticket_context: Compact ticket summary (subject, category, org_name) for SQL generation.

    Returns a formatted text block to inject into the Gemini Phase 2 context,
    or empty string if anything fails (graceful degradation).
    """
    from app.services.cost_tracker import (
        CallType,
        CostDivergenceError,
        CostGateError,
        calc_cost,
        check_cost_gate,
        check_divergence,
        estimate_tokens_from_text,
        get_gate_reserve,
        new_correlation_id,
        record_estimate,
        record_usage,
    )

    try:
        from app.services.gemini import _model, GenerationConfig
        import json as _json

        prompt = _BQ_ANALYTICS_SQL_PROMPT.format(
            schema=SCHEMA_DESCRIPTION,
            ticket_context=ticket_context or "(no ticket context provided)",
            question=question,
        )

        est_tokens = estimate_tokens_from_text(prompt)
        est_cost = calc_cost(est_tokens, est_tokens // 4, call_type=CallType.ANALYTICS_SQL)
        reserved = get_gate_reserve(CallType.ANALYTICS_SQL, est_cost)
        corr = new_correlation_id()
        check_cost_gate(CallType.ANALYTICS_SQL, reserved, fail_closed=False)
        record_estimate(
            CallType.ANALYTICS_SQL,
            est_cost,
            corr,
            est_basis="formula",
            est_input_tokens=est_tokens,
            est_output_tokens=est_tokens // 4,
            expected_cost_cad=est_cost,
            reserved_cost_cad=reserved,
        )

        resp = _model.generate_content(
            prompt,
            generation_config=GenerationConfig.from_dict({"temperature": 0.0, "max_output_tokens": 1024, "response_mime_type": "application/json", "thinking_config": {"thinking_budget": 0}}),
        )
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            t_in = getattr(usage, "prompt_token_count", 0) or 0
            t_out = getattr(usage, "candidates_token_count", 0) or 0
            t_think = getattr(usage, "thoughts_token_count", 0) or 0
            actual = record_usage(
                CallType.ANALYTICS_SQL, t_in, t_out, t_think,
                correlation_id=corr,
                est_cost_usd=est_cost,
                expected_cost_cad=est_cost,
            )
            check_divergence(
                est_cost,
                actual,
                CallType.ANALYTICS_SQL,
                t_think,
                thinking_budget_was_zero=True,
            )
        data = _json.loads(resp.text)
        sql = data.get("sql", "").strip()
        if not sql:
            return ""
        sql = _fix_timestamp_intervals(sql)

        result = execute_query(sql)
        rows = result.get("rows", [])
        columns = result.get("columns", [])
        row_count = result.get("row_count", 0)

        if not rows:
            return "\n=== ANALYTICS DATA ===\nNo data found for this query.\n=== END ANALYTICS ===\n"

        # Format as compact table
        lines = [f"\n=== ANALYTICS DATA ({row_count} rows) ==="]
        header = " | ".join(str(c) for c in columns)
        lines.append(header)
        lines.append("-" * min(len(header), 80))
        for row in rows[:50]:  # cap display at 50 rows
            lines.append(" | ".join(str(v) if v is not None else "" for v in row))
        if row_count > 50:
            lines.append(f"... ({row_count - 50} more rows not shown)")
        lines.append("=== END ANALYTICS ===\n")
        return "\n".join(lines)

    except (CostGateError, CostDivergenceError):
        raise
    except Exception as exc:
        logger.warning("run_bq_for_chat failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Google Drive chunks (drive_chunks table)
# ---------------------------------------------------------------------------

_DRIVE_CHUNKS_TABLE_SCHEMA = [
    bigquery.SchemaField("file_id",     "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("file_name",   "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("source_id",   "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("source_path", "STRING"),
    bigquery.SchemaField("source_path_slug", "STRING"),
    bigquery.SchemaField("mime_type", "STRING"),
    bigquery.SchemaField("chunk_index", "INT64",     mode="REQUIRED"),
    bigquery.SchemaField("chunk_text",  "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("drive_url",   "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("modified_at", "TIMESTAMP"),
    bigquery.SchemaField("indexed_at",  "TIMESTAMP"),
]

_DRIVE_SPREADSHEET_ROWS_TABLE_SCHEMA = [
    bigquery.SchemaField("row_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("file_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("file_name", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source_path", "STRING"),
    bigquery.SchemaField("source_path_slug", "STRING"),
    bigquery.SchemaField("mime_type", "STRING"),
    bigquery.SchemaField("drive_url", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("modified_at", "TIMESTAMP"),
    bigquery.SchemaField("indexed_at", "TIMESTAMP"),
    bigquery.SchemaField("source_format", "STRING"),
    bigquery.SchemaField("sheet_name", "STRING"),
    bigquery.SchemaField("sheet_order", "INT64"),
    bigquery.SchemaField("row_number", "INT64"),
    bigquery.SchemaField("row_type", "STRING"),
    bigquery.SchemaField("table_title", "STRING"),
    bigquery.SchemaField("section_title", "STRING"),
    bigquery.SchemaField("section_context", "STRING"),
    bigquery.SchemaField("headers_json", "STRING"),
    bigquery.SchemaField("values_json", "STRING"),
    bigquery.SchemaField("search_text", "STRING"),
]


def ensure_drive_chunks_table() -> None:
    """Create drive_chunks table if it does not exist. Idempotent."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"
    try:
        client.get_table(table_ref)
        # Backward-compatible schema evolution for existing environments.
        client.query(
            f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS source_path STRING"
        ).result()
        client.query(
            f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS source_path_slug STRING"
        ).result()
        client.query(
            f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS mime_type STRING"
        ).result()
    except Exception:
        table = bigquery.Table(table_ref, schema=_DRIVE_CHUNKS_TABLE_SCHEMA)
        client.create_table(table)
        logger.info("Created table drive_chunks")


def ensure_drive_spreadsheet_rows_table() -> None:
    """Create spreadsheet-specific Drive table if it does not exist. Idempotent."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"
    try:
        client.get_table(table_ref)
        client.query(
            f"ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS mime_type STRING"
        ).result()
    except Exception:
        table = bigquery.Table(table_ref, schema=_DRIVE_SPREADSHEET_ROWS_TABLE_SCHEMA)
        client.create_table(table)
        logger.info("Created table drive_spreadsheet_rows")


def get_indexed_drive_files(source_id: str) -> dict[str, str]:
    """
    Return {file_id: modified_at_iso} for all files currently in drive_chunks.
    Used by sync to detect new/modified/deleted files.
    Returns empty dict if table doesn't exist yet.

    Returns timestamps in Drive's ISO 8601 format (2024-05-15T10:30:00.000Z)
    so they compare correctly against Drive API modifiedTime strings.
    """
    client = _get_client()
    sql = f"""
        SELECT file_id,
               FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(modified_at)) AS modified_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.drive_chunks`
        WHERE source_id = @source_id
        GROUP BY file_id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("source_id", "STRING", source_id)]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
        return {r.file_id: r.modified_at for r in rows}
    except Exception:
        return {}


def build_drive_chunk_rows(
    file_id: str,
    file_name: str,
    source_id: str,
    source_path: str,
    source_path_slug: str,
    drive_url: str,
    modified_at: str,
    chunks: list[str],
    mime_type: str | None = None,
) -> list[dict]:
    """
    Build BQ row dicts for drive_chunks. Does NOT write to BQ.
    Call bulk_write_drive_chunks() once at end of sync to write all at once.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for i, chunk in enumerate(chunks):
        row = {
            "file_id":     file_id,
            "file_name":   file_name,
            "source_id":   source_id,
            "source_path": source_path,
            "source_path_slug": source_path_slug,
            "chunk_index": i,
            "chunk_text":  chunk,
            "drive_url":   drive_url,
            "modified_at": modified_at,
            "indexed_at":  now_iso,
        }
        if mime_type is not None:
            row["mime_type"] = mime_type
        rows.append(row)
    return rows


def build_drive_spreadsheet_row_rows(
    *,
    file_id: str,
    file_name: str,
    source_id: str,
    source_path: str,
    source_path_slug: str,
    drive_url: str,
    modified_at: str,
    source_format: str,
    row_records: list[dict],
    mime_type: str | None = None,
) -> list[dict]:
    """Build BQ row dicts for structured spreadsheet records."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    for record in row_records:
        row_id = f"{file_id}:{record['sheet_order']}:{record['row_number']}"
        rows.append(
            {
                "row_id": row_id,
                "file_id": file_id,
                "file_name": file_name,
                "source_id": source_id,
                "source_path": source_path,
                "source_path_slug": source_path_slug,
                "drive_url": drive_url,
                "modified_at": modified_at,
                "indexed_at": now_iso,
                "source_format": source_format,
                "sheet_name": record.get("sheet_name", ""),
                "sheet_order": record.get("sheet_order", 0),
                "row_number": record.get("row_number", 0),
                "row_type": record.get("row_type", ""),
                "table_title": record.get("table_title", ""),
                "section_title": record.get("section_title", ""),
                "section_context": record.get("section_context", ""),
                "headers_json": record.get("headers_json", "[]"),
                "values_json": record.get("values_json", "{}"),
                "search_text": record.get("search_text", ""),
            }
        )
        if mime_type is not None:
            rows[-1]["mime_type"] = mime_type
    return rows


def get_drive_chunks_for_files(file_ids: set[str]) -> list[dict]:
    """
    Fetch all existing drive_chunks rows for the given file_ids.
    Used during sync to re-include unchanged files in the full rebuild.
    """
    if not file_ids:
        return []
    import json as _json
    client = _get_client()
    id_list = ", ".join(f"'{fid}'" for fid in file_ids)
    sql = f"""
        SELECT file_id, file_name, source_id, source_path, source_path_slug,
               chunk_index, chunk_text, drive_url,
               CAST(modified_at AS STRING) AS modified_at,
               CAST(indexed_at AS STRING) AS indexed_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.drive_chunks`
        WHERE file_id IN ({id_list})
    """
    try:
        rows = list(client.query(sql).result())
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("get_drive_chunks_for_files failed: %s", exc)
        return []


def bulk_write_drive_chunks(rows: list[dict]) -> None:
    """
    Full rebuild of drive_chunks using WRITE_TRUNCATE.

    Replaces the entire table in a single load job.
    WRITE_TRUNCATE is a load operation (not DML) and is NOT blocked
    by the BQ streaming buffer — unlike DELETE/UPDATE/MERGE.

    Caller must pass ALL current rows (new + updated + unchanged files).
    Deleted files are excluded by not passing their rows.
    """
    import io
    import json as _json

    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"

    if not rows:
        # Nothing to write — truncate to empty (keeps table structure)
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=_DRIVE_CHUNKS_TABLE_SCHEMA,
        )
        load_job = client.load_table_from_file(
            io.BytesIO(b""),
            table_ref,
            job_config=job_config,
        )
        load_job.result()
        logger.info("bulk_write_drive_chunks: table truncated (0 rows)")
        return

    ndjson = "\n".join(_json.dumps(row) for row in rows).encode("utf-8")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=_DRIVE_CHUNKS_TABLE_SCHEMA,
    )
    load_job = client.load_table_from_file(
        io.BytesIO(ndjson),
        table_ref,
        job_config=job_config,
    )
    load_job.result()
    if load_job.errors:
        logger.error("bulk_write_drive_chunks: BQ load errors: %s", load_job.errors)
    else:
        logger.info("bulk_write_drive_chunks: rebuilt drive_chunks with %d rows", len(rows))


def delete_drive_chunks(file_id: str) -> None:
    """Delete all chunks for a file from drive_chunks."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("file_id", "STRING", file_id)]
    )
    client.query(
        f"DELETE FROM `{table_ref}` WHERE file_id = @file_id",
        job_config=job_config,
    ).result(timeout=120)


def replace_drive_chunks_for_file(file_id: str, rows: list[dict]) -> None:
    """
    Replace all drive_chunks rows for one file atomically at file granularity.

    Strategy:
    1) DELETE existing rows for file_id
    2) LOAD new rows with WRITE_APPEND (if rows exist)

    This makes Drive sync resilient to mid-run interruption:
    already-processed files stay persisted in BQ and are skipped on restart.
    """
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"

    delete_drive_chunks(file_id)
    if not rows:
        return

    import io
    import json as _json

    ndjson = "\n".join(_json.dumps(row) for row in rows).encode("utf-8")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=_DRIVE_CHUNKS_TABLE_SCHEMA,
    )
    load_job = client.load_table_from_file(
        io.BytesIO(ndjson),
        table_ref,
        job_config=job_config,
    )
    load_job.result(timeout=120)
    if load_job.errors:
        raise RuntimeError(f"replace_drive_chunks_for_file load errors: {load_job.errors}")


def delete_drive_spreadsheet_rows(file_id: str) -> None:
    """Delete all structured spreadsheet rows for a file."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("file_id", "STRING", file_id)]
    )
    try:
        client.query(
            f"DELETE FROM `{table_ref}` WHERE file_id = @file_id",
            job_config=job_config,
        ).result(timeout=120)
    except Exception:
        return


def replace_drive_spreadsheet_rows_for_file(file_id: str, rows: list[dict]) -> None:
    """Replace structured spreadsheet rows for a file atomically at file granularity."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"

    delete_drive_spreadsheet_rows(file_id)
    if not rows:
        return

    import io
    import json as _json

    ndjson = "\n".join(_json.dumps(row) for row in rows).encode("utf-8")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=_DRIVE_SPREADSHEET_ROWS_TABLE_SCHEMA,
    )
    load_job = client.load_table_from_file(
        io.BytesIO(ndjson),
        table_ref,
        job_config=job_config,
    )
    load_job.result(timeout=120)
    if load_job.errors:
        raise RuntimeError(f"replace_drive_spreadsheet_rows_for_file load errors: {load_job.errors}")


_DRIVE_QUERY_STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "contains", "did",
    "do", "does", "document", "exact", "find", "for", "from", "guide", "has", "have",
    "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "our", "please",
    "not", "say", "search", "show", "that", "the", "there", "this", "to", "what", "where",
    "which", "who", "with", "you", "your",
}

_DRIVE_TEAM_INTENT_WORDS = {
    "who", "team", "owner", "responsible", "responsibility", "escalate", "escalation",
    "contact", "level", "кто", "команда", "ответственный", "эскалация",
}

_DRIVE_STRUCTURED_LOOKUP_WORDS = {
    "teamviewer", "access", "code", "customer", "postal", "contract", "store",
    "row", "sheet", "table", "escalate", "escalation", "owner", "responsible",
    "vendor", "contact", "phone", "email",
}

_DRIVE_STRUCTURED_FIELD_WORDS = _DRIVE_STRUCTURED_LOOKUP_WORDS | {
    "name", "number", "no", "issues", "issue", "task",
}

_DRIVE_SHORT_CODE_TOKENS = {
    "ba", "bp", "dd", "frsh", "mm", "sd", "stc", "ue", "city_fries",
}

_DRIVE_CONTACT_LOOKUP_WORDS = {"vendor", "contact", "phone", "email"}
_DRIVE_CONTACT_FIELD_WORDS = _DRIVE_CONTACT_LOOKUP_WORDS | {
    "number", "support", "helpdesk", "service", "desk",
}


def _normalize_drive_query_text(value: str) -> str:
    lowered = (value or "").lower()
    lowered = re.sub(r"[^a-z0-9а-яё]+", " ", lowered, flags=re.IGNORECASE)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _tokenize_drive_query(value: str) -> list[str]:
    tokens = _normalize_drive_query_text(value).split()
    uppercase_codes = {
        match.group(0).lower()
        for match in re.finditer(r"\b[A-Z]{2,5}\b", value or "")
    }
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in _DRIVE_QUERY_STOPWORDS:
            continue
        keep_short_code = token in _DRIVE_SHORT_CODE_TOKENS or token in uppercase_codes
        if len(token) <= 2 and not token.isdigit() and not keep_short_code:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _compact_drive_query(value: str) -> str:
    return " ".join(_tokenize_drive_query(value)[:6]).strip()


def _is_team_lookup_query(value: str) -> bool:
    normalized = _normalize_drive_query_text(value)
    words = set(normalized.split())
    return bool(words & _DRIVE_TEAM_INTENT_WORDS)


def _is_structured_spreadsheet_lookup_query(value: str) -> bool:
    normalized = _normalize_drive_query_text(value)
    words = set(normalized.split())
    return bool(words & _DRIVE_STRUCTURED_LOOKUP_WORDS)


def _drive_file_id_from_text(value: str) -> str | None:
    text = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{25,80}", text):
        return text
    patterns = (
        r"/file/d/([A-Za-z0-9_-]{25,80})",
        r"/spreadsheets/d/([A-Za-z0-9_-]{25,80})",
        r"/document/d/([A-Za-z0-9_-]{25,80})",
        r"/presentation/d/([A-Za-z0-9_-]{25,80})",
        r"[?&]id=([A-Za-z0-9_-]{25,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _contact_lookup_entity_tokens(value: str) -> list[str]:
    tokens = _tokenize_drive_query(value)
    if not set(tokens).intersection(_DRIVE_CONTACT_LOOKUP_WORDS):
        return []
    return [
        token for token in tokens
        if token not in _DRIVE_CONTACT_FIELD_WORDS and not token.isdigit()
    ]


def _filter_drive_rows_by_tokens(rows: list[dict], required_tokens: list[str]) -> list[dict]:
    if not rows or not required_tokens:
        return rows
    out: list[dict] = []
    for row in rows:
        row_norm = _normalize_drive_query_text(
            " ".join(
                str(row.get(key, "") or "")
                for key in (
                    "chunk_text", "file_name", "source_path", "sheet_name",
                    "table_title", "section_title",
                )
            )
        )
        if all(token in row_norm for token in required_tokens):
            out.append(row)
    return out


def _filter_identifier_spreadsheet_rows(raw_query: str, rows: list[dict]) -> list[dict]:
    """For exact store/contract/code asks, keep rows carrying the explicit identifier."""
    numeric_tokens = [
        token for token in _tokenize_drive_query(raw_query)
        if token.isdigit() and len(token) >= 3
    ]
    if not numeric_tokens:
        return rows

    matching = []
    for row in rows:
        row_norm = _normalize_drive_query_text(str(row.get("chunk_text", "")))
        if all(token in row_norm for token in numeric_tokens):
            matching.append(row)
    return matching or rows


def _filter_named_spreadsheet_rows(raw_query: str, rows: list[dict]) -> list[dict]:
    """
    For specific tabular lookups without numeric IDs, keep rows carrying the
    distinguishing non-field token, e.g. "City Fries 2009" should not include every City Fries row.
    """
    if not rows or _is_team_lookup_query(raw_query):
        return rows

    query_tokens = [
        token for token in _tokenize_drive_query(raw_query)
        if token not in _DRIVE_STRUCTURED_FIELD_WORDS and len(token) >= 3
    ]
    if not query_tokens:
        return rows

    row_norms = [
        _normalize_drive_query_text(str(row.get("chunk_text", "")))
        for row in rows
    ]
    matching_tokens = [
        token for token in query_tokens
        if any(token in row_norm for row_norm in row_norms)
    ]
    if not matching_tokens:
        return rows

    matching = [
        row for row, row_norm in zip(rows, row_norms)
        if all(token in row_norm for token in matching_tokens)
    ]
    return matching or rows


def _extract_spreadsheet_issue_task(row_text: str) -> str:
    """Extract the row-specific issue/task label from rendered spreadsheet text."""
    match = re.search(
        r"\bISSUE/TASK:\s*(.*?)(?:\.\s+[A-Z][A-Za-z0-9 /-]*:|$)",
        row_text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _filter_general_team_lookup_rows(raw_query: str, rows: list[dict]) -> list[dict]:
    """
    Avoid over-answering from spreadsheet sections.

    Escalation matrices often have a section owner row followed by specific
    task rows. For a general question like "who do I escalate DispatchFlow
    issues to?", the section owner is authoritative. Child task rows are only
    relevant when the user explicitly mentions that task (e.g. Failed Orders).
    """
    if not rows or not _is_team_lookup_query(raw_query):
        return rows

    query_tokens = set(_tokenize_drive_query(raw_query))
    if not query_tokens:
        return rows

    headers = [r for r in rows if str(r.get("row_type", "")) == "section_header"]
    if not headers:
        return rows

    matching_task_rows: list[dict] = []
    for row in rows:
        if str(row.get("row_type", "")) == "section_header":
            continue
        issue_task = _extract_spreadsheet_issue_task(str(row.get("chunk_text", "")))
        if not issue_task:
            continue
        task_tokens = set(_tokenize_drive_query(issue_task))
        # Require an explicit task overlap. Section-title hits alone are not
        # enough; every child row inherits the same section title.
        if task_tokens and query_tokens.intersection(task_tokens):
            matching_task_rows.append(row)

    if matching_task_rows:
        matched_sections = {
            (
                str(row.get("file_name", "")),
                str(row.get("sheet_name", "")),
                str(row.get("section_title", "")),
            )
            for row in matching_task_rows
        }
        relevant_headers = [
            row for row in headers
            if (
                str(row.get("file_name", "")),
                str(row.get("sheet_name", "")),
                str(row.get("section_title", "")),
            ) in matched_sections
        ]
        out: list[dict] = []
        seen: set[tuple[str, str, int]] = set()
        for row in relevant_headers + matching_task_rows:
            key = (
                str(row.get("file_name", "")),
                str(row.get("sheet_name", "")),
                int(row.get("row_number") or 0),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    # General section-owner lookup. Keep the best header only; inherited child
    # rows and unrelated sheet sections can contain generic words like "issues"
    # and should not be allowed to distract the final answer.
    return headers[:1]


def _score_drive_spreadsheet_row(
    row: dict[str, Any],
    *,
    query_normalized: str,
    query_tokens: list[str],
    team_lookup: bool,
) -> float:
    search_norm = _normalize_drive_query_text(str(row.get("chunk_text", "")))
    section_norm = _normalize_drive_query_text(str(row.get("section_title", "")))
    table_norm = _normalize_drive_query_text(str(row.get("table_title", "")))
    row_type = str(row.get("row_type", ""))

    score = 0.0
    if query_normalized and query_normalized in search_norm:
        score += 10.0
    if query_normalized and query_normalized in section_norm:
        score += 12.0
    if query_normalized and query_normalized in table_norm:
        score += 6.0

    token_hits = sum(1 for token in query_tokens if token in search_norm)
    section_hits = sum(1 for token in query_tokens if token in section_norm)
    table_hits = sum(1 for token in query_tokens if token in table_norm)
    score += token_hits * 1.8
    score += section_hits * 2.5
    score += table_hits * 1.5

    if row_type == "section_header":
        score += 0.8
        if query_tokens and section_hits >= max(1, int(len(query_tokens) * 0.6)):
            score += 4.0
        if team_lookup:
            score += 3.0

    if team_lookup and "section owner / team" in search_norm:
        score += 4.0
    if team_lookup and any(label in search_norm for label in ("team responsible", "owner", "responsible")):
        score += 2.0

    asks_for_level = "level" in query_tokens and ("1" in query_tokens or "2" in query_tokens)
    if asks_for_level:
        if re.search(r"\blevel\s*[12]\b", search_norm):
            score += 6.0
        elif row_type == "section_header":
            score -= 2.0

    return score


_DRIVE_HIGH_VALUE_PATH_TERMS = (
    "1. full guide",
    "3. operations_suite",
    "4. labour",
    "5. inventory",
    "6. reports",
    "8.food forecasting/job aids",
    "office job aids",
    "posters",
)

_DRIVE_LOW_VALUE_TERMS = (
    "translation work",
    "feedback form",
    "form responses",
    "project debrief",
    "project plan",
    "job aid updates notes",
    "archived",
    "copies of",
    "sent for translation",
)


def _clean_drive_result_text(text: str) -> str:
    """Clean noisy OCR artifacts at retrieval time without mutating stored chunks."""
    value = str(text or "")
    if not value:
        return ""

    # Preserve the useful image description, but trim long keyboard/underscore
    # artifacts that Gemini OCR can emit for mobile screenshots.
    value = re.sub(r"(?:_\s*){12,}", " ", value)
    value = re.sub(r"(?:�\s*){3,}", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _drive_source_quality_score(row: dict[str, Any]) -> float:
    file_name = _normalize_drive_query_text(str(row.get("file_name", "")))
    source_path = _normalize_drive_query_text(str(row.get("source_path", "")))
    combined = f"{source_path} {file_name}".strip()

    score = 0.0
    if str(row.get("_drive_result_type", "")) in {"chunk", "chunk_ranked"}:
        score += 3.0
    elif str(row.get("_drive_result_type", "")) == "spreadsheet":
        score -= 1.5

    for term in _DRIVE_HIGH_VALUE_PATH_TERMS:
        if term in combined:
            score += 3.0

    if source_path in {"4. labour", "5. inventory", "6. reports", "office job aids"}:
        score += 4.0

    for term in _DRIVE_LOW_VALUE_TERMS:
        if term in combined:
            score -= 5.0

    if any(label in file_name for label in ("job aid", "reference guide", "full guide", "manual")):
        score += 2.5
    if file_name.startswith("store_operations - "):
        score += 4.0
    if any(file_name.endswith(ext) for ext in (".pdf", ".docx", ".pptx", ".xlsx")):
        score += 1.5
    if "responses" in file_name:
        score -= 6.0
    if file_name in {"translation work", "french to be updated", "french translation"}:
        score -= 8.0

    return score


def _score_drive_exact_result(row: dict[str, Any], raw_query: str) -> float:
    query_norm = _normalize_drive_query_text(raw_query)
    query_tokens = _tokenize_drive_query(raw_query)
    text_norm = _normalize_drive_query_text(str(row.get("chunk_text", "")))
    file_norm = _normalize_drive_query_text(str(row.get("file_name", "")))
    path_norm = _normalize_drive_query_text(str(row.get("source_path", "")))
    combined_norm = f"{path_norm} {file_norm}".strip()

    score = _drive_source_quality_score(row)
    if "full guide" in file_norm and "full guide" not in query_norm:
        # Full guides are useful fallbacks, but focused job aids should win for
        # exact operational phrases unless the user asked for the full guide.
        score -= 3.0
    if query_norm and query_norm in text_norm:
        score += 25.0
    if query_norm and query_norm in file_norm:
        score += 14.0
    if query_norm and query_norm in path_norm:
        score += 4.0

    if query_tokens:
        text_hits = sum(1 for token in query_tokens if token in text_norm)
        file_hits = sum(1 for token in query_tokens if token in file_norm)
        path_hits = sum(1 for token in query_tokens if token in path_norm)
        coverage = text_hits / max(1, len(query_tokens))
        score += text_hits * 1.5
        score += file_hits * 2.0
        score += path_hits * 0.7
        if coverage >= 0.75:
            score += 8.0
        elif coverage >= 0.45:
            score += 3.0

    if str(row.get("row_type", "")) == "section_header":
        score += 0.5

    if "operations_suite" in query_tokens:
        if "3. operations_suite" in combined_norm or "operations_suite" in file_norm:
            score += 8.0
        if "office job aids" in path_norm or "posters" in path_norm:
            score -= 6.0

    if any(token in query_tokens for token in ("labour", "labor", "pay", "period", "schedule", "coverage")):
        if "4. labour" in path_norm or file_norm.startswith("store_operations - labour"):
            score += 8.0
        if "french/7. other items needed for translation" in path_norm:
            score -= 6.0

    if "inventory" in query_tokens or "adjustment" in query_tokens:
        if "5. inventory" in path_norm or file_norm.startswith("store_operations - inventory"):
            score += 8.0

    if any(token in query_tokens for token in ("bonjour", "screenshot", "keyboard")):
        if "screenshots" in path_norm or file_norm.startswith("img_"):
            score += 10.0
        if "posters" in path_norm or "office job aids" in path_norm:
            score -= 6.0

    return score


def _rank_drive_exact_results(rows: list[dict], raw_query: str, page_size: int) -> list[dict]:
    ranked: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        cleaned = {**row, "chunk_text": _clean_drive_result_text(str(row.get("chunk_text", "")))}
        key = (
            str(cleaned.get("file_name", "")),
            str(cleaned.get("source_path", "")),
            str(cleaned.get("chunk_text", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned["_rank_score"] = _score_drive_exact_result(cleaned, raw_query)
        ranked.append(cleaned)

    ranked.sort(
        key=lambda r: (
            float(r.get("_rank_score", 0.0)),
            -int(r.get("chunk_index") or r.get("row_number") or 0),
        ),
        reverse=True,
    )

    out: list[dict] = []
    for row in ranked[:page_size]:
        row.pop("_rank_score", None)
        row.pop("_drive_result_type", None)
        out.append(row)
    return out


def _search_drive_chunk_rows_ranked(
    *,
    raw_query: str,
    page_size: int,
    source_ids: list[str],
) -> list[dict]:
    """Token-ranked fallback for Drive chunks when the full phrase is not contiguous."""
    query_tokens = _tokenize_drive_query(raw_query)
    compact_query = _compact_drive_query(raw_query)
    if not query_tokens and not compact_query:
        return []

    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"
    sql = f"""
        SELECT REGEXP_REPLACE(chunk_text, r'\\s+', ' ') AS chunk_text,
               file_name, drive_url, source_path, chunk_index
        FROM `{table_ref}`
        WHERE source_id IN UNNEST(@source_ids)
          AND (
                EXISTS (
                    SELECT 1
                    FROM UNNEST(@tokens) AS tok
                    WHERE STRPOS(LOWER(REGEXP_REPLACE(chunk_text, r'\\s+', ' ')), tok) > 0
                       OR STRPOS(LOWER(file_name), tok) > 0
                       OR STRPOS(LOWER(IFNULL(source_path, '')), tok) > 0
                )
                OR (@compact_query != '' AND CONTAINS_SUBSTR(LOWER(file_name), @compact_query))
          )
        ORDER BY file_name, chunk_index
        LIMIT 400
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("source_ids", "STRING", source_ids),
            bigquery.ArrayQueryParameter("tokens", "STRING", query_tokens),
            bigquery.ScalarQueryParameter("compact_query", "STRING", compact_query),
        ]
    )
    try:
        rows = [
            {
                "chunk_text": r.chunk_text,
                "file_name": r.file_name,
                "drive_url": r.drive_url,
                "source_path": r.source_path,
                "chunk_index": r.chunk_index,
                "_drive_result_type": "chunk_ranked",
            }
            for r in client.query(sql, job_config=job_config).result()
        ]
    except Exception as exc:
        logger.warning("search_drive_chunk_rows_ranked failed: %s", exc)
        return []

    return _rank_drive_exact_results(rows, raw_query, page_size)


def _search_drive_spreadsheet_rows_exact(
    *,
    query: str,
    page_size: int,
    source_ids: list[str],
) -> list[dict]:
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"
    sql = f"""
        SELECT search_text AS chunk_text, file_name, drive_url, source_path,
               sheet_name, row_number, row_type, table_title, section_title
        FROM `{table_ref}`
        WHERE CONTAINS_SUBSTR(LOWER(search_text), LOWER(@query))
          AND source_id IN UNNEST(@source_ids)
        ORDER BY file_name, sheet_order, row_number
        LIMIT {int(page_size)}
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("query", "STRING", query),
            bigquery.ArrayQueryParameter("source_ids", "STRING", source_ids),
        ]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    return [dict(r) for r in rows]


def _search_drive_spreadsheet_rows_ranked(
    *,
    raw_query: str,
    page_size: int,
    source_ids: list[str],
) -> list[dict]:
    query_tokens = _tokenize_drive_query(raw_query)
    compact_query = _compact_drive_query(raw_query)
    if not query_tokens and not compact_query:
        return []

    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"
    sql = f"""
        SELECT search_text AS chunk_text, file_name, drive_url, source_path,
               sheet_name, row_number, row_type, table_title, section_title
        FROM `{table_ref}`
        WHERE source_id IN UNNEST(@source_ids)
          AND (
                EXISTS (
                    SELECT 1
                    FROM UNNEST(@tokens) AS tok
                    WHERE STRPOS(LOWER(search_text), tok) > 0
                       OR STRPOS(LOWER(IFNULL(section_title, '')), tok) > 0
                       OR STRPOS(LOWER(IFNULL(table_title, '')), tok) > 0
                )
                OR (@compact_query != '' AND CONTAINS_SUBSTR(LOWER(file_name), @compact_query))
          )
        ORDER BY file_name, sheet_order, row_number
        LIMIT 400
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("source_ids", "STRING", source_ids),
            bigquery.ArrayQueryParameter("tokens", "STRING", query_tokens),
            bigquery.ScalarQueryParameter("compact_query", "STRING", compact_query),
        ]
    )
    rows = [dict(r) for r in client.query(sql, job_config=job_config).result()]
    if not rows:
        return []

    normalized = _normalize_drive_query_text(raw_query)
    team_lookup = _is_team_lookup_query(raw_query)
    for row in rows:
        row["_score"] = _score_drive_spreadsheet_row(
            row,
            query_normalized=normalized,
            query_tokens=query_tokens,
            team_lookup=team_lookup,
        )

    ranked = sorted(
        rows,
        key=lambda r: (
            float(r.get("_score", 0.0)),
            1 if str(r.get("row_type", "")) == "section_header" else 0,
            -int(r.get("row_number") or 0),
        ),
        reverse=True,
    )

    out: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for row in ranked:
        score = float(row.get("_score", 0.0))
        if score <= 0:
            continue
        key = (
            str(row.get("file_name", "")),
            str(row.get("sheet_name", "")),
            int(row.get("row_number") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        row.pop("_score", None)
        out.append(row)
        if len(out) >= page_size:
            break
    return out


def _search_drive_rows_by_file_id(
    *,
    file_id: str,
    page_size: int,
    source_ids: list[str],
) -> list[dict]:
    client = _get_client()
    spreadsheet_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"
    chunks_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_chunks"
    params = [
        bigquery.ScalarQueryParameter("file_id", "STRING", file_id),
        bigquery.ArrayQueryParameter("source_ids", "STRING", source_ids),
    ]
    sheet_sql = f"""
        SELECT search_text AS chunk_text, file_id, file_name, drive_url, source_path,
               sheet_name, row_number, row_type, table_title, section_title
        FROM `{spreadsheet_ref}`
        WHERE file_id = @file_id
          AND source_id IN UNNEST(@source_ids)
        ORDER BY sheet_order, row_number
        LIMIT {int(page_size)}
    """
    try:
        sheet_rows = [dict(r) for r in client.query(
            sheet_sql,
            job_config=bigquery.QueryJobConfig(query_parameters=params),
        ).result()]
    except Exception as exc:
        logger.warning("search_drive_rows_by_file_id spreadsheet lookup failed: %s", exc)
        sheet_rows = []
    if sheet_rows:
        return sheet_rows

    chunk_sql = f"""
        SELECT REGEXP_REPLACE(chunk_text, r'\\s+', ' ') AS chunk_text,
               file_id, file_name, drive_url, source_path, chunk_index
        FROM `{chunks_ref}`
        WHERE file_id = @file_id
          AND source_id IN UNNEST(@source_ids)
        ORDER BY chunk_index
        LIMIT {int(page_size)}
    """
    try:
        return [
            {
                "chunk_text": r.chunk_text,
                "file_id": r.file_id,
                "file_name": r.file_name,
                "drive_url": r.drive_url,
                "source_path": r.source_path,
                "chunk_index": r.chunk_index,
            }
            for r in client.query(
                chunk_sql,
                job_config=bigquery.QueryJobConfig(query_parameters=params),
            ).result()
        ]
    except Exception as exc:
        logger.warning("search_drive_rows_by_file_id chunk lookup failed: %s", exc)
        return []


def search_drive_spreadsheet_rows(
    query: str,
    *,
    page_size: int,
    source_ids: list[str],
    file_name: str | None = None,
    summarize: bool = False,
) -> list[dict]:
    """Structured spreadsheet row search over drive_spreadsheet_rows."""
    client = _get_client()
    table_ref = f"{GCP_PROJECT}.{BQ_DATASET}.drive_spreadsheet_rows"

    try:
        client.get_table(table_ref)
    except Exception:
        return []

    if summarize and file_name:
        sql = f"""
            SELECT search_text AS chunk_text, file_name, drive_url, source_path,
                   sheet_name, row_number, row_type, table_title, section_title
            FROM `{table_ref}`
            WHERE file_name = @file_name
              AND source_id IN UNNEST(@source_ids)
            ORDER BY sheet_order, row_number
        """
        params = [
            bigquery.ScalarQueryParameter("file_name", "STRING", file_name),
            bigquery.ArrayQueryParameter("source_ids", "STRING", source_ids),
        ]
    else:
        sql = ""
        params: list[Any] = []

    try:
        if summarize and file_name:
            job_config = bigquery.QueryJobConfig(query_parameters=params)
            rows = list(client.query(sql, job_config=job_config).result())
            return [dict(r) for r in rows]

        exact_rows = _search_drive_spreadsheet_rows_exact(
            query=query,
            page_size=page_size,
            source_ids=source_ids,
        )
        if exact_rows:
            filtered = _filter_general_team_lookup_rows(query, exact_rows)
            filtered = _filter_identifier_spreadsheet_rows(query, filtered)
            return _filter_named_spreadsheet_rows(query, filtered)

        compact_query = _compact_drive_query(query)
        if compact_query and compact_query.lower() != query.strip().lower():
            compact_rows = _search_drive_spreadsheet_rows_exact(
                query=compact_query,
                page_size=page_size,
                source_ids=source_ids,
            )
            if compact_rows:
                filtered = _filter_general_team_lookup_rows(query, compact_rows)
                filtered = _filter_identifier_spreadsheet_rows(query, filtered)
                return _filter_named_spreadsheet_rows(query, filtered)

        ranked_rows = _search_drive_spreadsheet_rows_ranked(
            raw_query=query,
            page_size=page_size,
            source_ids=source_ids,
        )
        filtered = _filter_general_team_lookup_rows(query, ranked_rows)
        filtered = _filter_identifier_spreadsheet_rows(query, filtered)
        return _filter_named_spreadsheet_rows(query, filtered)
    except Exception as exc:
        logger.warning("search_drive_spreadsheet_rows failed: %s", exc)
        return []


def _resolve_enabled_drive_sources(requested_source_ids: list[str] | None = None) -> list[str]:
    """
    Return Drive source_ids that are both enabled in Firestore and requested by caller.
    When requested_source_ids is None, returns all enabled gdrive_* sources.
    """
    from app.services.firestore import get_data_sources

    sources = get_data_sources()
    enabled = [
        sid for sid, cfg in sources.items()
        if sid.startswith("gdrive_") and bool(cfg.get("enabled", False))
    ]
    if requested_source_ids is None:
        return enabled
    requested = set(requested_source_ids)
    return [sid for sid in enabled if sid in requested]


def search_drive_exact(
    query: str,
    page_size: int = 5,
    intent: str = "search",
    file_name: str | None = None,
    source_ids: list[str] | None = None,
) -> list[dict]:
    """
    Exact phrase search in drive_chunks using BQ CONTAINS_SUBSTR.
    Returns list of {chunk_text, file_name, drive_url}.
    Returns empty list if no requested/eligible Drive source is enabled.

    intent="search"   (default): CONTAINS_SUBSTR match, up to page_size results.
    intent="summarize": fetch ALL chunks for a specific file (by file_name),
                        ordered by chunk_index — returns complete document content
                        for summarization. Requires file_name parameter.
                        If file_name not given, falls back to keyword search for
                        matching files and returns all chunks of the best match.
    """
    allowed_source_ids = _resolve_enabled_drive_sources(source_ids)
    if not allowed_source_ids:
        logger.info("search_drive_exact: no enabled Drive source matches request — skipping")
        return []

    client = _get_client()
    requested_file_id = _drive_file_id_from_text(query) or _drive_file_id_from_text(file_name or "")
    if requested_file_id:
        return _search_drive_rows_by_file_id(
            file_id=requested_file_id,
            page_size=page_size,
            source_ids=allowed_source_ids,
        )

    if intent == "summarize":
        if file_name:
            structured_rows = search_drive_spreadsheet_rows(
                query=query,
                page_size=page_size,
                source_ids=allowed_source_ids,
                file_name=file_name,
                summarize=True,
            )
            if structured_rows:
                return structured_rows
        # Get all chunks for a named file, ordered for reading.
        # If no file_name provided, find the file whose name best matches query.
        if not file_name:
            # First: find file names containing the query keywords
            find_sql = f"""
                SELECT DISTINCT file_name, drive_url
                FROM `{GCP_PROJECT}.{BQ_DATASET}.drive_chunks`
                WHERE CONTAINS_SUBSTR(LOWER(file_name), LOWER(@query))
                  AND source_id IN UNNEST(@source_ids)
                LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("query", "STRING", query),
                    bigquery.ArrayQueryParameter("source_ids", "STRING", allowed_source_ids),
                ]
            )
            try:
                match_rows = list(client.query(find_sql, job_config=job_config).result())
            except Exception as exc:
                logger.warning("search_drive_exact summarize file lookup failed: %s", exc)
                match_rows = []
            if match_rows:
                file_name = match_rows[0].file_name
            else:
                # Fall back to regular search if no file name match
                logger.info("search_drive_exact: summarize intent but no file_name match for %r, falling back to search", query)
                intent = "search"

        if intent == "summarize" and file_name:
            structured_rows = search_drive_spreadsheet_rows(
                query=query,
                page_size=page_size,
                source_ids=allowed_source_ids,
                file_name=file_name,
                summarize=True,
            )
            if structured_rows:
                return structured_rows
            sql = f"""
                SELECT REGEXP_REPLACE(chunk_text, r'\\s+', ' ') AS chunk_text,
                       file_name, drive_url, source_path, chunk_index
                FROM `{GCP_PROJECT}.{BQ_DATASET}.drive_chunks`
                WHERE file_name = @file_name
                  AND source_id IN UNNEST(@source_ids)
                ORDER BY chunk_index
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("file_name", "STRING", file_name),
                    bigquery.ArrayQueryParameter("source_ids", "STRING", allowed_source_ids),
                ]
            )
            try:
                rows = list(client.query(sql, job_config=job_config).result())
                return [
                    {
                        "chunk_text": r.chunk_text,
                        "file_name":  r.file_name,
                        "drive_url":  r.drive_url,
                        "source_path": r.source_path,
                    }
                    for r in rows
                ]
            except Exception as exc:
                logger.warning("search_drive_exact summarize failed: %s", exc)
                return []

    # intent == "search" (default path)
    candidate_size = max(page_size * 6, 30)
    ranked_chunk_rows = _search_drive_chunk_rows_ranked(
        raw_query=query,
        page_size=candidate_size,
        source_ids=allowed_source_ids,
    )
    ranked_chunk_rows = [
        {**row, "_drive_result_type": "chunk_ranked"}
        for row in ranked_chunk_rows
    ]

    spreadsheet_rows = search_drive_spreadsheet_rows(
        query=query,
        page_size=candidate_size,
        source_ids=allowed_source_ids,
    )
    spreadsheet_rows = [
        {**row, "_drive_result_type": "spreadsheet"}
        for row in spreadsheet_rows
    ]
    contact_entity_tokens = _contact_lookup_entity_tokens(query)
    if contact_entity_tokens:
        spreadsheet_rows = _filter_drive_rows_by_tokens(spreadsheet_rows, contact_entity_tokens)
        ranked_chunk_rows = _filter_drive_rows_by_tokens(ranked_chunk_rows, contact_entity_tokens)
        if not spreadsheet_rows and not ranked_chunk_rows:
            entity_query = " ".join(contact_entity_tokens)
            ranked_chunk_rows = [
                {**row, "_drive_result_type": "chunk_ranked"}
                for row in _search_drive_chunk_rows_ranked(
                    raw_query=entity_query,
                    page_size=candidate_size,
                    source_ids=allowed_source_ids,
                )
            ]
            ranked_chunk_rows = _filter_drive_rows_by_tokens(ranked_chunk_rows, contact_entity_tokens)
            spreadsheet_rows = [
                {**row, "_drive_result_type": "spreadsheet"}
                for row in search_drive_spreadsheet_rows(
                    query=entity_query,
                    page_size=candidate_size,
                    source_ids=allowed_source_ids,
                )
            ]
            spreadsheet_rows = _filter_drive_rows_by_tokens(spreadsheet_rows, contact_entity_tokens)

    if spreadsheet_rows and _is_structured_spreadsheet_lookup_query(query):
        # Structured table answers should come from structured row records. A
        # later Drive-wide re-rank may surface legacy spreadsheet chunks or
        # unrelated documents above the authoritative row, which regresses
        # escalation matrix and store/contract lookup answers.
        return spreadsheet_rows[:page_size]

    # Normalize whitespace in chunk_text before matching — pypdf sometimes produces
    # double-spaced text (e.g. "Oracle  POS  and  Cash  Drawers") for certain PDFs.
    # REGEXP_REPLACE collapses any run of whitespace to a single space so single-spaced
    # queries reliably find the content. Candidate results are capped before ranking.
    sql = f"""
        SELECT REGEXP_REPLACE(chunk_text, r'\\s+', ' ') AS chunk_text,
               file_name, drive_url, source_path, chunk_index
        FROM `{GCP_PROJECT}.{BQ_DATASET}.drive_chunks`
        WHERE CONTAINS_SUBSTR(LOWER(REGEXP_REPLACE(chunk_text, r'\\s+', ' ')), LOWER(@query))
          AND source_id IN UNNEST(@source_ids)
        ORDER BY file_name, chunk_index
        LIMIT {int(candidate_size)}
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("query", "STRING", query),
            bigquery.ArrayQueryParameter("source_ids", "STRING", allowed_source_ids),
        ]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
        chunk_rows = [
            {
                "chunk_text": r.chunk_text,
                "file_name":  r.file_name,
                "drive_url":  r.drive_url,
                "source_path": r.source_path,
                "chunk_index": r.chunk_index,
                "_drive_result_type": "chunk",
            }
            for r in rows
        ]
        combined_rows = chunk_rows + ranked_chunk_rows + spreadsheet_rows
        if contact_entity_tokens:
            entity_rows = _filter_drive_rows_by_tokens(combined_rows, contact_entity_tokens)
            if entity_rows:
                combined_rows = entity_rows
        if not spreadsheet_rows:
            return _rank_drive_exact_results(
                combined_rows,
                query,
                page_size,
            )

        return _rank_drive_exact_results(
            combined_rows,
            query,
            page_size,
        )
    except Exception as exc:
        logger.warning("search_drive_exact failed: %s", exc)
        return _rank_drive_exact_results(spreadsheet_rows, query, page_size)
