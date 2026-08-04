"""
Spreadsheet-aware parsing for Drive knowledge base ingestion.

The goal is to preserve logical table rows and section context instead of
flattening workbooks into plain free text. Google Sheets and XLSX ingestion
should both normalize into the same row-level model over time.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

_HEADER_KEYWORDS = {
    "issue", "issues", "task", "tasks", "job", "aid", "info", "information",
    "team", "owner", "escalation", "contact", "support", "details", "level",
    "vendor", "service", "responsible",
    # Google Form response sheets often use long question text as headers.
    "timestamp", "email", "address", "name", "brand", "role", "satisfaction",
    "feedback", "comments", "question", "response",
}


@dataclass
class SpreadsheetRowRecord:
    sheet_name: str
    sheet_order: int
    row_number: int
    row_type: str
    table_title: str = ""
    section_title: str = ""
    section_context: str = ""
    headers: list[str] = field(default_factory=list)
    values_by_column: dict[str, str] = field(default_factory=dict)
    raw_values: list[str] = field(default_factory=list)
    search_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SpreadsheetParseResult:
    source_format: str
    file_name: str
    row_records: list[SpreadsheetRowRecord] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)
    full_text: str = ""

    def chunk_texts(self) -> list[str]:
        return [row.search_text for row in self.row_records if row.search_text.strip()]


def parse_xlsx(content: bytes, file_name: str) -> SpreadsheetParseResult:
    wb = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
    result = SpreadsheetParseResult(
        source_format="xlsx",
        file_name=file_name,
        sheet_names=list(wb.sheetnames),
    )
    rendered_sections: list[str] = []
    for sheet_order, sheet_name in enumerate(wb.sheetnames):
        ws = wb[sheet_name]
        sheet_rows = _parse_sheet_rows(
            _worksheet_values(ws),
            sheet_name=sheet_name,
            sheet_order=sheet_order,
        )
        if not sheet_rows:
            continue
        result.row_records.extend(sheet_rows)
        rendered_sections.append(f"[Sheet: {sheet_name}]")
        rendered_sections.extend(row.search_text for row in sheet_rows if row.search_text.strip())
    result.full_text = "\n\n".join(rendered_sections).strip()
    return result


def parse_google_sheet_grids(
    sheets: list[tuple[str, list[list[str]]]],
    file_name: str,
) -> SpreadsheetParseResult:
    """
    Parse Google Sheets API grid data into the same canonical row model as XLSX.

    The Drive sync layer is responsible for reading formatted cell values before
    calling this function.
    """
    result = SpreadsheetParseResult(
        source_format="google_sheets",
        file_name=file_name,
        sheet_names=[sheet_name for sheet_name, _ in sheets],
    )
    rendered_sections: list[str] = []
    for sheet_order, (sheet_name, rows_values) in enumerate(sheets):
        sheet_rows = _parse_sheet_rows(
            rows_values,
            sheet_name=sheet_name,
            sheet_order=sheet_order,
        )
        if not sheet_rows:
            continue
        result.row_records.extend(sheet_rows)
        rendered_sections.append(f"[Sheet: {sheet_name}]")
        rendered_sections.extend(row.search_text for row in sheet_rows if row.search_text.strip())
    result.full_text = "\n\n".join(rendered_sections).strip()
    return result


def _worksheet_values(ws: Worksheet) -> list[list[str]]:
    merged_values = _build_merged_value_map(ws)
    return [
        _read_row_values(ws, row_number, merged_values)
        for row_number in range(1, ws.max_row + 1)
    ]


def _parse_sheet_rows(
    rows_values: list[list[str]],
    *,
    sheet_name: str,
    sheet_order: int,
) -> list[SpreadsheetRowRecord]:
    rows: list[SpreadsheetRowRecord] = []

    current_headers: dict[int, str] = {}
    active_table_title = ""
    active_section_title = ""
    active_section_context = ""

    for row_number, values in enumerate(rows_values, start=1):
        row_info = _classify_row(values, current_headers)
        kind = row_info["kind"]

        if kind == "empty":
            continue

        if kind == "table_title":
            active_table_title = row_info["title"]
            if row_info.get("context"):
                active_section_context = row_info["context"]
            current_headers = {}
            continue

        if kind == "section_header":
            active_section_title = row_info["title"]
            active_section_context = row_info.get("context", "")
            section_record = _build_section_record(
                values=values,
                row_number=row_number,
                sheet_name=sheet_name,
                sheet_order=sheet_order,
                section_title=active_section_title,
                section_context=active_section_context,
                headers=current_headers,
            )
            if section_record:
                rows.append(section_record)
            continue

        if kind == "column_header":
            current_headers = _build_header_map(values)
            if not active_table_title:
                active_table_title = row_info.get("title", "")
            continue

        if kind in {"note_row", "data_row"}:
            row_record = _build_row_record(
                values=values,
                row_number=row_number,
                sheet_name=sheet_name,
                sheet_order=sheet_order,
                table_title=active_table_title,
                section_title=active_section_title,
                section_context=active_section_context,
                headers=current_headers,
                row_type=kind,
            )
            if row_record:
                rows.append(row_record)

    return rows


def _build_merged_value_map(ws: Worksheet) -> dict[tuple[int, int], str]:
    merged_values: dict[tuple[int, int], str] = {}
    for cell_range in ws.merged_cells.ranges:
        top_left = ws.cell(cell_range.min_row, cell_range.min_col).value
        value = _clean_cell_value(top_left)
        if not value:
            continue
        for row in range(cell_range.min_row, cell_range.max_row + 1):
            for col in range(cell_range.min_col, cell_range.max_col + 1):
                merged_values[(row, col)] = value
    return merged_values


def _read_row_values(
    ws: Worksheet,
    row_number: int,
    merged_values: dict[tuple[int, int], str],
) -> list[str]:
    values: list[str] = []
    for col_number in range(1, ws.max_column + 1):
        raw = ws.cell(row=row_number, column=col_number).value
        cleaned = _clean_cell_value(raw)
        if not cleaned:
            cleaned = merged_values.get((row_number, col_number), "")
        values.append(cleaned)
    while values and not values[-1]:
        values.pop()
    return values


def _classify_row(values: list[str], current_headers: dict[int, str]) -> dict[str, str]:
    non_empty = [(idx, value) for idx, value in enumerate(values, start=1) if value]
    if not non_empty:
        return {"kind": "empty"}

    cell_texts = [value for _, value in non_empty]

    if current_headers and _looks_like_form_response_data_row(non_empty):
        return {"kind": "data_row"}

    if _looks_like_column_header(cell_texts):
        title = cell_texts[0] if len(cell_texts) == 1 else ""
        return {"kind": "column_header", "title": title}

    if _looks_like_table_title(non_empty):
        title = non_empty[0][1]
        context = " | ".join(value for _, value in non_empty[1:])
        return {"kind": "table_title", "title": title, "context": context}

    if _looks_like_section_header(non_empty, current_headers):
        title = non_empty[0][1]
        context = " | ".join(value for _, value in non_empty[1:])
        return {"kind": "section_header", "title": title, "context": context}

    if current_headers:
        return {"kind": "data_row"}

    return {"kind": "note_row"}


def _looks_like_form_response_data_row(non_empty: list[tuple[int, str]]) -> bool:
    first_values = [value for _, value in non_empty[:5]]
    if not first_values:
        return False
    first = first_values[0]
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}", first):
        return True
    if any(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value) for value in first_values):
        return True
    return False


def _looks_like_column_header(cell_texts: list[str]) -> bool:
    if len(cell_texts) < 2:
        return False
    hits = 0
    for text in cell_texts:
        normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
        words = [w for w in normalized.split() if w]
        if not words:
            continue
        if any(word in _HEADER_KEYWORDS for word in words):
            hits += 1
    if hits >= 2:
        return True

    if len(cell_texts) >= 4:
        short_cells = sum(1 for text in cell_texts if len(text) <= 40)
        alpha_cells = sum(1 for text in cell_texts if any(c.isalpha() for c in text))
        digit_cells = sum(1 for text in cell_texts if any(c.isdigit() for c in text))
        sentence_like = sum(1 for text in cell_texts if _looks_like_sentence(text))
        if (
            short_cells == len(cell_texts)
            and alpha_cells >= 4
            and digit_cells <= max(1, len(cell_texts) // 4)
            and sentence_like == 0
        ):
            return True
    return False


def _looks_like_table_title(non_empty: list[tuple[int, str]]) -> bool:
    if len(non_empty) != 1:
        return False
    text = non_empty[0][1]
    if len(text) > 80:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    uppercase_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return uppercase_ratio >= 0.65 or text.endswith(":")


def _looks_like_section_header(
    non_empty: list[tuple[int, str]],
    current_headers: dict[int, str],
) -> bool:
    if not non_empty:
        return False
    first_col, first_text = non_empty[0]
    if first_col != 1:
        return False
    if len(first_text) > 120:
        return False
    if len(non_empty) > 3:
        return False
    if _looks_like_sentence(first_text):
        return False
    if current_headers:
        trailing = [value for _, value in non_empty[1:]]
        if not trailing:
            return True
        if len(non_empty) <= 2 and all(len(value) <= 20 and not _looks_like_sentence(value) for value in trailing):
            return True
        return False
    return True


def _looks_like_sentence(text: str) -> bool:
    lowered = text.lower()
    if any(token in lowered for token in (" if ", " when ", " provide ", " check ", " click ")):
        return True
    return len(text.split()) > 12


def _build_header_map(values: list[str]) -> dict[int, str]:
    header_map: dict[int, str] = {}
    seen: dict[str, int] = {}
    for idx, value in enumerate(values, start=1):
        cleaned = _clean_header_name(value)
        if cleaned:
            seen[cleaned] = seen.get(cleaned, 0) + 1
            if seen[cleaned] > 1:
                cleaned = f"{cleaned} (Column {idx})"
            header_map[idx] = cleaned
    return header_map


def _build_row_record(
    *,
    values: list[str],
    row_number: int,
    sheet_name: str,
    sheet_order: int,
    table_title: str,
    section_title: str,
    section_context: str,
    headers: dict[int, str],
    row_type: str,
) -> Optional[SpreadsheetRowRecord]:
    non_empty = [(idx, value) for idx, value in enumerate(values, start=1) if value]
    if not non_empty:
        return None

    raw_values = [value for _, value in non_empty]
    values_by_column: dict[str, str] = {}
    for col_idx, value in non_empty:
        header = headers.get(col_idx) or f"Column {col_idx}"
        existing = values_by_column.get(header)
        if existing:
            values_by_column[header] = f"{existing} | {value}"
        else:
            values_by_column[header] = value

    search_text = _render_search_text(
        sheet_name=sheet_name,
        row_number=row_number,
        table_title=table_title,
        section_title=section_title,
        section_context=section_context,
        values_by_column=values_by_column,
        raw_values=raw_values,
    )
    return SpreadsheetRowRecord(
        sheet_name=sheet_name,
        sheet_order=sheet_order,
        row_number=row_number,
        row_type=row_type,
        table_title=table_title,
        section_title=section_title,
        section_context=section_context,
        headers=list(headers.values()),
        values_by_column=values_by_column,
        raw_values=raw_values,
        search_text=search_text,
    )


def _build_section_record(
    *,
    values: list[str],
    row_number: int,
    sheet_name: str,
    sheet_order: int,
    section_title: str,
    section_context: str,
    headers: dict[int, str],
) -> Optional[SpreadsheetRowRecord]:
    non_empty = [(idx, value) for idx, value in enumerate(values, start=1) if value]
    if not non_empty:
        return None

    raw_values = [value for _, value in non_empty]
    values_by_column: dict[str, str] = {}
    trailing = non_empty[1:]
    if len(trailing) == 1:
        col_idx, value = trailing[0]
        if col_idx >= 3:
            values_by_column["Section Owner / Team"] = value
        else:
            values_by_column[f"Section Context Col {col_idx}"] = value
    else:
        for col_idx, value in trailing:
            values_by_column[f"Section Context Col {col_idx}"] = value

    search_text = _render_search_text(
        row_type="section_header",
        sheet_name=sheet_name,
        row_number=row_number,
        table_title="",
        section_title=section_title,
        section_context=section_context,
        values_by_column=values_by_column,
        raw_values=raw_values,
    )
    return SpreadsheetRowRecord(
        sheet_name=sheet_name,
        sheet_order=sheet_order,
        row_number=row_number,
        row_type="section_header",
        table_title="",
        section_title=section_title,
        section_context=section_context,
        headers=list(headers.values()),
        values_by_column=values_by_column,
        raw_values=raw_values,
        search_text=search_text,
    )


def _render_search_text(
    *,
    row_type: str = "data_row",
    sheet_name: str,
    row_number: int,
    table_title: str,
    section_title: str,
    section_context: str,
    values_by_column: dict[str, str],
    raw_values: list[str],
) -> str:
    parts = [f"Sheet: {sheet_name}", f"Row: {row_number}"]
    if row_type == "section_header":
        parts.append("Record Type: Section Header")
    if table_title:
        parts.append(f"Table: {table_title}")
    if section_title:
        parts.append(f"Section: {section_title}")
    if section_context:
        parts.append(f"Section Context: {section_context}")

    if values_by_column:
        for key, value in values_by_column.items():
            parts.append(f"{key}: {value}")
    elif raw_values:
        parts.append("Values: " + " | ".join(raw_values))
    return ". ".join(part for part in parts if part).strip()


def serialize_headers(headers: list[str]) -> str:
    return json.dumps(headers, ensure_ascii=False)


def serialize_values(values_by_column: dict[str, str]) -> str:
    return json.dumps(values_by_column, ensure_ascii=False, sort_keys=True)


def _clean_header_name(value: str) -> str:
    value = _clean_cell_value(value)
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip(" :-")
    return value[:120]


def _clean_cell_value(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
