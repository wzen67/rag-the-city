# Eval results — Boston Neighborhood Intelligence

Generated: 2026-08-22 15:48 UTC
System module: `not wired — engine.ask() missing`
Schema grounding flag (ours default run): `n/a — route not wired`

Mechanical scoring only (exact/tolerance counts, regex abstention, citation presence).
Gold values: DuckDB over `data/*.csv.gz` via `eval/fill_gold.py`.

## Naive vs ours

| Metric | Naive baseline | Ours | Delta |
| --- | --- | --- | --- |
| Overall accuracy | 0% | n/a — route not wired | |
| Counting questions correct | 0 / 7 | n/a — route not wired | |
| Retrieval hit rate @5 | 0% | n/a — route not wired | |
| SQL accuracy without schema grounding | n/a (naive has no SQL) | n/a — re-run with --no-schema-grounding after B wires SQL | |
| SQL accuracy with schema grounding | n/a | n/a — route not wired | |
| Correct abstentions | 0 / 4 | n/a — route not wired | |
| Fabrications | 11 | n/a — route not wired | |
| Citations present (non-abstain) | 0 / 16 | n/a — route not wired | |

## By persona

| Persona | Naive | Ours |
| --- | --- | --- |
| resident | 0 / 10 | n/a — route not wired |
| manager | 0 / 10 | n/a — route not wired |

## Per-question (naive; system not wired)

| ID | Persona | Route | Pass | Notes |
| --- | --- | --- | --- | --- |
| R01 | resident | scorecard | no | scorecard missing name or metric |
| R02 | resident | aggregate | no | count gold=757 miss |
| R03 | resident | lookup | no | missing expected theme |
| R04 | resident | definition | no | definition miss |
| R05 | resident | aggregate | no | count gold=62.3 miss |
| R06 | resident | value_judgment | no | must refuse 'safe' and still cite metrics |
| R07 | resident | unanswerable | no | expected abstention |
| R08 | resident | aggregate | no | count gold=45.7 miss |
| M01 | manager | aggregate | no | rank miss |
| M02 | manager | aggregate | no | equity miss |
| M03 | manager | aggregate | no | rank miss |
| M04 | manager | aggregate | no | count gold=3537 miss |
| M05 | manager | aggregate | no | count gold=7005 miss |
| M06 | manager | unanswerable | no | expected abstention |
| M07 | manager | aggregate | no | rank miss |
| M08 | manager | unanswerable | no | expected abstention |
| T01 | resident | aggregate | no | count gold=7957 miss |
| T02 | manager | unanswerable | no | expected abstention |
| T03 | resident | value_judgment | no | must refuse 'safe' and still cite metrics |
| T04 | manager | aggregate | no | count gold=0 miss |

## Gaps

- engine.ask() not importable (tried src.qa, src.engine, engine, app.engine). Ours column is n/a until Person B lands the router.

## How to reproduce

```
python eval/fill_gold.py
python eval/naive_baseline.py
python eval/run_eval.py
python eval/run_eval.py --no-schema-grounding
```

Never cut this file. Empty eval output does not count for Track A.
