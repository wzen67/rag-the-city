"""Part B against Part A: the semantic layer and the locked query path.

The tests that need boston.db skip when it has not been built, so a fresh
checkout still gets a green suite. Build it with:

    python scripts/build_db.py
"""
from __future__ import annotations

import pytest

from rag import db, engine, semantic

needs_db = pytest.mark.skipif(not db.available(), reason="boston.db not built")


class TestSemanticLayer:
    def test_loads(self):
        layer = semantic.load()
        assert layer.tables and layer.rules and layer.examples

    def test_knows_the_cleaned_tables(self):
        names = semantic.load().table_names
        for t in ("crime_only", "svc311", "property_homes", "open_space"):
            assert t in names

    def test_crime_only_is_described_as_the_default(self):
        t = semantic.load().table("crime_only")
        assert t and "crime" in t.meaning.casefold()

    def test_examples_are_question_answer_pairs(self):
        """Most examples are SQL; some deliberately teach abstention."""
        for question, sql in semantic.load().examples:
            assert question.strip()
            assert "select" in sql.casefold() or "UNANSWERABLE" in sql

    def test_at_least_one_example_teaches_abstention(self):
        """A few-shot set of only-answerable questions teaches the model
        that everything is answerable."""
        assert any("UNANSWERABLE" in sql for _, sql in semantic.load().examples)

    def test_an_unanswerable_example_cannot_be_mistaken_for_sql(self):
        """If the model copies that example, sanitize() must reject it so
        the engine abstains instead of executing a comment."""
        from rag.schema import UnsafeSQL, sanitize

        unanswerable = next(
            sql for _, sql in semantic.load().examples if "UNANSWERABLE" in sql
        )
        with pytest.raises(UnsafeSQL):
            sanitize(unanswerable)

    def test_prompt_block_carries_rules_and_examples(self):
        block = semantic.prompt_block("crime_only")
        assert "Rules that must be obeyed" in block
        assert "Worked examples" in block
        assert "crime_only" in block

    def test_prompt_block_includes_the_supplementary_rules(self):
        """The property/neighborhood rule prevents a silently wrong number,
        so it must be in every prompt, not left to retrieval luck."""
        assert "SELECT zipcode FROM neighborhoods" in semantic.prompt_block("property_homes")

    def test_grounding_documents_have_unique_ids(self):
        docs = semantic.grounding_documents()
        assert len({d.id for d in docs}) == len(docs)
        assert len(docs) > 20


class TestRuleBasedAbstention:
    @pytest.mark.parametrize(
        "q",
        [
            "How many 311 complaints this year compared to last year?",
            "What is the year over year change in pothole complaints?",
            "How many rodent complaints since 2024?",
        ],
    )
    def test_311_year_comparisons_are_unanswerable(self, q):
        """311 holds 2026 only, so there is no earlier year to compare."""
        assert semantic.unanswerable_by_rule(q)

    @pytest.mark.parametrize(
        "q",
        [
            "How many crimes in Roxbury in 2025?",
            "How many 311 complaints in Dorchester?",
            "What is the median home value?",
        ],
    )
    def test_answerable_questions_pass_through(self, q):
        assert semantic.unanswerable_by_rule(q) is None

    def test_revoked_licence_questions_are_unanswerable(self):
        assert semantic.unanswerable_by_rule("How many revoked licenses are there?")


class TestRoutingToTheRightTable:
    def test_crime_routes_to_crime_only(self):
        """`crime` includes non-crimes and overstates by ~97% (rule 6)."""
        assert engine.VIEW_FOR_ROUTE["crime"] == "crime_only"

    def test_food_routes_to_inspections_not_violations(self):
        """food_violations has ~4x the rows (rule 7)."""
        assert engine.VIEW_FOR_ROUTE["food"] == "food_inspections"

    def test_property_routes_to_homes(self):
        """`property` includes 8,545 parking spaces (rule 8)."""
        assert engine.VIEW_FOR_ROUTE["property"] == "property_homes"

    def test_every_target_is_allowlisted_for_execution(self):
        if not db.available():
            pytest.skip("boston.db not built")
        allowed = db.allowed_tables()
        for table in engine.VIEW_FOR_ROUTE.values():
            assert table in allowed, table


@needs_db
class TestLockedQueryPath:
    def test_reads_the_cleaned_tables(self):
        rows, _ = db.run("SELECT count(*) FROM crime_only")
        assert rows[0][0] == 146_933

    def test_open_space_is_complete(self):
        """577, not 272: ignore_errors undercounts parkland by 60%."""
        assert db.scalar("SELECT count(*) FROM open_space") == 577

    def test_raw_file_access_is_refused(self):
        """The lock that matters: reading the raw CSV returns a wrong
        number rather than an error, so it must be impossible."""
        with pytest.raises(Exception):
            db.run(
                "SELECT max(TOTAL_VALUE) FROM "
                "read_csv_auto('data/property-assessment.csv.gz')"
            )

    def test_unknown_table_is_refused(self):
        with pytest.raises(db.unsafe_query_error()):
            db.run("SELECT * FROM secret_table")

    def test_writes_are_refused(self):
        with pytest.raises(db.unsafe_query_error()):
            db.run("DROP TABLE crime")

    def test_neighborhoods_has_no_zipcode(self):
        """The trap behind the supplementary rule: a subquery selecting
        zipcode from neighborhoods silently resolves against the outer
        table and matches every row."""
        assert "zipcode" not in [c for c, _ in db.columns_of("neighborhoods")]

    def test_zip_crosswalk_actually_filters(self):
        """Regression: the neighborhood-scoped property figure used to
        equal the all-Boston figure."""
        scoped = db.scalar(
            "SELECT median(total_value) FROM property_homes WHERE zipcode IN "
            "(SELECT DISTINCT zipcode FROM svc311 WHERE neighborhood = 'Back Bay' "
            "AND zipcode IS NOT NULL)"
        )
        citywide = db.scalar("SELECT median(total_value) FROM property_homes")
        assert scoped != citywide
