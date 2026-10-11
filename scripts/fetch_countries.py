"""Fetch the Natural Earth 50m admin-0 outlines once and keep the countries the samples need.

Natural Earth is public domain. Output: data/reference/countries.geojson
Usage: uv run python scripts/fetch_countries.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

SOURCE = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_50m_admin_0_countries.geojson"
)
KEEP = ["CO", "EC", "PE", "BR", "VE", "PA", "VN", "KH", "LA", "TH", "CN"]
OUT = Path(__file__).resolve().parent.parent / "data" / "reference" / "countries.geojson"


def main() -> None:
    with urllib.request.urlopen(SOURCE, timeout=60) as resp:  # noqa: S310 - fixed public URL
        full = json.load(resp)
    features = []
    for feat in full["features"]:
        props = feat["properties"]
        iso2 = props.get("ISO_A2")
        if iso2 in KEEP:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"iso2": iso2, "name": props["NAME"]},
                    "geometry": feat["geometry"],
                }
            )
    features.sort(key=lambda f: KEEP.index(f["properties"]["iso2"]))
    out = {
        "type": "FeatureCollection",
        "name": "countries",
        "source": "Natural Earth 50m admin-0 countries (public domain), " + SOURCE,
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {len(features)} countries to {OUT}")


if __name__ == "__main__":
    main()
