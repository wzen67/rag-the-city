# `rag/` — routing and retrieval layer (Role B)

Numbers come from SQL over every row. **Retrieval's job is to make that SQL
correct**, not to replace it.

## Modules

| Module | Purpose |
|---|---|
| `router.py` | Classify a question into one of six routes. Rules only, no model call. |
| `retrieval.py` | Hybrid BM25 + dense retrieval fused with Reciprocal Rank Fusion. |
| `schema.py` | Schema-grounded SQL generation, plus the guardrails that make executing generated SQL safe. |
| `citations.py` | The citation contract. Every grounded answer carries a traceable source or it is not an answer. |
| `neighborhoods.py` | The 26 canonical BPDA neighborhoods, plus alias resolution for 311's compound labels and residents' nicknames. |
| `datasets.py` | Dataset registry and the read options each file actually needs. **Shared with Role A.** |
| `llm.py` | Thin Ollama client: embed, generate, health. |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ollama pull qwen2.5-coder:7b && ollama pull granite3.1-dense:8b && ollama pull bge-m3
.venv/bin/python -m pytest tests/ -q
```

## The six routes

```
AGGREGATE       a number over many rows -> SQL. Never retrieval.
SCORECARD       a named neighborhood's profile, or a comparison.
DEFINITION      what a column or code means -> reference documents.
LOOKUP          qualitative or thematic -> hybrid retrieval.
VALUE_JUDGMENT  "is it safe?" -> metrics plus an explicit refusal to rank.
UNANSWERABLE    something the data provably does not record -> abstain.
```

`Decision.matched` carries the patterns that fired, so a demo can show *why*
a question routed the way it did.

## Interface for Role A (SQL layer)

`datasets.connect()` returns a DuckDB connection with one view per dataset,
named by registry key: `service_requests`, `crime`, `food`, `property`,
`open_space`, `neighborhoods`.

**The view names are the contract.** Replace the view *bodies* with the
richer versions — canonical neighborhood via spatial join, offense-code
dimension join — and everything downstream keeps working. Generated SQL
says `FROM crime`, never a raw `read_csv_auto`.

Note `warm_category_cache()` should be called once at startup: it
pre-computes the categorical value lists, which otherwise costs ~14s on the
first question.

## Interface for Role C (eval)

- `schema.generate_sql(q, key, grounding, include_notes=...)` — set
  `grounding=None, include_notes=False` for the **control arm** of the
  schema-grounding A/B. Leaving notes on gives the control half the help and
  understates the measured benefit.
- `Answer.abstained` / `Answer.declined` separate "the data doesn't say" from
  "I won't issue a verdict"; count them separately.
- `Answer.__post_init__` raises if a grounded answer has no citations, so an
  uncited answer cannot silently reach the eval.

## Data traps this layer encodes

Each of these produces a confidently wrong number rather than an error, so
they are captured in `datasets.py` and in `schema.reference_documents()`:

| Trap | Effect if ignored |
|---|---|
| `open_space` read with `ignore_errors=true` | 272 of 577 rows; parkland undercounted by **60%** |
| `crime.OFFENSE_CODE_GROUP` / `UCR_PART` | **100% empty** across all 290,130 rows; violent-vs-property is uncomputable without the offense-code lookup |
| `crime.OFFENSE_DESCRIPTION` | includes non-crime activity (`SICK ASSIST`, `INVESTIGATE PERSON`), so `count(*)` overstates crime |
| `property` money columns | all VARCHAR with comma separators (`'822,900'`), `gross_tax` also has `$`; arithmetic needs a cast |
| `property.lu_desc` | values are `RESIDENTIAL CONDO`, `SINGLE FAM DWELLING` — never plain `RESIDENTIAL`, so a guessed filter matches zero rows |
| `service_requests.neighborhood` | non-canonical; compound labels plus a city-wide `Boston` bucket (4,733 rows) |
| `service_requests.case_status` | open cases have null `closed_dt`; any average duration must exclude them |
| `bpda-neighborhood-boundaries.csv` | its `shape_wkt` column is **empty**; usable geometry is in the GeoJSON |

## Verified measurements

- Point-in-polygon against the BPDA GeoJSON assigns all **290,130 crime rows
  in 5.6s** (City Hall -> Downtown, Fenway Park -> Fenway). This is exact
  attribution and is preferable to the police-district crosswalk, whose modal
  purity is as low as 47.7% for D4/South End.
- `bge-m3` embeddings are 1024-dim and served by Ollama, so multilingual
  retrieval needs no PyTorch install.
- Schema grounding changed a failing query into a correct one on
  "average days to close a 311 case": without it the model divided two
  intervals and crashed; with it the model added `WHERE closed_dt IS NOT
  NULL` — the actual trap.
