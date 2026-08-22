"""Top-level ``ask()`` — the contract the eval harness imports.

``eval/run_eval.py`` probes for a module named ``src.qa``, ``src.engine``,
``engine`` or ``app.engine`` exposing::

    ask(question: str, schema_grounding: bool = True) -> dict

This module is that adapter, and nothing more. The pipeline lives in
``rag/``; keeping the translation here means neither side has to bend to
the other — the harness gets plain dicts, and ``rag.engine.ask()`` keeps
returning a typed ``Answer``.

``schema_grounding`` is the A/B switch the Track A anchor asks us to
measure. With it off, the SQL prompt loses both the retrieved
semantic-layer facts and the table CAUTION notes, so the control arm is a
real control rather than a partially-helped one.

    from engine import ask
    ask("How many crimes in Roxbury in 2025?")["answer"]
"""
from __future__ import annotations

from typing import Any

from rag import engine as _rag_engine

#: Built once and reused: preparing loads boston.db and embeds the
#: reference corpus, which takes ~15s and must not happen per question.
_ENGINE: _rag_engine.Engine | None = None


def _shared() -> _rag_engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _rag_engine.Engine().prepare()
    return _ENGINE


def ask(question: str, schema_grounding: bool = True) -> dict[str, Any]:
    """Answer one question in the harness's dict contract.

    Args:
        question: The user's question, verbatim.
        schema_grounding: When False, generate SQL without the semantic
            layer's rules, worked examples or column notes — the control
            arm of the grounding measurement.

    Returns:
        A dict carrying the answer text, the SQL actually executed, the
        citations, the retrieved document ids (for hit-rate scoring), and
        the honesty flags the harness scores separately: ``abstained``
        for "the data does not record this" and
        ``refused_value_judgment`` for "I will not issue that verdict".
    """
    eng = _shared()
    answer = eng.ask(question, schema_grounding=schema_grounding)

    return {
        "answer": answer.text,
        "sql": answer.sql,
        "citations": [
            {
                "dataset": c.dataset,
                "locator": c.locator,
                "kind": c.kind,
                "row_count": c.row_count,
                "note": c.note,
            }
            for c in answer.citations
        ],
        # Dataset+locator pairs, which is what the harness matches against
        # its expected sources for retrieval hit-rate.
        "retrieved_ids": [
            f"{c.dataset}#{c.locator}" if c.locator else c.dataset
            for c in answer.citations
        ],
        "schema_grounding": schema_grounding,
        "engine": "rag.engine",
        "abstained": answer.abstained,
        # The harness scores refusing a verdict separately from abstaining:
        # "I won't rank neighborhoods" is not "the data doesn't say".
        "refused_value_judgment": answer.declined,
        # Not part of the required contract, but free and useful in the
        # raw results file.
        "route": answer.route,
        "blocked": answer.blocked,
        "confidence": getattr(answer.uncertainty, "level", None),
        "rendered": answer.render(),
    }


def warm() -> None:
    """Prepare the engine ahead of the first question."""
    _shared()
