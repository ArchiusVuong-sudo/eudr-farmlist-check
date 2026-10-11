"""Flask app: pages for upload, mapping approval, results, investigation, rule-pack diff and exports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from flask import Flask, Response, abort, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from .mapping.fields import FIELDS
from .mapping.providers import MappingProvider, get_provider, live_available, selected_model
from .pipeline import explain_summary
from .queries import Q
from .reference import LAYERS
from .rulepack import DEMO_NOTE, available_versions, changed_params, get_pack
from .services import Engine, UserError
from .summary import STATUS_LABELS
from .trace import candidate_label

log = logging.getLogger(__name__)

STATUS_ORDER = ("ready", "repaired", "review", "needs_fix")
SEVERITY_LABELS = {"error": "Error", "review": "Review", "repaired": "Repaired", "warning": "Warning"}
STAGE_LABELS = {"parse": "Parse", "load": "Load", "repair": "Repair", "checks": "Checks", "assess": "Assess",
                "verdict": "Verdict", "trace": "Trace"}


def create_app(engine: Engine, provider_factory: Callable[[], MappingProvider] = get_provider) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.extensions["engine"] = engine
    db = engine.db

    @app.context_processor
    def globals_() -> dict[str, Any]:
        live, why = live_available()
        return {
            "replay_mode": not live,
            "replay_reason": why,
            "model_name": selected_model(),
            "STATUS_LABELS": STATUS_LABELS,
            "STATUS_ORDER": STATUS_ORDER,
            "SEVERITY_LABELS": SEVERITY_LABELS,
            "DEMO_NOTE": DEMO_NOTE,
        }

    @app.template_filter("ha")
    def fmt_ha(v: Any, digits: int = 2) -> str:
        return "-" if v is None else f"{float(v):.{digits}f} ha"

    @app.template_filter("ms")
    def fmt_ms(v: Any) -> str:
        if v is None:
            return "-"
        v = float(v)
        return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"

    @app.template_filter("cand")
    def fmt_cand(kind: str) -> str:
        return candidate_label(kind)

    @app.template_filter("tojson_pretty")
    def tojson_pretty(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False, indent=2)

    @app.errorhandler(UserError)
    def user_error(exc: UserError) -> tuple[str, int]:
        return render_template("error.html", title="That did not work", message=str(exc)), 400

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> tuple[str, int]:
        return render_template("error.html", title=exc.name, message=exc.description), exc.code or 500

    @app.errorhandler(Exception)
    def crash(exc: Exception) -> tuple[str, int]:
        log.exception("unhandled error")
        return render_template("error.html", title="Something went wrong", message="The error was logged on the server."), 500

    # -------------------------------------------------------------------------- farm lists
    @app.get("/")
    def index() -> str:
        with db.session() as conn:
            lists = list(Q.list_farm_lists(conn))
            countries = list(Q.list_countries(conn))
        return render_template("index.html", lists=lists, countries=countries)

    @app.post("/lists")
    def upload() -> Response:
        sample = request.form.get("sample")
        if sample:
            fl_id = engine.upload_sample(sample)
        else:
            f = request.files.get("file")
            if f is None or not f.filename:
                raise UserError("choose a CSV or XLSX file to upload")
            fl_id = engine.upload(f.filename, f.read(), request.form.get("country", ""))
        return redirect(url_for("mapping", farm_list_id=fl_id), code=303)

    @app.get("/lists/<int:farm_list_id>/mapping")
    def mapping(farm_list_id: int) -> str:
        with db.session() as conn:
            fl = Q.get_farm_list(conn, farm_list_id=farm_list_id) or abort(404)
            n_rows = len(list(Q.get_raw_rows(conn, farm_list_id=farm_list_id)))
        return render_template("mapping.html", fl=fl, n_rows=n_rows)

    @app.get("/lists/<int:farm_list_id>/proposal")
    def proposal(farm_list_id: int) -> str:
        prop = engine.propose(farm_list_id, provider_factory())
        with db.session() as conn:
            fl = Q.get_farm_list(conn, farm_list_id=farm_list_id) or abort(404)
        profiles = {p["header"]: p for p in fl["profiles"]}
        return render_template("_proposal.html", fl=fl, prop=prop, profiles=profiles, fields=FIELDS,
                               versions=available_versions(), packs={v: get_pack(v) for v in available_versions()})

    @app.post("/lists/<int:farm_list_id>/confirm")
    def confirm(farm_list_id: int) -> Response:
        with db.session() as conn:
            fl = Q.get_farm_list(conn, farm_list_id=farm_list_id) or abort(404)
        chosen = {h: request.form.get(f"field_{i}", "ignore") for i, h in enumerate(fl["headers"])}
        version = request.form.get("version", "v1")
        get_pack(version)
        run_id = engine.confirm_and_run(farm_list_id, chosen, version)
        return redirect(url_for("run_page", run_id=run_id), code=303)

    @app.post("/lists/<int:farm_list_id>/runs")
    def rerun(farm_list_id: int) -> Response:
        version = request.form.get("version", "v1")
        base = request.form.get("base_run", type=int)
        try:
            get_pack(version)
        except KeyError as exc:
            raise UserError(str(exc)) from exc
        run_id = engine.rerun(farm_list_id, version)
        if base:
            with db.session() as conn:
                base_run = Q.get_run(conn, run_id=base)
            if base_run and base_run["version"] != version:
                return redirect(url_for("diff_page", run_a=base, run_b=run_id), code=303)
        return redirect(url_for("run_page", run_id=run_id), code=303)

    # -------------------------------------------------------------------------- results
    def _run_or_404(conn: Any, run_id: int) -> dict[str, Any]:
        run = Q.get_run(conn, run_id=run_id)
        if run is None:
            abort(404)
        return run

    @app.get("/runs/<int:run_id>")
    def run_page(run_id: int) -> str:
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
            summary = list(Q.issue_summary(conn, run_id=run_id))
            runs = list(Q.runs_for_list(conn, farm_list_id=run["farm_list_id"]))
            prev = Q.previous_same_pack(conn, farm_list_id=run["farm_list_id"], rule_pack_id=run["rule_pack_id"], run_id=run_id)
            plots = list(Q.plots_for_run(conn, run_id=run_id))
        pack = get_pack(run["version"])
        by_code = pack.by_code()
        rules = []
        for s in summary:
            key, rule = by_code.get(s["code"], (s["rule_key"], pack.rule(s["rule_key"])))
            rules.append({**s, "title": rule.title, "citation": rule.cite, "legal": rule.citation is not None,
                          "params": {k: v.model_dump() for k, v in rule.params.items()}})
        score = engine.score(run_id)
        counts = {k: run["counts"].get(k, 0) for k in STATUS_ORDER}
        return render_template(
            "run.html", run=run, rules=rules, counts=counts, total=sum(counts.values()), runs=runs, prev=prev, score=score,
            plan_line=explain_summary(run["explain_plan"]), versions=available_versions(), packs={v: get_pack(v) for v in available_versions()},
            plots=plots, status=None, stage_labels=STAGE_LABELS,
        )

    @app.get("/runs/<int:run_id>/plots")
    def run_plots(run_id: int) -> str:
        status = request.args.get("status") or None
        if status and status not in STATUS_ORDER:
            abort(400)
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
            plots = [p for p in Q.plots_for_run(conn, run_id=run_id) if status is None or p["status"] == status]
        return render_template("_plots.html", run=run, plots=plots, status=status)

    @app.get("/runs/<int:run_id>/map.json")
    def run_map(run_id: int) -> Response:
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
            plots = Q.run_map_features(conn, run_id=run_id)
            ext = Q.run_extent(conn, run_id=run_id)
            layers = "{}"
            if ext and ext["xmin"] is not None:
                pad = 0.02
                layers = Q.layers_in_box(conn, xmin=ext["xmin"] - pad, ymin=ext["ymin"] - pad, xmax=ext["xmax"] + pad,
                                         ymax=ext["ymax"] + pad, iso2=run["country_iso2"])
        body = '{"plots":' + plots + ',"layers":' + layers + ',"extent":' + json.dumps(ext) + "}"
        return Response(body, mimetype="application/json")

    @app.get("/plots/<int:plot_id>")
    def plot_page(plot_id: int) -> str:
        with db.session() as conn:
            plot = Q.get_plot(conn, plot_id=plot_id) or abort(404)
            run = _run_or_404(conn, plot["run_id"])
            steps = list(Q.trace_for_plot(conn, plot_id=plot_id))
            hits = list(Q.hits_for_plot(conn, plot_id=plot_id))
            datasets = list(Q.dataset_versions(conn))
        pack = get_pack(run["version"])
        steps_by = {s["stage"]: s for s in steps}
        mini = {
            "plot": json.loads(plot["assess_json"] or plot["geom_json"] or "null"),
            "kind": plot["geom_kind"],
            "given": json.loads(plot["geom_json"] or "null"),
            "status": plot["status"],
            "hits": [{"layer": h["layer"], "id": h["ref_id"], "geom": json.loads(h["geom_json"]) if h["geom_json"] else None,
                      "overlap_ha": h["overlap_ha"]} for h in hits if h["layer"] in ("forest_loss", "protected_area")],
        }
        return render_template("plot.html", plot=plot, run=run, steps=steps, s=steps_by, pack=pack, datasets=datasets,
                               mini=mini, plan_line=explain_summary(run["explain_plan"]), layer_sources=LAYERS)

    @app.get("/search")
    def search() -> Response | str:
        code = (request.args.get("q") or "").strip()
        if not code:
            return redirect(url_for("index"))
        with db.session() as conn:
            hit = Q.search_plot(conn, plot_code=code)
        if hit is None:
            return render_template("error.html", title="No plot found", message=f"No plot with code {code!r} in any run."), 404
        return redirect(url_for("plot_page", plot_id=hit["id"]))

    @app.get("/runs/<int:run_a>/diff/<int:run_b>")
    def diff_page(run_a: int, run_b: int) -> str:
        d = engine.diff(run_a, run_b)
        pack_a, pack_b = get_pack(d["a"]["version"]), get_pack(d["b"]["version"])
        return render_template("diff.html", d=d, pack_a=pack_a, pack_b=pack_b)

    @app.get("/runs/<int:run_id>/export/ready.geojson")
    def export_ready(run_id: int) -> Response:
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
            body = Q.ready_geojson(conn, run_id=run_id)
        name = f"{run['file_name'].rsplit('.', 1)[0]}_run{run_id}_ready.geojson"
        return Response(body, mimetype="application/geo+json",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})

    @app.get("/runs/<int:run_id>/fixlist")
    def fixlist_page(run_id: int) -> str:
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
        _csv, rows = engine.fix_list_csv(run_id)
        return render_template("fixlist.html", run=run, rows=rows)

    @app.get("/runs/<int:run_id>/export/fixlist.csv")
    def export_fixlist(run_id: int) -> Response:
        with db.session() as conn:
            run = _run_or_404(conn, run_id)
        text, _rows = engine.fix_list_csv(run_id)
        name = f"{run['file_name'].rsplit('.', 1)[0]}_run{run_id}_fix_list.csv"
        return Response(text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/rules")
    def rules_page() -> str:
        versions = available_versions()
        packs = [get_pack(v) for v in versions]
        changed = changed_params(packs[0], packs[-1]) if len(packs) > 1 else {}
        return render_template("rules.html", packs=packs, changed=changed)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        with db.session() as conn:
            conn.execute("SELECT 1")
        live, why = live_available()
        return {"ok": True, "llm": "live" if live else "replay", "reason": why}

    return app
