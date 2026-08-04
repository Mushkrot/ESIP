"""File exports for the Service Desk KPI report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services import service_desk_kpi

PDF_MEDIA_TYPE = "application/pdf"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_HEADER_FILL = "E2E8F0"
_DATE_FILL = "CFC3DF"
_LEFT_FILL = "F8FAFC"
_ALT_FILL = "F8FBFD"
_WHITE = "FFFFFF"
_BORDER = "D8DEE8"
_SEPARATOR = "000000"
_TEXT = "111111"
_MUTED = "666666"
MAX_EXPORT_DAYS = 14

_ROW_ORDER = [
    "avery stone",
    "morgan reed",
    "cameron blake",
    "kevin park",
    "noah bennett",
    "rowan patel",
    "maya collins",
    "ethan walker",
    "riley morgan",
    "grace lin",
    "menu maintenance",
    "paul chen",
    "philip moore",
    "christopher hill",
    "marcus clark",
    "operations support",
    "thedeploymentanalystteam",
    "jordan king",
    "ravi dawson",
    "reference operator",
    service_desk_kpi.BLANK_OWNER_LABEL.lower(),
    "field_services_partner",
    "dana ellis",
    "operations support",
    "trainingteam",
    "service desk supervisors",
    "technical services",
]
_GROUP_ROW_ORDER = [
    "servicedesk",
    "menu maintenance",
    "technical services",
    "netops",
    "operations support",
    "thedeploymentanalystteam",
    "field_services_partner",
    "business analysts",
    "trainingteam",
    "service desk supervisors",
    service_desk_kpi.UNMAPPED_GROUP_LABEL.lower(),
]


@dataclass(frozen=True)
class KpiExportFile:
    content: bytes
    filename: str
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


def get_daily_excel_export(report_date: str | None = None) -> KpiExportFile:
    """Build the canonical one-day Excel artifact for manual and scheduled delivery."""
    return get_export_file(
        report_date=report_date,
        file_format="xlsx",
    )


def get_export_file(
    *,
    report_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    file_format: str = "pdf",
) -> KpiExportFile:
    """Build an on-demand KPI export file for a selected Eastern date range."""
    days = _parse_day_range(start_date=start_date, end_date=end_date, report_date=report_date)
    reports = [
        service_desk_kpi.get_daily_report(
            report_date=day.isoformat(),
        )
        for day in days
    ]
    safe_range = _safe_date_range(reports)

    requested = (file_format or "pdf").strip().lower()
    if requested == "pdf":
        return KpiExportFile(
            content=build_pdf_export_for_reports(reports),
            filename=f"service-desk-kpi-{safe_range}.pdf",
            media_type=PDF_MEDIA_TYPE,
            metadata=_export_metadata(reports),
        )
    if requested in {"xlsx", "excel"}:
        return KpiExportFile(
            content=build_excel_export_for_reports(reports),
            filename=f"service-desk-kpi-{safe_range}.xlsx",
            media_type=XLSX_MEDIA_TYPE,
            metadata=_export_metadata(reports),
        )
    raise ValueError("Unsupported KPI export format")


def _export_metadata(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        return {}
    current = reports[-1]
    telephony = (current.get("coverage") or {}).get("telephony") or {}
    return {
        "report_date": current.get("date"),
        "generated_at": current.get("generated_at"),
        "telephony_status": telephony,
        "cxone_cache_fresh": bool(telephony.get("is_fresh")),
        "cxone_run_started_at": telephony.get("started_at"),
        "cxone_run_completed_at": telephony.get("completed_at"),
    }


def build_excel_export(current: dict[str, Any], previous: dict[str, Any]) -> bytes:
    return build_excel_export_for_reports([previous, current])


def build_excel_export_for_reports(reports: list[dict[str, Any]]) -> bytes:
    if not reports:
        raise ValueError("At least one KPI report is required")
    wb = Workbook()
    ws = wb.active
    ws.title = "KPI Report"
    _write_grouped_kpi_table_sheet(ws, reports)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def build_pdf_export(current: dict[str, Any], previous: dict[str, Any]) -> bytes:
    return build_pdf_export_for_reports([previous, current])


def build_pdf_export_for_reports(reports: list[dict[str, Any]]) -> bytes:
    if not reports:
        raise ValueError("At least one KPI report is required")
    pdf = _SimplePdf()
    pdf.add_page()
    _draw_pdf_report(pdf, reports)
    return pdf.render()


def _parse_day(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except Exception:
            pass
    return datetime.now(service_desk_kpi.BUSINESS_TIMEZONE).date() - timedelta(days=1)


def _parse_day_range(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    report_date: str | None = None,
) -> list[date]:
    if start_date or end_date:
        start = _parse_day(start_date or end_date)
        end = _parse_day(end_date or start_date)
    else:
        start = end = _parse_day(report_date)
    if end < start:
        start, end = end, start
    day_count = (end - start).days + 1
    if day_count > MAX_EXPORT_DAYS:
        raise ValueError(f"Service Desk KPI export supports up to {MAX_EXPORT_DAYS} days at a time")
    return [start + timedelta(days=offset) for offset in range(day_count)]


def _write_kpi_table_sheet(
    ws,
    current: dict[str, Any],
    previous: dict[str, Any],
    *,
    row_key: str = "rows",
    title_prefix: str = "KPI Report",
    first_column_label: str = "Engineer / queue",
) -> None:
    rows = _email_style_rows(current, previous, row_key=row_key)
    max_col = 13

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max_col)
    ws.cell(1, 1).value = f"{title_prefix} for {_long_date(current['date'])}"
    ws.cell(1, 1).font = Font(bold=True, size=14)

    header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
    date_fill = PatternFill("solid", fgColor=_DATE_FILL)
    left_fill = PatternFill("solid", fgColor=_LEFT_FILL)
    alt_fill = PatternFill("solid", fgColor=_ALT_FILL)
    thin = Side(style="thin", color=_BORDER)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.cell(4, 1).value = "Date"
    ws.cell(4, 1).fill = header_fill
    ws.merge_cells(start_row=4, start_column=2, end_row=4, end_column=7)
    ws.cell(4, 2).value = _column_date(previous["date"])
    ws.merge_cells(start_row=4, start_column=8, end_row=4, end_column=13)
    ws.cell(4, 8).value = _column_date(current["date"])

    headers = [
        first_column_label,
        "Inbound Calls",
        "Avg Call Time",
        "Assigned",
        "Solved",
        "Outstanding",
        "FCR proxy",
        "Inbound Calls",
        "Avg Call Time",
        "Assigned",
        "Solved",
        "Outstanding",
        "FCR proxy",
    ]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(5, col)
        cell.value = header
        cell.fill = header_fill
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    for col in range(1, max_col + 1):
        for row in (4, 5):
            cell = ws.cell(row, col)
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if row == 5:
                cell.fill = header_fill
            elif row == 4:
                cell.fill = date_fill
            if col == 1 and row == 4:
                cell.fill = header_fill

    row_index = 6
    for item in rows:
        fill = alt_fill if row_index % 2 == 0 else PatternFill("solid", fgColor=_WHITE)
        values = _kpi_table_values(item)
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row_index, col)
            cell.value = value
            cell.border = border
            cell.fill = left_fill if col == 1 else fill
            cell.alignment = Alignment(
                horizontal="left" if col == 1 else "center",
                vertical="center",
                wrap_text=True,
            )
            if col in {7, 13} and isinstance(value, (int, float)):
                cell.number_format = "0%"
        row_index += 1

    widths = [28, 11, 12, 10, 10, 12, 10, 11, 12, 10, 10, 12, 10]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A6"
    ws.sheet_view.showGridLines = False


def _write_grouped_kpi_table_sheet(ws, reports: list[dict[str, Any]]) -> None:
    rows = _grouped_email_style_rows_for_reports(reports)
    max_col = 2 + len(reports) * 6

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max_col)
    ws.cell(1, 1).value = _report_title(reports)
    ws.cell(1, 1).font = Font(bold=True, size=14)

    date_fill = PatternFill("solid", fgColor=_DATE_FILL)
    header_fill = PatternFill("solid", fgColor=_HEADER_FILL)
    body_fill = PatternFill("solid", fgColor=_WHITE)
    alt_fill = PatternFill("solid", fgColor="F9FBFD")
    total_fill = PatternFill("solid", fgColor="EEF3F8")
    report_total_fill = PatternFill("solid", fgColor="244C6D")
    thin = Side(style="thin", color=_BORDER)
    double = Side(style="double", color=_SEPARATOR)

    def table_border(col: int, *, top: Side | None = thin, bottom: Side | None = thin) -> Border:
        first_metric_col = col == 3
        last_metric_col_before_next_day = col >= 3 and (col - 2) % 6 == 0 and col < max_col
        return Border(
            left=double if first_metric_col else thin,
            right=double if last_metric_col_before_next_day else thin,
            top=top,
            bottom=bottom,
        )

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=2)
    ws.cell(2, 1).value = "Date"
    ws.cell(2, 1).fill = date_fill
    ws.cell(2, 1).font = Font(bold=True)
    col_index = 3
    for report in reports:
        ws.merge_cells(start_row=2, start_column=col_index, end_row=2, end_column=col_index + 5)
        ws.cell(2, col_index).value = _column_date(report["date"])
        ws.cell(2, col_index).font = Font(bold=True)
        col_index += 6

    metric_headers = [
        "Inbound Calls",
        "Avg Call Time",
        "Assigned",
        "Solved",
        "Outstanding",
        "FCR proxy",
    ]
    headers = ["Groups", "Assignee"] + metric_headers * len(reports)
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(3, col)
        cell.value = header
        cell.fill = header_fill
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = table_border(col, bottom=double)

    for col in range(1, max_col + 1):
        for row in (2, 3):
            cell = ws.cell(row, col)
            cell.border = table_border(col, bottom=double if row == 3 else thin)
            cell.fill = date_fill if row == 2 else header_fill
            if row == 2:
                cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    row_index = 4
    for item in rows:
        is_total = item.get("row_type") in {"report_total", "group_total"}
        is_group_total = item.get("row_type") == "group_total"
        fill = (
            report_total_fill if item.get("row_type") == "report_total"
            else total_fill if is_total
            else alt_fill if row_index % 2 == 0
            else body_fill
        )
        values = _grouped_kpi_table_values(item)
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row_index, col)
            cell.value = value
            cell.border = table_border(col, top=double if is_group_total else thin, bottom=None if item.get("row_type") == "report_total" else thin)
            cell.fill = fill
            cell.font = Font(
                bold=is_total or col <= 2,
                color=_WHITE if item.get("row_type") == "report_total" else _TEXT,
            )
            cell.alignment = Alignment(
                horizontal="left" if col <= 2 else "center",
                vertical="center",
                wrap_text=True,
            )
            if col >= 3 and (col - 2) % 6 == 0 and isinstance(value, (int, float)):
                cell.number_format = "0%"
        row_index += 1

    widths = [20, 25] + [10, 11, 9, 9, 11, 9] * len(reports)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A4"
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def _write_detail_sheet(ws, current: dict[str, Any]) -> None:
    summary = current.get("summary", {})
    coverage = current.get("coverage", {})
    ws.cell(1, 1).value = f"Service Desk KPI details for {_long_date(current['date'])}"
    ws.cell(1, 1).font = Font(bold=True, size=14)

    summary_rows = [
        ("Active owners", summary.get("active_engineers", 0)),
        ("Inbound calls", summary.get("inbound_calls", 0)),
        ("Avg call time", _seconds_duration(summary.get("avg_call_time_seconds"))),
        ("Assigned", summary.get("new_assigned_tickets", 0)),
        ("Solved", summary.get("same_day_solved_tickets", 0)),
        ("Outstanding", summary.get("outstanding_tickets", 0)),
        ("Reply time", _duration(summary.get("median_customer_reply_hours"))),
        ("Single-cycle", f"{summary.get('single_cycle_solve_rate_pct', 0)}%"),
    ]
    for idx, (label, value) in enumerate(summary_rows, start=3):
        ws.cell(idx, 1).value = label
        ws.cell(idx, 1).font = Font(bold=True)
        ws.cell(idx, 2).value = value

    ws.cell(3, 4).value = "Coverage"
    ws.cell(3, 4).font = Font(bold=True)
    ws.cell(4, 4).value = "Assignee email coverage"
    ws.cell(4, 5).value = f"{coverage.get('assignee_email_coverage_pct', 0)}%"
    ws.cell(5, 4).value = "Reply author coverage"
    ws.cell(5, 5).value = f"{coverage.get('comment_author_email_coverage_pct', 0)}%"

    headers = [
        "Engineer / queue",
        "Email",
        "Inbound calls",
        "Avg call time",
        "Assigned",
        "Solved",
        "Outstanding",
        "First public reply",
        "Customer reply by author",
        "Solved ticket age",
        "Single-cycle",
        "Single-cycle sample",
        "SLA breach",
        "SLA sample",
    ]
    start_row = 10
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(start_row, col)
        cell.value = header
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor=_HEADER_FILL)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_index, row in enumerate(current.get("rows", []), start=start_row + 1):
        ws.cell(row_index, 1).value = _person_label(row)
        ws.cell(row_index, 2).value = "" if _synthetic_owner(row.get("email")) else row.get("email", "")
        ws.cell(row_index, 3).value = row.get("inbound_calls", 0)
        ws.cell(row_index, 4).value = _seconds_duration(row.get("avg_call_time_seconds"))
        ws.cell(row_index, 5).value = row.get("new_assigned_tickets", 0)
        ws.cell(row_index, 6).value = row.get("solved_tickets", 0)
        ws.cell(row_index, 7).value = row.get("outstanding_tickets", 0)
        ws.cell(row_index, 8).value = _duration(row.get("median_first_public_reply_hours"))
        ws.cell(row_index, 9).value = _duration(row.get("median_customer_reply_hours"))
        ws.cell(row_index, 10).value = _duration(row.get("median_resolution_hours"))
        ws.cell(row_index, 11).value = f"{row.get('single_cycle_solve_rate_pct', 0)}%"
        ws.cell(row_index, 12).value = _fraction(row.get("single_cycle_solved"), row.get("single_cycle_total"), "solved tickets")
        ws.cell(row_index, 13).value = f"{row.get('sla_breach_rate_pct', 0)}%" if row.get("sla_known") else ""
        ws.cell(row_index, 14).value = _fraction(row.get("sla_breached"), row.get("sla_known"), "known SLA tickets")

    notes_row = start_row + len(current.get("rows", [])) + 3
    ws.cell(notes_row, 1).value = "Notes"
    ws.cell(notes_row, 1).font = Font(bold=True)
    for idx, note in enumerate((current.get("warnings") or []) + (current.get("notes") or []), start=notes_row + 1):
        ws.cell(idx, 1).value = note
        ws.merge_cells(start_row=idx, start_column=1, end_row=idx, end_column=8)

    widths = [28, 30, 14, 14, 16, 14, 16, 16, 20, 16, 14, 24, 14, 24]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A11"
    ws.sheet_view.showGridLines = False


def _draw_pdf_report(pdf: "_SimplePdf", reports: list[dict[str, Any]]) -> None:
    margin = 24
    y = 580
    pdf.text(margin, y, _report_title(reports), size=15, bold=True)

    table_bottom = _draw_pdf_kpi_table(
        pdf,
        reports,
        margin,
        y - 30,
        title="Service Desk KPI by group and assignee",
    )
    _draw_pdf_warnings(pdf, reports[-1], margin, table_bottom - 20)


def _draw_pdf_summary(pdf: "_SimplePdf", current: dict[str, Any], x: float, y: float) -> None:
    summary = current.get("summary", {})
    cards = [
        ("Active owners", _int(summary.get("active_engineers")), "People or queues"),
        ("Inbound", _int(summary.get("inbound_calls")), "CXone calls"),
        ("Avg call", _seconds_duration(summary.get("avg_call_time_seconds")), "Agent talk time"),
        ("Assigned", _int(summary.get("new_assigned_tickets")), "Snapshot owner"),
        ("Solved", _int(summary.get("same_day_solved_tickets")), "Status changed"),
        ("Outstanding", _int(summary.get("outstanding_tickets")), "New/open/pending/hold"),
    ]
    width = 116
    gap = 7
    for idx, (title, value, detail) in enumerate(cards):
        cx = x + idx * (width + gap)
        pdf.rect(cx, y, width, 48, fill=(0.98, 0.98, 0.98), stroke=(0.82, 0.82, 0.82))
        pdf.text(cx + 6, y + 34, title.upper(), size=6.5, bold=True, color=(0.42, 0.42, 0.42))
        pdf.text(cx + 6, y + 17, value, size=15, bold=True)
        pdf.text(cx + 6, y + 6, detail, size=6.5, color=(0.40, 0.40, 0.40))


def _draw_pdf_kpi_table(
    pdf: "_SimplePdf",
    reports: list[dict[str, Any]],
    x: float,
    y: float,
    *,
    title: str = "Service Desk KPI table",
) -> float:
    rows = _grouped_email_style_rows_for_reports(reports)
    row_h = 8.8
    top_header_h = 12
    header_h = 17
    bottom_margin = 36
    group_w = 92
    name_w = 132
    metric_count = len(reports) * 6
    metric_w = (744 - group_w - name_w) / max(metric_count, 1)
    col_widths = [group_w, name_w] + [metric_w] * metric_count
    metric_headers = [
        "Inbound\nCalls",
        "Avg\nCall Time",
        "Assigned",
        "Solved",
        "Outstanding",
        "FCR\nproxy",
    ]
    headers = ["Groups", "Assignee"] + metric_headers * len(reports)

    pdf.text(x, y + 10, title, size=9, bold=True)
    current_y = y - 8

    def draw_header(at_y: float) -> float:
        pdf.rect(x, at_y, col_widths[0], top_header_h, fill=(0.89, 0.91, 0.94), stroke=(0.85, 0.87, 0.91))
        pdf.text(x + 4, at_y + 4, "Date", size=6.5, bold=True)
        pdf.rect(x + col_widths[0], at_y, col_widths[1], top_header_h, fill=(0.89, 0.91, 0.94), stroke=(0.85, 0.87, 0.91))
        px = x + col_widths[0] + col_widths[1]
        day_w = metric_w * 6
        for report in reports:
            pdf.rect(px, at_y, day_w, top_header_h, fill=(0.89, 0.91, 0.94), stroke=(0.85, 0.87, 0.91))
            pdf.center_text(px, at_y + 4, day_w, _column_date(report["date"]), size=6.5, bold=True)
            px += day_w

        header_y = at_y - header_h
        col_x = x
        for idx, label in enumerate(headers):
            pdf.rect(col_x, header_y, col_widths[idx], header_h, fill=(0.89, 0.91, 0.94), stroke=(0.85, 0.87, 0.91))
            if "\n" in label:
                first, second = label.split("\n", 1)
                pdf.center_text(col_x, header_y + 9, col_widths[idx], first, size=5.4, bold=True)
                pdf.center_text(col_x, header_y + 3, col_widths[idx], second, size=5.4, bold=True)
            elif idx in {0, 1}:
                pdf.text(col_x + 4, header_y + 7, label, size=6.2, bold=True)
            else:
                pdf.center_text(col_x, header_y + 7, col_widths[idx], label, size=5.4, bold=True)
            col_x += col_widths[idx]
        return header_y - row_h

    current_y = draw_header(current_y)
    for index, item in enumerate(rows):
        if current_y < bottom_margin:
            pdf.add_page()
            current_y = 560
            pdf.text(x, current_y + 12, f"Service Desk KPI table, continued", size=9, bold=True)
            current_y = draw_header(current_y - 6)

        values = _grouped_kpi_table_values(item, pdf=True)
        is_total = item.get("row_type") in {"report_total", "group_total"}
        fill = (
            (0.87, 0.91, 0.96) if item.get("row_type") == "report_total"
            else (0.91, 0.94, 0.98) if is_total
            else (0.97, 0.98, 0.99) if index % 2 == 0
            else (1, 1, 1)
        )
        col_x = x
        for col_idx, value in enumerate(values):
            cell_fill = fill if is_total or col_idx <= 1 else fill
            pdf.rect(col_x, current_y, col_widths[col_idx], row_h, fill=cell_fill, stroke=(0.86, 0.88, 0.91))
            if col_idx <= 1:
                limit = 17 if col_idx == 0 else 25
                pdf.text(col_x + 3, current_y + 2.6, _truncate(str(value), limit), size=5.2, bold=is_total or col_idx == 0)
            else:
                pdf.center_text(col_x, current_y + 2.6, col_widths[col_idx], str(value), size=5.1, bold=is_total)
            col_x += col_widths[col_idx]
        current_y -= row_h

    return current_y


def _draw_pdf_warnings(pdf: "_SimplePdf", current: dict[str, Any], x: float, y: float) -> None:
    note_items = current.get("warnings") or []
    if not note_items:
        return
    if y < 105:
        pdf.add_page()
        y = 560
    pdf.text(x, y, "Warnings", size=9, bold=True)
    y -= 14

    for note in note_items[:5]:
        if y < 40:
            pdf.add_page()
            y = 560
        for line_idx, line in enumerate(_wrap(note, 135)):
            prefix = "- " if line_idx == 0 else "  "
            pdf.text(x, y, prefix + line, size=7, color=(0.35, 0.35, 0.35))
            y -= 9


def _email_style_rows(current: dict[str, Any], previous: dict[str, Any], *, row_key: str = "rows") -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for report, key in ((previous, "previous"), (current, "current")):
        for row in report.get(row_key, []):
            owner_key = str(row.get("email") or _person_label(row)).strip().lower()
            item = by_key.setdefault(owner_key, {"key": owner_key, "label": _person_label(row)})
            item[key] = row
            if key == "current":
                item["label"] = _person_label(row)

    out = [
        item for item in by_key.values()
        if _has_table_activity(item.get("current")) or _has_table_activity(item.get("previous"))
    ]
    out.sort(key=lambda item: (_row_rank(item["label"], row_key=row_key), item["label"].lower()))
    return out


def _grouped_email_style_rows(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    return _grouped_email_style_rows_for_reports([previous, current])


def _grouped_email_style_rows_for_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    date_keys = [str(report.get("date") or "") for report in reports]
    out: list[dict[str, Any]] = [{
        "key": "__report_total__",
        "group_label": "Total",
        "assignee_label": "Report total",
        "row_type": "report_total",
        "by_date": {
            str(report.get("date") or ""): _summary_metric_row(report)
            for report in reports
        },
    }]

    sections_by_date = {
        str(report.get("date") or ""): _sections_by_key(report)
        for report in reports
    }
    group_keys = []
    for report in reports:
        for section in report.get("group_sections") or []:
            key = str(section.get("group_key") or section.get("group_name") or "").strip().lower()
            if key and key not in group_keys:
                group_keys.append(key)
    group_keys.sort(key=lambda key: (_row_rank(_group_name_for_key(key, sections_by_date, date_keys), row_key="group_rows"), key))

    for group_key in group_keys:
        group_name = _group_name_for_key(group_key, sections_by_date, date_keys)
        out.append({
            "key": f"__group_total__:{group_key}",
            "group_label": group_name,
            "assignee_label": "Group total",
            "row_type": "group_total",
            "by_date": {
                date_key: (sections_by_date.get(date_key, {}).get(group_key, {}) or {}).get("total")
                for date_key in date_keys
            },
        })

        row_keys = []
        rows_by_date: dict[str, dict[str, dict[str, Any]]] = {}
        for date_key in date_keys:
            section = sections_by_date.get(date_key, {}).get(group_key, {}) or {}
            keyed_rows = {
                str(row.get("email") or _person_label(row)).strip().lower(): row
                for row in section.get("rows", [])
            }
            rows_by_date[date_key] = keyed_rows
            for key in keyed_rows:
                if key not in row_keys:
                    row_keys.append(key)
        row_keys.sort(key=lambda key: (
            _section_row_rank(group_name, _person_label(_first_row_for_key(key, rows_by_date, date_keys))),
            _person_label(_first_row_for_key(key, rows_by_date, date_keys)).lower(),
        ))

        for key in row_keys:
            row = _first_row_for_key(key, rows_by_date, date_keys)
            out.append({
                "key": f"{group_key}:{key}",
                "group_label": "",
                "assignee_label": _person_label(row),
                "row_type": str(row.get("row_type") or "assignee"),
                "by_date": {
                    date_key: rows_by_date.get(date_key, {}).get(key)
                    for date_key in date_keys
                },
            })

    return out


def _group_name_for_key(group_key: str, sections_by_date: dict[str, dict[str, dict[str, Any]]], date_keys: list[str]) -> str:
    for date_key in reversed(date_keys):
        section = sections_by_date.get(date_key, {}).get(group_key, {})
        name = str(section.get("group_name") or "").strip()
        if name:
            return name
    return group_key


def _first_row_for_key(
    row_key: str,
    rows_by_date: dict[str, dict[str, dict[str, Any]]],
    date_keys: list[str],
) -> dict[str, Any]:
    for date_key in reversed(date_keys):
        row = rows_by_date.get(date_key, {}).get(row_key)
        if row:
            return row
    return {}


def _sections_by_key(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(section.get("group_key") or section.get("group_name") or "").strip().lower(): section
        for section in report.get("group_sections", [])
    }


def _summary_metric_row(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {})
    return {
        "inbound_calls": summary.get("inbound_calls", 0),
        "avg_call_time_seconds": summary.get("avg_call_time_seconds"),
        "new_assigned_tickets": summary.get("new_assigned_tickets", 0),
        "same_day_solved_tickets": summary.get("same_day_solved_tickets", 0),
        "solved_tickets": summary.get("solved_tickets", 0),
        "outstanding_tickets": summary.get("outstanding_tickets", 0),
        "single_cycle_solved": summary.get("single_cycle_solved", 0),
        "single_cycle_total": summary.get("single_cycle_total", 0),
        "single_cycle_solve_rate_pct": summary.get("single_cycle_solve_rate_pct", 0),
    }


def _section_row_rank(group_name: str, display_name: str) -> tuple[int, int]:
    members = list(dict(service_desk_kpi.SERVICE_DESK_GROUPS).get(group_name, ()))
    normalized = " ".join(str(display_name or "").lower().split())
    if group_name == "Menu Maintenance" and normalized == "menu maintenance":
        return (0, 0)
    for index, member in enumerate(members):
        if " ".join(member.lower().split()) == normalized:
            return (0, index + (1 if group_name == "Menu Maintenance" else 0))
    if normalized == service_desk_kpi.BLANK_OWNER_LABEL.lower() or normalized.endswith(" queue"):
        return (1, 0)
    return (2, len(members) + 1)


def _has_table_activity(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return any(
        int(row.get(key) or 0) > 0
        for key in ("inbound_calls", "new_assigned_tickets", "same_day_solved_tickets", "solved_tickets", "outstanding_tickets")
    )


def _kpi_table_values(item: dict[str, Any], *, pdf: bool = False) -> list[Any]:
    previous = item.get("previous")
    current = item.get("current")
    return [
        item["label"],
        _count(previous, "inbound_calls") if previous else ("-" if pdf else None),
        _seconds_duration((previous or {}).get("avg_call_time_seconds")) if previous else ("-" if pdf else None),
        _count(previous, "new_assigned_tickets"),
        _count(previous, "same_day_solved_tickets"),
        _count(previous, "outstanding_tickets"),
        _fcr(previous, pdf=pdf),
        _count(current, "inbound_calls") if current else ("-" if pdf else None),
        _seconds_duration((current or {}).get("avg_call_time_seconds")) if current else ("-" if pdf else None),
        _count(current, "new_assigned_tickets"),
        _count(current, "same_day_solved_tickets"),
        _count(current, "outstanding_tickets"),
        _fcr(current, pdf=pdf),
    ]


def _grouped_kpi_table_values(item: dict[str, Any], *, pdf: bool = False) -> list[Any]:
    if "by_date" in item:
        values: list[Any] = [item.get("group_label", ""), item.get("assignee_label", "")]
        for row in item.get("by_date", {}).values():
            values.extend(_metric_values(row, pdf=pdf))
        return values
    previous = item.get("previous")
    current = item.get("current")
    return [
        item.get("group_label", ""),
        item.get("assignee_label", ""),
        *_metric_values(previous, pdf=pdf),
        *_metric_values(current, pdf=pdf),
    ]


def _metric_values(row: dict[str, Any] | None, *, pdf: bool = False) -> list[Any]:
    return [
        _count(row, "inbound_calls") if row else ("-" if pdf else None),
        _seconds_duration((row or {}).get("avg_call_time_seconds")) if row else ("-" if pdf else None),
        _count(row, "new_assigned_tickets"),
        _count(row, "same_day_solved_tickets"),
        _count(row, "outstanding_tickets"),
        _fcr(row, pdf=pdf),
    ]


def _row_rank(label: str, *, row_key: str = "rows") -> int:
    normalized = " ".join(str(label or "").lower().split())
    if row_key == "group_rows":
        try:
            return _GROUP_ROW_ORDER.index(normalized)
        except ValueError:
            return len(_GROUP_ROW_ORDER) + 1
    try:
        return _ROW_ORDER.index(normalized)
    except ValueError:
        return len(_ROW_ORDER) + 1


def _person_label(row: dict[str, Any]) -> str:
    display = str(row.get("display_name") or "").strip()
    email = str(row.get("email") or "").strip()
    if display:
        return display
    if email and not _synthetic_owner(email):
        return email
    return "Unknown"


def _synthetic_owner(value: Any) -> bool:
    safe = str(value or "").strip().lower()
    return safe == service_desk_kpi.BLANK_OWNER_EMAIL or safe.startswith(service_desk_kpi.NAME_OWNER_PREFIX) or safe.startswith("__")


def _count(row: dict[str, Any] | None, key: str) -> int:
    return int((row or {}).get(key) or 0)


def _fcr(row: dict[str, Any] | None, *, pdf: bool) -> Any:
    if not row or int(row.get("single_cycle_total") or 0) <= 0:
        return "-" if pdf else None
    pct = int(row.get("single_cycle_solve_rate_pct") or 0)
    return f"{pct}%" if pdf else pct / 100


def _long_date(value: str) -> str:
    try:
        return date.fromisoformat(value[:10]).strftime("%B %-d, %Y")
    except Exception:
        try:
            return date.fromisoformat(value[:10]).strftime("%B %#d, %Y")
        except Exception:
            return value


def _column_date(value: str) -> str:
    try:
        dt = date.fromisoformat(value[:10])
        return f"{dt.month}/{dt.day}/{dt.year}"
    except Exception:
        return value


def _report_title(reports: list[dict[str, Any]]) -> str:
    if len(reports) == 1:
        return f"KPI Report for {_long_date(str(reports[0].get('date') or ''))}"
    return f"KPI Report for {_long_date(str(reports[0].get('date') or ''))} - {_long_date(str(reports[-1].get('date') or ''))}"


def _safe_date_range(reports: list[dict[str, Any]]) -> str:
    dates = [str(report.get("date") or "") for report in reports if report.get("date")]
    if not dates:
        return "report"
    if len(dates) == 1:
        return dates[0]
    return f"{dates[0]}_to_{dates[-1]}"


def _duration(value: Any) -> str:
    if value is None:
        return "-"
    try:
        hours = float(value)
    except Exception:
        return "-"
    if hours < 1:
        return f"{max(0, round(hours * 60))}m"
    if hours < 24:
        return f"{hours:.1f}h" if hours % 1 else f"{int(hours)}h"
    return f"{round(hours)}h"


def _seconds_duration(value: Any) -> str:
    if value is None:
        return "-"
    try:
        seconds = max(0, int(round(float(value))))
    except Exception:
        return "-"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{remaining:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{remaining:02d}"


def _fraction(part: Any, total: Any, label: str) -> str:
    return f"{int(part or 0)} of {int(total or 0)} {label}"


def _int(value: Any) -> str:
    return f"{int(value or 0):,}"


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _truncate(text: str, limit: int) -> str:
    safe = str(text or "")
    return safe if len(safe) <= limit else safe[: max(0, limit - 1)] + "."


class _SimplePdf:
    width = 792
    height = 612

    def __init__(self) -> None:
        self.pages: list[list[str]] = []
        self._ops: list[str] = []

    def add_page(self) -> None:
        if self._ops:
            self.pages.append(self._ops)
        self._ops = []

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
    ) -> None:
        if fill:
            self._ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
            self._ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
        if stroke:
            self._ops.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG")
            self._ops.append("0.4 w")
            self._ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = 8,
        bold: bool = False,
        color: tuple[float, float, float] = (0.05, 0.05, 0.05),
    ) -> None:
        font = "F2" if bold else "F1"
        self._ops.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg")
        self._ops.append(f"BT /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({_pdf_escape(text)}) Tj ET")

    def center_text(
        self,
        x: float,
        y: float,
        width: float,
        text: str,
        *,
        size: float = 8,
        bold: bool = False,
        color: tuple[float, float, float] = (0.05, 0.05, 0.05),
    ) -> None:
        approx_w = len(str(text or "")) * size * 0.46
        self.text(x + max(1, (width - approx_w) / 2), y, text, size=size, bold=bold, color=color)

    def render(self) -> bytes:
        if self._ops:
            self.pages.append(self._ops)
            self._ops = []
        objects: list[bytes] = []

        def add(obj: bytes) -> int:
            objects.append(obj)
            return len(objects)

        catalog_id = add(b"<< /Type /Catalog /Pages 2 0 R >>")
        pages_placeholder_id = add(b"")
        font_regular_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        font_bold_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

        page_ids: list[int] = []
        for ops in self.pages:
            stream = ("\n".join(ops) + "\n").encode("latin-1", "replace")
            content_id = add(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream")
            page = (
                f"<< /Type /Page /Parent {pages_placeholder_id} 0 R "
                f"/MediaBox [0 0 {self.width} {self.height}] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
            page_ids.append(add(page))

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects[pages_placeholder_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
        objects[catalog_id - 1] = b"<< /Type /Catalog /Pages 2 0 R >>"

        output = BytesIO()
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for idx, obj in enumerate(objects, start=1):
            offsets.append(output.tell())
            output.write(f"{idx} 0 obj\n".encode("ascii"))
            output.write(obj)
            output.write(b"\nendobj\n")
        xref_start = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_start}\n%%EOF\n".encode("ascii")
        )
        return output.getvalue()


def _pdf_escape(value: str) -> str:
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    safe = str(value or "")
    for src, dst in replacements.items():
        safe = safe.replace(src, dst)
    safe = safe.encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
