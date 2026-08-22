"""Resolve missing 311 ZIP codes with validated, distance-weighted KNN.

The source CSV is never overwritten. Reported ZIP codes remain authoritative;
only missing ZIPs with valid coordinates are candidates for imputation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.neighbors import BallTree


EARTH_RADIUS_MILES = 3958.7613
BOSTON_LAT_BOUNDS = (42.0, 42.6)
BOSTON_LON_BOUNDS = (-71.3, -70.7)


def normalize_zip(values: pd.Series) -> pd.Series:
    """Return five-digit ZIP strings without turning missing values into text."""
    extracted = values.astype("string").str.extract(r"(\d{4,5})", expand=False)
    return extracted.str.zfill(5).where(extracted.notna())


def coordinate_groups(rows: pd.DataFrame) -> pd.DataFrame:
    """Collapse repeated coordinates and select the modal reported ZIP.

    Grouping prevents duplicate coordinates from leaking between validation
    train/test sets and prevents high-volume locations from dominating KNN.
    """
    grouped = rows.copy()
    grouped["lat_key"] = grouped["latitude_num"].round(5)
    grouped["lon_key"] = grouped["longitude_num"].round(5)
    counts = (
        grouped.groupby(["lat_key", "lon_key", "zip_code"], dropna=False)
        .size()
        .rename("label_count")
        .reset_index()
    )
    counts["coordinate_total"] = counts.groupby(["lat_key", "lon_key"])["label_count"].transform("sum")
    counts = counts.sort_values(
        ["lat_key", "lon_key", "label_count", "zip_code"],
        ascending=[True, True, False, True],
    )
    chosen = counts.drop_duplicates(["lat_key", "lon_key"]).copy()
    chosen["label_purity"] = chosen["label_count"] / chosen["coordinate_total"]
    return chosen[["lat_key", "lon_key", "zip_code", "label_purity", "coordinate_total"]]


def predict_knn(
    train: pd.DataFrame,
    query_lat: np.ndarray,
    query_lon: np.ndarray,
    k: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Predict ZIP, distance-weighted confidence, and nearest distance."""
    effective_k = min(k, len(train))
    train_coords = np.radians(train[["lat_key", "lon_key"]].to_numpy(dtype=float))
    query_coords = np.radians(np.column_stack([query_lat, query_lon]).astype(float))
    tree = BallTree(train_coords, metric="haversine")
    distances_rad, indices = tree.query(query_coords, k=effective_k)
    labels = train["zip_code"].to_numpy()[indices]
    distances_miles = distances_rad * EARTH_RADIUS_MILES
    weights = 1.0 / np.maximum(distances_miles, 1e-6)

    predictions: list[str] = []
    confidences: list[float] = []
    for row_labels, row_weights in zip(labels, weights):
        totals: dict[str, float] = {}
        for label, weight in zip(row_labels, row_weights):
            totals[str(label)] = totals.get(str(label), 0.0) + float(weight)
        prediction, winning_weight = max(totals.items(), key=lambda item: (item[1], item[0]))
        predictions.append(prediction)
        confidences.append(winning_weight / float(row_weights.sum()))

    return (
        np.asarray(predictions, dtype=object),
        np.asarray(confidences, dtype=float),
        distances_miles[:, 0],
    )


def validate(
    groups: pd.DataFrame,
    k: int,
    confidence_threshold: float,
    max_distance_miles: float,
    test_size: float,
    random_state: int,
) -> dict:
    train, test = train_test_split(groups, test_size=test_size, random_state=random_state)
    predictions, confidences, nearest_distances = predict_knn(
        train,
        test["lat_key"].to_numpy(),
        test["lon_key"].to_numpy(),
        k,
    )
    actual = test["zip_code"].to_numpy(dtype=object)
    correct = predictions == actual
    accepted = (confidences >= confidence_threshold) & (nearest_distances <= max_distance_miles)

    detail = pd.DataFrame(
        {
            "actual_zip": actual,
            "predicted_zip": predictions,
            "correct": correct,
            "accepted": accepted,
        }
    )
    per_zip = (
        detail.groupby("actual_zip")
        .agg(test_rows=("correct", "size"), raw_accuracy=("correct", "mean"), acceptance_rate=("accepted", "mean"))
        .reset_index()
    )
    accepted_detail = detail[detail["accepted"]]
    accepted_accuracy = float(accepted_detail["correct"].mean()) if len(accepted_detail) else None

    return {
        "coordinate_group_train_rows": int(len(train)),
        "coordinate_group_test_rows": int(len(test)),
        "raw_accuracy": float(correct.mean()),
        "acceptance_rate": float(accepted.mean()),
        "accepted_accuracy": accepted_accuracy,
        "accepted_test_rows": int(accepted.sum()),
        "rejected_test_rows": int((~accepted).sum()),
        "per_zip": per_zip.to_dict(orient="records"),
    }


def write_markdown_report(report: dict, path: Path) -> None:
    validation = report["validation"]
    lines = [
        "# KNN ZIP Imputation Validation",
        "",
        "Missing 311 ZIP codes are inferred only when distance-weighted KNN meets both a confidence and distance threshold.",
        "Reported ZIP codes are never overwritten.",
        "",
        "## Configuration",
        "",
        f"- K: {report['configuration']['k']}",
        f"- Confidence threshold: {report['configuration']['confidence_threshold']:.2f}",
        f"- Maximum nearest-neighbor distance: {report['configuration']['max_distance_miles']:.2f} miles",
        "- Distance metric: Haversine",
        "- Validation split: coordinate-group holdout (repeated coordinates cannot leak across train/test)",
        "",
        "## Validation",
        "",
        f"- Raw accuracy: {validation['raw_accuracy']:.2%}",
        f"- Accepted prediction accuracy: {validation['accepted_accuracy']:.2%}" if validation["accepted_accuracy"] is not None else "- Accepted prediction accuracy: N/A",
        f"- Validation acceptance rate: {validation['acceptance_rate']:.2%}",
        f"- Accepted validation rows: {validation['accepted_test_rows']:,}",
        f"- Rejected validation rows: {validation['rejected_test_rows']:,}",
        "",
        "## Production coverage",
        "",
        f"- Total rows: {report['rows']['total']:,}",
        f"- Reported ZIP rows: {report['rows']['reported_zip']:,}",
        f"- KNN-resolved rows: {report['rows']['knn_resolved']:,}",
        f"- Unresolved rows: {report['rows']['unresolved']:,}",
        f"- Final ZIP coverage: {report['rows']['final_zip_coverage']:.2%}",
        "",
        "## Limitations",
        "",
        "KNN estimates ZIP codes from nearby labeled 311 coordinates rather than official ZIP polygons. Boundary locations may be misclassified. Low-confidence or distant predictions remain unresolved.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/311-service-requests.csv.gz"))
    parser.add_argument("--output", type=Path, default=Path("data/derived/311-service-requests-with-resolved-zip.csv.gz"))
    parser.add_argument("--json-report", type=Path, default=Path("eval-results/knn_zip_validation.json"))
    parser.add_argument("--markdown-report", type=Path, default=Path("eval-results/knn_zip_validation.md"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--confidence-threshold", type=float, default=0.80)
    parser.add_argument("--max-distance-miles", type=float, default=0.50)
    parser.add_argument("--test-size", type=float, default=0.20)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    data = pd.read_csv(args.input, dtype="string", low_memory=False)
    original_rows = len(data)
    data["location_zipcode_original"] = normalize_zip(data["location_zipcode"])
    data["latitude_num"] = pd.to_numeric(data["latitude"], errors="coerce")
    data["longitude_num"] = pd.to_numeric(data["longitude"], errors="coerce")
    valid_coordinates = (
        data["latitude_num"].between(*BOSTON_LAT_BOUNDS)
        & data["longitude_num"].between(*BOSTON_LON_BOUNDS)
    ).fillna(False)
    known = data["location_zipcode_original"].notna() & valid_coordinates
    missing_with_coordinates = data["location_zipcode_original"].isna() & valid_coordinates

    training_rows = data.loc[
        known, ["latitude_num", "longitude_num", "location_zipcode_original"]
    ].rename(columns={"location_zipcode_original": "zip_code"})
    groups = coordinate_groups(training_rows)
    validation = validate(
        groups,
        args.k,
        args.confidence_threshold,
        args.max_distance_miles,
        args.test_size,
        args.random_state,
    )

    data["location_zipcode_resolved"] = data["location_zipcode_original"]
    data["zip_source"] = np.where(data["location_zipcode_original"].notna(), "reported", "unresolved")
    data["zip_confidence"] = np.where(data["location_zipcode_original"].notna(), 1.0, np.nan)
    data["zip_nearest_distance_miles"] = np.where(data["location_zipcode_original"].notna(), 0.0, np.nan)

    candidates = data.loc[missing_with_coordinates]
    predictions, confidences, nearest_distances = predict_knn(
        groups,
        candidates["latitude_num"].to_numpy(),
        candidates["longitude_num"].to_numpy(),
        args.k,
    )
    accepted = (confidences >= args.confidence_threshold) & (nearest_distances <= args.max_distance_miles)
    candidate_indices = candidates.index.to_numpy()
    data.loc[candidate_indices, "zip_confidence"] = confidences
    data.loc[candidate_indices, "zip_nearest_distance_miles"] = nearest_distances
    accepted_indices = candidate_indices[accepted]
    data.loc[accepted_indices, "location_zipcode_resolved"] = predictions[accepted]
    data.loc[accepted_indices, "zip_source"] = "knn"

    data = data.drop(columns=["latitude_num", "longitude_num"])
    assert len(data) == original_rows
    assert data["case_enquiry_id"].nunique(dropna=False) == original_rows
    assert data.loc[data["zip_source"].eq("reported"), "location_zipcode_resolved"].equals(
        data.loc[data["zip_source"].eq("reported"), "location_zipcode_original"]
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(args.output, index=False, compression="gzip")

    source_counts = data["zip_source"].value_counts()
    report = {
        "configuration": {
            "k": args.k,
            "confidence_threshold": args.confidence_threshold,
            "max_distance_miles": args.max_distance_miles,
            "test_size": args.test_size,
            "random_state": args.random_state,
        },
        "training": {
            "reported_zip_rows_with_valid_coordinates": int(known.sum()),
            "unique_coordinate_groups": int(len(groups)),
            "coordinate_groups_with_conflicting_labels": int((groups["label_purity"] < 1.0).sum()),
        },
        "validation": validation,
        "rows": {
            "total": int(len(data)),
            "reported_zip": int(source_counts.get("reported", 0)),
            "knn_resolved": int(source_counts.get("knn", 0)),
            "unresolved": int(source_counts.get("unresolved", 0)),
            "missing_zip_with_valid_coordinates": int(missing_with_coordinates.sum()),
            "missing_zip_without_valid_coordinates": int((data["location_zipcode_original"].isna() & ~valid_coordinates).sum()),
            "final_zip_coverage": float(data["location_zipcode_resolved"].notna().mean()),
        },
        "output": str(args.output),
    }
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown_report(report, args.markdown_report)
    print(json.dumps({key: value for key, value in report.items() if key != "validation"}, indent=2))
    print(json.dumps({"validation": {key: value for key, value in validation.items() if key != "per_zip"}}, indent=2))


if __name__ == "__main__":
    main()
