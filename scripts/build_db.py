"""Bake the views into a real database file, so the raw CSVs stop being reachable.

The views in sql/views.sql are lazy - they call read_csv_auto() every time they
are queried, which means the raw files are always one function call away. A
generated query like

    SELECT max(TOTAL_VALUE) FROM read_csv_auto('data/property-assessment.csv.gz')

would bypass every safeguard and return 999,900 instead of 2,448,193,300.

This script materialises each view as a TABLE inside boston.db. After that,
scripts/query.py can open the file read-only with filesystem access disabled,
at which point reaching the raw CSVs is not merely discouraged - it errors.

Run from the repo root:  python scripts/build_db.py
"""

from pathlib import Path

import duckdb

DB = Path("boston.db")
VIEWS = Path("sql/views.sql")

# Materialised in this order; each becomes a real table in boston.db.
TABLES = [
    "neighborhoods",
    "offense_dim",
    "crime",
    "crime_only",
    "svc311",
    "food_violations",
    "food_inspections",
    "property",
    "property_homes",
    "licenses",
    "entertainment_licenses",
    "open_space",
]


def main() -> None:
    if DB.exists():
        DB.unlink()

    con = duckdb.connect(str(DB))
    con.execute("SET memory_limit='4GB'; SET threads=2;")
    con.execute(VIEWS.read_text())

    print(f"materialising {len(TABLES)} tables into {DB} ...")
    for name in TABLES:
        con.execute(f"CREATE TABLE _m_{name} AS SELECT * FROM {name}")
        # views.sql creates neighborhoods and offense_dim as TABLEs already,
        # the rest as VIEWs - drop whichever it turns out to be.
        kind = con.execute(
            "SELECT table_type FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()
        if kind and kind[0] == "VIEW":
            con.execute(f"DROP VIEW {name}")
        else:
            con.execute(f"DROP TABLE {name}")
        con.execute(f"ALTER TABLE _m_{name} RENAME TO {name}")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  {name:24} {n:>10,}")

    con.close()
    size = DB.stat().st_size / 1e6
    print(f"\n{DB} is {size:.0f} MB — regenerable, so it is gitignored")


if __name__ == "__main__":
    main()
