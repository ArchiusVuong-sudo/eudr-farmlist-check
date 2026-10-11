-- Set-based validation, repair and assessment of one run. Every statement works on all plots of
-- the run at once; thresholds, codes and severities come from the rule pack as parameters.

-- name: build_geometry!
UPDATE plot
SET geom_kind = CASE WHEN wkt_in IS NOT NULL THEN 'polygon' WHEN lat_in IS NOT NULL THEN 'point' END,
    given_geom = CASE
      WHEN wkt_in IS NOT NULL THEN ST_GeomFromText(wkt_in, 4326)
      WHEN abs(lat_in) <= 90 AND abs(lon_in) <= 180 THEN ST_SetSRID(ST_MakePoint(lon_in, lat_in), 4326)
    END
WHERE run_id = :run_id;

-- name: repair_coordinates!
-- Points: keep the point when it lies in the declared country; otherwise accept a fix only when
-- exactly one candidate reading (swapped, sign dropped, UTM metres) lands inside it.
WITH cand AS (
  SELECT p.id,
         jsonb_agg(jsonb_build_object('kind', c.kind, 'lat', round(c.lat::numeric, 6), 'lon', round(c.lon::numeric, 6),
                                      'inside', c.inside) ORDER BY c.ord) AS candidates,
         coalesce(bool_or(c.kind = 'as_given' AND c.inside), false) AS as_given_ok,
         count(*) FILTER (WHERE c.kind <> 'as_given' AND c.inside) AS n_fit,
         (array_agg(c.kind ORDER BY c.ord) FILTER (WHERE c.kind <> 'as_given' AND c.inside))[1] AS fit_kind,
         (array_agg(c.lat ORDER BY c.ord) FILTER (WHERE c.kind <> 'as_given' AND c.inside))[1] AS fit_lat,
         (array_agg(c.lon ORDER BY c.ord) FILTER (WHERE c.kind <> 'as_given' AND c.inside))[1] AS fit_lon
  FROM plot p
  CROSS JOIN LATERAL coord_candidates(p.lat_in, p.lon_in, CAST(:iso2 AS text)) c
  WHERE p.run_id = :run_id AND p.geom_kind = 'point'
  GROUP BY p.id
)
UPDATE plot p
SET repair = jsonb_build_object('candidates', cand.candidates, 'as_given_ok', cand.as_given_ok,
                                'n_fit', cand.n_fit, 'fit_kind', cand.fit_kind),
    geom = CASE
      WHEN cand.as_given_ok THEN p.given_geom
      WHEN cand.n_fit = 1 THEN ST_SetSRID(ST_MakePoint(cand.fit_lon, cand.fit_lat), 4326)
    END
FROM cand
WHERE p.id = cand.id;

-- name: repair_polygons!
-- Boundaries: ST_MakeValid for self-intersections, keeping the area before and after.
UPDATE plot
SET validity_reason = CASE WHEN NOT ST_IsValid(given_geom) THEN ST_IsValidReason(given_geom) END,
    area_given_ha = ha(given_geom),
    geom = CASE WHEN ST_IsValid(given_geom) THEN given_geom
                ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(given_geom), 3)) END
WHERE run_id = :run_id AND geom_kind = 'polygon';

-- name: measure_polygons!
UPDATE plot SET area_ha = ha(geom) WHERE run_id = :run_id AND geom_kind = 'polygon' AND geom IS NOT NULL;

-- name: check_precision!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'coordinate_precision',
       'Coordinates have ' || coord_decimals || ' decimal digits; at least ' || CAST(:min_decimals AS int) || ' are needed',
       jsonb_build_object('decimals', coord_decimals, 'format', coord_format)
FROM plot
WHERE run_id = :run_id AND coord_decimals IS NOT NULL AND coord_decimals < CAST(:min_decimals AS int);

-- name: check_zero_padded!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'zero_padded_precision',
       'Shows ' || coord_decimals || ' decimals but only ' || coord_sig_decimals || ' are real; the rest are padding zeros',
       jsonb_build_object('decimals', coord_decimals, 'significant', coord_sig_decimals, 'format', coord_format)
FROM plot
WHERE run_id = :run_id AND coord_decimals >= CAST(:min_decimals AS int)
  AND coord_sig_decimals < CAST(:min_decimals AS int)
  AND coord_decimals - coord_sig_decimals >= CAST(:min_trailing_zeros AS int);

-- name: check_point_over_4ha!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'point_over_4ha',
       'A point is given for a plot of ' || round(declared_area_ha::numeric, 2) || ' ha; plots over '
         || CAST(:max_ha AS double precision) || ' ha need a polygon',
       jsonb_build_object('declared_area_ha', declared_area_ha)
FROM plot
WHERE run_id = :run_id AND geom_kind = 'point' AND declared_area_ha > CAST(:max_ha AS double precision);

-- name: check_point_area_missing!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'point_area_missing',
       CASE WHEN CAST(:mode AS text) = 'assume'
            THEN 'No area given; assessed as ' || CAST(:assumed_ha AS double precision) || ' ha around the point (assumption)'
            ELSE 'No area given; a point needs its area before it can be assessed' END,
       jsonb_build_object('mode', CAST(:mode AS text), 'assumed_area_ha', CAST(:assumed_ha AS double precision))
FROM plot
WHERE run_id = :run_id AND geom_kind = 'point' AND declared_area_ha IS NULL;

-- name: check_self_intersection!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'self_intersection',
       validity_reason || '; repaired with ST_MakeValid into ' || ST_NumGeometries(geom) || ' part(s) measuring '
         || round(area_ha::numeric, 2) || ' ha (the crossing ring measured ' || round(area_given_ha::numeric, 2) || ' ha as given)',
       jsonb_build_object('reason', validity_reason, 'area_before_ha', round(area_given_ha::numeric, 4),
                          'area_after_ha', round(area_ha::numeric, 4))
FROM plot
WHERE run_id = :run_id AND validity_reason IS NOT NULL;

-- name: check_ring_closed!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'ring_not_closed',
       'The boundary ring was not closed; the first point was repeated at the end', '{}'::jsonb
FROM plot
WHERE run_id = :run_id AND ring_autoclosed;

-- name: check_coordinate_repair!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'coordinate_repair',
       CASE
         WHEN starts_with(repair->>'fit_kind', 'utm_')
           THEN 'Given in metres; read as UTM EPSG:' || split_part(repair->>'fit_kind', '_', 2) || ', the only zone that lands in the declared country'
         WHEN repair->>'fit_kind' = 'swapped' THEN 'Latitude and longitude were swapped; the swapped pair is the only reading inside the declared country'
         ELSE 'A minus sign was missing (' || replace(repair->>'fit_kind', '_', ' ') || '); that is the only reading inside the declared country'
       END || '. Corrected to ' || round(ST_Y(geom)::numeric, 6) || ', ' || round(ST_X(geom)::numeric, 6),
       jsonb_build_object('kind', repair->>'fit_kind', 'lat', round(ST_Y(geom)::numeric, 6), 'lon', round(ST_X(geom)::numeric, 6))
FROM plot
WHERE run_id = :run_id AND geom_kind = 'point' AND NOT (repair->>'as_given_ok')::boolean AND (repair->>'n_fit')::int = 1;

-- name: check_outside_country!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT p.run_id, p.id, :code, :severity, 'outside_country',
       CASE
         WHEN p.geom_kind = 'point' AND (p.repair->>'n_fit')::int > 1
           THEN (p.repair->>'n_fit') || ' readings of these numbers land in the declared country, so none was chosen'
         ELSE 'Not inside ' || c.name
              || coalesce(' (it lies in ' || (SELECT o.name FROM country o WHERE ST_Covers(o.geom, coalesce(p.geom, p.given_geom)) LIMIT 1) || ')', '')
              || CASE WHEN p.geom_kind = 'point' THEN '; no swapped, sign or UTM reading fits either' ELSE '' END
       END,
       jsonb_build_object('declared', c.iso2, 'candidates_inside', coalesce((p.repair->>'n_fit')::int, 0))
FROM plot p
JOIN country c ON c.iso2 = CAST(:iso2 AS text)
WHERE p.run_id = :run_id
  AND ((p.geom_kind = 'point' AND p.geom IS NULL)
       OR (p.geom_kind = 'polygon' AND p.geom IS NOT NULL AND NOT ST_Covers(c.geom_tol, p.geom)));

-- name: check_implausible_area!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'implausible_area',
       'Declared area ' || declared_area_ha || ' ha is outside 0 to ' || CAST(:max_ha AS double precision) || ' ha',
       jsonb_build_object('declared_area_ha', declared_area_ha)
FROM plot
WHERE run_id = :run_id AND (declared_area_ha <= 0 OR declared_area_ha > CAST(:max_ha AS double precision));

-- name: check_area_mismatch!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, id, :code, :severity, 'area_mismatch',
       'Declared ' || round(declared_area_ha::numeric, 2) || ' ha, boundary measures ' || round(area_ha::numeric, 2)
         || ' ha (geodesic), off by ' || round((100 * abs(area_ha - declared_area_ha) / declared_area_ha)::numeric) || ' percent',
       jsonb_build_object('declared_area_ha', declared_area_ha, 'measured_ha', round(area_ha::numeric, 4))
FROM plot
WHERE run_id = :run_id AND geom_kind = 'polygon' AND area_ha IS NOT NULL
  AND declared_area_ha > 0 AND declared_area_ha <= CAST(:max_ha AS double precision)
  AND abs(area_ha - declared_area_ha) / declared_area_ha > CAST(:max_rel AS double precision);

-- name: check_duplicates!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT DISTINCT ON (b.id) b.run_id, b.id, :code, :severity, 'duplicate_plot',
       'Same ' || CASE WHEN lower(a.plot_code) = lower(b.plot_code) THEN 'plot code' ELSE 'location' END
         || ' as row ' || a.row_number || ' (' || a.plot_code || ')',
       jsonb_build_object('first_row', a.row_number, 'first_plot_code', a.plot_code)
FROM plot a
JOIN plot b ON a.run_id = b.run_id AND a.row_number < b.row_number
WHERE a.run_id = :run_id
  AND (lower(a.plot_code) = lower(b.plot_code)
       OR (a.geom IS NOT NULL AND b.geom IS NOT NULL AND a.geom_kind = b.geom_kind
           AND CASE WHEN a.geom_kind = 'point'
                    THEN ST_DWithin(a.geom::geography, b.geom::geography, CAST(:same_point_m AS double precision))
                    ELSE ST_Equals(a.geom, b.geom) END))
ORDER BY b.id, a.row_number;

-- name: check_plot_overlaps!
WITH pairs AS (
  SELECT a.id AS a_id, b.id AS b_id, a.run_id, a.row_number AS a_row, b.row_number AS b_row,
         a.plot_code AS a_code, b.plot_code AS b_code, ha(ST_Intersection(a.geom, b.geom)) AS ov
  FROM plot a
  JOIN plot b ON a.run_id = b.run_id AND a.id < b.id AND ST_Intersects(a.geom, b.geom)
  WHERE a.run_id = :run_id AND a.geom_kind = 'polygon' AND b.geom_kind = 'polygon'
    AND NOT EXISTS (SELECT 1 FROM plot_issue i WHERE i.plot_id IN (a.id, b.id) AND i.rule_key = 'duplicate_plot')
)
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT run_id, a_id, :code, :severity, 'plot_overlap',
       'Overlaps ' || round(ov::numeric, 2) || ' ha with row ' || b_row || ' (' || b_code || ')',
       jsonb_build_object('other_row', b_row, 'other_plot_code', b_code, 'overlap_ha', round(ov::numeric, 4))
FROM pairs WHERE ov >= CAST(:min_ha AS double precision)
UNION ALL
SELECT run_id, b_id, :code, :severity, 'plot_overlap',
       'Overlaps ' || round(ov::numeric, 2) || ' ha with row ' || a_row || ' (' || a_code || ')',
       jsonb_build_object('other_row', a_row, 'other_plot_code', a_code, 'overlap_ha', round(ov::numeric, 4))
FROM pairs WHERE ov >= CAST(:min_ha AS double precision);

-- name: set_assessable!
UPDATE plot p
SET assessable = p.geom IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM plot_issue i WHERE i.plot_id = p.id AND i.rule_key = 'outside_country')
    AND NOT (CAST(:require_area AS boolean) AND p.geom_kind = 'point' AND p.declared_area_ha IS NULL)
WHERE p.run_id = :run_id;

-- name: build_assess_geometry!
-- Points become equal-area geodesic circles from the declared (or assumed) area. The radius is
-- scaled so the 64-sided buffer has the circle's area, not slightly less.
UPDATE plot
SET assess_basis = CASE WHEN geom_kind = 'polygon' THEN 'polygon'
                        WHEN declared_area_ha > 0 THEN 'circle_declared' ELSE 'circle_assumed' END,
    circle_radius_m = CASE WHEN geom_kind = 'point'
      THEN sqrt((CASE WHEN declared_area_ha > 0 THEN declared_area_ha ELSE CAST(:assumed_ha AS double precision) END) * 10000.0 / pi()) END
WHERE run_id = :run_id AND assessable;

-- name: buffer_points!
UPDATE plot
SET assess_geom = CASE WHEN geom_kind = 'polygon' THEN geom
  ELSE ST_Buffer(geom::geography, circle_radius_m * sqrt((2 * pi() / 64) / sin(2 * pi() / 64)), 'quad_segs=16')::geometry END
WHERE run_id = :run_id AND assessable;

-- name: analyze_plots#
ANALYZE plot;

-- name: insert_loss_hits!
INSERT INTO overlap_hit (run_id, plot_id, layer, ref_id, overlap_ha, detail)
SELECT p.run_id, p.id, 'forest_loss', l.id, ha(ST_Intersection(p.assess_geom, l.geom)),
       jsonb_build_object('detected_on', l.detected_on, 'confidence', round(l.confidence::numeric, 2),
                          'patch_ha', round(ha(l.geom)::numeric, 3))
FROM plot p
JOIN forest_loss l ON ST_Intersects(p.assess_geom, l.geom)
WHERE p.run_id = :run_id AND p.assessable AND l.detected_on > CAST(:cutoff AS date);

-- name: insert_protected_hits!
INSERT INTO overlap_hit (run_id, plot_id, layer, ref_id, overlap_ha, detail)
SELECT p.run_id, p.id, 'protected_area', a.id, ha(ST_Intersection(p.assess_geom, a.geom)), jsonb_build_object('name', a.name)
FROM plot p
JOIN protected_area a ON ST_Intersects(p.assess_geom, a.geom)
WHERE p.run_id = :run_id AND p.assessable;

-- name: insert_forest_hits!
INSERT INTO overlap_hit (run_id, plot_id, layer, ref_id, overlap_ha, detail)
SELECT p.run_id, p.id, 'forest_2020', f.id, ha(ST_Intersection(p.assess_geom, f.geom)), '{}'::jsonb
FROM plot p
JOIN forest_2020 f ON ST_Intersects(p.assess_geom, f.geom)
WHERE p.run_id = :run_id AND p.assessable;

-- name: check_loss!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT h.run_id, h.plot_id, :code, :severity, 'loss_after_cutoff',
       'Overlaps ' || round(sum(h.overlap_ha)::numeric, 2) || ' ha of forest loss detected after '
         || CAST(:cutoff AS text) || ' (' || string_agg(h.ref_id, ', ' ORDER BY h.overlap_ha DESC) || ')',
       jsonb_build_object('total_ha', round(sum(h.overlap_ha)::numeric, 4),
                          'patches', jsonb_agg(jsonb_build_object('id', h.ref_id, 'overlap_ha', round(h.overlap_ha::numeric, 4),
                                                                  'detected_on', h.detail->>'detected_on',
                                                                  'confidence', h.detail->'confidence')
                                               ORDER BY h.overlap_ha DESC))
FROM overlap_hit h
WHERE h.run_id = :run_id AND h.layer = 'forest_loss'
  AND h.overlap_ha >= CAST(:min_ha AS double precision)
  AND (h.detail->>'confidence')::real >= CAST(:min_confidence AS real)
GROUP BY h.run_id, h.plot_id;

-- name: check_protected!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT h.run_id, h.plot_id, :code, :severity, 'protected_area',
       'Overlaps ' || round(h.overlap_ha::numeric, 2) || ' ha of ' || (h.detail->>'name'),
       jsonb_build_object('area_id', h.ref_id, 'overlap_ha', round(h.overlap_ha::numeric, 4))
FROM overlap_hit h
WHERE h.run_id = :run_id AND h.layer = 'protected_area' AND h.overlap_ha >= CAST(:min_ha AS double precision);

-- name: set_status!
UPDATE plot p
SET status = s.status
FROM (
  SELECT p2.id,
         CASE WHEN bool_or(i.severity = 'error') THEN 'needs_fix'
              WHEN bool_or(i.severity = 'review') THEN 'review'
              WHEN bool_or(i.severity = 'repaired') THEN 'repaired'
              ELSE 'ready' END AS status
  FROM plot p2
  LEFT JOIN plot_issue i ON i.plot_id = p2.id
  WHERE p2.run_id = :run_id
  GROUP BY p2.id
) s
WHERE p.id = s.id;
