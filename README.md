# Boston Neighborhood Intelligence

Track A (“The Engine”) — a question-answering engine over Boston open data.

One Neighborhood Scorecard, two frames: a new resident asking “should I live here?” and a city manager asking “how is each neighborhood performing?”

**Pitch:** Numbers come from SQL over 100% of rows. Retrieval’s job is to make that SQL correct — and to say when the data does not record the answer.

## Architecture

```
question → router → AGGREGATE | SCORECARD | DEFINITION | LOOKUP | VALUE JUDGMENT | UNANSWERABLE
                         │           │            │          │            │                │
                      DuckDB      DuckDB      docs only   hybrid     metrics +         abstain
                      full rows   + cards                 BM25+dense  refuse to rank
```

- **DuckDB** computes every count, rate, trend, and comparison over the full CSVs (`read_csv_auto`). We do not ask a language model to eyeball totals.
- **Retrieval** injects field definitions into the SQL prompt (schema grounding), answers definitional questions, and retrieves qualitative 311/inspection text.
- **Dimension tables** (offense codes, CRM value index, occupancy codes) are **joined in SQL**, never retrieved. `UCR_PART` and `OFFENSE_CODE_GROUP` are empty on the live crime extract; “violent vs property vs non-crime” requires the RMS offense-code table.
- **Citations** on every non-abstaining answer: source dataset, generated SQL or row/case id, row count, geography-crosswalk purity when district ≠ neighborhood.
- **Abstention:** low retrieval confidence or no supporting data → “the data does not record this.” Tourism, restaurant quality, and license-suspension *reasons* are in-scope refusals.
- **Value judgment** is not abstention. “Is Roxbury safe?” returns cited metrics and an explicit refusal to rank neighborhoods.

Person B owns the router. Contract the eval harness expects:

```python
def ask(question: str, schema_grounding: bool = True) -> dict:
    # answer, sql, citations, retrieved_ids, abstained, refused_value_judgment
    ...
```

Import path: `src.qa`, `src.engine`, `engine`, or `app.engine`.

## Layout

- `data/` contains source CSVs, `dd.md`, and derived CSVs in `data/derived/`.
- `parse_csvs.py` transforms source exports into normalized entity tables.
- `mcp_server.py` exposes the derived table catalog through FastMCP.
- `run_mcp.sh` starts the HTTP MCP server with Conda environment `nn`.
- `eval/` is the Person C gold set, naive baseline, and harness.
- `eval-results/eval.md` is the committed eval table.

## Datasets (Analyze Boston)

| # | Dataset | In-repo file | Serves |
| --- | --- | --- | --- |
| 1 | 311 Service Requests | `data/311-service-requests.csv.gz` | Responsiveness, themes, geography hub |
| 2 | Crime Incident Reports (2023–present) | `data/crime-incident-reports-august-2015-to-date-source-new-system.csv.gz` | Safety |
| 3 | Food Establishment Inspections | `data/food-establishment-inspections.csv.gz` | Food safety (hygiene only) |
| 4 | Open Space | `data/open-space.csv.gz` | Parks / acres |
| 5 | Property Assessment FY2026 | add next to `data/` (or `~/Downloads/fy2026-property-assessment-data_rev.csv` for gold) | Housing cost / equity |

### Reference documents

| Document | Role |
| --- | --- |
| `rmsoffensecodes.xlsx` | DuckDB dim — mandatory (SICK ASSIST is not a crime) |
| `datadictionary-crmvaluecodeindex.pdf` | 311 reason/type/queue codes |
| `311-service-requests-data-dictionary-new-system.pdf` | Schema-grounded SQL |
| `rmscrimeincidentfieldexplanation.xlsx` | Crime field prose |
| `propertyoccupancycodes.pdf` | Residential vs commercial |
| `property-assessment-fy2026-data-key.pdf` | Assessment fields |

Food Establishment Inspections has **no dictionary on the portal**. That is a finding; it supports abstention, not guessing.

## Rebuild derived CSVs

```sh
conda run -n nn python parse_csvs.py
```

Outputs are written to `data/derived/`.

## Run the MCP server

```sh
./run_mcp.sh
```

The server listens on `http://127.0.0.1:3000/mcp` by default. Override the
host or port with `HOST` and `PORT`.

## Eval (Person C)

Track A 4-anchor: *measurably better than naive RAG, and they can show the numbers.* This repo commits `eval-results/eval.md`. An eval script with no output does not count.

| Artifact | Path |
| --- | --- |
| Gold questions (2 personas, 20 items) | [`eval/questions.json`](eval/questions.json) |
| Recompute gold from CSVs | [`eval/fill_gold.py`](eval/fill_gold.py) |
| Naive baseline (CSV head only — no SQL, no schema) | [`eval/naive_baseline.py`](eval/naive_baseline.py) |
| Harness | [`eval/run_eval.py`](eval/run_eval.py) |
| Committed numbers | [`eval-results/eval.md`](eval-results/eval.md) |

Question set includes: counting items, 4 abstentions, value-judgment (“is it safe?”), the B2 offense-code trap (naive `count(*)` = 10,540 in 2024; ours excluding PRD non-crime descriptions = 7,957), and a UCR_PART schema-grounding trap (column is empty).

```bash
python eval/fill_gold.py
python eval/run_eval.py
python eval/run_eval.py --no-schema-grounding   # SQL A/B once the engine is wired
```

`ask(..., schema_grounding=False)` must strip retrieved field definitions so the A/B is real.

### Eval table (latest harness run)

See [`eval-results/eval.md`](eval-results/eval.md) for the live table. Snapshot after gold fill, **before** the router lands:

| Metric | Naive baseline | Ours | Delta |
| --- | --- | --- | --- |
| Overall accuracy | 0% | n/a — route not wired | |
| Counting questions correct | 0 / 7 | n/a — route not wired | |
| Retrieval hit rate @5 | 0% | n/a — route not wired | |
| SQL accuracy without schema grounding | n/a (no SQL) | n/a — route not wired | |
| SQL accuracy with schema grounding | n/a | n/a — route not wired | |
| Correct abstentions | 0 / 4 | n/a — route not wired | |
| Fabrications | 11 | n/a until system run | |

Naive is *supposed* to fail counting: it never scans 100% of rows and it treats SICK ASSIST as crime.

## Limitations (honest scoping)

- **No tourism.** Analyze Boston does not cover attractions. We abstain.
- **No restaurant recommendations.** Inspections are hygiene, never quality or cuisine.
- **We never rank “best neighborhood.”** Value-judgment route returns metrics plus a refusal.
- **Crime geography is district-level.** B2 is ~66% Roxbury by 311 volume (purity disclosed). D4 is much mixed.
- **311 neighborhood labels disagree with themselves** (`Allston / Brighton` vs `Allston` vs `Brighton`; a meaningless `Boston` bucket). Canonical rollups are explicit.
- **In-repo 311 extract is 2026-only** — multi-year 311 trends are not in this file.
- **District-level crime ≠ neighborhood crime.** We say so.

## Eligibility

- Track A, locked.
- Public repo; solution code from the build window.
- Local / open-source stack (DuckDB, Chroma, BM25+RRF, Ollama). Cloud APIs are a parachute only.

## Team

| Role | Owns |
| --- | --- |
| A — Data & SQL | Crosswalk, DuckDB views, scorecard |
| B — Retrieval & Router | Hybrid search, schema-grounded SQL, citations |
| C — Eval & Docs | This README, `eval/`, `eval-results/eval.md` |
| D — Documents & Equity | Offense-code parse, equity ZIP join |
| E — Surface & adversarial QA | CLI / optional Streamlit, off-script questions |
