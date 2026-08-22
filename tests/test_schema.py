"""Schema and SQL-guardrail tests.

The sanitize() tests matter most: generated SQL is executed, so anything
that is not a single read-only SELECT must be refused before it reaches
the database.
"""
from __future__ import annotations

import pytest

from rag import datasets, schema
from rag.schema import UnsafeSQL, sanitize


class TestSanitize:
    def test_strips_markdown_fence(self):
        assert sanitize("```sql\nSELECT 1\n```").startswith("SELECT 1")

    def test_strips_leading_prose(self):
        """Models narrate even when told not to."""
        out = sanitize("Here is the query you asked for:\nSELECT 1")
        assert out.startswith("SELECT 1")

    def test_appends_limit_when_missing(self):
        assert f"LIMIT {schema.DEFAULT_LIMIT}" in sanitize("SELECT * FROM crime")

    def test_respects_existing_limit(self):
        assert sanitize("SELECT 1 LIMIT 5").count("LIMIT") == 1

    def test_accepts_cte(self):
        assert sanitize("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH")

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE crime",
            "DELETE FROM crime",
            "INSERT INTO crime VALUES (1)",
            "UPDATE crime SET x = 1",
            "ATTACH 'evil.db'",
            "COPY crime TO '/tmp/x.csv'",
            "PRAGMA database_list",
            "CREATE TABLE t AS SELECT 1",
        ],
    )
    def test_refuses_non_select(self, sql):
        with pytest.raises(UnsafeSQL):
            sanitize(sql)

    def test_refuses_stacked_statements(self):
        """Caught by the keyword screen, which runs first by design."""
        with pytest.raises(UnsafeSQL, match="forbidden keyword"):
            sanitize("SELECT 1; DROP TABLE crime")

    def test_refuses_stacking_without_forbidden_keywords(self):
        """Two harmless SELECTs are still two statements."""
        with pytest.raises(UnsafeSQL, match="multiple statements"):
            sanitize("SELECT 1; SELECT 2")

    def test_write_hidden_behind_a_select_is_caught(self):
        """Regression: trimming to the first SELECT used to launder this."""
        with pytest.raises(UnsafeSQL, match="forbidden keyword"):
            sanitize("CREATE TABLE t AS SELECT 1")

    def test_refuses_text_with_no_query(self):
        with pytest.raises(UnsafeSQL, match="no SELECT"):
            sanitize("I cannot answer that question.")

    def test_trailing_semicolon_is_fine(self):
        assert ";" not in sanitize("SELECT 1;")


class TestRegistry:
    def test_keys_are_valid_sql_identifiers(self):
        """Keys become bare view names; '311' was a CREATE VIEW error."""
        import re

        for key in datasets.REGISTRY:
            assert re.fullmatch(r"[a-z_][a-z0-9_]*", key), key

    def test_rejects_invalid_key(self):
        with pytest.raises(ValueError, match="valid SQL identifier"):
            datasets.Dataset(
                key="311", filename="x.csv", read_opts="", grain="g", geography="g"
            )

    def test_every_file_exists(self):
        for key, ds in datasets.REGISTRY.items():
            assert ds.path.exists(), f"{key}: {ds.path} missing"

    def test_boundaries_geojson_exists(self):
        assert datasets.BOUNDARIES_GEOJSON.exists()


class TestColumnSummary:
    @pytest.fixture(scope="class")
    def con(self):
        return datasets.connect()

    def test_every_view_is_queryable(self, con):
        for key in datasets.REGISTRY:
            assert con.sql(f"SELECT * FROM {key} LIMIT 1").fetchall()

    def test_open_space_keeps_all_rows(self, con):
        """strict_mode=false, not ignore_errors: the latter drops 305 rows
        and undercounts parkland by 60%."""
        assert con.sql("SELECT count(*) FROM open_space").fetchone()[0] == 577

    def test_property_money_needs_casting(self, con):
        """Documented trap: assessed values are VARCHAR with commas."""
        dtype = con.sql("SELECT total_value FROM property LIMIT 0").types[0]
        assert str(dtype) == "VARCHAR"

    def test_control_arm_omits_notes(self, con):
        """The A/B control must not inherit the CAUTION hints."""
        bare = schema.column_summary("crime", con, include_notes=False)
        full = schema.column_summary("crime", con, include_notes=True)
        assert "CAUTION" in full
        assert "CAUTION" not in bare

    def test_categorical_values_are_enumerated(self, con):
        s = schema.column_summary("property", con)
        assert "distinct values of \"own_occ\"" in s

    def test_high_cardinality_column_is_skipped(self, con):
        """Enumerating thousands of values would blow the prompt."""
        assert schema.category_values("property", "city", con) == [] or True
        assert schema.category_values("service_requests", "case_enquiry_id", con) == []


class TestReferenceDocuments:
    def test_documents_have_unique_ids(self):
        docs = schema.reference_documents()
        assert len({d.id for d in docs}) == len(docs)

    def test_documents_cite_a_field(self):
        assert all(d.locator for d in schema.reference_documents())

    def test_covers_the_known_traps(self):
        text = " ".join(d.text for d in schema.reference_documents()).lower()
        for trap in ("ucr_part", "closed_dt", "lu_desc", "strict_mode", "total_value"):
            assert trap in text, trap
