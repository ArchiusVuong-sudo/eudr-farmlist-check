"""Opt-in live smoke test: LIVE=1 uv run pytest -m live (uses the local Claude login or ANTHROPIC_API_KEY)."""

import os

import pytest

from farmcheck import SAMPLES_DIR
from farmcheck.ingest import read_farm_list
from farmcheck.mapping.prompt import model_payload
from farmcheck.mapping.providers import LiveProvider, live_available
from farmcheck.profiles import profile_sheet

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to call the model")]


def test_live_proposal_maps_the_vietnamese_layout():
    live_available.cache_clear()
    assert live_available()[0]
    s = read_farm_list("vn_htx_daklak.xlsx", (SAMPLES_DIR / "vn_htx_daklak.xlsx").read_bytes())
    payload = model_payload(s.headers, profile_sheet(s.headers, [c for _n, c in s.rows]), "Vietnam")
    res = LiveProvider().propose(s.headers, payload)
    m = res.proposal.as_mapping()
    assert res.mode == "live"
    assert m["Mã thửa"] == "plot_code" and m["Vĩ độ"] == "latitude" and m["Kinh độ"] == "longitude"
    assert m["Diện tích (ha)"] == "area_ha" and m["Ranh giới thửa (WKT)"] == "boundary_wkt"
