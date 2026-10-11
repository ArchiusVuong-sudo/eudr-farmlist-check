"""Record the mapping proposals replay mode uses, from real live runs on the Claude Agent SDK.

Needs a Claude Code login or ANTHROPIC_API_KEY. Writes fixtures/mapping/<key>.json for every bundled
sample whose layout the header rules cannot map (the Colombian CSV maps by rules, so it needs none).

Usage: uv run python scripts/record_fixtures.py [--model claude-sonnet-5-5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from farmcheck import REFERENCE_DIR, SAMPLES_DIR  # noqa: E402
from farmcheck.ingest import read_farm_list  # noqa: E402
from farmcheck.mapping.prompt import model_payload  # noqa: E402
from farmcheck.mapping.providers import LiveProvider, live_available, save_fixture  # noqa: E402
from farmcheck.mapping.rules import map_by_rules, rules_are_enough  # noqa: E402
from farmcheck.mapping.schema import validate_against_headers  # noqa: E402
from farmcheck.profiles import profile_sheet  # noqa: E402

SAMPLES = {"vn_htx_daklak.xlsx": "VN", "co_cooperativa_huila.csv": "CO"}


def country_names() -> dict[str, str]:
    data = json.loads((REFERENCE_DIR / "countries.geojson").read_text(encoding="utf-8"))
    return {f["properties"]["iso2"]: f["properties"]["name"] for f in data["features"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("FARMCHECK_MODEL", "claude-sonnet-5-5"))
    args = ap.parse_args()
    ok, why = live_available()
    if not ok:
        raise SystemExit(f"live mode is not available: {why}")
    names = country_names()
    provider = LiveProvider(model=args.model)
    for name, iso2 in SAMPLES.items():
        sheet = read_farm_list(name, (SAMPLES_DIR / name).read_bytes())
        if rules_are_enough(sheet.headers, map_by_rules(sheet.headers)):
            print(f"{name}: header rules map it, no fixture needed")
            continue
        payload = model_payload(sheet.headers, profile_sheet(sheet.headers, [c for _n, c in sheet.rows]), names[iso2])
        result = provider.propose(sheet.headers, payload)
        assert not validate_against_headers(result.proposal, sheet.headers)
        path = save_fixture(result, payload)
        print(f"{name}: {result.model}, {result.duration_ms} ms, {result.num_turns} turns, "
              f"cost {result.cost_usd}, attempts {result.attempts} -> {path.name}")
        for c in result.proposal.columns:
            print(f"   {c.column:24} -> {c.field:13} {c.confidence:.2f}  {c.reason}")


if __name__ == "__main__":
    main()
