"""One run: normalise rows in Python, then validate, repair and assess every plot with set-based PostGIS SQL."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import psycopg

from .normalize import normalize_row
from .queries import Q, returned_id
from .reference import dataset_checksum
from .rulepack import RulePack, get_pack
from .trace import build_traces

STAGES = ("parse", "load", "repair", "checks", "assess", "verdict", "trace")


def _rule_args(pack: RulePack, key: str) -> dict[str, Any]:
    r = pack.rule(key)
    return {"code": r.code, "severity": r.severity}


def _explain_line(plan: list[str]) -> str:
    """The plan line that says how forest_loss is read (index scan or not), as PostGIS printed it."""
    for line in plan:
        if "forest_loss" in line:
            return line.strip().lstrip("-> ").strip()
    return plan[0].strip() if plan else ""


def execute_run(conn: psycopg.Connection, farm_list_id: int, version: str, rule_pack_ids: dict[str, int]) -> int:
    """Create and run one run inside the caller's transaction; returns the run id."""
    pack = get_pack(version)
    fl = Q.get_farm_list(conn, farm_list_id=farm_list_id)
    if fl is None or fl["mapping"] is None:
        raise ValueError("the farm list has no confirmed mapping")
    headers: list[str] = fl["headers"]
    mapping: dict[str, str] = fl["mapping"]
    iso2: str = fl["country_iso2"]
    stage_ms: dict[str, float] = {}

    run_id = returned_id(Q.insert_run(conn, farm_list_id=farm_list_id, rule_pack_id=rule_pack_ids[version],
                                      dataset_checksum=dataset_checksum()))
    rid = {"run_id": run_id}

    # parse (Python)
    t = time.perf_counter()
    raw_rows = Q.get_raw_rows(conn, farm_list_id=farm_list_id)
    inputs = [(r["id"], normalize_row(r["row_number"], r["cells"], headers, mapping)) for r in raw_rows]
    stage_ms["parse"] = (time.perf_counter() - t) * 1000

    # load
    t = time.perf_counter()
    Q.insert_plots(conn, [
        {**rid, "raw_row_id": raw_id, "row_number": p.row_number, "plot_code": p.plot_code, "producer": p.producer,
         "admin_area": p.admin_area, "declared_area_ha": p.declared_area_ha, "lat_in": p.lat_in, "lon_in": p.lon_in,
         "coord_format": p.coord_format, "coord_decimals": p.coord_decimals, "coord_sig_decimals": p.coord_sig_decimals,
         "wkt_in": p.wkt_in, "ring_autoclosed": p.ring_autoclosed, "parse": json.dumps(p.parse, ensure_ascii=False)}
        for raw_id, p in inputs
    ])
    row_issues = []
    for _raw_id, p in inputs:
        for issue in p.issues:
            rule = pack.rule(issue.rule_key)
            row_issues.append({**rid, "row_number": p.row_number, "code": rule.code, "severity": rule.severity,
                               "rule_key": issue.rule_key, "message": issue.message,
                               "detail": json.dumps(issue.detail, ensure_ascii=False)})
    if row_issues:
        Q.insert_row_issues(conn, row_issues)
    Q.build_geometry(conn, **rid)
    stage_ms["load"] = (time.perf_counter() - t) * 1000

    # repair
    t = time.perf_counter()
    Q.repair_coordinates(conn, **rid, iso2=iso2)
    Q.repair_polygons(conn, **rid)
    Q.measure_polygons(conn, **rid)
    stage_ms["repair"] = (time.perf_counter() - t) * 1000

    # checks
    t = time.perf_counter()
    r = pack.rule
    Q.check_precision(conn, **rid, **_rule_args(pack, "coordinate_precision"), min_decimals=r("coordinate_precision").p("min_decimals"))
    Q.check_zero_padded(conn, **rid, **_rule_args(pack, "zero_padded_precision"),
                        min_decimals=r("coordinate_precision").p("min_decimals"),
                        min_trailing_zeros=r("zero_padded_precision").p("min_trailing_zeros"))
    Q.check_point_over_4ha(conn, **rid, **_rule_args(pack, "point_over_4ha"), max_ha=r("point_over_4ha").p("max_point_area_ha"))
    Q.check_point_area_missing(conn, **rid, **_rule_args(pack, "point_area_missing"),
                               mode=r("point_area_missing").p("mode"), assumed_ha=r("point_area_missing").p("assumed_area_ha"))
    Q.check_self_intersection(conn, **rid, **_rule_args(pack, "self_intersection"))
    Q.check_ring_closed(conn, **rid, **_rule_args(pack, "ring_not_closed"))
    Q.check_coordinate_repair(conn, **rid, **_rule_args(pack, "coordinate_repair"))
    Q.check_outside_country(conn, **rid, **_rule_args(pack, "outside_country"), iso2=iso2)
    Q.check_implausible_area(conn, **rid, **_rule_args(pack, "implausible_area"), max_ha=r("implausible_area").p("max_area_ha"))
    Q.check_area_mismatch(conn, **rid, **_rule_args(pack, "area_mismatch"), max_ha=r("implausible_area").p("max_area_ha"),
                          max_rel=r("area_mismatch").p("max_relative_diff"))
    Q.check_duplicates(conn, **rid, **_rule_args(pack, "duplicate_plot"), same_point_m=r("duplicate_plot").p("same_point_m"))
    Q.check_plot_overlaps(conn, **rid, **_rule_args(pack, "plot_overlap"), min_ha=r("plot_overlap").p("min_overlap_ha"))
    stage_ms["checks"] = (time.perf_counter() - t) * 1000

    # assess
    t = time.perf_counter()
    require_area = r("point_area_missing").p("mode") == "require"
    Q.set_assessable(conn, **rid, require_area=require_area)
    Q.build_assess_geometry(conn, **rid, assumed_ha=r("point_area_missing").p("assumed_area_ha"))
    Q.buffer_points(conn, **rid)
    Q.analyze_plots(conn)
    cutoff = str(r("loss_after_cutoff").p("cutoff_date"))
    # EXPLAIN the very SELECT the insert below runs (taken from its SQL), so the plan shown is the real one.
    plan = [row["QUERY PLAN"] for row in conn.execute(
        "EXPLAIN " + re.split(r"(?=SELECT)", Q.insert_loss_hits.sql, maxsplit=1)[1],
        {"run_id": run_id, "cutoff": cutoff}).fetchall()]
    q0 = time.perf_counter()
    Q.insert_loss_hits(conn, **rid, cutoff=cutoff)
    assess_ms = (time.perf_counter() - q0) * 1000
    Q.insert_protected_hits(conn, **rid)
    Q.insert_forest_hits(conn, **rid)
    Q.check_loss(conn, **rid, **_rule_args(pack, "loss_after_cutoff"), cutoff=cutoff,
                 min_ha=r("loss_after_cutoff").p("min_overlap_ha"), min_confidence=r("loss_after_cutoff").p("min_confidence"))
    Q.check_protected(conn, **rid, **_rule_args(pack, "protected_area"), min_ha=r("protected_area").p("min_overlap_ha"))
    stage_ms["assess"] = (time.perf_counter() - t) * 1000

    # verdict
    t = time.perf_counter()
    Q.set_status(conn, **rid)
    counts = {row["status"]: row["n"] for row in Q.status_counts(conn, **rid)}
    stage_ms["verdict"] = (time.perf_counter() - t) * 1000

    # trace
    t = time.perf_counter()
    steps = build_traces(conn, run_id, fl, pack, inputs)
    if steps:
        Q.insert_trace_steps(conn, steps)
    stage_ms["trace"] = (time.perf_counter() - t) * 1000

    Q.finish_run(conn, **rid, stage_ms=json.dumps({k: round(v, 1) for k, v in stage_ms.items()}),
                 counts=json.dumps(counts), result_hash=result_hash(conn, run_id, pack),
                 assess_ms=round(assess_ms, 2), explain_plan="\n".join(plan))
    return run_id


def result_hash(conn: psycopg.Connection, run_id: int, pack: RulePack) -> str:
    """Same list, same rule pack and same reference data give the same hash, run after run."""
    h = hashlib.sha256()
    h.update(f"pack:{pack.version}:{pack.checksum};data:{dataset_checksum()}\n".encode())
    for row in Q.result_rows(conn, run_id=run_id):
        h.update(json.dumps(row, sort_keys=True, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def explain_summary(plan_text: str | None) -> str:
    return _explain_line((plan_text or "").splitlines())

