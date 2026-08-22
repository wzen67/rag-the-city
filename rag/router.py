"""The router: pick an engine per question.

This is the "agentic retrieval" half of the Track A architecture. Sending
an aggregation question to a vector store is the single most common
failure in city-data RAG — embeddings cannot count — so the first thing
the system does is decide *what kind of question* it was handed.

Rules run before any model call, for three reasons: they are instant,
they are free, and they are explainable on stage. ``classify`` returns the
rule verdict plus the evidence that produced it, and only falls back to an
LLM when the rules find nothing (see ``needs_llm_fallback``).

Route semantics
---------------
``AGGREGATE``       a number over many rows -> SQL. Never retrieval.
``SCORECARD``       a named neighborhood's profile, or a comparison.
``DEFINITION``      what a column or code *means* -> reference documents.
``LOOKUP``          qualitative or thematic -> hybrid retrieval.
``VALUE_JUDGMENT``  asks for a verdict ("is it safe?") -> metrics + refusal.
``UNANSWERABLE``    asks for something the data provably does not record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from . import neighborhoods


class Route(str, Enum):
    AGGREGATE = "aggregate"
    SCORECARD = "scorecard"
    DEFINITION = "definition"
    LOOKUP = "lookup"
    VALUE_JUDGMENT = "value_judgment"
    UNANSWERABLE = "unanswerable"


@dataclass(frozen=True)
class Decision:
    """A routing verdict plus why it was reached.

    ``matched`` exists so the demo can show the judge *why* a question
    went down a given path — the 4 anchor rewards explaining WHY.
    """

    route: Route
    neighborhoods: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    confident: bool = True

    @property
    def is_comparison(self) -> bool:
        return len(self.neighborhoods) > 1


# Order matters: the first family to match wins. Value judgments and
# unanswerables are checked first because they override everything —
# "is Roxbury safe?" must never be answered as a plain aggregate.
_VALUE_JUDGMENT = (
    r"\bis it (safe|good|bad|nice|better|worse)\b",
    r"\b(is|are)\s+\w[\w\s]{0,30}?\s+(safe|dangerous|sketchy|nice|good|bad)\b",
    r"\bbest\s+(neighborhood|neighbourhood|place|area)\b",
    r"\bworst\s+(neighborhood|neighbourhood|place|area)\b",
    r"\bshould i (live|move|buy|rent)\b",
    r"\bwhere should i (live|move)\b",
    r"\b(rank|ranking)\s+(the\s+)?(neighborhood|neighbourhood|area)s?\b",
    r"\bwould you recommend\b",
)

# Things the datasets provably do not contain. Being able to say so
# without burning an LLM call is the cheapest possible abstention.
_UNANSWERABLE = (
    r"\bwhy (was|were|did|is|are)\b.*\b(suspend|revok|fail|clos|deni)",
    r"\bwhat caused\b",
    r"\broot cause\b",
    r"\bwho (is|was) (to blame|responsible)\b",
    r"\bhow (do|does) (residents?|people) feel\b",
    r"\b(phone number|email|contact details)\b",
)

_DEFINITION = (
    r"\bwhat does\b.*\bmean\b",
    r"\bwhat is (the )?(meaning|definition) of\b",
    r"\bhow is\b.*\b(calculated|computed|defined|measured|determined)\b",
    r"\bwhat(?:'s| is) the difference between\b",
    r"\bwhat (does|do)\b.*\b(code|column|field|flag|status)s?\b.*\b(mean|indicate)\b",
    r"\bdefine\b",
    r"\bdata dictionary\b",
)

_AGGREGATE = (
    r"\bhow many\b",
    r"\bhow much\b",
    r"\bhow long\b",
    r"\bcount of\b",
    r"\b(total|sum|average|mean|median|percentage|percent|rate|share)\b",
    r"\b(most|least|top|fewest|highest|lowest|busiest|slowest|fastest)\b",
    r"\b(trend|trending|over time|year over year|per year|per month|since \d{4})\b",
    r"\b(more|fewer|less) \w+ than\b",
    r"\bcompared? (to|with)\b",
    r"\bbreakdown\b",
)

_SCORECARD = (
    r"\b(how (is|are|does|do)|what(?:'s| is) (it )?like)\b",
    r"\b(profile|scorecard|overview|summary|snapshot|report card)\b",
    r"\btell me about\b",
    r"\b(look|looks) like\b",
    r"\bcompare\b",
)


def _hits(patterns: tuple[str, ...], text: str) -> tuple[str, ...]:
    return tuple(p for p in patterns if re.search(p, text))


def classify(question: str) -> Decision:
    """Route a question using rules only. Never calls a model.

    >>> classify("How many rodent complaints in Dorchester?").route
    <Route.AGGREGATE: 'aggregate'>
    >>> classify("Is Roxbury safe?").route
    <Route.VALUE_JUDGMENT: 'value_judgment'>
    >>> classify("What does on_time mean?").route
    <Route.DEFINITION: 'definition'>
    """
    text = re.sub(r"\s+", " ", question.casefold().strip())
    hoods = tuple(neighborhoods.find_in_question(question))

    # 1. Value judgments override everything. Answering "is it safe" with a
    #    number implies a verdict we are not willing to issue.
    if hit := _hits(_VALUE_JUDGMENT, text):
        return Decision(Route.VALUE_JUDGMENT, hoods, hit)

    # 2. Known-absent information: abstain without spending a model call.
    if hit := _hits(_UNANSWERABLE, text):
        return Decision(Route.UNANSWERABLE, hoods, hit)

    # 3. Definitions are about the schema, not the rows. Check before
    #    AGGREGATE so "how is on_time calculated" is not read as a metric.
    if hit := _hits(_DEFINITION, text):
        return Decision(Route.DEFINITION, hoods, hit)

    # 4. Anything numeric goes to SQL. Checked before SCORECARD so that
    #    "how many parks in JP" is a count, not a neighborhood profile.
    if hit := _hits(_AGGREGATE, text):
        return Decision(Route.AGGREGATE, hoods, hit)

    # 5. A named neighborhood plus a profile verb is a scorecard. Two or
    #    more named neighborhoods is a comparison, which is also a scorecard.
    if hoods and (hit := _hits(_SCORECARD, text)):
        return Decision(Route.SCORECARD, hoods, hit)
    if len(hoods) > 1:
        return Decision(Route.SCORECARD, hoods, ("multiple neighborhoods named",))

    # 6. Fall through to retrieval over free text, but flag low confidence
    #    so the caller may escalate to an LLM classifier.
    return Decision(Route.LOOKUP, hoods, (), confident=bool(hoods))


def needs_llm_fallback(decision: Decision) -> bool:
    """True when the rules found no positive signal at all.

    Kept separate from ``classify`` so the rule layer stays pure and
    testable, and so the demo can report how often rules alone sufficed.
    """
    return decision.route is Route.LOOKUP and not decision.confident
