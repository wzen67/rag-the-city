#!/usr/bin/env python3
"""Build typed, entity-oriented CSVs from the Boston source exports.

The data dictionary (dd.md) is the source of truth for column names and date
fields.  Output is deliberately CSV so it can be inspected anywhere, while
values are formatted for DuckDB's CSV reader: DATE values are YYYY-MM-DD and
timestamps are ISO-like strings with an explicit UTC offset when the source
had one.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from rag.catalog_meta import update_catalog_meta


DATE_TYPES = {"DATE"}
TIMESTAMP_TYPES = {"TIMESTAMP", "TIMESTAMPTZ"}


def snake(value: str) -> str:
    value = value.lstrip("\ufeff")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return {"lat": "latitude", "long": "longitude"}.get(value, value)


def load_dictionary(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            result[row["source_file"]][snake(row["column"])] = row
    return result


def clean(value: str | None) -> str:
    value = "" if value is None else value.strip()
    return "" if value.upper() in {"NULL", "N/A", "NA"} else value


def parse_date(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])", value):
        return value[:10]
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"cannot parse DATE value {value!r}") from exc


def parse_timestamp(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    # The source exports are ISO-shaped already.  Avoid constructing a
    # datetime for the common case; this matters for the 896k-row food file.
    fast = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", value)
    if fast:
        fraction = (fast.group(2) or "")[:7]
        offset = fast.group(3) or ""
        if offset == "Z":
            offset = "+00:00"
        elif offset and len(offset) == 5:
            offset = offset[:3] + ":" + offset[3:]
        return fast.group(1) + fraction + offset
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # A few exports contain a space before the offset or more than six
        # fractional-second digits.  Trim only the unsupported precision.
        candidate = re.sub(r"\.(\d{6})\d+", r".\1", candidate).replace(" +", "+")
        parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
        suffix = "+00:00"
    else:
        suffix = ""
    rendered = parsed.strftime("%Y-%m-%d %H:%M:%S")
    if parsed.microsecond:
        rendered += f".{parsed.microsecond:06d}".rstrip("0")
    return rendered + suffix


def normalize_value(name: str, value: str, metadata: dict[str, str] | None) -> str:
    value = clean(value)
    if not value:
        return ""
    dtype = (metadata or {}).get("data_type", "").upper()
    if dtype in DATE_TYPES:
        return parse_date(value)
    if dtype in TIMESTAMP_TYPES:
        return parse_timestamp(value)
    if name == "shooting":
        return {"0": "false", "1": "true"}.get(value.lower(), value.lower())
    if name in {"pos", "pa"}:
        return "true" if value.upper() == "X" else "false"
    if name == "year_acquired" and value == "0":
        return ""
    if name in {"location_zipcode", "zip", "zip_code"}:
        return value.zfill(5) if value.isdigit() and value != "00000" else ("" if value == "00000" else value)
    if name == "precinct":
        return value.zfill(4) if value.isdigit() else value
    if name == "pwd_district":
        return value.zfill(2) if value.isdigit() else value
    if name == "ward":
        match = re.search(r"\d+", value)
        return match.group().zfill(2) if match else value
    if name in {"day_of_week", "reporting_area"}:
        return value
    return value


def row_key(*parts: str) -> str:
    payload = "|".join(parts).encode()
    return hashlib.sha1(payload).hexdigest()[:16]


def location_parts(value: str) -> tuple[str, str]:
    match = re.search(r"\(?\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*\)?", value)
    return (match.group(1), match.group(2)) if match else ("", "")


def source_rows(path: Path, metadata: dict[str, dict[str, str]]):
    """Yield normalized rows from pandas chunks, keeping memory bounded."""
    header_emitted = False
    for frame in pd.read_csv(path, dtype=str, keep_default_na=False,
                             encoding="utf-8-sig", chunksize=100_000):
        frame.columns = [snake(column) for column in frame.columns]
        for column in frame.columns:
            frame[column] = frame[column].astype(str).str.strip()
            info = metadata.get(column)
            if info and info.get("data_type", "").upper() in DATE_TYPES | TIMESTAMP_TYPES:
                # Apply the dictionary-aware normalizer only to date columns;
                # pandas handles CSV parsing and chunking for the wide export.
                frame[column] = frame[column].apply(
                    lambda value, name=column, spec=info: normalize_value(name, value, spec)
                )
            else:
                frame[column] = frame[column].apply(
                    lambda value, name=column, spec=info: normalize_value(name, value, spec)
                )
        if "location" in frame.columns and "latitude" not in frame.columns:
            coordinates = frame["location"].apply(location_parts)
            frame["latitude"] = coordinates.str[0]
            frame["longitude"] = coordinates.str[1]
        if not header_emitted:
            yield [str(column) for column in frame.columns]
            header_emitted = True
        yield from frame.to_dict(orient="records")


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    project_dir = Path(__file__).resolve().parent
    data_dir = project_dir / "data"
    parser.add_argument("--input-dir", type=Path, default=data_dir)
    parser.add_argument("--dictionary", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    input_dir = args.input_dir.resolve()
    dictionary = args.dictionary or input_dir / "dd.md"
    output_dir = (args.output_dir or input_dir / "derived").resolve()
    metadata = load_dictionary(dictionary)

    files = sorted(input_dir.glob("*.csv"))
    outputs: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    # Straight entity tables: normalize and write the existing records directly.
    direct = {
        "311-service-requests.csv": "service_requests_311",
        "crime-incident-reports-august-2015-to-date-source-new-system.csv": "crime_incidents",
        "licensing-board-licenses.csv": "licenses_board",
        "entertainment-licenses-legacy.csv": "licenses_entertainment",
        "open-space.csv": "open_spaces",
        "bpda-neighborhood-boundaries.csv": "neighborhoods_bpda",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, table in direct.items():
        rows = source_rows(input_dir / filename, metadata.get(filename, {}))
        fields = next(rows)
        output = output_dir / f"{table}.csv"
        count = 0
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
                count += 1
        print(f"{table}: {count:,} rows")
        # Row count and header are known for free right now; save them so the
        # MCP catalog server can serve list_tables()/describe_table() without
        # re-streaming the full CSV from object storage.
        update_catalog_meta(output_dir, table, count, fields)

    # Food export is one row per violation.  Split stable location/business
    # identity, inspection visits, and violation facts into separate entities.
    food = source_rows(input_dir / "food-establishment-inspections.csv", metadata.get("food-establishment-inspections.csv", {}))
    food_fields = next(food)
    establishments: dict[str, dict[str, str]] = {}
    inspections: dict[str, dict[str, str]] = {}
    violations: list[dict[str, str]] = []
    for row in food:
        establishment_id = row.get("property_id") or row_key(row.get("businessname", ""), row.get("address", ""), row.get("zip", ""))
        establishment = establishments.setdefault(establishment_id, {
            "establishment_id": establishment_id,
            "property_id": row.get("property_id", ""),
            "businessname": row.get("businessname", ""),
            "dbaname": row.get("dbaname", ""),
            "legalowner": row.get("legalowner", ""),
            "address": row.get("address", ""), "city": row.get("city", ""),
            "state": row.get("state", ""), "zip": row.get("zip", ""),
            "latitude": row.get("latitude", ""), "longitude": row.get("longitude", ""),
        })
        for key in ("latitude", "longitude"):
            if not establishment[key] and row.get(key):
                establishment[key] = row[key]
        inspection_id = row_key(establishment_id, row.get("resultdttm", ""), row.get("result", ""))
        inspections.setdefault(inspection_id, {
            "inspection_id": inspection_id, "establishment_id": establishment_id,
            "resultdttm": row.get("resultdttm", ""), "result": row.get("result", ""),
            "issdttm": row.get("issdttm", ""), "expdttm": row.get("expdttm", ""),
            "licstatus": row.get("licstatus", ""), "licensecat": row.get("licensecat", ""),
        })
        violations.append({
            "violation_id": row_key(establishment_id, row.get("resultdttm", ""), row.get("violation", ""), row.get("violdesc", "")),
            "inspection_id": inspection_id, "establishment_id": establishment_id,
            "violation": row.get("violation", ""), "viol_level": row.get("viol_level", ""),
            "violdesc": row.get("violdesc", ""), "violdttm": row.get("violdttm", ""),
            "viol_status": row.get("viol_status", ""), "status_date": row.get("status_date", ""),
            "comments": row.get("comments", ""),
        })
    outputs["food_establishments"] = (list(next(iter(establishments.values())).keys()) if establishments else [], list(establishments.values()))
    outputs["food_inspections"] = (list(next(iter(inspections.values())).keys()) if inspections else [], list(inspections.values()))
    outputs["food_violations"] = (list(violations[0].keys()) if violations else [], violations)

    for table, (fields, rows) in outputs.items():
        write_csv(output_dir / f"{table}.csv", fields, rows)
        print(f"{table}: {len(rows):,} rows")
        update_catalog_meta(output_dir, table, len(rows), fields)
    print(f"Wrote {len(direct) + 3} derived CSVs to {output_dir}")
    print(
        f"Updated {output_dir / 'catalog_meta.json'} "
        "(upload alongside the CSVs so mcp_server.py can skip full-file scans)"
    )


if __name__ == "__main__":
    main()
