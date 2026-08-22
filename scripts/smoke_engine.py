"""Run the engine over one question per route. The pre-demo check.

    python scripts/smoke_engine.py            # all routes
    python scripts/smoke_engine.py --trace    # show progressive disclosure
    python scripts/smoke_engine.py "your own question"

Needs `ollama serve` plus qwen2.5-coder:7b, granite3.1-dense:8b and bge-m3.
Kept out of the pytest suite deliberately: a test that needs 11 GB of
models resident is a test nobody runs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Runnable as `python scripts/smoke_engine.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag import engine, llm  # noqa: E402

# One per route, chosen so a failure localises immediately.
SMOKE = [
    ("guard", "Where does John Smith live?"),
    ("unanswerable", "Why was this restaurant's license suspended?"),
    ("value_judgment", "Is Roxbury safe?"),
    ("definition", "What does on_time mean for a 311 case?"),
    ("aggregate", "How many crime incidents in Dorchester in 2025?"),
    ("aggregate/time", "How many violent crimes since 2024?"),
    ("aggregate/parks", "How many total acres of parkland?"),
    ("scorecard", "How does Jamaica Plain look?"),
    ("lookup", "What kinds of complaints come from East Boston?"),
    ("out-of-range", "How much crime was there in 2015?"),
]


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--trace"]
    show_trace = "--trace" in sys.argv

    missing = [m for m, ok in llm.health().items() if not ok]
    if missing:
        print(f"missing models: {', '.join(missing)}")
        return 1

    t = time.time()
    eng = engine.Engine().prepare()
    print(f"engine ready in {time.time() - t:.0f}s "
          f"({len(eng.retriever)} reference docs, "
          f"{len(eng.sql_grounding)} view-grounding facts)\n")

    cases = [("custom", q) for q in args] or SMOKE
    failures = 0
    for label, q in cases:
        print("=" * 78)
        print(f"[{label}] {q}")
        t = time.time()
        answer = eng.ask(q)
        print(answer.render(show_trace=show_trace))
        flags = [k for k, v in (
            ("abstained", answer.abstained),
            ("declined", answer.declined),
            ("blocked", answer.blocked),
        ) if v]
        print(f"\n-> route={answer.route} {' '.join(flags)} in {time.time() - t:.0f}s\n")

        # Every answer must be cited, or explicitly not a claim.
        if answer.is_grounded_claim and not answer.citations:
            print("!! uncited grounded answer")
            failures += 1

    print("=" * 78)
    print(f"{len(cases)} questions, {failures} contract violation(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
