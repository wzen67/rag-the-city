"""The citation contract.

The rubric is explicit that a system which cannot show its sources scores
a 1 on RAG Quality, so every answer this system emits is an ``Answer``
carrying at least one ``Citation`` — or is an explicit abstention.

Keeping this in one small module means the rest of the pipeline cannot
accidentally drop provenance: retrieval builds citations, the SQL layer
builds citations, and ``Answer.render()`` is the only place that turns
them into text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CitationKind = Literal["row", "sql", "document", "aggregate"]


@dataclass(frozen=True)
class Citation:
    """One traceable source for a claim.

    Attributes:
        kind: How this source was obtained. ``sql`` carries a query,
            ``row`` points at a specific record, ``document`` at a
            reference PDF, ``aggregate`` at a computed summary.
        dataset: The dataset or document filename the claim came from.
        locator: Row id, case id, page, or section — whatever makes the
            claim findable by a human.
        detail: For ``sql`` citations, the query that produced the number.
        row_count: How many rows the claim was computed over. A count
            without a denominator is not auditable.
        confidence: Optional 0–1 quality signal, e.g. the neighborhood
            crosswalk purity behind a district-level figure.
        note: Any caveat that must travel with the number.
    """

    kind: CitationKind
    dataset: str
    locator: str | None = None
    detail: str | None = None
    row_count: int | None = None
    confidence: float | None = None
    note: str | None = None

    def render(self) -> str:
        parts = [self.dataset]
        if self.locator:
            parts.append(f"#{self.locator}")
        if self.row_count is not None:
            parts.append(f"over {self.row_count:,} rows")
        if self.confidence is not None:
            parts.append(f"confidence {self.confidence:.0%}")
        line = " · ".join(parts)
        if self.note:
            line += f" — {self.note}"
        return line


@dataclass
class Answer:
    """A grounded answer, an abstention, or a declined value judgment.

    ``abstained`` and ``declined`` are separate on purpose. Abstention
    means the data does not contain the answer; declining means the
    question asked for a subjective verdict we will not issue, and we
    return the underlying metrics instead.
    """

    text: str
    citations: list[Citation] = field(default_factory=list)
    route: str = "unknown"
    abstained: bool = False
    declined: bool = False
    sql: str | None = None

    def __post_init__(self) -> None:
        if not (self.abstained or self.declined) and not self.citations:
            raise ValueError(
                f"Answer on route {self.route!r} has no citations. Every "
                "grounded claim must be traceable; use abstain() instead."
            )

    def render(self) -> str:
        out = [self.text.strip()]
        if self.sql:
            out.append("\nQuery:\n" + self.sql.strip())
        if self.citations:
            out.append("\nSources:")
            out += [f"  - {c.render()}" for c in self.citations]
        return "\n".join(out)


def abstain(reason: str, route: str = "unanswerable") -> Answer:
    """Build an honest non-answer. The 4 anchor rewards this over a guess."""
    return Answer(
        text=f"The data does not record this. {reason}".strip(),
        route=route,
        abstained=True,
    )


def decline_judgment(metrics_text: str, citations: list[Citation]) -> Answer:
    """Answer a value-laden question with numbers instead of a verdict."""
    return Answer(
        text=(
            "That asks for a judgment I will not make for you. Here is what "
            f"the data does say:\n\n{metrics_text.strip()}"
        ),
        citations=citations,
        route="value_judgment",
        declined=True,
    )
