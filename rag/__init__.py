"""Boston Neighborhood Intelligence — retrieval and routing layer.

Role B owns this package: the router that picks an engine per question,
hybrid (sparse + dense) retrieval, and the citation contract that every
answer must satisfy.

Numbers are never produced here. Aggregation belongs to the SQL layer;
this package decides *which* engine answers, supplies the schema context
that makes generated SQL correct, and guarantees every claim carries a
traceable source.
"""

__all__ = ["citations", "neighborhoods", "router", "retrieval", "schema", "llm"]
