"""PostGIS behaviour the checks rely on, on the session's PGlite server."""


def _cands(conn, lat, lon, iso2):
    return list(conn.execute("SELECT kind, inside FROM coord_candidates(%s, %s, %s) ORDER BY ord", (lat, lon, iso2)))


def test_reference_layers_are_loaded(conn, engine):
    n = conn.execute("SELECT (SELECT count(*) FROM forest_loss) AS loss, (SELECT count(*) FROM country) AS c, "
                     "(SELECT count(*) FROM protected_area) AS pa").fetchone()
    assert n == {"loss": 3000, "c": 11, "pa": 2}


def test_swapped_point_has_exactly_one_fit(conn, engine):
    rows = _cands(conn, 108.4, 12.9, "VN")
    assert [r["kind"] for r in rows if r["inside"]] == ["swapped"]


def test_sign_dropped_point_has_exactly_one_fit(conn, engine):
    rows = _cands(conn, 2.2, 75.8, "CO")
    assert [r["kind"] for r in rows if r["inside"]] == ["lon_sign"]


def test_utm_metres_fit_only_zone_49(conn, engine):
    e_n = conn.execute("SELECT ST_X(g) AS e, ST_Y(g) AS n FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(108.4, 12.9), 4326), 32649) g) s").fetchone()
    rows = _cands(conn, e_n["n"], e_n["e"], "VN")
    assert [r["kind"] for r in rows if r["inside"]] == ["utm_32649_e_n"]
    assert len(rows) == 4


def test_point_in_cambodia_has_no_fit(conn, engine):
    rows = _cands(conn, 13.05, 106.55, "VN")
    assert rows and not any(r["inside"] for r in rows)


def test_geodesic_area_function(conn, engine):
    ha = conn.execute("SELECT ha(ST_Transform(ST_MakeEnvelope(200000, 1400000, 201000, 1401000, 32649), 4326)) AS ha").fetchone()["ha"]
    assert abs(ha - 100) < 0.5


def test_make_valid_splits_a_bowtie(conn, engine):
    r = conn.execute(
        "SELECT ST_IsValid(g) AS ok, ST_NumGeometries(ST_MakeValid(g)) AS n FROM "
        "(SELECT ST_GeomFromText('POLYGON((-75.60 2.40,-75.59 2.40,-75.60 2.41,-75.59 2.41,-75.60 2.40))', 4326) g) s").fetchone()
    assert r == {"ok": False, "n": 2}
