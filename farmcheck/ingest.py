"""Read a supplier's CSV or XLSX into header + rows of cells, keeping each cell's text as shown."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

from openpyxl import load_workbook

from .parsing import excel_display_text

MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024


class IngestError(ValueError):
    """The file cannot be read as a farm list."""


@dataclass(frozen=True)
class Cell:
    raw: str
    kind: Literal["text", "number", "empty"]
    fmt: str | None = None  # Excel number format for numeric cells

    def as_json(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass
class Sheet:
    file_name: str
    file_kind: Literal["csv", "xlsx"]
    headers: list[str]
    rows: list[tuple[int, list[Cell]]]  # (spreadsheet row number, cells)
    delimiter: str | None = None


def header_signature(headers: list[str]) -> str:
    """Stable id for a column layout: same headers in the same order give the same signature."""
    norm = [unicodedata.normalize("NFC", h).strip().casefold() for h in headers]
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode()).hexdigest()[:16]


def _clean_header(h: object, i: int) -> str:
    text = str(h).strip() if h is not None else ""
    return re.sub(r"\s+", " ", text) or f"Column {i + 1}"


def _read_csv(name: str, data: bytes) -> Sheet:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    sample = text[:4096]
    first = sample.splitlines()[0] if sample else ""
    delimiter = max([";", ",", "\t", "|"], key=first.count)
    if first.count(delimiter) == 0:
        raise IngestError("could not find a column delimiter in the header row")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise IngestError("the file is empty")
    headers = [_clean_header(h, i) for i, h in enumerate(rows[0])]
    out: list[tuple[int, list[Cell]]] = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        cells = [
            Cell(raw=(row[i].strip() if i < len(row) else ""), kind="text" if i < len(row) and row[i].strip() else "empty")
            for i in range(len(headers))
        ]
        out.append((n, cells))
    return Sheet(name, "csv", headers, out, delimiter)


def _xlsx_cell(value: object, fmt: str | None) -> Cell:
    if value is None or (isinstance(value, str) and not value.strip()):
        return Cell("", "empty")
    if isinstance(value, str):
        return Cell(value.strip(), "text")
    if isinstance(value, (dt.datetime, dt.date)):
        return Cell(value.isoformat(), "text")
    if isinstance(value, (int, float)):
        return Cell(excel_display_text(value, fmt), "number", fmt or "General")
    return Cell(str(value), "text")


def _read_xlsx(name: str, data: bytes) -> Sheet:
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for bad files
        raise IngestError(f"not a readable XLSX file ({type(exc).__name__})") from exc
    ws = wb.worksheets[0]
    header_row: list[str] | None = None
    out: list[tuple[int, list[Cell]]] = []
    for row in ws.iter_rows():
        values = [c.value for c in row]
        if header_row is None:
            if any(v is not None and str(v).strip() for v in values):
                header_row = [_clean_header(v, i) for i, v in enumerate(values)]
                while header_row and header_row[-1].startswith("Column "):
                    header_row.pop()
            continue
        cells = [_xlsx_cell(c.value, c.number_format) for c in row[: len(header_row)]]
        cells += [Cell("", "empty")] * (len(header_row) - len(cells))
        if all(c.kind == "empty" for c in cells):
            continue
        out.append((row[0].row, cells))
    if header_row is None:
        raise IngestError("the first sheet is empty")
    return Sheet(name, "xlsx", header_row, out)


def read_farm_list(file_name: str, data: bytes) -> Sheet:
    if len(data) > MAX_BYTES:
        raise IngestError("the file is over 10 MB")
    lower = file_name.lower()
    if lower.endswith(".xlsx"):
        sheet = _read_xlsx(file_name, data)
    elif lower.endswith((".csv", ".txt")):
        sheet = _read_csv(file_name, data)
    else:
        raise IngestError("upload a .csv or .xlsx file")
    if not sheet.rows:
        raise IngestError("the file has a header row but no data rows")
    if len(sheet.rows) > MAX_ROWS:
        raise IngestError(f"the demo reads up to {MAX_ROWS} rows")
    if len(set(h.casefold() for h in sheet.headers)) != len(sheet.headers):
        raise IngestError("two columns have the same header")
    return sheet
