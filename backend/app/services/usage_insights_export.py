"""Excel export for the management-safe ESIP Usage report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services import usage_insights
from app.services.service_desk_kpi_export import XLSX_MEDIA_TYPE

_HEADER_FILL = "E2E8F0"
_LEFT_FILL = "F8FAFC"
_ALT_FILL = "F8FBFD"
_WHITE = "FFFFFF"
_BORDER = "D8DEE8"
_MIN_COLUMN_WIDTH = 8
_MAX_COLUMN_WIDTH = 56
_COLUMN_PADDING = 2


@dataclass(frozen=True)
class UsageExportFile:
    content: bytes
    filename: str
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


def get_export_file(
    *,
    range_mode: str = "custom",
    start_date: str | None = None,
    end_date: str | None = None,
    user_email: str | None = None,
    question_type: str | None = None,
    compare_previous: bool = True,
    group_by: str = "user",
    min_activity: int = 0,
    problem_signal: str = "all",
    category_focus: str = "all",
    include_super_admin_usage: bool = False,
    include_technical_quality: bool = False,
    file_format: str = "xlsx",
) -> UsageExportFile:
    """Build an on-demand ESIP Usage Excel export for a selected period and filters."""
    dashboard = usage_insights.get_usage_dashboard(
        range_mode=range_mode,
        start_date=start_date,
        end_date=end_date,
        user_email=user_email,
        question_type=question_type,
        include_translation_turns=False,
        include_super_admin_usage=include_super_admin_usage,
        include_technical_quality=include_technical_quality,
        compare_previous=compare_previous,
        group_by=group_by,
        min_activity=min_activity,
        problem_signal=problem_signal,
        category_focus=category_focus,
    )
    safe_range = _safe_range(dashboard)
    requested = (file_format or "xlsx").strip().lower()
    if requested in {"xlsx", "excel"}:
        return UsageExportFile(
            content=build_excel_export(dashboard),
            filename=f"esip-usage-{safe_range}.xlsx",
            media_type=XLSX_MEDIA_TYPE,
            metadata=_export_metadata(dashboard),
        )
    raise ValueError("ESIP Usage export supports Excel only")


def build_excel_export(dashboard: dict[str, Any]) -> bytes:
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "ESIP Usage Report"
    _write_summary_sheet(summary_ws, dashboard)
    _write_users_sheet(wb.create_sheet("Users"), dashboard)
    _write_details_sheet(wb.create_sheet("Details"), dashboard)
    _format_workbook_for_export(wb)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def _write_summary_sheet(ws, dashboard: dict[str, Any]) -> None:
    title = _report_title(dashboard)
    summary = dashboard.get("summary") or {}
    coverage = dashboard.get("source_coverage") or {}
    gaps = dashboard.get("document_gaps") or []
    controls = dashboard.get("controls") or {}

    ws.cell(1, 1).value = title
    ws.cell(1, 1).font = Font(bold=True, size=14)
    ws.cell(2, 1).value = "Period"
    ws.cell(2, 2).value = _period_label(dashboard)
    ws.cell(3, 1).value = "Generated"
    ws.cell(3, 2).value = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = [
        ("Queries to date", summary.get("total_queries_to_date"), "All-time adoption baseline"),
        ("Selected range queries", summary.get("total_queries_in_range"), "Questions in this report period"),
        ("Active users", summary.get("active_users_in_range"), "Users with at least one question"),
        ("Avg queries per active user", summary.get("avg_queries_per_active_user"), "Selected period"),
        ("Top question type", summary.get("top_question_type") or "-", "Most common selected-period use case"),
        ("Evidence shown", f"{coverage.get('evidence_coverage_pct', 0)}%", "Visible sources, tickets, or analytics"),
        ("No evidence shown", summary.get("no_source_turns"), "Questions without visible evidence artifacts"),
        ("Documentation opportunities", len(gaps), "Topics that may need better source coverage"),
    ]
    _write_table(
        ws,
        start_row=5,
        headers=["Metric", "Value", "Meaning"],
        rows=rows,
        widths=[28, 18, 54],
    )

    filter_rows = [
        ("User", (dashboard.get("range") or {}).get("user_email") or "All users"),
        ("Question type", (dashboard.get("range") or {}).get("question_type") or "All types"),
        ("Group by", controls.get("group_by") or "user"),
        ("Minimum activity", controls.get("min_activity") or 0),
        ("Problem signal", controls.get("problem_signal") or "all"),
        ("Category focus", controls.get("category_focus") or "all"),
    ]
    _write_table(
        ws,
        start_row=15,
        headers=["Filter", "Value"],
        rows=filter_rows,
        widths=[28, 54],
    )
    ws.sheet_view.showGridLines = False


def _write_users_sheet(ws, dashboard: dict[str, Any]) -> None:
    total = int((dashboard.get("summary") or {}).get("total_queries_in_range") or 0)
    rows = []
    for item in dashboard.get("users") or []:
        queries = int(item.get("queries_in_range") or 0)
        rows.append([
            _person_label(item),
            item.get("email") or "",
            int(item.get("queries_to_date") or 0),
            queries,
            _pct(queries, total),
            int(item.get("active_days") or 0),
            _date_text(item.get("last_active_at")),
            item.get("top_question_type") or "-",
            item.get("evidence_rate_pct") or 0,
            int(item.get("documentation_opportunities") or 0),
            _seconds(item.get("avg_latency_ms")),
        ])
    _write_title(ws, _report_title(dashboard), "Users")
    _write_table(
        ws,
        start_row=4,
        headers=[
            "User",
            "Email",
            "Total queries",
            "Range queries",
            "Share %",
            "Active days",
            "Last active",
            "Top use case",
            "Evidence %",
            "Doc opportunities",
            "Avg latency",
        ],
        rows=rows,
        widths=[26, 34, 14, 14, 10, 12, 18, 28, 12, 18, 14],
    )


def _write_details_sheet(ws, dashboard: dict[str, Any]) -> None:
    _write_title(ws, _report_title(dashboard), "Question types, daily trend, and documentation opportunities")
    next_row = _write_section(
        ws,
        start_row=4,
        title="Question types",
        headers=["Question type", "Queries", "Users", "No evidence", "Evidence %"],
        rows=[
            [
                item.get("question_type") or "-",
                int(item.get("count") or 0),
                int(item.get("users") or 0),
                int(item.get("no_source_count") or 0),
                item.get("evidence_rate_pct") or 0,
            ]
            for item in dashboard.get("question_types") or []
        ],
        widths=[36, 12, 12, 14, 12],
    )
    next_row = _write_section(
        ws,
        start_row=next_row,
        title="Daily trend",
        headers=["Date", "Queries", "Active users", "No evidence"],
        rows=[
            [
                item.get("date") or "",
                int(item.get("queries") or 0),
                int(item.get("active_users") or 0),
                int(item.get("no_source") or 0),
            ]
            for item in dashboard.get("timeseries") or []
        ],
        widths=[16, 12, 14, 14],
        freeze=False,
    )
    _write_section(
        ws,
        start_row=next_row,
        title="Documentation opportunities",
        headers=["Theme", "Question type", "Queries", "Users", "No evidence", "Suggested next step"],
        rows=[
            [
                item.get("theme") or "-",
                item.get("question_type") or "-",
                int(item.get("count") or 0),
                int(item.get("users_affected") or 0),
                int(item.get("no_source_count") or 0),
                item.get("suggested_action") or "",
            ]
            for item in dashboard.get("document_gaps") or []
        ],
        widths=[28, 32, 12, 12, 14, 56],
        freeze=False,
    )


def _write_section(
    ws,
    *,
    start_row: int,
    title: str,
    headers: list[str],
    rows: list[Any],
    widths: list[int],
    freeze: bool = True,
) -> int:
    ws.cell(start_row, 1).value = title
    ws.cell(start_row, 1).font = Font(bold=True, size=12)
    _write_table(
        ws,
        start_row=start_row + 1,
        headers=headers,
        rows=rows,
        widths=widths,
        freeze=freeze,
    )
    return start_row + len(rows) + 4


def _write_title(ws, title: str, subtitle: str) -> None:
    ws.cell(1, 1).value = title
    ws.cell(1, 1).font = Font(bold=True, size=14)
    ws.cell(2, 1).value = subtitle
    ws.cell(2, 1).font = Font(bold=True, color="44546A")
    ws.sheet_view.showGridLines = False


def _write_table(
    ws,
    *,
    start_row: int,
    headers: list[str],
    rows: list[Any],
    widths: list[int],
    freeze: bool = True,
) -> None:
    _ = widths  # Widths are now calculated from final sheet content just before save.
    header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
    left_fill = PatternFill("solid", fgColor=_LEFT_FILL)
    alt_fill = PatternFill("solid", fgColor=_ALT_FILL)
    white_fill = PatternFill("solid", fgColor=_WHITE)
    thin = Side(style="thin", color=_BORDER)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, header in enumerate(headers, start=1):
        cell = ws.cell(start_row, col)
        cell.value = header
        cell.fill = header_fill
        cell.font = Font(bold=True)
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_idx, values in enumerate(rows, start=start_row + 1):
        if not isinstance(values, (list, tuple)):
            values = [values]
        fill = alt_fill if row_idx % 2 == 0 else white_fill
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row_idx, col)
            cell.value = value
            cell.border = border
            cell.fill = left_fill if col == 1 else fill
            cell.alignment = Alignment(
                horizontal="left" if col == 1 or isinstance(value, str) else "center",
                vertical="center",
                wrap_text=True,
            )
            if str(headers[col - 1]).endswith("%") and isinstance(value, (int, float)):
                cell.number_format = "0.0"

    if freeze:
        ws.freeze_panes = ws.cell(start_row + 1, 1).coordinate


def _format_workbook_for_export(wb: Workbook) -> None:
    """Apply final workbook sizing after all sheets are populated."""
    for ws in wb.worksheets:
        _fit_columns_to_content(ws)
        ws.sheet_view.showGridLines = False


def _fit_columns_to_content(ws) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        if _is_standalone_label_row(row):
            continue
        for cell in row:
            if cell.value is None or cell.value == "":
                continue
            widths[cell.column] = max(widths.get(cell.column, 0), _text_width(cell.value))

    for column_index in range(1, ws.max_column + 1):
        width = widths.get(column_index)
        if not width:
            continue
        adjusted = min(_MAX_COLUMN_WIDTH, max(_MIN_COLUMN_WIDTH, width + _COLUMN_PADDING))
        ws.column_dimensions[get_column_letter(column_index)].width = adjusted


def _is_standalone_label_row(row) -> bool:
    non_empty = [cell for cell in row if cell.value is not None and cell.value != ""]
    return len(non_empty) == 1 and non_empty[0].column == 1


def _text_width(value: Any) -> int:
    if isinstance(value, datetime):
        text = value.isoformat(timespec="seconds")
    elif isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value)
    return max((len(part) for part in text.splitlines()), default=0)


def _report_title(dashboard: dict[str, Any]) -> str:
    label = _period_label(dashboard)
    return f"ESIP Usage Report for {label}"


def _period_label(dashboard: dict[str, Any]) -> str:
    range_data = dashboard.get("range") or {}
    start_label = _long_date(_date_from_iso(range_data.get("start_at")))
    end_label = _long_date(_date_from_iso(range_data.get("end_at")))
    if start_label and end_label and start_label != end_label:
        return f"{start_label} - {end_label}"
    if start_label or end_label:
        return start_label or end_label
    return "To date"


def _safe_range(dashboard: dict[str, Any]) -> str:
    range_data = dashboard.get("range") or {}
    start = _date_from_iso(range_data.get("start_at"))
    end = _date_from_iso(range_data.get("end_at"))
    if start and end:
        return start if start == end else f"{start}_to_{end}"
    if end:
        return f"to-date-{end}"
    return "to-date"


def _export_metadata(dashboard: dict[str, Any]) -> dict[str, Any]:
    range_data = dashboard.get("range") or {}
    return {
        "report_start_at": range_data.get("start_at"),
        "report_end_at": range_data.get("end_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _date_from_iso(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return raw[:10]


def _long_date(value: str) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value[:10]).strftime("%B %-d, %Y")
    except Exception:
        try:
            return date.fromisoformat(value[:10]).strftime("%B %#d, %Y")
        except Exception:
            return value


def _date_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return "-"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16]


def _person_label(item: dict[str, Any]) -> str:
    return str(item.get("display_name") or item.get("email") or item.get("key") or "Unknown user")


def _seconds(ms: Any) -> str:
    if ms is None:
        return "-"
    try:
        return f"{round(float(ms) / 1000)}s"
    except Exception:
        return "-"


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0
    return round((numerator / denominator) * 100, 1)
