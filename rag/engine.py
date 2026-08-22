"""``ask()`` — the entry point. Progressive disclosure over Boston's data.

The RAG the City talk's central argument is that naive RAG fails because
of *architecture*, not the model: a single-shot ``embed -> chunk ->
retrieve -> hope`` pipeline leaves every failure mode live at once. Its
prescription is staged narrowing, where each stage eliminates a class of
failure before the next runs.

That is the shape of this module. One question walks through six stages,
and every stage it passes is recorded in ``Answer.trace`` so a demo can
show *how* the system narrowed rather than only what it concluded:

    0. GUARD       refuse person-directed lookups before retrieving
    1. DISAMBIGUATE resolve place names; surface ambiguity, never guess
    2. ANCHOR      pin an explicit time window
    3. ROUTE       pick the engine (rules, no model call)
    4. EXECUTE     SQL for numbers, retrieval for text, abstain for gaps
    5. SIGNAL      attach an explicit confidence level

The division of labour that matters: **numbers come from SQL over every
row; retrieval's job is to make that SQL correct.** No count is ever read
out of an embedding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import guardrails, llm, neighborhoods, retrieval, schema, temporal
from .citations import Answer, Citation, abstain, block, decline_judgment
from .router import Decision, Route, classify

#: Role A's views (sql/views.sql). These names are the integration
#: contract; the mapping exists so a route can pick the *right* view —
#: crime_only excludes non-crime police activity, property_homes excludes
#: commercial parcels.
VIEW_FOR_ROUTE: dict[str, str] = {
    "crime": "crime_only",
    "requests": "svc311",
    "food": "food_violations",
    "property": "property_homes",
    "parks": "open_space",
}

#: Keyword -> subject, used to pick which view a numeric question needs.
_SUBJECT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("crime", ("crime", "violent", "shooting", "offense", "incident", "robbery", "assault", "theft", "safety")),
    ("food", ("restaurant", "inspection", "violation", "food", "kitchen", "hygiene")),
    ("property", ("property", "assessed", "value", "housing", "home price", "parcel", "rent", "tax")),
    ("parks", ("park", "parkland", "green space", "playground", "acres", "open space")),
    ("requests", ("311", "complaint", "pothole", "rodent", "trash", "streetlight", "request", "sla", "on time", "close", "department")),
)


def pick_subject(question: str, decision: Decision) -> str:
    """Choose which dataset a question is about. Falls back to 311."""
    q = question.casefold()
    best, score = "requests", 0
    for subject, words in _SUBJECT_HINTS:
        hits = sum(1 for w in words if w in q)
        if hits > score:
            best, score = subject, hits
    return best


@dataclass
class Engine:
    """Holds the connection and the retrieval index across questions.

    Built once and reused: the categorical cache and the embedded corpus
    are expensive to construct and static thereafter.
    """

    con: object | None = None
    retriever: retrieval.HybridRetriever | None = None
    sql_grounding: retrieval.HybridRetriever | None = None
    _ready: bool = field(default=False, repr=False)

    def prepare(self) -> "Engine":
        """Load views, warm caches, and index both reference corpora.

        Two corpora, deliberately:

        * ``retriever`` — everything, including Role D's parsed data
          dictionaries. Right for definition and lookup questions, which
          ask what a *published field* means.
        * ``sql_grounding`` — only facts written against Role A's cleaned
          views. Feeding the raw-file dictionaries into SQL generation
          actively harms it, because the views rename and derive columns:
          the dictionary says ``OCCURRED_ON_DATE`` while ``crime_only``
          exposes ``occurred_on``, and a model handed the former writes a
          query that will not bind.
        """
        from . import datasets

        if self.con is None:
            self.con = datasets.connect_views()
        if self.retriever is None:
            self.retriever = retrieval.HybridRetriever(
                collection="boston_reference_all", persist_dir=None
            )
            docs = schema.reference_documents() + load_reference_chunks()
            if docs:
                self.retriever.index(docs)
        if self.sql_grounding is None:
            self.sql_grounding = retrieval.HybridRetriever(
                collection="boston_view_grounding", persist_dir=None
            )
            self.sql_grounding.index(schema.view_grounding_documents())
        schema.warm_category_cache(self.con)
        self._ready = True
        return self

    def ask(self, question: str) -> Answer:
        if not self._ready:
            self.prepare()
        return ask(
            question,
            con=self.con,
            retriever=self.retriever,
            sql_grounding=self.sql_grounding,
        )


def load_reference_chunks() -> list[retrieval.Document]:
    """Load Role D's parsed data-dictionary chunks, if present.

    ``data/reference/chunks.jsonl`` carries ``text``, ``source`` and
    ``section`` per chunk. Absent, the hand-written facts in
    ``schema.reference_documents()`` still give the definition route
    something real to work with.
    """
    import json

    from . import datasets

    path = datasets.DATA_DIR / "reference" / "chunks.jsonl"
    if not path.exists():
        return []
    docs: list[retrieval.Document] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        docs.append(
            retrieval.Document(
                id=f"chunk-{i}",
                text=rec.get("text", ""),
                dataset=rec.get("source", "reference"),
                locator=rec.get("section"),
                kind="document",
            )
        )
    return docs


def ask(
    question: str,
    con=None,
    retriever: retrieval.HybridRetriever | None = None,
    sql_grounding: retrieval.HybridRetriever | None = None,
) -> Answer:
    """Answer one question. Never raises; degrades to an abstention.

    This is the function the eval harness and any UI should call.
    """
    trace: list[tuple[str, str]] = []

    # ── Stage 0: input guardrail ────────────────────────────────────
    verdict = guardrails.screen(question)
    if verdict.blocked:
        trace.append(("guard", f"blocked: {verdict.risk.value}"))
        return block(verdict.explain(), trace=trace)
    trace.append(("guard", "no person-directed lookup detected"))

    # ── Stage 1: disambiguate place names ──────────────────────────
    hoods = neighborhoods.find_in_question(question)
    ambiguous = _ambiguity_note(question, hoods)
    trace.append(("disambiguate", ", ".join(hoods) if hoods else "no neighborhood named"))

    # ── Stage 2: temporal anchor ───────────────────────────────────
    window = temporal.extract(question)
    range_warning = temporal.out_of_range(window)
    trace.append(("anchor", window.describe()))

    # ── Stage 3: route ─────────────────────────────────────────────
    decision = classify(question)
    trace.append(("route", f"{decision.route.value} ({', '.join(decision.matched) or 'fallback'})"))

    caveats = tuple(c for c in (ambiguous, range_warning) if c)

    # ── Stage 4: execute ───────────────────────────────────────────
    try:
        answer = _execute(
            question, decision, window, hoods, caveats, con, retriever,
            sql_grounding or retriever, trace,
        )
    except Exception as exc:  # a crash in front of judges is the worst outcome
        trace.append(("execute", f"failed: {type(exc).__name__}"))
        return abstain(
            f"I could not answer that reliably ({type(exc).__name__}).",
            route=decision.route.value,
            trace=trace,
        )

    # ── Stage 5: uncertainty signal ────────────────────────────────
    if answer.uncertainty is None:
        answer.uncertainty = guardrails.assess(caveats=caveats)
    answer.trace = trace
    return answer


def _ambiguity_note(question: str, hoods: list[str]) -> str | None:
    """Surface a compound alias instead of silently choosing one side.

    "Allston / Brighton" is two BPDA neighborhoods. Picking one is the
    entity-ambiguity failure mode; saying so is the fix.
    """
    for alias in ("allston / brighton", "allston/brighton", "south boston / south boston waterfront"):
        if alias in question.casefold():
            resolved = neighborhoods.resolve(alias)
            if len(resolved) > 1:
                return f"{alias!r} covers {len(resolved)} neighborhoods ({', '.join(resolved)}); reporting both"
    return None


def _execute(
    question: str,
    decision: Decision,
    window: temporal.Window,
    hoods: list[str],
    caveats: tuple[str, ...],
    con,
    retriever,
    sql_grounding,
    trace: list[tuple[str, str]],
) -> Answer:
    route = decision.route

    if route is Route.UNANSWERABLE:
        trace.append(("execute", "known-absent information; abstaining without a model call"))
        return abstain(
            "These datasets record codes, dispositions and counts, not causes "
            "or opinions.",
            route=route.value,
        )

    if route is Route.DEFINITION:
        return _answer_definition(question, retriever, trace)

    if route is Route.VALUE_JUDGMENT:
        return _answer_value_judgment(question, decision, window, hoods, con, retriever, trace)

    # SCORECARD and AGGREGATE both resolve to numbers from SQL.
    if route in (Route.AGGREGATE, Route.SCORECARD):
        return _answer_with_sql(question, decision, window, con, sql_grounding, trace, caveats)

    return _answer_lookup(question, retriever, trace)


def _answer_definition(question: str, retriever, trace) -> Answer:
    """Schema questions are answered from the reference documents only."""
    if retriever is None or len(retriever) == 0:
        return abstain("No reference documents are indexed.", route="definition")
    hits = retriever.search(question, k=4)
    if not hits:
        return abstain("No data dictionary entry matches that.", route="definition")
    trace.append(("execute", f"retrieved {len(hits)} definition chunks ({hits[0].found_by})"))
    context = "\n\n".join(f"[{h.doc.dataset}#{h.doc.locator}] {h.doc.text}" for h in hits)
    text = llm.generate(
        "Answer the question using ONLY the field definitions below. Quote the "
        "definition. If they do not cover it, say so.\n\n"
        f"{context}\n\nQuestion: {question}\nAnswer:"
    )
    return Answer(
        text=text,
        citations=[h.to_citation() for h in hits],
        route="definition",
        uncertainty=guardrails.assess(retrieval_scores=[h.score for h in hits]),
    )


def _answer_value_judgment(question, decision, window, hoods, con, retriever, trace) -> Answer:
    """Return metrics and explicitly refuse the verdict."""
    trace.append(("execute", "value judgment: returning metrics, refusing to rank"))
    metrics: list[str] = []
    citations: list[Citation] = []

    scope = hoods[0] if hoods else None
    for subject, label in (("crime", "crime incidents"), ("requests", "311 requests")):
        view = VIEW_FOR_ROUTE[subject]
        where = []
        if scope:
            where.append(f"neighborhood = '{scope}'")
        if f := window.sql_filter("YEAR" if subject == "crime" else "year(open_dt)"):
            where.append(f)
        sql = f"SELECT count(*) FROM {view}" + (" WHERE " + " AND ".join(where) if where else "")
        try:
            n = con.sql(sql).fetchone()[0]
        except Exception:
            continue
        metrics.append(f"- {label}: {n:,}" + (f" in {scope}" if scope else "") + f" ({window.describe()})")
        citations.append(
            Citation(kind="sql", dataset=view, detail=sql, row_count=n,
                     note="crime_only excludes non-crime police activity" if subject == "crime" else None)
        )

    if not metrics:
        return abstain("I could not compute the underlying metrics.", route="value_judgment")
    return decline_judgment("\n".join(metrics), citations)


def _answer_with_sql(question, decision, window, con, retriever, trace, caveats) -> Answer:
    """Numbers come from SQL, grounded by retrieved field definitions."""
    subject = pick_subject(question, decision)
    view = VIEW_FOR_ROUTE[subject]
    grounding = retriever.search(question, k=3) if retriever else None
    if grounding:
        trace.append(("ground", f"injected {len(grounding)} field definitions into the SQL prompt"))

    plan = schema.generate_sql(question, view, grounding, con=con, window=window)
    cols, rows = schema.execute(plan, con)
    trace.append(("execute", f"{view}: {len(rows)} row(s)"))

    if not rows or (len(rows) == 1 and all(v is None for v in rows[0])):
        return abstain(
            f"The query over {view} returned no matching rows.",
            route=decision.route.value,
            sql=plan.sql,
        )

    scalar = _is_scalar_aggregate(plan.sql, cols, rows)
    # The model can ignore the view we asked for and query another one —
    # grounding that mentions open_space can pull a 311 question over to
    # it. Cite what the SQL actually touched, never what we intended.
    actual = _views_in(plan.sql) or (view,)
    table = _format_rows(cols, rows)
    used = ", ".join(actual)
    text = (
        f"{table}\n\nComputed over {used} for {window.describe()}."
        if window.is_bounded
        else f"{table}\n\nComputed over {used}."
    )
    if view not in actual:
        trace.append(("verify", f"asked for {view}, query used {used}; citing actual"))
    return Answer(
        text=text,
        citations=[
            Citation(
                kind="sql",
                dataset=used,
                detail=plan.sql,
                # For a scalar aggregate the result is one figure over a
                # full-view scan, so a "1 row" label would understate it.
                row_count=None if scalar else len(rows),
                note="; ".join(caveats) or None,
            )
        ]
        + [g.to_citation() for g in (grounding or [])],
        route=decision.route.value,
        sql=plan.sql,
        uncertainty=guardrails.assess(
            row_count=len(rows),
            is_scalar_aggregate=scalar,
            caveats=caveats,
        ),
    )


def _answer_lookup(question, retriever, trace) -> Answer:
    """Qualitative questions: hybrid retrieval over free text."""
    if retriever is None or len(retriever) == 0:
        return abstain("Nothing is indexed to search.", route="lookup")
    hits = retriever.search(question, k=5)
    if not hits:
        return abstain("Retrieval found nothing relevant.", route="lookup")
    trace.append(("execute", f"hybrid retrieval: {len(hits)} hits, top found by {hits[0].found_by}"))
    context = "\n\n".join(f"[{h.doc.dataset}] {h.doc.text}" for h in hits)
    text = llm.generate(
        "Answer using ONLY the context. Cite the dataset for each claim. If the "
        "context does not answer it, say so plainly.\n\n"
        f"{context}\n\nQuestion: {question}\nAnswer:"
    )
    return Answer(
        text=text,
        citations=[h.to_citation() for h in hits],
        route="lookup",
        uncertainty=guardrails.assess(retrieval_scores=[h.score for h in hits]),
    )


_AGG_FN = re.compile(r"\b(count|sum|avg|median|min|max|percentile_cont)\s*\(", re.I)


def _views_in(sql: str) -> tuple[str, ...]:
    """Which known views a query actually references."""
    from . import datasets

    low = sql.casefold()
    return tuple(
        v for v in datasets.VIEWS
        if re.search(rf"\b(?:from|join)\s+{v}\b", low)
    )


def _is_scalar_aggregate(sql: str, cols: list[str], rows: list[tuple]) -> bool:
    """True when the result is one computed figure, not a listing.

    A single row from COUNT/AVG with no GROUP BY is the expected shape —
    the scan behind it covered the whole view — so it must not be scored
    as thin evidence.
    """
    return (
        len(rows) == 1
        and bool(_AGG_FN.search(sql))
        and not re.search(r"\bgroup\s+by\b", sql, re.I)
    )


def _format_rows(cols: list[str], rows: list[tuple], limit: int = 12) -> str:
    head = " | ".join(cols)
    body = [" | ".join("-" if v is None else f"{v:,}" if isinstance(v, int) else str(v) for v in r)
            for r in rows[:limit]]
    more = f"\n... {len(rows) - limit} more rows" if len(rows) > limit else ""
    return head + "\n" + "\n".join(body) + more
