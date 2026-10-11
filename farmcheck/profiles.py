"""Column profiles: what a column looks like without showing any of its cell values.

These are the only thing the mapping model sees besides the headers. Text becomes a shape mask
('Hộ SYN-001' -> 'Aa AAA-999'), so names and codes never leave the app; numeric columns add their
range and decimals, which a person needs to tell latitude from area or metres.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from .ingest import Cell
from .parsing import ParseError, parse_coordinate, parse_wkt


class ColumnProfile(BaseModel):
    index: int
    header: str
    rows: int
    non_empty: int
    distinct_share: float
    kind: str  # number | text | wkt | mixed | empty
    number_share: float
    decimal_comma_share: float
    dms_share: float
    wkt_share: float
    numeric_min: float | None = None
    numeric_max: float | None = None
    decimals_min: int | None = None
    decimals_max: int | None = None
    lat_range_share: float = 0.0  # numbers within -90..90
    lon_range_share: float = 0.0  # numbers within -180..180
    metres_share: float = 0.0  # numbers too large for degrees (projected metres)
    patterns: list[tuple[str, float]] = []


def shape_mask(text: str, limit: int = 18) -> str:
    out = []
    for ch in text[:limit]:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out) + ("…" if len(text) > limit else "")


def profile_column(index: int, header: str, cells: list[Cell]) -> ColumnProfile:
    values = [c.raw for c in cells if c.kind != "empty" and c.raw]
    n = len(values)
    nums: list[float] = []
    decimals: list[int] = []
    commas = dms = wkt = 0
    masks: Counter[str] = Counter()
    for v in values:
        try:
            p = parse_coordinate(v)
            nums.append(p.value)
            decimals.append(p.decimals)
            commas += p.decimal_comma
            dms += p.kind == "dms"
            masks[shape_mask(v)] += 1
            continue
        except ParseError:
            pass
        if v.lstrip().upper().startswith(("POLYGON", "MULTIPOLYGON")):
            try:
                parse_wkt(v)
                wkt += 1
                masks[v.split("(")[0].strip().upper() + " ((…))"] += 1
                continue
            except ParseError:
                masks["WKT (unreadable)"] += 1
                continue
        masks[shape_mask(v)] += 1

    def share(k: int) -> float:
        return round(k / n, 3) if n else 0.0

    if n == 0:
        kind = "empty"
    elif wkt / n >= 0.8:
        kind = "wkt"
    elif len(nums) / n >= 0.8:
        kind = "number"
    elif len(nums) / n <= 0.2 and wkt == 0:
        kind = "text"
    else:
        kind = "mixed"
    top = [(m, share(k)) for m, k in masks.most_common(3)]
    return ColumnProfile(
        index=index,
        header=header,
        rows=len(cells),
        non_empty=n,
        distinct_share=share(len(set(values))),
        kind=kind,
        number_share=share(len(nums)),
        decimal_comma_share=share(commas),
        dms_share=share(dms),
        wkt_share=share(wkt),
        numeric_min=round(min(nums), 6) if nums else None,
        numeric_max=round(max(nums), 6) if nums else None,
        decimals_min=min(decimals) if decimals else None,
        decimals_max=max(decimals) if decimals else None,
        lat_range_share=share(sum(1 for x in nums if -90 <= x <= 90)),
        lon_range_share=share(sum(1 for x in nums if -180 <= x <= 180)),
        metres_share=share(sum(1 for x in nums if 180 < abs(x) < 10_000_000)),
        patterns=top,
    )


def profile_sheet(headers: list[str], rows: list[list[Cell]]) -> list[ColumnProfile]:
    return [profile_column(i, h, [r[i] for r in rows]) for i, h in enumerate(headers)]
