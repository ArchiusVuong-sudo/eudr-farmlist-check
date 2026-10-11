-- Writes: reference data, uploads, mappings, runs and their results.

-- name: upsert_dataset_version!
INSERT INTO dataset_version (layer, version, checksum, features, source)
VALUES (:layer, :version, :checksum, :features, :source)
ON CONFLICT (layer) DO UPDATE
SET version = EXCLUDED.version, checksum = EXCLUDED.checksum, features = EXCLUDED.features,
    source = EXCLUDED.source, loaded_at = now();

-- name: dataset_checksum$
SELECT checksum FROM dataset_version WHERE layer = :layer;

-- name: upsert_country!
INSERT INTO country (iso2, name, utm_srids, geom, geom_tol)
SELECT :iso2, :name, CAST(:utm_srids AS integer[]), g, ST_Multi(ST_Buffer(g::geography, CAST(:tol_m AS double precision))::geometry)
FROM (SELECT ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)) AS g) s
ON CONFLICT (iso2) DO UPDATE
SET name = EXCLUDED.name, utm_srids = EXCLUDED.utm_srids, geom = EXCLUDED.geom, geom_tol = EXCLUDED.geom_tol;

-- name: insert_forest_2020*!
INSERT INTO forest_2020 (id, region, geom) VALUES (:id, :region, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326));

-- name: insert_forest_loss*!
INSERT INTO forest_loss (id, region, detected_on, confidence, geom)
VALUES (:id, :region, CAST(:detected_on AS date), :confidence, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326));

-- name: insert_protected_area*!
INSERT INTO protected_area (id, name, region, geom) VALUES (:id, :name, :region, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326));

-- name: clear_reference#
TRUNCATE forest_2020, forest_loss, protected_area;

-- name: analyze_reference#
ANALYZE country; ANALYZE forest_2020; ANALYZE forest_loss; ANALYZE protected_area;

-- name: upsert_rule_pack^
INSERT INTO rule_pack (version, checksum, title, body)
VALUES (:version, :checksum, :title, CAST(:body AS jsonb))
ON CONFLICT (version, checksum) DO UPDATE SET title = EXCLUDED.title
RETURNING id;

-- name: insert_farm_list^
INSERT INTO farm_list (file_name, file_kind, country_iso2, header_signature, headers, profiles)
VALUES (:file_name, :file_kind, :country_iso2, :header_signature, CAST(:headers AS jsonb), CAST(:profiles AS jsonb))
RETURNING id;

-- name: insert_raw_rows*!
INSERT INTO raw_row (farm_list_id, row_number, cells) VALUES (:farm_list_id, :row_number, CAST(:cells AS jsonb));

-- name: set_proposal!
UPDATE farm_list SET proposal = CAST(:proposal AS jsonb) WHERE id = :farm_list_id;

-- name: set_mapping!
UPDATE farm_list SET mapping = CAST(:mapping AS jsonb), mapping_profile_id = :profile_id WHERE id = :farm_list_id;

-- name: upsert_mapping_profile^
INSERT INTO mapping_profile (signature, headers, mapping, source)
VALUES (:signature, CAST(:headers AS jsonb), CAST(:mapping AS jsonb), :source)
ON CONFLICT (signature) DO UPDATE SET mapping = EXCLUDED.mapping, source = EXCLUDED.source, created_at = now()
RETURNING id;

-- name: get_mapping_profile^
SELECT * FROM mapping_profile WHERE signature = :signature;

-- name: insert_run^
INSERT INTO run (farm_list_id, rule_pack_id, dataset_checksum) VALUES (:farm_list_id, :rule_pack_id, :dataset_checksum)
RETURNING id;

-- name: finish_run!
UPDATE run
SET finished_at = now(), stage_ms = CAST(:stage_ms AS jsonb), counts = CAST(:counts AS jsonb),
    result_hash = :result_hash, assess_ms = :assess_ms, explain_plan = :explain_plan
WHERE id = :run_id;

-- name: insert_plots*!
INSERT INTO plot (run_id, raw_row_id, row_number, plot_code, producer, admin_area, declared_area_ha, lat_in, lon_in,
                  coord_format, coord_decimals, coord_sig_decimals, wkt_in, ring_autoclosed, parse)
VALUES (:run_id, :raw_row_id, :row_number, :plot_code, :producer, :admin_area, :declared_area_ha, :lat_in, :lon_in,
        :coord_format, :coord_decimals, :coord_sig_decimals, :wkt_in, :ring_autoclosed, CAST(:parse AS jsonb));

-- name: insert_row_issues*!
INSERT INTO plot_issue (run_id, plot_id, code, severity, rule_key, message, detail)
SELECT p.run_id, p.id, :code, :severity, :rule_key, :message, CAST(:detail AS jsonb)
FROM plot p WHERE p.run_id = :run_id AND p.row_number = :row_number;

-- name: insert_trace_steps*!
INSERT INTO trace_step (run_id, plot_id, seq, stage, title, detail)
VALUES (:run_id, :plot_id, :seq, :stage, :title, CAST(:detail AS jsonb));
