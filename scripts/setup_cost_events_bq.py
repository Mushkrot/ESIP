"""
One-time setup: create all CIS (Cost Intelligence System) BQ tables.

Run from project root:
  python3 scripts/setup_cost_events_bq.py

Idempotent — safe to re-run: uses exists_ok=True semantics.

Tables created:
  cost_events          — append-only ledger of ESTIMATE + ACTUAL events (Phase 1)
  cost_billing_daily   — daily GCP billing reconciliation results (Phase 2)
  cost_pricing_history — log of GCP pricing fetches and changes (Phase 3)

See COST_TRACKING_SPEC.md for full architecture.
"""

import sys
sys.path.insert(0, "backend")

from dotenv import load_dotenv
load_dotenv(override=True)

from app.config import settings
from google.cloud import bigquery

PROJECT = settings.google_cloud_project_id
DATASET = "esip_reference"
TABLE   = "cost_events"
FULL_ID = f"{PROJECT}.{DATASET}.{TABLE}"


SCHEMA = [
    # --- Identity ---
    bigquery.SchemaField("event_id",            "STRING",    mode="REQUIRED",
        description="UUID — unique ID for this event row"),
    bigquery.SchemaField("correlation_id",       "STRING",    mode="REQUIRED",
        description="Links ESTIMATE + ACTUAL rows for the same call"),
    bigquery.SchemaField("event_type",           "STRING",    mode="REQUIRED",
        description="ESTIMATE | ACTUAL"),
    bigquery.SchemaField("call_type",            "STRING",    mode="REQUIRED",
        description="Matches CallType enum: ocr_image, chat_phase2, embed_ticket, etc."),
    bigquery.SchemaField("session_id",           "STRING",    mode="NULLABLE",
        description="Process UUID (script run) or Cloud Run request ID"),
    bigquery.SchemaField("environment",          "STRING",    mode="NULLABLE",
        description="cloud_run | script | local"),
    bigquery.SchemaField("timestamp",            "TIMESTAMP", mode="REQUIRED",
        description="UTC timestamp of the event"),

    # --- Estimate fields (present on ESTIMATE events) ---
    bigquery.SchemaField("est_cost_usd",         "FLOAT64",   mode="NULLABLE",
        description="Pre-action estimated cost in CAD (legacy _usd field name kept)"),
    bigquery.SchemaField("est_basis",            "STRING",    mode="NULLABLE",
        description="How estimate was derived: measured_avg | formula | hardcoded"),
    bigquery.SchemaField("est_input_tokens",     "INT64",     mode="NULLABLE",
        description="Estimated input token count (approx from text length)"),
    bigquery.SchemaField("est_output_tokens",    "INT64",     mode="NULLABLE",
        description="Estimated output token count"),

    # --- Actual fields (present on ACTUAL events) ---
    bigquery.SchemaField("act_input_tokens",     "INT64",     mode="NULLABLE",
        description="Real input tokens from API response.usage_metadata"),
    bigquery.SchemaField("act_output_tokens",    "INT64",     mode="NULLABLE",
        description="Real output tokens from API response"),
    bigquery.SchemaField("act_thinking_tokens",  "INT64",     mode="NULLABLE",
        description="Real thinking tokens (must be 0 for non-reasoning tasks)"),
    bigquery.SchemaField("act_cost_usd",         "FLOAT64",   mode="NULLABLE",
        description="Actual cost in CAD from real tokens × effective rates (legacy _usd field name kept)"),

    # --- Pricing snapshot at time of estimate ---
    bigquery.SchemaField("pricing_version",      "STRING",    mode="NULLABLE",
        description="rates_effective_date from DEFAULT_RATES (e.g. 2025-05-15)"),
    bigquery.SchemaField("price_input_per_m",    "FLOAT64",   mode="NULLABLE",
        description="Input token rate used (effective CAD per 1M tokens)"),
    bigquery.SchemaField("price_output_per_m",   "FLOAT64",   mode="NULLABLE",
        description="Output token rate used (effective CAD per 1M tokens)"),
    bigquery.SchemaField("price_thinking_per_m", "FLOAT64",   mode="NULLABLE",
        description="Thinking token rate used (effective CAD per 1M tokens)"),

    # --- Error metrics (on ACTUAL events) ---
    bigquery.SchemaField("estimate_error_pct",   "FLOAT64",   mode="NULLABLE",
        description="(est_cost - act_cost) / act_cost × 100. Positive = overestimate."),

    bigquery.SchemaField("notes",                "STRING",    mode="NULLABLE",
        description="Optional context: script name, ticket_id, batch size, etc."),
    bigquery.SchemaField("expected_cost_cad",   "FLOAT64",   mode="NULLABLE",
        description="CIS Phase 1 — formula/measured expected cost in CAD"),
    bigquery.SchemaField("reserved_cost_cad",   "FLOAT64",   mode="NULLABLE",
        description="CIS Phase 1 — safety margin passed to gate"),
    bigquery.SchemaField("actual_cost_cad",     "FLOAT64",   mode="NULLABLE",
        description="CIS Phase 1 — mirror of act_cost_usd (CAD); legacy _usd name kept"),
]

TIME_PARTITION = bigquery.TimePartitioning(
    type_=bigquery.TimePartitioningType.DAY,
    field="timestamp",
)


SCHEMA_BILLING_DAILY = [
    bigquery.SchemaField("row_id",           "STRING",    mode="REQUIRED",
        description="UUID for this row"),
    bigquery.SchemaField("date",             "DATE",      mode="REQUIRED",
        description="Date of reconciliation (usage date, not run date)"),
    bigquery.SchemaField("gcp_service",      "STRING",    mode="REQUIRED",
        description="GCP service name: Vertex AI, Cloud Run, etc."),
    bigquery.SchemaField("sku_id",           "STRING",    mode="NULLABLE",
        description="Stable GCP billing sku.id for reconciliation joins"),
    bigquery.SchemaField("sku_description",  "STRING",    mode="NULLABLE",
        description="GCP billing SKU description"),
    bigquery.SchemaField("call_type",        "STRING",    mode="NULLABLE",
        description="Our internal call_type attribution when one exists"),
    bigquery.SchemaField("gcp_billed_usd",   "FLOAT64",   mode="REQUIRED",
        description="Amount billed by GCP in CAD (legacy _usd field name kept)"),
    bigquery.SchemaField("our_actual_usd",   "FLOAT64",   mode="REQUIRED",
        description="Sum of cost_events ACTUAL rows in CAD (legacy _usd field name kept)"),
    bigquery.SchemaField("our_estimated_usd","FLOAT64",   mode="NULLABLE",
        description="Sum of cost_events ESTIMATE rows in CAD (legacy _usd field name kept)"),
    bigquery.SchemaField("discrepancy_usd",  "FLOAT64",   mode="NULLABLE",
        description="gcp_billed_usd - our_actual_usd, both CAD values despite legacy names"),
    bigquery.SchemaField("discrepancy_pct",  "FLOAT64",   mode="NULLABLE",
        description="(gcp_billed - our_actual) / gcp_billed × 100"),
    bigquery.SchemaField("discrepancy_explanation", "STRING", mode="NULLABLE",
        description="pricing_change | untracked_call_type | infra_overhead | billing_lag | unknown"),
    bigquery.SchemaField("billing_currency", "STRING",    mode="NULLABLE",
        description='Billing currency for this row, normally "CAD"'),
    bigquery.SchemaField("reconciled_at",    "TIMESTAMP", mode="NULLABLE",
        description="When reconcile_billing.py ran"),
    bigquery.SchemaField("alert_fired",      "BOOL",      mode="NULLABLE",
        description="True if |discrepancy_pct| > 20% and alert was sent"),
    bigquery.SchemaField("notes",            "STRING",    mode="NULLABLE"),
]

SCHEMA_PRICING_HISTORY = [
    bigquery.SchemaField("row_id",           "STRING",    mode="REQUIRED",
        description="UUID for this row"),
    bigquery.SchemaField("check_date",       "DATE",      mode="REQUIRED",
        description="Date this price check ran"),
    bigquery.SchemaField("rate_key",         "STRING",    mode="REQUIRED",
        description="Key in DEFAULT_RATES: gemini_input_per_m, embedding_per_m, etc."),
    bigquery.SchemaField("current_rate",     "FLOAT64",   mode="NULLABLE",
        description="Our effective CAD rate at time of check"),
    bigquery.SchemaField("fetched_rate",     "FLOAT64",   mode="NULLABLE",
        description="Catalog price converted to effective CAD before comparison"),
    bigquery.SchemaField("pct_diff",         "FLOAT64",   mode="NULLABLE",
        description="(fetched - current) / current × 100"),
    bigquery.SchemaField("changed",          "BOOL",      mode="NULLABLE",
        description="True if |pct_diff| > 1%"),
    bigquery.SchemaField("sku_description",  "STRING",    mode="NULLABLE",
        description="Matched GCP SKU description (skuId is the stable key in notes)"),
    bigquery.SchemaField("source",           "STRING",    mode="NULLABLE",
        description="gcp_catalog_api | manual"),
    bigquery.SchemaField("rates_version",    "STRING",    mode="NULLABLE",
        description="rates_effective_date from DEFAULT_RATES at time of check"),
    bigquery.SchemaField("recorded_at",      "TIMESTAMP", mode="REQUIRED",
        description="UTC timestamp of this check"),
    bigquery.SchemaField("notes",            "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("confirmed_by_admin", "BOOL",    mode="NULLABLE",
        description="True for bootstrap / admin-confirmed rate rows (CIS Phase 1)"),
    bigquery.SchemaField("effective_date",   "DATE",      mode="NULLABLE",
        description="Date these rates take effect"),
    bigquery.SchemaField("sku_id",           "STRING",    mode="NULLABLE",
        description="Stable GCP Billing Catalog skuId used for matching"),
    bigquery.SchemaField("current_usd",      "FLOAT64",   mode="NULLABLE",
        description="USD list-price basis of our current effective CAD rate"),
    bigquery.SchemaField("fetched_usd",      "FLOAT64",   mode="NULLABLE",
        description="USD list price fetched from Billing Catalog API before CAD conversion"),
    bigquery.SchemaField("currency_conversion_rate", "FLOAT64", mode="NULLABLE",
        description="USD->CAD multiplier applied when deriving effective CAD fetched_rate"),
]


def _alter_add_column_if_missing(client: bigquery.Client, table_id: str, name: str, sql_type: str) -> None:
    ddl = f"ALTER TABLE `{table_id}` ADD COLUMN IF NOT EXISTS `{name}` {sql_type}"
    try:
        client.query(ddl).result()
        print(f"    ALTER {table_id.split('.')[-1]}: +{name}")
    except Exception as exc:
        print(f"    ALTER {name} skipped: {exc}")


def ensure_cost_events_phase1_columns(client: bigquery.Client) -> None:
    tid = f"{PROJECT}.{DATASET}.{TABLE}"
    _alter_add_column_if_missing(client, tid, "expected_cost_cad", "FLOAT64")
    _alter_add_column_if_missing(client, tid, "reserved_cost_cad", "FLOAT64")
    _alter_add_column_if_missing(client, tid, "actual_cost_cad", "FLOAT64")


def ensure_cost_billing_phase1_columns(client: bigquery.Client) -> None:
    tid = f"{PROJECT}.{DATASET}.cost_billing_daily"
    _alter_add_column_if_missing(client, tid, "sku_id", "STRING")
    _alter_add_column_if_missing(client, tid, "call_type", "STRING")
    _alter_add_column_if_missing(client, tid, "discrepancy_explanation", "STRING")
    _alter_add_column_if_missing(client, tid, "billing_currency", "STRING")


def ensure_cost_pricing_phase1_columns(client: bigquery.Client) -> None:
    tid = f"{PROJECT}.{DATASET}.cost_pricing_history"
    _alter_add_column_if_missing(client, tid, "confirmed_by_admin", "BOOL")
    _alter_add_column_if_missing(client, tid, "effective_date", "DATE")
    _alter_add_column_if_missing(client, tid, "sku_id", "STRING")
    _alter_add_column_if_missing(client, tid, "current_usd", "FLOAT64")
    _alter_add_column_if_missing(client, tid, "fetched_usd", "FLOAT64")
    _alter_add_column_if_missing(client, tid, "currency_conversion_rate", "FLOAT64")


def insert_bootstrap_pricing_rows(client: bigquery.Client) -> None:
    """One-time bootstrap so cost_tracker can load rates from BQ (COST_TRACKING_SPEC Phase 2.5)."""
    from datetime import date, datetime, timezone
    from uuid import uuid4

    from app.services.cost_tracker import BOOTSTRAP_RATES

    hist = f"{PROJECT}.{DATASET}.cost_pricing_history"
    ver = str(BOOTSTRAP_RATES.get("rates_effective_date", "unknown"))
    q = f"""
        SELECT COUNT(1) AS c
        FROM `{hist}`
        WHERE source = 'bootstrap' AND rates_version = @rates_version
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("rates_version", "STRING", ver),
        ]
    )
    cnt = list(client.query(q, job_config=cfg).result())[0].c or 0
    if int(cnt) > 0:
        print(f"  {hist}: bootstrap rows already present for version {ver} ({cnt}) — skip insert.")
        return

    now = datetime.now(timezone.utc)
    check = date.today()
    effective_date = ver if len(ver) == 10 and ver[4] == "-" and ver[7] == "-" else check.isoformat()
    rate_keys = [
        "gemini_input_per_m",
        "gemini_output_per_m",
        "gemini_thinking_per_m",
        "embedding_per_m",
        "vertex_search_per_query",
        "cloud_run_idle_per_hour",
    ]
    usd_key_map = {
        "gemini_input_per_m": "gemini_input_usd_per_m",
        "gemini_output_per_m": "gemini_output_usd_per_m",
        "gemini_thinking_per_m": "gemini_output_usd_per_m",
        "embedding_per_m": "embedding_usd_per_m",
    }
    rows = []
    for rk in rate_keys:
        if rk not in BOOTSTRAP_RATES:
            continue
        rows.append({
            "row_id":               str(uuid4()),
            "check_date":           check.isoformat(),
            "rate_key":             rk,
            "current_rate":         float(BOOTSTRAP_RATES[rk]),
            "fetched_rate":         None,
            "pct_diff":             None,
            "changed":              None,
            "sku_description":      None,
            "source":               "bootstrap",
            "rates_version":        ver,
            "recorded_at":          now.isoformat(),
            "notes":                "CIS Phase 1 bootstrap from BOOTSTRAP_RATES",
            "confirmed_by_admin":   True,
            "effective_date":       effective_date,
            "sku_id":               None,
            "current_usd":          (
                float(BOOTSTRAP_RATES[usd_key_map[rk]])
                if rk in usd_key_map and usd_key_map[rk] in BOOTSTRAP_RATES
                else None
            ),
            "fetched_usd":          None,
            "currency_conversion_rate": float(BOOTSTRAP_RATES.get("currency_conversion_rate", 1.0)),
        })
    errors = client.insert_rows_json(hist, rows)
    if errors:
        raise RuntimeError(f"Bootstrap pricing insert errors: {errors[:5]}")
    print(f"  Inserted {len(rows)} bootstrap pricing row(s) into {hist}.")


def _create_table(client, full_id: str, schema: list, partition_field: str, description: str) -> None:
    table_ref = bigquery.Table(full_id, schema=schema)
    table_ref.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field=partition_field,
    )
    table_ref.description = description
    client.create_table(table_ref, exists_ok=True)
    print(f"  {full_id}  ({len(schema)} fields, partitioned by {partition_field})")


def main():
    if settings.google_application_credentials:
        import os
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.google_application_credentials

    client = bigquery.Client(project=PROJECT)

    # Ensure dataset exists
    dataset_ref = bigquery.Dataset(f"{PROJECT}.{DATASET}")
    # Keep this as "US" for the existing esip_reference dataset. Google is
    # realigning the display wording for US/EU locations in 2026, but current
    # BigQuery APIs still use "US".
    dataset_ref.location = "US"
    try:
        client.get_dataset(dataset_ref)
        print(f"Dataset {DATASET} already exists.")
    except Exception:
        client.create_dataset(dataset_ref, exists_ok=True)
        print(f"Dataset {DATASET} created.")

    print("Creating CIS tables (idempotent):")

    _create_table(
        client, FULL_ID, SCHEMA, "timestamp",
        "CIS Phase 1 — append-only ledger of ESTIMATE + ACTUAL cost events. "
        "See COST_TRACKING_SPEC.md.",
    )
    _create_table(
        client, f"{PROJECT}.{DATASET}.cost_billing_daily", SCHEMA_BILLING_DAILY, "date",
        "CIS Phase 2 — daily GCP billing reconciliation. "
        "One row per day per GCP service. See COST_TRACKING_SPEC.md.",
    )
    _create_table(
        client, f"{PROJECT}.{DATASET}.cost_pricing_history", SCHEMA_PRICING_HISTORY, "check_date",
        "CIS Phase 3 — daily GCP pricing audit. "
        "One row per rate key per day from fetch_gcp_pricing.py. See COST_TRACKING_SPEC.md.",
    )

    print()
    print("Phase 1 schema / bootstrap (idempotent):")
    ensure_cost_events_phase1_columns(client)
    ensure_cost_billing_phase1_columns(client)
    ensure_cost_pricing_phase1_columns(client)
    insert_bootstrap_pricing_rows(client)

    print()
    print("All 3 CIS tables ready.")
    print()
    print("Phase 1 (live):  cost_events          — written by cost_tracker.py on every Gemini call")
    print("Phase 2 (Mon):   cost_billing_daily   — written by scripts/reconcile_billing.py (needs bigquery.jobUser)")
    print("Phase 3 (Mon):   cost_pricing_history — written by scripts/fetch_gcp_pricing.py  (needs billing.viewer)")


if __name__ == "__main__":
    main()
