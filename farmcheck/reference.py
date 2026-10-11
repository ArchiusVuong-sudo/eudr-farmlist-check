"""Load the reference layers (country outlines and the synthetic forest layers) into PostGIS once."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import psycopg

from . import REFERENCE_DIR
from .queries import Q, returned_id
from .rulepack import available_versions, get_pack

log = logging.getLogger(__name__)

# UTM zones each declared country spans, used to read coordinates given in metres.
UTM_SRIDS = {"VN": [32648, 32649], "CO": [32617, 32618, 32619]}
COUNTRY_TOLERANCE_M = 100.0  # Natural Earth 50m borders are coarse

LAYERS = {
    "countries": ("countries.geojson", "Natural Earth 50m admin-0 (public domain)"),
    "forest_2020": ("forest_2020.geojson", "Synthetic forest cover on 31 Dec 2020 (made by scripts/make_synthetic.py)"),
    "forest_loss": ("forest_loss.geojson", "Synthetic forest loss after the cut-off: a stand-in for a detection model's output, not a model"),
    "protected_areas": ("protected_areas.geojson", "Synthetic protected areas"),
}


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def dataset_checksum(base: Path = REFERENCE_DIR) -> str:
    h = hashlib.sha256()
    for name in sorted(LAYERS):
        h.update(_checksum(base / LAYERS[name][0]).encode())
    return h.hexdigest()[:12]


def _features(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def ensure_reference(conn: psycopg.Connection, base: Path = REFERENCE_DIR) -> bool:
    """(Re)load the layers when their files changed; returns True when it loaded anything."""
    current = {name: _checksum(base / fname) for name, (fname, _src) in LAYERS.items()}
    stored = {name: Q.dataset_checksum(conn, layer=name) for name in LAYERS}
    if current == stored:
        return False
    log.info("loading reference layers")
    with conn.transaction():
        Q.clear_reference(conn)
        for feat in _features(base / LAYERS["countries"][0]):
            props = feat["properties"]
            Q.upsert_country(
                conn,
                iso2=props["iso2"],
                name=props["name"],
                utm_srids=UTM_SRIDS.get(props["iso2"], []),
                geojson=json.dumps(feat["geometry"]),
                tol_m=COUNTRY_TOLERANCE_M,
            )
        Q.insert_forest_2020(
            conn,
            [{"id": f["properties"]["id"], "region": f["properties"]["region"], "geojson": json.dumps(f["geometry"])}
             for f in _features(base / LAYERS["forest_2020"][0])],
        )
        Q.insert_forest_loss(
            conn,
            [{"id": f["properties"]["id"], "region": f["properties"]["region"], "detected_on": f["properties"]["detected_on"],
              "confidence": f["properties"]["confidence"], "geojson": json.dumps(f["geometry"])}
             for f in _features(base / LAYERS["forest_loss"][0])],
        )
        Q.insert_protected_area(
            conn,
            [{"id": f["properties"]["id"], "name": f["properties"]["name"], "region": f["properties"]["region"],
              "geojson": json.dumps(f["geometry"])}
             for f in _features(base / LAYERS["protected_areas"][0])],
        )
        for name, (fname, src) in LAYERS.items():
            n = len(_features(base / fname))
            Q.upsert_dataset_version(conn, layer=name, version="1", checksum=current[name], features=n, source=src)
    Q.analyze_reference(conn)
    return True


def ensure_rule_packs(conn: psycopg.Connection) -> dict[str, int]:
    ids: dict[str, int] = {}
    for version in available_versions():
        pack = get_pack(version)
        ids[version] = returned_id(Q.upsert_rule_pack(
            conn, version=pack.version, checksum=pack.checksum, title=pack.title,
            body=json.dumps(pack.model_dump(mode="json")),
        ))
    return ids


def init_database(conn: psycopg.Connection) -> None:
    Q.create_schema(conn)
    ensure_reference(conn)
    ensure_rule_packs(conn)
