"""Stage 2 of progressive disclosure: anchor the question in time.

"Temporal drift" — conflating 1963 with 2024 — is one of the five failure
modes the RAG the City talk calls out, and "demonstrates temporal
awareness" is one of its five Track A judging criteria. A naive pipeline
never anchors *when*, so it happily blends a design spec with a current
restriction.

The fix is cheap and deterministic: pull an explicit window out of the
question before retrieving or generating SQL, and pass it down as a
filter. No model call, so it is free and explainable.

Anchored to the corpus, not to wall-clock time: the committed crime
extract covers 2023-2026, so "last year" resolves against the newest
year present in the data rather than today's date. Otherwise the answer
silently changes meaning as the calendar moves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Newest year present in the committed extracts. Verified: the crime file
#: holds 2023-2026 and 311 is current-year.
CORPUS_LATEST_YEAR = 2026
CORPUS_EARLIEST_YEAR = 2023


@dataclass(frozen=True)
class Window:
    """An inclusive year range, plus how it was derived."""

    start_year: int | None = None
    end_year: int | None = None
    phrase: str | None = None

    @property
    def is_bounded(self) -> bool:
        return self.start_year is not None or self.end_year is not None

    def sql_filter(self, column: str = "YEAR") -> str:
        """A SQL predicate, or empty string when unbounded."""
        if not self.is_bounded:
            return ""
        if self.start_year == self.end_year:
            return f"{column} = {self.start_year}"
        lo = self.start_year or CORPUS_EARLIEST_YEAR
        hi = self.end_year or CORPUS_LATEST_YEAR
        return f"{column} BETWEEN {lo} AND {hi}"

    def describe(self) -> str:
        if not self.is_bounded:
            return "no time window (all available years)"
        if self.start_year == self.end_year:
            return f"{self.start_year}"
        return f"{self.start_year or CORPUS_EARLIEST_YEAR}-{self.end_year or CORPUS_LATEST_YEAR}"


_EXPLICIT_RANGE = re.compile(r"\b(20\d{2})\s*(?:-|to|through|until|–)\s*(20\d{2})\b")
_SINCE = re.compile(r"\bsince\s+(20\d{2})\b")
_BEFORE = re.compile(r"\b(?:before|prior to|up to)\s+(20\d{2})\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
_LAST_N = re.compile(r"\blast\s+(\d+|two|three|four|five)\s+years?\b")
_WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5}


def extract(question: str) -> Window:
    """Derive a time window from a question.

    >>> extract("How many incidents in 2025?").describe()
    '2025'
    >>> extract("crime trend since 2024").describe()
    '2024-2026'
    >>> extract("how many potholes?").is_bounded
    False
    """
    q = question.casefold()

    if m := _EXPLICIT_RANGE.search(q):
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return Window(lo, hi, m.group(0))

    if m := _SINCE.search(q):
        return Window(int(m.group(1)), CORPUS_LATEST_YEAR, m.group(0))

    if m := _BEFORE.search(q):
        return Window(CORPUS_EARLIEST_YEAR, int(m.group(1)) - 1, m.group(0))

    if m := _LAST_N.search(q):
        raw = m.group(1)
        n = _WORD_NUM.get(raw, None) or int(raw) if raw.isdigit() else _WORD_NUM.get(raw, 1)
        return Window(max(CORPUS_EARLIEST_YEAR, CORPUS_LATEST_YEAR - n + 1),
                      CORPUS_LATEST_YEAR, m.group(0))

    if "last year" in q:
        return Window(CORPUS_LATEST_YEAR - 1, CORPUS_LATEST_YEAR - 1, "last year")
    if "this year" in q:
        return Window(CORPUS_LATEST_YEAR, CORPUS_LATEST_YEAR, "this year")

    # A bare year mention, e.g. "in 2024".
    if m := _YEAR.search(q):
        y = int(m.group(1))
        return Window(y, y, m.group(0))

    return Window()


def out_of_range(window: Window) -> str | None:
    """Warn when a window falls outside what the corpus can support.

    Answering "crime in 2015" from a 2023-2026 extract with a confident
    zero is the "confidently wrong" failure class. Better to say so.
    """
    if not window.is_bounded:
        return None
    lo = window.start_year or CORPUS_EARLIEST_YEAR
    hi = window.end_year or CORPUS_LATEST_YEAR
    if hi < CORPUS_EARLIEST_YEAR or lo > CORPUS_LATEST_YEAR:
        return (
            f"asked about {window.describe()}, but the committed data covers "
            f"{CORPUS_EARLIEST_YEAR}-{CORPUS_LATEST_YEAR}"
        )
    if lo < CORPUS_EARLIEST_YEAR:
        return (
            f"data begins in {CORPUS_EARLIEST_YEAR}; the part of "
            f"{window.describe()} before that is not covered"
        )
    return None
