# rag-the-city

Boston data parser and MCP catalog server.

## Layout

- `data/` contains source CSVs, `dd.md`, and derived CSVs in `data/derived/`.
- `parse_csvs.py` transforms source exports into normalized entity tables.
- `mcp_server.py` exposes the derived table catalog through FastMCP.
- `run_mcp.sh` starts the HTTP MCP server with Conda environment `nn`.

## Rebuild derived CSVs

```sh
conda run -n nn python parse_csvs.py
```

Outputs are written to `data/derived/`.

## Run the MCP server

```sh
./run_mcp.sh
```

The server listens on `http://127.0.0.1:3000/mcp` by default. Override the
host or port with `HOST` and `PORT`.

`mcp_server.py` streams derived tables from Oracle Object Storage rather than
from disk. `list_tables()`/`describe_table()` need each table's row count and
header; instead of streaming a full (sometimes 100MB+) CSV to compute those,
they read them from `catalog_meta.json`, which every build script above
writes into `data/derived/` via `rag.catalog_meta.update_catalog_meta()` the
moment it finishes writing a table (merging, so no script clobbers another's
entries). **Upload `data/derived/catalog_meta.json` to the object storage
bucket alongside the CSVs whenever you re-run a build script** — the server
