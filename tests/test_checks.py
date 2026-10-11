"""Every rule fires on a row built to break it, and on nothing else (Colombian layout, rule pack v1 and v2)."""

import csv
import io
import json

import pytest

from farmcheck import REFERENCE_DIR, SAMPLES_DIR
from farmcheck.mapping.providers import ReplayProvider
from farmcheck.queries import Q

HEAD = ["Código finca", "Productor", "Vereda", "Municipio", "Departamento", "País", "Latitud", "Longitud", "Área (ha)", "Polígono (WKT)"]


def _sample():
    truth = json.loads((SAMPLES_DIR / "co_cooperativa_huila.truth.json").read_text())
    kinds = {r["row"]: r["kind"] for r in truth["rows"]}
    with (SAMPLES_DIR / "co_cooperativa_huila.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    by_kind: dict[str, list[dict[str, str]]] = {}
    for i, r in enumerate(rows, start=2):
        kind = kinds[i]
        if kind == "clean":
            kind = "clean_poly" if r["Polígono (WKT)"] else "clean_point"
        by_kind.setdefault(kind, []).append(r)
    return by_kind


def _dc(x: float) -> str:
    return f"{x:.6f}".replace(".", ",")


def _patch_point(layer: str, pred) -> tuple[float, float]:
    feats = json.loads((REFERENCE_DIR / layer).read_text())["features"]
    f = next(f for f in feats if pred(f["properties"]) and f["properties"].get("region", "CO") == "CO")
    ring = f["geometry"]["coordinates"][0][:-1]
    return sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)


def build_rows() -> list[tuple[dict[str, str], list[str], list[str]]]:
    k = _sample()
    pts, polys = k["clean_point"], k["clean_poly"]
    out: list[tuple[dict[str, str], list[str], list[str]]] = []

    def add(base, v1, v2=None, **changes):
        row = dict(base)
        row.update({h: v for h, v in changes.items()})
        row["Código finca"] = f"T-{len(out) + 1:02d}"
        out.append((row, v1, v1 if v2 is None else v2))

    def latlon(r):
        return float(r["Latitud"].replace(",", ".")), float(r["Longitud"].replace(",", "."))

    add(pts[0], [])
    la, lo = latlon(pts[1]); add(pts[1], ["COORD_PRECISION"], Latitud=f"{la:.4f}".replace(".", ","))
    la, lo = latlon(pts[2]); add(pts[2], ["COORD_ZERO_PADDED"], Latitud=f"{round(la, 4):.6f}".replace(".", ","))
    la, lo = latlon(pts[3]); add(pts[3], ["COORD_REPAIRED"], Latitud=_dc(lo), Longitud=_dc(la))
    la, lo = latlon(pts[4]); add(pts[4], ["COORD_REPAIRED"], Longitud=_dc(-lo))
    add(pts[5], ["POINT_OVER_4HA"], **{"Área (ha)": "6,40"})
    add(pts[6], ["POINT_AREA_ASSUMED"], ["POINT_AREA_NEEDED"], **{"Área (ha)": ""})
    add(pts[7], ["MISSING_GEOLOCATION"], Latitud="", Longitud="")
    add(pts[8], ["UNPARSEABLE_VALUE"], Latitud="2,3x5")
    add(pts[9], ["OUTSIDE_COUNTRY"], Latitud=_dc(-1.751234), Longitud=_dc(-79.451237))
    add(k["bowtie"][0], ["SELF_INTERSECTION"])
    add(k["unclosed"][0], ["RING_AUTOCLOSED"])
    add(polys[0], ["IMPLAUSIBLE_AREA"], **{"Área (ha)": "0"})
    area = float(polys[1]["Área (ha)"].replace(",", "."))
    add(polys[1], ["AREA_MISMATCH"], **{"Área (ha)": f"{area * 2:.2f}".replace(".", ",")})
    add(k["overlap"][0], ["PLOT_OVERLAP"])
    add(k["overlap"][1], ["PLOT_OVERLAP"])
    la, lo = _patch_point("forest_loss.geojson", lambda p: p["id"].startswith("LP-CO") and p["confidence"] >= 0.7 and p["id"] > "LP-CO-0100")
    add(pts[10], ["LOSS_AFTER_CUTOFF"], Latitud=_dc(la), Longitud=_dc(lo), **{"Área (ha)": "2,00"})
    la, lo = _patch_point("protected_areas.geojson", lambda p: p["id"] == "PA-CO-1")
    add(pts[11], ["PROTECTED_AREA_OVERLAP"], Latitud=_dc(la), Longitud=_dc(lo))
    dup = dict(out[0][0])
    out.append((dup, ["DUPLICATE_PLOT"], ["DUPLICATE_PLOT"]))
    return out


@pytest.fixture(scope="module")
def runs(engine):
    rows = build_rows()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEAD, delimiter=";")
    w.writeheader()
    for r, _a, _b in rows:
        w.writerow(r)
    fl = engine.upload("crafted_rules.csv", buf.getvalue().encode(), "CO")
    prop = engine.propose(fl, ReplayProvider())
    assert prop["source"] in ("rules", "profile")
    mapping = {c["column"]: c["field"] for c in prop["columns"]}
    r1 = engine.confirm_and_run(fl, mapping, "v1")
    r2 = engine.rerun(fl, "v2")
    with engine.db.session() as conn:
        p1 = list(Q.plots_for_run(conn, run_id=r1))
        p2 = list(Q.plots_for_run(conn, run_id=r2))
    return rows, p1, p2


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_each_rule_fires_only_on_its_row(runs, version):
    rows, p1, p2 = runs
    plots = p1 if version == "v1" else p2
    assert len(plots) == len(rows)
    for (row, want1, want2), p in zip(rows, plots, strict=True):
        want = want1 if version == "v1" else want2
        got = sorted(i["code"] for i in p["issues"])
        assert got == sorted(want), f"{row['Código finca']} ({version}): {got} != {want}"


def test_statuses_follow_severity(runs):
    _rows, p1, p2 = runs
    s1 = {p["plot_code"] + str(p["row_number"]): p["status"] for p in p1}
    assert list(s1.values())[0] == "ready"
    by = {p["row_number"]: p for p in p1}
    assert by[2 + 2]["status"] == "ready"  # zero padded is a warning in v1
    assert {p["row_number"]: p for p in p2}[4]["status"] == "needs_fix"  # and an error in v2
    assert by[5]["status"] == "repaired" and by[18]["status"] == "review" and by[3]["status"] == "needs_fix"


def test_unparseable_issue_quotes_the_raw_cell(runs):
    _rows, p1, _p2 = runs
    msg = next(i["message"] for p in p1 for i in p["issues"] if i["code"] == "UNPARSEABLE_VALUE")
    assert "'2,3x5'" in msg and "Latitud" in msg
