# Eval results — Boston Neighborhood Intelligence

Generated: 2026-08-22 19:16 UTC
System module: `engine`
Schema grounding flag (ours default run): `1 / 4`

Mechanical scoring only (exact/tolerance counts, regex abstention, citation presence).
Gold values: DuckDB over `data/*.csv.gz` via `eval/fill_gold.py`.

## Naive vs ours

| Metric | Naive baseline | Ours | Delta |
| --- | --- | --- | --- |
| Overall accuracy | 0% | 25% | |
| Counting questions correct | 0 / 7 | 0 / 7 | |
| Retrieval hit rate @5 | 0% | 0% | |
| SQL accuracy without schema grounding | n/a (naive has no SQL) | n/a — re-run with --no-schema-grounding after B wires SQL | |
| SQL accuracy with schema grounding | n/a | 1 / 4 | |
| Correct abstentions | 0 / 4 | 2 / 4 | |
| Fabrications | 11 | 5 | |
| Citations present (non-abstain) | 0 / 16 | 13 / 16 | |

## By persona

| Persona | Naive | Ours |
| --- | --- | --- |
| resident | 0 / 10 | 2 / 10 |
| manager | 0 / 10 | 3 / 10 |

## Per-question (ours)

| ID | Persona | Route | Pass | Notes |
| --- | --- | --- | --- | --- |
| R01 | resident | scorecard | no | scorecard missing name or metric |
| R02 | resident | aggregate | no | count gold=757 miss |
| R03 | resident | lookup | no | missing expected theme |
| R04 | resident | definition | no | definition miss |
| R05 | resident | aggregate | no | count gold=62.3 miss |
| R06 | resident | value_judgment | yes | value-judgment refuse+metrics |
| R07 | resident | unanswerable | no | expected abstention |
| R08 | resident | aggregate | no | count gold=45.7 miss |
| M01 | manager | aggregate | no | rank miss |
| M02 | manager | aggregate | no | equity miss |
| M03 | manager | aggregate | yes | rank labels present |
| M04 | manager | aggregate | no | count gold=3537 miss |
| M05 | manager | aggregate | no | count gold=7005 miss |
| M06 | manager | unanswerable | yes | abstain |
| M07 | manager | aggregate | no | rank miss |
| M08 | manager | unanswerable | yes | abstain |
| T01 | resident | aggregate | no | count gold=7957 miss |
| T02 | manager | unanswerable | no | expected abstention |
| T03 | resident | value_judgment | yes | value-judgment refuse+metrics |
| T04 | manager | aggregate | no | count gold=0 miss |

## Gaps

- None recorded.

## How to reproduce

```
python eval/fill_gold.py
python eval/naive_baseline.py
python eval/run_eval.py
python eval/run_eval.py --no-schema-grounding
```

Never cut this file. Empty eval output does not count for Track A.
