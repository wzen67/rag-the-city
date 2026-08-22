"""The demo web app. Starlette + one static page, no new dependencies.

    ./run_web.sh                 # http://127.0.0.1:8000

Three endpoints, and the split between the first two is the point:

* ``POST /api/plan`` — stages 0-3 only. Pure rules, so it returns in a few
  milliseconds and the UI can show *how* the question was narrowed while
  the slow part is still running.
* ``POST /api/ask``  — the full pipeline, including the model call.
* ``GET  /api/stats`` — corpus figures for the stat tiles.

``/api/ask`` re-parses the executed SQL and returns the result as columns
and rows rather than pre-rendered text, so the page can choose the right
form: one figure is a hero number, several rows are a bar comparison.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import engine as engine_adapter
from rag import db, engine as rag_engine, semantic

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def _table_from_answer(answer) -> dict | None:
    """Re-run the answer's SQL to get typed columns and rows.

    The engine renders a text table for the CLI; the browser wants
    structure so it can pick a form. Re-executing is cheap — the query is
    already validated, and boston.db answers in milliseconds.
    """
    if not answer.sql:
        return None
    try:
        rows, _ = db.run(answer.sql)
    except Exception:
        return None
    if not rows:
        return {"columns": [], "rows": []}
    cols = rag_engine._result_columns(answer.sql, rows)
    return {"columns": cols, "rows": [list(r) for r in rows[:200]]}


async def home(_: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


async def api_plan(request: Request) -> JSONResponse:
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    return JSONResponse(rag_engine.plan(question))


async def api_ask(request: Request) -> JSONResponse:
    body = await request.json()
    question = (body.get("question") or "").strip()
    grounding = bool(body.get("schema_grounding", True))
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    started = time.perf_counter()
    try:
        eng = engine_adapter._shared()
        answer = eng.ask(question, schema_grounding=grounding)
    except Exception as exc:  # never 500 in front of an audience
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}", "question": question},
            status_code=200,
        )

    out = {
        "question": question,
        "answer": answer.text,
        "route": answer.route,
        "sql": answer.sql,
        "abstained": answer.abstained,
        "refused_value_judgment": answer.declined,
        "blocked": answer.blocked,
        "confidence": getattr(answer.uncertainty, "level", None),
        "note": getattr(answer.uncertainty, "render", lambda: "")(),
        "citations": [
            {
                "dataset": c.dataset,
                "locator": c.locator,
                "row_count": c.row_count,
                "note": c.note,
            }
            for c in answer.citations
        ],
        "trace": [{"stage": s, "detail": d} for s, d in answer.trace],
        "table": _table_from_answer(answer),
        "source": answer.citations[0].dataset if answer.citations else None,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    return JSONResponse(out)


async def api_stats(_: Request) -> JSONResponse:
    """Corpus figures for the stat tiles, read live from the database."""
    tiles: list[tuple[str, str]] = []
    try:
        layer = semantic.load()
        total = sum(t.rows for t in layer.tables if t.name not in ("crime_only", "property_homes"))
        crime_all = next((t.rows for t in layer.tables if t.name == "crime"), 0)
        crime_only = next((t.rows for t in layer.tables if t.name == "crime_only"), 0)
        pct = round(100 * (1 - crime_only / crime_all)) if crime_all else 0
        tiles = [
            (f"{total:,}", "rows queried, no sampling"),
            ("26", "neighbourhoods, assigned by point-in-polygon"),
            (f"{pct}%", "of “crime” rows are not crimes — excluded"),
            ("6", "routes, chosen without a model call"),
            (f"{len(layer.rules)}", "semantic-layer rules enforced"),
            ("0", "cloud API calls"),
        ]
    except Exception:
        tiles = [("—", "database not built — run scripts/build_db.py")]
    return JSONResponse(
        {
            "tiles": tiles,
            "footer": (
                "Boston open data via Analyze Boston. Crime covers 2023-2026 and 311 covers "
                "2026 (Jan-Aug), so the system refuses year-over-year 311 comparisons rather "
                "than inventing a baseline."
            ),
        }
    )


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Prepare the engine at boot so the first question is not the slow one."""
    try:
        engine_adapter.warm()
        print("engine ready")
    except Exception as exc:
        print(f"engine not ready ({type(exc).__name__}: {exc}) - "
              "answers will fail until boston.db and Ollama are up")
    yield


routes = [
    Route("/", home),
    Route("/api/plan", api_plan, methods=["POST"]),
    Route("/api/ask", api_ask, methods=["POST"]),
    Route("/api/stats", api_stats, methods=["GET"]),
    Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
