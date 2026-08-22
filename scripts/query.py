"""The only way the engine is allowed to touch data.

The semantic layer TELLS the model to use the clean tables. This module makes
it so it cannot do anything else. Three locks, cheapest first:

  1. read-only connection      - no INSERT / UPDATE / DROP / ATTACH
  2. enable_external_access=false - read_csv_auto(), ST_Read() and httpfs all
                                    error, so the raw CSVs are unreachable
  3. table allowlist           - the query is rejected before execution if it
                                 names anything outside ALLOWED

Lock 2 is the important one. Without it a generated query like

    SELECT max(TOTAL_VALUE) FROM read_csv_auto('data/property-assessment.csv.gz')

returns 999,900 instead of 2,448,193,300 and nothing objects. With it, that
query raises a Permission Error before it can produce a wrong number.

Usage:
    from scripts.query import run
    rows, sql = run("SELECT count(*) FROM crime_only WHERE neighborhood = 'Roxbury'")
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb

DB = Path("boston.db")

ALLOWED = {
    "crime", "crime_only", "svc311", "food_violations", "food_inspections",
    "property", "property_homes", "licenses", "entertainment_licenses",
    "open_space", "neighborhoods", "offense_dim",
}

# Functions that would reach outside the database file.
BANNED_FUNCS = re.compile(
    r"\b(read_csv|read_csv_auto|read_parquet|read_json|read_json_auto|"
    r"st_read|glob|attach|copy|install|load)\s*\(?",
    re.IGNORECASE,
)

_WRITE = re.compile(
    r"^\s*(insert|update|delete|drop|create|alter|attach|copy|pragma|set)\b",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    """Raised when a generated query tries to leave the allowlist."""


def _tables_in(sql: str) -> set[str]:
    """Table names appearing after FROM or JOIN."""
    stripped = re.sub(r"--[^\n]*", " ", sql)
    return {
        m.group(1).lower()
        for m in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", stripped, re.I)
    }


def check(sql: str) -> None:
    """Reject a query before it runs. Raises UnsafeQuery."""
    if _WRITE.match(sql):
        raise UnsafeQuery("only SELECT queries are allowed")
    if BANNED_FUNCS.search(sql):
        raise UnsafeQuery(
            "file-reading functions are not allowed - query the clean tables, "
            "not the raw CSVs"
        )
    unknown = _tables_in(sql) - ALLOWED
    if unknown:
        raise UnsafeQuery(
            f"unknown table(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ALLOWED))}"
        )


def connect() -> duckdb.DuckDBPyConnection:
    """A connection that physically cannot read the raw files."""
    if not DB.exists():
        raise FileNotFoundError(f"{DB} missing - run: python scripts/build_db.py")
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("LOAD spatial;")          # must load BEFORE access is disabled
    con.execute("SET enable_external_access = false;")
    return con


def run(sql: str, con: duckdb.DuckDBPyConnection | None = None):
    """Validate, execute, and return (rows, sql). Raises UnsafeQuery if unsafe."""
    check(sql)
    owned = con is None
    con = con or connect()
    try:
        return con.execute(sql).fetchall(), sql
    finally:
        if owned:
            con.close()


if __name__ == "__main__":
    con = connect()
    print("These should succeed:\n")
    for q in [
        "SELECT count(*) FROM crime_only WHERE neighborhood = 'Roxbury'",
        "SELECT max(total_value) FROM property",
        "SELECT count(*) FROM food_inspections",
    ]:
        rows, _ = run(q, con)
        print(f"  {rows[0][0]:>15,}   {q[:64]}")

    print("\nThese are blocked:\n")
    for q in [
        "SELECT max(TOTAL_VALUE) FROM read_csv_auto('data/property-assessment.csv.gz')",
        "SELECT * FROM secret_table",
        "DROP TABLE crime",
        "SELECT * FROM ST_Read('data/boston_neighborhood_boundaries.json.gz')",
    ]:
        try:
            run(q, con)
            print(f"  NOT BLOCKED  {q[:66]}")
        except (UnsafeQuery, duckdb.Error) as e:
            print(f"  blocked      {q[:52]}\n               -> {str(e).splitlines()[0][:78]}")
    con.close()
