"""
scripts/fetch_gcp_pricing.py

Fetches current GCP pricing from the Billing Catalog API and compares against
our effective CAD rates. Runs daily via Cloud Scheduler.

What it does:
  1. Calls GCP Cloud Billing Catalog API (public, uses service account).
  2. Parses SKUs for Vertex AI (Gemini, embeddings) and Cloud Run.
  3. Maps stable skuId values → our runtime rate keys.
  4. Compares fetched prices with the latest confirmed BQ rates
     (fallback: BOOTSTRAP_RATES in cost_tracker.py).
  5. Writes result to BQ cost_pricing_history (one row per rate key per day).
  6. If any price changed: prints warning + writes Firestore notification.

Usage:
  python3 scripts/fetch_gcp_pricing.py             # full run
  python3 scripts/fetch_gcp_pricing.py --dry-run   # compare only, no BQ/Firestore writes
  python3 scripts/fetch_gcp_pricing.py --list-skus # print all Vertex AI SKUs (for SKU_MAP maintenance)

See COST_TRACKING_SPEC.md → Pricing Update Strategy.
"""

import argparse
import json
import sys
import os
from datetime import date, datetime, timezone
from uuid import uuid4

sys.path.insert(0, "backend")
from dotenv import load_dotenv
load_dotenv(override=True)

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account
import requests

from app.config import settings
from app.services.cost_tracker import BOOTSTRAP_RATES as DEFAULT_RATES  # noqa: N816 — fallback only

# ---------------------------------------------------------------------------
# GCP Billing Catalog API service IDs
# ---------------------------------------------------------------------------

SERVICES = {
    # Public Cloud Billing Catalog service identifiers:
    #   C7E2-9256-1C43 = Vertex AI
    #   152E-C115-5142 = Cloud Run
    "vertex_ai":  "C7E2-9256-1C43",
    "cloud_run":  "152E-C115-5142",
}

CATALOG_URL = "https://cloudbilling.googleapis.com/v1/services/{service_id}/skus"

# ---------------------------------------------------------------------------
# RATE_SPECS: stable skuId first, description fallback second.
#
# The Catalog API returns USD list prices. Our runtime uses effective CAD rates
# (`*_per_m`) derived from USD × `currency_conversion_rate`, so this script
# converts fetched USD → effective CAD before comparing.
#
# `multiplier` converts the per-token raw unit price into our canonical unit
# (per 1M tokens). For these SKUs, raw unit price is per token/count.
# ---------------------------------------------------------------------------

RATE_SPECS = [
    {
        "rate_key": "gemini_input_per_m",
        "usd_key": "gemini_input_usd_per_m",
        "sku_id": "FDAB-647C-5A22",
        "desc_fallback": "Gemini 2.5 Flash GA Text Input - Predictions",
        "multiplier": 1_000_000,
    },
    {
        "rate_key": "gemini_output_per_m",
        "usd_key": "gemini_output_usd_per_m",
        "sku_id": "AF56-1BF9-492A",
        "desc_fallback": "Gemini 2.5 Flash GA Text Output - Predictions",
        "multiplier": 1_000_000,
    },
    {
        "rate_key": "gemini_thinking_per_m",
        "usd_key": "gemini_output_usd_per_m",
        "sku_id": "CD33-11F4-1220",
        "desc_fallback": "Gemini 2.5 Flash GA Thinking Text Output - Predictions",
        "multiplier": 1_000_000,
    },
    {
        "rate_key": "embedding_per_m",
        "usd_key": "embedding_usd_per_m",
        "sku_id": "E7B1-197A-4394",
        "desc_fallback": "Embeddings for Text - Predictions",
        "multiplier": 1_000_000,
    },
]

# rate_key → human label for display
RATE_LABELS = {
    "gemini_input_per_m":       "Gemini 2.5 Flash input   (CAD/1M tokens)",
    "gemini_output_per_m":      "Gemini 2.5 Flash output  (CAD/1M tokens)",
    "gemini_thinking_per_m":    "Gemini 2.5 Flash thinking(CAD/1M tokens)",
    "embedding_per_m":          "Text embeddings           (CAD/1M tokens)",
    "vertex_search_per_query":  "Vertex AI search          (CAD/query)",
    "cloud_run_idle_per_hour":  "Cloud Run idle            (CAD/hour)",
}

# Threshold: if fetched price differs from DEFAULT_RATES by more than this → flag as changed
CHANGE_THRESHOLD_PCT = 1.0  # 1% — ignore float noise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_auth_token() -> str:
    """Get Bearer token using service account credentials."""
    creds_path = settings.google_application_credentials
    if creds_path:
        creds = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    else:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)
    return creds.token


def _fetch_skus(service_id: str, token: str) -> list[dict]:
    """
    Fetch all SKUs for a GCP service. Handles pagination.

    Practical requirements verified on 2026-04-20:
      - caller must present identity (Bearer token or API key)
      - Cloud Billing API must be ENABLED on the caller project

    The Catalog data itself is public, but unauthenticated callers receive
    "Method doesn't allow unregistered callers", and authenticated callers
    receive 403 if `cloudbilling.googleapis.com` is disabled on their project.
    """
    skus: list[dict] = []
    url = CATALOG_URL.format(service_id=service_id)
    params: dict = {"currencyCode": "USD"}

    while True:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        if resp.status_code == 403:
            body = resp.text or ""
            print()
            print("ERROR: 403 Forbidden from Cloud Billing Catalog API.")
            print()
            if "Method doesn't allow unregistered callers" in body:
                print("Caller has no established identity. Use a Bearer token or API key.")
            elif "Cloud Billing API has not been used in project" in body or "is disabled" in body:
                print("Cloud Billing API is disabled on the caller project.")
                print()
                print("Fix:")
                print("  gcloud services enable cloudbilling.googleapis.com \\")
                print(f"    --project={settings.google_cloud_project_id}")
            else:
                print(body[:800])
            print()
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
        skus.extend(data.get("skus", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token

    return skus


def _extract_price(sku: dict) -> float | None:
    """
    Extract the first-tier USD price from a SKU dict.
    Returns price per GCP's native unit (could be per token, per 1M tokens, etc.).
    Returns None if no USD pricing found.
    """
    for info in sku.get("pricingInfo", []):
        expr = info.get("pricingExpression", {})
        for tier in expr.get("tieredRates", []):
            unit_price = tier.get("unitPrice", {})
            if unit_price.get("currencyCode") != "USD":
                continue
            units = float(unit_price.get("units", 0) or 0)
            nanos = float(unit_price.get("nanos", 0) or 0)
            price = units + nanos / 1_000_000_000
            if price > 0:
                return price
    return None


def _sku_matches(sku_desc: str, contains1: str, contains2: str | None) -> bool:
    """Check if SKU description contains both keywords (case-insensitive)."""
    desc = sku_desc.lower()
    if contains2:
        return contains1.lower() in desc and contains2.lower() in desc
    return contains1.lower() in desc


def _find_rate_sku(va_skus_regional: list[dict], sku_id: str, desc_fallback: str) -> dict | None:
    """Find a pricing SKU by stable skuId first, exact description second."""
    for sku in va_skus_regional:
        if sku.get("skuId") == sku_id:
            return sku
    for sku in va_skus_regional:
        if sku.get("description", "").strip() == desc_fallback.strip():
            return sku
    return None


def _pct_diff(a: float, b: float) -> float:
    """Percentage difference: (a - b) / b × 100. Returns 0 if b==0."""
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _load_confirmed_rates_from_bq() -> tuple[dict[str, float], str]:
    """
    Return latest confirmed effective CAD rates from BQ cost_pricing_history.
    Falls back to BOOTSTRAP_RATES when no confirmed rows exist yet.
    """
    from google.cloud import bigquery

    project = settings.google_cloud_project_id
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)

    client = bigquery.Client(project=project)
    query = f"""
        WITH ranked AS (
          SELECT
            rate_key,
            current_rate,
            rates_version,
            effective_date,
            recorded_at,
            ROW_NUMBER() OVER (
              PARTITION BY rate_key
              ORDER BY effective_date DESC NULLS LAST, recorded_at DESC
            ) AS rn
          FROM `{project}.esip_reference.cost_pricing_history`
          WHERE confirmed_by_admin = TRUE
            AND rate_key IS NOT NULL
        )
        SELECT rate_key, current_rate, rates_version, effective_date, recorded_at
        FROM ranked
        WHERE rn = 1
    """
    try:
        rows = list(client.query(query).result())
    except Exception as exc:
        print(f"WARNING: could not load confirmed BQ pricing rows — using BOOTSTRAP_RATES ({exc})")
        return dict(DEFAULT_RATES), str(DEFAULT_RATES.get("rates_effective_date", "unknown"))

    if not rows:
        print("WARNING: no confirmed pricing rows in BQ — using BOOTSTRAP_RATES")
        return dict(DEFAULT_RATES), str(DEFAULT_RATES.get("rates_effective_date", "unknown"))

    baseline = dict(DEFAULT_RATES)
    version = str(DEFAULT_RATES.get("rates_effective_date", "unknown"))
    latest_marker: tuple[str, str] | None = None
    for row in rows:
        try:
            if row.rate_key in baseline and row.current_rate is not None:
                baseline[row.rate_key] = float(row.current_rate)
        except (TypeError, ValueError):
            pass
        marker = (
            str(getattr(row, "effective_date", "") or ""),
            str(getattr(row, "recorded_at", "") or ""),
        )
        if getattr(row, "rates_version", None) and (latest_marker is None or marker > latest_marker):
            latest_marker = marker
            version = str(row.rates_version)
    baseline["rates_effective_date"] = version
    return baseline, version


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def fetch_and_compare(dry_run: bool = False, list_skus: bool = False) -> dict[str, dict]:
    """
    Fetch current GCP prices and compare to DEFAULT_RATES.
    Returns dict: {rate_key: {fetched, current, changed, pct_diff}}
    """
    baseline_rates, baseline_version = _load_confirmed_rates_from_bq()
    print(f"Using baseline rates version: {baseline_version}")
    print("Fetching GCP auth token...")
    token = _get_auth_token()

    print("Fetching Vertex AI SKUs...")
    va_skus = _fetch_skus(SERVICES["vertex_ai"], token)
    print(f"  Found {len(va_skus)} Vertex AI SKUs")

    if list_skus:
        print("\n--- ALL VERTEX AI SKUs (for SKU_MAP maintenance) ---")
        for sku in va_skus:
            price = _extract_price(sku)
            regions = sku.get("serviceRegions", [])
            print(f"  [{', '.join(regions)}] {sku['description']!r:70s}  price={price}")
        return {}

    # Filter to us-central1 (our region) — some SKUs are global
    def in_our_region(sku: dict) -> bool:
        regions = sku.get("serviceRegions", [])
        return not regions or "us-central1" in regions or "global" in " ".join(regions).lower()

    va_skus_regional = [s for s in va_skus if in_our_region(s)]
    print(f"  {len(va_skus_regional)} SKUs in us-central1 / global")

    results: dict[str, dict] = {}

    conversion_rate = float(baseline_rates.get("currency_conversion_rate", 1.0))

    # Match SKUs against RATE_SPECS (stable skuId primary, description fallback)
    for spec in RATE_SPECS:
        rate_key = spec["rate_key"]
        usd_key = spec["usd_key"]
        multiplier = spec["multiplier"]
        sku = _find_rate_sku(va_skus_regional, spec["sku_id"], spec["desc_fallback"])
        if not sku:
            print(f"  WARNING: no SKU matched for rate_key={rate_key!r} "
                  f"(looking for skuId={spec['sku_id']!r} / desc={spec['desc_fallback']!r})")
            results[rate_key] = {
                "fetched":  None,
                "current":  baseline_rates.get(rate_key),
                "changed":  False,
                "pct_diff": 0.0,
                "sku_desc": None,
                "note":     "SKU not found — check --list-skus / skuId map",
            }
            continue
        raw_price = _extract_price(sku)

        if raw_price is None:
            print(f"  WARNING: no USD price found for {sku['description']!r}")
            results[rate_key] = {
                "fetched":  None,
                "current":  baseline_rates.get(rate_key),
                "changed":  False,
                "pct_diff": 0.0,
                "sku_desc": sku["description"],
                "note":     "No USD price in SKU",
            }
            continue

        # GCP expresses Vertex AI token prices as "per 1 token" or "per 1M tokens"
        # Check usageUnit to determine actual unit
        usage_unit = ""
        for info in sku.get("pricingInfo", []):
            usage_unit = info.get("pricingExpression", {}).get("usageUnit", "")
            break

        fetched_usd = raw_price * multiplier
        fetched_effective = fetched_usd * conversion_rate

        current = baseline_rates.get(rate_key, 0.0)
        current_usd = baseline_rates.get(usd_key, 0.0)
        diff_pct = _pct_diff(fetched_effective, current)
        changed = abs(diff_pct) > CHANGE_THRESHOLD_PCT

        results[rate_key] = {
            "fetched":    fetched_effective,
            "current":    current,
            "changed":    changed,
            "pct_diff":   diff_pct,
            "sku_desc":   sku["description"],
            "sku_id":     sku.get("skuId"),
            "usage_unit": usage_unit,
            "raw_price":  raw_price,
            "fetched_usd": fetched_usd,
            "current_usd": current_usd,
            "note":       (
                f"USD list {fetched_usd:.6f}; conversion_rate={conversion_rate}; "
                f"skuId={sku.get('skuId')}"
            ),
            "baseline_version": baseline_version,
            "conversion_rate": conversion_rate,
        }

    return results


def print_report(results: dict[str, dict]) -> list[str]:
    """Print comparison table. Returns list of changed rate keys."""
    print()
    print(f"{'Rate':<44}  {'Current':>10}  {'Fetched':>10}  {'Diff':>8}  Status")
    print("-" * 90)
    changed_keys: list[str] = []
    for rate_key, r in results.items():
        label = RATE_LABELS.get(rate_key, rate_key)
        current = r["current"]
        fetched = r["fetched"]
        diff_pct = r["pct_diff"]
        if fetched is None:
            status = "⚠️  SKU not found"
            row = f"{label:<44}  {current:>10.5f}  {'N/A':>10}  {'N/A':>8}  {status}"
        elif r["changed"]:
            status = "🔴 CHANGED"
            changed_keys.append(rate_key)
            row = f"{label:<44}  {current:>10.5f}  {fetched:>10.5f}  {diff_pct:>+7.1f}%  {status}"
        else:
            status = "✅ OK"
            row = f"{label:<44}  {current:>10.5f}  {fetched:>10.5f}  {diff_pct:>+7.1f}%  {status}"
        print(row)
    print()
    return changed_keys


def write_to_bq(results: dict[str, dict], changed_keys: list[str]) -> None:
    """Write one row per rate key to cost_pricing_history BQ table."""
    from google.cloud import bigquery

    project = settings.google_cloud_project_id
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)

    client = bigquery.Client(project=project)
    table_id = f"{project}.esip_reference.cost_pricing_history"

    # Ensure table exists
    _ensure_pricing_history_table(client, project)

    now = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()
    rows: list[dict] = []

    for rate_key, r in results.items():
        if r["fetched"] is None:
            continue  # skip unmatched SKUs
        changed = bool(r["changed"])
        rows.append({
            "row_id":           str(uuid4()),
            "check_date":       today,
            "rate_key":         rate_key,
            "current_rate":     r["current"],
            "fetched_rate":     r["fetched"],
            "pct_diff":         r["pct_diff"],
            "changed":          changed,
            "sku_description":  r.get("sku_desc", ""),
            "source":           "gcp_catalog_api",
            "rates_version":    r.get("baseline_version", DEFAULT_RATES.get("rates_effective_date", "unknown")),
            "recorded_at":      now,
            "notes":            r.get("note", ""),
            "confirmed_by_admin": (not changed),
            "effective_date":   (today if not changed else None),
            "sku_id":           r.get("sku_id"),
            "current_usd":      r.get("current_usd"),
            "fetched_usd":      r.get("fetched_usd"),
            "currency_conversion_rate": float(r.get("conversion_rate", DEFAULT_RATES.get("currency_conversion_rate", 1.0))),
        })

    if not rows:
        print("No rows to write to BQ.")
        return

    errors = client.insert_rows_json(table_id, rows)
    if errors:
        print(f"BQ write errors: {errors}")
    else:
        print(f"Wrote {len(rows)} rows to {table_id}")


def _ensure_pricing_history_table(client, project: str) -> None:
    """Create cost_pricing_history table if it doesn't exist."""
    from google.cloud import bigquery as bq

    table_id = f"{project}.esip_reference.cost_pricing_history"
    schema = [
        bq.SchemaField("row_id",          "STRING",    mode="REQUIRED",
            description="UUID for this row"),
        bq.SchemaField("check_date",       "DATE",      mode="REQUIRED",
            description="Date this price check ran"),
        bq.SchemaField("rate_key",         "STRING",    mode="REQUIRED",
            description="Key in DEFAULT_RATES: gemini_input_per_m, embedding_per_m, etc."),
        bq.SchemaField("current_rate",     "FLOAT64",   mode="NULLABLE",
            description="Our current DEFAULT_RATES value"),
        bq.SchemaField("fetched_rate",     "FLOAT64",   mode="NULLABLE",
            description="Price fetched from GCP Billing Catalog API"),
        bq.SchemaField("pct_diff",         "FLOAT64",   mode="NULLABLE",
            description="(fetched - current) / current × 100"),
        bq.SchemaField("changed",          "BOOL",      mode="NULLABLE",
            description="True if |pct_diff| > 1%"),
        bq.SchemaField("sku_description",  "STRING",    mode="NULLABLE",
            description="GCP SKU description matched"),
        bq.SchemaField("source",           "STRING",    mode="NULLABLE",
            description="gcp_catalog_api | manual"),
        bq.SchemaField("rates_version",    "STRING",    mode="NULLABLE",
            description="rates_effective_date from DEFAULT_RATES at time of check"),
        bq.SchemaField("recorded_at",      "TIMESTAMP", mode="REQUIRED",
            description="UTC timestamp of this check"),
        bq.SchemaField("notes",            "STRING",    mode="NULLABLE"),
        bq.SchemaField("confirmed_by_admin", "BOOL",    mode="NULLABLE"),
        bq.SchemaField("effective_date",   "DATE",      mode="NULLABLE"),
        bq.SchemaField("sku_id",           "STRING",    mode="NULLABLE"),
        bq.SchemaField("current_usd",      "FLOAT64",   mode="NULLABLE"),
        bq.SchemaField("fetched_usd",      "FLOAT64",   mode="NULLABLE"),
        bq.SchemaField("currency_conversion_rate", "FLOAT64", mode="NULLABLE"),
    ]
    table_ref = bq.Table(table_id, schema=schema)
    table_ref.time_partitioning = bq.TimePartitioning(
        type_=bq.TimePartitioningType.DAY, field="check_date"
    )
    table_ref.description = (
        "Daily GCP pricing audit — one row per rate key per day. "
        "Tracks whether GCP Billing Catalog prices match our DEFAULT_RATES. "
        "See COST_TRACKING_SPEC.md → Pricing Update Strategy."
    )
    client.create_table(table_ref, exists_ok=True)


def write_firestore_alert(changed_keys: list[str], results: dict[str, dict]) -> None:
    """Write a notification to Firestore so Admin Panel can display the price change alert."""
    import firebase_admin
    from firebase_admin import credentials, firestore as fs

    creds_path = settings.google_application_credentials
    if not firebase_admin._apps:
        cred = credentials.Certificate(creds_path) if creds_path else credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)

    db = fs.client()

    changes = {}
    for k in changed_keys:
        r = results[k]
        changes[k] = {
            "current": r["current"],
            "fetched": r["fetched"],
            "current_usd": r.get("current_usd"),
            "fetched_usd": r.get("fetched_usd"),
            "sku_id": r.get("sku_id"),
            "pct_diff": round(r["pct_diff"], 2),
        }

    db.collection("system").document("cost_alerts").set({
        "pricing_change_detected": True,
        "pricing_change_date": date.today().isoformat(),
        "pricing_changes": changes,
        "message": (
            f"GCP price change detected for: {', '.join(changed_keys)}. "
            "Review in Admin Panel → Costs → Pricing."
        ),
    }, merge=True)

    print(f"Firestore alert written: system/cost_alerts.pricing_change_detected = true")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GCP pricing and compare to DEFAULT_RATES")
    parser.add_argument("--dry-run",   action="store_true", help="Compare only — no BQ or Firestore writes")
    parser.add_argument("--list-skus", action="store_true", help="Print all Vertex AI SKUs and exit")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN — no writes to BQ or Firestore\n")

    results = fetch_and_compare(dry_run=args.dry_run, list_skus=args.list_skus)
    if not results:
        return  # --list-skus mode

    changed_keys = print_report(results)

    if changed_keys:
        print(f"⚠️  PRICE CHANGE DETECTED for: {', '.join(changed_keys)}")
        print("   Review the diff above. To apply: update DEFAULT_RATES in cost_tracker.py")
        print("   and set rates_effective_date to today.")
    else:
        print("✅ All fetched prices match DEFAULT_RATES (within 1%).")

    if not args.dry_run:
        print("\nWriting to BQ cost_pricing_history...")
        write_to_bq(results, changed_keys)

        if changed_keys:
            print("Writing Firestore price-change alert...")
            write_firestore_alert(changed_keys, results)

    print("\nDone.")
    print(f"DEFAULT_RATES version: {DEFAULT_RATES.get('rates_effective_date', 'unknown')}")
    print(f"To check history: SELECT * FROM `{settings.google_cloud_project_id}.esip_reference.cost_pricing_history`")
    print(f"  WHERE check_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) ORDER BY check_date DESC, rate_key")


if __name__ == "__main__":
    main()
