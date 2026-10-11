"""Value checks: code tests every proposed column against what that field's values must look like."""

from __future__ import annotations

from dataclasses import dataclass

from ..profiles import ColumnProfile


@dataclass(frozen=True)
class ValueCheck:
    ok: bool
    message: str


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def check_column(field: str, p: ColumnProfile) -> ValueCheck:
    if field in ("ignore", "notes", "row_ref"):
        return ValueCheck(True, "not used by the checks")
    if p.non_empty == 0:
        return ValueCheck(False, "column is empty")
    filled = p.non_empty / p.rows if p.rows else 0.0
    if field in ("latitude", "longitude"):
        in_range = p.lat_range_share if field == "latitude" else p.lon_range_share
        if p.number_share < 0.8:
            return ValueCheck(False, f"only {_pct(p.number_share)} of cells read as numbers or DMS")
        if in_range >= 0.8:
            extra = f"; {_pct(p.metres_share)} look like metres" if p.metres_share else ""
            return ValueCheck(True, f"{_pct(in_range)} are valid {field}s{extra}")
        if p.metres_share >= 0.8:
            return ValueCheck(True, f"{_pct(p.metres_share)} look like projected metres (UTM)")
        return ValueCheck(False, f"only {_pct(in_range)} are valid {field}s")
    if field == "area_ha":
        if p.number_share < 0.8:
            return ValueCheck(False, f"only {_pct(p.number_share)} of cells are numbers")
        if p.numeric_min is not None and p.numeric_max is not None and p.numeric_max > 10_000:
            return ValueCheck(False, "values over 10,000 do not look like hectares")
        return ValueCheck(True, f"numbers from {p.numeric_min:g} to {p.numeric_max:g}")
    if field == "boundary_wkt":
        if p.wkt_share >= 0.8:
            return ValueCheck(True, f"{_pct(p.wkt_share)} of filled cells read as WKT polygons")
        return ValueCheck(False, f"only {_pct(p.wkt_share)} read as WKT polygons")
    if field == "plot_code":
        if filled < 0.95:
            return ValueCheck(False, f"only {_pct(filled)} of rows have a value")
        if p.distinct_share < 0.9:
            return ValueCheck(False, f"only {_pct(p.distinct_share)} of values are distinct")
        return ValueCheck(True, f"filled on {_pct(filled)} of rows, {_pct(p.distinct_share)} distinct")
    # free-text fields
    if p.kind in ("text", "mixed"):
        return ValueCheck(True, f"text on {_pct(filled)} of rows")
    return ValueCheck(False, f"looks like {p.kind}, expected text")
