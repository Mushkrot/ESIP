"""Deterministic analytics for Zendesk organization POS Type profiles."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery

from app.services import bigquery as bq_service

BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_LIMIT = 10
OPEN_STATUSES = ("new", "open", "pending", "hold")


def _parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _effective_end_date(value: date | None) -> date:
    return value or datetime.now(BUSINESS_TIMEZONE).date()


def _date_window(start_date: date | None, end_date: date | None) -> dict[str, str | None]:
    return {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "timezone": "America/New_York",
    }


def _date_filters(start_date: date | None, end_date: date | None) -> str:
    clauses: list[str] = []
    if start_date:
        clauses.append("DATE(t.created_at, 'America/New_York') >= @start_date")
    if end_date:
        clauses.append("DATE(t.created_at, 'America/New_York') <= @end_date")
    if not clauses:
        return ""
    return " AND " + " AND ".join(clauses)


def _clean_status_filter(status_filter: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    return tuple(
        str(value).casefold()
        for value in (status_filter or ())
        if str(value or "").strip()
    )


def _status_filter_clause(status_filter: tuple[str, ...] | list[str] | None) -> str:
    if not _clean_status_filter(status_filter):
        return ""
    return """
      AND LOWER(COALESCE(t.status, '')) IN UNNEST(@status_filter)
    """


def _brand_filter() -> str:
    return """
      AND (
        @brand IS NULL
        OR REGEXP_REPLACE(LOWER(COALESCE(store_profiles.brand, '')), r'[^a-z0-9]', '')
          LIKE CONCAT('%', REGEXP_REPLACE(LOWER(@brand), r'[^a-z0-9]', ''), '%')
        OR REGEXP_REPLACE(LOWER(@brand), r'[^a-z0-9]', '')
          LIKE CONCAT('%', REGEXP_REPLACE(LOWER(COALESCE(store_profiles.brand, '')), r'[^a-z0-9]', ''), '%')
      )
    """


def _single_pos_type_cte(
    start_date: date | None,
    end_date: date | None,
    status_filter: tuple[str, ...] | list[str] | None = None,
) -> str:
    return f"""
    WITH store_profiles AS (
      SELECT
        org_id,
        org_name,
        store_id,
        brand,
        pos_type
      FROM `{bq_service.zendesk_organization_profiles_table_id()}` store_profiles
      WHERE store_profiles.pos_type IS NOT NULL
        AND LOWER(store_profiles.pos_type) = LOWER(@pos_type)
        {_brand_filter()}
    ),
    ticket_matches AS (
      SELECT
        'org_id' AS match_method,
        store_profiles.org_id AS profile_org_id,
        store_profiles.org_name AS profile_org_name,
        store_profiles.store_id AS profile_store_id,
        store_profiles.brand AS profile_brand,
        store_profiles.pos_type AS profile_pos_type,
        t.id,
        t.subject,
        t.status,
        t.created_at,
        t.updated_at,
        t.org_id AS ticket_org_id,
        t.store_id AS ticket_store_id,
        t.issue_category,
        t.product_type,
        t.caused_by
      FROM `{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets` t
      JOIN store_profiles
        ON t.org_id = store_profiles.org_id
      WHERE TRUE
        {_date_filters(start_date, end_date)}
        {_status_filter_clause(status_filter)}

      UNION ALL

      SELECT
        'store_id_fallback' AS match_method,
        store_profiles.org_id AS profile_org_id,
        store_profiles.org_name AS profile_org_name,
        store_profiles.store_id AS profile_store_id,
        store_profiles.brand AS profile_brand,
        store_profiles.pos_type AS profile_pos_type,
        t.id,
        t.subject,
        t.status,
        t.created_at,
        t.updated_at,
        t.org_id AS ticket_org_id,
        t.store_id AS ticket_store_id,
        t.issue_category,
        t.product_type,
        t.caused_by
      FROM `{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets` t
      JOIN store_profiles
        ON t.org_id IS NULL
       AND t.store_id IS NOT NULL
       AND store_profiles.store_id IS NOT NULL
       AND t.store_id = store_profiles.store_id
      WHERE TRUE
        {_date_filters(start_date, end_date)}
        {_status_filter_clause(status_filter)}
    )
    """


def _comparison_cte(
    start_date: date | None,
    end_date: date | None,
    status_filter: tuple[str, ...] | list[str] | None = None,
) -> str:
    return f"""
    WITH store_profiles AS (
      SELECT
        org_id,
        org_name,
        store_id,
        brand,
        pos_type
      FROM `{bq_service.zendesk_organization_profiles_table_id()}` store_profiles
      WHERE store_profiles.pos_type IS NOT NULL
        AND LOWER(store_profiles.pos_type) IN UNNEST(@pos_type_keys)
        {_brand_filter()}
    ),
    ticket_matches AS (
      SELECT
        'org_id' AS match_method,
        store_profiles.org_id AS profile_org_id,
        store_profiles.org_name AS profile_org_name,
        store_profiles.store_id AS profile_store_id,
        store_profiles.brand AS profile_brand,
        store_profiles.pos_type AS profile_pos_type,
        t.id,
        t.subject,
        t.status,
        t.created_at,
        t.updated_at,
        t.org_id AS ticket_org_id,
        t.store_id AS ticket_store_id,
        t.issue_category,
        t.product_type,
        t.caused_by
      FROM `{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets` t
      JOIN store_profiles
        ON t.org_id = store_profiles.org_id
      WHERE TRUE
        {_date_filters(start_date, end_date)}
        {_status_filter_clause(status_filter)}

      UNION ALL

      SELECT
        'store_id_fallback' AS match_method,
        store_profiles.org_id AS profile_org_id,
        store_profiles.org_name AS profile_org_name,
        store_profiles.store_id AS profile_store_id,
        store_profiles.brand AS profile_brand,
        store_profiles.pos_type AS profile_pos_type,
        t.id,
        t.subject,
        t.status,
        t.created_at,
        t.updated_at,
        t.org_id AS ticket_org_id,
        t.store_id AS ticket_store_id,
        t.issue_category,
        t.product_type,
        t.caused_by
      FROM `{bq_service.GCP_PROJECT}.{bq_service.BQ_DATASET}.tickets` t
      JOIN store_profiles
        ON t.org_id IS NULL
       AND t.store_id IS NOT NULL
       AND store_profiles.store_id IS NOT NULL
       AND t.store_id = store_profiles.store_id
      WHERE TRUE
        {_date_filters(start_date, end_date)}
        {_status_filter_clause(status_filter)}
    )
    """


def _query_params(
    *,
    pos_type: str | None = None,
    pos_types: list[str] | None = None,
    start_date: date | None,
    end_date: date | None,
    brand: str | None,
    limit: int,
    recent_cutoff: date,
    status_filter: tuple[str, ...] | list[str] | None = None,
) -> list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]:
    params: list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter] = [
        bigquery.ScalarQueryParameter("brand", "STRING", brand),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
        bigquery.ScalarQueryParameter("recent_cutoff", "DATE", recent_cutoff),
        bigquery.ArrayQueryParameter(
            "status_filter",
            "STRING",
            [str(value).casefold() for value in (status_filter or ()) if str(value or "").strip()],
        ),
    ]
    if pos_type is not None:
        params.append(bigquery.ScalarQueryParameter("pos_type", "STRING", pos_type))
    if pos_types is not None:
        params.append(
            bigquery.ArrayQueryParameter(
                "pos_type_keys",
                "STRING",
                [value.casefold() for value in pos_types],
            )
        )
    if start_date:
        params.append(bigquery.ScalarQueryParameter("start_date", "DATE", start_date))
    if end_date:
        params.append(bigquery.ScalarQueryParameter("end_date", "DATE", end_date))
    return params


def build_pos_type_analytics_queries(
    pos_type: str,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    brand: str | None = None,
    status_filter: tuple[str, ...] | list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, tuple[str, list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    safe_limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    recent_cutoff = _effective_end_date(end) - timedelta(days=30)
    clean_status_filter = _clean_status_filter(status_filter)
    cte = _single_pos_type_cte(start, end, clean_status_filter)
    params = _query_params(
        pos_type=pos_type,
        start_date=start,
        end_date=end,
        brand=brand,
        limit=safe_limit,
        recent_cutoff=recent_cutoff,
        status_filter=clean_status_filter,
    )

    coverage_sql = cte + """
    SELECT
      (SELECT COUNT(DISTINCT org_id) FROM store_profiles) AS orgs_matched,
      COUNT(DISTINCT id) AS tickets_matched,
      COUNT(DISTINCT IF(match_method = 'org_id', id, NULL)) AS org_id_join_ticket_count,
      COUNT(DISTINCT IF(match_method = 'store_id_fallback', id, NULL)) AS store_id_fallback_ticket_count,
      (
        SELECT COUNT(*)
        FROM store_profiles store_profiles
        WHERE NOT EXISTS (
          SELECT 1
          FROM ticket_matches tm
          WHERE tm.profile_org_id = store_profiles.org_id
        )
      ) AS profile_orgs_without_tickets_count
    FROM ticket_matches
    """
    stores_sql = cte + f"""
    SELECT
      profile_org_id AS org_id,
      ANY_VALUE(profile_org_name) AS org_name,
      ANY_VALUE(profile_store_id) AS store_id,
      ANY_VALUE(profile_brand) AS brand,
      COUNT(DISTINCT id) AS ticket_count,
      COUNT(DISTINCT IF(status IN {OPEN_STATUSES}, id, NULL)) AS open_ticket_count,
      COUNT(DISTINCT IF(DATE(updated_at, 'America/New_York') >= @recent_cutoff, id, NULL)) AS recent_ticket_count,
      ARRAY_AGG(
        STRUCT(id, subject, status, created_at)
        ORDER BY created_at DESC
        LIMIT 5
      ) AS sample_tickets
    FROM ticket_matches
    GROUP BY profile_org_id
    ORDER BY ticket_count DESC, recent_ticket_count DESC, org_name
    LIMIT @limit
    """
    problems_sql = cte + f"""
    SELECT
      COALESCE(NULLIF(issue_category, ''), 'unknown') AS issue_category,
      COALESCE(NULLIF(product_type, ''), 'unknown') AS product_type,
      COALESCE(NULLIF(caused_by, ''), 'unknown') AS caused_by,
      COUNT(DISTINCT id) AS ticket_count,
      COUNT(DISTINCT profile_org_id) AS store_count,
      COUNT(DISTINCT IF(status IN {OPEN_STATUSES}, id, NULL)) AS open_ticket_count,
      ARRAY_AGG(
        STRUCT(id, subject, status, created_at)
        ORDER BY created_at DESC
        LIMIT 5
      ) AS subject_samples
    FROM ticket_matches
    GROUP BY issue_category, product_type, caused_by
    ORDER BY ticket_count DESC, store_count DESC, issue_category
    LIMIT @limit
    """
    open_tickets_sql = cte + f"""
    SELECT
      id,
      subject,
      status,
      profile_org_id AS org_id,
      profile_org_name AS org_name,
      profile_store_id AS store_id,
      profile_brand AS brand,
      issue_category,
      product_type,
      caused_by,
      created_at,
      updated_at,
      match_method
    FROM ticket_matches
    WHERE status IN {OPEN_STATUSES}
    ORDER BY updated_at DESC
    LIMIT @limit
    """
    return {
        "coverage": (coverage_sql, params),
        "repeated_stores": (stores_sql, params),
        "repeated_problems": (problems_sql, params),
        "open_tickets": (open_tickets_sql, params),
    }


def build_pos_type_comparison_query(
    pos_types: list[str],
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    brand: str | None = None,
    status_filter: tuple[str, ...] | list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[str, list[bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    safe_limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    recent_cutoff = _effective_end_date(end) - timedelta(days=30)
    clean_status_filter = _clean_status_filter(status_filter)
    cte = _comparison_cte(start, end, clean_status_filter)
    params = _query_params(
        pos_types=pos_types,
        start_date=start,
        end_date=end,
        brand=brand,
        limit=safe_limit,
        recent_cutoff=recent_cutoff,
        status_filter=clean_status_filter,
    )
    sql = cte + f"""
    SELECT *
    FROM (
      SELECT
        profile_pos_type AS pos_type,
        COALESCE(NULLIF(issue_category, ''), 'unknown') AS issue_category,
        COALESCE(NULLIF(product_type, ''), 'unknown') AS product_type,
        COALESCE(NULLIF(caused_by, ''), 'unknown') AS caused_by,
        COUNT(DISTINCT id) AS ticket_count,
        COUNT(DISTINCT profile_org_id) AS store_count,
        COUNT(DISTINCT IF(status IN {OPEN_STATUSES}, id, NULL)) AS open_ticket_count,
        ARRAY_AGG(
          STRUCT(id, subject, status, created_at)
          ORDER BY created_at DESC
          LIMIT 3
        ) AS subject_samples
      FROM ticket_matches
      GROUP BY pos_type, issue_category, product_type, caused_by
    )
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY pos_type
      ORDER BY ticket_count DESC, store_count DESC, issue_category
    ) <= @limit
    ORDER BY pos_type, ticket_count DESC, store_count DESC, issue_category
    """
    return sql, params


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if hasattr(row, "items"):
        return dict(row.items())
    return dict(row)


def _serialize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _query(client: bigquery.Client, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    job_config = bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=20 * 1024**3,
        use_query_cache=True,
    )
    return [
        _serialize(_row_to_dict(row))
        for row in client.query(sql, job_config=job_config).result()
    ]


def _empty_result(
    *,
    pos_type: str,
    start_date: date | None,
    end_date: date | None,
    brand: str | None,
    status_filter: tuple[str, ...] | list[str] | None,
    warning: str | None = None,
) -> dict[str, Any]:
    clean_status_filter = _clean_status_filter(status_filter)
    return {
        "available": warning is None,
        "warning": warning,
        "filters": {
            "pos_type": pos_type,
            "brand": brand,
            "status_filter": clean_status_filter,
            "date_window": _date_window(start_date, end_date),
        },
        "coverage": {
            "orgs_matched": 0,
            "tickets_matched": 0,
            "org_id_join_ticket_count": 0,
            "store_id_fallback_ticket_count": 0,
            "profile_orgs_without_tickets_count": 0,
            "date_window": _date_window(start_date, end_date),
        },
        "repeated_stores": [],
        "repeated_problems": [],
        "open_tickets": [],
    }


def get_pos_type_analytics(
    pos_type: str,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    brand: str | None = None,
    status_filter: tuple[str, ...] | list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    client: bigquery.Client | None = None,
) -> dict[str, Any]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if not str(pos_type or "").strip():
        raise ValueError("pos_type is required")
    queries = build_pos_type_analytics_queries(
        pos_type,
        start_date=start,
        end_date=end,
        brand=brand,
        status_filter=status_filter,
        limit=limit,
    )
    clean_status_filter = _clean_status_filter(status_filter)
    client = client or bq_service._get_client()
    try:
        coverage_rows = _query(client, *queries["coverage"])
        coverage = coverage_rows[0] if coverage_rows else {}
        coverage["date_window"] = _date_window(start, end)
        return {
            "available": True,
            "warning": None,
            "filters": {
                "pos_type": pos_type,
                "brand": brand,
                "status_filter": clean_status_filter,
                "date_window": _date_window(start, end),
            },
            "coverage": coverage,
            "repeated_stores": _query(client, *queries["repeated_stores"]),
            "repeated_problems": _query(client, *queries["repeated_problems"]),
            "open_tickets": _query(client, *queries["open_tickets"]),
        }
    except google_exceptions.NotFound:
        return _empty_result(
            pos_type=pos_type,
            start_date=start,
            end_date=end,
            brand=brand,
            status_filter=clean_status_filter,
            warning=(
                "Zendesk organization profile table is not available yet; "
                "run the organization profile sync before using POS Type analytics."
            ),
        )


def get_pos_type_comparison(
    pos_types: list[str],
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    brand: str | None = None,
    status_filter: tuple[str, ...] | list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    client: bigquery.Client | None = None,
) -> dict[str, Any]:
    clean_pos_types = [value.strip() for value in pos_types if str(value or "").strip()]
    if len(clean_pos_types) < 2:
        raise ValueError("At least two POS Type values are required for comparison")
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    sql, params = build_pos_type_comparison_query(
        clean_pos_types,
        start_date=start,
        end_date=end,
        brand=brand,
        status_filter=status_filter,
        limit=limit,
    )
    clean_status_filter = _clean_status_filter(status_filter)
    client = client or bq_service._get_client()
    rows = _query(client, sql, params)
    grouped: dict[str, list[dict[str, Any]]] = {value: [] for value in clean_pos_types}
    for row in rows:
        grouped.setdefault(str(row.get("pos_type") or ""), []).append(row)
    return {
        "available": True,
        "filters": {
            "pos_types": clean_pos_types,
            "brand": brand,
            "status_filter": clean_status_filter,
            "date_window": _date_window(start, end),
        },
        "problems_by_pos_type": grouped,
    }
