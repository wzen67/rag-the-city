-- Clean views over the Boston datasets.
--
-- Every known data trap is handled INSIDE these views. Query the views and
-- you get correct numbers by default. Nobody has to remember the rules.
--
-- Run from the repo root:  duckdb boston.db < sql/views.sql
--
-- See data/DATA_DICTIONARY.md for why each rule exists.

INSTALL spatial; LOAD spatial;

-- ---------------------------------------------------------------------------
-- Geography. Every dataset with coordinates joins to these 26 polygons.
-- The bpda CSV is NOT used: its geometry column is empty on all 26 rows.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE TABLE neighborhoods AS
SELECT name AS neighborhood, geom
FROM ST_Read('data/boston_neighborhood_boundaries.json.gz');


-- ---------------------------------------------------------------------------
-- Crime
--   trap 1: OFFENSE_CODE_GROUP and UCR_PART are 100% empty -> not exposed
--   trap 2: most rows are not crimes (SICK ASSIST, INVESTIGATE PERSON...)
--   trap 3: DISTRICT has junk keys -> use the spatial join instead
--   trap 4: DAY_OF_WEEK is space padded
-- ---------------------------------------------------------------------------

CREATE OR REPLACE TABLE offense_dim AS
SELECT DISTINCT ON (OFFENSE_CODE)          -- one row per code: 111 has 2 spellings
    OFFENSE_CODE                AS offense_code,
    OFFENSE_DESCRIPTION         AS offense_description,
    CASE
        WHEN OFFENSE_DESCRIPTION LIKE '%HOMICIDE%'
          OR OFFENSE_DESCRIPTION LIKE 'MURDER%'
          OR OFFENSE_DESCRIPTION LIKE 'ASSAULT%'
          OR OFFENSE_DESCRIPTION LIKE '%ROBBERY%'
          OR OFFENSE_DESCRIPTION LIKE '%RAPE%'
          OR OFFENSE_DESCRIPTION LIKE '%KIDNAPPING%'
          OR OFFENSE_DESCRIPTION LIKE '%MANSLAUGHTER%'
            THEN 'violent'
        WHEN OFFENSE_DESCRIPTION LIKE '%LARCENY%'
          OR OFFENSE_DESCRIPTION LIKE '%BURGLARY%'
          OR OFFENSE_DESCRIPTION LIKE '%VANDALISM%'
          OR OFFENSE_DESCRIPTION LIKE '%ARSON%'
          OR OFFENSE_DESCRIPTION LIKE '%AUTO THEFT%'
          OR OFFENSE_DESCRIPTION LIKE '%PROPERTY DAMAGE%'
            THEN 'property'
        -- Not offences: police service calls, medical assists, accidents,
        -- and civil/administrative matters. A report exists, but no crime
        -- was committed. Note that LEAVING THE SCENE of an accident IS an
        -- offence and is deliberately not caught here.
        WHEN OFFENSE_DESCRIPTION LIKE 'INVESTIGATE%'
          OR OFFENSE_DESCRIPTION LIKE 'SICK ASSIST%'
          OR OFFENSE_DESCRIPTION LIKE 'SICK/INJURED/MEDICAL%'
          OR OFFENSE_DESCRIPTION LIKE '%SERVICE TO OTHER%'
          OR OFFENSE_DESCRIPTION LIKE 'TOWED%'
          OR OFFENSE_DESCRIPTION LIKE '%MISSING PERSON%'
          OR OFFENSE_DESCRIPTION LIKE 'PROPERTY - LOST%'
          OR OFFENSE_DESCRIPTION LIKE 'PROPERTY - FOUND%'
          OR OFFENSE_DESCRIPTION LIKE 'PROPERTY - ACCIDENTAL%'
          OR OFFENSE_DESCRIPTION LIKE '%WELL BEING%'
          OR (OFFENSE_DESCRIPTION LIKE 'M/V ACCIDENT%')
          OR OFFENSE_DESCRIPTION LIKE 'SUDDEN DEATH%'
          OR OFFENSE_DESCRIPTION LIKE 'DEATH INVESTIGATION%'
          OR OFFENSE_DESCRIPTION LIKE 'FIRE REPORT%'
          OR OFFENSE_DESCRIPTION LIKE 'LANDLORD - TENANT%'
          OR OFFENSE_DESCRIPTION LIKE 'VERBAL DISPUTE%'
          OR OFFENSE_DESCRIPTION LIKE '%ASSIST%CITIZEN%'
          OR OFFENSE_DESCRIPTION LIKE 'PRISONER%'
          OR OFFENSE_DESCRIPTION LIKE '%LICENSE PREMISE%'
          OR OFFENSE_DESCRIPTION LIKE 'SEARCH WARRANT%'
            THEN 'not_a_crime'
        ELSE 'other_crime'
    END AS crime_class
FROM read_csv_auto('data/crime-incident-reports-august-2015-to-date-source-new-system.csv.gz',
                   ignore_errors = true)
ORDER BY OFFENSE_CODE, OFFENSE_DESCRIPTION;

-- The neighborhood is a scalar subquery, not a join: 2 incidents fall inside
-- two overlapping BPDA polygons, and a plain join would return 290,132 rows
-- for a 290,130-row file.
CREATE OR REPLACE VIEW crime AS
SELECT
    c.INCIDENT_NUMBER            AS incident_number,
    c.OFFENSE_CODE               AS offense_code,
    c.OFFENSE_DESCRIPTION        AS offense_description,
    d.crime_class,
    d.crime_class <> 'not_a_crime' AS is_crime,
    c.OCCURRED_ON_DATE           AS occurred_on,
    c.YEAR                       AS year,
    c.MONTH                      AS month,
    trim(c.DAY_OF_WEEK)          AS day_of_week,
    c.HOUR                       AS hour,
    c.SHOOTING                   AS shooting,
    c.STREET                     AS street,
    c.Lat                        AS latitude,
    c.Long                       AS longitude,
    (SELECT n.neighborhood FROM neighborhoods n
      WHERE c.Lat IS NOT NULL
        AND ST_Contains(n.geom, ST_Point(c.Long, c.Lat)) LIMIT 1) AS neighborhood
FROM read_csv_auto('data/crime-incident-reports-august-2015-to-date-source-new-system.csv.gz',
                   ignore_errors = true) c
LEFT JOIN offense_dim d ON c.OFFENSE_CODE = d.offense_code;

-- Only actual crimes. Use this for "how much crime" questions.
CREATE OR REPLACE VIEW crime_only AS
SELECT * FROM crime WHERE is_crime;


-- ---------------------------------------------------------------------------
-- 311
--   trap 1: the neighborhood column contradicts itself -> replaced
--   trap 2: 2026 only, Jan-Aug
--   trap 3: 49.1% of cases are still open -> response time only on closed
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW svc311 AS
SELECT
    s.case_enquiry_id,
    s.open_dt,
    s.closed_dt,
    s.case_status,
    s.on_time,
    s.case_title,
    s.subject,
    s.reason,
    s.type,
    s.department,
    s.queue,
    s.closure_reason,
    s.location_zipcode        AS zipcode,
    s.latitude,
    s.longitude,
    (SELECT n.neighborhood FROM neighborhoods n
      WHERE s.latitude IS NOT NULL
        AND ST_Contains(n.geom, ST_Point(s.longitude, s.latitude)) LIMIT 1) AS neighborhood,
    s.closed_dt IS NULL       AS is_open,
    CASE WHEN s.closed_dt IS NOT NULL
         THEN date_diff('hour', s.open_dt, s.closed_dt)
    END                       AS hours_to_close
FROM read_csv_auto('data/311-service-requests.csv.gz', ignore_errors = true) s;


-- ---------------------------------------------------------------------------
-- Food inspections
--   trap: the raw file is one row per VIOLATION. 896,379 rows are only
--         219,732 inspections. Counting raw rows overstates by 4.08x.
-- ---------------------------------------------------------------------------

-- Violation grain: use for "what violations" questions.
CREATE OR REPLACE VIEW food_violations AS
SELECT
    businessname             AS business_name,
    dbaname                  AS dba_name,
    licenseno                AS license_no,
    licstatus                AS license_status,
    result,
    resultdttm               AS inspected_at,
    violation                AS violation_code,
    viol_level               AS violation_level,
    violdesc                 AS violation_description,
    viol_status              AS violation_status,
    comments,
    address, city, zip,
    TRY_CAST(regexp_extract(location, '\(([-0-9.]+),', 1) AS DOUBLE) AS latitude,
    TRY_CAST(regexp_extract(location, ',\s*([-0-9.]+)\)', 1) AS DOUBLE) AS longitude
FROM read_csv_auto('data/food-establishment-inspections.csv.gz', ignore_errors = true);

-- Inspection grain: one row per actual inspection (219,732). Use for any RATE.
-- The key is license_no + inspected_at ONLY. Adding other columns to a
-- DISTINCT inflates this to 220,375 because result varies within an
-- inspection.
CREATE OR REPLACE VIEW food_inspections AS
SELECT license_no, business_name, inspected_at, result,
       license_status, address, city, zip, latitude, longitude
FROM food_violations
QUALIFY row_number() OVER (PARTITION BY license_no, inspected_at
                           ORDER BY result) = 1;


-- ---------------------------------------------------------------------------
-- Property assessment
--   trap 1: value columns are TEXT with commas. max() sorts alphabetically
--           and returns 999,900 instead of 2,448,193,300.
--   trap 2: 8,545 condo parking spaces and 5,855 unusable land parcels are
--           not homes. Including them drags the median down by $71,300.
--   trap 3: CITY is not a neighborhood - 26% of it is just 'BOSTON'.
--   trap 4: YR_BUILT has zeros and one value of 20198.
--   No coordinates in this table. It joins by ZIP, never spatially.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW property AS
SELECT
    PID                                   AS pid,
    GIS_ID                                AS gis_id,
    ZIP_CODE                              AS zipcode,
    LU                                    AS land_use,
    LU_DESC                               AS land_use_desc,
    OWN_OCC                               AS owner_occupied,
    OWNER                                 AS owner,
    ST_NUM || ' ' || ST_NAME              AS street_address,
    CASE WHEN YR_BUILT BETWEEN 1600 AND 2026 THEN YR_BUILT END AS year_built,
    RES_UNITS                             AS res_units,
    BED_RMS                               AS bedrooms,
    FULL_BTH                              AS full_baths,
    TRY_CAST(replace(replace(replace(TOTAL_VALUE, ',', ''), '$', ''), ' ', '') AS DOUBLE) AS total_value,
    TRY_CAST(replace(replace(replace(LAND_VALUE, ',', ''), '$', ''), ' ', '') AS DOUBLE)  AS land_value,
    TRY_CAST(replace(replace(replace(BLDG_VALUE, ',', ''), '$', ''), ' ', '') AS DOUBLE)  AS bldg_value,
    TRY_CAST(replace(replace(replace(GROSS_TAX, ',', ''), '$', ''), ' ', '') AS DOUBLE)   AS gross_tax,
    TRY_CAST(replace(replace(LAND_SF, ',', ''), ' ', '') AS DOUBLE)                       AS land_sf,
    LU IN ('R1','R2','R3','R4','CD')      AS is_residential
FROM read_csv_auto('data/property-assessment.csv.gz', ignore_errors = true);

-- Actual homes with a real value. Use this for any "home value" question.
CREATE OR REPLACE VIEW property_homes AS
SELECT * FROM property WHERE is_residential AND total_value > 0;


-- ---------------------------------------------------------------------------
-- Licences
--   trap 1: gpsx/gpsy are MA State Plane FEET (EPSG:2249), not lat/long.
--   trap 2: every row is status='Active'. There is no history here, so
--           questions about revocations cannot be answered.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW licenses AS
SELECT
    l.license_num, l.status, l.license_category, l.license_type,
    l.business_name, l.dba_name, l.expires,
    TRY_CAST(l.capacity AS INTEGER) AS capacity,
    l.opening, l.closing, l.descpremadd AS premises_description, l.comments,
    l.address, l.city, l.zip AS zipcode,
    n.neighborhood
FROM read_csv_auto('data/licensing-board-licenses.csv.gz',
                   ignore_errors = true, all_varchar = true) l
LEFT JOIN neighborhoods n
       ON TRY_CAST(l.gpsx AS DOUBLE) IS NOT NULL
      AND ST_Contains(n.geom,
            ST_Transform(ST_Point(TRY_CAST(l.gpsx AS DOUBLE), TRY_CAST(l.gpsy AS DOUBLE)),
                         'EPSG:2249', 'EPSG:4326', always_xy := true));

CREATE OR REPLACE VIEW entertainment_licenses AS
SELECT
    e.license_num, e.status, e.license_type,
    e.business_name, e.dba_name, e.expires, e.comments,
    e.address, e.city, e.zip AS zipcode,
    n.neighborhood
FROM read_csv_auto('data/entertainment-licenses-legacy.csv.gz',
                   ignore_errors = true, all_varchar = true) e
LEFT JOIN neighborhoods n
       ON TRY_CAST(e.gpsx AS DOUBLE) IS NOT NULL
      AND ST_Contains(n.geom,
            ST_Transform(ST_Point(TRY_CAST(e.gpsx AS DOUBLE), TRY_CAST(e.gpsy AS DOUBLE)),
                         'EPSG:2249', 'EPSG:4326', always_xy := true));


-- ---------------------------------------------------------------------------
-- Open space
--   trap: this CSV has NO usable geometry. Its header declares 27 fields but
--         data rows carry 28 (an unquoted comma inside a text field), so the
--         parser shifts Shape_Area into shape_wkt. Zero rows contain a real
--         POLYGON. Verified: no spatial join is possible from this file.
--         Joins by ZipCode. DISTRICT is the source's own grouping, NOT one
--         of the canonical 26 neighborhoods.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW open_space AS
SELECT
    SITE_NAME  AS site_name,
    OWNERSHIP  AS ownership,
    TypeLong   AS space_type,
    TRY_CAST(ACRES AS DOUBLE) AS acres,
    ADDRESS    AS address,
    ZipCode    AS zipcode,
    DISTRICT   AS source_district
-- strict_mode=false, NOT ignore_errors: ignore_errors silently parses only
-- 272 of 577 rows and undercounts parkland by 60% (2,327 vs 5,862 acres).
FROM read_csv_auto('data/open-space.csv.gz', strict_mode = false, all_varchar = true);
