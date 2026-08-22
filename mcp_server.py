"""FastMCP catalog server for the derived Boston CSV entities.

Derived tables are streamed from the public Oracle Object Storage bucket
(rag-the-city, us-ashburn-1) rather than read from data/derived/ on disk.

Run with:
    conda run -n nn uvicorn mcp_server:app --host 127.0.0.1 --port 3000
"""

from __future__ import annotations

import csv
import io
import json
import os
from contextlib import contextmanager
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

import requests
from fastmcp import FastMCP


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DICTIONARY = DATA / "dd.md"

OBJECT_STORAGE_BASE = (
    "https://objectstorage.us-ashburn-1.oraclecloud.com"
    "/n/idn7xwcj4g1k/b/rag-the-city/o"
)


def object_url(table: str) -> str:
    object_name = OBJECT_STORAGE_NAMES.get(table, table)
    return f"{OBJECT_STORAGE_BASE}/{object_name}.csv"


@contextmanager
def open_table_stream(table: str) -> Iterator[io.TextIOWrapper]:
    """Stream one derived table's CSV object from Oracle Object Storage.

    Mirrors ``path.open(newline="", encoding="utf-8-sig")`` on a local file,
    so csv.reader/DictReader behave identically over the network stream.
    """
    response = requests.get(object_url(table), stream=True, timeout=60)
    response.raise_for_status()
    response.raw.decode_content = True
    # urllib3 auto-releases (and marks closed) the connection the moment it
    # sees EOF; leave the last "did we hit EOF" read to TextIOWrapper instead
    # of urllib3, or its next read raises "I/O operation on closed file".
    response.raw.auto_close = False
    wrapper = io.TextIOWrapper(response.raw, encoding="utf-8-sig", newline="")
    try:
        yield wrapper
    finally:
        response.close()


TABLE_SOURCES = {
    "service_requests_311": "311-service-requests.csv",
    "service_requests_311_geography": "derived direct polygon assignment for 311 cases",
    "police_district_neighborhood_purity": "derived 311 district fallback crosswalk",
    "service_request_reasons_311": "311_reasons.csv (derived aggregate)",
    "service_request_types_311": "311_types.csv (derived aggregate)",
    "crime_incidents": "crime-incident-reports-august-2015-to-date-source-new-system.csv",
    "crime_incidents_geography": "derived direct polygon assignment for crime incidents",
    "offense_dim": "official RMS offense-code dimension with reviewed crime classes",
    "offense_codes": "offense_codes.csv (raw RMS offense-code dimension, pre-classification)",
    "occupancy_codes": "occupancy_codes.csv (property occupancy code dimension)",
    "licenses_board": "licensing-board-licenses.csv",
    "licenses_entertainment": "entertainment-licenses-legacy.csv",
    "open_spaces": "open-space.csv",
    "neighborhoods_bpda": "bpda-neighborhood-boundaries.csv",
    "food_establishments": "food-establishment-inspections.csv (derived)",
    "food_inspections": "food-establishment-inspections.csv (derived)",
    "food_violations": "food-establishment-inspections.csv (derived)",
}

# The descriptive aggregates are uploaded with names beginning ``311_``.
# Present SQL-safe identifiers to clients while preserving those object names.
OBJECT_STORAGE_NAMES = {
    "service_request_reasons_311": "311_reasons",
    "service_request_types_311": "311_types",
}

TABLE_DESCRIPTIONS = {
    "service_requests_311": "One row per 311 service request.",
    "service_requests_311_geography": (
        "One direct BPDA point-in-polygon assignment per 311 case. "
        "Use this for canonical neighborhood attribution; unresolved coordinates remain explicit."
    ),
    "police_district_neighborhood_purity": (
        "One modal canonical-neighborhood fallback per police district, derived from "
        "direct 311 polygon assignments. Use only when a record has no usable coordinates; "
        "report purity_pct and spatial_coverage_pct."
    ),
    "service_request_reasons_311": "Citywide 311 request counts aggregated by reason.",
    "service_request_types_311": "Citywide 311 request counts aggregated by request type.",
    "crime_incidents": "One row per crime incident/offense record.",
    "crime_incidents_geography": (
        "One direct BPDA point-in-polygon assignment per crime incident. "
        "Use the district fallback only when geography_method is missing_or_invalid_coordinates."
    ),
    "offense_dim": (
        "Official RMS offense-code names classified once at build time. "
        "Join on offense_code; unclassified codes must not be counted as crimes."
    ),
    "offense_codes": (
        "One row per raw RMS offense code and name, without crime-class review. "
        "Prefer offense_dim for classified joins; this is the unclassified source dimension."
    ),
    "occupancy_codes": "One row per property occupancy code, mapping code to a human-readable description.",
    "food_establishments": "One row per food establishment/property location.",
    "food_inspections": "One row per distinct food inspection visit.",
    "food_violations": "One row per violation reported during a food inspection.",
    "licenses_board": "One row per active licensing-board license.",
    "licenses_entertainment": "One row per entertainment license.",
    "open_spaces": "One row per open-space feature.",
    "neighborhoods_bpda": "One row per BPDA neighborhood summary; geometry is empty in this CSV.",
}

TYPE_OVERRIDES = {
    "establishment_id": "VARCHAR",
    "inspection_id": "VARCHAR",
    "violation_id": "VARCHAR",
    "latitude": "DOUBLE",
    "longitude": "DOUBLE",
    "gpsx": "DOUBLE",
    "gpsy": "DOUBLE",
}

GENERATED_TYPES = {
    "food_establishments": {
        "property_id": "VARCHAR", "zip": "VARCHAR", "latitude": "DOUBLE", "longitude": "DOUBLE"
    },
    "food_inspections": {},
    "food_violations": {},
    "service_requests_311_geography": {
        "case_enquiry_id": "BIGINT", "neighborhood": "VARCHAR",
        "neighborhood_raw": "VARCHAR", "police_district": "VARCHAR",
        "geography_method": "VARCHAR", "boundary_candidate_count": "BIGINT",
    },
    "police_district_neighborhood_purity": {
        "police_district": "VARCHAR", "neighborhood": "VARCHAR",
        "case_count": "BIGINT", "district_total": "BIGINT",
        "spatially_assigned_cases": "BIGINT", "purity_pct": "DOUBLE",
        "spatial_coverage_pct": "DOUBLE", "method": "VARCHAR",
    },
    "service_request_reasons_311": {"reason": "VARCHAR", "count": "BIGINT"},
    "service_request_types_311": {"type": "VARCHAR", "count": "BIGINT"},
    "crime_incidents_geography": {
        "incident_number": "VARCHAR", "district": "VARCHAR", "neighborhood": "VARCHAR",
        "geography_method": "VARCHAR", "boundary_candidate_count": "BIGINT",
    },
    "offense_dim": {
        "offense_code": "VARCHAR", "offense_name": "VARCHAR", "crime_class": "VARCHAR",
        "classification_source": "VARCHAR",
    },
    "offense_codes": {"offense_code": "VARCHAR", "offense_name": "VARCHAR"},
    "occupancy_codes": {"occupancy_code": "VARCHAR", "description": "VARCHAR"},
}

# These are relationship metadata, not claims that every row matches.
JOINS = [
    {"left_table": "service_requests_311", "left_column": "case_enquiry_id", "right_table": "service_requests_311_geography", "right_column": "case_enquiry_id", "relationship": "one_to_one", "confidence": "exact", "notes": "Direct BPDA polygon assignment. Prefer this canonical neighborhood over the raw 311 label."},
    {"left_table": "service_requests_311_geography", "left_column": "police_district", "right_table": "police_district_neighborhood_purity", "right_column": "police_district", "relationship": "many_to_one", "confidence": "derived", "notes": "Fallback metadata only. The purity row must not replace a direct point-in-polygon assignment."},
    {"left_table": "crime_incidents", "left_column": "district", "right_table": "police_district_neighborhood_purity", "right_column": "police_district", "relationship": "many_to_one", "confidence": "derived", "notes": "Fallback for crime records without usable coordinates. Report purity_pct and spatial_coverage_pct; External and Outside of have no fallback."},
    {"left_table": "crime_incidents", "left_column": "incident_number", "right_table": "crime_incidents_geography", "right_column": "incident_number", "relationship": "one_to_one", "confidence": "exact", "notes": "Direct BPDA polygon attribution. Prefer it over district fallback whenever geography_method is point_in_polygon."},
    {"left_table": "crime_incidents", "left_column": "offense_code", "right_table": "offense_dim", "right_column": "offense_code", "relationship": "many_to_one", "confidence": "exact", "notes": "Official RMS lookup; unmatched codes are explicitly unclassified and excluded from crime-only totals."},
    {"left_table": "crime_incidents", "left_column": "offense_code", "right_table": "offense_codes", "right_column": "offense_code", "relationship": "many_to_one", "confidence": "exact", "notes": "Raw RMS name lookup, no crime-class review. Prefer offense_dim unless you need the unclassified source."},
    {"left_table": "food_establishments", "left_column": "establishment_id", "right_table": "food_inspections", "right_column": "establishment_id", "relationship": "one_to_many", "confidence": "exact", "notes": "Generated from property_id; fallback is a stable hash of business/address/ZIP."},
    {"left_table": "food_inspections", "left_column": "inspection_id", "right_table": "food_violations", "right_column": "inspection_id", "relationship": "one_to_many", "confidence": "exact", "notes": "Generated from establishment_id, resultdttm, and result."},
    {"left_table": "service_requests_311", "left_column": "police_district", "right_table": "crime_incidents", "right_column": "district", "relationship": "many_to_many", "confidence": "loose", "notes": "District codes overlap; crime also contains External and Outside of values."},
    {"left_table": "service_requests_311", "left_column": "location_zipcode", "right_table": "food_establishments", "right_column": "zip", "relationship": "many_to_many", "confidence": "loose", "notes": "ZIP is a geographic filter, not an entity key."},
    {"left_table": "service_requests_311", "left_column": "location_zipcode", "right_table": "licenses_board", "right_column": "zip", "relationship": "many_to_many", "confidence": "loose", "notes": "ZIP is a geographic filter, not an entity key."},
    {"left_table": "service_requests_311", "left_column": "location_zipcode", "right_table": "licenses_entertainment", "right_column": "zip", "relationship": "many_to_many", "confidence": "loose", "notes": "ZIP is a geographic filter, not an entity key."},
    {"left_table": "food_establishments", "left_column": "businessname", "right_table": "licenses_board", "right_column": "business_name", "relationship": "many_to_many", "confidence": "fuzzy", "notes": "No shared license key; combine normalized names with address/ZIP."},
    {"left_table": "food_establishments", "left_column": "businessname", "right_table": "licenses_entertainment", "right_column": "business_name", "relationship": "many_to_many", "confidence": "fuzzy", "notes": "No shared license key; combine normalized names with address/ZIP."},
    {"left_table": "licenses_board", "left_column": "city", "right_table": "neighborhoods_bpda", "right_column": "name", "relationship": "many_to_one", "confidence": "fuzzy", "notes": "License city values can contain compound or noncanonical neighborhood labels."},
    {"left_table": "open_spaces", "left_column": "district", "right_table": "neighborhoods_bpda", "right_column": "name", "relationship": "many_to_one", "confidence": "fuzzy", "notes": "Hyphenation and combined neighborhood names differ."},
]


def snake(value: str) -> str:
    import re
    value = value.lstrip("\ufeff")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return {"lat": "latitude", "long": "longitude"}.get(value, value)


def load_dictionary() -> dict[tuple[str, str], dict[str, str]]:
    result = {}
    with DICTIONARY.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            result[(row["destination_table"], snake(row["column"]))] = row
    return result


DICTIONARY_ROWS = load_dictionary()
DICTIONARY_TABLE_ALIASES = {"open_spaces": "open_space"}


@lru_cache(maxsize=1)
def remote_catalog_meta() -> dict[str, Any]:
    """Row counts and headers precomputed by parse_csvs.py at build time.

    Reading this tiny sidecar instead of streaming each multi-hundred-MB
    derived CSV is what keeps list_tables()/describe_table() fast. If it's
    missing, or a given table isn't in it, callers fall back to the slow
    (network-scanning) path below.
    """
    try:
        response = requests.get(f"{OBJECT_STORAGE_BASE}/catalog_meta.json", timeout=10)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return {}


def table_columns(table: str) -> list[str]:
    cached = remote_catalog_meta().get(table)
    if cached and cached.get("columns"):
        return [snake(value) for value in cached["columns"]]
    with open_table_stream(table) as fh:
        return [snake(value) for value in (next(csv.reader(fh), []))]


def column_type(table: str, column: str) -> str:
    if column in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[column]
    if table in GENERATED_TYPES and column in GENERATED_TYPES[table]:
        return GENERATED_TYPES[table][column]
    dictionary_table = DICTIONARY_TABLE_ALIASES.get(table, table)
    dictionary_row = DICTIONARY_ROWS.get((dictionary_table, column))
    if dictionary_row:
        return dictionary_row["data_type"].upper()
    # Derived food fields retain their source table's metadata.
    if table.startswith("food_"):
        source_row = DICTIONARY_ROWS.get(("food_inspections", column))
        if source_row:
            return source_row["data_type"].upper()
    return "VARCHAR"


@lru_cache(maxsize=None)
def row_count(table: str) -> int:
    cached = remote_catalog_meta().get(table)
    if cached and "row_count" in cached:
        return cached["row_count"]
    with open_table_stream(table) as fh:
        return max(0, sum(1 for _ in fh) - 1)


@lru_cache(maxsize=None)
def schema(table: str) -> dict[str, Any]:
    if table not in TABLE_SOURCES:
        raise ValueError(f"Unknown table {table!r}. Available: {', '.join(TABLE_SOURCES)}")
    columns = [
        {"name": column, "type": column_type(table, column)}
        for column in table_columns(table)
    ]
    return {
        "table": table,
        "source": TABLE_SOURCES[table],
        "path": object_url(table),
        "row_count": row_count(table),
        "description": TABLE_DESCRIPTIONS.get(table, "Derived data table."),
        "columns": columns,
    }


def catalog() -> dict[str, Any]:
    return {"tables": [schema(table) for table in TABLE_SOURCES], "joins": JOINS}


mcp = FastMCP("Boston derived data catalog")


@mcp.tool
def list_tables() -> list[dict[str, Any]]:
    """List every derived table, row count, source, and column count."""
    return [
        {key: item[key] for key in ("table", "source", "row_count", "description")}
        | {"column_count": len(item["columns"])}
        for item in catalog()["tables"]
    ]


@mcp.tool
def describe_table(table: str) -> dict[str, Any]:
    """Return all columns and DuckDB-oriented types for one derived table."""
    return schema(table)


@mcp.tool
def list_joins(table: str | None = None) -> list[dict[str, Any]]:
    """List known exact, loose, and fuzzy joins, optionally filtered by table."""
    if not table:
        return JOINS
    return [join for join in JOINS if table in {join["left_table"], join["right_table"]}]


@mcp.tool
def sample_rows(table: str, limit: int = 5) -> list[dict[str, str]]:
    """Read a small sample from a derived CSV without loading the full table."""
    if table not in TABLE_SOURCES:
        raise ValueError(f"Unknown table {table!r}")
    limit = max(1, min(limit, 100))
    with open_table_stream(table) as fh:
        return list(islice(csv.DictReader(fh), limit))


@mcp.resource("catalog://tables")
def tables_resource() -> str:
    """JSON catalog of all tables and their typed columns."""
    return json.dumps(catalog(), indent=2)


@mcp.resource("catalog://table/{table}")
def table_resource(table: str) -> str:
    """JSON schema for one derived table."""
    return json.dumps(schema(table), indent=2)


@mcp.resource("catalog://joins")
def joins_resource() -> str:
    """JSON list of documented joins and their confidence levels."""
    return json.dumps(JOINS, indent=2)


app = mcp.http_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "3000")))
