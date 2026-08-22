#!/usr/bin/env python3
"""Build the deterministic offense-code dimension from the RMS workbook."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.offense import classify_offense


DEFAULT_WORKBOOK = ROOT / "data" / "reference" / "rmsoffensecodes.xlsx"
DEFAULT_CRIME = ROOT / "data" / "derived" / "crime_incidents.csv"
DEFAULT_OUTPUT = ROOT / "data" / "reference" / "offense_dim.csv"
DEFAULT_REPORT = ROOT / "results" / "offense_dim_coverage.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--crime", type=Path, default=DEFAULT_CRIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    for label, path in (("workbook", args.workbook), ("crime", args.crime)):
        if not path.exists():
            parser.error(f"{label} file not found: {path}")

    source = pd.read_excel(args.workbook, dtype={"CODE": "string", "NAME": "string"})
    source.columns = [str(column).strip().lower() for column in source.columns]
    if not {"code", "name"}.issubset(source.columns):
        raise ValueError("RMS workbook must provide CODE and NAME columns")
    dimension = source[["code", "name"]].rename(columns={"code": "offense_code", "name": "offense_name"})
    dimension["offense_code"] = dimension["offense_code"].str.strip()
    dimension["offense_name"] = dimension["offense_name"].str.strip()
    dimension = dimension.dropna().drop_duplicates("offense_code").sort_values("offense_code", key=lambda s: pd.to_numeric(s))
    dimension["crime_class"] = dimension["offense_name"].map(classify_offense)
    dimension["classification_source"] = "official_rms_name+reviewed_rules_v1"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    dimension.to_csv(args.output, index=False)

    crime = pd.read_csv(args.crime, usecols=["offense_code"], dtype="string")
    observed = crime["offense_code"].dropna().str.strip()
    matched = observed.isin(set(dimension["offense_code"]))
    report = {
        "source": str(args.workbook.relative_to(ROOT)),
        "dimension_rows": len(dimension),
        "crime_rows": len(crime),
        "crime_rows_matched": int(matched.sum()),
        "crime_rows_unmatched": int((~matched).sum()),
        "crime_row_match_pct": round(100 * float(matched.mean()), 3),
        "unmatched_offense_codes": sorted(observed[~matched].unique().tolist()),
        "class_counts": {key: int(value) for key, value in dimension["crime_class"].value_counts().sort_index().items()},
        "checks": {
            "SICK ASSIST": classify_offense("SICK ASSIST") == "not_a_crime",
            "ASSAULT - AGGRAVATED": classify_offense("ASSAULT - AGGRAVATED") == "violent",
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Wrote {len(dimension):,} offense dimension rows to {args.output}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
