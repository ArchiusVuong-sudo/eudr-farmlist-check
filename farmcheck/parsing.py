"""Cell parsing that keeps the text as given, so precision checks can read what the supplier sent.

Numbers keep their shown decimals (a float would drop '12.683400' to 12.6834), coordinates accept
decimal commas and degrees-minutes-seconds, and a small WKT reader keeps every coordinate string.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal

CoordKind = Literal["decimal", "dms"]


class ParseError(ValueError):
    """A cell could not be read; the message quotes the raw text."""


@dataclass(frozen=True)
class ParsedNumber:
    value: float
    raw: str
    kind: CoordKind
    decimals: int  # decimals shown (for DMS: the decimal-degree precision they are worth)
    sig_decimals: int  # decimals left after dropping trailing zeros
    decimal_comma: bool = False

    @property
    def trailing_zeros(self) -> int:
        return self.decimals - self.sig_decimals


_DECIMAL_RE = re.compile(r"^[+-]?\d+(?:([.,])(\d+))?$")
_DMS_RE = re.compile(
    r"""^\s*(?P<pre>[NSEWnsew+-])?\s*
        (?P<deg>\d{1,3}(?:[.,]\d+)?)\s*[°º˚d]\s*
        (?:(?P<min>\d{1,2}(?:[.,]\d+)?)\s*['′’m]\s*)?
        (?:(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*(?:"|″|''|”|s)\s*)?
        (?P<hem>[NSEWnsew])?\s*$""",
    re.VERBOSE,
)
# 1 arc-second is about 2.78e-4 degrees, so k decimals on seconds are worth k + 3 decimal-degree
# digits (log10(3600) = 3.56); k decimals on minutes are worth k + 1 (log10(60) = 1.78).
_SEC_EXTRA = 3
_MIN_EXTRA = 1


def _decimals_of(frac: str | None) -> tuple[int, int]:
    if not frac:
        return 0, 0
    return len(frac), len(frac.rstrip("0"))


def parse_number(text: str) -> ParsedNumber:
    """Read a plain number with '.' or ',' as the decimal separator."""
    raw = text.strip().replace(" ", "").replace(" ", "")
    m = _DECIMAL_RE.match(raw)
    if not m:
        raise ParseError(f"not a number: {text!r}")
    sep, frac = m.group(1), m.group(2)
    value = float(raw.replace(",", ".")) if sep == "," else float(raw)
    shown, sig = _decimals_of(frac)
    return ParsedNumber(value, text.strip(), "decimal", shown, sig, decimal_comma=sep == ",")


def _num(part: str) -> float:
    return float(part.replace(",", "."))


def parse_dms(text: str) -> ParsedNumber:
    m = _DMS_RE.match(text)
    if not m:
        raise ParseError(f"not a coordinate: {text!r}")
    deg, minutes, sec = m.group("deg"), m.group("min"), m.group("sec")
    value = _num(deg) + (_num(minutes) / 60 if minutes else 0.0) + (_num(sec) / 3600 if sec else 0.0)
    if minutes and _num(minutes) >= 60 or sec and _num(sec) >= 60:
        raise ParseError(f"minutes or seconds over 60: {text!r}")
    hems = {h.upper() for h in (m.group("pre"), m.group("hem")) if h and h not in "+-"}
    negative = m.group("pre") == "-" or bool(hems & {"S", "W"})
    if sec:
        frac = re.split(r"[.,]", sec)[1] if re.search(r"[.,]", sec) else ""
        decimals = len(frac) + _SEC_EXTRA
    elif minutes:
        frac = re.split(r"[.,]", minutes)[1] if re.search(r"[.,]", minutes) else ""
        decimals = len(frac) + _MIN_EXTRA
    else:
        frac = re.split(r"[.,]", deg)[1] if re.search(r"[.,]", deg) else ""
        decimals = len(frac)
    return ParsedNumber(-value if negative else value, text.strip(), "dms", decimals, decimals)


def parse_coordinate(text: str) -> ParsedNumber:
    """Decimal degrees (either separator), projected metres, or degrees-minutes-seconds."""
    if not text or not text.strip():
        raise ParseError("empty")
    try:
        return parse_number(text)
    except ParseError:
        return parse_dms(text)


def parse_area(text: str) -> float:
    n = parse_number(text)
    if math.isnan(n.value) or math.isinf(n.value):
        raise ParseError(f"not a number: {text!r}")
    return n.value


# ---------------------------------------------------------------------------------------- Excel
_FMT_DECIMALS_RE = re.compile(r"[0#?]\.([0#?]+)")


def excel_display_text(value: float | int, number_format: str | None) -> str:
    """The text Excel shows for a numeric cell, which is what a person reads and copies."""
    if isinstance(value, bool):
        return str(value).upper()
    fmt = (number_format or "General").split(";")[0]
    if fmt == "General" or fmt == "@":
        if isinstance(value, int) or float(value).is_integer() and abs(value) < 1e15:
            return str(int(value))
        text = repr(float(value))
        if "e" in text or "E" in text:
            text = f"{value:.10f}".rstrip("0")
        return text
    m = _FMT_DECIMALS_RE.search(fmt)
    decimals = len(m.group(1)) if m else 0
    return f"{float(value):.{decimals}f}"


# ------------------------------------------------------------------------------------------ WKT
@dataclass
class ParsedWkt:
    kind: Literal["polygon", "multipolygon"]
    polygons: list[list[list[tuple[str, str]]]]  # polygon -> ring -> (x, y) as text
    autoclosed: bool = False
    raw: str = ""
    decimals: int = 0
    sig_decimals: int = 0
    notes: list[str] = field(default_factory=list)

    def to_wkt(self) -> str:
        def ring(r: list[tuple[str, str]]) -> str:
            return "(" + ", ".join(f"{x} {y}" for x, y in r) + ")"

        def poly(p: list[list[tuple[str, str]]]) -> str:
            return "(" + ", ".join(ring(r) for r in p) + ")"

        if self.kind == "polygon":
            return "POLYGON " + poly(self.polygons[0])
        return "MULTIPOLYGON (" + ", ".join(poly(p) for p in self.polygons) + ")"


_TOKEN_RE = re.compile(r"\s*(\(|\)|,|[A-Za-z]+|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def _tokenize(text: str) -> list[str]:
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ParseError(f"unexpected text at character {pos + 1}: {text[pos:pos + 12]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Reader:
    def __init__(self, tokens: list[str]) -> None:
        self.t, self.i = tokens, 0

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None:
            raise ParseError("WKT ends too early")
        if expected is not None and tok != expected:
            raise ParseError(f"expected {expected!r} but found {tok!r}")
        self.i += 1
        return tok


def _ring(r: _Reader) -> list[tuple[str, str]]:
    r.take("(")
    pts: list[tuple[str, str]] = []
    while True:
        x, y = r.take(), r.take()
        if not _is_number(x) or not _is_number(y):
            raise ParseError(f"bad coordinate {x} {y}")
        if _is_number(r.peek() or ""):
            r.take()  # ignore Z values
        pts.append((x, y))
        tok = r.take()
        if tok == ")":
            return pts
        if tok != ",":
            raise ParseError(f"expected ',' or ')' but found {tok!r}")


def _polygon(r: _Reader) -> list[list[tuple[str, str]]]:
    r.take("(")
    rings = [_ring(r)]
    while r.peek() == ",":
        r.take(",")
        rings.append(_ring(r))
    r.take(")")
    return rings


def _is_number(tok: str) -> bool:
    return bool(re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", tok))


def parse_wkt(text: str) -> ParsedWkt:
    """Read POLYGON or MULTIPOLYGON text; close open rings and record that it happened."""
    r = _Reader(_tokenize(text))
    head = r.take().upper()
    if r.peek() and r.peek().upper() in {"Z", "M", "ZM"}:  # type: ignore[union-attr]
        r.take()
    if head == "POLYGON":
        polys, kind = [_polygon(r)], "polygon"
    elif head == "MULTIPOLYGON":
        r.take("(")
        polys = [_polygon(r)]
        while r.peek() == ",":
            r.take(",")
            polys.append(_polygon(r))
        r.take(")")
        kind = "multipolygon"
    else:
        raise ParseError(f"only POLYGON or MULTIPOLYGON boundaries are read, got {head}")
    if r.peek() is not None:
        raise ParseError(f"unexpected text after the geometry: {r.peek()!r}")

    parsed = ParsedWkt(kind=kind, polygons=polys, raw=text.strip())  # type: ignore[arg-type]
    shown: list[int] = []
    sig: list[int] = []
    for poly in polys:
        for ring in poly:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
                parsed.autoclosed = True
            if len({p for p in ring[:-1]}) < 3:
                raise ParseError("a ring needs at least 3 distinct points")
            for x, y in ring:
                for s in (x, y):
                    fx = float(s)
                    if math.isnan(fx) or math.isinf(fx):
                        raise ParseError(f"bad coordinate {s}")
                    frac = s.split(".")[1] if "." in s else ""
                    a, b = _decimals_of(frac)
                    shown.append(a)
                    sig.append(b)
                if not (-180 <= float(x) <= 180 and -90 <= float(y) <= 90):
                    raise ParseError(f"vertex {x} {y} is not a longitude/latitude pair")
    parsed.decimals = min(shown)
    parsed.sig_decimals = min(sig)
    if parsed.autoclosed:
        parsed.notes.append("ring was not closed; the first point was repeated at the end")
    return parsed
