# Data Dictionary — `main` branch

Every number below was measured against the actual files in `data/`, not from
documentation. Where the city's published dictionary disagrees with the file,
**the file wins and the disagreement is noted** — those disagreements are the
most dangerous thing in this dataset.

Profiled with DuckDB 1.5.5. DuckDB reads `.csv.gz` directly, no decompression.
**All paths below are relative to the repo root**, not to this `data/` folder:

```sql
INSTALL spatial; LOAD spatial;
SELECT * FROM read_csv_auto('data/crime-incident-reports-august-2015-to-date-source-new-system.csv.gz');
```

---

## What's in the branch

| File | Rows | Cols | Grain |
|---|---:|---:|---|
| `crime-incident-reports-...csv.gz` | 290,130 | 17 | one row per incident |
| `311-service-requests.csv.gz` | 78,526 | 30 | one row per service request |
| `food-establishment-inspections.csv.gz` | 896,379 | 26 | **one row per violation**, not per inspection |
| `property-assessment.csv.gz` | 184,552 | 67 | one row per parcel / condo unit (FY2026) |
| `licensing-board-licenses.csv.gz` | 3,659 | 26 | one row per active license |
| `entertainment-licenses-legacy.csv.gz` | 1,246 | 17 | one row per active license |
| `open-space.csv.gz` | 272 | 27 | one row per park/open space |
| `boston_neighborhood_boundaries.json.gz` | 26 | — | one polygon per neighborhood ✅ |
| `bpda-neighborhood-boundaries.csv.gz` | 26 | 7 | ❌ **geometry column empty — do not use** |

### Branch note

**Work from `main`.** `main` is a strict superset of `data2` — same eight files
plus `property-assessment.csv.gz` (merged in PR #2) and the PRD. `data2` has
nothing `main` lacks; treat it as retired.

### Two gaps against the PRD

1. **311 is 2026 only** — Jan 1 to Aug 20, 2026. No 2025. Year-over-year 311
   questions are unanswerable; only crime has multi-year history (2023–2026).
   The equity-gap metric still works (property value is a snapshot, not a
   trend), but it can only be stated for 2026.
2. **Two datasets nobody planned for**: licensing board licenses and
   entertainment licenses. Both are usable and both are geocoded (see the
   State Plane warning below).

---

## Canonical geography — use this everywhere

Do not use any dataset's own neighborhood column. Use the GeoJSON polygons.

```sql
CREATE TABLE hoods AS
SELECT name, geom FROM ST_Read('data/boston_neighborhood_boundaries.json.gz');
-- 26 polygons
```

The 26 canonical names: Allston, Back Bay, Bay Village, Beacon Hill, Brighton,
Charlestown, Chinatown, Dorchester, Downtown, East Boston, Fenway, Harbor
Islands, Hyde Park, Jamaica Plain, Leather District, Longwood, Mattapan,
Mission Hill, North End, Roslindale, Roxbury, South Boston, South Boston
Waterfront, South End, West End, West Roxbury.

**Measured join coverage:**

| Dataset | Matched | Of total | Of rows that have coordinates |
|---|---:|---:|---:|
| crime | 276,781 | 95.40% | 99.96% |
| 311 | 78,306 | 99.72% | 99.97% |
| licensing board | 3,456 | 94.45% | 96.16% |

Crime's 95.40% is not a join failure — 13,237 rows (4.56%) have no coordinates
at all. Always report both denominators; quoting only 99.96% overstates
coverage of the full table.

---

## 1. Crime Incident Reports

**290,130 rows · 2023-01-01 → 2026-08-15 · one row per incident**

| Column | Meaning | Notes |
|---|---|---|
| `INCIDENT_NUMBER` | report id | not unique — one report can list several offenses |
| `OFFENSE_CODE` | numeric offense code | 121 distinct values |
| `OFFENSE_DESCRIPTION` | text description | 122 distinct pairs with code — see landmine 2 |
| `OFFENSE_CODE_GROUP` | ⛔ **100.00% empty** | documented by the city, absent from the file |
| `UCR_PART` | ⛔ **100.00% empty** | same |
| `DISTRICT` | police district | 12 real districts + junk, see landmine 3 |
| `REPORTING_AREA` | sub-district area | 10.58% null |
| `SHOOTING` | 0 / 1 | 1,939 shootings (0.67%) |
| `OCCURRED_ON_DATE` | timestamp | |
| `YEAR` `MONTH` `DAY_OF_WEEK` `HOUR` | pre-split date parts | `DAY_OF_WEEK` is space-padded (`'Friday   '`) — always `trim()` |
| `Lat` `Long` | WGS84 coordinates | 13,237 rows (4.56%) null |
| `Location` | `"(lat, long)"` string | redundant with Lat/Long |

**Rows per year:** 2023 → 78,055 · 2024 → 79,124 · 2025 → 81,162 · 2026 → 51,789

### Landmines

**1 — Most rows are not crimes.** The top two "offenses" are `INVESTIGATE
PERSON` (29,330) and `SICK ASSIST` (27,115). The file also carries traffic
accidents, medical assists, sudden deaths, fire reports and landlord-tenant
disputes — reports that exist, but where no offence was committed.

⚠️ **The size of the error depends on where you draw the line, so quote only
the number the committed classifier produces.** `sql/views.sql` defines
`offense_dim.crime_class`; against that classifier:

```
naive count(*)     290,130
crime_only         146,933      naive overstates by 97.5%
```

Loosening the classifier (counting accidents and medical calls as crime)
drops this to roughly 59%. That is a 40-point swing driven purely by a
definition, so **never state a crime total without pointing at the
classifier**, and expect a judge to ask where the line is. The defensible
answer is that the rule is committed SQL anyone can read and change — not
that a single number is objectively right.

The overstatement also varies sharply by neighborhood, which is the real
argument: naive counting ranks neighborhoods wrongly, not just totals.

| Neighborhood | naive | actual crime | overstated by |
|---|---:|---:|---:|
| Roslindale | 5,821 | 2,400 | +142.5% |
| West End | 4,738 | 1,957 | +142.1% |
| Beacon Hill | 3,143 | 1,391 | +126.0% |
| Mattapan | 9,472 | 4,314 | +119.6% |
| Fenway | 6,577 | 2,999 | +119.3% |

**2 — Code 111 has two spellings.** `MURDER, NON-NEGLIGENT MANSLAUGHTER` and
`MURDER, NON-NEGLIGIENT MANSLAUGHTER` (typo in the source). That's why there
are 122 code/description pairs for 121 codes. **Deduplicate to one row per
code before joining anything**, or every murder row doubles. The same problem
is much worse in the city's `rmsoffensecodes.xlsx`: 576 rows for 425 codes,
65 with conflicting names — joining it raw takes crime from 290,130 to
495,035 rows (+70.6%).

**3 — `DISTRICT` has junk keys**: `'External'` (404), `'Outside of'` (2), NULL
(532). Moot if you use the spatial join.

**4 — 2026 is partial.** Eight months (through Aug 15) vs. twelve for 2025.
Comparing raw counts produces a fake 36% decline. Either annualize or compare
Jan–Aug windows.

### Example queries

```sql
-- Run sql/views.sql first. These query the views, which already handle
-- the classification, the spatial join and the empty columns.

-- Q: How many crimes in Roxbury in 2025?
SELECT count(*) FROM crime_only WHERE neighborhood = 'Roxbury' AND year = 2025;

-- Q: Is crime in Roxbury getting better or worse?
-- NOTE: 2026 is Jan-Aug only, so compare like-for-like windows.
SELECT year, count(*) FROM crime_only
WHERE neighborhood = 'Roxbury' AND month <= 8
GROUP BY 1 ORDER BY 1;
```

---

## 2. 311 Service Requests

**78,526 rows · 2026-01-01 → 2026-08-20 · one row per request · 2026 ONLY**

| Column | Meaning | Notes |
|---|---|---|
| `case_enquiry_id` | unique request id | |
| `open_dt` `closed_dt` | opened / closed timestamps | `closed_dt` null on all 38,570 open cases (49.1%) |
| `sla_target_dt` | service-level target | basis for `on_time` |
| `on_time` | `ONTIME` 35,729 / `OVERDUE` 42,797 | pre-computed by the city — use it, don't recompute. **54.5% are overdue** |
| `case_status` | `Closed` 39,956 / `Open` 38,570 | |
| `closure_reason` | free text | useful for thematic retrieval |
| `subject` `reason` `type` | 3-level taxonomy | 45 distinct `reason`, 168 distinct `type` |
| `department` `queue` | owning dept | |
| `neighborhood` | ⚠️ **unreliable — see landmine** | |
| `police_district` `ward` `precinct` `city_council_district` | other geographies | |
| `location_zipcode` | ZIP | |
| `latitude` `longitude` | WGS84 | only 195 rows null (0.25%) |
| `geom_4326` | EWKB point | already a geometry — usable directly |
| `source` | how it was filed | |

**Response time** = `closed_dt - open_dt`, computed only on closed cases.

### Landmines

**1 — The `neighborhood` column contradicts itself.** 23 distinct values that
overlap and disagree: `Allston` *and* `Allston / Brighton`; `Mattapan` *and*
`Greater Mattapan`; `South Boston` *and* `South Boston / South Boston
Waterfront`; `Fenway / Kenmore / Audubon Circle / Longwood` as one bucket.
`Chestnut Hill` is not a Boston neighborhood. 4,990 rows are null, blank, or
the meaningless value `Boston`. **Replace it with the spatial join** — 99.72%
coverage, 26 clean names.

**2 — One year only.** Any "compared to last year" 311 question must abstain.
This is a legitimate UNANSWERABLE example.

**3 — Median response time is badly biased by open cases.** **49.1% of
requests are still open** and have no `closed_dt`. Computing a median over
closed cases only silently discards half the data — and the discarded half is
the slow half, so the median flatters the city substantially. Always report
the closed-case median *alongside* the open-case count, and never present it
as "the" response time.

### Example queries

```sql
-- Q: Which neighborhood has the worst 311 response time?
SELECT h.name,
       median(date_diff('hour', s.open_dt, s.closed_dt)) AS median_hours,
       count(*) FILTER (WHERE s.closed_dt IS NULL)       AS still_open
FROM svc311 s
JOIN hoods h ON ST_Contains(h.geom, ST_Point(s.longitude, s.latitude))
GROUP BY 1 ORDER BY 2 DESC;

-- Q: Rat complaints by neighborhood
SELECT h.name, count(*) FROM svc311 s
JOIN hoods h ON ST_Contains(h.geom, ST_Point(s.longitude, s.latitude))
WHERE s.type ILIKE '%rodent%' OR s.case_title ILIKE '%rat%'
GROUP BY 1 ORDER BY 2 DESC;
```

---

## 3. Food Establishment Inspections

**896,379 rows · 2006-04-04 → 2026-08-05 · one row per VIOLATION**

⚠️ **This is the single easiest table to get wrong.** 896,379 rows are not
896,379 inspections. An inspection that found five violations is five rows.

**Actual inspections = 219,732** (distinct `licenseno` + `resultdttm`).
Counting rows overstates inspections by **4.08×**.

| Column | Meaning | Notes |
|---|---|---|
| `businessname` `dbaname` | legal / trading name | |
| `licenseno` | establishment licence no. | **join key**; with `resultdttm` identifies an inspection |
| `licstatus` | `Active` / `Inactive` | |
| `licensecat` | licence category | `FS` = food service, etc. |
| `result` | inspection outcome | see values below |
| `resultdttm` | inspection timestamp | **part of the inspection key** |
| `violation` | violation code | |
| `viol_level` | severity `*` / `**` / `***` | 61,378 null; `-` (7,172) is not a severity |
| `violdesc` | violation text | good retrieval target |
| `viol_status` | `Fail` / resolved | |
| `comments` | inspector free text | best unstructured field in the whole branch |
| `address` `city` `state` `zip` | address | |
| `location` | `"(lat, lng)"` **string** | not two numeric columns — needs parsing |

**`result` distribution:** `HE_Fail` 374,655 · `HE_Pass` 285,526 · `HE_Filed`
93,195 · `HE_FailExt` 75,684 · `HE_Hearing` 27,813 · `HE_NotReq` 23,989.
Note these are violation-row counts, not inspection counts.

`viol_level` is conventionally minor / major / critical for `*` / `**` / `***`
— confirm against the city's published inspection guide before putting that
wording in an answer.

### Landmines

**1 — Grain.** Any per-inspection rate must deduplicate first:

```sql
WITH inspections AS (
  SELECT DISTINCT licenseno, resultdttm, result FROM food
)
SELECT result, count(*) FROM inspections GROUP BY 1;
```

**2 — Twenty years of history.** 2006–2026. Almost every question means
"recently" — filter the window or a 2009 failure counts against a restaurant
today.

**3 — Coordinates are a string.** `location` is `"(42.359, -71.058)"`. Parse
before any spatial work; there is no `latitude`/`longitude` pair.

---

## 4. Property Assessment (FY2026)

**184,552 rows · 67 columns · one row per parcel or condo unit**

⚠️ **No coordinates.** No `Lat`/`Long`, no `gpsx`/`gpsy`, no geometry. The only
geography is `ZIP_CODE` (and a `CITY` field that is not usable — see landmine
3). This is the one dataset that cannot use the canonical spatial join; it
needs a ZIP → neighborhood crosswalk. Getting coordinates would mean pulling
the separate Parcels dataset and joining on `PID_LONG`/`GIS_ID` — not worth it
inside the build window.

| Column | Meaning | Notes |
|---|---|---|
| `PID` | parcel id | primary key |
| `GIS_ID` | GIS parcel id | join key to the Parcels dataset (not in this repo) |
| `CM_ID` | condo main id | links a unit to its building |
| `ST_NUM` `ST_NAME` `UNIT_NUM` | street address parts | |
| `CITY` | ⚠️ **not the canonical 26** — see landmine 3 | |
| `ZIP_CODE` | ZIP | 38 distinct, only **4 null** — clean, use this |
| `LUC` `LU` `LU_DESC` | land-use code / short / description | **the most important filter in the table** |
| `OWN_OCC` | owner-occupied Y/N | Y 78,061 · N 106,491 |
| `OWNER` `MAIL_*` | ownership and mailing address | |
| `LAND_SF` `GROSS_AREA` `LIVING_AREA` | areas | `LAND_SF` is text |
| `LAND_VALUE` `BLDG_VALUE` `SFYI_VALUE` `TOTAL_VALUE` | assessed values | ⛔ **all text** |
| `GROSS_TAX` | annual tax | ⛔ text, formatted `" $10,203.96 "` |
| `YR_BUILT` `YR_REMODEL` | years | see landmine 4 |
| `RES_UNITS` `COM_UNITS` `RC_UNITS` `NUM_BLDGS` | unit counts | |
| `BED_RMS` `FULL_BTH` `HLF_BTH` `KITCHENS` `TT_RMS` | room counts | |
| `INT_COND` `EXT_COND` `OVERALL_COND` | condition ratings | `A - Average` style codes |
| `HEAT_TYPE` `AC_TYPE` `ROOF_*` `EXT_FNISHED` | building characteristics | coded `X - Label` |

**Top land uses:** `CD` Residential Condo 74,239 · `R1` Single Fam 30,439 ·
`R2` Two-Fam 16,701 · `R3` Three-Fam 13,169 · `CM` Condo Main 11,139 ·
`CP` **Condo Parking 8,545** · `RL` Res Land (Unusable) 4,103 · `R4` Apt 4-6
2,485 · `RC` Res/Commercial 2,323 · `E` Other Exempt 2,149.

### Landmines

**1 — Value columns are text, and it fails silently.** `TOTAL_VALUE`,
`LAND_VALUE`, `BLDG_VALUE`, `SFYI_VALUE`, `GROSS_TAX`, `LAND_SF`, `CD_FLOOR`
all carry embedded commas (`"822,900"`), so DuckDB types them `VARCHAR`.
Aggregates then sort alphabetically and return a confident wrong answer with
no error:

```
max(TOTAL_VALUE) as text  →       999,900
max(TOTAL_VALUE) cleaned  → $2,448,193,300
```

The naive query understates Boston's most valuable property by **2,450×**.
"What's the most valuable property in Boston?" is a perfect demo question:
naive RAG answers $999,900 and is off by two and a half billion dollars.

Clean in the view, once:

```sql
TRY_CAST(replace(replace(replace(TOTAL_VALUE, ',', ''), '$', ''), ' ', '') AS DOUBLE)
```

All 184,552 rows cast successfully with that expression — zero failures.

**2 — Not every row is a home.** A median over all rows silently includes
8,545 **condo parking spaces** (median value $44,000), 5,855 unusable land
parcels, and 11,146 rows valued at zero.

```
median, all 184,552 rows            → $671,000
median, residential + value > 0     → $742,300
```

Naive understates the typical Boston home by **$71,300 (9.6%)**. Filter
`LU IN ('R1','R2','R3','R4','CD')` and `value > 0` for anything describing
what a resident would actually buy. This is the same shape of error as
`SICK ASSIST` in the crime table — the row is real, it just isn't the thing
the question asked about.

**3 — `CITY` is not the canonical neighborhood.** 48,160 rows (26%) say
`BOSTON`, which swallows Back Bay, Beacon Hill, Fenway, North End, South End
and Downtown into one meaningless bucket. It also contains `ROXBURY CROSSING`
and `CHESTNUT HILL` (neither is one of the 26) and 32 rows in **Brookline,
Dedham, Newton and Readville** — outside Boston entirely. Use `ZIP_CODE` and a
crosswalk instead.

**4 — `YR_BUILT` has junk.** 22,560 rows (12.2%) are 0 or null, and one row
reads `20198`. Filter `YR_BUILT BETWEEN 1600 AND 2026` before any age
calculation.

### Example queries

```sql
-- Q: What is the median home value in each neighborhood?
-- NOTE: ZIP crosswalk, not spatial join - this table has no coordinates.
SELECT z.neighborhood,
       median(TRY_CAST(replace(replace(p.TOTAL_VALUE,',',''),'$','') AS DOUBLE)) AS median_value,
       count(*) AS n
FROM property p
JOIN zip_neighborhood z ON p.ZIP_CODE = z.zip
WHERE p.LU IN ('R1','R2','R3','R4','CD')
  AND TRY_CAST(replace(replace(p.TOTAL_VALUE,',',''),'$','') AS DOUBLE) > 0
GROUP BY 1 ORDER BY 2 DESC;

-- Q: What is the most valuable property in Boston?
SELECT OWNER, ST_NUM, ST_NAME, CITY,
       TRY_CAST(replace(replace(TOTAL_VALUE,',',''),'$','') AS DOUBLE) AS v
FROM property ORDER BY v DESC LIMIT 5;
```

---

## 5. Licensing Board Licenses

**3,659 rows · one row per ACTIVE license**

| Column | Meaning | Notes |
|---|---|---|
| `license_num` | id (`LB-…`) | |
| `status` | ⛔ **100% `Active`** | no history — see landmine |
| `license_category` | Common Victualler (2,647), Misc (829), Inn (102), Club (52), General on Premise (29) | |
| `license_type` | detailed type incl. alcohol privileges | |
| `issued` `expires` | dates | `issued` largely blank |
| `business_name` `dba_name` | names | |
| `capacity` `opening` `closing` `patronsout` | occupancy and hours | genuinely interesting, rare in open data |
| `descpremadd` | free-text premises description | strong retrieval target |
| `comments` | licence conditions, free text | |
| `address` `city` `zip` | address | |
| `gpsx` `gpsy` | ⚠️ **MA State Plane, not lat/long** | 65 null |

### Landmines

**1 — `gpsx`/`gpsy` are Massachusetts State Plane feet (EPSG:2249)**, values
like `764720`, `2940110`. Passing them to `ST_Point` as lat/long silently
matches nothing. Reproject:

```sql
ST_Transform(ST_Point(gpsx, gpsy), 'EPSG:2249', 'EPSG:4326', always_xy := true)
```

Verified: `(764720.25, 2940110.43)` → `(-71.0986, 42.3151)`, central Boston.
Coverage after reprojection: 3,456 / 3,659 = 94.45%.

**2 — Active licences only.** No revoked, expired, or historical rows. "How
many licences were revoked last year?" is **unanswerable from this data** —
a good abstention demo, and the honest answer is "this file contains only
currently-active licences."

---

## 6. Entertainment Licenses (Legacy)

**1,246 rows · one row per ACTIVE license**

`license_num` (`CAL-…`), `status` (⛔ 100% `Active`), `license_type`
— Non-Live Entertainment (860), Live Entertainment (224), Night Club (162) —
`issued`, `expires`, `business_name`, `dba_name`, `comments` (noise
restrictions and conditions, good free text), `address`/`city`/`zip`,
`gpsx`/`gpsy` (**same State Plane caveat**, 10 null).

"Legacy" means a retired system; treat as a historical snapshot, not a live
register. Same active-only landmine as above.

---

## 7. Open Space

**272 rows · one row per park / open space**

`SITE_NAME`, `OWNERSHIP`, `TYPECODE` / `TypeLong` (Parks, Playgrounds &
Athletic Fields; Parkways, Reservations & Beaches; …), `ACRES`, `ADDRESS`,
`DISTRICT` (its own neighborhood-ish grouping — not the canonical 26),
`ZipCode`, `OS_ID`, `shape_wkt`.

❌ **This file has no usable geometry** — correcting an earlier claim in this
document that 234 of 272 rows carried WKT. They do not. The header declares 27
fields but data rows carry 28 (an unquoted comma inside a text field), so the
parser shifts `Shape_Area` into `shape_wkt`. **Zero rows contain a POLYGON.**
Open space cannot be joined spatially from this CSV; it joins by `ZipCode`.

`DISTRICT` here is the source's own grouping (`Allston-Brighton`,
`Central Boston`), **not** one of the canonical 26.

Small enough to embed whole. Best used to answer "what parks are in X" and as
a green-space denominator per ZIP.

---

## 8. Neighborhood Boundaries — two files, use the right one

✅ **`boston_neighborhood_boundaries.json.gz`** — 26 polygons, real `geom`.
Read with `ST_Read`. **This is the one.**

❌ **`bpda-neighborhood-boundaries.csv.gz`** — same 26 neighborhoods, but
`shape_wkt` is **empty on all 26 rows**. It carries `name`, `acres`,
`neighborhood_id`, `sqmiles` and nothing spatial. Useful only as an area
lookup for density calculations. Anyone who tries to build point-in-polygon
from this file will lose 30 minutes to an empty result set.

---

## Cross-dataset joins

| From | To | Key | Note |
|---|---|---|---|
| any geocoded table | `hoods` | `ST_Contains(h.geom, ST_Point(lon, lat))` | the canonical join |
| licences | `hoods` | same, **after `ST_Transform`** | State Plane |
| food | `hoods` | parse `location` string first | |
| food | licences | `licenseno` ↔ `license_num` | different formats — needs normalising, verify before relying on it |
| open space | — | ⚠️ **no geometry — ZIP only** | `shape_wkt` is empty/shifted |
| **property** | `hoods` | ⚠️ **ZIP crosswalk only — no coordinates** | the one exception to the spatial join |
| property | 311 / food | `ZIP_CODE` ↔ `location_zipcode` / `zip` | the equity-gap join |

Every dataset except property assessment geocodes to the same 26 polygons,
which is what makes a neighborhood scorecard possible. Property is the one
exception and joins by ZIP; say so in any answer that mixes it with the
others, because the two geographies do not align exactly.

---

## Rules for the SQL-generation prompt

Copy these verbatim into the schema-grounding context:

1. Never select `OFFENSE_CODE_GROUP` or `UCR_PART` — both are 100% empty.
2. Never use 311's `neighborhood` column — use the spatial join.
3. Never use `bpda-neighborhood-boundaries.csv.gz` for geometry — it is empty.
4. Food rows are violations, not inspections — deduplicate on
   `licenseno` + `resultdttm` before any rate.
5. Deduplicate offence codes to one row per code before joining.
6. 311 covers 2026 only; crime 2026 is Jan–Aug. Never compare a partial year
   to a full one.
7. Licence tables contain active records only — no revocations, no history.
8. Licence `gpsx`/`gpsy` need `ST_Transform` from EPSG:2249.
9. Food `location` is a string, not numeric columns.
10. Property value/tax/area columns are text with commas — always `TRY_CAST`
    after stripping `,` and `$`. `max()` on the raw column returns 999,900.
11. Property medians must filter `LU IN ('R1','R2','R3','R4','CD')` and
    value > 0, or condo parking spaces drag the answer down.
12. Never use property `CITY` as a neighborhood — 26% of it is just `BOSTON`.
    Property joins by ZIP, never by spatial join.
13. Report coverage as a share of all rows *and* of geocoded rows.

---

*Profiled 2026-08-22 against `main` @ `cf62db4`. Every count above is
reproducible with the queries in this file.*
