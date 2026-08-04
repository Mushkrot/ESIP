"""Read-only validation helpers for the Service Desk KPI definitions."""

from __future__ import annotations

from typing import Any

from app.services import service_desk_kpi


def build_validation_payload(
    report: dict[str, Any],
    *,
    manual_totals: dict[str, int] | None = None,
    top_n: int = 12,
) -> dict[str, Any]:
    """Return a management-readable definition check without writing data."""
    summary = report.get("summary") or {}
    rows = list(report.get("rows") or [])
    assigned = _int(summary.get("new_assigned_tickets"))
    solved = _int(summary.get("same_day_solved_tickets"))
    outstanding = _int(summary.get("outstanding_tickets"))

    payload = {
        "date": report.get("date"),
        "timezone": report.get("timezone", "America/New_York"),
        "generated_at": report.get("generated_at"),
        "read_only": True,
        "definitions": [
            {
                "metric": "Assigned",
                "value": assigned,
                "definition": "Tickets created on the selected date whose reconstructed snapshot owner is that engineer or queue.",
                "data_source": "BigQuery tickets + Zendesk ticket audit events: created_at + assignee/group as of report snapshot.",
                "caveat": "Depends on ticket audit-event coverage for historical owner and queue reconstruction.",
            },
            {
                "metric": "Solved",
                "value": solved,
                "definition": "Tickets whose Zendesk status changed to Solved on the selected date, counted under the assignee or queue at solve time.",
                "data_source": "BigQuery tickets + Zendesk ticket audit events: status changes plus assignee/group reconstruction.",
                "caveat": "Closed status is intentionally ignored. This is not based on the tickets.solved_at proxy field.",
            },
            {
                "metric": "Outstanding",
                "value": outstanding,
                "definition": "Reconstructed new, open, pending, or hold backlog at the report snapshot.",
                "data_source": "BigQuery tickets + Zendesk ticket audit events: status + assignee/group as of report snapshot.",
                "caveat": "Normally uses 8:00 a.m. Eastern on the next calendar day for historical reports; exact manual report time can legitimately differ.",
            },
        ],
        "totals": {
            "assigned": assigned,
            "solved": solved,
            "outstanding_now": outstanding,
        },
        "manual_comparison": _manual_comparison(
            {"assigned": assigned, "solved": solved, "outstanding": outstanding},
            manual_totals or {},
        ),
        "top_rows": _top_rows(rows, top_n=top_n),
        "warnings": _warnings(report, assigned=assigned, solved=solved),
    }
    return payload


def format_validation_text(payload: dict[str, Any]) -> str:
    """Format the validation payload for terminal review."""
    lines = [
        f"Service Desk KPI definition check for {payload.get('date')}",
        "READ ONLY: no Zendesk, BigQuery, OCR, embedding, or scheduler writes.",
        "",
        "Totals:",
    ]
    totals = payload.get("totals") or {}
    lines.extend(
        [
            f"  Assigned: {totals.get('assigned', 0)}",
            f"  Solved: {totals.get('solved', 0)}",
            f"  Outstanding: {totals.get('outstanding_now', 0)}",
        ]
    )
    if payload.get("manual_comparison"):
        lines.extend(["", "Manual comparison:"])
        for item in payload["manual_comparison"]:
            lines.append(
                f"  {item['metric']}: ESIP {item['esip_value']} vs manual {item['manual_value']} "
                f"(delta {item['delta']:+d})"
            )
    lines.extend(["", "Definitions:"])
    for item in payload.get("definitions") or []:
        lines.append(f"  {item['metric']}: {item['definition']}")
        lines.append(f"    Caveat: {item['caveat']}")
    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(["", "Warnings:"])
        for warning in warnings:
            lines.append(f"  - {warning}")
    return "\n".join(lines)


def _manual_comparison(esip_totals: dict[str, int], manual_totals: dict[str, int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    labels = {
        "assigned": "Assigned",
        "solved": "Solved",
        "outstanding": "Outstanding",
    }
    for key, label in labels.items():
        if key not in manual_totals or manual_totals[key] is None:
            continue
        manual_value = _int(manual_totals[key])
        esip_value = _int(esip_totals.get(key))
        out.append(
            {
                "metric": label,
                "esip_value": esip_value,
                "manual_value": manual_value,
                "delta": esip_value - manual_value,
                "matches": esip_value == manual_value,
            }
        )
    return out


def _top_rows(rows: list[dict[str, Any]], *, top_n: int) -> list[dict[str, Any]]:
    def score(row: dict[str, Any]) -> tuple[int, str]:
        return (
            -(
                _int(row.get("new_assigned_tickets"))
                + _int(row.get("same_day_solved_tickets"))
                + _int(row.get("outstanding_tickets"))
            ),
            str(row.get("display_name") or row.get("email") or ""),
        )

    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=score)[: max(0, top_n)]:
        email = str(row.get("email") or "")
        out.append(
            {
                "name": row.get("display_name") or email,
                "email": "" if _synthetic_owner(email) else email,
                "assigned": _int(row.get("new_assigned_tickets")),
                "solved": _int(row.get("same_day_solved_tickets")),
                "outstanding_now": _int(row.get("outstanding_tickets")),
            }
        )
    return out


def _warnings(
    report: dict[str, Any],
    *,
    assigned: int,
    solved: int,
) -> list[str]:
    warnings = list(report.get("warnings") or [])
    if assigned == 0 and solved:
        warnings.append("Assigned is zero while solved work exists. Review owner metadata coverage.")
    coverage = report.get("coverage") or {}
    if _int(coverage.get("assignee_email_coverage_pct")) < 95:
        warnings.append("Owner email coverage is below 95%, so per-owner rows may be incomplete.")
    return warnings


def _synthetic_owner(email: str) -> bool:
    safe = str(email or "").strip().lower()
    return safe == service_desk_kpi.BLANK_OWNER_EMAIL or safe.startswith(service_desk_kpi.NAME_OWNER_PREFIX)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
