#!/usr/bin/env python3
"""Naive RAG baseline — Person C owned.

Deliberately dumb: stuff the first N CSV rows into a prompt.
No DuckDB, no offense-code dimension join, no schema injection.
That is the comparison Track A asks for.

If Ollama is up, we call a local generate model. If not, we return an
extractive stub so the harness still produces numbers.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRIME = ROOT / "data" / "crime-incident-reports-august-2015-to-date-source-new-system.csv.gz"
THREE11 = ROOT / "data" / "311-service-requests.csv.gz"
QUESTIONS = ROOT / "eval" / "questions.json"
HEAD_N = 50
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("NAIVE_MODEL", "granite3.1-dense:8b")


def _read_head(path: Path, n: int = HEAD_N) -> str:
    if not path.exists():
        return f"(missing {path.name})"
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        rows = []
        for i, row in enumerate(reader):
            if i >= n:
                break
            rows.append(",".join(row[:12]))
        cols = ",".join(header[:12])
        return cols + "\n" + "\n".join(rows)


def _snippet_bank() -> str:
    return (
        "CRIME CSV HEAD (first 50 rows, truncated columns):\n"
        + _read_head(CRIME)
        + "\n\n311 CSV HEAD (first 50 rows, truncated columns):\n"
        + _read_head(THREE11)
    )


def _ollama(prompt: str) -> str | None:
    body = json.dumps(
        {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return (data.get("response") or "").strip() or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def answer(question: str, context: str | None = None) -> dict:
    """Return a naive answer dict compatible with eval/run_eval.py."""
    ctx = context if context is not None else _snippet_bank()
    prompt = (
        "You are a helpful assistant over Boston open data. "
        "Answer from the CSV snippets below. Guess if needed.\n\n"
        f"{ctx}\n\nQUESTION: {question}\nANSWER:"
    )
    text = _ollama(prompt)
    engine = "ollama" if text else "head-rows-extractive"
    if not text:
        # Extractive fallback: no counting over 100% of rows.
        text = (
            "Based on the first 50 spreadsheet rows I was given, I cannot compute "
            "a citywide total. The snippet may include SICK ASSIST mixed with other "
            "incidents. A rough guess from the head of the file is that counts are "
            "on the order of the rows shown (about 50)."
        )
    return {
        "answer": text,
        "sql": None,
        "citations": [],
        "retrieved_ids": [],
        "schema_grounding": False,
        "engine": engine,
        "abstained": False,
        "refused_value_judgment": False,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--questions", type=Path, default=QUESTIONS)
    p.add_argument("--out", type=Path, default=ROOT / "eval-results" / "naive_answers.json")
    args = p.parse_args()
    qs = json.loads(args.questions.read_text())["questions"]
    ctx = _snippet_bank()
    rows = []
    for q in qs:
        r = answer(q["question"], context=ctx)
        r["id"] = q["id"]
        rows.append(r)
        print(f"{q['id']}: {r['engine']} {r['answer'][:80]!r}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
