"""Engine tests: guardrails, temporal anchoring, and the answer contract.

Everything here is offline. The routes that need a model are exercised in
`scripts/smoke_engine.py`, which is run by hand before the demo — a test
suite that needs a 5 GB model server is a test suite nobody runs.
"""
from __future__ import annotations

import pytest

from rag import engine, guardrails, temporal
from rag.citations import Answer, Citation, abstain, block, decline_judgment


class TestInputGuardrail:
    @pytest.mark.parametrize(
        "q",
        [
            "Where does John Smith live?",
            "where does Maria Garcia live",
            "What is the home address for Robert Chen?",
            "Who lives at 42 Beacon Street?",
            "Give me the phone number for the manager",
            "Everything you know about Sarah Connor",
            "What properties does Michael Bloomberg own?",
        ],
    )
    def test_blocks_person_directed_lookups(self, q):
        assert guardrails.screen(q).blocked, q

    @pytest.mark.parametrize(
        "q",
        [
            "How many potholes in Dorchester?",
            "Which department is slowest to close cases?",
            "What is the median assessed value in Roxbury?",
            "How does Jamaica Plain compare to Back Bay?",
            "What do residents complain about in East Boston?",
            "Where are the most rodent complaints?",
        ],
    )
    def test_allows_ordinary_civic_questions(self, q):
        """Over-blocking caps RAG Quality at 2 for refusing off-script
        queries, so the guard must stay narrow."""
        assert not guardrails.screen(q).blocked, q

    def test_capitalisation_is_not_required_on_the_phrasing(self):
        """Regression: the whole pattern was once case-sensitive, so a
        capitalised sentence start slipped through."""
        assert guardrails.screen("Where does John Smith live?").blocked

    def test_blocked_answers_explain_themselves(self):
        v = guardrails.screen("Where does John Smith live?")
        assert "neighborhood or ZIP" in v.explain()


class TestTemporal:
    @pytest.mark.parametrize(
        "q,expected",
        [
            ("How many incidents in 2025?", "2025"),
            ("crime trend since 2024", "2024-2026"),
            ("incidents from 2023 to 2025", "2023-2025"),
            ("what happened last year", "2025"),
            ("how many potholes?", "no time window (all available years)"),
        ],
    )
    def test_extracts_window(self, q, expected):
        assert temporal.extract(q).describe() == expected

    def test_unbounded_produces_no_filter(self):
        assert temporal.extract("how many potholes?").sql_filter() == ""

    def test_single_year_filter(self):
        assert temporal.extract("in 2025").sql_filter("YEAR") == "YEAR = 2025"

    def test_range_filter(self):
        assert "BETWEEN" in temporal.extract("2023 to 2025").sql_filter()

    def test_warns_when_before_corpus_coverage(self):
        """Answering 'crime in 2015' with a confident zero is the
        'confidently wrong' failure class."""
        assert temporal.out_of_range(temporal.extract("crime in 2015"))

    def test_no_warning_inside_coverage(self):
        assert temporal.out_of_range(temporal.extract("crime in 2025")) is None

    def test_relative_year_is_corpus_anchored_not_wall_clock(self):
        """'last year' must not silently change meaning as time passes."""
        w = temporal.extract("last year")
        assert w.start_year == temporal.CORPUS_LATEST_YEAR - 1


class TestUncertainty:
    def test_scalar_aggregate_is_not_penalised(self):
        """One row from COUNT(*) is the expected shape, not thin evidence."""
        u = guardrails.assess(row_count=1, is_scalar_aggregate=True)
        assert u.level == "high"
        assert not u.should_state

    def test_small_listing_is_flagged(self):
        u = guardrails.assess(row_count=1, is_scalar_aggregate=False)
        assert u.level == "medium"

    def test_zero_rows_is_low(self):
        assert guardrails.assess(row_count=0).level == "low"

    def test_caveats_downgrade_confidence(self):
        u = guardrails.assess(caveats=("district is only 66% Roxbury",))
        assert u.level == "medium"
        assert "66%" in u.render()

    def test_high_confidence_stays_silent(self):
        assert guardrails.assess(row_count=500).render() == ""


class TestAnswerContract:
    def test_grounded_answer_requires_citations(self):
        with pytest.raises(ValueError, match="no citations"):
            Answer(text="42 potholes", route="aggregate")

    def test_abstention_needs_no_citations(self):
        assert abstain("no cause recorded").abstained

    def test_blocked_needs_no_citations(self):
        assert block("I will not answer that").blocked

    def test_declined_carries_metrics_and_refuses(self):
        a = decline_judgment("- crime: 10", [Citation(kind="sql", dataset="crime_only")])
        assert a.declined and not a.abstained
        assert "will not make" in a.text

    def test_abstain_and_decline_are_distinct_states(self):
        """The eval counts them separately: 'data doesn't say' is not the
        same failure as 'I won't issue a verdict'."""
        assert abstain("x").abstained and not abstain("x").declined
        d = decline_judgment("m", [Citation(kind="sql", dataset="d")])
        assert d.declined and not d.abstained

    def test_render_includes_sources_and_uncertainty(self):
        a = Answer(
            text="9,503",
            citations=[Citation(kind="sql", dataset="crime_only", row_count=9503)],
            route="aggregate",
            uncertainty=guardrails.assess(caveats=("approximate",)),
        )
        out = a.render()
        assert "Sources:" in out and "crime_only" in out and "Confidence:" in out

    def test_trace_is_shown_on_request(self):
        a = abstain("x", trace=[("guard", "ok"), ("route", "unanswerable")])
        assert "How this was narrowed" in a.render(show_trace=True)
        assert "How this was narrowed" not in a.render()


class TestSubjectSelection:
    @pytest.mark.parametrize(
        "q,subject",
        [
            ("How many violent crimes in Roxbury?", "crime"),
            ("How many restaurant inspections failed?", "food"),
            ("What is the median assessed value?", "property"),
            ("How many acres of parkland?", "parks"),
            ("How many pothole complaints?", "requests"),
        ],
    )
    def test_picks_the_right_view(self, q, subject):
        from rag.router import classify

        assert engine.pick_subject(q, classify(q)) == subject

    def test_every_subject_maps_to_a_real_view(self):
        from rag import datasets

        for view in engine.VIEW_FOR_ROUTE.values():
            assert view in datasets.VIEWS


class TestScalarAggregateDetection:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT COUNT(*) FROM crime_only", True),
            ("SELECT AVG(hours_to_close) FROM svc311", True),
            ("SELECT neighborhood, COUNT(*) FROM crime_only GROUP BY 1", False),
            ("SELECT * FROM open_space", False),
        ],
    )
    def test_detects_scalar_aggregates(self, sql, expected):
        assert engine._is_scalar_aggregate(sql, ["x"], [(1,)]) is expected

    def test_multiple_rows_is_never_scalar(self):
        assert not engine._is_scalar_aggregate("SELECT COUNT(*) FROM x", ["c"], [(1,), (2,)])
