"""Embed the parsed prose chunks into a local Chroma collection.

Embeddings come from bge-m3 served by Ollama - no PyTorch, no downloads at
runtime, and it handles the Spanish / Haitian Creole / Chinese text that turns
up in real Boston 311 submissions.

Run from the repo root, with `ollama serve` running:
    python scripts/build_index.py            # build
    python scripts/build_index.py "query"    # build then search
"""

import json
import sys
from pathlib import Path

import chromadb
import requests

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "bge-m3"
CHUNKS = Path("data/reference/chunks.jsonl")
STORE = Path("data/chroma")
COLLECTION = "boston_reference"


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for t in texts:
        r = requests.post(
            f"{OLLAMA}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": t},
            timeout=60,
        )
        r.raise_for_status()
        out.append(r.json()["embedding"])
    return out


def build() -> chromadb.Collection:
    rows = [json.loads(line) for line in CHUNKS.open()]
    print(f"embedding {len(rows)} chunks with {EMBED_MODEL} ...")

    client = chromadb.PersistentClient(path=str(STORE))
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    col = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})

    batch = 32
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        col.add(
            ids=[f"chunk-{i+j}" for j in range(len(part))],
            documents=[r["text"] for r in part],
            embeddings=embed([r["text"] for r in part]),
            metadatas=[{"source": r["source"], "section": r["section"]} for r in part],
        )
        print(f"  {min(i+batch, len(rows))}/{len(rows)}")

    print(f"done - {col.count()} vectors in {STORE}/")
    return col


def search(col: chromadb.Collection, query: str, k: int = 5) -> None:
    res = col.query(query_embeddings=embed([query]), n_results=k)
    print(f'\nsearch: "{query}"')
    for doc, meta, dist in zip(
        res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        print(f"  [{dist:.3f}] ({meta['source']}) {doc[:110]}")


if __name__ == "__main__":
    collection = build()
    if len(sys.argv) > 1:
        search(collection, " ".join(sys.argv[1:]))
