"""File exports for the ESIP Impact report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services import impact_metrics
from app.services.service_desk_kpi_export import PDF_MEDIA_TYPE, XLSX_MEDIA_TYPE, _SimplePdf

_HEADER_FILL = "E2E8F0"
_LEFT_FILL = "F8FAFC"
_ALT_FILL = "F8FBFD"
_WHITE = "FFFFFF"
_BORDER = "D8DEE8"


@dataclass(frozen=True)
class ImpactExportFile:
    content: bytes
    filename: str
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


def get_export_file(
    *,
    rollout_date: str | None = None,
    end_date: str | None = None,
    file_format: str = "pdf",
) -> ImpactExportFile:
    """Build a direct-download Impact report file."""
    report = impact_metrics.get_impact_report(rollout_date=rollout_date, end_date=end_date)
    safe_range = _safe_range(report)
    requested = (file_format or "pdf").strip().lower()
    if requested == "pdf":
        return ImpactExportFile(
            content=build_pdf_export(report),
            filename=f"esip-impact-{safe_range}.pdf",
            media_type=PDF_MEDIA_TYPE,
            metadata=_export_metadata(report),
        )
    if requested in {"xlsx", "excel"}:
        return ImpactExportFile(
            content=build_excel_export(report),
            filename=f"esip-impact-{safe_range}.xlsx",
            media_type=XLSX_MEDIA_TYPE,
            metadata=_export_metadata(report),
        )
    raise ValueError("Unsupported Impact export format")


def build_excel_export(report: dict[str, Any]) -> bytes:
    wb = Workbook()
    summary_ws = wb.active
    summary_ws.title = "Impact Report"
    _write_summary_sheet(summary_ws, report)
    _write_periods_sheet(wb.create_sheet("Periods"), report)
    _write_workflows_sheet(wb.create_sheet("Workflows"), report)
    _write_engineers_sheet(wb.create_sheet("Engineer View"), report)
    _write_notes_sheet(wb.create_sheet("Notes"), report)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def build_pdf_export(report: dict[str, Any]) -> bytes:
    pdf = _SimplePdf()
    pdf.add_page()
    _draw_pdf_report(pdf, report)
    return pdf.render()


def _write_summary_sheet(ws, report: dict[str, Any]) -> None:
    report_meta = report.get("report") or {}
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    ws.cell(1, 1).value = report_meta.get("title") or "ESIP Early Impact Report"
    ws.cell(1, 1).font = Font(bold=True, size=14)
    ws.cell(2, 1).value = report_meta.get("subtitle") or "Cautious before/ramp/after view for leadership"
    ws.cell(3, 1).value = report_meta.get("headline") or ""
    ws.cell(5, 1).value = "Rollout date"
    ws.cell(5, 2).value = (report.get("range") or {}).get("rollout_date")
    ws.cell(6, 1).value = "Generated"
    ws.cell(6, 2).value = report_meta.get("generated_at")

    rows = [
        [
            item.get("label") or "",
            _display_value(item.get("value"), item.get("suffix")),
            item.get("detail") or "",
        ]
        for item in report_meta.get("kpis") or []
    ]
    _write_table(
        ws,
        start_row=8,
        headers=["KPI", "Value", "Detail"],
        rows=rows,
        widths=[30, 18, 60],
    )
    ws.sheet_view.showGridLines = False


def _write_periods_sheet(ws, report: dict[str, Any]) -> None:
    rows = []
    range_data = report.get("range") or {}
    adoption = (report.get("adoption") or {}).get("periods") or {}
    response = (report.get("response_speed") or {}).get("periods") or {}
    outcomes = (report.get("outcomes") or {}).get("periods") or {}
    quality = (report.get("quality") or {}).get("periods") or {}
    for key in ("baseline", "ramp", "post"):
        period_range = range_data.get(key) or {}
        period_adoption = adoption.get(key) or {}
        period_response = response.get(key) or {}
        period_outcomes = outcomes.get(key) or {}
        period_quality = quality.get(key) or {}
        rows.append([
            period_range.get("label") or key,
            period_range.get("start_date") or "",
            period_range.get("end_date") or "",
            period_adoption.get("events") or 0,
            period_adoption.get("active_users") or 0,
            period_adoption.get("assisted_tickets") or 0,
            period_response.get("replies") or 0,
            period_response.get("median_latency_hours"),
            period_outcomes.get("solved_tickets") or 0,
            period_outcomes.get("median_resolution_hours"),
            period_quality.get("documentation_gap_signals") or 0,
        ])
    _write_title(ws, "ESIP Early Impact Report", "Before, ramp-up, after")
    _write_table(
        ws,
        start_row=4,
        headers=[
            "Period",
            "Start",
            "End",
            "ESIP touches",
            "Active users",
            "Assisted tickets",
            "Replies",
            "Median reply hours",
            "Solved tickets",
            "Median resolution hours",
            "Doc gap signals",
        ],
        rows=rows,
        widths=[18, 14, 14, 14, 14, 16, 12, 18, 16, 22, 18],
    )


def _write_workflows_sheet(ws, report: dict[str, Any]) -> None:
    rows = [
        [
            item.get("period") or "",
            item.get("workflow") or "",
            item.get("events") or 0,
            item.get("users") or 0,
            item.get("tickets") or 0,
        ]
        for item in (report.get("adoption") or {}).get("workflows") or []
    ]
    _write_title(ws, "ESIP Early Impact Report", "Where ESIP was used")
    _write_table(
        ws,
        start_row=4,
        headers=["Period", "Workflow", "Events", "Users", "Tickets"],
        rows=rows,
        widths=[16, 34, 12, 12, 12],
    )


def _write_engineers_sheet(ws, report: dict[str, Any]) -> None:
    display_period = ((report.get("summary") or {}).get("display_period") or (report.get("range") or {}).get("display_period") or "post")
    rows = []
    for item in report.get("users") or []:
        usage = (item.get("usage") or {}).get(display_period) or {}
        replies = (item.get("replies") or {}).get(display_period) or {}
        solved = (item.get("solved") or {}).get(display_period) or {}
        rows.append([
            item.get("display_name") or item.get("email") or item.get("key") or "",
            item.get("email") or "",
            item.get("confidence") or "",
            item.get("confidence_reason") or "",
            usage.get("events") or 0,
            usage.get("active_days") or 0,
            usage.get("assisted_tickets") or 0,
            replies.get("median_latency_hours"),
            solved.get("solved_tickets") or 0,
            item.get("outstanding_tickets") or 0,
        ])
    _write_title(ws, "ESIP Early Impact Report", f"Engineer view: {display_period}")
    _write_table(
        ws,
        start_row=4,
        headers=[
            "Engineer",
            "Email",
            "Confidence",
            "Confidence reason",
            "ESIP touches",
            "Active days",
            "Assisted tickets",
            "Median reply hours",
            "Solved",
            "Outstanding",
        ],
        rows=rows,
        widths=[26, 34, 16, 46, 14, 12, 16, 18, 12, 14],
    )


def _write_notes_sheet(ws, report: dict[str, Any]) -> None:
    rows = []
    for label, values in (
        ("What improved", (report.get("insights") or {}).get("wins") or []),
        ("What still needs work", (report.get("insights") or {}).get("concerns") or []),
        ("Data notes", (report.get("insights") or {}).get("caveats") or []),
        ("Warnings", report.get("warnings") or []),
    ):
        for value in values:
            rows.append([label, value])
    _write_title(ws, "ESIP Early Impact Report", "Notes")
    _write_table(ws, start_row=4, headers=["Section", "Note"], rows=rows, widths=[24, 100])


def _write_title(ws, title: str, subtitle: str) -> None:
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    ws.cell(1, 1).value = title
    ws.cell(1, 1).font = Font(bold=True, size=14)
    ws.cell(2, 1).value = subtitle
    ws.cell(2, 1).font = Font(bold=True, color="44546A")
    ws.sheet_view.showGridLines = False


def _write_table(ws, *, start_row: int, headers: list[str], rows: list[Any], widths: list[int]) -> None:
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

    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = ws.cell(start_row + 1, 1).coordinate


def _draw_pdf_report(pdf: _SimplePdf, report: dict[str, Any]) -> None:
    meta = report.get("report") or {}
    range_data = report.get("range") or {}
    x = 32
    y = 570
    pdf.text(x, y, meta.get("title") or "ESIP Early Impact Report", size=15, bold=True)
    pdf.text(x, y - 15, meta.get("subtitle") or "Cautious before/ramp/after view for leadership", size=8, color=(0.40, 0.40, 0.40))
    pdf.text(600, y, f"Rollout: {range_data.get('rollout_date') or '-'}", size=7, color=(0.35, 0.35, 0.35))
    pdf.text(600, y - 12, f"Generated: {_short_datetime(meta.get('generated_at'))}", size=7, color=(0.35, 0.35, 0.35))
    y -= 45
    for line in _wrap(meta.get("headline") or "", 112)[:2]:
        pdf.text(x, y, line, size=8, color=(0.25, 0.25, 0.25))
        y -= 11

    y -= 12
    _draw_pdf_kpis(pdf, meta.get("kpis") or [], x, y)
    y -= 72
    y = _draw_pdf_period_table(pdf, report, x, y)
    y = _draw_pdf_workflows(pdf, report, x, y - 18)
    _draw_pdf_notes(pdf, report, x, y - 18)


def _draw_pdf_kpis(pdf: _SimplePdf, items: list[dict[str, Any]], x: float, y: float) -> None:
    width = 176
    gap = 8
    for idx, item in enumerate(items[:4]):
        cx = x + idx * (width + gap)
        pdf.rect(cx, y, width, 54, fill=(0.98, 0.98, 0.98), stroke=(0.82, 0.82, 0.82))
        pdf.text(cx + 7, y + 38, str(item.get("label") or "").upper(), size=6.3, bold=True, color=(0.42, 0.42, 0.42))
        pdf.text(cx + 7, y + 21, _display_value(item.get("value"), item.get("suffix")), size=14, bold=True)
        pdf.text(cx + 7, y + 8, _truncate(item.get("detail") or "", 32), size=6.3, color=(0.40, 0.40, 0.40))


def _draw_pdf_period_table(pdf: _SimplePdf, report: dict[str, Any], x: float, y: float) -> float:
    pdf.text(x, y, "Before, ramp-up, after", size=9, bold=True)
    y -= 17
    headers = ["Period", "Touches", "Users", "Assisted", "Replies", "Solved", "Doc gaps"]
    widths = [126, 74, 64, 76, 70, 70, 72]
    y = _draw_pdf_table_header(pdf, x, y, headers, widths)
    range_data = report.get("range") or {}
    adoption = (report.get("adoption") or {}).get("periods") or {}
    response = (report.get("response_speed") or {}).get("periods") or {}
    outcomes = (report.get("outcomes") or {}).get("periods") or {}
    quality = (report.get("quality") or {}).get("periods") or {}
    for index, key in enumerate(("baseline", "ramp", "post")):
        row = [
            (range_data.get(key) or {}).get("label") or key,
            _int((adoption.get(key) or {}).get("events")),
            _int((adoption.get(key) or {}).get("active_users")),
            _int((adoption.get(key) or {}).get("assisted_tickets")),
            _int((response.get(key) or {}).get("replies")),
            _int((outcomes.get(key) or {}).get("solved_tickets")),
            _int((quality.get(key) or {}).get("documentation_gap_signals")),
        ]
        y = _draw_pdf_table_row(pdf, x, y, row, widths, fill=(0.97, 0.98, 0.99) if index % 2 == 0 else (1, 1, 1))
    return y


def _draw_pdf_workflows(pdf: _SimplePdf, report: dict[str, Any], x: float, y: float) -> float:
    if y < 140:
        pdf.add_page()
        y = 560
    pdf.text(x, y, "Where ESIP was used", size=9, bold=True)
    y -= 17
    headers = ["Period", "Workflow", "Events", "Users", "Tickets"]
    widths = [78, 220, 70, 70, 70]
    y = _draw_pdf_table_header(pdf, x, y, headers, widths)
    for index, item in enumerate(((report.get("adoption") or {}).get("workflows") or [])[:8]):
        row = [
            item.get("period") or "",
            _truncate(item.get("workflow") or "", 36),
            _int(item.get("events")),
            _int(item.get("users")),
            _int(item.get("tickets")),
        ]
        y = _draw_pdf_table_row(pdf, x, y, row, widths, fill=(0.97, 0.98, 0.99) if index % 2 == 0 else (1, 1, 1))
    return y


def _draw_pdf_notes(pdf: _SimplePdf, report: dict[str, Any], x: float, y: float) -> float:
    if y < 150:
        pdf.add_page()
        y = 560
    columns = [
        ("What improved", (report.get("insights") or {}).get("wins") or []),
        ("What still needs work", (report.get("insights") or {}).get("concerns") or []),
    ]
    width = 344
    for idx, (title, items) in enumerate(columns):
        cx = x + idx * (width + 16)
        pdf.text(cx, y, title, size=9, bold=True)
        cy = y - 18
        for item in items[:4] or ["No signals in this range."]:
            pdf.rect(cx, cy - 2, width, 22, fill=(0.98, 0.98, 0.98), stroke=(0.86, 0.88, 0.91))
            pdf.text(cx + 6, cy + 7, _truncate(item, 72), size=6.5, color=(0.24, 0.24, 0.24))
            cy -= 27
    return y - 130


def _draw_pdf_table_header(pdf: _SimplePdf, x: float, y: float, headers: list[str], widths: list[float]) -> float:
    col_x = x
    for header, width in zip(headers, widths):
        pdf.rect(col_x, y, width, 14, fill=(0.89, 0.91, 0.94), stroke=(0.85, 0.87, 0.91))
        pdf.text(col_x + 3, y + 4.5, header, size=6.2, bold=True)
        col_x += width
    return y - 10


def _draw_pdf_table_row(
    pdf: _SimplePdf,
    x: float,
    y: float,
    values: list[Any],
    widths: list[float],
    *,
    fill: tuple[float, float, float],
) -> float:
    col_x = x
    row_h = 12
    for index, (value, width) in enumerate(zip(values, widths)):
        pdf.rect(col_x, y, width, row_h, fill=fill, stroke=(0.86, 0.88, 0.91))
        if index <= 1:
            pdf.text(col_x + 3, y + 3.8, str(value), size=5.8, bold=index == 0)
        else:
            pdf.center_text(col_x, y + 3.8, width, str(value), size=5.8)
        col_x += width
    return y - row_h


def _safe_range(report: dict[str, Any]) -> str:
    range_data = report.get("range") or {}
    start = _safe_date(range_data.get("rollout_date"))
    end = _safe_date(range_data.get("as_of_date"))
    if start and end and start != end:
        return f"{start}_to_{end}"
    return start or end or "impact"


def _export_metadata(report: dict[str, Any]) -> dict[str, Any]:
    range_data = report.get("range") or {}
    report_meta = report.get("report") or {}
    return {
        "rollout_date": range_data.get("rollout_date"),
        "as_of_date": range_data.get("as_of_date"),
        "generated_at": report_meta.get("generated_at"),
    }


def _safe_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except Exception:
        return raw[:10].replace("/", "-")


def _short_datetime(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return "-"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16]


def _display_value(value: Any, suffix: Any = None) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        formatted = f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{value:,.0f}"
    else:
        formatted = str(value)
    return f"{formatted}{suffix or ''}"


def _int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def _wrap(text: Any, width: int) -> list[str]:
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


def _truncate(value: Any, limit: int) -> str:
    safe = str(value or "")
    return safe if len(safe) <= limit else safe[: max(0, limit - 1)] + "."
