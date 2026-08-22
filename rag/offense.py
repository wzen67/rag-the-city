"""Build-time classification for the official BPD offense-code lookup.

The RMS workbook defines codes and names but does not supply UCR parts or a
violent/property flag.  These rules are deliberately kept here, applied once
to create ``offense_dim.csv``, and never reimplemented in generated SQL.
"""

from __future__ import annotations


NON_CRIME_TERMS = (
    "INVESTIGATE", "SICK ASSIST", "SICK/INJURED/MEDICAL", "SERVICE TO OTHER",
    "TOWED", "MISSING PERSON", "PROPERTY - LOST", "PROPERTY - FOUND",
    "PROPERTY - ACCIDENTAL", "WELL BEING", "M/V ACCIDENT", "SUDDEN DEATH",
    "DEATH INVESTIGATION", "FIRE REPORT", "LANDLORD - TENANT", "VERBAL DISPUTE",
    "ASSIST CITIZEN", "PRISONER", "LICENSE PREMISE", "SEARCH WARRANT",
)
VIOLENT_TERMS = (
    "HOMICIDE", "MURDER", "ASSAULT", "ROBBERY", "RAPE", "KIDNAPPING", "MANSLAUGHTER",
)
PROPERTY_TERMS = (
    "LARCENY", "BURGLARY", "VANDALISM", "ARSON", "AUTO THEFT", "PROPERTY DAMAGE",
    "FORGERY", "FRAUD", "EMBEZZLEMENT", "STOLEN PROPERTY", "SHOPLIFTING",
)


def classify_offense(offense_name: str | None) -> str:
    """Return the scorecard class for one official offense-code name."""
    name = (offense_name or "").upper().replace("\u00a0", " ")
    if any(term in name for term in NON_CRIME_TERMS):
        return "not_a_crime"
    if any(term in name for term in VIOLENT_TERMS):
        return "violent"
    if any(term in name for term in PROPERTY_TERMS):
        return "property"
    return "other_crime"
