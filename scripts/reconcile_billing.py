"""
scripts/reconcile_billing.py

Service-level reconciliation between the org GCP Billing Export and our internal
`esip_reference.cost_events` ACTUAL / ESTIMATE rows.

This script is intentionally read/compare/write only:
  - it never changes runtime gates,
  - it never mutates Firestore counters used by Soft Cap / Hard Cap,
  - it only writes analytical rows to `esip_reference.cost_billing_daily`
    and optional non-blocking alerts.

Amounts are written in CAD (billing currency), while legacy `*_usd` field names
are preserved for backward compatibility with existing schema/API consumers.

BigQuery US/EU location and SKU display wording is being realigned by Google in
2026. Keep reconciliation joins on stable SKU IDs; treat SKU descriptions as
display/fallback text that can be renamed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

sys.path.insert(0, "backend")

from dotenv import load_dotenv

load_dotenv(override=True)

from google.cloud import bigquery

from app.config import settings
from app.services.email_service import send_alert

PROJECT_ID = settings.google_cloud_project_id
BILLING_TABLE = f"`{settings.gcp_billing_export_table}`"
RECON_TABLE = f"{PROJECT_ID}.esip_reference.cost_billing_daily"
DISCREPANCY_ALERT_THRESHOLD_PCT = 30.0

_SERVICE_CASE_SQL = """
CASE
  WHEN call_type = 'vertex_search' THEN 'Vertex AI Search'
  WHEN call_type = 'cloud_run_idle' THEN 'Cloud Run'
  WHEN call_type = 'cloud_build' THEN 'Cloud Build'
  WHEN call_type IN (
    'ocr_image', 'drive_ocr',
    'embed_ticket', 'embed_query', 'embed_drive',
    'chat_phase0', 'chat_phase1', 'chat_phase2',
    'analytics_sql', 'analytics_decision', 'analytics_answer',
    'summary', 'docs_gap',
    'universal_p1', 'universal_p2', 'universal_translate'
  ) THEN 'Vertex AI'
  ELSE 'Other / Unmapped'
END
"""

TRACKED_SERVICES = {"Vertex AI", "Vertex AI Search", "Cloud Run", "Cloud Build"}
ALERTABLE_SERVICES = {"Vertex AI Search", "Cloud Run", "Cloud Build"}
INFRA_SERVICES = {
    "BigQuery",
    "Cloud Storage",
    "Cloud Logging",
    "Artifact Registry",
    "Secret Manager",
    "Cloud Scheduler",
    "Cloud Functions",
    "Cloud SQL",
    "Networking",
}


def _bq_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=PROJECT_ID)


def _pct_diff(gcp_billed: float, our_actual: float) -> float:
    if abs(gcp_billed) < 1e-9:
        return 0.0
    return (gcp_billed - our_actual) / gcp_billed * 100.0


def _classify(service: str, billed: float, actual: float, est: float, top_skus: str) -> str:
    diff = abs(_pct_diff(billed, actual))
    if diff <= DISCREPANCY_ALERT_THRESHOLD_PCT:
        return "matched"
    if service in INFRA_SERVICES:
        return "infra_overhead"
    if service == "Vertex AI Search" and any(
        token in top_skus.lower() for token in ("storage", "import", "ingest")
    ):
        return "infra_overhead"
    if actual <= 1e-9 and billed > 1e-9:
        return "untracked_call_type"
    if actual > billed and est >= actual:
        return "billing_lag"
    return "unknown"


def _query_billing_by_service(client: bigquery.Client, from_date: date, to_date: date) -> dict[tuple[str, str], dict]:
    sql = f"""
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'America/New_York')) AS day,
          service.description AS gcp_service,
          SUM(cost) AS billed_cad,
          STRING_AGG(
            DISTINCT CONCAT(COALESCE(sku.id, ''), ' :: ', COALESCE(sku.description, '')),
            ' | '
            LIMIT 5
          ) AS top_skus
        FROM {BILLING_TABLE}
        WHERE
          project.id = @project_id
          AND DATE(usage_start_time, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day, gcp_service
        ORDER BY day, gcp_service
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("project_id", "STRING", PROJECT_ID),
            bigquery.ScalarQueryParameter("from_date", "DATE", from_date.isoformat()),
            bigquery.ScalarQueryParameter("to_date", "DATE", to_date.isoformat()),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        out[(str(row.day), str(row.gcp_service))] = {
            "billed_cad": float(row.billed_cad or 0.0),
            "top_skus": str(row.top_skus or ""),
        }
    return out


def _query_our_costs_by_service(client: bigquery.Client, from_date: date, to_date: date) -> dict[tuple[str, str], dict]:
    sql = f"""
        SELECT
          FORMAT_DATE('%Y-%m-%d', DATE(timestamp, 'America/New_York')) AS day,
          {_SERVICE_CASE_SQL} AS gcp_service,
          SUM(IF(event_type = 'ACTUAL', COALESCE(actual_cost_cad, act_cost_usd, 0), 0)) AS actual_cad,
          SUM(IF(event_type = 'ESTIMATE', COALESCE(expected_cost_cad, est_cost_usd, 0), 0)) AS estimated_cad,
          COUNT(DISTINCT IF(event_type = 'ACTUAL', call_type, NULL)) AS actual_call_types,
          STRING_AGG(DISTINCT IF(event_type = 'ACTUAL', call_type, NULL), ', ') AS call_types
        FROM `{PROJECT_ID}.esip_reference.cost_events`
        WHERE DATE(timestamp, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day, gcp_service
        ORDER BY day, gcp_service
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_date", "DATE", from_date.isoformat()),
            bigquery.ScalarQueryParameter("to_date", "DATE", to_date.isoformat()),
        ]
    )
    rows = list(client.query(sql, job_config=cfg).result())
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        call_type = None
        if int(row.actual_call_types or 0) == 1:
            call_type = str((row.call_types or "").strip() or "")
        out[(str(row.day), str(row.gcp_service))] = {
            "actual_cad": float(row.actual_cad or 0.0),
            "estimated_cad": float(row.estimated_cad or 0.0),
            "call_type": call_type or None,
            "call_types": str(row.call_types or ""),
        }
    return out


def _build_rows(
    billed_by_service: dict[tuple[str, str], dict],
    our_by_service: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    flagged: list[dict] = []
    keys = sorted(set(billed_by_service) | set(our_by_service))
    for day, service in keys:
        billed = billed_by_service.get((day, service), {})
        ours = our_by_service.get((day, service), {})
        billed_cad = float(billed.get("billed_cad", 0.0))
        actual_cad = float(ours.get("actual_cad", 0.0))
        estimated_cad = float(ours.get("estimated_cad", 0.0))
        diff_cad = billed_cad - actual_cad
        diff_pct = _pct_diff(billed_cad, actual_cad)
        top_skus = str(billed.get("top_skus", ""))
        explanation = _classify(service, billed_cad, actual_cad, estimated_cad, top_skus)
        should_alert = (
            service in ALERTABLE_SERVICES
            and abs(diff_pct) > DISCREPANCY_ALERT_THRESHOLD_PCT
            and explanation not in {"billing_lag"}
        )
        notes_parts = []
        if ours.get("call_types"):
            notes_parts.append(f"call_types={ours['call_types']}")
        if top_skus:
            notes_parts.append(f"top_skus={top_skus}")
        if service not in TRACKED_SERVICES and billed_cad > 0:
            notes_parts.append("not part of runtime gates; tracked only via billing export")

        row = {
            "row_id": str(uuid4()),
            "date": day,
            "gcp_service": service,
            "sku_id": None,
            "sku_description": None,
            "call_type": ours.get("call_type"),
            "gcp_billed_usd": billed_cad,
            "our_actual_usd": actual_cad,
            "our_estimated_usd": estimated_cad,
            "discrepancy_usd": diff_cad,
            "discrepancy_pct": diff_pct,
            "discrepancy_explanation": explanation,
            "billing_currency": "CAD",
            "reconciled_at": now,
            "alert_fired": should_alert,
            "notes": " | ".join(notes_parts) if notes_parts else None,
        }
        rows.append(row)
        if should_alert:
            flagged.append(row)
    return rows, flagged


def _replace_rows(client: bigquery.Client, from_date: date, to_date: date, rows: list[dict]) -> None:
    delete_sql = f"""
        DELETE FROM `{RECON_TABLE}`
        WHERE date BETWEEN @from_date AND @to_date
    """
    delete_cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("from_date", "DATE", from_date.isoformat()),
            bigquery.ScalarQueryParameter("to_date", "DATE", to_date.isoformat()),
        ]
    )
    client.query(delete_sql, job_config=delete_cfg).result()
    if not rows:
        return
    errors = client.insert_rows_json(RECON_TABLE, rows)
    if errors:
        raise RuntimeError(f"Failed to insert reconciliation rows: {errors[:5]}")


def _write_firestore_alert(flagged: list[dict]) -> None:
    from app.services.firestore import get_db

    get_db().document("system/cost_alerts").set(
        {
            "billing_reconciliation_alert": bool(flagged),
            "billing_reconciliation_at": datetime.now(timezone.utc).isoformat(),
            "billing_reconciliation_rows": [
                {
                    "date": row["date"],
                    "gcp_service": row["gcp_service"],
                    "gcp_billed_cad": round(float(row["gcp_billed_usd"]), 6),
                    "our_actual_cad": round(float(row["our_actual_usd"]), 6),
                    "discrepancy_pct": round(float(row["discrepancy_pct"]), 2),
                    "explanation": row["discrepancy_explanation"],
                }
                for row in flagged[:10]
            ],
        },
        merge=True,
    )


def _send_email_alert(flagged: list[dict]) -> None:
    if not flagged:
        return
    lines = [
        "Billing reconciliation exceeded threshold for tracked services:",
        "",
    ]
    for row in flagged[:10]:
        lines.append(
            f"- {row['date']} | {row['gcp_service']} | "
            f"billed=C${row['gcp_billed_usd']:.4f} | actual=C${row['our_actual_usd']:.4f} | "
            f"diff={row['discrepancy_pct']:+.1f}% | {row['discrepancy_explanation']}"
        )
    send_alert(
        "Billing reconciliation discrepancy detected",
        "\n".join(lines),
        level="warning",
    )


def run_reconciliation(*, from_date: date, to_date: date, dry_run: bool) -> dict:
    client = _bq_client()
    billed = _query_billing_by_service(client, from_date, to_date)
    ours = _query_our_costs_by_service(client, from_date, to_date)
    rows, flagged = _build_rows(billed, ours)
    if not dry_run:
        _replace_rows(client, from_date, to_date, rows)
        _write_firestore_alert(flagged)
        _send_email_alert(flagged)
    return {
        "ok": True,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "rows_written": len(rows),
        "flagged_rows": len(flagged),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile GCP billing export vs internal cost_events")
    parser.add_argument("--days", type=int, default=14, help="Rolling Eastern-date window ending today")
    parser.add_argument("--from-date", dest="from_date", default="", help="Optional YYYY-MM-DD override")
    parser.add_argument("--to-date", dest="to_date", default="", help="Optional YYYY-MM-DD override")
    parser.add_argument("--dry-run", action="store_true", help="Query and compare only; do not write")
    args = parser.parse_args()

    if args.from_date:
        from_date = date.fromisoformat(args.from_date)
    else:
        from_date = datetime.now(timezone.utc).date() - timedelta(days=max(1, min(args.days, 60)) - 1)
    if args.to_date:
        to_date = date.fromisoformat(args.to_date)
    else:
        to_date = datetime.now(timezone.utc).date()
    if to_date < from_date:
        raise SystemExit("--to-date must be >= --from-date")

    result = run_reconciliation(from_date=from_date, to_date=to_date, dry_run=args.dry_run)
    print(result)


if __name__ == "__main__":
    main()
