-- Reads for the pages, the trace builder and the exports.

-- name: list_farm_lists
SELECT f.id, f.file_name, f.file_kind, f.country_iso2, c.name AS country_name, f.uploaded_at,
       jsonb_array_length(f.headers) AS n_columns,
       (SELECT count(*) FROM raw_row r WHERE r.farm_list_id = f.id) AS n_rows,
       f.mapping IS NOT NULL AS mapped,
       lr.id AS last_run_id, lr.counts AS last_counts, rp.version AS last_version
FROM farm_list f
JOIN country c ON c.iso2 = f.country_iso2
LEFT JOIN LATERAL (SELECT * FROM run r WHERE r.farm_list_id = f.id ORDER BY r.id DESC LIMIT 1) lr ON true
LEFT JOIN rule_pack rp ON rp.id = lr.rule_pack_id
ORDER BY f.id DESC;

-- name: get_farm_list^
SELECT f.*, c.name AS country_name FROM farm_list f JOIN country c ON c.iso2 = f.country_iso2 WHERE f.id = :farm_list_id;

-- name: get_raw_rows
SELECT id, row_number, cells FROM raw_row WHERE farm_list_id = :farm_list_id ORDER BY row_number;

-- name: get_raw_row^
SELECT id, row_number, cells FROM raw_row WHERE id = :raw_row_id;

-- name: list_countries
SELECT iso2, name FROM country WHERE cardinality(utm_srids) > 0 ORDER BY name;

-- name: get_run^
SELECT r.*, rp.version, rp.checksum AS pack_checksum, rp.title AS pack_title,
       f.file_name, f.country_iso2, c.name AS country_name, f.mapping, f.mapping_profile_id, f.headers
FROM run r
JOIN rule_pack rp ON rp.id = r.rule_pack_id
JOIN farm_list f ON f.id = r.farm_list_id
JOIN country c ON c.iso2 = f.country_iso2
WHERE r.id = :run_id;

-- name: runs_for_list
SELECT r.id, r.started_at, r.result_hash, r.counts, rp.version, rp.checksum AS pack_checksum
FROM run r JOIN rule_pack rp ON rp.id = r.rule_pack_id
WHERE r.farm_list_id = :farm_list_id ORDER BY r.id;

-- name: plots_for_run
SELECT p.id, p.row_number, p.plot_code, p.producer, p.admin_area, p.declared_area_ha, p.geom_kind, p.status,
       p.coord_format, p.area_ha, p.assess_basis, p.repair, p.lat_in, p.lon_in,
       round(ST_Y(ST_PointOnSurface(p.geom))::numeric, 6) AS lat, round(ST_X(ST_PointOnSurface(p.geom))::numeric, 6) AS lon,
       coalesce((SELECT jsonb_agg(jsonb_build_object('code', i.code, 'severity', i.severity, 'rule_key', i.rule_key,
                                                     'message', i.message, 'detail', i.detail) ORDER BY i.id)
                 FROM plot_issue i WHERE i.plot_id = p.id), '[]'::jsonb) AS issues
FROM plot p
WHERE p.run_id = :run_id
ORDER BY p.row_number;

-- name: issue_summary
SELECT i.rule_key, i.code, i.severity, count(*) AS n, array_agg(p.plot_code ORDER BY p.row_number) AS plots
FROM plot_issue i JOIN plot p ON p.id = i.plot_id
WHERE i.run_id = :run_id
GROUP BY i.rule_key, i.code, i.severity
ORDER BY CASE i.severity WHEN 'error' THEN 0 WHEN 'review' THEN 1 WHEN 'repaired' THEN 2 ELSE 3 END, count(*) DESC, i.code;

-- name: get_plot^
SELECT p.*, ST_AsGeoJSON(p.geom, 6) AS geom_json, ST_AsGeoJSON(p.given_geom, 6) AS given_json,
       ST_AsGeoJSON(p.assess_geom, 6) AS assess_json,
       round(ST_Y(ST_PointOnSurface(p.geom))::numeric, 6) AS lat, round(ST_X(ST_PointOnSurface(p.geom))::numeric, 6) AS lon
FROM plot p WHERE p.id = :plot_id;

-- name: plot_by_code^
SELECT id FROM plot WHERE run_id = :run_id AND lower(plot_code) = lower(:plot_code) ORDER BY row_number LIMIT 1;

-- name: search_plot^
SELECT p.id, p.run_id FROM plot p
WHERE lower(p.plot_code) = lower(:plot_code)
ORDER BY p.run_id DESC, p.row_number LIMIT 1;

-- name: issues_for_plot
SELECT code, severity, rule_key, message, detail FROM plot_issue WHERE plot_id = :plot_id ORDER BY id;

-- name: hits_for_plot
SELECT h.layer, h.ref_id, h.overlap_ha, h.detail,
       CASE h.layer
         WHEN 'forest_loss' THEN (SELECT ST_AsGeoJSON(l.geom, 6) FROM forest_loss l WHERE l.id = h.ref_id)
         WHEN 'protected_area' THEN (SELECT ST_AsGeoJSON(a.geom, 6) FROM protected_area a WHERE a.id = h.ref_id)
       END AS geom_json
FROM overlap_hit h WHERE h.plot_id = :plot_id ORDER BY h.layer, h.overlap_ha DESC;

-- name: hits_for_run
SELECT h.plot_id, h.layer, h.ref_id, h.overlap_ha, h.detail FROM overlap_hit h WHERE h.run_id = :run_id ORDER BY h.plot_id, h.layer, h.overlap_ha DESC;

-- name: trace_for_plot
SELECT seq, stage, title, detail FROM trace_step WHERE plot_id = :plot_id ORDER BY seq;

-- name: plot_trace_rows
-- Everything the trace builder needs per plot, in one pass.
SELECT p.id, p.row_number, p.plot_code, p.raw_row_id, p.geom_kind, p.coord_format, p.coord_decimals, p.coord_sig_decimals,
       p.declared_area_ha, p.area_ha, p.area_given_ha, p.validity_reason, p.ring_autoclosed, p.repair, p.assessable,
       p.assess_basis, p.circle_radius_m, p.status, p.parse, p.lat_in, p.lon_in,
       round(ST_Y(ST_PointOnSurface(p.geom))::numeric, 6) AS lat, round(ST_X(ST_PointOnSurface(p.geom))::numeric, 6) AS lon,
       round(ha(p.assess_geom)::numeric, 4) AS assess_ha
FROM plot p WHERE p.run_id = :run_id ORDER BY p.row_number;

-- name: issues_for_run
SELECT plot_id, code, severity, rule_key, message, detail FROM plot_issue WHERE run_id = :run_id ORDER BY plot_id, id;

-- name: result_rows
-- The inputs of the result hash: what a person would call the result, nothing run-specific.
SELECT p.row_number, p.plot_code, p.status,
       coalesce((SELECT string_agg(i.code, ',' ORDER BY i.code) FROM plot_issue i WHERE i.plot_id = p.id), '') AS codes,
       coalesce((SELECT round(sum(h.overlap_ha)::numeric, 3)::text FROM overlap_hit h
                 WHERE h.plot_id = p.id AND h.layer = 'forest_loss'), '0') AS loss_ha,
       coalesce(ST_AsText(ST_ReducePrecision(p.geom, 0.000001)), '') AS geom_wkt
FROM plot p WHERE p.run_id = :run_id ORDER BY p.row_number;

-- name: status_counts
SELECT status, count(*) AS n FROM plot WHERE run_id = :run_id GROUP BY status;

-- name: run_map_features$
SELECT jsonb_build_object('type', 'FeatureCollection', 'features', coalesce(jsonb_agg(jsonb_build_object(
         'type', 'Feature',
         'geometry', ST_AsGeoJSON(coalesce(p.assess_geom, p.geom), 6)::jsonb,
         'properties', jsonb_build_object('id', p.id, 'code', p.plot_code, 'status', p.status, 'row', p.row_number,
                                          'kind', p.geom_kind, 'basis', p.assess_basis))), '[]'::jsonb))::text
FROM plot p WHERE p.run_id = :run_id AND p.geom IS NOT NULL;

-- name: run_extent^
SELECT ST_XMin(e) AS xmin, ST_YMin(e) AS ymin, ST_XMax(e) AS xmax, ST_YMax(e) AS ymax
FROM (SELECT ST_Extent(geom)::geometry AS e FROM plot WHERE run_id = :run_id AND assessable) s;

-- name: layers_in_box$
SELECT jsonb_build_object(
  'loss', (SELECT jsonb_build_object('type', 'FeatureCollection', 'features', coalesce(jsonb_agg(jsonb_build_object('type', 'Feature',
             'geometry', ST_AsGeoJSON(l.geom, 6)::jsonb, 'properties', jsonb_build_object('id', l.id, 'date', l.detected_on, 'confidence', round(l.confidence::numeric, 2)))), '[]'::jsonb))
           FROM forest_loss l WHERE l.geom && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)),
  'forest', (SELECT jsonb_build_object('type', 'FeatureCollection', 'features', coalesce(jsonb_agg(jsonb_build_object('type', 'Feature',
             'geometry', ST_AsGeoJSON(f.geom, 5)::jsonb, 'properties', jsonb_build_object('id', f.id))), '[]'::jsonb))
           FROM forest_2020 f WHERE f.geom && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)),
  'protected', (SELECT jsonb_build_object('type', 'FeatureCollection', 'features', coalesce(jsonb_agg(jsonb_build_object('type', 'Feature',
             'geometry', ST_AsGeoJSON(a.geom, 6)::jsonb, 'properties', jsonb_build_object('id', a.id, 'name', a.name))), '[]'::jsonb))
           FROM protected_area a WHERE a.geom && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)),
  'country', (SELECT ST_AsGeoJSON(ST_Intersection(c.geom, ST_MakeEnvelope(:xmin - 2, :ymin - 2, :xmax + 2, :ymax + 2, 4326)), 5)::jsonb
           FROM country c WHERE c.iso2 = :iso2)
)::text;

-- name: ready_geojson$
SELECT jsonb_build_object('type', 'FeatureCollection', 'name', 'ready_plots',
  'crs_note', 'WGS 84 (EPSG:4326), longitude/latitude, 6 decimals', 'synthetic_data', true,
  'features', coalesce(jsonb_agg(jsonb_build_object('type', 'Feature',
    'geometry', ST_AsGeoJSON(p.geom, 6)::jsonb,
    'properties', jsonb_build_object('plot_code', p.plot_code, 'row', p.row_number,
                                     'area_ha', round(coalesce(p.area_ha, p.declared_area_ha)::numeric, 4),
                                     'geometry_kind', p.geom_kind)) ORDER BY p.row_number), '[]'::jsonb))::text
FROM plot p WHERE p.run_id = :run_id AND p.status = 'ready';

-- name: fix_list_rows
SELECT p.row_number, p.plot_code, i.code, i.severity, i.rule_key, i.message
FROM plot p JOIN plot_issue i ON i.plot_id = p.id
WHERE p.run_id = :run_id AND i.severity IN ('error', 'repaired', 'warning')
ORDER BY p.row_number, CASE i.severity WHEN 'error' THEN 0 WHEN 'repaired' THEN 1 ELSE 2 END, i.id;

-- name: dataset_versions
SELECT layer, version, checksum, features, source, loaded_at FROM dataset_version ORDER BY layer;

-- name: previous_same_pack^
SELECT r.id, r.result_hash FROM run r
WHERE r.farm_list_id = :farm_list_id AND r.rule_pack_id = :rule_pack_id AND r.id < :run_id
ORDER BY r.id DESC LIMIT 1;
