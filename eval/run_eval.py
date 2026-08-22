#!/usr/bin/env python3
"""Eval harness — mechanical scores, no LLM-as-judge.

Wraps:
  1) Person C naive baseline (eval.naive_baseline.answer)
  2) Team engine ask(), if importable

Usage:
  python eval/run_eval.py
  python eval/run_eval.py --no-schema-grounding   # A/B for SQL path
  python eval/run_eval.py --system-only
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

QUESTIONS = ROOT / "eval" / "questions.json"
OUT_MD = ROOT / "results" / "eval.md"
OUT_RAW = ROOT / "results" / "eval_raw.json"

ABSTAIN_RE = re.compile(
    r"\b(does not (record|say|cover|include)|do not (record|know)|don't know|"
    r"no (supporting )?data|cannot (answer|determine)|not in (the )?data|"
    r"no Analyze Boston|out of scope|unanswerable|won't (answer|guess)|will not guess)\b",
    re.I,
)
REFUSE_JUDGMENT_RE = re.compile(
    r"\b(won't make|will not make|won't (rank|declare)|will not (rank|declare)|"
    r"judgment I won'?t|not a (data )?question I can (settle|answer)|"
    r"whether that means|I (can|will) not (call|label|say) (it |.{0,20})safe)\b",
    re.I,
)
SAFE_CLAIM_RE = re.compile(r"\b(is safe|isn't safe|is not safe|unsafe neighborhood|safe neighborhood)\b", re.I)
NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w.])")
SQL_RE = re.compile(r"\bSELECT\b.+\bFROM\b", re.I | re.S)
CITE_RE = re.compile(
    r"(source dataset|source:|case_enquiry_id|INCIDENT_NUMBER|generated SQL|"
    r"row count|Analyze Boston|read_csv_auto|citation:)",
    re.I,
)


def load_system_ask(schema_grounding: bool):
    """Team contract: ask(question: str, schema_grounding: bool = True) -> dict."""
    candidates = [
        "src.qa",
        "src.engine",
        "engine",
        "app.engine",
    ]
    for name in candidates:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        fn = getattr(mod, "ask", None)
        if callable(fn):
            def _ask(q, _fn=fn, _sg=schema_grounding):
                try:
                    return _fn(q, schema_grounding=_sg)
                except TypeError:
                    return _fn(q)

            return _ask, name
    return None, None


def normalize_answer(raw) -> dict:
    if raw is None:
        return {
            "answer": "",
            "sql": None,
            "citations": [],
            "retrieved_ids": [],
            "schema_grounding": None,
            "engine": "missing",
            "abstained": False,
            "refused_value_judgment": False,
        }
    if isinstance(raw, str):
        return normalize_answer({"answer": raw})
    text = raw.get("answer") or raw.get("text") or ""
    return {
        "answer": text,
        "sql": raw.get("sql") or raw.get("generated_sql"),
        "citations": raw.get("citations") or [],
        "retrieved_ids": raw.get("retrieved_ids") or raw.get("retrieved") or [],
        "schema_grounding": raw.get("schema_grounding"),
        "engine": raw.get("engine") or "system",
        "abstained": bool(raw.get("abstained")),
        "refused_value_judgment": bool(raw.get("refused_value_judgment")),
    }


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in NUMBER_RE.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def close_enough(got: float, gold: float, tol: float) -> bool:
    if gold is None:
        return False
    if float(tol) == 0:
        return abs(got - float(gold)) < 0.51  # allow 7957 vs 7957.0 / rounding
    return abs(got - float(gold)) <= float(tol) + 1e-9


def primary_pass(q: dict, ans: dict) -> tuple[bool, str]:
    text = ans["answer"] or ""
    gold = q.get("gold") or {}
    gtype = gold.get("type")

    if q.get("must_abstain"):
        flagged = ans["abstained"] or bool(ABSTAIN_RE.search(text))
        nums = extract_numbers(text)
        # A specific invented count is a fabrication on an unanswerable item.
        fabricated = any(n >= 10 for n in nums) and not flagged
        ok = flagged and not fabricated
        return ok, "abstain" if ok else "expected abstention"

    if q.get("must_refuse_value_judgment"):
        refused = ans["refused_value_judgment"] or bool(REFUSE_JUDGMENT_RE.search(text))
        claimed = bool(SAFE_CLAIM_RE.search(text))
        cited = bool(ans["citations"]) or bool(CITE_RE.search(text))
        ok = refused and not claimed and cited
        return ok, "value-judgment refuse+metrics" if ok else "must refuse 'safe' and still cite metrics"

    if q.get("is_counting") and gold.get("value") is not None:
        nums = extract_numbers(text)
        tol = gold.get("tolerance", 0)
        target = float(gold["value"])
        # T04: accepting "0 populated UCR_PART" OR an explicit abstain-on-UCR
        if q["id"] == "T04":
            empty = bool(re.search(r"\b(empty|all null|not populated|100%\s*empty)\b", text, re.I))
            if empty:
                return True, "UCR_PART empty (correct)"
        hit = any(close_enough(n, target, tol) for n in nums)
        return hit, f"count gold={gold['value']}" + (" hit" if hit else " miss")

    if gtype == "percent" and gold.get("value") is not None:
        nums = extract_numbers(text)
        hit = any(close_enough(n, float(gold["value"]), gold.get("tolerance", 0.2)) for n in nums)
        return hit, f"percent gold={gold['value']}"

    if gtype == "rank":
        val = gold.get("value") or {}
        blob = text.lower()
        keys = [val.get("highest"), val.get("lowest"), val.get("worst")]
        hits = [k.lower() in blob for k in keys if k]
        ok = bool(hits) and all(hits)
        # department question
        if "BTDT" in val:
            ok = "btdt" in blob and ("isd" in blob or "26.1" in text or "88.7" in text)
        return ok, "rank labels present" if ok else "rank miss"

    if gtype == "qualitative":
        needles = gold.get("must_contain") or []
        ok = all(n.lower() in text.lower() for n in needles)
        return ok, "theme" if ok else "missing expected theme"

    if gtype == "definition":
        needles = gold.get("must_contain") or []
        ok = all(n.lower() in text.lower() for n in needles)
        return ok, "definition" if ok else "definition miss"

    if gtype == "scorecard":
        checks = gold.get("checks") or {}
        blob = text.lower()
        named = (checks.get("neighborhood") or "").lower() in blob
        nums = extract_numbers(text)
        metric = False
        if checks.get("ontime_pct") is not None:
            metric = any(close_enough(n, float(checks["ontime_pct"]), 1.0) for n in nums)
        if checks.get("park_acres") is not None:
            metric = metric or any(close_enough(n, float(checks["park_acres"]), 5.0) for n in nums)
        ok = named and metric
        return ok, "scorecard" if ok else "scorecard missing name or metric"

    if gtype == "equity":
        blob = text.lower()
        ok = ("02119" in text or "roxbury" in blob) and ("02130" in text or "jamaica" in blob)
        return ok, "equity both ZIPs/neighborhoods" if ok else "equity miss"

    # default: citations if required
    if q.get("must_cite"):
        ok = bool(ans["citations"]) or bool(CITE_RE.search(text)) or bool(SQL_RE.search(text))
        return ok, "cited" if ok else "missing citation"
    return bool(text.strip()), "non-empty"


def citation_ok(q: dict, ans: dict) -> bool:
    if not q.get("must_cite"):
        return True
    text = ans["answer"] or ""
    return bool(ans["citations"]) or bool(ans["sql"]) or bool(CITE_RE.search(text)) or bool(SQL_RE.search(text))


def fabrication(q: dict, ans: dict) -> bool:
    """Specific number that is not the gold, on counting items, or any big number on abstain items."""
    text = ans["answer"] or ""
    nums = extract_numbers(text)
    if q.get("must_abstain"):
        return any(n >= 10 for n in nums) and not (ans["abstained"] or ABSTAIN_RE.search(text))
    if q.get("is_counting") and q.get("gold", {}).get("value") is not None:
        target = float(q["gold"]["value"])
        tol = q["gold"].get("tolerance", 0)
        if not nums:
            return False
        if any(close_enough(n, target, tol) for n in nums):
            return False
        # Naive B2 trap: 10540 is the wrong (unfiltered) count — that is a fabrication relative to gold.
        naive = q.get("gold", {}).get("naive_value")
        if naive is not None and any(close_enough(n, float(naive), 0) for n in nums):
            return True
        return True
    return False


def retrieval_hit_at_5(q: dict, ans: dict) -> bool | None:
    if not q.get("retrieval"):
        return None
    gold_ids = (q.get("gold") or {}).get("gold_chunk_ids") or []
    if not gold_ids:
        # Keyword fallback until B commits chunk ids.
        hints = (q.get("gold") or {}).get("doc_hints") or (q.get("gold") or {}).get("must_contain") or []
        blob = " ".join(map(str, ans.get("retrieved_ids") or [])) + " " + (ans.get("answer") or "")
        if not hints:
            return None
        return all(h.lower() in blob.lower() for h in hints)
    top = list(ans.get("retrieved_ids") or [])[:5]
    return any(g in top for g in gold_ids)


def sql_accurate(q: dict, ans: dict) -> bool | None:
    if q.get("route") not in {"aggregate", "scorecard"} and not q.get("schema_grounding"):
        if not q.get("is_counting"):
            return None
    sql = ans.get("sql") or ""
    text = ans.get("answer") or ""
    blob = f"{sql}\n{text}"
    if q["id"] == "T04":
        return bool(re.search(r"UCR_PART", blob, re.I)) and bool(
            re.search(r"empty|null|not populated", blob, re.I)
        )
    if q.get("is_counting") and q.get("gold", {}).get("value") is not None:
        ok, _ = primary_pass(q, ans)
        return ok
    if not sql:
        return None
    return True


def score_one(q: dict, ans: dict) -> dict:
    ok, why = primary_pass(q, ans)
    fab = fabrication(q, ans)
    if fab:
        ok = False
    return {
        "id": q["id"],
        "persona": q["persona"],
        "route": q["route"],
        "pass": ok,
        "why": why,
        "citation_ok": citation_ok(q, ans),
        "fabrication": fab,
        "retrieval_hit@5": retrieval_hit_at_5(q, ans),
        "sql_accurate": sql_accurate(q, ans),
        "is_counting": bool(q.get("is_counting")),
        "must_abstain": bool(q.get("must_abstain")),
        "schema_grounding": bool(q.get("schema_grounding")),
        "engine": ans.get("engine"),
        "answer_preview": (ans.get("answer") or "")[:280],
    }


def summarize(rows: list[dict], label: str) -> dict:
    n = len(rows)
    passed = sum(1 for r in rows if r["pass"])
    counting = [r for r in rows if r["is_counting"]]
    counting_ok = sum(1 for r in counting if r["pass"])
    abstain = [r for r in rows if r["must_abstain"]]
    abstain_ok = sum(1 for r in abstain if r["pass"])
    fabs = sum(1 for r in rows if r["fabrication"])
    cites = [r for r in rows if not r["must_abstain"]]
    cite_ok = sum(1 for r in cites if r["citation_ok"])
    hits = [r["retrieval_hit@5"] for r in rows if r["retrieval_hit@5"] is not None]
    sqls = [r for r in rows if r["schema_grounding"] and r["sql_accurate"] is not None]
    sql_ok = sum(1 for r in sqls if r["sql_accurate"])
    by_p = defaultdict(lambda: {"n": 0, "pass": 0})
    for r in rows:
        by_p[r["persona"]]["n"] += 1
        by_p[r["persona"]]["pass"] += int(r["pass"])
    return {
        "label": label,
        "n": n,
        "passed": passed,
        "overall": (passed / n) if n else math.nan,
        "counting_ok": counting_ok,
        "counting_n": len(counting),
        "abstain_ok": abstain_ok,
        "abstain_n": len(abstain),
        "fabrications": fabs,
        "citation_ok": cite_ok,
        "citation_n": len(cites),
        "hit_at_5": (sum(hits) / len(hits)) if hits else None,
        "sql_ok": sql_ok,
        "sql_n": len(sqls),
        "by_persona": dict(by_p),
    }


def pct(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a — route not wired"
    return f"{100.0 * x:.0f}%"


def frac(ok, n) -> str:
    if n == 0:
        return "n/a"
    return f"{ok} / {n}"


def render_md(naive_s, ours_s, naive_rows, ours_rows, schema_on, schema_off, system_name, notes) -> str:
    lines = [
        "# Eval results — Boston Neighborhood Intelligence",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"System module: `{system_name or 'not wired — engine.ask() missing'}`",
        f"Schema grounding flag (ours default run): `{schema_on}`",
        "",
        "Mechanical scoring only (exact/tolerance counts, regex abstention, citation presence).",
        "Gold values: DuckDB over `data/*.csv.gz` via `eval/fill_gold.py`.",
        "",
        "## Naive vs ours",
        "",
        "| Metric | Naive baseline | Ours | Delta |",
        "| --- | --- | --- | --- |",
    ]
    ours_live = bool(ours_rows)
    ours_overall = pct(ours_s["overall"]) if ours_live else "n/a — route not wired"
    ours_count = frac(ours_s["counting_ok"], ours_s["counting_n"]) if ours_live else "n/a — route not wired"
    ours_hit = pct(ours_s["hit_at_5"]) if ours_live else "n/a — route not wired"
    ours_abs = frac(ours_s["abstain_ok"], ours_s["abstain_n"]) if ours_live else "n/a — route not wired"
    ours_fab = str(ours_s["fabrications"]) if ours_live else "n/a — route not wired"
    ours_cite = frac(ours_s["citation_ok"], ours_s["citation_n"]) if ours_live else "n/a — route not wired"
    lines += [
        f"| Overall accuracy | {pct(naive_s['overall'])} | {ours_overall} | |",
        f"| Counting questions correct | {frac(naive_s['counting_ok'], naive_s['counting_n'])} | {ours_count} | |",
        f"| Retrieval hit rate @5 | {pct(naive_s['hit_at_5'])} | {ours_hit} | |",
        f"| SQL accuracy without schema grounding | n/a (naive has no SQL) | {schema_off} | |",
        f"| SQL accuracy with schema grounding | n/a | {schema_on} | |",
        f"| Correct abstentions | {frac(naive_s['abstain_ok'], naive_s['abstain_n'])} | {ours_abs} | |",
        f"| Fabrications | {naive_s['fabrications']} | {ours_fab} | |",
        f"| Citations present (non-abstain) | {frac(naive_s['citation_ok'], naive_s['citation_n'])} | {ours_cite} | |",
        "",
        "## By persona",
        "",
        "| Persona | Naive | Ours |",
        "| --- | --- | --- |",
    ]
    for persona in ("resident", "manager"):
        n = naive_s["by_persona"].get(persona, {"n": 0, "pass": 0})
        o = ours_s["by_persona"].get(persona, {"n": 0, "pass": 0})
        ours_p = frac(o["pass"], o["n"]) if ours_live else "n/a — route not wired"
        lines.append(f"| {persona} | {frac(n['pass'], n['n'])} | {ours_p} |")
    qhead = "## Per-question (ours)" if ours_live else "## Per-question (naive; system not wired)"
    lines += [
        "",
        qhead,
        "",
        "| ID | Persona | Route | Pass | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    src = ours_rows if ours_rows else naive_rows
    for r in src:
        mark = "yes" if r["pass"] else "no"
        lines.append(f"| {r['id']} | {r['persona']} | {r['route']} | {mark} | {r['why']} |")
    lines += [
        "",
        "## Gaps",
        "",
    ]
    if notes:
        for n in notes:
            lines.append(f"- {n}")
    else:
        lines.append("- None recorded.")
    lines += [
        "",
        "## How to reproduce",
        "",
        "```",
        "python eval/fill_gold.py",
        "python eval/naive_baseline.py",
        "python eval/run_eval.py",
        "python eval/run_eval.py --no-schema-grounding",
        "```",
        "",
        "Never cut this file. Empty eval output does not count for Track A.",
        "",
    ]
    return "\n".join(lines)


def run_suite(qs, ask_fn, schema_grounding: bool, label: str):
    rows = []
    raw = []
    for q in qs:
        try:
            ans = normalize_answer(ask_fn(q["question"]) if ask_fn else None)
        except Exception as exc:  # noqa: BLE001 — eval must not die on one item
            ans = normalize_answer({"answer": f"ERROR: {exc}", "engine": "error"})
        scored = score_one(q, ans)
        scored["label"] = label
        scored["schema_flag"] = schema_grounding
        rows.append(scored)
        raw.append({"id": q["id"], "answer": ans, "score": scored})
    return rows, raw


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--questions", type=Path, default=QUESTIONS)
    p.add_argument("--no-schema-grounding", action="store_true")
    p.add_argument("--skip-naive", action="store_true")
    p.add_argument("--system-only", action="store_true")
    args = p.parse_args()

    qs = json.loads(args.questions.read_text())["questions"]
    schema_flag = not args.no_schema_grounding
    notes = []

    naive_rows, naive_raw = [], []
    if not args.system_only:
        from naive_baseline import answer as naive_answer

        naive_rows, naive_raw = run_suite(qs, naive_answer, False, "naive")
    naive_s = summarize(naive_rows, "naive") if naive_rows else summarize([], "naive")

    ask, sys_name = load_system_ask(schema_flag)
    ours_rows, ours_raw = [], []
    if ask:
        ours_rows, ours_raw = run_suite(qs, ask, schema_flag, "ours")
    else:
        notes.append(
            "engine.ask() not importable (tried src.qa, src.engine, engine, app.engine). "
            "Ours column is n/a until Person B lands the router."
        )

    ours_s = summarize(ours_rows, "ours") if ours_rows else {
        **summarize([], "ours"),
        "overall": math.nan,
        "hit_at_5": None,
        "counting_ok": 0,
        "counting_n": naive_s["counting_n"],
        "abstain_ok": 0,
        "abstain_n": naive_s["abstain_n"],
        "citation_n": naive_s["citation_n"],
    }

    schema_on = (
        frac(ours_s["sql_ok"], ours_s["sql_n"]) if ours_rows else "n/a — route not wired"
    )
    schema_off = "n/a — re-run with --no-schema-grounding after B wires SQL"
    if args.no_schema_grounding and ours_rows:
        schema_off = frac(ours_s["sql_ok"], ours_s["sql_n"])
        schema_on = "see default run (without --no-schema-grounding)"
        notes.append("This invocation used --no-schema-grounding; write both runs into this file by merging.")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    md = render_md(naive_s, ours_s, naive_rows, ours_rows, schema_on, schema_off, sys_name, notes)
    OUT_MD.write_text(md)
    OUT_RAW.write_text(
        json.dumps(
            {
                "naive": naive_raw,
                "ours": ours_raw,
                "naive_summary": naive_s,
                "ours_summary": ours_s,
                "system": sys_name,
                "schema_grounding": schema_flag,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    print(md)
    print(f"\nwrote {OUT_MD}")


if __name__ == "__main__":
    main()
