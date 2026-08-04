"""On-demand ESIP impact dashboard and report metrics."""

from __future__ import annotations

import math
import os
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from app.config import settings
from app.reference_data.fixtures import IDENTITY_DOMAIN, LEGACY_IDENTITY_ALIASES
from app.services import firestore, impact_telemetry

BUSINESS_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_ROLLOUT_DATE = date(2026, 5, 26)
BASELINE_DAYS = 28
RAMP_DAYS = 7
logger = logging.getLogger(__name__)


def _get_client() -> bigquery.Client:
    if settings.google_application_credentials:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.google_application_credentials)
    return bigquery.Client(project=settings.google_cloud_project_id)


def _parse_rollout_date(value: str | None) -> date:
    if not value:
        return DEFAULT_ROLLOUT_DATE
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return DEFAULT_ROLLOUT_DATE


def _allowed_domains() -> list[str]:
    domains = {
        str(domain or "").strip().lower().lstrip("@")
        for domain in settings.allowed_email_domains_list
        if str(domain or "").strip()
    }
    return sorted(domains)


def _email_domain_condition(expr: str) -> str:
    return f"LOWER(SPLIT(IFNULL({expr}, ''), '@')[SAFE_OFFSET(1)]) IN UNNEST(@allowed_domains)"


def resolve_windows(rollout_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    rollout = _parse_rollout_date(rollout_date)
    end_day = _parse_rollout_date(end_date) if end_date else datetime.now(BUSINESS_TIMEZONE).date()
    if end_day < rollout:
        end_day = rollout
    baseline_start = rollout - timedelta(days=BASELINE_DAYS)
    ramp_end = rollout + timedelta(days=RAMP_DAYS)
    post_start = ramp_end
    post_end = max(end_day + timedelta(days=1), post_start)
    post_started = end_day >= post_start
    ramp_day = max(0, min(RAMP_DAYS, (min(end_day, ramp_end - timedelta(days=1)) - rollout).days + 1))

    def to_utc(day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=BUSINESS_TIMEZONE).astimezone(ZoneInfo("UTC"))

    return {
        "rollout_date": rollout.isoformat(),
        "baseline": {
            "label": "Baseline",
            "start_date": baseline_start.isoformat(),
            "end_date": rollout.isoformat(),
            "start_at": to_utc(baseline_start),
            "end_at": to_utc(rollout),
        },
        "ramp": {
            "label": "Ramp-up",
            "start_date": rollout.isoformat(),
            "end_date": ramp_end.isoformat(),
            "start_at": to_utc(rollout),
            "end_at": to_utc(ramp_end),
        },
        "post": {
            "label": "Stable post-ramp",
            "start_date": post_start.isoformat(),
            "end_date": post_end.isoformat(),
            "start_at": to_utc(post_start),
            "end_at": to_utc(post_end),
        },
        "as_of_date": end_day.isoformat(),
        "post_started": post_started,
        "current_phase": "post" if post_started else "ramp",
        "display_period": "post" if post_started else "ramp",
        "ramp_day": ramp_day,
        "query_start_at": to_utc(baseline_start),
        "query_end_at": to_utc(post_end),
    }


def _param_windows(windows: dict[str, Any]) -> list[bigquery.ScalarQueryParameter]:
    return [
        bigquery.ScalarQueryParameter("start_at", "TIMESTAMP", windows["query_start_at"]),
        bigquery.ScalarQueryParameter("end_at", "TIMESTAMP", windows["query_end_at"]),
        bigquery.ScalarQueryParameter("rollout_date", "DATE", windows["rollout_date"]),
        bigquery.ScalarQueryParameter("ramp_end_date", "DATE", windows["ramp"]["end_date"]),
        bigquery.ArrayQueryParameter("allowed_domains", "STRING", _allowed_domains()),
    ]


def _query(client: bigquery.Client, sql: str, params: list[bigquery.ScalarQueryParameter]) -> list[dict[str, Any]]:
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(row.items()) for row in client.query(sql, job_config=job_config).result()]


def _public_warning(area: str, exc: Exception) -> str:
    logger.warning("impact_metrics: %s unavailable: %s", area, exc)
    messages = {
        "usage": "Some ESIP usage events could not be included. Usage counts may be understated.",
        "response_speed": "Response-speed metrics are not available for this date range. Other sections are still shown.",
        "first_action": "First-action timing is not available for this date range. Other sections are still shown.",
        "outcomes": "Solved-ticket outcome metrics are not available for this date range. Other sections are still shown.",
        "outstanding": "Current outstanding-ticket counts are not available. Historical sections are still shown.",
        "quality": "Quality guardrail metrics are not available for this date range. Other sections are still shown.",
        "per_user_reply": "Per-person reply-speed rows are not available for this date range. Overall sections are still shown.",
        "per_user_solved": "Per-person solved-ticket rows are not available for this date range. Overall sections are still shown.",
    }
    return messages.get(area, "One report section is temporarily unavailable. Other sections are still shown.")


def _empty_periods() -> dict[str, dict[str, Any]]:
    return {
        "baseline": {"label": "Baseline", "events": 0, "active_users": 0, "assisted_tickets": 0},
        "ramp": {"label": "Ramp-up", "events": 0, "active_users": 0, "assisted_tickets": 0},
        "post": {"label": "Stable post-ramp", "events": 0, "active_users": 0, "assisted_tickets": 0},
    }


def _bucket_expr(field: str) -> str:
    return f"""
        CASE
          WHEN DATE({field}, 'America/New_York') < @rollout_date THEN 'baseline'
          WHEN DATE({field}, 'America/New_York') < @ramp_end_date THEN 'ramp'
          ELSE 'post'
        END
    """


def _touches_cte() -> str:
    return f"""
      impact_events AS (
        SELECT
          created_at,
          LOWER(owner_email) AS owner_email,
          ticket_id,
          event_type,
          COALESCE(workflow,
            CASE
              WHEN event_type IN ('summary_viewed', 'summary_generated') THEN 'Summary'
              WHEN event_type IN ('customer_risk_viewed', 'customer_risk_refreshed') THEN 'Customer Risk'
              WHEN event_type IN ('similar_tickets_viewed', 'similar_ticket_opened') THEN 'Similar cases'
              WHEN event_type IN ('source_link_clicked', 'document_link_clicked', 'ticket_link_clicked') THEN 'Source clicks'
              WHEN event_type IN ('zaf_sidebar_opened', 'zaf_modal_opened') THEN 'Zendesk app'
              WHEN event_type = 'ticket_page_opened' THEN 'Ticket page'
              ELSE 'Other'
            END
          ) AS workflow,
          surface
        FROM `{settings.google_cloud_project_id}.esip_reference.esip_impact_events`
        WHERE created_at >= @start_at
          AND created_at < @end_at
          AND owner_email IS NOT NULL
          AND owner_email != ''
          AND {_email_domain_condition('owner_email')}
      ),
      chat_events AS (
        SELECT
          created_at,
          LOWER(owner_email) AS owner_email,
          COALESCE(target_ticket_id, ticket_id) AS ticket_id,
          'universal_chat_completed' AS event_type,
          'Chat' AS workflow,
          surface
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE created_at >= @start_at
          AND created_at < @end_at
          AND owner_email IS NOT NULL
          AND owner_email != ''
          AND {_email_domain_condition('owner_email')}
          AND NOT (IFNULL(JSON_VALUE(policy_flags, '$.translation_request'), 'false') = 'true'
                   OR STARTS_WITH(IFNULL(latest_question, ''), '__translate__:'))
      ),
      touches AS (
        SELECT * FROM impact_events
        UNION ALL
        SELECT * FROM chat_events
      )
    """


def _reply_cycles_cte() -> str:
    return f"""
      public_comments AS (
        SELECT
          c.ticket_id,
          c.comment_id,
          c.created_at,
          LOWER(c.author_email) AS owner_email,
          c.author_is_agent,
          LAG(c.created_at) OVER (
            PARTITION BY c.ticket_id
            ORDER BY c.created_at, c.comment_id
          ) AS previous_public_at,
          LAG(c.author_is_agent) OVER (
            PARTITION BY c.ticket_id
            ORDER BY c.created_at, c.comment_id
          ) AS previous_public_author_is_agent
        FROM `{settings.google_cloud_project_id}.esip_reference.ticket_comments` c
        WHERE c.created_at < @end_at
          AND c.created_at >= @start_at
          AND c.is_internal = FALSE
          AND c.author_is_agent IS NOT NULL
          AND {_email_domain_condition('c.author_email')}
      ),
      reply_cycles AS (
        SELECT
          ticket_id,
          owner_email,
          created_at AS reply_at,
          previous_public_at AS customer_at
        FROM public_comments
        WHERE created_at >= @start_at
          AND created_at < @end_at
          AND author_is_agent = TRUE
          AND previous_public_author_is_agent = FALSE
          AND previous_public_at IS NOT NULL
          AND owner_email IS NOT NULL
          AND owner_email != ''
      ),
      eligible AS (
        SELECT
          rc.ticket_id,
          rc.owner_email,
          rc.reply_at,
          rc.customer_at,
          TIMESTAMP_DIFF(rc.reply_at, rc.customer_at, MINUTE) / 60.0 AS latency_hours,
          COUNTIF(t.created_at IS NOT NULL) > 0 AS esip_assisted
        FROM reply_cycles rc
        LEFT JOIN touches t
          ON t.ticket_id = rc.ticket_id
         AND t.owner_email = rc.owner_email
         AND t.created_at >= rc.customer_at
         AND t.created_at <= rc.reply_at
        GROUP BY rc.ticket_id, rc.owner_email, rc.reply_at, rc.customer_at
      )
    """


def _safe_touch_query(client: bigquery.Client, windows: dict[str, Any], sql_tail: str) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        impact_telemetry.ensure_table(client)
        sql = f"WITH {_touches_cte()} {sql_tail}"
        return _query(client, sql, _param_windows(windows)), warnings
    except Exception as exc:
        warnings.append(_public_warning("usage", exc))
        return [], warnings


def _load_profiles() -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    profile_display_names: dict[str, str] = {}
    try:
        db = firestore.get_db()
    except Exception:
        return profiles

    try:
        for doc in db.collection("profiles").stream():
            data = doc.to_dict() or {}
            email = str(data.get("email") or "").strip().lower()
            if not _is_allowed_email(email):
                continue
            display = firestore.clean_auth_display_name(
                data.get("display_name") or data.get("name") or data.get("full_name") or "",
                email,
            )
            if display:
                profile_display_names[email] = display
    except Exception as exc:
        logger.warning("impact_metrics: could not load profile display names: %s", exc)

    try:
        for doc in db.collection("users").stream():
            email = str(doc.id or "").strip().lower()
            if not _is_allowed_email(email):
                continue
            data = doc.to_dict() or {}
            display = firestore.clean_auth_display_name(
                data.get("display_name")
                or data.get("name")
                or data.get("full_name")
                or profile_display_names.get(email)
                or "",
                email,
            )
            profiles[email] = {
                "email": email,
                "display_name": display,
                "role": firestore._role_from_doc(data),
                "active": bool(data.get("active", False)),
                "esip_access_granted_at": data.get("esip_access_granted_at") or DEFAULT_ROLLOUT_DATE.isoformat(),
            }
    except Exception as exc:
        logger.warning("impact_metrics: could not load user profiles: %s", exc)
    for email, display in profile_display_names.items():
        profiles.setdefault(
            email,
            {
                "email": email,
                "display_name": display,
                "role": "user",
                "active": False,
                "esip_access_granted_at": DEFAULT_ROLLOUT_DATE.isoformat(),
            },
        )
    return profiles


def _is_allowed_email(email: str) -> bool:
    safe = str(email or "").strip().lower()
    if "@" not in safe:
        return False
    if safe in LEGACY_IDENTITY_ALIASES:
        return True
    domain = safe.rsplit("@", 1)[1]
    return domain in set(_allowed_domains())


def _preferred_alias_email(items: list[tuple[str, dict[str, Any]]]) -> str:
    def score(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
        email, profile = item
        domain = email.rsplit("@", 1)[1] if "@" in email else ""
        return (
            0 if profile.get("active") else 1,
            0 if domain == IDENTITY_DOMAIN else 1,
            email,
        )

    return sorted(items, key=score)[0][0]


def _profile_alias_map(profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    by_local: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for email, profile in profiles.items():
        if not _is_allowed_email(email):
            continue
        local = email.split("@", 1)[0]
        by_local[local].append((email, profile))

    aliases: dict[str, str] = {}
    for items in by_local.values():
        preferred = _preferred_alias_email(items)
        for email, _profile in items:
            aliases[email] = preferred
    for alias, canonical in LEGACY_IDENTITY_ALIASES.items():
        if alias in profiles or canonical in profiles:
            aliases[alias] = canonical
            aliases.setdefault(canonical, canonical)
    return aliases


def _canonical_owner_key(key: str, aliases: dict[str, str]) -> str:
    safe = str(key or "").strip().lower()
    if not safe or safe == "unknown":
        return ""
    if "@" not in safe:
        return safe
    if safe in LEGACY_IDENTITY_ALIASES:
        return LEGACY_IDENTITY_ALIASES[safe]
    if not _is_allowed_email(safe):
        return ""
    if safe in aliases:
        return aliases[safe]
    local = safe.split("@", 1)[0]
    for known, preferred in aliases.items():
        if known.split("@", 1)[0] == local:
            return preferred
    return safe


def _ticket_columns(client: bigquery.Client) -> set[str]:
    try:
        table = client.get_table(f"{settings.google_cloud_project_id}.esip_reference.tickets")
        return {field.name for field in table.schema}
    except Exception:
        return set()


def _round(value: Any, digits: int = 1) -> float | None:
    try:
        if value is None:
            return None
        val = float(value)
        if math.isnan(val) or math.isinf(val):
            return None
        return round(val, digits)
    except Exception:
        return None


def _pct(part: Any, total: Any) -> float:
    try:
        total_f = float(total or 0)
        if total_f <= 0:
            return 0
        return round((float(part or 0) / total_f) * 100, 1)
    except Exception:
        return 0


def _delta_pct(current: float | int | None, previous: float | int | None, *, lower_is_better: bool = False) -> dict[str, Any]:
    if current is None or previous is None:
        return {"value": None, "direction": "neutral", "label": "No comparison"}
    if float(previous) == 0:
        if float(current) == 0:
            return {"value": 0, "direction": "neutral", "label": "No change"}
        return {"value": None, "direction": "neutral", "label": "New activity"}
    pct = round(((float(current) - float(previous)) / float(previous)) * 100, 1)
    improved = pct < 0 if lower_is_better else pct > 0
    direction = "up" if pct > 0 else "down" if pct < 0 else "neutral"
    if improved:
        direction = "improved"
    elif pct != 0:
        direction = "worse"
    return {"value": pct, "direction": direction, "label": f"{pct:+.1f}% vs baseline"}


def compute_reply_latencies_from_comments(
    comments: list[dict[str, Any]],
    touches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Pure helper used by tests to mirror the BQ public customer -> next public
    agent reply latency logic. Internal notes and agent follow-up replies are
    intentionally ignored.
    """
    ordered = sorted(comments, key=lambda item: item["created_at"])
    out: list[dict[str, Any]] = []
    previous_public_by_ticket: dict[Any, dict[str, Any]] = {}
    for comment in ordered:
        if comment.get("is_internal"):
            continue
        previous_public = previous_public_by_ticket.get(comment["ticket_id"])
        previous_public_by_ticket[comment["ticket_id"]] = comment
        if comment.get("author_is_agent") is not True:
            continue
        if previous_public is None or previous_public.get("author_is_agent") is not False:
            continue
        previous_customer = previous_public
        owner = str(comment.get("author_email") or "").lower()
        assisted = any(
            touch.get("ticket_id") == comment["ticket_id"]
            and str(touch.get("owner_email") or "").lower() == owner
            and previous_customer["created_at"] <= touch["created_at"] <= comment["created_at"]
            for touch in touches
        )
        out.append({
            "ticket_id": comment["ticket_id"],
            "owner_email": owner,
            "latency_hours": round((comment["created_at"] - previous_customer["created_at"]).total_seconds() / 3600, 3),
            "esip_assisted": assisted,
        })
    return out


def _period_rows_by_key(rows: list[dict[str, Any]], key: str = "period") -> dict[str, dict[str, Any]]:
    out = {period: {} for period in ("baseline", "ramp", "post")}
    for row in rows:
        period = str(row.get(key) or "")
        if period in out:
            out[period] = row
    return out


def _adoption(client: bigquery.Client, windows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    rows, warnings = _safe_touch_query(
        client,
        windows,
        f"""
        SELECT
          {_bucket_expr('created_at')} AS period,
          COUNT(*) AS events,
          COUNT(DISTINCT owner_email) AS active_users,
          COUNT(DISTINCT IF(ticket_id IS NOT NULL, CAST(ticket_id AS STRING), NULL)) AS assisted_tickets
        FROM touches
        GROUP BY period
        """,
    )
    periods = _empty_periods()
    for row in rows:
        period = str(row.get("period") or "")
        if period in periods:
            periods[period].update({
                "events": int(row.get("events") or 0),
                "active_users": int(row.get("active_users") or 0),
                "assisted_tickets": int(row.get("assisted_tickets") or 0),
            })

    workflow_rows, workflow_warnings = _safe_touch_query(
        client,
        windows,
        f"""
        SELECT
          {_bucket_expr('created_at')} AS period,
          workflow,
          COUNT(*) AS events,
          COUNT(DISTINCT owner_email) AS users,
          COUNT(DISTINCT IF(ticket_id IS NOT NULL, CAST(ticket_id AS STRING), NULL)) AS tickets
        FROM touches
        GROUP BY period, workflow
        ORDER BY period, events DESC, workflow
        LIMIT 100
        """,
    )
    warnings.extend(workflow_warnings)

    timeline_rows, timeline_warnings = _safe_touch_query(
        client,
        windows,
        """
        SELECT
          DATE(created_at, 'America/New_York') AS day,
          COUNT(*) AS events,
          COUNT(DISTINCT owner_email) AS active_users,
          COUNT(DISTINCT IF(ticket_id IS NOT NULL, CAST(ticket_id AS STRING), NULL)) AS assisted_tickets
        FROM touches
        GROUP BY day
        ORDER BY day
        """,
    )
    warnings.extend(timeline_warnings)

    repeat_rows, repeat_warnings = _safe_touch_query(
        client,
        windows,
        f"""
        SELECT COUNT(*) AS repeat_users
        FROM (
          SELECT owner_email, COUNT(DISTINCT DATE(created_at, 'America/New_York')) AS active_days
          FROM touches
          WHERE {_bucket_expr('created_at')} = 'post'
          GROUP BY owner_email
        )
        WHERE active_days >= 2
        """,
    )
    warnings.extend(repeat_warnings)

    return {
        "periods": periods,
        "workflows": [
            {
                "period": row.get("period"),
                "workflow": row.get("workflow") or "Other",
                "events": int(row.get("events") or 0),
                "users": int(row.get("users") or 0),
                "tickets": int(row.get("tickets") or 0),
            }
            for row in workflow_rows
        ],
        "timeline": [
            {
                "date": str(row.get("day")),
                "events": int(row.get("events") or 0),
                "active_users": int(row.get("active_users") or 0),
                "assisted_tickets": int(row.get("assisted_tickets") or 0),
            }
            for row in timeline_rows
        ],
        "repeat_users_post": int((repeat_rows[0] if repeat_rows else {}).get("repeat_users") or 0),
    }, warnings


def _response_speed(client: bigquery.Client, windows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        impact_telemetry.ensure_table(client)
        sql = f"""
        WITH {_touches_cte()},
        {_reply_cycles_cte()}
        SELECT
          {_bucket_expr('reply_at')} AS period,
          COUNT(*) AS replies,
          COUNTIF(esip_assisted) AS assisted_replies,
          APPROX_QUANTILES(latency_hours, 100)[OFFSET(50)] AS median_latency_hours,
          APPROX_QUANTILES(IF(esip_assisted, latency_hours, NULL), 100 IGNORE NULLS)[OFFSET(50)] AS assisted_median_latency_hours,
          APPROX_QUANTILES(IF(NOT esip_assisted, latency_hours, NULL), 100 IGNORE NULLS)[OFFSET(50)] AS non_assisted_median_latency_hours
        FROM eligible
        GROUP BY period
        """
        rows = _query(client, sql, _param_windows(windows))
    except Exception as exc:
        warnings.append(_public_warning("response_speed", exc))
        rows = []

    periods = _period_rows_by_key(rows)
    out = {}
    for period in ("baseline", "ramp", "post"):
        row = periods.get(period) or {}
        out[period] = {
            "replies": int(row.get("replies") or 0),
            "assisted_replies": int(row.get("assisted_replies") or 0),
            "median_latency_hours": _round(row.get("median_latency_hours")),
            "assisted_median_latency_hours": _round(row.get("assisted_median_latency_hours")),
            "non_assisted_median_latency_hours": _round(row.get("non_assisted_median_latency_hours")),
        }
    out["headline_delta"] = _delta_pct(
        out["post"]["median_latency_hours"],
        out["baseline"]["median_latency_hours"],
        lower_is_better=True,
    )
    return {"periods": out}, warnings


def _first_action(client: bigquery.Client, windows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    try:
        sql = f"""
        WITH first_actions AS (
          SELECT
            t.id AS ticket_id,
            t.created_at AS ticket_created_at,
            ARRAY_AGG(LOWER(c.author_email) IGNORE NULLS ORDER BY c.created_at LIMIT 1)[SAFE_OFFSET(0)] AS owner_email,
            MIN(c.created_at) AS first_agent_at
          FROM `{settings.google_cloud_project_id}.esip_reference.tickets` t
          JOIN `{settings.google_cloud_project_id}.esip_reference.ticket_comments` c
            ON c.ticket_id = t.id
          WHERE t.created_at >= @start_at
            AND t.created_at < @end_at
            AND c.created_at >= t.created_at
            AND c.author_is_agent = TRUE
          GROUP BY t.id, t.created_at
        )
        SELECT
          {_bucket_expr('ticket_created_at')} AS period,
          COUNT(*) AS tickets,
          APPROX_QUANTILES(TIMESTAMP_DIFF(first_agent_at, ticket_created_at, MINUTE) / 60.0, 100)[OFFSET(50)]
            AS median_first_action_hours
        FROM first_actions
        WHERE first_agent_at IS NOT NULL
        GROUP BY period
        """
        rows = _query(client, sql, _param_windows(windows))
        warnings: list[str] = []
    except Exception as exc:
        rows = []
        warnings = [_public_warning("first_action", exc)]
    periods = _period_rows_by_key(rows)
    return {
        "periods": {
            period: {
                "tickets": int((periods.get(period) or {}).get("tickets") or 0),
                "median_first_action_hours": _round((periods.get(period) or {}).get("median_first_action_hours")),
            }
            for period in ("baseline", "ramp", "post")
        }
    }, warnings


def _outcomes(client: bigquery.Client, windows: dict[str, Any], columns: set[str]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    has_assignee_email = "assignee_email" in columns
    identity_expr = (
        "LOWER(COALESCE(assignee_email, ''))"
        if has_assignee_email
        else "LOWER(COALESCE(assignee_name, ''))"
    )
    identity_domain_filter = (
        f"AND {_email_domain_condition('assignee_email')}"
        if has_assignee_email
        else ""
    )
    if not has_assignee_email:
        warnings.append("Ticket assignee_email is not available yet; solved/backlog ownership uses assignee_name fallback.")
    try:
        sql = f"""
        WITH solved AS (
          SELECT
            {_bucket_expr('solved_at')} AS period,
            {identity_expr} AS owner_key,
            COUNT(*) AS solved_tickets,
            APPROX_QUANTILES(resolution_hours, 100 IGNORE NULLS)[OFFSET(50)] AS median_resolution_hours,
            COUNTIF(resolution_sla_breached = TRUE) AS sla_breached,
            COUNTIF(resolution_sla_status IS NOT NULL OR resolution_sla_target_hours IS NOT NULL) AS sla_known,
            COUNTIF(public_comment_count <= 2) AS single_cycle_solved
          FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
          WHERE solved_at >= @start_at
            AND solved_at < @end_at
            AND LOWER(COALESCE(status, '')) IN ('solved', 'closed')
            AND {identity_expr} != ''
            {identity_domain_filter}
          GROUP BY period, owner_key
        )
        SELECT
          period,
          SUM(solved_tickets) AS solved_tickets,
          APPROX_QUANTILES(median_resolution_hours, 100 IGNORE NULLS)[OFFSET(50)] AS median_resolution_hours,
          SUM(sla_breached) AS sla_breached,
          SUM(sla_known) AS sla_known,
          SUM(single_cycle_solved) AS single_cycle_solved
        FROM solved
        GROUP BY period
        """
        rows = _query(client, sql, _param_windows(windows))
    except Exception as exc:
        rows = []
        warnings.append(_public_warning("outcomes", exc))

    try:
        backlog_sql = f"""
        SELECT
          {identity_expr} AS owner_key,
          COUNT(*) AS outstanding_tickets
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
        WHERE LOWER(COALESCE(status, '')) IN ('open', 'pending', 'hold')
          AND {identity_expr} != ''
          {identity_domain_filter}
        GROUP BY owner_key
        """
        backlog_rows = _query(client, backlog_sql, [])
    except Exception as exc:
        backlog_rows = []
        warnings.append(_public_warning("outstanding", exc))

    periods = _period_rows_by_key(rows)
    out_periods = {}
    for period in ("baseline", "ramp", "post"):
        row = periods.get(period) or {}
        solved = int(row.get("solved_tickets") or 0)
        sla_known = int(row.get("sla_known") or 0)
        out_periods[period] = {
            "solved_tickets": solved,
            "median_resolution_hours": _round(row.get("median_resolution_hours")),
            "sla_breach_rate_pct": _pct(row.get("sla_breached"), sla_known),
            "sla_known_tickets": sla_known,
            "single_cycle_solve_rate_pct": _pct(row.get("single_cycle_solved"), solved),
        }
    return {
        "periods": out_periods,
        "headline_solved_delta": _delta_pct(
            out_periods["post"]["solved_tickets"],
            out_periods["baseline"]["solved_tickets"],
        ),
        "headline_resolution_delta": _delta_pct(
            out_periods["post"]["median_resolution_hours"],
            out_periods["baseline"]["median_resolution_hours"],
            lower_is_better=True,
        ),
        "outstanding_tickets": sum(int(row.get("outstanding_tickets") or 0) for row in backlog_rows),
        "assignee_email_coverage": has_assignee_email,
    }, warnings


def _quality(client: bigquery.Client, windows: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        sql = f"""
        SELECT
          {_bucket_expr('created_at')} AS period,
          COUNT(*) AS chat_turns,
          COUNTIF(
            LOWER(IFNULL(assistant_answer, '')) LIKE '%not enough%'
            OR LOWER(IFNULL(assistant_answer, '')) LIKE '%could not find%'
            OR LOWER(IFNULL(assistant_answer, '')) LIKE '%no results%'
            OR LOWER(IFNULL(assistant_answer, '')) LIKE '%unable to find%'
          ) AS weak_or_no_answer,
          COUNTIF(
            COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.sources') AS INT64), 0)
            + COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.tickets') AS INT64), 0)
            + COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.bq') AS INT64), 0) = 0
          ) AS no_evidence,
          COUNTIF(
            (
              LOWER(IFNULL(assistant_answer, '')) LIKE '%not enough%'
              OR LOWER(IFNULL(assistant_answer, '')) LIKE '%could not find%'
              OR LOWER(IFNULL(assistant_answer, '')) LIKE '%no results%'
              OR LOWER(IFNULL(assistant_answer, '')) LIKE '%unable to find%'
              OR COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.sources') AS INT64), 0)
                 + COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.tickets') AS INT64), 0)
                 + COALESCE(SAFE_CAST(JSON_VALUE(counts, '$.bq') AS INT64), 0) = 0
            )
            AND (
              LOWER(TO_JSON_STRING(phase1_decision)) LIKE '%search_knowledge_base%'
              OR LOWER(TO_JSON_STRING(phase1_decision)) LIKE '%search_drive_exact%'
              OR REGEXP_CONTAINS(LOWER(IFNULL(latest_question, '')), r'\\b(kb|article|document|doc|guide|manual|procedure|how to|steps)\\b')
            )
          ) AS documentation_gap_signals
        FROM `{settings.google_cloud_project_id}.esip_reference.universal_chat_turn_logs`
        WHERE created_at >= @start_at
          AND created_at < @end_at
          AND owner_email IS NOT NULL
          AND owner_email != ''
          AND {_email_domain_condition('owner_email')}
          AND NOT (IFNULL(JSON_VALUE(policy_flags, '$.translation_request'), 'false') = 'true'
                   OR STARTS_WITH(IFNULL(latest_question, ''), '__translate__:'))
        GROUP BY period
        """
        rows = _query(client, sql, _param_windows(windows))
    except Exception as exc:
        rows = []
        warnings.append(_public_warning("quality", exc))

    try:
        impact_telemetry.ensure_table(client)
        feedback_sql = f"""
        SELECT
          {_bucket_expr('created_at')} AS period,
          COUNTIF(event_type = 'answer_feedback') AS answer_feedback_events,
          COUNTIF(event_type = 'answer_copied') AS answer_copied_events,
          COUNTIF(LOWER(JSON_VALUE(metadata, '$.rating')) IN ('not_useful', 'bad', 'thumbs_down')) AS not_useful_events
        FROM `{settings.google_cloud_project_id}.esip_reference.esip_impact_events`
        WHERE created_at >= @start_at
          AND created_at < @end_at
          AND owner_email IS NOT NULL
          AND owner_email != ''
          AND {_email_domain_condition('owner_email')}
        GROUP BY period
        """
        feedback_rows = _query(client, feedback_sql, _param_windows(windows))
    except Exception:
        feedback_rows = []

    period_rows = _period_rows_by_key(rows)
    feedback_by_period = _period_rows_by_key(feedback_rows)
    return {
        "periods": {
            period: {
                "chat_turns": int((period_rows.get(period) or {}).get("chat_turns") or 0),
                "weak_or_no_answer": int((period_rows.get(period) or {}).get("weak_or_no_answer") or 0),
                "no_evidence": int((period_rows.get(period) or {}).get("no_evidence") or 0),
                "documentation_gap_signals": int((period_rows.get(period) or {}).get("documentation_gap_signals") or 0),
                "answer_feedback_events": int((feedback_by_period.get(period) or {}).get("answer_feedback_events") or 0),
                "answer_copied_events": int((feedback_by_period.get(period) or {}).get("answer_copied_events") or 0),
                "not_useful_events": int((feedback_by_period.get(period) or {}).get("not_useful_events") or 0),
            }
            for period in ("baseline", "ramp", "post")
        }
    }, warnings


def _user_rows(client: bigquery.Client, windows: dict[str, Any], profiles: dict[str, dict[str, Any]], columns: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    aliases = _profile_alias_map(profiles)
    display_period = str(windows.get("display_period") or "post")
    usage_rows, usage_warnings = _safe_touch_query(
        client,
        windows,
        f"""
        SELECT
          owner_email,
          {_bucket_expr('created_at')} AS period,
          COUNT(*) AS events,
          COUNT(DISTINCT DATE(created_at, 'America/New_York')) AS active_days,
          COUNT(DISTINCT IF(ticket_id IS NOT NULL, CAST(ticket_id AS STRING), NULL)) AS assisted_tickets
        FROM touches
        GROUP BY owner_email, period
        """,
    )
    warnings.extend(usage_warnings)

    try:
        impact_telemetry.ensure_table(client)
        reply_sql = f"""
        WITH {_touches_cte()},
        {_reply_cycles_cte()}
        SELECT
          owner_email,
          {_bucket_expr('reply_at')} AS period,
          COUNT(*) AS replies,
          COUNTIF(esip_assisted) AS assisted_replies,
          APPROX_QUANTILES(latency_hours, 100)[OFFSET(50)] AS median_latency_hours
        FROM eligible
        GROUP BY owner_email, period
        """
        reply_rows = _query(client, reply_sql, _param_windows(windows))
    except Exception as exc:
        reply_rows = []
        warnings.append(_public_warning("per_user_reply", exc))

    has_assignee_email = "assignee_email" in columns
    identity_expr = "LOWER(COALESCE(assignee_email, ''))" if has_assignee_email else "LOWER(COALESCE(assignee_name, ''))"
    identity_domain_filter = (
        f"AND {_email_domain_condition('assignee_email')}"
        if has_assignee_email
        else ""
    )
    try:
        solved_sql = f"""
        SELECT
          {identity_expr} AS owner_key,
          {_bucket_expr('solved_at')} AS period,
          COUNT(*) AS solved_tickets,
          APPROX_QUANTILES(resolution_hours, 100 IGNORE NULLS)[OFFSET(50)] AS median_resolution_hours,
          COUNTIF(resolution_sla_breached = TRUE) AS sla_breached,
          COUNTIF(resolution_sla_status IS NOT NULL OR resolution_sla_target_hours IS NOT NULL) AS sla_known,
          COUNTIF(public_comment_count <= 2) AS single_cycle_solved
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
        WHERE solved_at >= @start_at
          AND solved_at < @end_at
          AND LOWER(COALESCE(status, '')) IN ('solved', 'closed')
          AND {identity_expr} != ''
          {identity_domain_filter}
        GROUP BY owner_key, period
        """
        solved_rows = _query(client, solved_sql, _param_windows(windows))
    except Exception as exc:
        solved_rows = []
        warnings.append(_public_warning("per_user_solved", exc))

    try:
        backlog_sql = f"""
        SELECT
          {identity_expr} AS owner_key,
          COUNT(*) AS outstanding_tickets
        FROM `{settings.google_cloud_project_id}.esip_reference.tickets`
        WHERE LOWER(COALESCE(status, '')) IN ('open', 'pending', 'hold')
          AND {identity_expr} != ''
          {identity_domain_filter}
        GROUP BY owner_key
        """
        backlog_rows = _query(client, backlog_sql, [])
    except Exception:
        backlog_rows = []

    users: dict[str, dict[str, Any]] = {}

    def ensure_user(key: str) -> dict[str, Any]:
        safe = _canonical_owner_key(key, aliases)
        if not safe:
            return {}
        profile = profiles.get(safe, {})
        item = users.setdefault(
            safe,
            {
                "email": safe if "@" in safe else None,
                "display_name": profile.get("display_name") or "",
                "access_granted_at": profile.get("esip_access_granted_at") or DEFAULT_ROLLOUT_DATE.isoformat(),
                "usage": defaultdict(lambda: {"events": 0, "active_days": 0, "assisted_tickets": 0}),
                "replies": defaultdict(lambda: {"replies": 0, "assisted_replies": 0, "median_latency_hours": None}),
                "solved": defaultdict(lambda: {
                    "solved_tickets": 0,
                    "median_resolution_hours": None,
                    "sla_breach_rate_pct": 0,
                    "single_cycle_solve_rate_pct": 0,
                }),
                "outstanding_tickets": 0,
                "identity_source": "email" if "@" in safe else "name",
            },
        )
        return item

    for row in usage_rows:
        item = ensure_user(str(row.get("owner_email") or "unknown"))
        if not item:
            continue
        period = str(row.get("period") or "")
        if period in {"baseline", "ramp", "post"}:
            item["usage"][period] = {
                "events": int(row.get("events") or 0),
                "active_days": int(row.get("active_days") or 0),
                "assisted_tickets": int(row.get("assisted_tickets") or 0),
            }
    for row in reply_rows:
        item = ensure_user(str(row.get("owner_email") or "unknown"))
        if not item:
            continue
        period = str(row.get("period") or "")
        if period in {"baseline", "ramp", "post"}:
            item["replies"][period] = {
                "replies": int(row.get("replies") or 0),
                "assisted_replies": int(row.get("assisted_replies") or 0),
                "median_latency_hours": _round(row.get("median_latency_hours")),
            }
    for row in solved_rows:
        item = ensure_user(str(row.get("owner_key") or "unknown"))
        if not item:
            continue
        period = str(row.get("period") or "")
        if period in {"baseline", "ramp", "post"}:
            solved = int(row.get("solved_tickets") or 0)
            item["solved"][period] = {
                "solved_tickets": solved,
                "median_resolution_hours": _round(row.get("median_resolution_hours")),
                "sla_breach_rate_pct": _pct(row.get("sla_breached"), row.get("sla_known")),
                "single_cycle_solve_rate_pct": _pct(row.get("single_cycle_solved"), solved),
            }
    for row in backlog_rows:
        item = ensure_user(str(row.get("owner_key") or "unknown"))
        if not item:
            continue
        item["outstanding_tickets"] = int(row.get("outstanding_tickets") or 0)

    out: list[dict[str, Any]] = []
    for key, item in users.items():
        baseline_replies = item["replies"]["baseline"]["replies"]
        post_replies = item["replies"]["post"]["replies"]
        baseline_solved = item["solved"]["baseline"]["solved_tickets"]
        post_solved = item["solved"]["post"]["solved_tickets"]
        post_usage = item["usage"]["post"]["events"]
        display_usage = item["usage"][display_period]["events"]
        display_replies = item["replies"][display_period]["replies"]
        display_solved = item["solved"][display_period]["solved_tickets"]
        sample = display_replies + display_solved
        if display_period == "ramp" and (display_usage > 0 or sample > 0):
            confidence = "Ramp-up"
        elif sample < 5:
            confidence = "Low sample"
        elif item["identity_source"] != "email":
            confidence = "Needs review"
        elif display_usage == 0:
            confidence = "Low ESIP usage"
        else:
            confidence = "Enough data"
        out.append({
            "key": key,
            "email": item["email"],
            "display_name": item["display_name"],
            "access_granted_at": item["access_granted_at"],
            "confidence": confidence,
            "confidence_reason": "Assignee email coverage is pending." if item["identity_source"] != "email" else "",
            "identity_source": item["identity_source"],
            "usage": {period: dict(item["usage"][period]) for period in ("baseline", "ramp", "post")},
            "replies": {period: dict(item["replies"][period]) for period in ("baseline", "ramp", "post")},
            "solved": {period: dict(item["solved"][period]) for period in ("baseline", "ramp", "post")},
            "outstanding_tickets": item["outstanding_tickets"],
            "reply_latency_delta": _delta_pct(
                item["replies"]["post"]["median_latency_hours"],
                item["replies"]["baseline"]["median_latency_hours"],
                lower_is_better=True,
            ),
            "solved_delta": _delta_pct(post_solved, baseline_solved),
        })
    out.sort(
        key=lambda row: (
            row["usage"][display_period]["events"],
            row["solved"][display_period]["solved_tickets"],
            row["replies"][display_period]["replies"],
            row["outstanding_tickets"],
            row["display_name"] or row["email"] or row["key"],
        ),
        reverse=True,
    )
    return out[:250], warnings


def _insights(payload: dict[str, Any]) -> dict[str, Any]:
    adoption = payload["adoption"]
    response = payload["response_speed"]["periods"]
    outcomes = payload["outcomes"]["periods"]
    quality = payload["quality"]["periods"]
    post_started = bool(payload.get("range", {}).get("post_started"))
    post_events = adoption["periods"]["post"]["events"]
    post_users = adoption["periods"]["post"]["active_users"]
    post_assisted = adoption["periods"]["post"]["assisted_tickets"]
    wins: list[str] = []
    concerns: list[str] = []

    latency_delta = payload["response_speed"]["periods"]["headline_delta"]
    if latency_delta.get("direction") == "improved":
        wins.append(f"Median public reply latency improved {latency_delta['label']} after the ramp-up window.")
    elif response["post"]["replies"] > 0:
        concerns.append("Median public reply latency has not improved yet in the stable post-ramp window.")

    solved_delta = payload["outcomes"]["headline_solved_delta"]
    if solved_delta.get("direction") == "improved":
        wins.append(f"Solved-ticket volume increased {solved_delta['label']} after ramp-up.")

    if post_events > 0:
        wins.append(f"{post_users} users generated {post_events} ESIP product interactions after ramp-up.")
    else:
        concerns.append("Stable post-ramp usage has not started yet; impact conclusions should wait for more days.")
    if post_assisted > 0:
        wins.append(f"{post_assisted} tickets had a ESIP-assisted touch after ramp-up.")

    post_quality = quality["post"]
    if post_quality["chat_turns"] and _pct(post_quality["weak_or_no_answer"], post_quality["chat_turns"]) >= 15:
        concerns.append("Weak or no-answer chat turns are still high enough to investigate.")
    if post_quality["documentation_gap_signals"] > 0:
        concerns.append(f"{post_quality['documentation_gap_signals']} post-ramp turns look like documentation-gap signals.")
    if post_started and outcomes["post"]["sla_known_tickets"] == 0:
        concerns.append("SLA coverage is limited in the selected post-ramp window.")

    caveats = [
        "This compares the periods before and after rollout. It shows signals, not proof that ESIP caused every change.",
        "The first 7 days after rollout are treated as learning time and are not used in headline comparisons.",
        "Resolution time can be affected by vendors, store availability, parts, internet providers, and the mix of tickets.",
        "Call duration is not included because ESIP is not connected to phone-call data.",
        "The FCR-like number is only a proxy: tickets solved with one customer/agent exchange. It is not an official FCR field.",
    ]
    return {"wins": wins[:4], "concerns": concerns[:4], "caveats": caveats}


def get_impact_dashboard(*, rollout_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    windows = resolve_windows(rollout_date, end_date)
    client = _get_client()
    warnings: list[str] = []
    columns = _ticket_columns(client)
    profiles = _load_profiles()

    adoption, w = _adoption(client, windows)
    warnings.extend(w)
    response, w = _response_speed(client, windows)
    warnings.extend(w)
    first_action, w = _first_action(client, windows)
    warnings.extend(w)
    outcomes, w = _outcomes(client, windows, columns)
    warnings.extend(w)
    quality, w = _quality(client, windows)
    warnings.extend(w)
    users, w = _user_rows(client, windows, profiles, columns)
    warnings.extend(w)
    display_period = str(windows.get("display_period") or "post")

    payload = {
        "range": {
            "rollout_date": windows["rollout_date"],
            "as_of_date": windows["as_of_date"],
            "post_started": windows["post_started"],
            "current_phase": windows["current_phase"],
            "display_period": display_period,
            "ramp_day": windows["ramp_day"],
            "baseline": {k: windows["baseline"][k] for k in ("label", "start_date", "end_date")},
            "ramp": {k: windows["ramp"][k] for k in ("label", "start_date", "end_date")},
            "post": {k: windows["post"][k] for k in ("label", "start_date", "end_date")},
        },
        "summary": {
            "display_period": display_period,
            "display_period_label": "Ramp-up so far" if display_period == "ramp" else "Stable post-ramp",
            "post_started": bool(windows["post_started"]),
            "ramp_day": int(windows["ramp_day"]),
            "active_users_post": adoption["periods"]["post"]["active_users"],
            "active_users_display": adoption["periods"][display_period]["active_users"],
            "repeat_users_post": adoption["repeat_users_post"],
            "esip_assisted_tickets_post": adoption["periods"]["post"]["assisted_tickets"],
            "esip_assisted_tickets_display": adoption["periods"][display_period]["assisted_tickets"],
            "median_reply_latency_post_hours": response["periods"]["post"]["median_latency_hours"],
            "median_reply_latency_display_hours": response["periods"][display_period]["median_latency_hours"],
            "solved_tickets_post": outcomes["periods"]["post"]["solved_tickets"],
            "solved_tickets_display": outcomes["periods"][display_period]["solved_tickets"],
            "outstanding_tickets": outcomes["outstanding_tickets"],
            "cautious_label": "Early signal" if adoption["periods"]["post"]["events"] else "Collecting ramp-up data",
        },
        "adoption": adoption,
        "response_speed": response,
        "first_action": first_action,
        "outcomes": outcomes,
        "quality": quality,
        "users": users,
        "warnings": sorted(set(warnings)),
    }
    payload["insights"] = _insights(payload)
    return payload


def get_impact_report(*, rollout_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    dashboard = get_impact_dashboard(rollout_date=rollout_date, end_date=end_date)
    summary = dashboard["summary"]
    headline = (
        "ESIP impact is still in ramp-up; use this as a readiness snapshot."
        if summary["cautious_label"] == "Collecting ramp-up data"
        else "ESIP shows early operational impact signals after the ramp-up window."
    )
    return {
        **dashboard,
        "report": {
            "title": "ESIP Early Impact Report",
            "subtitle": "Cautious before/ramp/after view for leadership",
            "headline": headline,
            "generated_at": datetime.now(BUSINESS_TIMEZONE).isoformat(),
            "kpis": [
                {
                    "label": "Active users after ramp",
                    "value": summary["active_users_post"],
                    "detail": f"{summary['repeat_users_post']} repeat users",
                },
                {
                    "label": "ESIP-assisted tickets",
                    "value": summary["esip_assisted_tickets_post"],
                    "detail": "Tickets with a ESIP touch after ramp",
                },
                {
                    "label": "Median reply latency",
                    "value": summary["median_reply_latency_post_hours"],
                    "suffix": "h",
                    "detail": dashboard["response_speed"]["periods"]["headline_delta"]["label"],
                },
                {
                    "label": "Solved tickets after ramp",
                    "value": summary["solved_tickets_post"],
                    "detail": dashboard["outcomes"]["headline_solved_delta"]["label"],
                },
            ],
        },
    }
