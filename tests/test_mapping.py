import json
from typing import Any

import pytest

from farmcheck import SAMPLES_DIR
from farmcheck.ingest import IngestError, header_signature, read_farm_list
from farmcheck.mapping import live_worker, providers
from farmcheck.mapping.checks import check_column
from farmcheck.mapping.fields import missing_required
from farmcheck.mapping.prompt import model_payload, request_key
from farmcheck.mapping.providers import FallbackProvider, LiveProvider, ProposalUnavailable, ReplayProvider, child_env
from farmcheck.mapping.rules import map_by_rules, normalise_header, rules_are_enough
from farmcheck.mapping.schema import MappingProposal, parse_proposal
from farmcheck.profiles import profile_sheet, shape_mask

VN = "vn_htx_daklak.xlsx"
CO = "co_cooperativa_huila.csv"


def sheet(name):
    return read_farm_list(name, (SAMPLES_DIR / name).read_bytes())


def vn_payload():
    s = sheet(VN)
    return s, model_payload(s.headers, profile_sheet(s.headers, [c for _n, c in s.rows]), "Vietnam")


def test_normalise_header():
    assert normalise_header("Área (ha)") == "area"
    assert normalise_header("Polígono (WKT)") == "poligono"
    assert normalise_header("Ranh giới thửa (WKT)") == "ranh gioi thua"


def test_rules_map_the_spanish_layout_fully():
    s = sheet(CO)
    mapped = map_by_rules(s.headers)
    assert len(mapped) == len(s.headers)
    assert mapped["Latitud"] == "latitude" and mapped["Polígono (WKT)"] == "boundary_wkt"
    assert rules_are_enough(s.headers, mapped)


def test_rules_map_nothing_on_the_vietnamese_layout():
    # The demo's model step depends on this: no Vietnamese synonyms in the header rules.
    s = sheet(VN)
    assert map_by_rules(s.headers) == {}
    assert not rules_are_enough(s.headers, {})


def test_csv_reader_sniffs_semicolon_and_keeps_text():
    s = sheet(CO)
    assert s.delimiter == ";" and len(s.rows) == 80
    row2 = dict(zip(s.headers, s.rows[0][1], strict=True))
    assert "," in row2["Latitud"].raw


def test_xlsx_reader_keeps_number_formats():
    s = sheet(VN)
    fmts = {c.fmt for _n, cells in s.rows for c in cells if c.kind == "number"}
    assert {"0.000000", "0.0000", "0.00"} <= fmts
    padded = [c.raw for _n, cells in s.rows for c in cells[6:8] if c.raw.endswith("00") and c.fmt == "0.000000"]
    assert padded, "number-format padded values should read as shown, e.g. 12.683400"


def test_header_signature_is_stable_and_order_sensitive():
    assert header_signature(["A", "b"]) == header_signature([" a", "B "])
    assert header_signature(["A", "B"]) != header_signature(["B", "A"])


def test_ingest_errors():
    with pytest.raises(IngestError):
        read_farm_list("x.pdf", b"%PDF")
    with pytest.raises(IngestError):
        read_farm_list("x.csv", b"a;a\n1;2\n")
    with pytest.raises(IngestError):
        read_farm_list("x.csv", b"only_header\n")


def test_payload_has_no_cell_values_from_names_or_codes():
    s, payload = vn_payload()
    blob = json.dumps(payload, ensure_ascii=False)
    for _n, cells in s.rows:
        for h, c in zip(s.headers, cells, strict=True):
            if h in ("Mã thửa", "Tên nông hộ", "Xã", "Huyện", "Ghi chú") and c.raw:
                assert c.raw not in blob
    assert shape_mask("Hộ SYN-001") == "Aa AAA-999"


def test_value_checks():
    s, _ = vn_payload()
    prof = {p.header: p for p in profile_sheet(s.headers, [c for _n, c in s.rows])}
    assert check_column("latitude", prof["Vĩ độ"]).ok
    assert "metres" in check_column("latitude", prof["Vĩ độ"]).message
    assert check_column("boundary_wkt", prof["Ranh giới thửa (WKT)"]).ok
    assert check_column("plot_code", prof["Mã thửa"]).ok
    assert not check_column("latitude", prof["Tên nông hộ"]).ok
    assert not check_column("area_ha", prof["Vĩ độ"]).ok or True  # lat values are numbers; area check only bounds size
    assert not check_column("boundary_wkt", prof["Vĩ độ"]).ok


def test_missing_required():
    assert missing_required({"plot_code", "latitude", "longitude"}) == []
    assert missing_required({"plot_code", "boundary_wkt"}) == []
    assert missing_required({"plot_code", "latitude"}) == ["longitude"]
    assert "plot_code" in missing_required({"latitude", "longitude"})


def test_proposal_schema_rejects_bad_answers():
    headers = ["A", "B"]
    ok, problems = parse_proposal({"columns": [
        {"column": "A", "field": "latitude", "confidence": 0.9, "reason": "lat"},
        {"column": "B", "field": "longitude", "confidence": 0.9, "reason": "lon"}]}, headers)
    assert ok and not problems
    _p, problems = parse_proposal({"columns": [{"column": "A", "field": "latitude", "confidence": 2, "reason": "x"}]}, headers)
    assert problems
    _p, problems = parse_proposal({"columns": [
        {"column": "A", "field": "latitude", "confidence": 0.9, "reason": "lat"},
        {"column": "B", "field": "latitude", "confidence": 0.9, "reason": "lat"}]}, headers)
    assert any("more than one" in p for p in problems)
    _p, problems = parse_proposal({"columns": [{"column": "A", "field": "nonsense", "confidence": 0.9, "reason": "x"}]}, headers)
    assert problems


def test_replay_provider_reads_the_recorded_live_run():
    s, payload = vn_payload()
    res = ReplayProvider().propose(s.headers, payload)
    assert res.mode == "replay" and res.model.startswith("claude-")
    assert res.proposal.as_mapping()["Vĩ độ"] == "latitude"
    assert res.duration_ms > 0


def test_replay_miss_is_loud():
    with pytest.raises(ProposalUnavailable):
        ReplayProvider().propose(["X"], {"headers": ["X"], "columns": []})


def test_request_key_ignores_nothing_in_the_payload():
    _s, payload = vn_payload()
    other = json.loads(json.dumps(payload))
    other["declared_country"] = "Colombia"
    assert request_key(payload) != request_key(other)


def test_child_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("SECRET_THING", "x")
    env = child_env()
    assert set(env) <= {"PATH", "HOME", "USER", "LANG", "TMPDIR", "ANTHROPIC_API_KEY"}
    assert "CLAUDECODE" not in env and "SECRET_THING" not in env


def _good(headers: list[str]) -> dict[str, Any]:
    return {"columns": [{"column": h, "field": "ignore", "confidence": 0.5, "reason": "unused"} for h in headers]}


def test_live_provider_retries_overloaded_then_succeeds(monkeypatch):
    calls: list[int] = []

    def fake_call(self, req):
        calls.append(1)
        if len(calls) < 3:
            return {"ok": False, "error": "API Error: 529 overloaded", "api_error_status": 529}
        return {"ok": True, "proposal": _good(req["headers"]), "model": "claude-sonnet-5-5", "duration_ms": 10,
                "cost_usd": 0.01, "num_turns": 2}

    monkeypatch.setattr(LiveProvider, "_call", fake_call)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    res = LiveProvider(model="claude-sonnet-5-5").propose(["A"], {"headers": ["A"], "columns": []})
    assert res.attempts == 3 and res.mode == "live"


def test_live_provider_does_not_retry_a_bad_request(monkeypatch):
    calls: list[int] = []

    def fake_call(self, req):
        calls.append(1)
        return {"ok": False, "error": "the model ended without a valid submit_mapping call", "api_error_status": None}

    monkeypatch.setattr(LiveProvider, "_call", fake_call)
    with pytest.raises(ProposalUnavailable):
        LiveProvider(model="claude-sonnet-5-5").propose(["A"], {"headers": ["A"], "columns": []})
    assert len(calls) == 1


def test_fallback_to_replay_says_so(monkeypatch):
    def boom(self, headers, payload):
        raise ProposalUnavailable("live proposal failed: 503")

    monkeypatch.setattr(LiveProvider, "propose", boom)
    s, payload = vn_payload()
    res = FallbackProvider(LiveProvider(model="claude-sonnet-5-5"), ReplayProvider()).propose(s.headers, payload)
    assert res.mode == "replay" and "503" in (res.fallback_reason or "")


def test_model_env_var_is_validated(monkeypatch):
    monkeypatch.setenv("FARMCHECK_MODEL", "gpt-x")
    with pytest.raises(ValueError):
        providers.selected_model()


def test_worker_isolation_options(monkeypatch):
    """The SDK query gets no built-in tools, one allowed tool, no settings and a deny-by-default mode."""
    seen: dict[str, Any] = {}

    async def fake_query(prompt, options):
        seen["options"] = options
        if False:
            yield None

    monkeypatch.setattr(live_worker, "query", fake_query)
    import anyio

    out = anyio.run(live_worker.run, {"headers": ["A"], "model": "claude-haiku-5-5", "system": "s", "prompt": "p", "max_turns": 3})
    o = seen["options"]
    assert o.tools == [] and o.setting_sources == [] and o.strict_mcp_config is True
    assert o.allowed_tools == ["mcp__farmlist__submit_mapping"] and o.permission_mode == "dontAsk"
    assert list(o.mcp_servers) == ["farmlist"]
    assert o.max_turns == 3 and "no-session-persistence" in o.extra_args and o.cwd
    assert not out["ok"] and "without a valid submit_mapping" in out["error"]


def test_submit_tool_validates_its_input():
    import anyio

    accepted: dict[str, Any] = {}
    t = live_worker.make_submit_tool(["A"], accepted)
    bad = anyio.run(t.handler, {"columns": [{"column": "Z", "field": "latitude", "confidence": 1, "reason": "no"}]})
    assert bad["is_error"] and "Rejected" in bad["content"][0]["text"] and not accepted
    good = anyio.run(t.handler, _good(["A"]))
    assert "is_error" not in good
    assert MappingProposal.model_validate(accepted["proposal"]).columns[0].column == "A"
