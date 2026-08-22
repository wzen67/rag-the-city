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
