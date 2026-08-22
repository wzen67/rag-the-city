"""FastMCP catalog server for the derived Boston CSV entities.

Run with:
    conda run -n nn uvicorn mcp_server:app --host 127.0.0.1 --port 3000
"""

from __future__ import annotations

import csv
import json
import os
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any

from fastmcp import FastMCP


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DERIVED = DATA / "derived"
DICTIONARY = DATA / "dd.md"

TABLE_SOURCES = {
    "service_requests_311": "311-service-requests.csv",
    "crime_incidents": "crime-incident-reports-august-2015-to-date-source-new-system.csv",
    "licenses_board": "licensing-board-licenses.csv",
    "licenses_entertainment": "entertainment-licenses-legacy.csv",
    "open_spaces": "open-space.csv",
    "neighborhoods_bpda": "bpda-neighborhood-boundaries.csv",
    "food_establishments": "food-establishment-inspections.csv (derived)",
    "food_inspections": "food-establishment-inspections.csv (derived)",
    "food_violations": "food-establishment-inspections.csv (derived)",
}

TABLE_DESCRIPTIONS = {
    "service_requests_311": "One row per 311 service request.",
    "crime_incidents": "One row per crime incident/offense record.",
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
}

# These are relationship metadata, not claims that every row matches.
JOINS = [
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


def table_columns(table: str) -> list[str]:
    path = DERIVED / f"{table}.csv"
    with path.open(newline="", encoding="utf-8-sig") as fh:
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
def schema(table: str) -> dict[str, Any]:
    if table not in TABLE_SOURCES:
        raise ValueError(f"Unknown table {table!r}. Available: {', '.join(TABLE_SOURCES)}")
    columns = [
        {"name": column, "type": column_type(table, column)}
        for column in table_columns(table)
    ]
    path = DERIVED / f"{table}.csv"
    with path.open("rb") as fh:
        rows = max(0, sum(1 for _ in fh) - 1)
    return {
        "table": table,
        "source": TABLE_SOURCES[table],
        "path": str(path),
        "row_count": rows,
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
    with (DERIVED / f"{table}.csv").open(newline="", encoding="utf-8-sig") as fh:
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
