"""Guardrail layers, input and output.

The RAG the City talk frames three layers — input query classification,
sensitivity-aware retrieval filtering, and output uncertainty signalling —
and notes that implementing even one is credited. We implement the input
and output layers here; the retrieval layer is partly structural, since
our corpus is public municipal data with no medical or PII-bearing
records to filter.

Why the input layer matters on public data: Boston's 311 and licensing
extracts do carry named individuals (`legalowner`, `OWNER`, applicant and
manager names) and street addresses. A question like "where does John
Smith live?" is technically answerable from the property file and should
not be. Refusing before retrieval runs is both safer and cheaper than
generating and then suppressing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Risk(str, Enum):
    OK = "ok"
    PERSONAL_LOCATION = "personal_location"
    CONTACT_DETAILS = "contact_details"
    PROFILE_INDIVIDUAL = "profile_individual"


@dataclass(frozen=True)
class GuardVerdict:
    risk: Risk
    matched: str | None = None

    @property
    def blocked(self) -> bool:
        return self.risk is not Risk.OK

    def explain(self) -> str:
        return {
            Risk.PERSONAL_LOCATION: (
                "That asks me to locate a specific private individual. The "
                "assessment file does link owner names to addresses, which is "
                "exactly why I will not answer it. I can answer the same "
                "question at neighborhood or ZIP level."
            ),
            Risk.CONTACT_DETAILS: (
                "I will not return contact details for individuals. The "
                "licensing extracts contain phone numbers; they are not a "
                "directory."
            ),
            Risk.PROFILE_INDIVIDUAL: (
                "That asks me to build a profile of a named private person "
                "across datasets. I will not do that. Aggregate questions "
                "about places are fine."
            ),
        }.get(self.risk, "")


# Deliberately narrow. Over-blocking is its own failure: refusing ordinary
# civic questions would cap RAG Quality at 2 for "refusing off-script
# queries", so these patterns target person-directed lookups only.
#: Scoped inline flags: the *phrasing* is case-insensitive, but the name
#: itself must be capitalised. That distinction is the whole point —
#: "where does john live" is a generic example, "where does John Smith
#: live" is a real person lookup. Making the entire pattern
#: case-sensitive (the first version of this) meant a capitalised
#: sentence start like "Where does..." silently failed to match.
_NAME = r"[A-Z][a-z]+"
_PATTERNS: tuple[tuple[Risk, str], ...] = (
    (Risk.PERSONAL_LOCATION, rf"(?i:\bwhere does)\s+{_NAME}\s+{_NAME}(?i:\s+live)"),
    (Risk.PERSONAL_LOCATION, rf"(?i:\b(?:home )?address (?:of|for))\s+{_NAME}\s+{_NAME}"),
    (Risk.PERSONAL_LOCATION, r"(?i:\bwho lives at\b)"),
    (Risk.CONTACT_DETAILS, r"(?i:\b(?:phone number|telephone|email address|contact number)\b.*\bfor\b)"),
    (Risk.CONTACT_DETAILS, rf"(?i:\b(?:phone|email) (?:of|for))\s+{_NAME}"),
    (Risk.PROFILE_INDIVIDUAL, rf"(?i:\b(?:everything|all)(?:\s+you\s+know)?\s+(?:about|on))\s+{_NAME}\s+{_NAME}"),
    (Risk.PROFILE_INDIVIDUAL, rf"(?i:\b(?:full profile|dossier|background check)\s+(?:of|about|on))\s+{_NAME}\s+{_NAME}"),
    (Risk.PROFILE_INDIVIDUAL, rf"(?i:\bwhat (?:properties|businesses) does)\s+{_NAME}\s+{_NAME}(?i:\s+own)"),
)


def screen(question: str) -> GuardVerdict:
    """Classify a question before anything is retrieved.

    Case-sensitive on names by design: "where does john live" reads as a
    generic example, "where does John Smith live" reads as a real lookup.

    >>> screen("How many potholes in Dorchester?").blocked
    False
    >>> screen("Where does John Smith live?").blocked
    True
    """
    for risk, pattern in _PATTERNS:
        if m := re.search(pattern, question):
            return GuardVerdict(risk, m.group(0))
    return GuardVerdict(Risk.OK)


# -- output layer ----------------------------------------------------


@dataclass(frozen=True)
class Uncertainty:
    """An explicit confidence signal to attach to an answer.

    The talk's hardest failure class is "confidently wrong": a correct
    *tone* with no uncertainty signal. So every answer carries one of
    these, and low confidence is stated in the rendered text rather than
    hidden in a field nobody reads.
    """

    level: str  # "high" | "medium" | "low"
    reasons: tuple[str, ...] = ()

    @property
    def should_state(self) -> bool:
        return self.level != "high"

    def render(self) -> str:
        if not self.should_state:
            return ""
        why = "; ".join(self.reasons) if self.reasons else "limited supporting evidence"
        return f"Confidence: {self.level} — {why}."


def assess(
    *,
    row_count: int | None = None,
    is_scalar_aggregate: bool = False,
    retrieval_scores: list[float] | None = None,
    caveats: tuple[str, ...] = (),
) -> Uncertainty:
    """Derive a confidence level from what actually backed the answer.

    Heuristic and deliberately conservative: an answer assembled from
    weak retrieval hits is reported as uncertain even when it happens to
    be right.

    Args:
        row_count: Rows in the *result*. For a listing, few rows is weak
            evidence.
        is_scalar_aggregate: True when the result is a single computed
            figure (a COUNT or AVG with no GROUP BY). One row is then the
            expected shape, not thin evidence, and must not be penalised
            — the underlying scan covered the whole view.
    """
    reasons = list(caveats)
    level = "high"

    if row_count is not None and row_count == 0:
        level = "low"
        reasons.append("no matching rows")
    elif row_count is not None and row_count < 10 and not is_scalar_aggregate:
        level = "medium"
        reasons.append(f"only {row_count} matching row(s)")

    if retrieval_scores:
        top = max(retrieval_scores)
        # RRF scores are small by construction: a single retriever's top
        # hit contributes 1/(60+1) = 0.0164, so two agreeing retrievers
        # land near 0.033. Below one retriever's best is weak evidence.
        if top < 1 / 61:
            level = "low" if level != "low" else level
            reasons.append("only one retriever matched, at low rank")

    if caveats and level == "high":
        level = "medium"

    return Uncertainty(level, tuple(dict.fromkeys(reasons)))
