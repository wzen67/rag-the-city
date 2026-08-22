"""Hybrid retrieval: sparse + dense, fused by Reciprocal Rank Fusion.

Why both halves are needed, concretely: an appropriation of ``4200000``
sits in a CSV field with no dollar sign and no word "million". A
prose-shaped question embeds nowhere near it, so dense retrieval misses
it entirely — while BM25 finds it instantly on the literal digits.
Conversely BM25 is blind to "rodents" matching "rat infestation".

RRF is the fusion step. Dense similarity and BM25 relevance are on
incompatible scales, so rather than trying to normalise them we discard
the magnitudes and combine *rank positions* only:

    score(d) = sum over retrievers of 1 / (k + rank(d)),  k = 60

A document ranked first by either retriever scores well; one both
retrievers liked ranks highest. No training, no tuning beyond k.

Note that ``1/(k + rank)`` is convex, so RRF mildly favours a document
one retriever was *certain* about (ranked 1st and last) over one both
found merely acceptable (2nd and 2nd). That is a property of the metric,
not a bug — see ``test_convexity_favours_a_strong_single_hit``.

The corpus here is deliberately small (see ``MAX_DOCS``). Numbers come
from SQL over every row; this index holds only reference-document
chunks, computed neighborhood summaries, and a sample of free text.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from rank_bm25 import BM25Okapi

from . import llm
from .citations import Citation, CitationKind

#: Hard ceiling on the embedded corpus. Embedding 896k inspection rows
#: would consume the whole build window and buy nothing — aggregation is
#: SQL's job. Enforced, not advisory.
MAX_DOCS = 5_000

#: RRF constant. 60 is the value from the original paper and is what
#: everyone reports against; there is no reason to tune it here.
RRF_K = 60

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens.

    Digits are kept as their own tokens on purpose: BM25's whole value
    here is matching literal codes and figures that embeddings lose.
    """
    return _TOKEN.findall(text.casefold())


@dataclass(frozen=True)
class Document:
    """One retrievable unit, carrying enough provenance to cite itself."""

    id: str
    text: str
    dataset: str
    locator: str | None = None
    kind: CitationKind = "row"
    note: str | None = None
    extra: dict[str, str | int | float | bool] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, str | int | float | bool]:
        meta: dict[str, str | int | float | bool] = {
            "dataset": self.dataset,
            "kind": self.kind,
        }
        if self.locator is not None:
            meta["locator"] = self.locator
        if self.note is not None:
            meta["note"] = self.note
        meta.update(self.extra)
        return meta


@dataclass(frozen=True)
class Scored:
    """A retrieved document with its fused score and per-retriever ranks.

    The ranks are retained so the demo can show *which* retriever found a
    given document — the clearest way to justify hybrid search to a judge.
    """

    doc: Document
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None

    @property
    def found_by(self) -> str:
        if self.dense_rank is not None and self.sparse_rank is not None:
            return "both"
        if self.dense_rank is not None:
            return "dense"
        return "sparse"

    def to_citation(self) -> Citation:
        return Citation(
            kind=self.doc.kind,
            dataset=self.doc.dataset,
            locator=self.doc.locator,
            note=self.doc.note,
        )


def reciprocal_rank_fusion(
    rankings: dict[str, list[str]], k: int = RRF_K
) -> list[tuple[str, float]]:
    """Fuse named ranked ID lists into one ranking.

    Args:
        rankings: retriever name -> document ids, best first.
        k: RRF damping constant.

    Returns:
        ``(doc_id, score)`` sorted best first.

    >>> r = reciprocal_rank_fusion({"a": ["x", "y"], "b": ["y", "x"]})
    >>> [d for d, _ in r]
    ['x', 'y']
    """
    scores: dict[str, float] = defaultdict(float)
    for ids in rankings.values():
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    # Ties broken by id for determinism — a flaky demo is worse than a
    # slightly arbitrary order.
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class _OllamaEmbeddings(EmbeddingFunction[Documents]):
    """Chroma embedding function backed by a local Ollama model."""

    def __init__(self, model: str = llm.EMBED_MODEL) -> None:
        self._model = model

    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - Chroma's name
        return llm.embed(list(input), model=self._model)  # type: ignore[return-value]

    @staticmethod
    def name() -> str:
        return "ollama-embed"

    def get_config(self) -> dict[str, str]:
        return {"model": self._model}

    @classmethod
    def build_from_config(cls, config: dict[str, str]) -> "_OllamaEmbeddings":
        return cls(model=config.get("model", llm.EMBED_MODEL))


class HybridRetriever:
    """Dense (Chroma) + sparse (BM25) retrieval fused with RRF.

    The Chroma collection is persisted when ``persist_dir`` is given so
    the index build is reproducible rather than a one-off in memory.
    """

    def __init__(
        self,
        collection: str = "boston",
        persist_dir: str | Path | None = ".chroma",
        embed_model: str = llm.EMBED_MODEL,
    ) -> None:
        # Chroma enforces this but reports it as an opaque validation
        # error from deep inside its Rust bindings; fail clearly instead.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,510}[a-zA-Z0-9]", collection):
            raise ValueError(
                f"collection name {collection!r} is invalid: Chroma requires "
                "3-512 characters from [a-zA-Z0-9._-], starting and ending "
                "alphanumeric"
            )
        self._client = (
            chromadb.PersistentClient(path=str(persist_dir))
            if persist_dir
            else chromadb.EphemeralClient()
        )
        self._embed = _OllamaEmbeddings(embed_model)
        self._collection = self._client.get_or_create_collection(
            collection, embedding_function=self._embed
        )
        self._docs: dict[str, Document] = {}
        self._bm25: BM25Okapi | None = None
        self._bm25_ids: list[str] = []

    # -- build ---------------------------------------------------------

    def index(self, docs: list[Document], batch_size: int = 128) -> int:
        """Embed and index documents. Returns the count indexed.

        Raises:
            ValueError: if the corpus exceeds ``MAX_DOCS``, or if any two
                documents share an id (which would silently drop one).
        """
        if len(docs) > MAX_DOCS:
            raise ValueError(
                f"{len(docs)} documents exceeds MAX_DOCS={MAX_DOCS}. Aggregate "
                "in SQL and embed summaries instead of raw rows."
            )
        ids = [d.id for d in docs]
        if len(set(ids)) != len(ids):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"duplicate document ids: {sorted(dupes)[:5]}")

        for i in range(0, len(docs), batch_size):
            chunk = docs[i : i + batch_size]
            self._collection.upsert(
                ids=[d.id for d in chunk],
                documents=[d.text for d in chunk],
                metadatas=[d.to_metadata() for d in chunk],
            )

        self._docs.update({d.id: d for d in docs})
        self._rebuild_sparse()
        return len(docs)

    def _rebuild_sparse(self) -> None:
        self._bm25_ids = list(self._docs)
        corpus = [tokenize(self._docs[i].text) for i in self._bm25_ids]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    # -- query ---------------------------------------------------------

    def _dense(self, query: str, n: int) -> list[str]:
        got = self._collection.query(query_texts=[query], n_results=n)
        return list(got.get("ids", [[]])[0])

    def _sparse(self, query: str, n: int) -> list[str]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(zip(self._bm25_ids, scores), key=lambda kv: -kv[1])
        # Drop zero-score hits: BM25 returns every document, and padding
        # the fusion with irrelevant ids only dilutes it.
        return [i for i, s in ranked[:n] if s > 0]

    def search(self, query: str, k: int = 5, candidates: int = 20) -> list[Scored]:
        """Retrieve ``k`` documents using both retrievers, fused by RRF.

        ``candidates`` is how deep each retriever goes before fusion.
        Wider costs nothing at this corpus size and gives RRF more to
        work with.
        """
        if not self._docs:
            return []

        dense = self._dense(query, candidates)
        sparse = self._sparse(query, candidates)
        fused = reciprocal_rank_fusion({"dense": dense, "sparse": sparse})

        dense_pos = {d: i + 1 for i, d in enumerate(dense)}
        sparse_pos = {d: i + 1 for i, d in enumerate(sparse)}

        out: list[Scored] = []
        for doc_id, score in fused[:k]:
            doc = self._docs.get(doc_id)
            if doc is None:  # persisted from an earlier run, not in memory
                continue
            out.append(
                Scored(
                    doc=doc,
                    score=score,
                    dense_rank=dense_pos.get(doc_id),
                    sparse_rank=sparse_pos.get(doc_id),
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._docs)
