"""Turn one raw row plus a confirmed mapping into typed plot input, with notes for the trace.

Python reads the cells (decimal commas, DMS, metres, WKT text, precision as shown); PostGIS does all
geometry math afterwards. A cell that cannot be read becomes a row-level issue quoting it, and the
batch carries on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .parsing import ParsedNumber, ParseError, parse_area, parse_coordinate, parse_wkt


@dataclass
class RowIssue:
    rule_key: str  # missing_geolocation | unparseable_value
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlotInput:
    row_number: int
    plot_code: str
    producer: str | None = None
    admin_area: str | None = None
    declared_area_ha: float | None = None
    lat_in: float | None = None
    lon_in: float | None = None
    coord_format: str | None = None
    coord_decimals: int | None = None
    coord_sig_decimals: int | None = None
    wkt_in: str | None = None
    ring_autoclosed: bool = False
    issues: list[RowIssue] = field(default_factory=list)
    parse: dict[str, Any] = field(default_factory=dict)


def _cell(cells: list[dict[str, Any]], headers: list[str], mapping: dict[str, str], fld: str) -> tuple[str | None, dict[str, Any] | None]:
    for i, h in enumerate(headers):
        if mapping.get(h) == fld and i < len(cells):
            c = cells[i]
            return h, c
    return None, None


def _coord_note(header: str, cell: dict[str, Any], p: ParsedNumber) -> dict[str, Any]:
    fmt = "degrees-minutes-seconds" if p.kind == "dms" else ("decimal comma" if p.decimal_comma else "decimal point")
    return {
        "column": header,
        "raw": cell["raw"],
        "excel_format": cell.get("fmt"),
        "value": p.value,
        "format": fmt,
        "decimals_shown": p.decimals,
        "decimals_significant": p.sig_decimals,
    }


def normalize_row(row_number: int, cells: list[dict[str, Any]], headers: list[str], mapping: dict[str, str]) -> PlotInput:
    def text(fld: str) -> str | None:
        _h, c = _cell(cells, headers, mapping, fld)
        return c["raw"] if c and c.get("raw") else None

    code = text("plot_code")
    out = PlotInput(row_number=row_number, plot_code=code or f"(row {row_number})")
    out.producer = text("producer")
    out.admin_area = text("admin_area")
    if not code:
        out.issues.append(RowIssue("unparseable_value", "Plot code is empty", {"column": "plot_code", "raw": ""}))

    # area
    h_area, c_area = _cell(cells, headers, mapping, "area_ha")
    if c_area and c_area.get("raw"):
        try:
            out.declared_area_ha = parse_area(c_area["raw"])
            out.parse["area"] = {"column": h_area, "raw": c_area["raw"], "value": out.declared_area_ha}
        except ParseError:
            out.issues.append(RowIssue("unparseable_value", f"Area cell {c_area['raw']!r} in '{h_area}' is not a number",
                                       {"column": h_area, "raw": c_area["raw"]}))

    # boundary first: when a polygon is given it is the plot's geometry
    h_wkt, c_wkt = _cell(cells, headers, mapping, "boundary_wkt")
    if c_wkt and c_wkt.get("raw"):
        try:
            w = parse_wkt(c_wkt["raw"])
            out.wkt_in = w.to_wkt()
            out.ring_autoclosed = w.autoclosed
            out.coord_format = "wkt"
            out.coord_decimals = w.decimals
            out.coord_sig_decimals = w.sig_decimals
            raw = c_wkt["raw"]
            out.parse["boundary"] = {
                "column": h_wkt,
                "raw": raw if len(raw) <= 160 else raw[:157] + "...",
                "kind": w.kind,
                "vertices": sum(len(r) for p in w.polygons for r in p),
                "decimals_shown": w.decimals,
                "decimals_significant": w.sig_decimals,
                "notes": w.notes,
            }
        except ParseError as exc:
            out.issues.append(RowIssue("unparseable_value", f"Boundary in '{h_wkt}' cannot be read: {exc}",
                                       {"column": h_wkt, "raw": c_wkt["raw"][:120]}))
            out.parse["boundary"] = {"column": h_wkt, "raw": c_wkt["raw"][:120], "error": str(exc)}

    h_lat, c_lat = _cell(cells, headers, mapping, "latitude")
    h_lon, c_lon = _cell(cells, headers, mapping, "longitude")
    lat_raw = c_lat.get("raw") if c_lat else None
    lon_raw = c_lon.get("raw") if c_lon else None
    parsed: dict[str, ParsedNumber] = {}
    bad = False
    for name, header, cell, raw in (("lat", h_lat, c_lat, lat_raw), ("lon", h_lon, c_lon, lon_raw)):
        if not raw:
            continue
        try:
            p = parse_coordinate(raw)
            parsed[name] = p
            out.parse[name] = _coord_note(header or name, cell or {}, p)
        except ParseError:
            bad = True
            out.parse[name] = {"column": header, "raw": raw, "error": "cannot be read as a coordinate"}
            if out.wkt_in is None:
                out.issues.append(RowIssue("unparseable_value", f"Cell {raw!r} in '{header}' is not a coordinate",
                                           {"column": header, "raw": raw}))

    if out.wkt_in is not None:
        if parsed:
            out.parse["point_note"] = "a boundary is given, so the point columns are kept for reference only"
        return out

    if "lat" in parsed and "lon" in parsed:
        la, lo = parsed["lat"], parsed["lon"]
        out.lat_in, out.lon_in = la.value, lo.value
        if abs(la.value) > 180 or abs(lo.value) > 180:
            out.coord_format = "metres"
            out.parse["metres_note"] = "values too large for degrees, read as projected metres"
        else:
            out.coord_format = "dms" if "dms" in (la.kind, lo.kind) else "decimal"
            out.coord_decimals = min(la.decimals, lo.decimals)
            out.coord_sig_decimals = min(la.sig_decimals, lo.sig_decimals)
    elif not bad:
        if lat_raw or lon_raw:
            missing = "longitude" if lat_raw else "latitude"
            out.issues.append(RowIssue("missing_geolocation", f"The {missing} is empty and no boundary is given", {}))
        elif not any(i.rule_key == "unparseable_value" and i.detail.get("column") == h_wkt for i in out.issues):
            out.issues.append(RowIssue("missing_geolocation", "No coordinates and no boundary are given", {}))
    return out
