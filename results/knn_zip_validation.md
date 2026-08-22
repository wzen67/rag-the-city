# KNN ZIP Imputation Validation

Missing 311 ZIP codes are inferred only when distance-weighted KNN meets both a confidence and distance threshold.
Reported ZIP codes are never overwritten.

## Configuration

- K: 5
- Confidence threshold: 0.80
- Maximum nearest-neighbor distance: 0.50 miles
- Distance metric: Haversine
- Validation split: coordinate-group holdout (repeated coordinates cannot leak across train/test)

## Validation

- Raw accuracy: 98.31%
- Accepted prediction accuracy: 99.22%
- Validation acceptance rate: 97.11%
- Accepted validation rows: 5,109
- Rejected validation rows: 152

## Production coverage

- Total rows: 78,526
- Reported ZIP rows: 59,156
- KNN-resolved rows: 17,349
- Unresolved rows: 2,021
- Final ZIP coverage: 97.43%

## Limitations

KNN estimates ZIP codes from nearby labeled 311 coordinates rather than official ZIP polygons. Boundary locations may be misclassified. Low-confidence or distant predictions remain unresolved.
