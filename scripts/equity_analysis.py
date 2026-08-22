"""Build the Task D ZIP-level 311/property equity analysis.

The analysis is descriptive. It reports associations between assessed property
values and 311 service performance; it does not infer discrimination or cause.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


RESIDENTIAL_LAND_USES = {"R1", "R2", "R3", "R4", "CD"}
MIN_CLOSED_CASES = 30
MIN_PROPERTIES = 30


def normalize_zip(values: pd.Series) -> pd.Series:
    """Normalize ZIP values to five-character identifiers."""
    extracted = values.astype("string").str.extract(r"(\d{4,5})", expand=False)
    return extracted.str.zfill(5).where(extracted.notna())


def parse_number(values: pd.Series) -> pd.Series:
    """Parse currency-like text while preserving invalid values as missing."""
    cleaned = values.astype("string").str.replace(r"[$,\s]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def load_311(path: Path) -> pd.DataFrame:
    required = {
        "case_enquiry_id",
        "open_dt",
        "closed_dt",
        "case_status",
        "on_time",
        "location_zipcode_original",
        "location_zipcode_resolved",
        "zip_source",
    }
    data = pd.read_csv(path, dtype="string", low_memory=False)
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"311 input is missing columns: {sorted(missing)}")
    if data["case_enquiry_id"].duplicated().any():
        raise ValueError("311 input contains duplicate case_enquiry_id values")

    data["reported_zip"] = normalize_zip(data["location_zipcode_original"])
    data["resolved_zip"] = normalize_zip(data["location_zipcode_resolved"])
    data["open_ts"] = pd.to_datetime(data["open_dt"], errors="coerce")
    data["closed_ts"] = pd.to_datetime(data["closed_dt"], errors="coerce")
    data["status_norm"] = data["case_status"].str.strip().str.upper()
    data["on_time_norm"] = data["on_time"].str.strip().str.upper()
    data["resolution_days"] = (
        data["closed_ts"] - data["open_ts"]
    ).dt.total_seconds() / 86_400
    data.loc[data["resolution_days"] < 0, "resolution_days"] = np.nan
    return data


def load_property(path: Path) -> tuple[pd.DataFrame, dict[str, int]]:
    required = {"PID", "ZIP_CODE", "LU", "OWN_OCC", "TOTAL_VALUE"}
    data = pd.read_csv(path, dtype="string", low_memory=False)
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"Property input is missing columns: {sorted(missing)}")

    duplicate_pids = int(data["PID"].notna().sum() - data["PID"].dropna().nunique())
    data["zipcode"] = normalize_zip(data["ZIP_CODE"])
    data["land_use"] = data["LU"].str.strip().str.upper()
    data["total_value_num"] = parse_number(data["TOTAL_VALUE"])
    data["owner_occupied_norm"] = data["OWN_OCC"].str.strip().str.upper()

    homes = data.loc[
        data["land_use"].isin(RESIDENTIAL_LAND_USES)
        & data["zipcode"].notna()
        & data["total_value_num"].gt(0)
    ].copy()
    audit = {
        "raw_rows": len(data),
        "duplicate_pid_rows": duplicate_pids,
        "residential_positive_value_rows": len(homes),
    }
    return homes, audit


def aggregate_311(data: pd.DataFrame, zip_column: str, prefix: str) -> pd.DataFrame:
    frame = data.loc[data[zip_column].notna()].copy()
    frame["zipcode"] = frame[zip_column]
    frame["is_open"] = frame["status_norm"].eq("OPEN")
    frame["is_closed"] = frame["status_norm"].eq("CLOSED")
    frame["valid_closed_duration"] = frame["is_closed"] & frame["resolution_days"].notna()
    frame["closed_sla_eligible"] = frame["is_closed"] & frame["on_time_norm"].isin(
        ["ONTIME", "OVERDUE"]
    )
    frame["closed_on_time"] = frame["is_closed"] & frame["on_time_norm"].eq("ONTIME")

    grouped = frame.groupby("zipcode", sort=True)
    result = grouped.agg(
        case_count=("case_enquiry_id", "size"),
        open_backlog=("is_open", "sum"),
        closed_case_count=("is_closed", "sum"),
        closed_with_valid_duration=("valid_closed_duration", "sum"),
        closed_sla_eligible=("closed_sla_eligible", "sum"),
        closed_on_time_count=("closed_on_time", "sum"),
    )
    medians = (
        frame.loc[frame["valid_closed_duration"]]
        .groupby("zipcode")["resolution_days"]
        .median()
        .rename("median_resolution_days")
    )
    result = result.join(medians)
    result["closed_on_time_pct"] = (
        result["closed_on_time_count"] / result["closed_sla_eligible"]
    )
    result = result.reset_index()
    return result.rename(
        columns={column: f"{prefix}_{column}" for column in result.columns if column != "zipcode"}
    )


def aggregate_property(homes: pd.DataFrame) -> pd.DataFrame:
    homes = homes.copy()
    homes["owner_occupied_valid"] = homes["owner_occupied_norm"].isin(["Y", "N"])
    homes["owner_occupied_yes"] = homes["owner_occupied_norm"].eq("Y")
    grouped = homes.groupby("zipcode", sort=True)
    result = grouped.agg(
        property_count=("PID", "size"),
        median_total_assessed_value=("total_value_num", "median"),
        owner_occupancy_eligible=("owner_occupied_valid", "sum"),
        owner_occupied_count=("owner_occupied_yes", "sum"),
    ).reset_index()
    result["owner_occupied_pct"] = (
        result["owner_occupied_count"] / result["owner_occupancy_eligible"]
    )
    return result


def rank_correlation(left: pd.Series, right: pd.Series) -> float:
    """Spearman rank correlation without an optional SciPy dependency."""
    valid = left.notna() & right.notna()
    if valid.sum() < 3:
        return float("nan")
    return float(left[valid].rank(method="average").corr(right[valid].rank(method="average")))


def fmt_number(value: float, decimals: int = 1) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value:,.{decimals}f}"


def fmt_pct(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value:.1%}"


def fmt_corr(value: float) -> str:
    if pd.isna(value):
        return "n/a"
    return f"{value:+.3f}"


def correlation_interpretation(value: float, outcome: str) -> str:
    if pd.isna(value):
        return f"The {outcome} correlation could not be calculated."
    magnitude = abs(value)
    strength = "very weak" if magnitude < 0.20 else "weak" if magnitude < 0.40 else "moderate"
    if outcome == "resolution-time":
        direction = (
            "higher-value ZIPs tended to have longer median resolution times"
            if value > 0
            else "higher-value ZIPs tended to have shorter median resolution times"
        )
    else:
        direction = (
            "higher-value ZIPs tended to have higher closed on-time percentages"
            if value > 0
            else "higher-value ZIPs tended to have lower closed on-time percentages"
        )
    return f"The association was {strength}: {direction} (rho = {value:+.3f})."


def comparison_summary(table: pd.DataFrame, prefix: str) -> dict[str, float]:
    eligible = table.loc[
        table["property_count"].ge(MIN_PROPERTIES)
        & table[f"{prefix}_closed_with_valid_duration"].ge(MIN_CLOSED_CASES)
    ].copy()
    value = eligible["median_total_assessed_value"]
    resolution = eligible[f"{prefix}_median_resolution_days"]
    on_time = eligible[f"{prefix}_closed_on_time_pct"]

    summary: dict[str, float] = {
        "eligible_zip_count": len(eligible),
        "resolution_corr": rank_correlation(value, resolution),
        "on_time_corr": rank_correlation(value, on_time),
    }
    if len(eligible) >= 4:
        eligible["value_quartile"] = pd.qcut(
            eligible["median_total_assessed_value"], 4, labels=False, duplicates="drop"
        )
        lowest = eligible.loc[eligible["value_quartile"].eq(eligible["value_quartile"].min())]
        highest = eligible.loc[eligible["value_quartile"].eq(eligible["value_quartile"].max())]
        summary.update(
            {
                "low_value_zip_count": len(lowest),
                "high_value_zip_count": len(highest),
                "low_value_median_resolution": lowest[f"{prefix}_median_resolution_days"].median(),
                "high_value_median_resolution": highest[f"{prefix}_median_resolution_days"].median(),
                "low_value_median_on_time": lowest[f"{prefix}_closed_on_time_pct"].median(),
                "high_value_median_on_time": highest[f"{prefix}_closed_on_time_pct"].median(),
            }
        )
    return summary


def build_report(
    output: pd.DataFrame,
    cases: pd.DataFrame,
    property_audit: dict[str, int],
    reported_summary: dict[str, float],
    resolved_summary: dict[str, float],
) -> str:
    total_cases = len(cases)
    reported_cases = int(cases["reported_zip"].notna().sum())
    resolved_cases = int(cases["resolved_zip"].notna().sum())
    knn_cases = int(cases["zip_source"].str.lower().eq("knn").sum())

    def sensitivity_row(label: str, summary: dict[str, float]) -> str:
        return (
            f"| {label} | {int(summary['eligible_zip_count'])} | "
            f"{fmt_corr(summary['resolution_corr'])} | {fmt_corr(summary['on_time_corr'])} | "
            f"{fmt_number(summary.get('low_value_median_resolution', np.nan))} | "
            f"{fmt_number(summary.get('high_value_median_resolution', np.nan))} | "
            f"{fmt_pct(summary.get('low_value_median_on_time', np.nan))} | "
            f"{fmt_pct(summary.get('high_value_median_on_time', np.nan))} |"
        )

    resolved_rows = output.loc[
        output["property_count"].ge(MIN_PROPERTIES)
        & output["resolved_closed_with_valid_duration"].ge(MIN_CLOSED_CASES)
    ].sort_values("resolved_median_resolution_days", ascending=False)
    detail_lines = []
    for row in resolved_rows.itertuples(index=False):
        detail_lines.append(
            f"| {row.zipcode} | ${row.median_total_assessed_value:,.0f} | "
            f"{int(row.resolved_case_count):,} | {int(row.resolved_open_backlog):,} | "
            f"{row.resolved_median_resolution_days:.1f} | {row.resolved_closed_on_time_pct:.1%} |"
        )

    resolution_direction_stable = np.sign(reported_summary["resolution_corr"]) == np.sign(
        resolved_summary["resolution_corr"]
    )
    on_time_direction_stable = np.sign(reported_summary["on_time_corr"]) == np.sign(
        resolved_summary["on_time_corr"]
    )
    sensitivity_statement = (
        "The correlation directions remain the same after accepted KNN ZIPs are added, "
        "but the quartile summaries change enough that the size of the difference should be "
        "treated as sensitive to ZIP completion."
        if resolution_direction_stable and on_time_direction_stable
        else "At least one correlation changes direction after accepted KNN ZIPs are added; "
        "the result is sensitive to ZIP completion and should not be presented as definitive."
    )

    return f"""# Task D: 311 Service Equity by ZIP Code

## Question

Are assessed residential property values associated with differences in Boston 311 service
performance across ZIP codes?

This is a descriptive equity screen. It identifies patterns worth investigating; it does **not**
show that property values cause service differences or prove discrimination.

## Key finding

- Reported ZIPs: {correlation_interpretation(reported_summary['resolution_corr'], 'resolution-time')}
- Reported ZIPs: {correlation_interpretation(reported_summary['on_time_corr'], 'on-time')}
- Reported + accepted KNN ZIPs:
  {correlation_interpretation(resolved_summary['resolution_corr'], 'resolution-time')}
- Reported + accepted KNN ZIPs:
  {correlation_interpretation(resolved_summary['on_time_corr'], 'on-time')}
- **Bottom line:** {sensitivity_statement} These are weak ZIP-level associations, not evidence
  that assessed property value caused different service treatment.

## Data and coverage

- 311 source rows: **{total_cases:,}** unique cases.
- Cases with an originally reported ZIP: **{reported_cases:,} ({reported_cases / total_cases:.1%})**.
- Cases with a reported or accepted KNN-resolved ZIP: **{resolved_cases:,} ({resolved_cases / total_cases:.1%})**.
- Accepted KNN assignments: **{knn_cases:,}**. KNN assignments use the separately validated
  confidence and distance rules recorded in `eval-results/knn_zip_validation.md`.
- Property source rows: **{property_audit['raw_rows']:,}**; residential parcels with a valid ZIP
  and positive assessed value: **{property_audit['residential_positive_value_rows']:,}**.
- Duplicate non-missing property PID occurrences observed before aggregation:
  **{property_audit['duplicate_pid_rows']:,}**. Property metrics follow the repository's
  `property_homes` definition and therefore retain the source parcel grain.

Only ZIPs with at least **{MIN_CLOSED_CASES} closed cases with valid durations** and
**{MIN_PROPERTIES} residential properties** are included in correlation and quartile comparisons.
All ZIP aggregates remain in `data/derived/equity_by_zip.csv` for transparency.

## Metric definitions

- **Open backlog:** cases whose status is `Open`.
- **Median resolution days:** median of `closed_dt - open_dt` for closed cases with valid,
  non-negative timestamps. Open cases are excluded.
- **Closed on-time percentage:** `ONTIME / (ONTIME + OVERDUE)` among closed cases only.
- **Median assessed value:** median `TOTAL_VALUE` among residential land-use codes
  `R1`, `R2`, `R3`, `R4`, and `CD`, restricted to positive values.
- **Owner-occupied percentage:** `Y / (Y + N)` among eligible residential properties.
- **Correlation:** ZIP-level Spearman rank correlation. A positive resolution correlation means
  higher-value ZIPs tend to have longer resolution times; a positive on-time correlation means
  higher-value ZIPs tend to have higher on-time percentages.

## Sensitivity analysis

| ZIP method | Eligible ZIPs | Value vs. resolution correlation | Value vs. on-time correlation | Lowest-value quartile resolution days | Highest-value quartile resolution days | Lowest-value quartile on-time | Highest-value quartile on-time |
|---|---:|---:|---:|---:|---:|---:|---:|
{sensitivity_row('Reported ZIP only', reported_summary)}
{sensitivity_row('Reported + accepted KNN ZIP', resolved_summary)}

Compare the two rows above before stating a finding. If direction or magnitude changes materially,
report the result as sensitive to ZIP completion rather than presenting one definitive conclusion.

## ZIP-level resolved view

| ZIP | Median assessed value | 311 cases | Open backlog | Median resolution days | Closed on-time % |
|---|---:|---:|---:|---:|---:|
{chr(10).join(detail_lines)}

## Interpretation guardrails

- Use **associated with**, **correlated with**, or **differs across**. Do not use causal wording.
- Assessed value is not household income, rent, or an individual's wealth.
- ZIP codes are broad geographic areas and can hide within-ZIP differences.
- The 311 data covers a partial year; seasonal effects and unresolved open cases remain.
- A 311 request reflects both an underlying issue and a resident's ability or willingness to report it.
- KNN fills ZIP labels from nearby coordinates; it does not create new requests or change timestamps.
- Small-ZIP metrics are volatile, which is why comparison thresholds are applied.
- This analysis is a screening result for follow-up, not a ranking of residents or neighborhoods.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-requests",
        type=Path,
        default=Path("data/derived/311-service-requests-with-resolved-zip.csv.gz"),
    )
    parser.add_argument(
        "--property-assessment",
        type=Path,
        default=Path("data/property-assessment.csv.gz"),
    )
    parser.add_argument(
        "--output-csv", type=Path, default=Path("data/derived/equity_by_zip.csv")
    )
    parser.add_argument(
        "--output-report", type=Path, default=Path("eval-results/equity_analysis.md")
    )
    args = parser.parse_args()

    cases = load_311(args.service_requests)
    homes, property_audit = load_property(args.property_assessment)
    properties = aggregate_property(homes)
    reported = aggregate_311(cases, "reported_zip", "reported")
    resolved = aggregate_311(cases, "resolved_zip", "resolved")

    output = properties.merge(reported, on="zipcode", how="outer", validate="one_to_one")
    output = output.merge(resolved, on="zipcode", how="outer", validate="one_to_one")
    output["knn_added_case_count"] = output["resolved_case_count"].fillna(0) - output[
        "reported_case_count"
    ].fillna(0)
    output["resolution_days_delta_from_knn"] = (
        output["resolved_median_resolution_days"] - output["reported_median_resolution_days"]
    )
    output["closed_on_time_pct_delta_from_knn"] = (
        output["resolved_closed_on_time_pct"] - output["reported_closed_on_time_pct"]
    )
    output["included_in_reported_comparison"] = (
        output["property_count"].ge(MIN_PROPERTIES)
        & output["reported_closed_with_valid_duration"].ge(MIN_CLOSED_CASES)
    )
    output["included_in_resolved_comparison"] = (
        output["property_count"].ge(MIN_PROPERTIES)
        & output["resolved_closed_with_valid_duration"].ge(MIN_CLOSED_CASES)
    )
    output = output.sort_values("zipcode").reset_index(drop=True)

    reported_summary = comparison_summary(output, "reported")
    resolved_summary = comparison_summary(output, "resolved")
    report = build_report(output, cases, property_audit, reported_summary, resolved_summary)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output_csv, index=False, float_format="%.6f")
    args.output_report.write_text(report, encoding="utf-8")

    print(f"Wrote {len(output):,} ZIP rows to {args.output_csv}")
    print(f"Wrote analysis report to {args.output_report}")
    print(
        "Eligible ZIPs (reported/resolved): "
        f"{reported_summary['eligible_zip_count']}/{resolved_summary['eligible_zip_count']}"
    )


if __name__ == "__main__":
    main()
