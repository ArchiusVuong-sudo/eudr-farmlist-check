"""The main flow through the web app in replay mode: upload, mapping approval, run, trace, v2 diff, exports."""

import csv
import io
import json
import re

import pytest

from farmcheck import SAMPLES_DIR
from farmcheck.mapping.providers import ProposalUnavailable, ReplayProvider
from farmcheck.queries import Q
from farmcheck.services import load_truth


class NoCallProvider:
    mode = "replay"

    def propose(self, headers, payload):
        raise AssertionError("the model must not be called for this layout")


def _upload(client, name, country):
    data = {"file": (io.BytesIO((SAMPLES_DIR / name).read_bytes()), name), "country": country}
    res = client.post("/lists", data=data, content_type="multipart/form-data")
    assert res.status_code == 303
    return int(re.search(r"/lists/(\d+)/mapping", res.headers["Location"]).group(1))


def _confirm(client, engine, fl_id, version="v1"):
    with engine.db.session() as conn:
        fl = Q.get_farm_list(conn, farm_list_id=fl_id)
    form = {f"field_{i}": c["field"] for i, c in enumerate(fl["proposal"]["columns"])}
    form["version"] = version
    res = client.post(f"/lists/{fl_id}/confirm", data=form)
    assert res.status_code == 303, res.get_data(as_text=True)[:400]
    return int(res.headers["Location"].rsplit("/", 1)[1])


@pytest.fixture()
def fresh_profiles(engine):
    with engine.db.transaction() as conn:
        conn.execute("UPDATE farm_list SET mapping_profile_id = NULL")
        conn.execute("DELETE FROM mapping_profile")


@pytest.mark.parametrize(("name", "country", "source"), [("vn_htx_daklak.xlsx", "VN", "model"), ("co_cooperativa_huila.csv", "CO", "rules")])
def test_main_flow_end_to_end(client, engine, fresh_profiles, name, country, source):
    fl_id = _upload(client, name, country)
    page = client.get(f"/lists/{fl_id}/mapping").get_data(as_text=True)
    assert "Proposing a column mapping" in page and "Synthetic data" in page

    prop = client.get(f"/lists/{fl_id}/proposal").get_data(as_text=True)
    assert "Confirm mapping and run" in prop and "check fail" not in prop
    if source == "model":
        assert "replayed, claude-" in prop and "What the model saw" in prop
    else:
        assert "Mapped by header rules, no model call" in prop

    run1 = _confirm(client, engine, fl_id)
    page = client.get(f"/runs/{run1}").get_data(as_text=True)
    truth = load_truth(name)
    assert f"{truth['v1_problems']}/{truth['v1_problems']}" in page

    # every injected problem found, no unexpected flag, the expected status on every row
    for run_id, version in ((run1, "v1"),):
        score = engine.score(run_id)
        assert score["perfect"], score

    # the assessment join uses the GiST index on the seeded loss layer
    with engine.db.session() as conn:
        run = Q.get_run(conn, run_id=run1)
        plots = list(Q.plots_for_run(conn, run_id=run1))
        circles = [dict(r) for r in conn.execute(
            "SELECT declared_area_ha, ha(assess_geom) AS got FROM plot WHERE run_id = %s AND assess_basis = 'circle_declared'", (run1,))]
    assert "forest_loss_geom_gix" in run["explain_plan"]
    assert "Index Scan using forest_loss_geom_gix" in page or "Bitmap Index Scan on forest_loss_geom_gix" in page
    # point plots are assessed as equal-area geodesic circles
    assert circles and all(abs(c["got"] - c["declared_area_ha"]) / c["declared_area_ha"] < 0.01 for c in circles)

    # re-run under v2: the diff lists exactly the plots the answer key predicts
    res = client.post(f"/lists/{fl_id}/runs", data={"version": "v2", "base_run": run1})
    assert res.status_code == 303 and "/diff/" in res.headers["Location"]
    run2 = int(res.headers["Location"].rsplit("/", 1)[1])
    assert engine.score(run2)["perfect"]
    diff = engine.diff(run1, run2)
    assert sorted(c["plot_code"] for c in diff["changes"]) == sorted(truth["changed_v1_to_v2"])
    assert all(c["causes"] for c in diff["changes"])
    html = client.get(res.headers["Location"]).get_data(as_text=True)
    assert "exactly the" in html and "simulated update for this demo, not an EU text" in html

    # re-running v1 gives the same result hash
    res = client.post(f"/lists/{fl_id}/runs", data={"version": "v1", "base_run": run1})
    run3 = int(res.headers["Location"].rsplit("/", 1)[1])
    with engine.db.session() as conn:
        h1, h3 = Q.get_run(conn, run_id=run1)["result_hash"], Q.get_run(conn, run_id=run3)["result_hash"]
    assert h1 == h3
    assert f"same as run #{run1}" in client.get(f"/runs/{run3}").get_data(as_text=True)

    # GeoJSON of ready plots: only Ready plots, WGS 84 at 6 decimals, valid in PostGIS
    gj = client.get(f"/runs/{run1}/export/ready.geojson").get_json()
    ready = [p for p in plots if p["status"] == "ready"]
    assert len(gj["features"]) == len(ready)
    assert {f["properties"]["plot_code"] for f in gj["features"]} == {p["plot_code"] for p in ready}
    with engine.db.session() as conn:
        for f in gj["features"]:
            assert all(len(str(n).split(".")[-1]) <= 6 for n in re.findall(r"-?\d+\.\d+", json.dumps(f["geometry"])))
            assert conn.execute("SELECT ST_IsValid(ST_GeomFromGeoJSON(%s)) AS ok", (json.dumps(f["geometry"]),)).fetchone()["ok"]

    # the supplier fix list has every needs-fix row and a confirm line for each repaired row
    text = client.get(f"/runs/{run1}/export/fixlist.csv").get_data(as_text=True)
    lines = list(csv.DictReader(io.StringIO(text)))
    needs = {p["plot_code"] for p in plots if p["status"] == "needs_fix"}
    repaired = {p["plot_code"] for p in plots if p["status"] == "repaired"}
    assert needs <= {r["plot_code"] for r in lines}
    assert repaired <= {r["plot_code"] for r in lines if r["severity"] == "repaired" and r["fix_hint"].startswith("Confirm")}
    assert "Fix list for the supplier" in client.get(f"/runs/{run1}/fixlist").get_data(as_text=True)


def test_investigation_page_for_the_support_case(client, engine):
    fl_id = engine.upload_sample("vn_htx_daklak.xlsx")
    engine.propose(fl_id, ReplayProvider())
    with engine.db.session() as conn:
        mapping = {c["column"]: c["field"] for c in Q.get_farm_list(conn, farm_list_id=fl_id)["proposal"]["columns"]}
    engine.confirm_and_run(fl_id, mapping, "v1")
    res = client.get("/search?q=dl-0137")
    assert res.status_code == 302
    page = client.get(res.headers["Location"]).get_data(as_text=True)
    for needle in ("Row received", "1420690.95", "UTM zone 49N metres (EPSG:32649), easting, northing", "chosen",
                   "Forest loss after the cut-off", "Art. 2(13)", "LP-VN-", "Next step", "plot boundary polygon",
                   "confirm the corrected coordinates", "forest_loss_geom_gix", "Synthetic data"):
        assert needle in page, needle
    assert client.get("/search?q=NOPE-1").status_code == 404


def test_saved_profile_is_reused_without_a_model_call(app, engine):
    a = engine.upload_sample("vn_htx_daklak.xlsx")
    engine.propose(a, ReplayProvider())
    with engine.db.session() as conn:
        mapping = {c["column"]: c["field"] for c in Q.get_farm_list(conn, farm_list_id=a)["proposal"]["columns"]}
    engine.confirm_and_run(a, mapping, "v1")
    b = engine.upload_sample("vn_htx_daklak.xlsx")
    prop = engine.propose(b, NoCallProvider())
    assert prop["source"] == "profile" and all(c["field"] == mapping[c["column"]] for c in prop["columns"])


def test_header_rules_layout_never_calls_the_model(engine):
    fl = engine.upload_sample("co_cooperativa_huila.csv")
    assert engine.propose(fl, NoCallProvider())["source"] in ("rules", "profile")


def test_no_recording_means_map_by_hand(engine):
    class Missing:
        mode = "replay"

        def propose(self, headers, payload):
            raise ProposalUnavailable("no recorded proposal for this layout")

    data = "Ma lo;Vido;Kinhdo\nA-1;2,123457;-75,123457\n".encode()
    fl = engine.upload("unknown.csv", data, "CO")
    prop = engine.propose(fl, Missing())
    assert prop["error"] and all(c["field"] == "ignore" for c in prop["columns"])


def test_errors_are_pages_not_crashes(client, engine):
    assert client.post("/lists", data={"country": "VN"}).status_code == 400
    assert client.get("/runs/999999").status_code == 404
    bad = client.post("/lists", data={"file": (io.BytesIO(b"%PDF"), "a.pdf"), "country": "VN"}, content_type="multipart/form-data")
    assert bad.status_code == 400 and "upload a .csv or .xlsx file" in bad.get_data(as_text=True)
    fl = engine.upload_sample("co_cooperativa_huila.csv")
    engine.propose(fl, ReplayProvider())
    res = client.post(f"/lists/{fl}/confirm", data={"version": "v1"})  # every field 'ignore'
    assert res.status_code == 400 and "plot_code" in res.get_data(as_text=True)


def test_pages_render(client):
    for url in ("/", "/rules", "/healthz"):
        assert client.get(url).status_code == 200
    assert "Simulated update" in client.get("/rules").get_data(as_text=True)
