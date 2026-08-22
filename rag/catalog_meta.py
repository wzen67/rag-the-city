"""Shared helper for the MCP catalog server's row-count/column sidecar.

Each build step (parse_csvs.py, scripts/build_geography.py,
scripts/build_district_purity.py, ...) already knows a derived table's exact
row count and header the moment it finishes writing that table's CSV. Calling
update_catalog_meta() here saves that for free, so mcp_server.py can serve
list_tables()/describe_table() from data/derived/catalog_meta.json instead of
streaming the full CSV over the network just to count rows (see
mcp_server.py:remote_catalog_meta()). Upload catalog_meta.json to object
storage alongside the CSVs it describes.

Because build steps run as separate processes in sequence, this merges into
whatever catalog_meta.json is already in output_dir rather than overwriting
it, so no step clobbers another step's entries.
"""

from __future__ import annotations

import json
from pathlib import Path


def update_catalog_meta(output_dir: Path, table: str, row_count: int, columns: list[str]) -> None:
    meta_path = output_dir / "catalog_meta.json"
    meta: dict[str, dict[str, object]] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta[table] = {"row_count": row_count, "columns": columns}
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
