# Boston Neighborhood Intelligence

**RAG the City · Track A ("The Engine") · August 22, 2026**

A question-answering engine over Boston's open data that shows its work — or declines
honestly. Ask it about safety, city responsiveness, food safety, parks and housing
across Boston's 26 neighbourhoods.

> **Numbers come from SQL over every row. Retrieval's job is to make that SQL correct.**
>
> Embeddings cannot count, so no figure this system reports is read out of a vector
> store. Retrieval supplies the field semantics that make the generated query right.

---

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_db.py        # materialise boston.db (~1 min, 103 MB)

ollama pull qwen2.5-coder:7b                # writes SQL
ollama pull granite3.1-dense:8b             # writes grounded prose
ollama pull bge-m3                          # embeddings (multilingual, 1024-dim)

./run_web.sh        # demo UI      -> http://127.0.0.1:8000
./run_mcp.sh        # MCP server   -> http://127.0.0.1:3000/mcp
```

Other entry points:

```bash
.venv/bin/python -m pytest tests/ -q        # 179 tests
.venv/bin/python scripts/smoke_engine.py    # one question per route
PYTHONPATH=. .venv/bin/python eval/run_eval.py   # naive vs ours, writes eval-results/
```

```python
from engine import ask
ask("How many crimes were there in Roxbury in 2025?")["answer"]   # -> '4,543'
```

The demo UI supports shareable links: `?q=How+many+crimes+per+neighbourhood+in+2025`.

---

## The use case

Two personas, **one engine**. Both want the same computation, framed differently — a
resident asks about one neighbourhood, a manager ranks all of them — so there is one
scorecard layer, not two products.

| Persona | Asks | Example |
|---|---|---|
| **New resident** — *"should I live here?"* | safety, responsiveness, parks, food safety, housing cost | *"What is the typical home value in Dorchester?"* |
| **City manager** — *"how is each area performing?"* | SLA compliance, backlog, department comparison, service equity | *"Which department is slowest to close 311 cases?"* |

**Deliberately out of scope**, because the data cannot support it: tourism and
attractions (no such dataset), restaurant *quality* (inspections record hygiene, not
food), and reasons behind a licence suspension (codes, never causes). The system says
so rather than inventing an answer.

---

## Architecture

```
                                USER QUESTION
                                      │
                         ┌────────────▼────────────┐
                         │         ROUTER          │  rules only, no model call
                         └────────────┬────────────┘
        ┌───────────┬─────────────┬───┴────────┬──────────────┬──────────────┐
        ▼           ▼             ▼            ▼              ▼              ▼
   AGGREGATE   SCORECARD    DEFINITION      LOOKUP     VALUE JUDGMENT  UNANSWERABLE
   counts,     neighbourhood "what does    qualitative  "is it safe?"    no data
   trends      profile       X mean?"       themes      → metrics +      exists
        │           │             │            │         refusal          │
        ▼           ▼             ▼            ▼              ▼           ▼
   DuckDB over  scorecard    retrieval over  HYBRID      numbers,      "the data
   100% of rows SQL          reference docs  BM25+dense  no verdict     does not
        ▲           ▲                        via RRF                    record this"
        │           │
        └───────────┴── schema grounding: semantic-layer rules + worked
                        examples injected into the SQL prompt
                                      │
                                      ▼
                      GROUNDED ANSWER + CITATIONS
              dataset · generated SQL · row count · confidence
```

**Progressive disclosure.** A question walks six stages, each eliminating a class of
failure before the next runs, and every stage is recorded in `Answer.trace` so the demo
can show *how* it narrowed — not just what it concluded:

| # | Stage | Cost | What it does |
|---|-------|------|--------------|
| 0 | `guard` | ~0 ms | refuses person-directed lookups **before** retrieval runs |
| 1 | `disambiguate` | ~0 ms | resolves place names; surfaces compound aliases instead of guessing |
| 2 | `anchor` | ~0 ms | pins an explicit time window, warns when it exceeds coverage |
| 3 | `route` | ~5 ms | picks the engine and reports the pattern that fired |
| 4 | `execute` | SQL or model | SQL for numbers, retrieval for text, abstain for gaps |
| 5 | `signal` | ~0 ms | attaches an explicit confidence level |

Stages 0–3 are pure rules, which is why the UI can show the routing decision instantly
while the model call is still in flight.

---

## What we built

### Retrieval
- **Hybrid search**: BM25 (sparse) over tokenised text + `bge-m3` dense vectors in
  ChromaDB, merged by **Reciprocal Rank Fusion** (k=60). Results report *which*
  retriever found them.
- Sparse is not optional: a figure like `4200000` sits in a CSV field with no currency
  symbol. Dense retrieval misses it; BM25 finds it on the digits.
- **Two corpora, deliberately separate.** The parsed data dictionaries describe the
  *raw files* (`OCCURRED_ON_DATE`); the cleaned tables expose derived columns
  (`occurred_on`). Feeding dictionaries into SQL generation produced queries that would
  not bind, so SQL grounding draws only from the semantic layer.

### Numbers
- **DuckDB over 100% of rows** — 1.67M records, no sampling. `read_csv_auto` needs no
  import step; a group-by over 78k rows returns in 0.44s.
- **Three locks on execution** (`scripts/query.py`): read-only connection,
  `enable_external_access = false`, and a table allowlist. The middle one is a
  *correctness* lock — `SELECT max(TOTAL_VALUE) FROM read_csv_auto('…property…')`
  returns 999,900 instead of 2,448,193,300 because the raw column is VARCHAR and
  `max()` compares strings. It now errors instead.
- Generated SQL is screened for write operations **before** being trimmed to its first
  SELECT, bounded with a LIMIT, and printed alongside the answer.

### Honesty
- **Citations on every grounded claim.** `Answer.__post_init__` raises if a grounded
  answer has no citations, so an uncited answer cannot reach the user.
- **Three distinct non-answers**, scored separately: `blocked` (person-directed
  lookup), `abstained` (the data does not record it), `declined` (a value judgement we
  will not issue).
- **Explicit confidence, rendered into the text** rather than hidden in a field — the
  hardest failure class is a correct-sounding tone with no uncertainty signal.
- **Semantic-layer rules enforced before SQL is generated.** A year-over-year 311
  question abstains in 0 s with the reason ("the extract covers 2026 only") instead of
  returning a confident zero.

### Interfaces
- **Demo UI** (`webapp.py` + `static/index.html`) — Starlette, no CDN, works offline.
  Instant routing trace, hero figure or sorted bar comparison, the SQL, the citations.
- **MCP server** (`mcp_server.py`) — the data catalog (tables, schemas, joins, samples)
  plus `ask` (the whole pipeline), `query` (the locked SQL path), and `semantic_rules`.

### Models — all local, no cloud
| Job | Model | Size |
|---|---|---|
| Writes the SQL | `qwen2.5-coder:7b` | 4.7 GB |
| Writes grounded prose | `granite3.1-dense:8b` | 5.0 GB |
| Embeddings | `bge-m3` | 1.2 GB |

---

## Data traps we handle

Each of these produces a **confidently wrong number rather than an error** — which is
why they are handled inside the views and encoded in the grounding, not left to the
model. Every figure was measured against the committed extracts.

| Trap | Effect if ignored |
|---|---|
| `OFFENSE_CODE_GROUP` and `UCR_PART` are **100% empty** across all 290,130 crime rows | violent-vs-property is uncomputable from the file alone |
| `OFFENSE_DESCRIPTION` includes `SICK ASSIST`, `INVESTIGATE PERSON`, `SERVICE TO OTHER AGENCY` | a plain `count(*)` overstates crime — **49% of "crime" rows are not crimes** |
| `open-space.csv` read with `ignore_errors` parses 272 of 577 rows | parkland undercounted by **60%** (2,327 vs 5,861 acres) |
| Property money columns are VARCHAR with comma separators (`'822,900'`) | any average or median errors, or silently compares strings |
| `lu_desc` values are `RESIDENTIAL CONDO`, never plain `RESIDENTIAL` | a guessed filter matches zero rows and returns null |
| `property` includes 8,545 parking spaces at ~$44k | drags "typical home value" down |
| Food inspections file is one row per **violation**, not per inspection | inspection counts overstated ~4x |
| `neighborhoods` has no `zipcode`, and DuckDB resolves an unknown subquery column against the **outer** query | `WHERE zipcode IN (SELECT zipcode FROM neighborhoods …)` silently matches every row — the all-Boston figure labelled as one neighbourhood |
| 311's own `neighborhood` column contradicts itself, plus a city-wide `Boston` bucket | compound labels and 4,733 unusable rows |
| 49.1% of 311 cases are still open (`closed_dt` null) | average resolution time skews low |
| The BPDA boundaries CSV has an **empty** `shape_wkt` | geometry must come from the GeoJSON |

Geography is resolved by **point-in-polygon** against the 26 BPDA polygons —
290,130 crime rows assigned in 5.6s — not by police district, whose modal purity is as
low as 47.7% for D4/South End.

---

## Evaluation

`eval/` holds the gold question set (20 questions across both personas), a naive
baseline, and the harness. Results are committed to
[`eval-results/eval.md`](eval-results/eval.md).

```bash
PYTHONPATH=. .venv/bin/python eval/run_eval.py                        # both arms
PYTHONPATH=. .venv/bin/python eval/run_eval.py --no-schema-grounding  # control arm
```

Metrics: overall accuracy, counting questions correct, retrieval hit rate @5, correct
abstentions, **fabrications**, citation presence, and SQL accuracy with vs without
schema grounding. The naive baseline scores **0% with 11 fabrications**.

`--no-schema-grounding` is a genuine control: it removes the semantic layer's rules,
worked examples *and* the column notes, rather than leaving half the help in place and
understating the measured difference.

---

## Repo map

```
engine.py                 ask(question, schema_grounding) -> dict  (harness contract)
webapp.py  static/        demo UI (Starlette, no CDN)
mcp_server.py             MCP: data catalog + ask / query / semantic_rules
run_web.sh  run_mcp.sh    launchers (build boston.db if missing)

rag/                      the engine — see rag/README.md
  engine.py                 ask() and plan(); the six staged routes
  router.py                 six-route classifier, rules only
  retrieval.py              BM25 + dense + Reciprocal Rank Fusion
  schema.py                 schema-grounded SQL + execution guardrails
  semantic.py               loads sql/semantic_layer.json as grounding
  db.py                     the locked execution path
  citations.py              the citation contract
  neighborhoods.py          26 canonical names + alias resolution
  temporal.py               time-window extraction, corpus-anchored
  guardrails.py             input screen + output uncertainty
  llm.py                    Ollama client

sql/views.sql             cleaned views; every data trap handled inside
sql/semantic_layer.json   tables, 11 rules, 10 worked question->SQL examples
scripts/build_db.py       materialise the views into boston.db
scripts/query.py          the only sanctioned way to touch data
scripts/parse_docs.py     parse reference PDFs/XLSX -> data/reference/
eval/                     gold set, naive baseline, harness
data/                     nine gzipped source extracts + reference docs
tests/                    179 tests
```

---

## Datasets

All from [Analyze Boston](https://data.boston.gov), committed gzipped under `data/`.

| # | Dataset | Rows | Serves |
|---|---|---|---|
| 1 | 311 Service Requests | 78,526 | responsiveness, complaint themes, geography hub |
| 2 | Crime Incident Reports | 290,130 | safety |
| 3 | Food Establishment Inspections | 896,379 | food safety |
| 4 | Property Assessment (FY2026) | 184,552 | housing cost |
| 5 | Open Space | 577 | parks |
| 6 | BPDA Neighborhood Boundaries | 26 | canonical geography |
| 7 | Licensing Board Licenses | 3,659 | active licences |
| 8 | Entertainment Licenses | 1,246 | active licences |
| 9 | Neighborhood boundaries (GeoJSON) | 26 polygons | point-in-polygon assignment |

Plus reference documents in `data/reference/` — 311 data dictionary and CRM value
codes, RMS offense codes, crime field explanations, property data key and occupancy
codes.

---

## Limitations

Stated plainly, because a system that knows its limits is worth more than one that
pretends otherwise:

- **Neighbourhood-scoped property and parks figures go through ZIP**, and ZIPs straddle
  neighbourhood lines. Those answers carry the caveat and a downgraded confidence.
- **311 covers 2026 (Jan–Aug) only**; crime covers 2023–2026 with 2026 partial. Any
  question needing an earlier 311 baseline is refused, not estimated.
- **The scorecard route returns a single metric**, not all six dimensions — it generates
  one query rather than assembling a profile.
- **Qualitative lookup is thin.** Only reference documents are embedded; the sampled
  free-text tier from the plan was not built, so thematic questions answer weakly.
- **Latency is 15–100s** on routes that need the model. The UI shows routing instantly
  to cover it, but this is not a snappy system.
- Answers reflect the committed extracts and may lag the live portal.

---

## License

Code under [Apache 2.0](LICENSE). Boston open data is published by the City of Boston
via Analyze Boston.
