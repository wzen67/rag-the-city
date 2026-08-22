"""Dataset registry and the read options each file actually needs.

Shared with the SQL layer (Role A) — the parse options here are not
cosmetic. Two of them prevent silently wrong answers:

* ``open-space`` must be read with ``strict_mode=false``. Reading it with
  ``ignore_errors=true`` parses only 272 of 577 rows and undercounts
  Boston's parkland by 60% (2,327 vs 5,862 acres) — with no error raised.
* ``property-assessment`` has a header field with literal surrounding
  spaces (`` GROSS_TAX ``), so column names must be stripped on load.

Verified against the files committed in ``data/`` on 2026-08-22.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@dataclass(frozen=True)
class Dataset:
    """One source file plus everything needed to read it correctly."""

    key: str
    filename: str
    read_opts: str
    grain: str
    geography: str
    note: str = ""
    #: Low-cardinality columns whose actual values belong in the prompt.
    #: A model that guesses ``lu_desc = 'RESIDENTIAL'`` silently matches
    #: zero rows, because the real values are 'RESIDENTIAL CONDO',
    #: 'SINGLE FAM DWELLING', and so on.
    categoricals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Keys become bare SQL view names, so they must be valid
        # identifiers. "311" is not — it is a syntax error at CREATE VIEW.
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.key):
            raise ValueError(
                f"dataset key {self.key!r} is not a valid SQL identifier; "
                "use lowercase letters, digits and underscores, not starting "
                "with a digit"
            )

    @property
    def path(self) -> Path:
        return DATA_DIR / self.filename

    def scan(self) -> str:
        """A DuckDB table expression for this file."""
        opts = f", {self.read_opts}" if self.read_opts else ""
        return f"read_csv_auto('{self.path.as_posix()}'{opts})"


REGISTRY: dict[str, Dataset] = {
    d.key: d
    for d in (
        Dataset(
            key="service_requests",
            filename="311-service-requests.csv.gz",
            read_opts="",
            grain="one service request",
            geography="neighborhood, police_district, location_zipcode, latitude/longitude",
            note=(
                "The geography hub: carries neighborhood, police district, ZIP, ward "
                "and coordinates on every row. Its own neighborhood labels are "
                "non-canonical (see rag.neighborhoods)."
            ),
            categoricals=("on_time", "case_status", "queue", "reason"),
        ),
        Dataset(
            key="crime",
            filename="crime-incident-reports-august-2015-to-date-source-new-system.csv.gz",
            read_opts="ignore_errors=true",
            grain="one reported incident",
            geography="DISTRICT (police), Lat/Long",
            note=(
                "OFFENSE_CODE_GROUP and UCR_PART are 100% EMPTY across all 290,130 "
                "rows (2023-2026), so violent-vs-property cannot be derived from this "
                "file alone; it needs the offense-code lookup joined in. "
                "OFFENSE_DESCRIPTION mixes real crime with non-crime police activity "
                "('SICK ASSIST', 'INVESTIGATE PERSON'), so a bare count(*) overstates "
                "crime badly. Despite the filename it holds 2023 onward, not 2015."
            ),
            categoricals=("DISTRICT", "SHOOTING", "YEAR"),
        ),
        Dataset(
            key="food",
            filename="food-establishment-inspections.csv.gz",
            read_opts="ignore_errors=true",
            grain="one violation line on one inspection",
            geography="zip, address, property_id",
            note=(
                "896,379 rows — never bulk-embed. Records violation codes and "
                "dispositions, never narrative reasons, so 'why did it fail?' is "
                "correctly unanswerable. No data dictionary exists on the portal."
            ),
            categoricals=("result", "viol_level", "licstatus"),
        ),
        Dataset(
            key="property",
            filename="property-assessment.csv.gz",
            read_opts="normalize_names=true",
            grain="one parcel-building record",
            geography="ZIP_CODE, ST_NUM/ST_NAME, CITY",
            note=(
                "Header ' GROSS_TAX ' has literal surrounding spaces; "
                "normalize_names strips them — but note it also lowercases every "
                "column here, so write property columns lowercase (gross_tax, "
                "zip_code, total_value) while crime stays uppercase. "
                "EVERY money column (total_value, land_value, bldg_value, "
                "sfyi_value, gross_tax) is VARCHAR with comma thousands "
                "separators ('822,900') and gross_tax also carries a '$' prefix, "
                "so arithmetic needs "
                "TRY_CAST(replace(replace(col,',',''),'$','') AS DOUBLE). "
                "lu_desc separates residential from commercial, which matters "
                "before averaging assessed values."
            ),
            categoricals=("lu_desc", "own_occ", "city"),
        ),
        Dataset(
            key="open_space",
            filename="open-space.csv.gz",
            read_opts="strict_mode=false",
            grain="one park or open-space site",
            geography="DISTRICT (neighborhood-style name), ZipCode",
            note=(
                "MUST use strict_mode=false. ignore_errors=true silently yields 272 "
                "of 577 rows and undercounts parkland by 60%."
            ),
            categoricals=("TypeLong", "OWNERSHIP"),
        ),
        Dataset(
            key="neighborhoods",
            filename="bpda-neighborhood-boundaries.csv.gz",
            read_opts="",
            grain="one BPDA neighborhood",
            geography="name",
            note=(
                "Canonical 26-neighborhood list. Its shape_wkt column is EMPTY — the "
                "usable geometry is in boston_neighborhood_boundaries.json.gz "
                "(WGS84 MultiPolygons), which is what point-in-polygon must use."
            ),
        ),
    )
}

#: GeoJSON polygons for exact point-in-neighborhood assignment.
BOUNDARIES_GEOJSON = DATA_DIR / "boston_neighborhood_boundaries.json.gz"


def get(key: str) -> Dataset:
    if key not in REGISTRY:
        raise KeyError(f"unknown dataset {key!r}; have {sorted(REGISTRY)}")
    return REGISTRY[key]


REPO_ROOT = DATA_DIR.parent
VIEWS_SQL = REPO_ROOT / "sql" / "views.sql"

#: The view names Role A's script defines. This is the integration
#: contract between the SQL layer and everything downstream.
VIEWS = (
    "neighborhoods",
    "crime",
    "crime_only",
    "svc311",
    "food_inspections",
    "food_violations",
    "property",
    "property_homes",
    "licenses",
    "entertainment_licenses",
    "open_space",
)


def connect_views() -> "duckdb.DuckDBPyConnection":
    """Build a connection with Role A's cleaned views from sql/views.sql.

    Prefer this over ``connect()``: the views handle every known data trap
    internally — the empty crime classification columns, non-crime rows,
    the parkland undercount, the VARCHAR money columns — so querying them
    gives correct numbers by default.

    The script uses paths relative to the repo root, so we run it from
    there regardless of the caller's working directory.
    """
    import os

    import duckdb

    if not VIEWS_SQL.exists():
        raise FileNotFoundError(f"{VIEWS_SQL} not found; run from a full checkout")

    con = duckdb.connect()
    prev = os.getcwd()
    try:
        os.chdir(REPO_ROOT)
        con.execute(VIEWS_SQL.read_text(encoding="utf-8"))
    finally:
        os.chdir(prev)
    return con


def connect(keys: list[str] | None = None) -> "duckdb.DuckDBPyConnection":
    """Return a connection with one view per dataset, named by key.

    Generated SQL should say ``FROM crime``, not repeat a
    ``read_csv_auto`` expression with the right options — getting those
    options wrong is exactly how the parkland undercount happens. Views
    make the correct read the only read.

    Role A's richer views (canonical neighborhood via spatial join,
    offense-code dimension joins) are expected to replace these; the view
    *names* are the contract.
    """
    import duckdb  # local import so the module stays importable without it

    con = duckdb.connect()
    for key in keys or list(REGISTRY):
        con.sql(f"CREATE OR REPLACE VIEW {key} AS SELECT * FROM {get(key).scan()}")
    return con
