"""
Import Cloud Build charges from the org GCP Billing Export (BigQuery) into Firestore
estimated counters (`cloud_build` call type), so Admin → Costs → By call type matches GCP.

Uses a Firestore ledger (`system/cost_cloud_build_bq_ledger`) to store the last seen BQ
total per Eastern calendar day and only applies positive deltas — avoids double-counting
on repeated syncs.

Manual POST /cost/cloud-build is still available for same-day estimates before billing
export lands; avoid duplicating the same build both manually and after sync.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from google.cloud import bigquery as bq
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

LEDGER_DOC = "system/cost_cloud_build_bq_ledger"

_BILLING_TABLE = f"`{settings.gcp_billing_export_table}`"
_PROJECT_ID = "esip-reference-project"


def _query_cloud_build_by_day(client: bq.Client, from_d: date, to_d: date) -> dict[str, float]:
    sql = f"""
        SELECT
            FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time, 'America/New_York')) AS day,
            SUM(cost) AS cost_cad
        FROM {_BILLING_TABLE}
        WHERE
            project.id = @project_id
            AND LOWER(TRIM(service.description)) LIKE '%cloud build%'
            AND DATE(usage_start_time, 'America/New_York') BETWEEN @from_date AND @to_date
        GROUP BY day
        ORDER BY day
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("project_id", "STRING", _PROJECT_ID),
            bq.ScalarQueryParameter("from_date", "DATE", from_d.isoformat()),
            bq.ScalarQueryParameter("to_date", "DATE", to_d.isoformat()),
        ]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    return {str(row.day): float(row.cost_cad or 0.0) for row in rows}


def sync_cloud_build_from_billing(*, max_days: int = 14) -> dict[str, Any]:
    """
    Pull Cloud Build line items for ``max_days`` ending today (Eastern), apply deltas to
    ``cost_counters/{YYYY-MM-DD}`` via ``record_flat_cost(..., counter_date_key=...)``.
    """
    from app.services.bigquery import _get_client  # noqa: PLC0415
    from app.services import firestore  # noqa: PLC0415
    from app.services.cost_tracker import CallType, record_flat_cost  # noqa: PLC0415

    BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
    now_d = datetime.now(BUSINESS_TIMEZONE).date()
    from_d = now_d - timedelta(days=max(1, min(max_days, 60)))

    client = _get_client()
    bq_by_day = _query_cloud_build_by_day(client, from_d, now_d)

    db = firestore.get_db()
    led_ref = db.document(LEDGER_DOC)
    snap = led_ref.get()
    ledger = (snap.to_dict() or {}) if snap.exists else {}
    raw_totals = ledger.get("day_totals") or {}
    day_totals: dict[str, float] = {str(k): float(v) for k, v in raw_totals.items()}

    imported = 0.0
    days_touched: list[str] = []

    for day, bq_total in sorted(bq_by_day.items()):
        prev = float(day_totals.get(day, 0.0))
        delta = round(bq_total - prev, 8)
        if delta > 1e-9:
            record_flat_cost(
                CallType.CLOUD_BUILD,
                cost_usd=delta,
                units=1,
                counter_date_key=day,
            )
            day_totals[day] = bq_total
            imported += delta
            days_touched.append(day)
        elif delta < -1e-9:
            logger.warning(
                "cloud_build billing sync: day %s BQ total dropped %.6f -> %.6f (ledger only)",
                day,
                prev,
                bq_total,
            )
            day_totals[day] = bq_total

    led_ref.set(
        {
            "day_totals":   day_totals,
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
            "last_from":    from_d.isoformat(),
            "last_to":      now_d.isoformat(),
        },
        merge=True,
    )

    logger.info(
        "cloud_build billing sync: imported=$%.6f days=%s bq_rows=%d",
        imported,
        days_touched,
        len(bq_by_day),
    )
    return {
        "ok": True,
        "imported_cost": round(imported, 6),
        "days_updated": days_touched,
        "bq_days_with_cost": len(bq_by_day),
    }
