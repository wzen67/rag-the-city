#!/usr/bin/env python3
"""Build a direct 311-to-BPDA-neighborhood crosswalk.

The output is deliberately a small, inspectable CSV keyed by case ID.  The
DuckDB views join it rather than re-running point-in-polygon work on every
query.  It is also the evidence for geography coverage in the final demo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
# ``python scripts/build_geography.py`` places scripts/, not the repository,
# on sys.path. Keep the documented direct invocation working.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.catalog_meta import update_catalog_meta
from rag.geography import MemoizedNeighborhoodAssigner


DEFAULT_BOUNDARIES = ROOT / "data" / "boston_neighborhood_boundaries.json.gz"
DEFAULT_311 = ROOT / "data" / "derived" / "service_requests_311.csv"
DEFAULT_OUTPUT = ROOT / "data" / "derived" / "service_requests_311_geography.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", type=Path, default=DEFAULT_BOUNDARIES)
    parser.add_argument("--service-requests", type=Path, default=DEFAULT_311)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--precision", type=int, default=6)
    args = parser.parse_args()

    if not args.boundaries.exists():
        parser.error(f"boundary file not found: {args.boundaries}")
    if not args.service_requests.exists():
        parser.error(f"311 file not found: {args.service_requests}")

    columns = ["case_enquiry_id", "latitude", "longitude", "neighborhood", "police_district"]
    cases = pd.read_csv(args.service_requests, usecols=columns, dtype={"case_enquiry_id": "string"})
    assigner = MemoizedNeighborhoodAssigner(args.boundaries, precision=args.precision)

    # ``assign`` is called for every valid row. Its LRU cache ensures repeated
    # rounded coordinates incur only one point-in-polygon lookup, while its
    # hit/miss statistics make that behavior auditable in the build output.
    points = cases[["latitude", "longitude"]].copy()
    points["latitude"] = pd.to_numeric(points["latitude"], errors="coerce")
    points["longitude"] = pd.to_numeric(points["longitude"], errors="coerce")
    valid = points["latitude"].between(-90, 90) & points["longitude"].between(-180, 180)
    points["lat_key"] = points["latitude"].round(args.precision)
    points["lon_key"] = points["longitude"].round(args.precision)
    unique = points.loc[valid, ["lat_key", "lon_key"]].drop_duplicates()

    assigned = [
        assigner.assign(latitude, longitude)
        if is_valid else None
        for latitude, longitude, is_valid in zip(points["latitude"], points["longitude"], valid)
    ]
    result = cases.rename(columns={"neighborhood": "neighborhood_raw"}).copy()
    result["neighborhood"] = [item.neighborhood if item else None for item in assigned]
    result["geography_method"] = [
        item.method if item else "missing_or_invalid_coordinates" for item in assigned
    ]
    result["boundary_candidate_count"] = [
        item.candidate_count if item else 0 for item in assigned
    ]
    result = result[[
        "case_enquiry_id", "neighborhood", "neighborhood_raw", "police_district",
        "geography_method", "boundary_candidate_count",
    ]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    update_catalog_meta(
        args.output.parent, "service_requests_311_geography", len(result), list(result.columns)
    )

    resolved = int(result["neighborhood"].notna().sum())
    cache = assigner.cache_info()
    print(f"Wrote {len(result):,} 311 geography assignments to {args.output}")
    print(f"Direct polygon coverage: {resolved:,}/{len(result):,} ({resolved / len(result):.1%})")
    print(f"Unique rounded coordinates: {len(unique):,}; cache hits/misses: {cache.hits:,}/{cache.misses:,}")
    print(result["geography_method"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
