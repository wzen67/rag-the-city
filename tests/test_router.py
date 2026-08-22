"""Router and neighborhood-resolution tests.

The cases here are the ones that actually bite: compound 311 labels,
"South Boston" shadowing "South Boston Waterfront", and value-judgment
questions that must never be answered as plain aggregates.
"""
from __future__ import annotations

import pytest

from rag import neighborhoods as nb
from rag.router import Route, classify, needs_llm_fallback


class TestResolve:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Dorchester", ("Dorchester",)),
            ("dorchester", ("Dorchester",)),
            ("Greater Mattapan", ("Mattapan",)),
            ("Allston / Brighton", ("Allston", "Brighton")),
            ("Allston/Brighton", ("Allston", "Brighton")),
            ("South Boston / South Boston Waterfront", ("South Boston", "South Boston Waterfront")),
            ("Downtown / Financial District", ("Downtown",)),
            ("Fenway / Kenmore / Audubon Circle / Longwood", ("Fenway", "Longwood")),
            ("Southie", ("South Boston",)),
        ],
    )
    def test_maps_real_311_labels(self, label, expected):
        assert nb.resolve(label) == expected

    @pytest.mark.parametrize("label", ["Boston", "Chestnut Hill", "", None, "nan"])
    def test_refuses_to_guess(self, label):
        """Unplaceable labels return empty, never a wrong neighborhood."""
        assert nb.resolve(label) == ()

    def test_every_canonical_name_resolves_to_itself(self):
        for name in nb.CANONICAL:
            assert nb.resolve(name) == (name,)


class TestFindInQuestion:
    def test_longest_match_wins(self):
        """'South Boston Waterfront' must not be read as 'South Boston'."""
        assert nb.find_in_question("crime in South Boston Waterfront") == [
            "South Boston Waterfront"
        ]

    def test_bare_city_name_is_not_a_neighborhood(self):
        assert nb.find_in_question("How many potholes in Boston?") == []

    def test_east_boston_not_shadowed_by_boston(self):
        assert nb.find_in_question("East Boston parks") == ["East Boston"]

    def test_multiple_neighborhoods(self):
        got = nb.find_in_question("Compare Southie and the Seaport")
        assert got == ["South Boston", "South Boston Waterfront"]

    def test_no_duplicates(self):
        got = nb.find_in_question("Dorchester vs Dorchester")
        assert got == ["Dorchester"]

    def test_nickname_and_canonical_dedupe(self):
        got = nb.find_in_question("JP and Jamaica Plain")
        assert got == ["Jamaica Plain"]


class TestRouting:
    @pytest.mark.parametrize(
        "q",
        [
            "How many rodent complaints in Dorchester last year?",
            "What is the average time to close a 311 case?",
            "Which department is slowest?",
            "Is crime trending up or down since 2023?",
            "What percentage of inspections had critical violations?",
            "How much parkland does Roxbury have?",
        ],
    )
    def test_numeric_questions_go_to_sql(self, q):
        assert classify(q).route is Route.AGGREGATE

    @pytest.mark.parametrize(
        "q",
        [
            "Is Roxbury safe?",
            "Is Dorchester a good place to live?",
            "What's the best neighborhood in Boston?",
            "Should I move to Hyde Park?",
            "Rank the neighborhoods by livability",
        ],
    )
    def test_value_judgments_are_intercepted(self, q):
        assert classify(q).route is Route.VALUE_JUDGMENT

    @pytest.mark.parametrize(
        "q",
        [
            "Why was this license suspended?",
            "What caused the inspection failure?",
            "How do residents feel about the new bike lane?",
        ],
    )
    def test_known_absent_information_abstains(self, q):
        assert classify(q).route is Route.UNANSWERABLE

    @pytest.mark.parametrize(
        "q",
        [
            "What does on_time mean for a 311 case?",
            "How is the SLA target calculated?",
            "What's the difference between reason and type?",
            "What does offense code 3115 mean?",
        ],
    )
    def test_schema_questions_go_to_documents(self, q):
        assert classify(q).route is Route.DEFINITION

    @pytest.mark.parametrize(
        "q",
        [
            "How does Jamaica Plain look?",
            "Tell me about East Boston",
            "Give me a profile of Roslindale",
        ],
    )
    def test_named_neighborhood_profile_is_scorecard(self, q):
        d = classify(q)
        assert d.route is Route.SCORECARD
        assert d.neighborhoods

    def test_two_neighborhoods_is_a_comparison(self):
        d = classify("Roxbury and Back Bay")
        assert d.route is Route.SCORECARD
        assert d.is_comparison

    def test_definition_beats_aggregate(self):
        """'How is X calculated' is about the schema, not a metric."""
        assert classify("How is on_time calculated?").route is Route.DEFINITION

    def test_aggregate_beats_scorecard(self):
        """A count in a named neighborhood is still a count."""
        assert classify("How many parks in JP?").route is Route.AGGREGATE

    def test_value_judgment_beats_aggregate(self):
        """Must not answer 'safest' as a plain ranking query."""
        assert classify("Which is the best neighborhood by crime rate?").route is (
            Route.VALUE_JUDGMENT
        )

    def test_qualitative_falls_through_to_retrieval(self):
        d = classify("What do East Boston residents complain about?")
        assert d.route is Route.LOOKUP
        assert d.neighborhoods == ("East Boston",)

    def test_unmatched_question_requests_llm_fallback(self):
        d = classify("asdf qwerty zxcv")
        assert d.route is Route.LOOKUP
        assert needs_llm_fallback(d)

    def test_matched_route_reports_its_evidence(self):
        """The demo must be able to show WHY a question routed as it did."""
        assert classify("How many potholes?").matched
