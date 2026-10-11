"""Per-plot trace: every step from the raw cells to the verdict, stored so Support can replay it later."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import psycopg

from .mapping.fields import FIELD_LABELS
from .normalize import PlotInput
from .queries import Q
from .rulepack import RulePack
from .summary import summarize

CANDIDATE_LABELS = {
    "as_given": "As given",
    "swapped": "Latitude and longitude swapped",
    "lat_sign": "Minus sign added to latitude",
    "lon_sign": "Minus sign added to longitude",
    "both_signs": "Minus signs added to both",
    "swapped_lat_sign": "Swapped, minus on latitude",
    "swapped_lon_sign": "Swapped, minus on longitude",
    "swapped_both_signs": "Swapped, minus on both",
}


def candidate_label(kind: str) -> str:
    if kind.startswith("utm_"):
        _u, srid, axis = kind.split("_", 2)
        zone = int(srid) - 32600
        order = "easting, northing" if axis == "e_n" else "northing, easting"
        return f"UTM zone {zone}N metres (EPSG:{srid}), {order}"
    return CANDIDATE_LABELS.get(kind, kind)


def build_traces(conn: psycopg.Connection, run_id: int, farm_list: dict[str, Any], pack: RulePack,
                 inputs: list[tuple[int, PlotInput]]) -> list[dict[str, Any]]:
    headers: list[str] = farm_list["headers"]
    mapping: dict[str, str] = farm_list["mapping"]
    cells_by_raw = {r["id"]: r["cells"] for r in Q.get_raw_rows(conn, farm_list_id=farm_list["id"])}
    issues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i in Q.issues_for_run(conn, run_id=run_id):
        issues[i["plot_id"]].append(i)
    hits: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for h in Q.hits_for_run(conn, run_id=run_id):
        hits[h["plot_id"]].append(h)
    by_code = pack.by_code()
    loss_rule = pack.rule("loss_after_cutoff")
    profile_source = (farm_list.get("proposal") or {}).get("source_summary", "confirmed by a person")

    steps: list[dict[str, Any]] = []
    for p in Q.plot_trace_rows(conn, run_id=run_id):
        seq = 0

        def add(stage: str, title: str, detail: dict[str, Any], p: dict[str, Any] = p) -> None:
            nonlocal seq
            seq += 1
            steps.append({"run_id": run_id, "plot_id": p["id"], "seq": seq, "stage": stage, "title": title,
                          "detail": json.dumps(detail, ensure_ascii=False, default=str)})

        cells = cells_by_raw.get(p["raw_row_id"], [])
        add("received", "Row received", {
            "file": farm_list["file_name"], "row": p["row_number"],
            "cells": [{"header": h, "raw": c.get("raw", ""), "fmt": c.get("fmt")} for h, c in zip(headers, cells, strict=False)],
        })
        used = [{"header": h, "field": f, "label": FIELD_LABELS.get(f, f)} for h, f in mapping.items() if f not in ("ignore",)]
        add("mapping", "Columns mapped", {"profile_id": farm_list.get("mapping_profile_id"), "source": profile_source, "used": used})
        add("parse", "Values read", {"fields": p["parse"], "format": p["coord_format"], "decimals": p["coord_decimals"],
                                     "significant_decimals": p["coord_sig_decimals"]})

        if p["geom_kind"] == "polygon":
            add("geometry", "Boundary built", {
                "kind": "polygon", "valid_as_given": p["validity_reason"] is None, "reason": p["validity_reason"],
                "area_given_ha": p["area_given_ha"], "area_ha": p["area_ha"], "ring_autoclosed": p["ring_autoclosed"],
                "declared_area_ha": p["declared_area_ha"],
            })
        elif p["geom_kind"] == "point":
            add("geometry", "Point built", {"kind": "point", "lat_in": p["lat_in"], "lon_in": p["lon_in"],
                                            "declared_area_ha": p["declared_area_ha"], "format": p["coord_format"]})
        else:
            add("geometry", "No geometry", {"kind": None, "reason": "nothing could be read as a location"})

        if p["geom_kind"] == "point" and p["repair"]:
            rep = p["repair"]
            cands = [{**c, "label": candidate_label(c["kind"])} for c in rep.get("candidates", [])]
            if rep.get("as_given_ok"):
                outcome = "as_given"
            elif rep.get("n_fit") == 1:
                outcome = "repaired"
            elif (rep.get("n_fit") or 0) > 1:
                outcome = "ambiguous"
            else:
                outcome = "none"
            add("country", f"Checked against {farm_list['country_name']}", {
                "outcome": outcome, "fit_kind": rep.get("fit_kind"), "fit_label": candidate_label(rep["fit_kind"]) if rep.get("fit_kind") else None,
                "candidates": cands, "tolerance_m": pack.rule("coordinate_repair").p("country_tolerance_m"),
                "lat": p["lat"], "lon": p["lon"],
            })
        elif p["geom_kind"] == "polygon":
            outside = any(i["rule_key"] == "outside_country" for i in issues[p["id"]])
            add("country", f"Checked against {farm_list['country_name']}", {"outcome": "none" if outside else "as_given",
                                                                             "candidates": [], "polygon": True})

        fired = []
        for i in issues[p["id"]]:
            rule_key, rule = by_code.get(i["code"], (i["rule_key"], pack.rule(i["rule_key"])))
            fired.append({"code": i["code"], "severity": i["severity"], "rule_key": rule_key, "title": rule.title,
                          "message": i["message"], "citation": rule.cite, "fix_hint": rule.fix_hint})
        add("checks", f"{len(fired)} check{'s' if len(fired) != 1 else ''} fired", {"issues": fired})

        loss_hits = []
        for h in hits[p["id"]]:
            if h["layer"] != "forest_loss":
                continue
            conf = float(h["detail"].get("confidence") or 0)
            counted = h["overlap_ha"] >= loss_rule.p("min_overlap_ha") and conf >= loss_rule.p("min_confidence")
            why = "counted" if counted else (
                f"below the {loss_rule.p('min_overlap_ha')} ha minimum" if h["overlap_ha"] < loss_rule.p("min_overlap_ha")
                else f"confidence under {loss_rule.p('min_confidence')}")
            loss_hits.append({"id": h["ref_id"], "overlap_ha": round(h["overlap_ha"], 4), "detected_on": h["detail"].get("detected_on"),
                              "confidence": conf, "counted": counted, "why": why})
        protected = [{"id": h["ref_id"], "name": h["detail"].get("name"), "overlap_ha": round(h["overlap_ha"], 4)}
                     for h in hits[p["id"]] if h["layer"] == "protected_area"]
        forest = sum(h["overlap_ha"] for h in hits[p["id"]] if h["layer"] == "forest_2020")
        if p["assessable"]:
            add("assess", "Assessed against reference layers", {
                "basis": p["assess_basis"], "radius_m": p["circle_radius_m"], "assess_ha": p["assess_ha"],
                "loss": loss_hits, "protected": protected, "forest_2020_ha": round(forest, 4),
                "min_overlap_ha": loss_rule.p("min_overlap_ha"), "min_confidence": loss_rule.p("min_confidence"),
                "cutoff": loss_rule.p("cutoff_date"),
            })
        else:
            add("assess", "Not assessed", {"reason": _not_assessed_reason(p, issues[p["id"]])})

        summary, next_step = summarize(p, issues[p["id"]], loss_hits, protected, pack, farm_list["country_name"])
        add("verdict", "Verdict", {"status": p["status"], "summary": summary, "next_step": next_step})
    return steps


def _not_assessed_reason(p: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    keys = {i["rule_key"] for i in issues}
    if p["geom_kind"] is None:
        return "no usable location"
    if "outside_country" in keys:
        return "the location is not in the declared country"
    if p["geom_kind"] == "point" and p["declared_area_ha"] is None:
        return "the rule pack needs an area before a point can be assessed"
    return "no usable geometry"
