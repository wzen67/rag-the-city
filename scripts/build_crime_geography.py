#!/usr/bin/env python3
"""Build direct BPDA point-in-polygon assignments for every crime record."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag.geography import MemoizedNeighborhoodAssigner


DEFAULT_BOUNDARIES = ROOT / "data" / "boston_neighborhood_boundaries.json.gz"
DEFAULT_CRIME = ROOT / "data" / "derived" / "crime_incidents.csv"
DEFAULT_OUTPUT = ROOT / "data" / "derived" / "crime_incidents_geography.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", type=Path, default=DEFAULT_BOUNDARIES)
    parser.add_argument("--crime", type=Path, default=DEFAULT_CRIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--precision", type=int, default=6)
    args = parser.parse_args()
    if not args.boundaries.exists():
        parser.error(f"boundary file not found: {args.boundaries}")
    if not args.crime.exists():
        parser.error(f"crime file not found: {args.crime}")

    crime = pd.read_csv(
        args.crime,
        usecols=["incident_number", "district", "latitude", "longitude"],
        dtype={"incident_number": "string", "district": "string"},
    )
    if crime["incident_number"].isna().any() or crime["incident_number"].duplicated().any():
        raise ValueError("crime incident_number must be a present, unique join key")
    latitude = pd.to_numeric(crime["latitude"], errors="coerce")
    longitude = pd.to_numeric(crime["longitude"], errors="coerce")
    valid = latitude.between(-90, 90) & longitude.between(-180, 180)
    assigner = MemoizedNeighborhoodAssigner(args.boundaries, precision=args.precision)
    assigned = [
        assigner.assign(lat, lon) if is_valid else None
        for lat, lon, is_valid in zip(latitude, longitude, valid)
    ]
    result = crime[["incident_number", "district"]].copy()
    result["neighborhood"] = [item.neighborhood if item else None for item in assigned]
    result["geography_method"] = [
        item.method if item else "missing_or_invalid_coordinates" for item in assigned
    ]
    result["boundary_candidate_count"] = [item.candidate_count if item else 0 for item in assigned]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)

    resolved = int(result["neighborhood"].notna().sum())
    cache = assigner.cache_info()
    print(f"Wrote {len(result):,} crime geography assignments to {args.output}")
    print(f"Direct polygon coverage: {resolved:,}/{len(result):,} ({resolved / len(result):.1%})")
    print(f"Cache hits/misses: {cache.hits:,}/{cache.misses:,}")
    print(result["geography_method"].value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
