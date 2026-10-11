-- name: create_schema#
-- Tables, indexes and the two database functions. Safe to run on every start.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS dataset_version (
  id          bigserial PRIMARY KEY,
  layer       text NOT NULL UNIQUE,
  version     text NOT NULL,
  checksum    text NOT NULL,
  features    integer NOT NULL,
  source      text NOT NULL,
  loaded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS country (
  iso2       text PRIMARY KEY,
  name       text NOT NULL,
  utm_srids  integer[] NOT NULL DEFAULT '{}',
  geom       geometry(MultiPolygon, 4326) NOT NULL,
  geom_tol   geometry(MultiPolygon, 4326) NOT NULL  -- outline grown by the containment tolerance
);
CREATE INDEX IF NOT EXISTS country_geom_gix ON country USING gist (geom);
CREATE INDEX IF NOT EXISTS country_geom_tol_gix ON country USING gist (geom_tol);

CREATE TABLE IF NOT EXISTS forest_2020 (
  id      text PRIMARY KEY,
  region  text NOT NULL,
  geom    geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS forest_2020_geom_gix ON forest_2020 USING gist (geom);

CREATE TABLE IF NOT EXISTS forest_loss (
  id           text PRIMARY KEY,
  region       text NOT NULL,
  detected_on  date NOT NULL,
  confidence   real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  geom         geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS forest_loss_geom_gix ON forest_loss USING gist (geom);

CREATE TABLE IF NOT EXISTS protected_area (
  id      text PRIMARY KEY,
  name    text NOT NULL,
  region  text NOT NULL,
  geom    geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS protected_area_geom_gix ON protected_area USING gist (geom);

CREATE TABLE IF NOT EXISTS rule_pack (
  id         bigserial PRIMARY KEY,
  version    text NOT NULL,
  checksum   text NOT NULL,
  title      text NOT NULL,
  body       jsonb NOT NULL,
  loaded_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (version, checksum)
);

CREATE TABLE IF NOT EXISTS mapping_profile (
  id          bigserial PRIMARY KEY,
  signature   text NOT NULL UNIQUE,      -- hash of the normalised header row
  headers     jsonb NOT NULL,
  mapping     jsonb NOT NULL,            -- {"<header>": "<field>"}
  source      text NOT NULL,             -- rules | model | person
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS farm_list (
  id                  bigserial PRIMARY KEY,
  file_name           text NOT NULL,
  file_kind           text NOT NULL CHECK (file_kind IN ('csv', 'xlsx')),
  country_iso2        text NOT NULL REFERENCES country (iso2),
  header_signature    text NOT NULL,
  headers             jsonb NOT NULL,
  profiles            jsonb NOT NULL,
  proposal            jsonb,                -- mapping proposal shown to the person
  mapping             jsonb,                -- mapping the person confirmed
  mapping_profile_id  bigint REFERENCES mapping_profile (id),
  uploaded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_row (
  id            bigserial PRIMARY KEY,
  farm_list_id  bigint NOT NULL REFERENCES farm_list (id) ON DELETE CASCADE,
  row_number    integer NOT NULL,           -- spreadsheet row, header is row 1
  cells         jsonb NOT NULL,             -- [{"raw": text, "fmt": number format, "kind": text|number|empty}]
  UNIQUE (farm_list_id, row_number)
);

CREATE TABLE IF NOT EXISTS run (
  id               bigserial PRIMARY KEY,
  farm_list_id     bigint NOT NULL REFERENCES farm_list (id) ON DELETE CASCADE,
  rule_pack_id     bigint NOT NULL REFERENCES rule_pack (id),
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  stage_ms         jsonb NOT NULL DEFAULT '{}',
  counts           jsonb NOT NULL DEFAULT '{}',
  result_hash      text,
  dataset_checksum text,
  assess_ms        double precision,
  explain_plan     text
);

CREATE TABLE IF NOT EXISTS plot (
  id                bigserial PRIMARY KEY,
  run_id            bigint NOT NULL REFERENCES run (id) ON DELETE CASCADE,
  raw_row_id        bigint NOT NULL REFERENCES raw_row (id),
  row_number        integer NOT NULL,
  plot_code         text NOT NULL,
  producer          text,
  admin_area        text,
  declared_area_ha  double precision,
  lat_in            double precision,       -- numbers as given (may be metres)
  lon_in            double precision,
  coord_format      text,                   -- decimal | dms | metres | wkt
  coord_decimals    integer,                -- fewest decimals shown on any coordinate used
  coord_sig_decimals integer,               -- same, after dropping trailing zeros
  wkt_in            text,                   -- boundary as text, ring closed by the parser
  ring_autoclosed   boolean NOT NULL DEFAULT false,
  geom_kind         text CHECK (geom_kind IN ('point', 'polygon')),
  given_geom        geometry(Geometry, 4326),
  geom              geometry(Geometry, 4326),   -- after repairs
  repair            jsonb,                      -- coordinate candidates tried
  validity_reason   text,
  area_given_ha     double precision,
  area_ha           double precision,           -- geodesic area of the boundary
  assessable        boolean NOT NULL DEFAULT false,
  assess_geom       geometry(Geometry, 4326),
  assess_basis      text,                       -- polygon | circle_declared | circle_assumed
  circle_radius_m   double precision,
  status            text,                       -- ready | repaired | review | needs_fix
  parse             jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS plot_run_idx ON plot (run_id);
CREATE INDEX IF NOT EXISTS plot_geom_gix ON plot USING gist (geom);
CREATE INDEX IF NOT EXISTS plot_assess_geom_gix ON plot USING gist (assess_geom);

CREATE TABLE IF NOT EXISTS plot_issue (
  id        bigserial PRIMARY KEY,
  run_id    bigint NOT NULL REFERENCES run (id) ON DELETE CASCADE,
  plot_id   bigint NOT NULL REFERENCES plot (id) ON DELETE CASCADE,
  code      text NOT NULL,
  severity  text NOT NULL CHECK (severity IN ('error', 'review', 'repaired', 'warning')),
  rule_key  text NOT NULL,
  message   text NOT NULL,
  detail    jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS plot_issue_run_idx ON plot_issue (run_id, plot_id);

CREATE TABLE IF NOT EXISTS overlap_hit (
  id          bigserial PRIMARY KEY,
  run_id      bigint NOT NULL REFERENCES run (id) ON DELETE CASCADE,
  plot_id     bigint NOT NULL REFERENCES plot (id) ON DELETE CASCADE,
  layer       text NOT NULL CHECK (layer IN ('forest_loss', 'protected_area', 'forest_2020', 'plot')),
  ref_id      text NOT NULL,
  overlap_ha  double precision NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS overlap_hit_run_idx ON overlap_hit (run_id, plot_id);

CREATE TABLE IF NOT EXISTS trace_step (
  id       bigserial PRIMARY KEY,
  run_id   bigint NOT NULL REFERENCES run (id) ON DELETE CASCADE,
  plot_id  bigint NOT NULL REFERENCES plot (id) ON DELETE CASCADE,
  seq      integer NOT NULL,
  stage    text NOT NULL,
  title    text NOT NULL,
  detail   jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS trace_step_plot_idx ON trace_step (plot_id, seq);

-- Geodesic area in hectares.
CREATE OR REPLACE FUNCTION ha(g geometry) RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT ST_Area(g::geography) / 10000.0
$$;

-- Candidate readings of a coordinate pair, each tested against the declared country.
-- Degrees: as given, swapped, and with dropped minus signs. Metres: each UTM zone of the
-- country, in both axis orders. The caller repairs only when exactly one candidate fits.
CREATE OR REPLACE FUNCTION coord_candidates(p_lat double precision, p_lon double precision, p_iso2 text)
RETURNS TABLE (ord integer, kind text, lat double precision, lon double precision, inside boolean)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  c_geom  geometry;
  c_srids integer[];
  z       integer;
  pt      geometry;
  i       integer := 0;
  pair    record;
BEGIN
  SELECT geom_tol, utm_srids INTO c_geom, c_srids FROM country WHERE iso2 = p_iso2;
  IF c_geom IS NULL OR p_lat IS NULL OR p_lon IS NULL THEN
    RETURN;
  END IF;

  IF abs(p_lat) > 180 OR abs(p_lon) > 180 THEN
    FOREACH z IN ARRAY c_srids LOOP
      FOR pair IN SELECT * FROM (VALUES ('e_n', p_lon, p_lat), ('n_e', p_lat, p_lon)) AS v(axis, e, n) LOOP
        i := i + 1;
        BEGIN
          pt := ST_Transform(ST_SetSRID(ST_MakePoint(pair.e, pair.n), z), 4326);
          ord := i; kind := 'utm_' || z || '_' || pair.axis;
          lat := ST_Y(pt); lon := ST_X(pt);
          inside := abs(lat) <= 90 AND abs(lon) <= 180 AND ST_Covers(c_geom, pt);
          RETURN NEXT;
        EXCEPTION WHEN others THEN
          CONTINUE;
        END;
      END LOOP;
    END LOOP;
    RETURN;
  END IF;

  FOR pair IN
    SELECT * FROM (VALUES
      ('as_given',          p_lat,  p_lon),
      ('swapped',           p_lon,  p_lat),
      ('lat_sign',         -p_lat,  p_lon),
      ('lon_sign',          p_lat, -p_lon),
      ('both_signs',       -p_lat, -p_lon),
      ('swapped_lat_sign', -p_lon,  p_lat),
      ('swapped_lon_sign',  p_lon, -p_lat),
      ('swapped_both_signs', -p_lon, -p_lat)
    ) AS v(k, la, lo)
  LOOP
    i := i + 1;
    IF abs(pair.la) <= 90 AND abs(pair.lo) <= 180 THEN
      ord := i; kind := pair.k; lat := pair.la; lon := pair.lo;
      inside := ST_Covers(c_geom, ST_SetSRID(ST_MakePoint(pair.lo, pair.la), 4326));
      RETURN NEXT;
    END IF;
  END LOOP;
END
$$;
