"""Load the semantic layer as SQL-grounding documents.

``sql/semantic_layer.json`` is the authoritative description of the
cleaned tables in ``boston.db``: real column names and types, a plain
statement of what one row means, eleven rules about which table to use
and what not to compare, and ten worked question -> SQL examples.

That replaces the hand-written facts this module used to carry. Two
reasons it is better:

* it is generated from the views, so it cannot drift from them, and
* the rules encode decisions no model would infer — that ``crime``
  overstates crime by ~97% versus ``crime_only``, that ``property``
  includes 8,545 parking spaces at ~$44,000 each, that 311 covers 2026
  only so year-over-year comparisons are unanswerable.

The worked examples are used as few-shot pairs, which is what actually
moves text-to-SQL accuracy on a schema this irregular.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .retrieval import Document

SEMANTIC_LAYER = Path(__file__).resolve().parents[1] / "sql" / "semantic_layer.json"


@dataclass(frozen=True)
class Table:
    name: str
    rows: int
    meaning: str
    columns: tuple[tuple[str, str], ...]

    def column_block(self) -> str:
        return "\n".join(f"  {n} {t}" for n, t in self.columns)


@dataclass(frozen=True)
class SemanticLayer:
    tables: tuple[Table, ...]
    rules: tuple[str, ...]
    examples: tuple[tuple[str, str], ...]

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tables)


@lru_cache(maxsize=1)
def load(path: Path | None = None) -> SemanticLayer:
    """Parse the semantic layer. Cached — the file is static."""
    p = path or SEMANTIC_LAYER
    if not p.exists():
        return SemanticLayer((), (), ())
    raw = json.loads(p.read_text(encoding="utf-8"))
    tables = tuple(
        Table(
            name=t["name"],
            rows=t.get("rows", 0),
            meaning=t.get("meaning", ""),
            columns=tuple((c["name"], c["type"]) for c in t.get("columns", [])),
        )
        for t in raw.get("tables", [])
    )
    return SemanticLayer(
        tables=tables,
        rules=tuple(raw.get("rules", [])),
        examples=tuple((e["question"], e["sql"]) for e in raw.get("examples", [])),
    )


#: Patterns the generated layer does not cover, each written because a
#: model got it wrong against the real database.
_EXTRA_FACTS: tuple[tuple[str, str], ...] = (
    (
        "property-by-neighborhood",
        "property_homes and property have NO neighborhood column, and the "
        "neighborhoods table has ONLY neighborhood and geom — it has no "
        "zipcode. Never write `SELECT zipcode FROM neighborhoods`: DuckDB "
        "resolves the unknown column against the outer query instead of "
        "erroring, so the filter silently matches every row and you get the "
        "all-Boston figure labelled as one neighborhood. To scope property to "
        "a neighborhood, take the ZIPs from svc311, which has both: "
        "SELECT median(total_value) FROM property_homes WHERE zipcode IN "
        "(SELECT DISTINCT zipcode FROM svc311 WHERE neighborhood = 'X' AND "
        "zipcode IS NOT NULL). This is approximate because ZIPs straddle "
        "neighborhood lines.",
    ),
    (
        "open-space-by-neighborhood",
        "open_space has no neighborhood and no coordinates. Its "
        "source_district column holds neighborhood-style names but "
        "hyphenated differently from the BPDA list (e.g. 'Allston-Brighton'). "
        "For a citywide total use SUM(acres) with no WHERE clause; all 577 "
        "rows are already parkland.",
    ),
    (
        "no-invented-columns",
        "Only use columns listed for the table you are querying. If the "
        "question needs a column that does not exist, return a query over "
        "what does exist rather than inventing a join key.",
    ),
)


def grounding_documents() -> list[Document]:
    """One retrievable document per table, per rule, and per example.

    Splitting them means retrieval can surface the *relevant* rule for a
    question rather than pasting all eleven into every prompt.
    """
    layer = load()
    docs: list[Document] = [
        Document(
            id=f"sem-extra-{name}",
            text=f"Rule: {body}",
            dataset="semantic_layer (supplement)",
            locator=name,
            kind="document",
        )
        for name, body in _EXTRA_FACTS
    ]

    for t in layer.tables:
        cols = ", ".join(n for n, _ in t.columns)
        docs.append(
            Document(
                id=f"sem-table-{t.name}",
                text=f"Table {t.name} ({t.rows:,} rows). {t.meaning} Columns: {cols}.",
                dataset="semantic_layer",
                locator=t.name,
                kind="document",
            )
        )

    for i, rule in enumerate(layer.rules):
        docs.append(
            Document(
                id=f"sem-rule-{i}",
                text=f"Rule: {rule}",
                dataset="semantic_layer",
                locator=f"rule-{i + 1}",
                kind="document",
            )
        )

    for i, (question, sql) in enumerate(layer.examples):
        docs.append(
            Document(
                id=f"sem-example-{i}",
                text=f"Example question: {question}\nCorrect SQL: {sql}",
                dataset="semantic_layer",
                locator=f"example-{i + 1}",
                kind="document",
            )
        )

    return docs


def prompt_block(table: str, examples: int = 2) -> str:
    """The schema section of a SQL prompt, built from the semantic layer.

    Always includes every rule. The rules are short, there are only
    eleven, and each one exists because getting it wrong produces a
    confidently wrong number rather than an error — that is not something
    to leave to retrieval luck.
    """
    layer = load()
    out: list[str] = []

    t = layer.table(table)
    if t:
        out += [f"-- table: {t.name}  ({t.rows:,} rows)", f"-- {t.meaning}", t.column_block()]
    if layer.rules:
        out.append("\n-- Rules that must be obeyed:")
        out += [f"--  {i + 1}. {r}" for i, r in enumerate(layer.rules)]
        for name, body in _EXTRA_FACTS:
            out.append(f"--  * {body}")
    if layer.examples:
        out.append("\n-- Worked examples:")
        for question, sql in layer.examples[:examples]:
            out += [f"-- Q: {question}", f"--    {sql}"]
    return "\n".join(out)


def unanswerable_by_rule(question: str) -> str | None:
    """Catch questions the rules declare unanswerable, before generating SQL.

    Rules 4 and 10 are absolute: 311 holds 2026 only, so a
    year-over-year 311 comparison has no data behind it, and the licence
    tables hold active records only. Answering either with a number is
    the confidently-wrong failure; saying so costs nothing.
    """
    q = question.casefold()
    wants_311 = any(w in q for w in ("311", "service request", "complaint", "pothole", "rodent"))
    compares_years = any(
        w in q for w in ("year over year", "year-over-year", "compared to last year",
                         "vs last year", "previous year", "last year", "since 2023",
                         "since 2024", "since 2025", "over the last 3 years",
                         "over the past 3 years", "trend over", "trend in",
                         "per year", "each year", "by year", "yearly",
                         "year on year", "annually", "over the years")
    )
    if wants_311 and compares_years:
        return (
            "the 311 extract covers 2026 only (January to August), so there is no "
            "earlier year in it to compare against"
        )

    if any(w in q for w in ("revoked", "expired", "suspended licence", "suspended license")) and (
        "licen" in q
    ):
        return "the licence tables contain only currently active records"

    return None
