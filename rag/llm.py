"""Thin Ollama client.

Deliberately not LangChain: the pipeline needs exactly three things from
a model server — embed a batch, generate text, and fail in a way we can
route around — and hand-rolling that is fewer moving parts than a
framework wrapper, with no hidden prompt templating.

Two generation models are configured because SQL and prose are different
skills: a code-specialised 7B beats a general 8B at text-to-SQL, while
granite is better at citation-bearing prose. Both fit in memory
alongside the embedder.
"""
from __future__ import annotations

import os

import requests

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

#: Code-specialised, for text-to-SQL.
SQL_MODEL = os.environ.get("SQL_MODEL", "qwen2.5-coder:7b")
#: Prose answers. Defaults to the SAME model as SQL on purpose: holding two
#: 5 GB generation models resident costs ~10 GB, and on a memory-constrained
#: machine Ollama evicts and reloads between calls, which turned a 2 s
#: generation into 40 s. One model means no swapping. Set CHAT_MODEL to
#: granite3.1-dense:8b if there is headroom — it writes better prose.
CHAT_MODEL = os.environ.get("CHAT_MODEL", SQL_MODEL)
#: Multilingual, 1024-dim. Boston's 311 text is not only English.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")

TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))

#: Ollama unloads a model after 5 minutes by default, so the first question
#: after a pause pays a full cold load. Hold them for the length of a demo.
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


class OllamaUnavailable(RuntimeError):
    """Raised when the model server cannot be reached or errors out.

    Callers are expected to catch this and degrade — an abstention beats
    a crash in front of judges.
    """


def _post(path: str, payload: dict) -> dict:
    try:
        r = requests.post(f"{HOST}{path}", json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise OllamaUnavailable(f"{path} failed: {exc}") from exc


def embed(texts: list[str], model: str = EMBED_MODEL, batch_size: int = 64) -> list[list[float]]:
    """Embed texts, batched. Returns one vector per input, in order."""
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        data = _post("/api/embed",
                     {"model": model, "input": chunk, "keep_alive": KEEP_ALIVE})
        vecs = data.get("embeddings")
        if not vecs or len(vecs) != len(chunk):
            raise OllamaUnavailable(
                f"embed returned {len(vecs or [])} vectors for {len(chunk)} inputs"
            )
        out.extend(vecs)
    return out


#: Cap on generated tokens. Answers here are short — a figure, a table, a
#: quoted definition — and an uncapped 8B model will happily continue the
#: prompt pattern and invent a follow-up Q&A turn. Capping is both a
#: latency fix and a correctness one.
MAX_TOKENS = 320

#: Anything after these means the model has started a new turn on its own.
_RUNAWAY = ("\nQuestion:", "\nQ:", "\nUser:", "\nContext:")


def _truncate_runaway(text: str) -> str:
    """Cut a self-continued Q&A turn off the end of a response."""
    cut = len(text)
    for marker in _RUNAWAY:
        i = text.find(marker)
        if i != -1:
            cut = min(cut, i)
    return text[:cut].strip()


def generate(
    prompt: str,
    model: str = CHAT_MODEL,
    temperature: float = 0.0,
    system: str | None = None,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Single-turn generation. Temperature 0 by default — this is a
    question-answering system, not a creative one."""
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "stop": ["\nQuestion:", "\nQ:", "\nUser:"],
        },
    }
    if system:
        payload["system"] = system
    raw = _post("/api/generate", payload).get("response", "").strip()
    return _truncate_runaway(raw)


def health() -> dict[str, bool]:
    """Report which configured models are actually present.

    Called at startup so a missing pull surfaces immediately rather than
    mid-demo.
    """
    try:
        tags = requests.get(f"{HOST}/api/tags", timeout=10).json().get("models", [])
    except requests.RequestException as exc:
        raise OllamaUnavailable(f"cannot reach {HOST}: {exc}") from exc
    have = {m.get("name", "") for m in tags}
    have |= {n.split(":")[0] for n in have}
    return {m: (m in have or m.split(":")[0] in have) for m in (SQL_MODEL, CHAT_MODEL, EMBED_MODEL)}
