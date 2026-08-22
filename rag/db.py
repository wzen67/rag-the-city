"""Execution path for generated SQL: always through ``scripts/query.py``.

That module is the only sanctioned way to touch data, and its locks are
stronger than validating the SQL string alone:

1. the connection is read-only,
2. ``enable_external_access = false`` makes ``read_csv_auto()``,
   ``ST_Read()`` and httpfs *error* rather than merely be discouraged, and
3. a table allowlist rejects anything outside the cleaned tables.

Lock 2 is the one that matters for correctness rather than safety. A
generated query like

    SELECT max(TOTAL_VALUE) FROM read_csv_auto('data/property-assessment.csv.gz')

returns 999,900 instead of 2,448,193,300 — the raw column is VARCHAR, so
``max()`` compares strings — and nothing objects. Through this path it
raises instead.

So ``rag.schema.sanitize()`` and this module do different jobs:
sanitize() shapes model output into one bounded SELECT; this refuses to
execute anything that could read around the clean tables.
"""
from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "boston.db"


class DatabaseMissing(RuntimeError):
    """boston.db has not been built yet."""


@lru_cache(maxsize=1)
def _query_module():
    """Import ``scripts/query.py`` without requiring scripts/ to be a package."""
    path = REPO_ROOT / "scripts" / "query.py"
    if not path.exists():
        raise DatabaseMissing(f"{path} not found")
    spec = importlib.util.spec_from_file_location("_rag_query", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rag_query"] = module
    spec.loader.exec_module(module)
    return module


def unsafe_query_error() -> type[Exception]:
    """The exception ``scripts/query.py`` raises for a rejected query."""
    return _query_module().UnsafeQuery


def allowed_tables() -> set[str]:
    return set(_query_module().ALLOWED)


@lru_cache(maxsize=1)
def connect():
    """A read-only connection with external access disabled.

    Cached: the locks are set at connect time and the file is read-only,
    so one connection is reused for the process.
    """
    if not DB_PATH.exists():
        raise DatabaseMissing(
            f"{DB_PATH.name} missing — run: python scripts/build_db.py"
        )
    prev = Path.cwd()
    try:
        # scripts/query.py resolves boston.db relative to the cwd.
        import os

        os.chdir(REPO_ROOT)
        return _query_module().connect()
    finally:
        import os

        os.chdir(prev)


def run(sql: str) -> tuple[list[tuple], str]:
    """Validate and execute. Returns (rows, sql).

    Raises the ``UnsafeQuery`` type from ``scripts/query.py`` when the
    statement names an unknown table or reaches for a file.
    """
    return _query_module().run(sql, connect())


def columns_of(table: str) -> list[tuple[str, str]]:
    """(name, type) pairs for one allowlisted table."""
    rel = connect().sql(f"SELECT * FROM {table} LIMIT 0")
    return list(zip(rel.columns, [str(t) for t in rel.types]))


def scalar(sql: str) -> Any:
    rows, _ = run(sql)
    return rows[0][0] if rows and rows[0] else None


def available() -> bool:
    """True when the database is built and queryable."""
    try:
        connect()
        return True
    except Exception:
        return False
