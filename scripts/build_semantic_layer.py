"""Generate the semantic layer that gets injected into the SQL-generation prompt.

Two outputs:
  sql/semantic_layer.md    prompt-ready. Paste into the SQL-gen system prompt.
  sql/semantic_layer.json  same content, structured, for programmatic use.

Column lists are read from the LIVE views, so this file cannot drift from the
schema. The prose (what each view means, when to use it, the rules) is
hand-written here because only a human knows the intent.

Why this exists: data/DATA_DICTIONARY.md describes the RAW csv files. The SQL
generator must never see those - it queries the VIEWS, which already handle
the traps. Feeding it raw column names would reintroduce every bug the views
were built to prevent.

Run from the repo root:  python scripts/build_semantic_layer.py
"""

import json
from pathlib import Path

import duckdb

VIEWS = Path("sql/views.sql")
OUT_MD = Path("sql/semantic_layer.md")
OUT_JSON = Path("sql/semantic_layer.json")

# What each view is FOR. The generator picks a table from these descriptions.
MEANING = {
    "crime_only": (
        "Crime incidents that are actually crimes. **Default for any question "
        "about crime.** Non-offences (medical assists, traffic accidents, "
        "sudden deaths, fire reports, investigations) are already excluded."
    ),
    "crime": (
        "All 290,130 police incident reports, including non-crimes. Use only "
        "when the question is explicitly about police activity or reports "
        "rather than crime, or to contrast naive vs corrected counts."
    ),
    "svc311": (
        "311 service requests, 2026 only (Jan 1 - Aug 20). Responsiveness, "
        "complaint themes, department performance."
    ),
    "food_inspections": (
        "One row per actual restaurant inspection. **Use for any rate or "
        "count of inspections.**"
    ),
    "food_violations": (
        "One row per violation found. Use only when asking about violations "
        "themselves. Never count these as inspections."
    ),
    "property_homes": (
        "Residential properties with a real assessed value. **Default for any "
        "question about home or property value.** Parking spaces, unusable "
        "land and zero-value parcels are already excluded."
    ),
    "property": (
        "All 184,552 assessed parcels including parking spaces and land. Use "
        "only for total-stock questions, never for typical home value."
    ),
    "licenses": (
        "Currently active liquor / common victualler licences, with capacity "
        "and opening hours. Contains no history."
    ),
    "entertainment_licenses": (
        "Currently active entertainment licences (live music, night clubs). "
        "Contains no history."
    ),
    "open_space": "Parks and green space. Joins by zipcode only - it has no coordinates.",
    "neighborhoods": "The 26 canonical neighborhood polygons. Join key for everything else.",
    "offense_dim": "Offence code to description and crime_class. Already joined into crime.",
}

# Facts the generator cannot infer from column names alone.
RULES = [
    "Every view already has a `neighborhood` column EXCEPT property, property_homes and open_space. Never write a spatial join yourself.",
    "property and property_homes have NO coordinates. They join to other tables by `zipcode` only.",
    "open_space has no coordinates and no neighborhood. It joins by `zipcode` only.",
    "311 data covers 2026 only (Jan-Aug). Any question comparing 311 to a previous year is UNANSWERABLE - say so, do not write SQL.",
    "Crime 2026 is partial (Jan 15 Aug). Never compare a full year to 2026 without restricting to `month <= 8`.",
    "Use `crime_only` for crime questions. `crime` includes non-crimes and will overstate by ~97%.",
    "Use `food_inspections` for inspection counts and rates. `food_violations` has ~4x more rows.",
    "Use `property_homes` for typical home value. `property` includes 8,545 parking spaces worth ~$44,000 each.",
    "311 `hours_to_close` is NULL for the 49.1% of cases still open. Report the open-case count alongside any median.",
    "Licence tables contain only active records. Questions about revoked or expired licences are UNANSWERABLE.",
    "`on_time` in svc311 is pre-computed by the city ('ONTIME' / 'OVERDUE'). Use it, do not recompute from timestamps.",
]

EXAMPLES = [
    ("How many crimes were there in Roxbury in 2025?",
     "SELECT count(*) FROM crime_only WHERE neighborhood = 'Roxbury' AND year = 2025;"),
    ("Which neighborhood has the most violent crime?",
     "SELECT neighborhood, count(*) AS n FROM crime_only\n"
     "WHERE crime_class = 'violent' AND neighborhood IS NOT NULL\n"
     "GROUP BY 1 ORDER BY n DESC LIMIT 1;"),
    ("Is crime in Dorchester getting better or worse?",
     "-- 2026 is Jan-Aug only, so compare like-for-like windows.\n"
     "SELECT year, count(*) AS n FROM crime_only\n"
     "WHERE neighborhood = 'Dorchester' AND month <= 8\n"
     "GROUP BY 1 ORDER BY 1;"),
    ("Which neighborhood waits longest for 311 requests?",
     "SELECT neighborhood,\n"
     "       median(hours_to_close) AS median_hours,\n"
     "       count(*) FILTER (WHERE is_open) AS still_open\n"
     "FROM svc311 WHERE neighborhood IS NOT NULL\n"
     "GROUP BY 1 ORDER BY median_hours DESC;"),
    ("What do people in East Boston complain about most?",
     "SELECT type, count(*) AS n FROM svc311\n"
     "WHERE neighborhood = 'East Boston' GROUP BY 1 ORDER BY n DESC LIMIT 10;"),
    ("What is the median home value in each neighborhood?",
     "-- property has no coordinates; it joins by zipcode.\n"
     "SELECT p.zipcode, median(p.total_value) AS median_value, count(*) AS n\n"
     "FROM property_homes p GROUP BY 1 ORDER BY median_value DESC;"),
    ("What is the most valuable property in Boston?",
     "SELECT owner, street_address, total_value\n"
     "FROM property ORDER BY total_value DESC LIMIT 5;"),
    ("What share of restaurant inspections fail?",
     "SELECT result, count(*) AS n,\n"
     "       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct\n"
     "FROM food_inspections GROUP BY 1 ORDER BY n DESC;"),
    ("How many 311 cases were closed on time in Mattapan?",
     "SELECT on_time, count(*) FROM svc311\n"
     "WHERE neighborhood = 'Mattapan' GROUP BY 1;"),
    ("How many liquor licences were revoked last year?",
     "-- UNANSWERABLE: the licence tables contain only currently-active\n"
     "-- records. There is no revocation history. Abstain."),
]


def main() -> None:
    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'; SET threads=2;")
    con.execute(VIEWS.read_text())

    tables = []
    for name, meaning in MEANING.items():
        cols = [
            {"name": c[0], "type": c[1]}
            for c in con.execute(f"DESCRIBE {name}").fetchall()
            if c[0] != "geom"
        ]
        rows = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        tables.append({"name": name, "rows": rows, "meaning": meaning, "columns": cols})

    OUT_JSON.write_text(json.dumps(
        {"tables": tables, "rules": RULES,
         "examples": [{"question": q, "sql": s} for q, s in EXAMPLES]},
        indent=2) + "\n")

    lines = [
        "# Semantic layer — inject this into the SQL-generation prompt",
        "",
        "Generated by `scripts/build_semantic_layer.py` from the live views in",
        "`sql/views.sql`. Do not edit by hand; edit the script and re-run.",
        "",
        "These views already handle every known trap in the raw data. Query them",
        "directly. Never read the raw CSVs, and never use a column that is not",
        "listed here.",
        "",
        "## Tables",
        "",
    ]
    for t in tables:
        lines += [f"### `{t['name']}` — {t['rows']:,} rows", "", t["meaning"], "",
                  "```", "  " + ", ".join(f"{c['name']} {c['type']}" for c in t["columns"]),
                  "```", ""]

    lines += ["## Rules", ""]
    lines += [f"{i}. {r}" for i, r in enumerate(RULES, 1)]
    lines += ["", "## Example questions and correct SQL", ""]
    for q, s in EXAMPLES:
        lines += [f"**{q}**", "", "```sql", s, "```", ""]

    OUT_MD.write_text("\n".join(lines))

    chars = len(OUT_MD.read_text())
    print(f"wrote {OUT_MD}   {len(tables)} tables, {len(RULES)} rules, "
          f"{len(EXAMPLES)} examples")
    print(f"wrote {OUT_JSON}")
    print(f"prompt size: {chars:,} chars (~{chars // 4:,} tokens) — fits in one prompt")


if __name__ == "__main__":
    main()
