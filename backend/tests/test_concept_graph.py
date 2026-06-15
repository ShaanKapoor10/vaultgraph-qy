"""
Integration tests for the concept graph builder.
Uses an in-memory SQLite DB populated with fixture triples.
No LLM or external calls.
"""
from __future__ import annotations

import importlib
import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "graph_test.db"
    monkeypatch.setenv("BRAHMASTRA_DB", str(db_file))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def _seed(db):
    """Insert a small set of notes + triples + canonical map."""
    db.upsert_note("n1", "Note 1", "Alice reports to Bob.", mark_pending=False)
    db.upsert_note("n2", "Note 2", "Bob owns ProjectX.", mark_pending=False)
    db.upsert_note("n3", "Note 3", "ProjectX depends on TensorFlow.", mark_pending=False)

    triples = [
        {
            "subject_text": "Alice", "subject_type": "person",
            "relation": "reports_to",
            "object_text": "Bob", "object_type": "person",
            "confidence": 0.95, "source_quote": "Alice reports to Bob",
            "source_note_id": "n1",
        },
        {
            "subject_text": "Bob", "subject_type": "person",
            "relation": "owns",
            "object_text": "ProjectX", "object_type": "project",
            "confidence": 0.9, "source_quote": "Bob owns ProjectX",
            "source_note_id": "n2",
        },
        {
            "subject_text": "ProjectX", "subject_type": "project",
            "relation": "depends_on",
            "object_text": "TensorFlow", "object_type": "tool",
            "confidence": 0.85, "source_quote": "ProjectX depends on TensorFlow",
            "source_note_id": "n3",
        },
    ]
    db.insert_triples(triples)

    # Identity canonical map (no merging needed for this fixture)
    clusters = [
        {"cluster_id": "c0001", "canonical_name": "Alice", "mentions": ["Alice"]},
        {"cluster_id": "c0002", "canonical_name": "Bob", "mentions": ["Bob"]},
        {"cluster_id": "c0003", "canonical_name": "ProjectX", "mentions": ["ProjectX"]},
        {"cluster_id": "c0004", "canonical_name": "TensorFlow", "mentions": ["TensorFlow"]},
    ]
    db.replace_canonical_map(clusters)


def test_build_graph_nodes_and_edges(temp_db):
    _seed(temp_db)
    from brahmastra.concept_graph import run_build_graph
    result = run_build_graph()
    stats = result["stats"]
    assert stats["nodes"] == 4
    assert stats["edges"] == 3


def test_pagerank_computed(temp_db):
    _seed(temp_db)
    from brahmastra.concept_graph import run_build_graph
    result = run_build_graph()
    nodes = result["graph"]["nodes"]
    ranks = {n["id"]: n["pagerank"] for n in nodes}
    # All nodes have non-negative pagerank
    for v in ranks.values():
        assert v >= 0.0
    # Bob is both target (from Alice) and source (to ProjectX) — should rank well
    assert ranks["Bob"] > 0


def test_graph_cached_after_build(temp_db):
    _seed(temp_db)
    from brahmastra.concept_graph import run_build_graph
    run_build_graph()
    cached = temp_db.get_cached_graph()
    assert cached is not None
    assert cached["stats"]["nodes"] == 4


def test_empty_graph_no_crash(temp_db):
    """Building from empty DB should return zeros gracefully."""
    from brahmastra.concept_graph import run_build_graph
    result = run_build_graph()
    assert result["stats"]["nodes"] == 0
    assert result["stats"]["edges"] == 0


def test_contradiction_detection(temp_db):
    """Seed conflicting reports_to triples — should surface as contradiction."""
    db = temp_db
    db.upsert_note("n1", "A", "text", mark_pending=False)
    db.upsert_note("n2", "B", "text", mark_pending=False)

    triples = [
        {
            "subject_text": "Alice", "subject_type": "person",
            "relation": "reports_to",
            "object_text": "Bob", "object_type": "person",
            "confidence": 0.9, "source_quote": "Alice reports to Bob",
            "source_note_id": "n1",
        },
        {
            "subject_text": "Alice", "subject_type": "person",
            "relation": "reports_to",
            "object_text": "Carol", "object_type": "person",
            "confidence": 0.9, "source_quote": "Alice reports to Carol",
            "source_note_id": "n2",
        },
    ]
    db.insert_triples(triples)

    clusters = [
        {"cluster_id": "c0001", "canonical_name": "Alice", "mentions": ["Alice"]},
        {"cluster_id": "c0002", "canonical_name": "Bob", "mentions": ["Bob"]},
        {"cluster_id": "c0003", "canonical_name": "Carol", "mentions": ["Carol"]},
    ]
    db.replace_canonical_map(clusters)

    from brahmastra.concept_graph import run_build_graph
    result = run_build_graph()
    contradictions = result["stats"]["contradictions"]
    assert len(contradictions) == 1
    c = contradictions[0]
    assert c["subject"] == "Alice"
    assert c["relation"] == "reports_to"
    assert set(c["conflicting_values"]) == {"Bob", "Carol"}
