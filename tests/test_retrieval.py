"""Retrieval tests.

The RRF tests are pure and always run. The tests that need embeddings are
marked ``live`` and skip when Ollama is unreachable, so the suite stays
green on a machine without a model server.
"""
from __future__ import annotations

import pytest

from rag import llm
from rag.retrieval import (
    Document,
    HybridRetriever,
    MAX_DOCS,
    reciprocal_rank_fusion,
    tokenize,
)


def _ollama_up() -> bool:
    try:
        return all(llm.health().values())
    except llm.OllamaUnavailable:
        return False


live = pytest.mark.skipif(not _ollama_up(), reason="Ollama or a model is unavailable")


class TestTokenize:
    def test_keeps_digits_as_tokens(self):
        """BM25's job here is matching literal codes; digits must survive."""
        assert "4200000" in tokenize("appropriation of 4200000 dollars")

    def test_case_and_punctuation_insensitive(self):
        assert tokenize("Rodent-Activity!") == ["rodent", "activity"]


class TestRRF:
    def test_agreed_top_hit_wins(self):
        """A document both retrievers rank first beats one both rank last."""
        fused = reciprocal_rank_fusion({"dense": ["a", "b", "c"], "sparse": ["a", "b", "c"]})
        assert [d for d, _ in fused] == ["a", "b", "c"]

    def test_convexity_favours_a_strong_single_hit(self):
        """1/(k+r) is convex, so 1st-and-last edges out 2nd-and-2nd.

        Documenting this deliberately: it looks like a bug and is not.
        RRF mildly rewards a document one retriever was certain about
        over one both retrievers found merely acceptable. The margin is
        tiny (0.0322665 vs 0.0322581 at k=60) but it is real, and a
        future reader should not "fix" it.
        """
        fused = dict(reciprocal_rank_fusion({"dense": ["a", "b", "c"], "sparse": ["c", "b", "a"]}))
        assert fused["a"] > fused["b"]
        assert fused["a"] == pytest.approx(fused["c"])

    def test_single_retriever_passthrough_preserves_order(self):
        fused = reciprocal_rank_fusion({"dense": ["x", "y", "z"]})
        assert [d for d, _ in fused] == ["x", "y", "z"]

    def test_union_not_intersection(self):
        """A document only one retriever found must still appear."""
        fused = reciprocal_rank_fusion({"dense": ["a"], "sparse": ["b"]})
        assert {d for d, _ in fused} == {"a", "b"}

    def test_deterministic_on_ties(self):
        a = reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        b = reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        assert a == b

    def test_empty_input(self):
        assert reciprocal_rank_fusion({}) == []


CORPUS = [
    Document(
        id="budget-csv",
        text="FY2027,Streets,Public Works,PWD-2701,Bridge Rehabilitation Design and Engineering,Capital,4200000",
        dataset="operating-budget.csv",
        locator="PWD-2701",
    ),
    Document(
        id="budget-narrative",
        text=(
            "The FY2027 plan commits 3.65 million dollars to design and engineering "
            "for the bridge rehabilitation, advancing the pedestrian-only concept."
        ),
        dataset="budget-narrative.pdf",
        locator="p4",
    ),
    Document(
        id="rodent-311",
        text="Rodent Activity reported on Blue Hill Avenue: rat burrows along the fence line near dumpsters.",
        dataset="311-service-requests.csv",
        locator="101002318033",
    ),
    Document(
        id="pothole-311",
        text="Pothole in the crosswalk lane at Congress Street, deep enough to bend a wheel.",
        dataset="311-service-requests.csv",
        locator="101002315402",
    ),
    Document(
        id="on-time-def",
        text=(
            "on_time indicates whether the case was closed on or before sla_target_dt. "
            "Cases still open past target are marked OVERDUE."
        ),
        dataset="311-data-dictionary.pdf",
        locator="on_time",
        kind="document",
    ),
]


@live
class TestHybridSearch:
    @pytest.fixture(scope="class")
    def retriever(self, tmp_path_factory):
        r = HybridRetriever(
            collection="test_hybrid",
            persist_dir=tmp_path_factory.mktemp("chroma"),
        )
        r.index(CORPUS)
        return r

    def test_indexes_everything(self, retriever):
        assert len(retriever) == len(CORPUS)

    def test_semantic_match_without_shared_words(self, retriever):
        """'rat infestation' shares no term with the rodent record."""
        top = retriever.search("rat infestation near trash", k=3)
        assert top[0].doc.id == "rodent-311"

    def test_literal_number_is_findable(self, retriever):
        """The core hybrid argument: a bare figure with no currency word.

        Dense retrieval alone loses this; BM25 finds it on the digits.
        """
        top = retriever.search("4200000", k=3)
        ids = [s.doc.id for s in top]
        assert "budget-csv" in ids
        found = next(s for s in top if s.doc.id == "budget-csv")
        assert found.sparse_rank is not None, "BM25 should be the retriever that found it"

    def test_definition_lookup(self, retriever):
        top = retriever.search("what does on_time mean", k=3)
        assert top[0].doc.id == "on-time-def"

    def test_results_carry_citations(self, retriever):
        top = retriever.search("rodents", k=1)
        cite = top[0].to_citation()
        assert cite.dataset and cite.locator

    def test_found_by_is_reported(self, retriever):
        """Needed to justify hybrid on stage."""
        top = retriever.search("design and engineering budget", k=3)
        assert {s.found_by for s in top} <= {"dense", "sparse", "both"}


class TestGuardrails:
    def test_rejects_oversized_corpus(self):
        docs = [Document(id=str(i), text="x", dataset="d") for i in range(MAX_DOCS + 1)]
        with pytest.raises(ValueError, match="MAX_DOCS"):
            HybridRetriever(persist_dir=None).index(docs)

    def test_rejects_duplicate_ids(self):
        docs = [Document(id="same", text="a", dataset="d"), Document(id="same", text="b", dataset="d")]
        with pytest.raises(ValueError, match="duplicate"):
            HybridRetriever(persist_dir=None).index(docs)

    def test_empty_index_returns_nothing(self):
        assert HybridRetriever(persist_dir=None).search("anything") == []
