-- =============================================================================
-- Boston open-data warehouse — DuckDB schema
-- Generated from analysis in DataDictionary.csv and DataConnections.txt
--
-- Layers:
--   1. RAW      — one table per source CSV/JSON, all-VARCHAR, byte-for-byte load.
--   2. PARSED   — same grain as source, typed columns, per-source cleanup applied.
--   3. DERIVED  — novel entities extracted/resolved ACROSS sources (streets,
--                 addresses, establishments, neighborhoods, police-district
--                 crosswalk). These do not exist as columns in any one source;
--                 they are built by the ETL queries below.
--
-- Requires the DuckDB spatial extension for GEOMETRY/ST_* functions:
--   INSTALL spatial; LOAD spatial;
-- =============================================================================

INSTALL spatial;
LOAD spatial;

-- =============================================================================
-- 1. RAW LAYER — load exactly as delivered, all VARCHAR, no coercion
-- =============================================================================

-- Plain read_csv(all_varchar=true) correctly auto-sniffs every file here
-- EXCEPT open-space.csv, whose dialect DuckDB's sniffer collapses to a
-- single column by default (verified) — that one file needs an explicit
-- delimiter/quote plus null_padding/strict_mode=false to parse its embedded
-- newline correctly. Do NOT apply those same overrides to the larger files:
-- combined with parallel=false they force a single-threaded scan that runs
-- the multi-hundred-MB files (food_inspections, crime_incidents) out of
-- memory instead of just parsing correctly. Keep the two loading styles separate.

CREATE OR REPLACE TABLE raw_service_requests_311 AS
    SELECT * FROM read_csv('data/311-service-requests.csv', all_varchar = true);

CREATE OR REPLACE TABLE raw_crime_incidents AS
    SELECT * FROM read_csv(
        'data/crime-incident-reports-august-2015-to-date-source-new-system.csv',
        all_varchar = true);

CREATE OR REPLACE TABLE raw_food_inspections AS
    SELECT * FROM read_csv('data/food-establishment-inspections.csv', all_varchar = true);

CREATE OR REPLACE TABLE raw_licenses_board AS
    SELECT * FROM read_csv('data/licensing-board-licenses.csv', all_varchar = true);

CREATE OR REPLACE TABLE raw_licenses_entertainment AS
    SELECT * FROM read_csv('data/entertainment-licenses-legacy.csv', all_varchar = true);

CREATE OR REPLACE TABLE raw_open_space AS
    SELECT * FROM read_csv('data/open-space.csv',
        all_varchar = true, delim = ',', quote = '"',
        strict_mode = false, null_padding = true, parallel = false);

CREATE OR REPLACE TABLE raw_neighborhoods_bpda AS
    SELECT * FROM read_csv('data/bpda-neighborhood-boundaries.csv', all_varchar = true);

-- GeoJSON is not flat CSV; read_json returns one row holding the whole
-- FeatureCollection (a single "features" array column) rather than one row
-- per feature. Unnest it so the raw layer sits at the same grain (one row =
-- one neighborhood feature) as every other raw_* table, with properties/
-- geometry still nested exactly as delivered.
CREATE OR REPLACE TABLE raw_neighborhoods_boundary AS
    SELECT UNNEST(features) AS feature
    FROM read_json('data/boston_neighborhood_boundaries.json',
        format = 'auto', maximum_object_size = 20000000);

-- =============================================================================
-- 2. PARSED LAYER — typed, same grain as source, per-column casts from
--    DataDictionary.csv's "notes" column applied here
-- =============================================================================

CREATE OR REPLACE TABLE service_requests_311 AS
SELECT
    TRY_CAST(case_enquiry_id AS BIGINT)               AS case_enquiry_id,
    TRY_CAST(open_dt AS TIMESTAMP)                    AS open_dt,
    TRY_CAST(sla_target_dt AS TIMESTAMP)              AS sla_target_dt,
    TRY_CAST(closed_dt AS TIMESTAMP)                  AS closed_dt,
    on_time,
    case_status,
    NULLIF(closure_reason, '')                        AS closure_reason,
    case_title, subject, reason, type, queue, department,
    NULLIF(submitted_photo, '')                        AS submitted_photo,
    NULLIF(closed_photo, '')                           AS closed_photo,
    location,
    fire_district,
    pwd_district,                                     -- keep zero-padded, VARCHAR
    city_council_district,
    NULLIF(police_district, '')                        AS police_district,
    NULLIF(neighborhood, '')                           AS neighborhood_raw,
    neighborhood_services_district,
    -- normalize ward to 2-digit zero-padded code: "Ward 14"/"14"/"04" -> "14"/"04"
    LPAD(REGEXP_REPLACE(ward, '[^0-9]', '', 'g'), 2, '0') AS ward,
    precinct,                                         -- 4-digit zero-padded, VARCHAR
    location_street_name,
    NULLIF(location_zipcode, '')                       AS location_zipcode,
    TRY_CAST(latitude AS DOUBLE)                       AS latitude,
    TRY_CAST(longitude AS DOUBLE)                      AS longitude,
    TRY_CAST(ST_GeomFromHEXWKB(geom_4326) AS GEOMETRY) AS geom,
    source
FROM raw_service_requests_311;

CREATE OR REPLACE TABLE crime_incidents AS
SELECT
    incident_number                                    AS incident_number,
    TRY_CAST(offense_code AS INTEGER)                  AS offense_code,
    NULLIF(offense_code_group, '')                      AS offense_code_group,  -- always NULL today; see notes
    offense_description,
    district,                                          -- FK -> dim_police_district
    NULLIF(TRIM(reporting_area), '')                    AS reporting_area,
    (shooting = '1')                                    AS shooting,
    TRY_CAST(occurred_on_date AS TIMESTAMPTZ)           AS occurred_on,
    TRY_CAST(year AS INTEGER)                           AS year,
    TRY_CAST(month AS INTEGER)                          AS month,
    TRIM(day_of_week)                                   AS day_of_week,
    TRY_CAST(hour AS INTEGER)                           AS hour,
    NULLIF(ucr_part, '')                                AS ucr_part,            -- always NULL today; see notes
    street,
    TRY_CAST(NULLIF(lat, '0.0') AS DOUBLE)              AS lat,
    TRY_CAST(NULLIF(long, '0.0') AS DOUBLE)             AS long
FROM raw_crime_incidents;

CREATE OR REPLACE TABLE food_inspections AS
SELECT
    businessname, NULLIF(dbaname, '') AS dbaname, legalowner, namelast, namefirst,
    licenseno,
    TRY_CAST(issdttm AS TIMESTAMPTZ)                    AS issued_at,
    TRY_CAST(expdttm AS TIMESTAMPTZ)                    AS expires_at,
    licstatus, licensecat, descript,
    result,
    TRY_CAST(resultdttm AS TIMESTAMPTZ)                 AS result_at,
    violation, viol_level, violdesc,
    TRY_CAST(violdttm AS TIMESTAMPTZ)                   AS violation_at,
    viol_status,
    TRY_CAST(NULLIF(status_date, '') AS TIMESTAMP)      AS status_date,
    comments, address,
    UPPER(city)                                         AS city,               -- normalize casing
    state,
    NULLIF(NULLIF(zip, '00000'), '')                    AS zip,                -- '00000' sentinel -> NULL
    property_id,
    -- location is "(lat, long)" text; split into real doubles
    TRY_CAST(SPLIT_PART(TRIM(location, '()'), ', ', 1) AS DOUBLE) AS lat,
    TRY_CAST(SPLIT_PART(TRIM(location, '()'), ', ', 2) AS DOUBLE) AS lon
FROM raw_food_inspections;

CREATE OR REPLACE TABLE licenses_board AS
SELECT
    license_num, NULLIF(historicallicensenum, '') AS historical_license_num,
    status, license_category, license_type,
    TRY_CAST(NULLIF(issued, '') AS DATE)                AS issued,
    TRY_CAST(NULLIF(expires, '') AS DATE)               AS expires,
    business_name, dba_name, comments, location_comments,
    opening, closing,
    TRY_CAST(NULLIF(patronsout, '') AS INTEGER)         AS patrons_out,
    TRY_CAST(NULLIF(capacity, '') AS INTEGER)           AS capacity,
    descpremadd, applicant, manager, day_phone, evening_phone,
    address, city, state, zip,
    TRY_CAST(NULLIF(gpsx, '0') AS DOUBLE)               AS gpsx_ma_state_plane_ft,
    TRY_CAST(NULLIF(gpsy, '0') AS DOUBLE)               AS gpsy_ma_state_plane_ft,
    -- reproject MA State Plane Mainland NAD83 (ft), EPSG:2249, to WGS84
    TRY_CAST(ST_Transform(
        ST_Point(TRY_CAST(NULLIF(gpsx,'0') AS DOUBLE), TRY_CAST(NULLIF(gpsy,'0') AS DOUBLE)),
        'EPSG:2249', 'EPSG:4326') AS GEOMETRY)          AS geom
FROM raw_licenses_board;

CREATE OR REPLACE TABLE licenses_entertainment AS
SELECT
    license_num, status, license_type,
    TRY_CAST(NULLIF(issued, '') AS DATE)                AS issued,
    TRY_CAST(NULLIF(expires, '') AS DATE)               AS expires,
    business_name, dba_name, comments, location_comments,
    applicant, manager, address,
    UPPER(city)                                         AS city,
    state, zip,
    TRY_CAST(NULLIF(gpsx, '0') AS DOUBLE)               AS gpsx_ma_state_plane_ft,
    TRY_CAST(NULLIF(gpsy, '0') AS DOUBLE)               AS gpsy_ma_state_plane_ft,
    TRY_CAST(ST_Transform(
        ST_Point(TRY_CAST(NULLIF(gpsx,'0') AS DOUBLE), TRY_CAST(NULLIF(gpsy,'0') AS DOUBLE)),
        'EPSG:2249', 'EPSG:4326') AS GEOMETRY)          AS geom
FROM raw_licenses_entertainment;

CREATE OR REPLACE TABLE open_space AS
SELECT
    site_name, ownership, protection, typecode, district,
    TRY_CAST(acres AS DOUBLE)                           AS acres,
    address, zonagg, typelong, os_own_jur,
    NULLIF(os_mngmnt, 'NULL')                           AS os_mngmnt,          -- literal 'NULL' string -> real NULL
    (pos = 'X')                                         AS is_public_open_space,
    (pa = 'X')                                          AS is_publicly_accessible,
    NULLIF(alt_name, '')                                AS alt_name,
    os_id,
    (f_100ftrule = 'YES')                               AS f_100ft_rule,
    zipcode,
    parcelnumber,
    NULLIF(TRY_CAST(yearacquired AS INTEGER), 0)        AS year_acquired       -- 0 means unknown -> NULL
FROM raw_open_space;

CREATE OR REPLACE TABLE neighborhoods_bpda AS
SELECT
    name, TRY_CAST(acres AS DOUBLE) AS acres, neighborhood_id,
    TRY_CAST(sqmiles AS DOUBLE) AS sqmiles
FROM raw_neighborhoods_bpda;

-- =============================================================================
-- 3. DERIVED ENTITY LAYER — cross-source extraction/resolution
--    (the actual "novel entities": these tables have no 1:1 source file)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 3.1 dim_neighborhood — merge bpda attributes + boundary geometry
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_neighborhood AS
SELECT
    b.neighborhood_id,
    b.name,
    b.acres,
    b.sqmiles,
    ST_GeomFromGeoJSON(TO_JSON(g.feature.geometry))     AS geom
FROM neighborhoods_bpda b
JOIN raw_neighborhoods_boundary g
  ON b.neighborhood_id = g.feature.properties.neighborhood_id;

ALTER TABLE dim_neighborhood ADD PRIMARY KEY (neighborhood_id);

-- ---------------------------------------------------------------------------
-- 3.2 xref_neighborhood_alias — hand-curated map from every raw neighborhood/
--     district spelling seen across sources to the canonical neighborhood_id.
--     311's own "neighborhood" column can't be trusted to auto-resolve
--     (it mixes granularities: "Allston / Brighton" combined vs "Allston",
--     "Brighton" separate polygons; "South Boston / South Boston Waterfront"
--     vs "South Boston"; a meaningless "Boston" bucket; 254 NULLs).
--     Populate this table manually / with a review step, not a blind join.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE xref_neighborhood_alias (
    raw_value       VARCHAR NOT NULL,
    source_column   VARCHAR NOT NULL,   -- e.g. 'service_requests_311.neighborhood_raw'
    neighborhood_id VARCHAR NOT NULL REFERENCES dim_neighborhood(neighborhood_id),
    PRIMARY KEY (raw_value, source_column, neighborhood_id)
);
-- Example seed rows (extend after reviewing DISTINCT values per source):
-- INSERT INTO xref_neighborhood_alias VALUES
--   ('Allston / Brighton', 'service_requests_311.neighborhood_raw', '2'),   -- Allston
--   ('Allston / Brighton', 'service_requests_311.neighborhood_raw', '4'),   -- Brighton  (one alias -> two ids)
--   ('Allston-Brighton',   'open_space.district',                   '2'),
--   ('Allston-Brighton',   'open_space.district',                   '4');

-- ---------------------------------------------------------------------------
-- 3.3 dim_police_district — canonical union of every district code observed.
--     crime_incidents contributes two codes never seen in 311's
--     police_district column: 'External' and 'Outside of' (the latter always
--     paired with REPORTING_AREA='OOJ' — Out Of [BPD] Jurisdiction). Both are
--     flagged via is_out_of_jurisdiction rather than dropped, since they are
--     real incidents, just not attributable to a normal BPD district.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_police_district AS
SELECT DISTINCT
    district AS police_district_code,
    (district IN ('External', 'Outside of')) AS is_out_of_jurisdiction
FROM (
    SELECT police_district AS district FROM service_requests_311 WHERE police_district IS NOT NULL
    UNION
    SELECT district FROM crime_incidents WHERE district IS NOT NULL
);

ALTER TABLE dim_police_district ADD PRIMARY KEY (police_district_code);

-- ---------------------------------------------------------------------------
-- 3.4 xref_neighborhood_district_purity — DERIVED crosswalk (not hardcoded):
--     for each police district, the modal 311 neighborhood value and what
--     fraction of that district's 311 cases agree with it. Replaces a
--     hand-built name-mapping table; carries a confidence score per row.
--     See DataConnections.txt section 1d for the source finding (A7/A15 are
--     100% pure; D4 is only 47.7% pure and should not be treated as 1:1).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE xref_neighborhood_district_purity AS
WITH counts AS (
    SELECT police_district, neighborhood_raw AS neighborhood, COUNT(*) AS case_count
    FROM service_requests_311
    WHERE police_district IS NOT NULL AND neighborhood_raw IS NOT NULL
    GROUP BY 1, 2
),
totals AS (
    SELECT police_district, SUM(case_count) AS district_total
    FROM counts
    GROUP BY 1
),
ranked AS (
    SELECT c.*, t.district_total,
           ROW_NUMBER() OVER (PARTITION BY c.police_district ORDER BY c.case_count DESC) AS rn
    FROM counts c JOIN totals t USING (police_district)
)
SELECT
    police_district AS police_district_code,
    neighborhood,
    case_count,
    district_total,
    ROUND(100.0 * case_count / district_total, 1) AS purity_pct
FROM ranked
WHERE rn = 1;

-- ---------------------------------------------------------------------------
-- 3.5 dim_street — canonical street names parsed out of every free-text
--     address column. Normalization: upper-case, strip leading house number/
--     range, collapse whitespace, standardize common suffix abbreviations.
--     Extend the suffix map as new abbreviations are observed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE MACRO normalize_street(raw_street) AS (
    TRIM(REGEXP_REPLACE(
        REGEXP_REPLACE(
            REGEXP_REPLACE(UPPER(TRIM(raw_street)), '^[0-9][0-9A-Z\-]*\s+', ''),  -- strip leading house number/range
            '\s+', ' ', 'g'),
        '\bSTREET\b', 'ST', 'g'))
);

CREATE OR REPLACE TABLE dim_street AS
SELECT
    ROW_NUMBER() OVER (ORDER BY street_name_canonical) AS street_id,
    street_name_canonical
FROM (
    SELECT DISTINCT normalize_street(location_street_name) AS street_name_canonical
    FROM service_requests_311 WHERE location_street_name IS NOT NULL
    UNION
    SELECT DISTINCT normalize_street(street) FROM crime_incidents WHERE street IS NOT NULL
    UNION
    SELECT DISTINCT normalize_street(address) FROM food_inspections WHERE address IS NOT NULL
    UNION
    SELECT DISTINCT normalize_street(address) FROM licenses_board WHERE address IS NOT NULL
    UNION
    SELECT DISTINCT normalize_street(address) FROM licenses_entertainment WHERE address IS NOT NULL
    UNION
    SELECT DISTINCT normalize_street(address) FROM open_space WHERE address IS NOT NULL
) s
WHERE street_name_canonical <> '';

ALTER TABLE dim_street ADD PRIMARY KEY (street_id);

-- ---------------------------------------------------------------------------
-- 3.6 dim_address — canonical civic addresses, one row per (street, house
--     number, zip) triple seen across all sources, with best-available point
--     geometry. This is the join spine the graph-layer design (see
--     DataConnections.txt section 6) expects an ADDRESS node to be.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE dim_address AS
WITH parsed AS (
    SELECT
        's311'                                                       AS source_system,
        REGEXP_EXTRACT(location_street_name, '^[0-9][0-9A-Z\-]*', 0) AS house_number,
        normalize_street(location_street_name)                      AS street_name_canonical,
        location_zipcode                                            AS zip,
        latitude AS lat, longitude AS lon
    FROM service_requests_311
    WHERE location_street_name IS NOT NULL

    UNION ALL
    SELECT
        'food',
        REGEXP_EXTRACT(address, '^[0-9][0-9A-Z\-]*', 0),
        normalize_street(address),
        zip,
        lat, lon
    FROM food_inspections
    WHERE address IS NOT NULL

    UNION ALL
    SELECT
        'lic_board',
        REGEXP_EXTRACT(address, '^[0-9][0-9A-Z\-]*', 0),
        normalize_street(address),
        zip,
        ST_Y(geom), ST_X(geom)
    FROM licenses_board
    WHERE address IS NOT NULL

    UNION ALL
    SELECT
        'lic_ent',
        REGEXP_EXTRACT(address, '^[0-9][0-9A-Z\-]*', 0),
        normalize_street(address),
        zip,
        ST_Y(geom), ST_X(geom)
    FROM licenses_entertainment
    WHERE address IS NOT NULL
)
SELECT
    ROW_NUMBER() OVER (ORDER BY p.street_name_canonical, p.house_number, p.zip) AS address_id,
    st.street_id,
    p.house_number,
    p.zip,
    ARG_MAX(p.lat, (p.lat IS NOT NULL))  AS lat,   -- prefer a non-null geocode among duplicates
    ARG_MAX(p.lon, (p.lon IS NOT NULL))  AS lon,
    ARG_MAX(p.source_system, (p.lat IS NOT NULL)) AS source_system
FROM parsed p
JOIN dim_street st ON st.street_name_canonical = p.street_name_canonical
GROUP BY st.street_id, p.house_number, p.zip, p.street_name_canonical;

ALTER TABLE dim_address ADD PRIMARY KEY (address_id);
-- FK: dim_address.street_id REFERENCES dim_street(street_id)

-- ---------------------------------------------------------------------------
-- 3.7 dim_establishment + xref_establishment_source — resolve one canonical
--     business/location entity across food_inspections, licenses_board and
--     licenses_entertainment, which share NO common license-number format
--     (see DataConnections.txt section 2). Matching is tiered by confidence;
--     nothing below match_confidence 0.85 should be trusted without review.
-- ---------------------------------------------------------------------------

-- 3.7a Per-source candidate rows, normalized for matching. DISTINCT here
--     matters beyond dedup-for-its-own-sake: the raw licensing_board and
--     licenses_entertainment CSVs contain a small number of exact duplicate
--     rows (same license_num, same address, same zip — e.g. LB-130760 is
--     listed twice) which would otherwise violate the primary key below.
CREATE OR REPLACE TABLE _establishment_candidates AS
SELECT DISTINCT 'food_inspections' AS source_system, licenseno AS source_key,
       UPPER(TRIM(businessname)) AS name_norm, address, zip, property_id
FROM food_inspections
UNION
SELECT DISTINCT 'licenses_board', license_num,
       UPPER(TRIM(COALESCE(dba_name, business_name))), address, zip, NULL
FROM licenses_board
UNION
SELECT DISTINCT 'licenses_entertainment', license_num,
       UPPER(TRIM(COALESCE(dba_name, business_name))), address, zip, NULL
FROM licenses_entertainment;

-- 3.7b Tiered match: exact address+zip is the deterministic tier; fuzzy name
--      match is the stretch-goal tier (see additional_dd section 11: "do not
--      fuzzy-link by default" — kept here but gated by a confidence column so
--      callers can filter it out).
CREATE OR REPLACE TABLE _establishment_groups AS
SELECT
    address, zip,
    ROW_NUMBER() OVER (ORDER BY address, zip) AS establishment_id
FROM (SELECT DISTINCT address, zip FROM _establishment_candidates WHERE address IS NOT NULL);

-- Scalar subqueries (not a join+GROUP BY) so this is guaranteed exactly one
-- output row per establishment_id: a join on street+zip alone (without also
-- pinning the house number) fans out across every address on that street and
-- re-duplicates establishment_id, which is what a plain join here produced.
CREATE OR REPLACE TABLE dim_establishment AS
SELECT
    g.establishment_id,
    (SELECT c.name_norm FROM _establishment_candidates c
      WHERE c.address = g.address AND c.zip = g.zip
      ORDER BY c.source_system LIMIT 1)                        AS canonical_name,
    (SELECT a.address_id FROM dim_address a
      WHERE a.zip = g.zip
        AND a.house_number = REGEXP_EXTRACT(g.address, '^[0-9][0-9A-Z\-]*', 0)
        AND a.street_id = (SELECT street_id FROM dim_street
                             WHERE street_name_canonical = normalize_street(g.address))
      LIMIT 1)                                                  AS address_id,
    (SELECT c.property_id FROM _establishment_candidates c
      WHERE c.address = g.address AND c.zip = g.zip AND c.property_id IS NOT NULL
      LIMIT 1)                                                  AS property_id
FROM _establishment_groups g;

ALTER TABLE dim_establishment ADD PRIMARY KEY (establishment_id);

CREATE OR REPLACE TABLE xref_establishment_source AS
SELECT
    g.establishment_id,
    c.source_system,
    c.source_key,
    CASE WHEN c.property_id IS NOT NULL THEN 'property_id_match'
         ELSE 'exact_address_zip' END           AS match_method,
    CASE WHEN c.property_id IS NOT NULL THEN 1.0 ELSE 0.9 END AS match_confidence
FROM _establishment_groups g
JOIN _establishment_candidates c ON c.address = g.address AND c.zip = g.zip;

ALTER TABLE xref_establishment_source
    ADD PRIMARY KEY (establishment_id, source_system, source_key);

DROP TABLE _establishment_candidates;
DROP TABLE _establishment_groups;

-- NOTE: this ships only the deterministic exact-address+zip tier. A fuzzy
-- name+address tier (jaro_winkler_similarity(name_norm, ...) with a
-- match_confidence < 0.9) is a stretch goal per additional_dd — add it as an
-- appended INSERT into xref_establishment_source once a similarity threshold
-- has been validated against a labeled sample, not by default.

-- =============================================================================
-- 4. FOREIGN KEY / RELATIONSHIP SUMMARY (documented, not all enforced above
--    because DuckDB FK enforcement across CREATE OR REPLACE rebuilds is
--    fragile — validate with the ANALYZE queries below instead)
-- =============================================================================
--   service_requests_311.police_district   -> dim_police_district.police_district_code
--   crime_incidents.district               -> dim_police_district.police_district_code
--   xref_neighborhood_district_purity.police_district_code -> dim_police_district
--   dim_address.street_id                  -> dim_street.street_id
--   dim_establishment.address_id           -> dim_address.address_id
--   xref_establishment_source.establishment_id -> dim_establishment.establishment_id
--   dim_neighborhood.neighborhood_id       -> (self, canonical)
--   xref_neighborhood_alias.neighborhood_id -> dim_neighborhood.neighborhood_id

-- =============================================================================
-- 5. VALIDATION QUERIES — run after load to sanity-check the derived layer
-- =============================================================================
-- SELECT COUNT(*) FROM dim_street;                                  -- expect a few thousand
-- SELECT COUNT(*) FROM dim_address;                                 -- expect ~100k-300k
-- SELECT COUNT(*) FROM dim_establishment;                           -- expect low thousands
-- SELECT police_district_code, neighborhood, purity_pct
--   FROM xref_neighborhood_district_purity ORDER BY purity_pct;     -- lowest-confidence districts first
-- SELECT match_method, COUNT(*), AVG(match_confidence)
--   FROM xref_establishment_source GROUP BY 1;
