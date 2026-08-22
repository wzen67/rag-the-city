"""Schema-grounded SQL generation.

This is the module that makes retrieval load-bearing for *correctness*
rather than decoration. A model asked to write SQL over Boston's data
will guess what ``on_time`` means, assume ``UCR_PART`` is populated when
it is 100% empty, and average commercial towers into residential housing
costs. None of that is fixable by a better prompt alone — the model needs
the field semantics in front of it.

So before generating SQL we retrieve the relevant field definitions from
the reference documents and inject them. The effect is measurable: run
the eval with and without the retrieved context and compare SQL accuracy.
That A/B is the number the Track A anchor asks for.

Guardrails, because generated SQL is executed:
  * one statement only, and it must be a SELECT
  * no DDL, DML, PRAGMA, ATTACH, or COPY
  * a LIMIT is appended if the model omitted one
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import datasets, llm
from .retrieval import Document, Scored

#: Statements that must never come out of a generated query.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|"
    r"install|load|export|import|set)\b",
    re.IGNORECASE,
)

DEFAULT_LIMIT = 200


class UnsafeSQL(ValueError):
    """Generated SQL failed validation and was not executed."""


@dataclass(frozen=True)
class SQLPlan:
    """A generated query plus the grounding that produced it."""

    sql: str
    dataset_key: str
    grounding: tuple[str, ...] = ()
    row_count: int | None = None

    @property
    def was_grounded(self) -> bool:
        return bool(self.grounding)


MAX_CATEGORY_VALUES = 18

# A DISTINCT scan over the 896k-row inspections file is far too slow to
# repeat on every prompt build. The files are static, so cache forever.
_CATEGORY_CACHE: dict[tuple[str, str], list[str]] = {}


def category_values(dataset_key: str, column: str, con=None) -> list[str]:
    """Actual values of a low-cardinality column, or [] if too many.

    This closes a whole class of silent failure: a model that writes
    ``lu_desc = 'RESIDENTIAL'`` gets zero rows and a null answer, because
    the real values are 'RESIDENTIAL CONDO', 'SINGLE FAM DWELLING', and
    so on. Showing the model the vocabulary is cheaper than teaching it
    to guess.

    Cached per (dataset, column) for the process lifetime.
    """
    ck = (dataset_key, column)
    if ck in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[ck]

    own = con is None
    con = con or datasets.connect([dataset_key])
    try:
        rows = con.sql(
            f'SELECT "{column}" FROM {dataset_key} '
            f'WHERE "{column}" IS NOT NULL '
            f'GROUP BY 1 LIMIT {MAX_CATEGORY_VALUES + 1}'
        ).fetchall()
        out = [] if len(rows) > MAX_CATEGORY_VALUES else sorted(str(r[0]) for r in rows)
    except Exception:
        out = []  # column absent or unreadable; not worth failing a prompt over
    finally:
        if own:
            con.close()

    _CATEGORY_CACHE[ck] = out
    return out


def warm_category_cache(con=None) -> int:
    """Pre-compute every declared categorical. Call once at startup.

    Keeps the first user question from paying the scan cost mid-demo.
    """
    own = con is None
    con = con or datasets.connect()
    try:
        n = 0
        for key, ds in datasets.REGISTRY.items():
            for col in ds.categoricals:
                category_values(key, col, con)
                n += 1
        return n
    finally:
        if own:
            con.close()


def column_summary(
    dataset_key: str,
    con=None,
    include_notes: bool = True,
    include_values: bool = True,
) -> str:
    """Describe a dataset's columns for a prompt.

    Types come from DuckDB's own inference rather than a hand-maintained
    list, so the description cannot drift from the file.

    Args:
        include_notes: Emit the registry's CAUTION lines. Set False to
            build the *control* arm of the grounding A/B — leaving them
            in gives the ungrounded arm half the help and understates
            the measured benefit.
        include_values: Enumerate declared low-cardinality columns.
    """
    ds = datasets.get(dataset_key)
    own = con is None
    con = con or datasets.connect([dataset_key])
    try:
        rel = con.sql(f"SELECT * FROM {dataset_key} LIMIT 0")
        lines = [f"{name} {dtype}" for name, dtype in zip(rel.columns, rel.types)]
        values = (
            {c: category_values(dataset_key, c, con) for c in ds.categoricals}
            if include_values
            else {}
        )
    finally:
        if own:
            con.close()

    out = [f"-- view: {dataset_key}  (one row = {ds.grain})"]
    if include_notes and ds.note:
        out += [f"-- CAUTION: {w}" for w in _wrap(ds.note, 96)]
    out.append(f"-- geography columns: {ds.geography}")
    out += lines
    for col, vals in values.items():
        if vals:
            out.append(f'-- distinct values of "{col}": ' + ", ".join(repr(v) for v in vals))
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def build_prompt(
    question: str,
    dataset_key: str,
    grounding: list[Scored] | None = None,
    include_notes: bool = True,
) -> str:
    """Assemble a SQL-generation prompt.

    Args:
        question: The user's question, verbatim.
        dataset_key: Which registered dataset to query. One table per
            prompt on purpose — pasting five schemas at once degrades
            accuracy and wastes context.
        grounding: Retrieved reference-document chunks describing the
            relevant fields. Omitting these is the "without" arm of the
            A/B measurement.
    """
    parts = [
        "You write DuckDB SQL. Output ONLY the query, no prose, no markdown fence.",
        f"Query the view named `{dataset_key}` directly, e.g. FROM {dataset_key}.",
        "",
        column_summary(dataset_key, include_notes=include_notes),
    ]
    if grounding:
        parts += ["", "-- Field definitions retrieved from the official data dictionaries:"]
        for g in grounding:
            src = f"{g.doc.dataset}" + (f"#{g.doc.locator}" if g.doc.locator else "")
            parts += [f"-- [{src}]"] + [f"--   {w}" for w in _wrap(g.doc.text, 96)]
    parts += [
        "",
        "Rules:",
        "- A single SELECT statement. Never modify data.",
        "- Prefer explicit filters over assuming a column is populated.",
        "- If a column is documented as empty, do not use it.",
        "",
        f"Question: {question}",
        "SQL:",
    ]
    return "\n".join(parts)


def sanitize(sql: str) -> str:
    """Strip fences and prose, validate, and bound the result set.

    Raises:
        UnsafeSQL: if the text is not a single read-only SELECT.
    """
    text = sql.strip()
    # Models fence their output even when told not to.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()

    # Screen for write operations BEFORE trimming to the first SELECT.
    # Order matters: trimming first would turn "CREATE TABLE t AS SELECT 1"
    # into a clean-looking "SELECT 1" and wave it straight through.
    if bad := _FORBIDDEN.search(text):
        raise UnsafeSQL(f"forbidden keyword {bad.group(0)!r}")

    # Keep only from the first SELECT/WITH onward.
    m = re.search(r"\b(with|select)\b", text, re.IGNORECASE)
    if not m:
        raise UnsafeSQL(f"no SELECT found in: {text[:120]!r}")
    text = text[m.start() :].strip().rstrip(";").strip()

    if ";" in text:
        raise UnsafeSQL("multiple statements are not allowed")

    if not re.search(r"\blimit\s+\d+", text, re.IGNORECASE):
        text = f"{text}\nLIMIT {DEFAULT_LIMIT}"
    return text


def generate_sql(
    question: str,
    dataset_key: str,
    grounding: list[Scored] | None = None,
    model: str = llm.SQL_MODEL,
    include_notes: bool = True,
) -> SQLPlan:
    """Generate and validate SQL. Does not execute it."""
    prompt = build_prompt(question, dataset_key, grounding, include_notes)
    raw = llm.generate(prompt, model=model)
    return SQLPlan(
        sql=sanitize(raw),
        dataset_key=dataset_key,
        grounding=tuple(
            f"{g.doc.dataset}#{g.doc.locator or ''}" for g in (grounding or [])
        ),
    )


def execute(plan: SQLPlan, con=None) -> tuple[list[str], list[tuple]]:
    """Run a validated plan against the registered views.

    Returns (column names, rows).
    """
    own = con is None
    con = con or datasets.connect()
    try:
        rel = con.sql(plan.sql)
        return list(rel.columns), rel.fetchall()
    finally:
        if own:
            con.close()


def reference_documents() -> list[Document]:
    """Field definitions available for grounding.

    Role D parses the official data dictionaries into
    ``data/reference/*.csv``; until those land, these hand-written entries
    cover the fields our own profiling proved are traps, so the grounding
    path is testable today and simply gets richer later.
    """
    facts = [
        (
            "crime",
            "UCR_PART",
            "UCR_PART and OFFENSE_CODE_GROUP are empty in every row of the "
            "published extract (verified across all 290,130 rows, 2023-2026). "
            "Do not filter or group on them. Violent-vs-property classification "
            "requires joining the external offense-code lookup.",
        ),
        (
            "crime",
            "OFFENSE_DESCRIPTION",
            "Free-text description of the incident. Includes non-crime police "
            "activity such as SICK ASSIST, INVESTIGATE PERSON, INVESTIGATE "
            "PROPERTY and SERVICE TO OTHER AGENCY. A plain count(*) therefore "
            "overstates crime; exclude non-crime dispositions explicitly.",
        ),
        (
            "service_requests",
            "on_time",
            "Whether the case closed on or before sla_target_dt. Values are "
            "ONTIME and OVERDUE. It is a compliance flag, not a duration: "
            "elapsed time must be computed from open_dt and closed_dt.",
        ),
        (
            "service_requests",
            "neighborhood",
            "Non-canonical. Ships compound labels ('Allston / Brighton'), a "
            "redundant prefix ('Greater Mattapan'), and a city-wide catch-all "
            "'Boston' (4,733 rows) that is not a neighborhood. Prefer a spatial "
            "join on latitude/longitude against the BPDA polygons.",
        ),
        (
            "service_requests",
            "case_status",
            "Open or Closed. Open cases have a null closed_dt, so any average "
            "resolution time must exclude them or it silently skews low.",
        ),
        (
            "property",
            "lu_desc",
            "Land-use description. Separates residential from commercial and "
            "tax-exempt parcels. Averaging total_value without filtering on it "
            "mixes office towers into residential housing costs.",
        ),
        (
            "property",
            "total_value",
            "Assessed total value. Stored as VARCHAR with comma thousands "
            "separators, e.g. '822,900' — as are land_value, bldg_value and "
            "sfyi_value. Any average, median or comparison must cast first: "
            "TRY_CAST(replace(total_value, ',', '') AS DOUBLE). Casting the raw "
            "column fails outright.",
        ),
        (
            "property",
            "gross_tax",
            "The published header is ' GROSS_TAX ' with literal surrounding "
            "spaces; read with normalize_names=true, which also lowercases "
            "every column in this file.",
        ),
        (
            "open_space",
            "ACRES",
            "Site area. The file must be read with strict_mode=false; reading "
            "it with ignore_errors=true yields 272 of 577 rows and undercounts "
            "total parkland by 60 percent.",
        ),
        (
            "food",
            "violdesc",
            "Coded violation description. The dataset records codes and "
            "dispositions only, never a narrative cause, so questions asking "
            "why an establishment failed are not answerable from it.",
        ),
    ]
    return [
        Document(
            id=f"def-{ds}-{field}".lower(),
            text=f"{field} ({ds}): {body}",
            dataset=f"{ds}-field-definitions",
            locator=field,
            kind="document",
        )
        for ds, field, body in facts
    ]
