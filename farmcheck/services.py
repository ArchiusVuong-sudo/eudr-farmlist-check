"""Use cases behind the pages: upload, propose a mapping, confirm and run, re-run, diff, score, export."""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import SAMPLES_DIR
from .db import Database
from .ingest import Cell, header_signature, read_farm_list
from .mapping.checks import check_column
from .mapping.fields import FIELD_NAMES, missing_required
from .mapping.prompt import model_payload
from .mapping.providers import MappingProvider, ProposalUnavailable
from .mapping.rules import map_by_rules, rules_are_enough
from .pipeline import execute_run
from .profiles import ColumnProfile, profile_sheet
from .queries import Q, returned_id
from .reference import init_database, ensure_rule_packs
from .rulepack import changed_params, get_pack

log = logging.getLogger(__name__)


class UserError(ValueError):
    """Something the person can fix (shown on the page, not a crash)."""


class Engine:
    def __init__(self, db: Database) -> None:
        self.db = db
        with db.session() as conn:
            init_database(conn)
            self.pack_ids = ensure_rule_packs(conn)

    # ------------------------------------------------------------------------------ upload
    def upload(self, file_name: str, data: bytes, country_iso2: str) -> int:
        try:
            sheet = read_farm_list(file_name, data)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        rows = [cells for _n, cells in sheet.rows]
        profiles = profile_sheet(sheet.headers, rows)
        with self.db.transaction() as conn:
            if country_iso2 not in {c["iso2"] for c in Q.list_countries(conn)}:
                raise UserError("pick the country the plots are declared in")
            fl_id = returned_id(Q.insert_farm_list(
                conn, file_name=file_name, file_kind=sheet.file_kind, country_iso2=country_iso2,
                header_signature=header_signature(sheet.headers),
                headers=json.dumps(sheet.headers, ensure_ascii=False),
                profiles=json.dumps([p.model_dump() for p in profiles], ensure_ascii=False),
            ))
            Q.insert_raw_rows(conn, [
                {"farm_list_id": fl_id, "row_number": n, "cells": json.dumps([c.as_json() for c in cells], ensure_ascii=False)}
                for n, cells in sheet.rows
            ])
        return fl_id

    def upload_sample(self, name: str) -> int:
        samples = {"vn_htx_daklak.xlsx": "VN", "co_cooperativa_huila.csv": "CO"}
        if name not in samples:
            raise UserError("unknown sample")
        return self.upload(name, (SAMPLES_DIR / name).read_bytes(), samples[name])

    # ------------------------------------------------------------------------------ mapping
    def propose(self, farm_list_id: int, provider: MappingProvider) -> dict[str, Any]:
        """Header rules, then a saved profile, then the model. The model call runs outside the DB lock."""
        with self.db.session() as conn:
            fl = Q.get_farm_list(conn, farm_list_id=farm_list_id)
            if fl is None:
                raise UserError("no such farm list")
            if fl["proposal"]:
                return fl["proposal"]
            saved = Q.get_mapping_profile(conn, signature=fl["header_signature"])
        headers: list[str] = fl["headers"]
        profiles = [ColumnProfile.model_validate(p) for p in fl["profiles"]]
        by_rules = map_by_rules(headers)
        proposal: dict[str, Any] = {"columns": [], "model": None, "error": None, "payload": None}
        if saved:
            proposal["source"] = "profile"
            proposal["source_summary"] = f"saved mapping profile #{saved['id']} (same header layout), no model call"
            proposal["profile_id"] = saved["id"]
            cols = [{"column": h, "field": saved["mapping"].get(h, "ignore"), "confidence": 1.0,
                     "reason": "from the saved profile", "source": "profile"} for h in headers]
        elif rules_are_enough(headers, by_rules):
            proposal["source"] = "rules"
            proposal["source_summary"] = "header rules (English, Spanish, Portuguese, French), no model call"
            cols = [{"column": h, "field": by_rules.get(h, "ignore"), "confidence": 1.0 if h in by_rules else 0.0,
                     "reason": "header rule match" if h in by_rules else "no header rule matched", "source": "rules" if h in by_rules else "none"}
                    for h in headers]
        else:
            payload = model_payload(headers, profiles, fl["country_name"])
            proposal["payload"] = payload
            proposal["source"] = "model"
            proposal["rules_matched"] = len(by_rules)
            try:
                result = provider.propose(headers, payload)
                model_cols = {c.column: c for c in result.proposal.columns}
                cols = []
                for h in headers:
                    if h in by_rules:
                        cols.append({"column": h, "field": by_rules[h], "confidence": 1.0, "reason": "header rule match", "source": "rules"})
                    else:
                        c = model_cols[h]
                        cols.append({"column": h, "field": c.field, "confidence": c.confidence, "reason": c.reason, "source": "model"})
                proposal["model"] = result.model_dump(mode="json", exclude={"proposal"})
                proposal["source_summary"] = (
                    f"model proposal ({result.mode}, {result.model}, {result.recorded_at}) confirmed by a person")
            except ProposalUnavailable as exc:
                proposal["error"] = str(exc)
                proposal["source_summary"] = "mapped by a person"
                cols = [{"column": h, "field": by_rules.get(h, "ignore"), "confidence": 0.0, "reason": "map by hand",
                         "source": "rules" if h in by_rules else "none"} for h in headers]
        prof_by_header = {p.header: p for p in profiles}
        for c in cols:
            vc = check_column(c["field"], prof_by_header[c["column"]])
            c["check_ok"], c["check"] = vc.ok, vc.message
        proposal["columns"] = cols
        with self.db.transaction() as conn:
            Q.set_proposal(conn, farm_list_id=farm_list_id, proposal=json.dumps(proposal, ensure_ascii=False))
        return proposal

    def confirm_and_run(self, farm_list_id: int, mapping: dict[str, str], version: str) -> int:
        with self.db.transaction() as conn:
            fl = Q.get_farm_list(conn, farm_list_id=farm_list_id)
            if fl is None:
                raise UserError("no such farm list")
            headers: list[str] = fl["headers"]
            if set(mapping) != set(headers) or any(f not in FIELD_NAMES for f in mapping.values()):
                raise UserError("every column needs one of the listed fields")
            used = [f for f in mapping.values() if f not in ("ignore", "notes")]
            dupes = sorted({f for f in used if used.count(f) > 1})
            if dupes:
                raise UserError(f"one column per field: {', '.join(dupes)} is used twice")
            missing = missing_required(set(mapping.values()))
            if missing:
                raise UserError("the mapping still needs: " + ", ".join(missing))
            proposal = fl["proposal"] or {}
            changed = any(c["field"] != mapping[c["column"]] for c in proposal.get("columns", []))
            source = "person" if changed else proposal.get("source", "person")
            prof_id = returned_id(Q.upsert_mapping_profile(
                conn, signature=fl["header_signature"], headers=json.dumps(headers, ensure_ascii=False),
                mapping=json.dumps(mapping, ensure_ascii=False), source=source))
            Q.set_mapping(conn, farm_list_id=farm_list_id, mapping=json.dumps(mapping, ensure_ascii=False), profile_id=prof_id)
            if changed:
                proposal["source_summary"] = (proposal.get("source_summary", "") + "; edited by a person").lstrip("; ")
                Q.set_proposal(conn, farm_list_id=farm_list_id, proposal=json.dumps(proposal, ensure_ascii=False))
            return execute_run(conn, farm_list_id, version, self.pack_ids)

    def rerun(self, farm_list_id: int, version: str) -> int:
        get_pack(version)
        with self.db.transaction() as conn:
            return execute_run(conn, farm_list_id, version, self.pack_ids)

    # ------------------------------------------------------------------------------ results
    def diff(self, run_a: int, run_b: int) -> dict[str, Any]:
        with self.db.session() as conn:
            ra, rb = Q.get_run(conn, run_id=run_a), Q.get_run(conn, run_id=run_b)
            if ra is None or rb is None or ra["farm_list_id"] != rb["farm_list_id"]:
                raise UserError("pick two runs of the same farm list")
            pa = {p["row_number"]: p for p in Q.plots_for_run(conn, run_id=run_a)}
            pb = {p["row_number"]: p for p in Q.plots_for_run(conn, run_id=run_b)}
        return compute_diff(ra, rb, pa, pb)

    def score(self, run_id: int) -> dict[str, Any] | None:
        with self.db.session() as conn:
            run = Q.get_run(conn, run_id=run_id)
            if run is None:
                return None
            plots = Q.plots_for_run(conn, run_id=run_id)
        return score_against_truth(run["file_name"], run["version"], plots)

    def fix_list_csv(self, run_id: int) -> tuple[str, list[dict[str, Any]]]:
        with self.db.session() as conn:
            run = Q.get_run(conn, run_id=run_id)
            rows = Q.fix_list_rows(conn, run_id=run_id)
        pack = get_pack(run["version"])
        out_rows = []
        for r in rows:
            rule = pack.rule(r["rule_key"])
            hint = rule.fix_hint
            if r["severity"] == "repaired":
                hint = rule.fix_hint if r["rule_key"] != "coordinate_repair" else "Confirm the corrected coordinates"
            out_rows.append({"row_number": r["row_number"], "plot_code": r["plot_code"], "issue_code": r["code"],
                             "severity": r["severity"], "message": r["message"], "fix_hint": hint})
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["row_number", "plot_code", "issue_code", "severity", "message", "fix_hint"])
        w.writeheader()
        w.writerows(out_rows)
        return buf.getvalue(), out_rows


# ---------------------------------------------------------------------------------- diff, score
def _codes(p: dict[str, Any]) -> list[str]:
    return sorted(i["code"] for i in p["issues"])


def _verdict(p: dict[str, Any]) -> str:
    codes = set(_codes(p))
    if "LOSS_AFTER_CUTOFF" in codes:
        return "loss after cut-off"
    if "PROTECTED_AREA_OVERLAP" in codes:
        return "protected-area overlap"
    return "no overlap found" if p.get("assessable") else "not assessed"


def compute_diff(ra: dict[str, Any], rb: dict[str, Any], pa: dict[int, dict[str, Any]], pb: dict[int, dict[str, Any]]) -> dict[str, Any]:
    pack_a, pack_b = get_pack(ra["version"]), get_pack(rb["version"])
    params = changed_params(pack_a, pack_b)
    code_rule = {**{r.code: k for k, r in pack_a.rules.items()}, **{r.code: k for k, r in pack_b.rules.items()}}
    changes = []
    for row in sorted(set(pa) | set(pb)):
        a, b = pa.get(row), pb.get(row)
        if a is None or b is None:
            continue
        ca, cb = _codes(a), _codes(b)
        if a["status"] == b["status"] and ca == cb and _verdict(a) == _verdict(b):
            continue
        added = sorted(set(cb) - set(ca))
        removed = sorted(set(ca) - set(cb))
        sev_a = {i["code"]: i["severity"] for i in a["issues"]}
        sev_b = {i["code"]: i["severity"] for i in b["issues"]}
        same_sev = sorted(c for c in set(ca) & set(cb) if sev_a[c] != sev_b[c])
        causes = []
        for code in added + removed + same_sev:
            key = code_rule.get(code)
            if not key:
                continue
            for pk, (old, new) in params.items():
                if pk.startswith(key + ".") and pk != f"{key}.code":
                    causes.append({"rule_key": pk, "old": old, "new": new, "code": code})
        if not causes:
            for code in ca:
                key = code_rule.get(code)
                causes += [{"rule_key": pk, "old": o, "new": n, "code": code} for pk, (o, n) in params.items()
                           if key and pk.startswith(key + ".") and pk != f"{key}.code"]
        rename = [c for c in added if c not in pack_a.by_code() and any(pack_b.rule(k).code == c for k in pack_b.rules)]
        changes.append({
            "row": row, "plot_code": b["plot_code"], "plot_id": b["id"],
            "status_old": a["status"], "status_new": b["status"], "verdict_old": _verdict(a), "verdict_new": _verdict(b),
            "codes_old": ca, "codes_new": cb, "added": added, "removed": removed, "severity_changed": same_sev,
            "causes": _dedupe(causes), "renamed": rename,
        })
    truth = load_truth(ra["file_name"])
    predicted = truth.get("changed_v1_to_v2") if truth and {ra["version"], rb["version"]} == {"v1", "v2"} else None
    found = [c["plot_code"] for c in changes]
    return {
        "a": ra, "b": rb, "params": params, "changes": changes,
        "predicted": predicted,
        "matches_prediction": predicted is not None and sorted(predicted) == sorted(found),
        "same_pack": ra["version"] == rb["version"],
    }


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for c in items:
        k = (c["rule_key"], str(c["old"]), str(c["new"]))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def load_truth(file_name: str) -> dict[str, Any] | None:
    path = SAMPLES_DIR / (file_name.rsplit(".", 1)[0] + ".truth.json")
    if "/" in file_name or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Score:
    sample: str
    version: str
    injected: int
    found: int
    unexpected: list[dict[str, Any]] = field(default_factory=list)
    missed: list[dict[str, Any]] = field(default_factory=list)
    status_match: int = 0
    rows: int = 0
    status_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def perfect(self) -> bool:
        return self.found == self.injected and not self.unexpected and self.status_match == self.rows


def score_against_truth(file_name: str, version: str, plots: list[dict[str, Any]]) -> dict[str, Any] | None:
    truth = load_truth(file_name)
    if truth is None or version not in ("v1", "v2"):
        return None
    by_row = {p["row_number"]: p for p in plots}
    s = Score(sample=file_name, version=version, injected=0, found=0, rows=len(truth["rows"]))
    for t in truth["rows"]:
        exp = t[version]
        p = by_row.get(t["row"])
        got = set(_codes(p)) if p else set()
        want = set(exp["codes"])
        s.injected += len(want)
        s.found += len(want & got)
        for c in sorted(want - got):
            s.missed.append({"row": t["row"], "plot_code": t["plot_code"], "code": c})
        for c in sorted(got - want):
            s.unexpected.append({"row": t["row"], "plot_code": t["plot_code"], "code": c})
        if p and p["status"] == exp["status"]:
            s.status_match += 1
        else:
            s.status_mismatches.append({"row": t["row"], "plot_code": t["plot_code"], "expected": exp["status"],
                                        "got": p["status"] if p else None})
    return {**s.__dict__, "perfect": s.perfect}


__all__ = ["Engine", "UserError", "Cell", "compute_diff", "score_against_truth", "load_truth"]
