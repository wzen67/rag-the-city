"""Canonical neighborhoods and alias resolution.

The 26 canonical names come from ``bpda-neighborhood-boundaries.csv.gz``
(the BPDA planning-district list), which is also the ``name`` field on the
polygons in ``boston_neighborhood_boundaries.json.gz``. Using BPDA as the
spine means the router's entity extraction and the SQL layer's spatial
join agree on one vocabulary.

Two separate problems live here:

1. **311 labels are not canonical.** 311 ships compound labels
   (``"Allston / Brighton"``), a redundant prefix (``"Greater Mattapan"``),
   a meaningless catch-all (``"Boston"``, 4,733 rows), and duplicates that
   coexist with their own compound form (``"South Boston"`` alongside
   ``"South Boston / South Boston Waterfront"``).
2. **Humans use nicknames.** A resident asks about "Southie" or "JP".

Both map onto the canonical set here. Compound labels resolve to *several*
canonical names, so callers get a tuple and must decide how to handle
ambiguity rather than silently picking one.

This module is deliberately data-free: assigning a *row* to a
neighborhood is the SQL layer's spatial join. This is only for resolving
names that appear in text.
"""
from __future__ import annotations

import re
from functools import lru_cache

#: The 26 BPDA neighborhoods. Source of truth for every other vocabulary.
CANONICAL: tuple[str, ...] = (
    "Allston",
    "Back Bay",
    "Bay Village",
    "Beacon Hill",
    "Brighton",
    "Charlestown",
    "Chinatown",
    "Dorchester",
    "Downtown",
    "East Boston",
    "Fenway",
    "Harbor Islands",
    "Hyde Park",
    "Jamaica Plain",
    "Leather District",
    "Longwood",
    "Mattapan",
    "Mission Hill",
    "North End",
    "Roslindale",
    "Roxbury",
    "South Boston",
    "South Boston Waterfront",
    "South End",
    "West End",
    "West Roxbury",
)

#: Sentinel for geography we cannot place on the canonical map.
UNKNOWN = "UNKNOWN"

# Compound and non-canonical labels as they actually appear in 311, plus
# the nicknames residents use. Values are tuples because a compound label
# legitimately covers more than one canonical neighborhood.
_ALIASES: dict[str, tuple[str, ...]] = {
    # --- 311's own labels, verified against the shipped extract ---
    "south boston / south boston waterfront": ("South Boston", "South Boston Waterfront"),
    "allston / brighton": ("Allston", "Brighton"),
    "downtown / financial district": ("Downtown",),
    "fenway / kenmore / audubon circle / longwood": ("Fenway", "Longwood"),
    "greater mattapan": ("Mattapan",),
    # --- nicknames and common variants ---
    "southie": ("South Boston",),
    "eastie": ("East Boston",),
    "jp": ("Jamaica Plain",),
    "rozzie": ("Roslindale",),
    "dot": ("Dorchester",),
    "the fenway": ("Fenway",),
    "financial district": ("Downtown",),
    "kenmore": ("Fenway",),
    "audubon circle": ("Fenway",),
    "seaport": ("South Boston Waterfront",),
    "the seaport": ("South Boston Waterfront",),
    "fort point": ("South Boston Waterfront",),
    "downtown crossing": ("Downtown",),
    "government center": ("Downtown",),
    "uphams corner": ("Dorchester",),
    "codman square": ("Dorchester",),
    "grove hall": ("Roxbury",),
    "dudley square": ("Roxbury",),
    "nubian square": ("Roxbury",),
    "egleston square": ("Roxbury",),
    "jamaica pond": ("Jamaica Plain",),
}

# Labels we refuse to guess at. "Boston" is a city-wide catch-all in 311;
# Chestnut Hill straddles Brookline and Newton and is not a BPDA district.
_UNRESOLVABLE: frozenset[str] = frozenset({"boston", "chestnut hill", "", "nan", "none"})


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, and normalise slash spacing."""
    t = text.casefold().strip()
    t = t.replace("&", " and ")
    t = re.sub(r"\s*/\s*", " / ", t)
    return re.sub(r"\s+", " ", t)


@lru_cache(maxsize=2048)
def resolve(label: str | None) -> tuple[str, ...]:
    """Map any geography label onto canonical neighborhoods.

    Returns an empty tuple for labels we deliberately refuse to place —
    ``"Boston"``, ``"Chestnut Hill"``, nulls — so callers can count
    unresolved rows instead of inheriting a wrong neighborhood.

    >>> resolve("Greater Mattapan")
    ('Mattapan',)
    >>> resolve("Allston / Brighton")
    ('Allston', 'Brighton')
    >>> resolve("Boston")
    ()
    """
    if label is None:
        return ()
    key = _norm(label)
    if key in _UNRESOLVABLE:
        return ()
    if key in _ALIASES:
        return _ALIASES[key]
    for name in CANONICAL:
        if key == _norm(name):
            return (name,)
    return ()


# Longest-first so "South Boston Waterfront" wins over "South Boston",
# and "East Boston" is never shadowed by a bare "Boston" match.
_MENTION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    sorted(
        ([(_norm(n), (n,)) for n in CANONICAL] + [(k, v) for k, v in _ALIASES.items()]),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
)


def find_in_question(question: str) -> list[str]:
    """Extract canonical neighborhoods mentioned in free text.

    Matches on word boundaries and resolves overlaps in favour of the
    longest term, so "South Boston Waterfront" is never read as "South
    Boston". Results come back in the order they appear in the question,
    de-duplicated. Used by the router to tell a neighborhood profile
    ("how does Roxbury look?") from a city-wide aggregate.

    >>> find_in_question("Compare Southie and the Seaport")
    ['South Boston', 'South Boston Waterfront']
    >>> find_in_question("How many potholes in Boston?")
    []
    """
    haystack = _norm(question)
    # Scan longest-term-first so overlaps resolve correctly, but record
    # where each hit sat so the caller sees question order, not term order.
    hits: list[tuple[int, tuple[str, ...]]] = []
    claimed: list[tuple[int, int]] = []

    for term, canon in _MENTION_TERMS:
        for m in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", haystack):
            span = (m.start(), m.end())
            if any(s <= span[0] and span[1] <= e for s, e in claimed):
                continue  # already covered by a longer term
            claimed.append(span)
            hits.append((span[0], canon))

    found: list[str] = []
    for _, canon in sorted(hits, key=lambda h: h[0]):
        found += [c for c in canon if c not in found]
    return found
