"""Direct, memoized point-in-neighborhood attribution.

The source 311 neighborhood label is useful evidence but is not a canonical
geography: it contains compound labels and a city-wide ``Boston`` bucket.
This module assigns a point to the BPDA polygon instead.  The lookup is
memoized by rounded WGS84 coordinate because city exports frequently repeat a
location across requests and a point-in-polygon test is otherwise needlessly
repeated.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class NeighborhoodAssignment:
    """One coordinate's canonical-geography result and its provenance."""

    neighborhood: str | None
    method: str
    candidate_count: int


class MemoizedNeighborhoodAssigner:
    """Assign WGS84 coordinates to BPDA neighborhoods using GeoPandas/Shapely.

    ``precision`` deliberately rounds cache keys, rather than geometry.  Six
    decimal places is about 11 cm at Boston's latitude: enough to coalesce
    duplicate exported coordinates without moving an ordinary address across
    a neighborhood boundary.
    """

    def __init__(self, boundary_path: Path | str, *, precision: int = 6,
                 cache_size: int = 200_000) -> None:
        import geopandas as gpd

        self.precision = precision
        path = Path(boundary_path)
        # GeoPandas/pyogrio selects a driver from the suffix; our source is
        # ``.json.gz`` rather than ``.geojson.gz``, so pass decompressed bytes
        # explicitly instead of relying on driver inference.
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as source:
                boundaries = gpd.read_file(source.read())
        else:
            boundaries = gpd.read_file(path)
        if "name" not in boundaries.columns:
            raise ValueError("boundary GeoJSON must contain a 'name' property")
        if boundaries.crs is None:
            raise ValueError("boundary GeoJSON has no CRS; expected EPSG:4326")
        self.boundaries = boundaries.to_crs("EPSG:4326")[["name", "geometry"]].copy()
        self.boundaries["name"] = self.boundaries["name"].astype(str)
        if self.boundaries["name"].duplicated().any():
            raise ValueError("boundary GeoJSON has duplicate neighborhood names")
        self._index = self.boundaries.sindex

        # Build the cache per instance, so distinct boundary files cannot
        # accidentally share results.  Expose cache_info() for build metrics.
        self._lookup = lru_cache(maxsize=cache_size)(self._lookup_uncached)

    def _key(self, latitude: float, longitude: float) -> tuple[float, float]:
        return (round(float(latitude), self.precision),
                round(float(longitude), self.precision))

    def _lookup_uncached(self, latitude: float, longitude: float) -> NeighborhoodAssignment:
        from shapely.geometry import Point

        # A bounding-box index makes the usual lookup a comparison against one
        # polygon, not all 26.  ``covers`` also attributes a point on a shared
        # polygon edge instead of silently dropping it.
        point = Point(longitude, latitude)
        positions = self._index.query(point, predicate="intersects")
        candidates = self.boundaries.iloc[positions]
        matches = candidates[candidates.geometry.apply(lambda polygon: polygon.covers(point))]
        if matches.empty:
            return NeighborhoodAssignment(None, "unresolved", 0)
        # BPDA polygons should not overlap.  Preserve ambiguity rather than
        # inventing a winner if a future boundary source does overlap.
        if len(matches) > 1:
            return NeighborhoodAssignment(None, "ambiguous_boundary", len(matches))
        return NeighborhoodAssignment(str(matches.iloc[0]["name"]), "point_in_polygon", 1)

    def assign(self, latitude: float | None, longitude: float | None) -> NeighborhoodAssignment:
        if latitude is None or longitude is None:
            return NeighborhoodAssignment(None, "missing_coordinates", 0)
        try:
            lat, lon = float(latitude), float(longitude)
        except (TypeError, ValueError):
            return NeighborhoodAssignment(None, "invalid_coordinates", 0)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return NeighborhoodAssignment(None, "invalid_coordinates", 0)
        return self._lookup(*self._key(lat, lon))

    def cache_info(self):
        """Return ``functools.lru_cache`` statistics for build reporting."""
        return self._lookup.cache_info()


def derive_district_purity(assignments):
    """Return one explicit neighborhood fallback per police district.

    ``purity_pct`` is the modal neighborhood's share of requests that could
    be spatially assigned. ``spatial_coverage_pct`` separately reports the
    share of all district requests with a usable direct assignment. Keeping
    the two denominators distinct prevents a low-coordinate-coverage district
    from appearing more certain than it is.
    """
    import pandas as pd

    required = {"police_district", "neighborhood"}
    missing = required - set(assignments.columns)
    if missing:
        raise ValueError(f"assignment data missing columns: {', '.join(sorted(missing))}")

    frame = assignments[["police_district", "neighborhood"]].copy()
    frame["police_district"] = frame["police_district"].fillna("").astype(str).str.strip()
    frame["neighborhood"] = frame["neighborhood"].fillna("").astype(str).str.strip()
    frame = frame[frame["police_district"] != ""]

    totals = frame.groupby("police_district").size().rename("district_total")
    located = frame[frame["neighborhood"] != ""]
    counts = (
        located.groupby(["police_district", "neighborhood"])
        .size()
        .rename("case_count")
        .reset_index()
        .sort_values(["police_district", "case_count", "neighborhood"],
                     ascending=[True, False, True])
        .drop_duplicates("police_district")
        .set_index("police_district")
    )
    spatial = located.groupby("police_district").size().rename("spatially_assigned_cases")

    result = totals.to_frame().join(spatial).join(counts[["neighborhood", "case_count"]])
    result["spatially_assigned_cases"] = result["spatially_assigned_cases"].fillna(0).astype(int)
    result["case_count"] = result["case_count"].fillna(0).astype(int)
    result["neighborhood"] = result["neighborhood"].fillna("UNKNOWN")
    result["purity_pct"] = (
        100 * result["case_count"] / result["spatially_assigned_cases"].replace(0, pd.NA)
    ).fillna(0).round(1)
    result["spatial_coverage_pct"] = (
        100 * result["spatially_assigned_cases"] / result["district_total"]
    ).round(1)
    result["method"] = "311_direct_polygon_modal_fallback"
    return result.reset_index()[[
        "police_district", "neighborhood", "case_count", "district_total",
        "spatially_assigned_cases", "purity_pct", "spatial_coverage_pct", "method",
    ]]
