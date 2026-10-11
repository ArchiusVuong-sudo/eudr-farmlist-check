"""Make the synthetic reference layers, the two sample farm lists and their answer keys.

Everything here is invented: producers, communes, veredas, plots, forest cover, forest loss and
protected areas. Real names are used only for the provinces/departments and country outlines.

Usage: uv run python scripts/make_synthetic.py --seed 7

Guarantees the answer key depends on (asserted below):
- clean plots are under the implausible-area threshold, their declared area is within 5% of the
  boundary's own area, coordinates have 6 real decimals (last digit never 0);
- every plot is at least 5 km from the declared country's border and at least 300 m from any
  forest-loss patch, unless a problem is injected on purpose;
- the Vietnam box lies east of 108 E, so UTM zone 49 is the only zone that fits;
- swapped and sign-dropped rows have exactly one candidate reading inside the declared country,
  wrong-country rows have none and lie at least 10 km inside the neighbour.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "data" / "reference"
SAMPLES = ROOT / "data" / "samples"

# ------------------------------------------------------------------------------------- geometry
KY = 110_574.0


@dataclass
class Region:
    key: str
    iso2: str
    lon_min: float
    lon_max: float
    lat_min: float
    lat_max: float

    @property
    def lat0(self) -> float:
        return (self.lat_min + self.lat_max) / 2

    @property
    def lon0(self) -> float:
        return (self.lon_min + self.lon_max) / 2

    @property
    def kx(self) -> float:
        return 111_320.0 * math.cos(math.radians(self.lat0))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * KY

    def to_ll(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / KY, self.lon0 + x / self.kx

    def half_w(self) -> float:
        return (self.lon_max - self.lon_min) / 2 * self.kx

    def half_h(self) -> float:
        return (self.lat_max - self.lat_min) / 2 * KY


VN = Region("VN", "VN", 108.22, 108.62, 12.80, 13.10)
CO = Region("CO", "CO", -76.02, -75.58, 1.98, 2.42)


def blob(rng: random.Random, cx: float, cy: float, r: float, n: int = 20, jitter: float = 0.25) -> Polygon:
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        rr = r * (1 + rng.uniform(-jitter, jitter))
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return Polygon(pts)


def polygon_of_area(rng: random.Random, cx: float, cy: float, area_m2: float, n: int | None = None) -> Polygon:
    n = n or rng.randint(5, 8)
    # keep the vertices spread out so the polygon stays simple and plausible
    angles = [2 * math.pi * i / n + rng.uniform(-0.25, 0.25) * (2 * math.pi / n) for i in range(n)]
    radii = [rng.uniform(0.8, 1.2) for _ in range(n)]
    poly = Polygon([(cx + r * math.cos(a), cy + r * math.sin(a)) for a, r in zip(angles, radii, strict=True)])
    scale = math.sqrt(area_m2 / poly.area)
    return Polygon([(cx + (x - cx) * scale, cy + (y - cy) * scale) for x, y in poly.exterior.coords[:-1]])


def circle(cx: float, cy: float, area_m2: float) -> Polygon:
    return Point(cx, cy).buffer(math.sqrt(area_m2 / math.pi), 32)


def fmt6(v: float) -> str:
    """Six decimals whose last digit is never 0, so no value looks zero-padded."""
    s = f"{v:.6f}"
    if s.endswith("0"):
        s = f"{v + (1e-6 if v >= 0 else -1e-6):.6f}"
    return s


def utm_forward(lat: float, lon: float, zone: int) -> tuple[float, float]:
    """WGS 84 to UTM north (Snyder's series), easting and northing in metres."""
    a, f, k0 = 6378137.0, 1 / 298.257223563, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = ep2 * math.cos(phi) ** 2
    aa = math.cos(phi) * (lam - lon0)
    m = a * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * phi
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * phi)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * phi)
        - (35 * e2**3 / 3072) * math.sin(6 * phi)
    )
    x = k0 * n * (aa + (1 - t + c) * aa**3 / 6 + (5 - 18 * t + t**2 + 72 * c - 58 * ep2) * aa**5 / 120) + 500_000
    y = k0 * (m + n * math.tan(phi) * (aa**2 / 2 + (5 - t + 9 * c + 4 * c**2) * aa**4 / 24
                                      + (61 - 58 * t + t**2 + 600 * c - 330 * ep2) * aa**6 / 720))
    return x, y


def to_dms(v: float, is_lat: bool, sec_decimals: int = 3) -> str:
    hem = ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
    v = abs(v)
    d = int(v)
    m_full = (v - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60, sec_decimals)
    if s >= 60:
        s, m = 0.0, m + 1
    if m >= 60:
        m, d = 0, d + 1
    sec = f"{s:0{3 + sec_decimals if sec_decimals else 2}.{sec_decimals}f}" if sec_decimals else f"{int(s):02d}"
    return f"{d}°{m:02d}'{sec}\"{hem}"


# ------------------------------------------------------------------------------------ countries
def load_countries() -> dict[str, Any]:
    data = json.loads((REF / "countries.geojson").read_text(encoding="utf-8"))
    return {f["properties"]["iso2"]: shape(f["geometry"]) for f in data["features"]}


def candidates(lat: float, lon: float) -> list[tuple[str, float, float]]:
    raw = [
        ("as_given", lat, lon), ("swapped", lon, lat), ("lat_sign", -lat, lon), ("lon_sign", lat, -lon),
        ("both_signs", -lat, -lon), ("swapped_lat_sign", -lon, lat), ("swapped_lon_sign", lon, -lat),
        ("swapped_both_signs", -lon, -lat),
    ]
    return [(k, la, lo) for k, la, lo in raw if abs(la) <= 90 and abs(lo) <= 180]


def fits(countries: dict[str, Any], iso2: str, lat: float, lon: float) -> list[str]:
    c = countries[iso2].buffer(0.0015)
    return [k for k, la, lo in candidates(lat, lon) if c.covers(Point(lo, la))]


# ------------------------------------------------------------------------------------ plot specs
CODES = {
    "clean": ([], "ready", [], "ready"),
    "dms": ([], "ready", [], "ready"),
    "utm": (["COORD_REPAIRED"], "repaired", ["COORD_REPAIRED"], "repaired"),
    "utm_loss": (["COORD_REPAIRED", "LOSS_AFTER_CUTOFF"], "review", ["COORD_REPAIRED", "LOSS_AFTER_CUTOFF"], "review"),
    "swapped": (["COORD_REPAIRED"], "repaired", ["COORD_REPAIRED"], "repaired"),
    "sign": (["COORD_REPAIRED"], "repaired", ["COORD_REPAIRED"], "repaired"),
    "four_dec": (["COORD_PRECISION"], "needs_fix", ["COORD_PRECISION"], "needs_fix"),
    "four_dec_general": (["COORD_PRECISION"], "needs_fix", ["COORD_PRECISION"], "needs_fix"),
    "dms_low": (["COORD_PRECISION"], "needs_fix", ["COORD_PRECISION"], "needs_fix"),
    "zero_pad_fmt": (["COORD_ZERO_PADDED"], "ready", ["COORD_ZERO_PADDED"], "needs_fix"),
    "zero_pad_text": (["COORD_ZERO_PADDED"], "ready", ["COORD_ZERO_PADDED"], "needs_fix"),
    "point_big": (["POINT_OVER_4HA"], "needs_fix", ["POINT_OVER_4HA"], "needs_fix"),
    "bowtie": (["SELF_INTERSECTION"], "repaired", ["SELF_INTERSECTION"], "repaired"),
    "unclosed": (["RING_AUTOCLOSED"], "repaired", ["RING_AUTOCLOSED"], "repaired"),
    "duplicate": (["DUPLICATE_PLOT"], "needs_fix", ["DUPLICATE_PLOT"], "needs_fix"),
    "overlap": (["PLOT_OVERLAP"], "needs_fix", ["PLOT_OVERLAP"], "needs_fix"),
    "wrong_country": (["OUTSIDE_COUNTRY"], "needs_fix", ["OUTSIDE_COUNTRY"], "needs_fix"),
    "missing": (["MISSING_GEOLOCATION"], "needs_fix", ["MISSING_GEOLOCATION"], "needs_fix"),
    "unparseable": (["UNPARSEABLE_VALUE"], "needs_fix", ["UNPARSEABLE_VALUE"], "needs_fix"),
    "area_mismatch": (["AREA_MISMATCH"], "ready", ["AREA_MISMATCH"], "ready"),
    "implausible": (["IMPLAUSIBLE_AREA"], "needs_fix", ["IMPLAUSIBLE_AREA"], "needs_fix"),
    "protected": (["PROTECTED_AREA_OVERLAP"], "review", ["PROTECTED_AREA_OVERLAP"], "review"),
    "no_area": (["POINT_AREA_ASSUMED"], "ready", ["POINT_AREA_NEEDED"], "needs_fix"),
    "loss": (["LOSS_AFTER_CUTOFF"], "review", ["LOSS_AFTER_CUTOFF"], "review"),
    "loss_small": ([], "ready", ["LOSS_AFTER_CUTOFF"], "review"),
}
NOTES = {
    "clean": "clean", "dms": "clean, coordinates in degrees-minutes-seconds",
    "utm": "point given in UTM metres", "utm_loss": "support case: 3.5 ha point given in UTM zone 49N metres; its circle overlaps a loss patch edge",
    "swapped": "latitude and longitude swapped", "sign": "minus sign dropped from the longitude",
    "four_dec": "coordinates with 4 decimals", "four_dec_general": "coordinates with 4 decimals (General cell format)",
    "dms_low": "DMS with whole seconds (under 6-decimal precision)",
    "zero_pad_fmt": "4 real decimals shown as 6 by the cell number format", "zero_pad_text": "4 real decimals padded with zeros in text",
    "point_big": "point given for a plot over 4 ha", "bowtie": "self-intersecting (bowtie) boundary",
    "unclosed": "boundary ring not closed", "duplicate": "repeat of an earlier row", "overlap": "boundary overlaps another plot",
    "wrong_country": "point inside a neighbouring country", "missing": "no coordinates and no boundary",
    "unparseable": "coordinate cell with a typo", "area_mismatch": "declared area far from the boundary's area",
    "implausible": "declared area of 0 ha", "protected": "inside a protected area", "no_area": "point with no area",
    "loss": "overlaps forest loss after the cut-off", "loss_small": "small loss overlap: below the v1 minimum, above the v2 minimum",
}


@dataclass
class Spec:
    kind: str
    geom: str  # point | polygon | none
    x: float = 0.0
    y: float = 0.0
    area_ha: float | None = None
    poly: Polygon | None = None
    assess: Polygon | None = None
    loss_target_ha: float | None = None
    lat: float = 0.0
    lon: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def place_specs(rng: random.Random, region: Region, kinds: list[str], pa: Polygon, forests: list[Polygon]) -> list[Spec]:
    specs: list[Spec] = []
    centers: list[tuple[float, float]] = []
    margin = 2500.0
    pa_far = pa.buffer(600)
    forest_union = prep(unary_union(forests))

    def free_point(inside_pa: bool = False, near_forest: bool = False) -> tuple[float, float]:
        for _ in range(20000):
            if inside_pa:
                minx, miny, maxx, maxy = pa.buffer(-350).bounds
                x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
                if not pa.buffer(-350).contains(Point(x, y)):
                    continue
            else:
                x = rng.uniform(-region.half_w() + margin, region.half_w() - margin)
                y = rng.uniform(-region.half_h() + margin, region.half_h() - margin)
                if pa_far.contains(Point(x, y)):
                    continue
            if near_forest and not forest_union.contains(Point(x, y)):
                continue
            if all(math.hypot(x - cx, y - cy) >= 1300 for cx, cy in centers):
                centers.append((x, y))
                return x, y
        raise RuntimeError("could not place a plot")

    for kind in kinds:
        if kind in ("missing", "duplicate", "wrong_country"):
            specs.append(Spec(kind, "point" if kind != "missing" else "none"))
            continue
        polygon_kinds = {"bowtie", "unclosed", "overlap", "area_mismatch", "implausible"}
        is_poly = kind in polygon_kinds or (kind in ("clean", "loss") and rng.random() < 0.4)
        x, y = free_point(inside_pa=kind == "protected", near_forest=kind.startswith("loss") or kind == "utm_loss")
        s = Spec(kind, "polygon" if is_poly else "point", x, y)
        if is_poly:
            area = rng.uniform(4.5, 11.0) if kind == "clean" and rng.random() < 0.5 else rng.uniform(0.8, 3.8)
            if kind == "bowtie":
                w, h = math.sqrt(area * 10000 * 1.4), math.sqrt(area * 10000 / 1.4)
                corners = [(x - w / 2, y - h / 2), (x + w / 2, y - h / 2), (x - w / 2, y + h / 2), (x + w / 2, y + h / 2)]
                s.poly = Polygon(corners)  # invalid on purpose: edges cross in the middle
                s.extra["valid_area_ha"] = (w * h / 2) / 10000
                s.area_ha = round(s.extra["valid_area_ha"] * rng.uniform(0.97, 1.03), 2)
                s.assess = Polygon([corners[0], corners[1], (x, y)]).union(Polygon([corners[2], corners[3], (x, y)]))
            else:
                s.poly = polygon_of_area(rng, x, y, area * 10000)
                true_ha = s.poly.area / 10000
                s.area_ha = round(true_ha * rng.uniform(0.97, 1.03), 2)
                if kind == "area_mismatch":
                    s.area_ha = round(true_ha * 1.6, 2)
                if kind == "implausible":
                    s.area_ha = 0.0
                s.assess = s.poly
        else:
            if kind == "point_big":
                s.area_ha = round(rng.uniform(5.5, 9.5), 2)
            elif kind == "utm_loss":
                s.area_ha = 3.5
            elif kind == "no_area":
                s.area_ha = None
            else:
                s.area_ha = round(rng.uniform(0.6, 3.6), 2)
            s.assess = circle(x, y, (s.area_ha or 1.0) * 10000)
        if kind in ("loss", "utm_loss"):
            s.loss_target_ha = 0.3 if kind == "utm_loss" else round(rng.uniform(0.45, 0.8), 2)
        if kind == "loss_small":
            s.loss_target_ha = 0.05
        specs.append(s)
    # the second plot of an overlapping pair sits on the first one's edge
    pair = [s for s in specs if s.kind == "overlap"]
    if len(pair) == 2:
        a, b = pair
        ra = math.sqrt(a.poly.area / math.pi)
        b.poly = polygon_of_area(rng, a.x + ra * 1.35, a.y + ra * 0.2, a.poly.area * 0.8)
        b.x, b.y = b.poly.centroid.x, b.poly.centroid.y
        b.area_ha = round(b.poly.area / 10000 * rng.uniform(0.98, 1.02), 2)
        b.assess = b.poly
        ov = a.poly.intersection(b.poly).area / 10000
        assert 0.15 < ov < a.poly.area / 10000 * 0.6, f"overlap pair overlaps {ov:.2f} ha"
        b.extra["overlap_ha"] = ov
    return specs


def dedicated_patch(rng: random.Random, s: Spec) -> Polygon:
    """A loss patch on the plot's edge whose overlap with the assessed area is the target."""
    target = s.loss_target_ha * 10000  # type: ignore[operator]
    patch_area = rng.uniform(1.2, 2.0) * 10000
    theta = rng.uniform(0, 2 * math.pi)
    shape0 = polygon_of_area(rng, 0, 0, patch_area, n=7)
    lo, hi = 0.0, 1500.0
    for _ in range(60):
        mid = (lo + hi) / 2
        cand = Polygon([(x + s.x + mid * math.cos(theta), y + s.y + mid * math.sin(theta)) for x, y in shape0.exterior.coords])
        if cand.intersection(s.assess).area > target:
            lo = mid
        else:
            hi = mid
    d = (lo + hi) / 2
    return Polygon([(x + s.x + d * math.cos(theta), y + s.y + d * math.sin(theta)) for x, y in shape0.exterior.coords])


# ------------------------------------------------------------------------------------- layers
def make_layers(rng: random.Random, region: Region, n_forest: int, n_loss: int, pa_offset: tuple[float, float]):
    forests = []
    for _ in range(n_forest):
        cx = rng.uniform(-region.half_w() * 0.85, region.half_w() * 0.85)
        cy = rng.uniform(-region.half_h() * 0.85, region.half_h() * 0.85)
        forests.append(blob(rng, cx, cy, rng.uniform(2200, 4200), n=24))
    pa = blob(rng, pa_offset[0], pa_offset[1], 1700, n=18, jitter=0.15)
    return forests, pa, n_loss


def background_patches(rng: random.Random, region: Region, forests: list[Polygon], n: int, keep_out: Any) -> list[Polygon]:
    keep_out_p = prep(keep_out)
    out: list[Polygon] = []
    tries = 0
    while len(out) < n:
        tries += 1
        if tries > n * 200:
            raise RuntimeError("could not place loss patches")
        f = rng.choice(forests)
        minx, miny, maxx, maxy = f.bounds
        x, y = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
        if not f.contains(Point(x, y)):
            continue
        p = polygon_of_area(rng, x, y, rng.uniform(0.3, 3.0) * 10000, n=rng.randint(5, 7))
        if keep_out_p.intersects(p):
            continue
        out.append(p)
    return out


def ll_ring(region: Region, poly: Polygon, decimals: int = 6) -> list[list[float]]:
    return [[round(region.to_ll(x, y)[1], decimals), round(region.to_ll(x, y)[0], decimals)] for x, y in poly.exterior.coords]


def random_date(rng: random.Random) -> str:
    start = dt.date(2021, 1, 1)
    return (start + dt.timedelta(days=rng.randint(0, (dt.date(2025, 12, 31) - start).days))).isoformat()


# --------------------------------------------------------------------------------- sample rows
def wkt_text(region: Region, poly: Polygon, close: bool = True) -> str:
    pts = [region.to_ll(x, y) for x, y in poly.exterior.coords[:-1]]
    coords = [f"{fmt6(lo)} {fmt6(la)}" for la, lo in pts]
    if close:
        coords.append(coords[0])
    return "POLYGON ((" + ", ".join(coords) + "))"


def bowtie_wkt(region: Region, poly: Polygon) -> str:
    pts = [region.to_ll(x, y) for x, y in poly.exterior.coords[:-1]]
    coords = [f"{fmt6(lo)} {fmt6(la)}" for la, lo in pts]
    return "POLYGON ((" + ", ".join(coords + [coords[0]]) + "))"


def build(seed: int) -> None:
    rng = random.Random(seed)
    countries = load_countries()

    # region guarantees
    for reg in (VN, CO):
        border = countries[reg.iso2].boundary
        for lat in (reg.lat_min, reg.lat_max):
            for lon in (reg.lon_min, reg.lon_max):
                assert countries[reg.iso2].contains(Point(lon, lat))
                assert border.distance(Point(lon, lat)) * 108_000 > 5000, f"{reg.key} box too close to a border"
    assert VN.lon_min > 108.0

    vn_kinds = (
        ["utm", "utm"] + ["swapped"] * 3 + ["four_dec", "four_dec", "four_dec_general"] + ["zero_pad_fmt"] * 2
        + ["zero_pad_text"] + ["point_big"] * 2 + ["bowtie"] * 2 + ["unclosed", "duplicate", "overlap", "overlap",
        "wrong_country", "missing", "unparseable", "area_mismatch", "protected", "no_area", "no_area"]
        + ["loss"] * 3 + ["loss_small"]
    )
    co_kinds = (
        ["sign"] * 3 + ["swapped"] * 2 + ["wrong_country", "dms_low", "four_dec", "four_dec"] + ["zero_pad_text"] * 2
        + ["point_big"] * 2 + ["bowtie", "unclosed", "duplicate", "overlap", "overlap", "missing", "unparseable",
        "area_mismatch", "implausible", "protected", "no_area", "no_area"] + ["loss"] * 3 + ["loss_small"] + ["dms"] * 6
    )
    vn_kinds += ["clean"] * (60 - 1 - len(vn_kinds))  # the support case is added below
    co_kinds += ["clean"] * (80 - len(co_kinds))

    layers: dict[str, list[dict[str, Any]]] = {"forest_2020": [], "forest_loss": [], "protected_areas": []}
    for region, kinds, n_forest, n_loss, pa_off, sample in (
        (VN, vn_kinds, 14, 1500, (9000.0, -6000.0), "vn"),
        (CO, co_kinds, 14, 1500, (-9000.0, 8000.0), "co"),
    ):
        forests, pa, _ = make_layers(rng, region, n_forest, n_loss, pa_off)
        order = [k for k in kinds if k != "duplicate"]
        rng.shuffle(order)
        if sample == "vn":
            order.insert(36, "utm_loss")  # row 38, plot code DL-0137
        order.insert(len(order) - 4, "duplicate")  # after the clean row it repeats
        specs = place_specs(rng, region, order, pa, forests)

        # loss patches: dedicated ones first, then background patches kept 300 m from every plot
        loss: list[tuple[Polygon, float]] = []
        for s in specs:
            if s.loss_target_ha:
                loss.append((dedicated_patch(rng, s), round(rng.uniform(0.70, 0.92), 2)))
        keep_out = unary_union([s.assess.buffer(300) for s in specs if s.assess is not None]
                               + [p.buffer(300) for p, _c in loss])
        for p in background_patches(rng, region, forests, n_loss - len(loss), keep_out):
            loss.append((p, round(rng.uniform(0.40, 0.95), 2)))
        # guarantee: only dedicated patches touch a plot
        for s in specs:
            if s.assess is None:
                continue
            near = [p for p, _ in loss if p.distance(s.assess) < 300]
            if s.loss_target_ha:
                assert len(near) == 1, f"{s.kind} plot near {len(near)} patches"
            else:
                assert not near, f"{s.kind} plot within 300 m of a loss patch"

        for i, f in enumerate(forests, 1):
            layers["forest_2020"].append({"type": "Feature", "properties": {"id": f"F20-{region.key}-{i:02d}", "region": region.key},
                                          "geometry": {"type": "Polygon", "coordinates": [ll_ring(region, f, 5)]}})
        for i, (p, conf) in enumerate(loss, 1):
            layers["forest_loss"].append({"type": "Feature", "properties": {
                "id": f"LP-{region.key}-{i:04d}", "region": region.key, "detected_on": random_date(rng), "confidence": conf},
                "geometry": {"type": "Polygon", "coordinates": [ll_ring(region, p)]}})
        layers["protected_areas"].append({"type": "Feature", "properties": {
            "id": f"PA-{region.key}-1", "name": f"Synthetic protected area {region.key}-1", "region": region.key},
            "geometry": {"type": "Polygon", "coordinates": [ll_ring(region, pa)]}})

        for s in specs:
            if s.geom != "none" and s.kind not in ("duplicate", "wrong_country"):
                s.lat, s.lon = region.to_ll(s.x, s.y)
                dist_border = countries[region.iso2].boundary.distance(Point(s.lon, s.lat)) * 108_000
                assert dist_border > 5000
        if sample == "vn":
            write_vn(rng, specs, countries)
        else:
            write_co(rng, specs, countries)

    for name, feats in layers.items():
        path = REF / f"{name}.geojson"
        body = {"type": "FeatureCollection", "name": name, "synthetic": True,
                "note": "Synthetic data made by scripts/make_synthetic.py; " + (
                    "a stand-in for a detection model's output, not a model" if name == "forest_loss" else "invented for this demo"),
                "features": feats}
        path.write_text(json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(feats)} features to {path.relative_to(ROOT)}")


def _truth_row(row: int, code: str, s: Spec) -> dict[str, Any]:
    c1, s1, c2, s2 = CODES[s.kind]
    return {"row": row, "plot_code": code, "kind": s.kind, "note": NOTES[s.kind],
            "v1": {"codes": sorted(c1), "status": s1}, "v2": {"codes": sorted(c2), "status": s2}}


def _write_truth(name: str, rows: list[dict[str, Any]], seed_note: str) -> None:
    changed = [r["plot_code"] for r in rows if r["v1"] != r["v2"]]
    problems = sum(len(r["v1"]["codes"]) for r in rows)
    body = {"sample": name, "synthetic": True, "generator": seed_note, "rows": rows,
            "v1_problems": problems, "changed_v1_to_v2": changed}
    (SAMPLES / f"{Path(name).stem}.truth.json").write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {name}: {len(rows)} rows, {problems} injected problems, {len(changed)} change under v2")


def _wrong_country_point(countries: dict[str, Any], declared: str, neighbour: str, options: list[tuple[float, float]]) -> tuple[float, float]:
    for lat, lon in options:
        pt = Point(lon, lat)
        if countries[neighbour].contains(pt) and countries[neighbour].boundary.distance(pt) * 108_000 > 10_000 \
                and not fits(countries, declared, lat, lon):
            return lat, lon
    raise RuntimeError("no wrong-country point fits the guarantees")


def write_vn(rng: random.Random, specs: list[Spec], countries: dict[str, Any]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Danh sach thua"
    headers = ["STT", "Mã thửa", "Tên nông hộ", "Xã", "Huyện", "Tỉnh", "Vĩ độ", "Kinh độ", "Diện tích (ha)", "Ranh giới thửa (WKT)", "Ghi chú"]
    ws.append(headers)
    communes = [f"Xã SYN-{c}" for c in "ABCDEF"]
    truth = []
    first_clean_point: tuple[int, list[Any], list[str]] | None = None
    for i, s in enumerate(specs):
        row = i + 2
        if s.kind == "duplicate":
            assert first_clean_point is not None
            src_row, values, fmts = first_clean_point
            values = list(values)
            values[0] = i + 1
            ws.append(values)
            for col, fmt in enumerate(fmts, 1):
                ws.cell(row=row, column=col).number_format = fmt
            truth.append(_truth_row(row, values[1], s) | {"note": f"repeat of row {src_row}"})
            continue
        code = f"DL-{101 + i:04d}"
        producer = f"Hộ SYN-{i + 1:03d}"
        commune = communes[rng.randrange(len(communes))]
        district = f"Huyện SYN-{1 + communes.index(commune) // 2}"
        lat_v: Any = None
        lon_v: Any = None
        lat_fmt = lon_fmt = "0.000000"
        wkt = None
        note = None
        if s.kind == "wrong_country":
            lat, lon = _wrong_country_point(countries, "VN", "KH", [(13.05, 106.55), (12.95, 106.35), (13.25, 106.1)])
            lat_v, lon_v = float(fmt6(lat)), float(fmt6(lon))
            s.area_ha = 1.8
        elif s.geom == "point":
            if s.kind in ("utm", "utm_loss"):
                e, n = utm_forward(s.lat, s.lon, 49)
                lat_v, lon_v = round(n, 2), round(e, 2)
                lat_fmt = lon_fmt = "0.00"
                note = "Tọa độ VN-UTM"
            elif s.kind == "swapped":
                lat_v, lon_v = float(fmt6(s.lon)), float(fmt6(s.lat))
            elif s.kind in ("four_dec", "four_dec_general"):
                lat_v, lon_v = round(s.lat, 4), round(s.lon, 4)
                lat_fmt = lon_fmt = "0.0000" if s.kind == "four_dec" else "General"
            elif s.kind == "zero_pad_fmt":
                lat_v, lon_v = round(s.lat, 4), round(s.lon, 4)
            elif s.kind == "zero_pad_text":
                lat_v = f"{round(s.lat, 4):.6f}".replace(".", ",")
                lon_v = float(fmt6(s.lon))
            elif s.kind == "unparseable":
                t = fmt6(s.lat)
                lat_v = t[:4] + "O" + t[5:]  # a letter O typed for a zero digit
                lon_v = float(fmt6(s.lon))
            else:
                lat_v, lon_v = float(fmt6(s.lat)), float(fmt6(s.lon))
        elif s.geom == "polygon":
            assert s.poly is not None
            wkt = bowtie_wkt(VN, s.poly) if s.kind == "bowtie" else wkt_text(VN, s.poly, close=s.kind != "unclosed")
            lat_v, lon_v = float(fmt6(s.lat)), float(fmt6(s.lon))
        values = [i + 1, code, producer, commune, district, "Đắk Lắk", lat_v, lon_v, s.area_ha, wkt, note]
        ws.append(values)
        fmts = ["General", "@", "@", "@", "@", "@", lat_fmt, lon_fmt, "0.00", "@", "@"]
        for col, fmt in enumerate(fmts, 1):
            if isinstance(values[col - 1], str) and col in (7, 8):
                continue  # text cells keep the default format
            ws.cell(row=row, column=col).number_format = fmt
        if s.kind == "clean" and s.geom == "point" and first_clean_point is None:
            first_clean_point = (row, values, fmts)
        if s.kind in ("swapped",):
            assert fits(countries, "VN", lat_v, lon_v) == ["swapped"], fits(countries, "VN", lat_v, lon_v)
        truth.append(_truth_row(row, code, s))
    widths = [6, 11, 14, 11, 12, 10, 13, 13, 14, 60, 16]
    for col, w in enumerate(widths):
        ws.column_dimensions[chr(65 + col)].width = w
    SAMPLES.mkdir(parents=True, exist_ok=True)
    path = SAMPLES / "vn_htx_daklak.xlsx"
    wb.save(path)
    _write_truth(path.name, truth, "scripts/make_synthetic.py --seed 7")


def write_co(rng: random.Random, specs: list[Spec], countries: dict[str, Any]) -> None:
    headers = ["Código finca", "Productor", "Vereda", "Municipio", "Departamento", "País", "Latitud", "Longitud", "Área (ha)", "Polígono (WKT)"]
    veredas = [f"Vereda SYN-{c}" for c in "ABCDEFG"]
    rows: list[list[str]] = []
    truth = []
    first_clean_point: tuple[int, list[str]] | None = None

    def dc(v: str) -> str:
        return v.replace(".", ",")

    for i, s in enumerate(specs):
        row = i + 2
        if s.kind == "duplicate":
            assert first_clean_point is not None
            rows.append(list(first_clean_point[1]))
            truth.append(_truth_row(row, first_clean_point[1][0], s) | {"note": f"repeat of row {first_clean_point[0]}"})
            continue
        code = f"HU-{201 + i:04d}"
        producer = f"Productor SYN-{i + 1:03d}"
        vereda = veredas[rng.randrange(len(veredas))]
        municipio = f"Municipio SYN-{1 + veredas.index(vereda) // 3}"
        lat_t = lon_t = wkt = ""
        area = "" if s.area_ha is None else dc(f"{s.area_ha:.2f}")
        if s.kind == "wrong_country":
            lat, lon = _wrong_country_point(countries, "CO", "EC", [(-1.75, -79.45), (-1.9, -79.6), (-1.2, -79.2)])
            lat_t, lon_t, area = dc(fmt6(lat)), dc(fmt6(lon)), "1,60"
        elif s.geom == "point":
            if s.kind == "sign":
                lat_t, lon_t = dc(fmt6(s.lat)), dc(fmt6(-s.lon))
            elif s.kind == "swapped":
                lat_t, lon_t = dc(fmt6(s.lon)), dc(fmt6(s.lat))
            elif s.kind == "four_dec":
                lat_t, lon_t = dc(f"{s.lat:.4f}"), dc(f"{s.lon:.4f}")
            elif s.kind == "zero_pad_text":
                lat_t, lon_t = dc(f"{round(s.lat, 4):.6f}"), dc(f"{round(s.lon, 4):.6f}")
            elif s.kind == "dms":
                lat_t, lon_t = to_dms(s.lat, True), to_dms(s.lon, False)
            elif s.kind == "dms_low":
                lat_t, lon_t = to_dms(s.lat, True, 0), to_dms(s.lon, False, 0)
            elif s.kind == "unparseable":
                lat_t, lon_t = "N/D", dc(fmt6(s.lon))
            else:
                lat_t, lon_t = dc(fmt6(s.lat)), dc(fmt6(s.lon))
        elif s.geom == "polygon":
            assert s.poly is not None
            wkt = bowtie_wkt(CO, s.poly) if s.kind == "bowtie" else wkt_text(CO, s.poly, close=s.kind != "unclosed")
            lat_t, lon_t = dc(fmt6(s.lat)), dc(fmt6(s.lon))
        values = [code, producer, vereda, municipio, "Huila", "Colombia", lat_t, lon_t, area, wkt]
        rows.append(values)
        if s.kind == "clean" and s.geom == "point" and first_clean_point is None:
            first_clean_point = (row, values)
        if s.kind in ("sign", "swapped"):
            la, lo = float(lat_t.replace(",", ".")), float(lon_t.replace(",", "."))
            expect = "lon_sign" if s.kind == "sign" else "swapped"
            assert fits(countries, "CO", la, lo) == [expect], fits(countries, "CO", la, lo)
        truth.append(_truth_row(row, code, s))
    path = SAMPLES / "co_cooperativa_huila.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(headers)
        w.writerows(rows)
    _write_truth(path.name, truth, "scripts/make_synthetic.py --seed 7")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    build(args.seed)


if __name__ == "__main__":
    main()
