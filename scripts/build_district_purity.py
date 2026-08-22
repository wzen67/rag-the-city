#!/usr/bin/env python3
"""Build the 311-derived police-district neighborhood fallback crosswalk."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.catalog_meta import update_catalog_meta
from rag.geography import derive_district_purity


DEFAULT_ASSIGNMENTS = ROOT / "data" / "derived" / "service_requests_311_geography.csv"
DEFAULT_OUTPUT = ROOT / "data" / "derived" / "police_district_neighborhood_purity.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignments", type=Path, default=DEFAULT_ASSIGNMENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.assignments.exists():
        parser.error(f"assignment file not found: {args.assignments}; run build_geography.py first")

    assignments = pd.read_csv(args.assignments, dtype="string")
    result = derive_district_purity(assignments)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    update_catalog_meta(
        args.output.parent, "police_district_neighborhood_purity", len(result), list(result.columns)
    )
    print(f"Wrote {len(result)} district fallback rows to {args.output}")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
