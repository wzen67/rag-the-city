# Task D: 311 Service Equity by ZIP Code

## Question

Are assessed residential property values associated with differences in Boston 311 service
performance across ZIP codes?

This is a descriptive equity screen. It identifies patterns worth investigating; it does **not**
show that property values cause service differences or prove discrimination.

## Key finding

- Reported ZIPs: The association was very weak: higher-value ZIPs tended to have shorter median resolution times (rho = -0.176).
- Reported ZIPs: The association was weak: higher-value ZIPs tended to have higher closed on-time percentages (rho = +0.259).
- Reported + accepted KNN ZIPs:
  The association was weak: higher-value ZIPs tended to have shorter median resolution times (rho = -0.258).
- Reported + accepted KNN ZIPs:
  The association was very weak: higher-value ZIPs tended to have higher closed on-time percentages (rho = +0.180).
- **Bottom line:** The correlation directions remain the same after accepted KNN ZIPs are added, but the quartile summaries change enough that the size of the difference should be treated as sensitive to ZIP completion. These are weak ZIP-level associations, not evidence
  that assessed property value caused different service treatment.

## Data and coverage

- 311 source rows: **78,526** unique cases.
- Cases with an originally reported ZIP: **59,156 (75.3%)**.
- Cases with a reported or accepted KNN-resolved ZIP: **76,505 (97.4%)**.
- Accepted KNN assignments: **17,349**. KNN assignments use the separately validated
  confidence and distance rules recorded in `eval-results/knn_zip_validation.md`.
- Property source rows: **184,552**; residential parcels with a valid ZIP
  and positive assessed value: **137,224**.
- Duplicate non-missing property PID occurrences observed before aggregation:
  **0**. Property metrics follow the repository's
  `property_homes` definition and therefore retain the source parcel grain.

Only ZIPs with at least **30 closed cases with valid durations** and
**30 residential properties** are included in correlation and quartile comparisons.
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
| Reported ZIP only | 27 | -0.176 | +0.259 | 1.7 | 2.0 | 81.7% | 85.1% |
| Reported + accepted KNN ZIP | 27 | -0.258 | +0.180 | 2.0 | 1.8 | 82.3% | 82.0% |

Compare the two rows above before stating a finding. If direction or magnitude changes materially,
report the result as sensitive to ZIP completion rather than presenting one definitive conclusion.

## ZIP-level resolved view

| ZIP | Median assessed value | 311 cases | Open backlog | Median resolution days | Closed on-time % |
|---|---:|---:|---:|---:|---:|
| 02136 | $582,000 | 1,565 | 660 | 8.5 | 71.0% |
| 02122 | $701,100 | 2,414 | 1,202 | 7.0 | 80.5% |
| 02131 | $703,500 | 2,783 | 1,313 | 6.2 | 74.4% |
| 02132 | $741,600 | 1,391 | 578 | 4.9 | 76.1% |
| 02128 | $700,300 | 5,283 | 3,215 | 4.7 | 84.2% |
| 02129 | $903,750 | 2,716 | 1,657 | 3.8 | 81.1% |
| 02125 | $737,550 | 3,746 | 1,882 | 3.1 | 82.0% |
| 02135 | $567,100 | 4,468 | 2,430 | 3.0 | 79.0% |
| 02124 | $731,900 | 4,159 | 1,871 | 2.8 | 79.8% |
| 02108 | $1,436,300 | 1,083 | 539 | 2.8 | 82.0% |
| 02127 | $836,700 | 8,153 | 4,429 | 2.6 | 89.7% |
| 02126 | $625,400 | 1,660 | 701 | 2.1 | 75.4% |
| 02130 | $807,550 | 4,071 | 1,900 | 2.0 | 87.1% |
| 02134 | $635,250 | 2,357 | 1,235 | 2.0 | 83.7% |
| 02113 | $609,300 | 1,115 | 617 | 2.0 | 82.3% |
| 02110 | $1,370,650 | 522 | 315 | 1.9 | 81.2% |
| 02210 | $1,435,500 | 1,649 | 1,160 | 1.8 | 83.4% |
| 02116 | $1,172,600 | 5,688 | 2,508 | 1.8 | 72.3% |
| 02119 | $631,700 | 3,123 | 1,205 | 1.2 | 82.8% |
| 02215 | $555,350 | 1,888 | 995 | 1.1 | 85.6% |
| 02115 | $754,800 | 1,921 | 876 | 1.1 | 88.2% |
| 02114 | $688,200 | 1,793 | 1,022 | 1.0 | 87.8% |
| 02121 | $729,050 | 2,331 | 996 | 1.0 | 82.2% |
| 02111 | $852,100 | 1,815 | 1,119 | 0.9 | 83.5% |
| 02118 | $930,050 | 6,332 | 2,001 | 0.8 | 94.4% |
| 02109 | $800,050 | 815 | 533 | 0.7 | 84.0% |
| 02120 | $884,900 | 1,459 | 709 | 0.7 | 89.1% |

## Interpretation guardrails

- Use **associated with**, **correlated with**, or **differs across**. Do not use causal wording.
- Assessed value is not household income, rent, or an individual's wealth.
- ZIP codes are broad geographic areas and can hide within-ZIP differences.
- The 311 data covers a partial year; seasonal effects and unresolved open cases remain.
- A 311 request reflects both an underlying issue and a resident's ability or willingness to report it.
- KNN fills ZIP labels from nearby coordinates; it does not create new requests or change timestamps.
- Small-ZIP metrics are volatile, which is why comparison thresholds are applied.
- This analysis is a screening result for follow-up, not a ranking of residents or neighborhoods.
