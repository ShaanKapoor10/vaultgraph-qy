"""
Tests for the GraphStore contract.

Covers the SQLite backend only — the Neo4j backend needs live credentials, so
its parity with these results is verified manually against Aura. What is
locked in here is the contract itself: any backend must return these shapes,
which is what lets rag.py stay backend-agnostic.
"""

from __future__ import annotations

import pytest

from brahmastra import db
from brahmastra.stores import backend_name, get_store, reset_store
from brahmastra.stores.base import GraphStore
from brahmastra.stores.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Fresh DB per test, and a store cache that cannot leak between them."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    reset_store()
    db.init_db()
    yield
    reset_store()


GRAPH = {
    "nodes": [
        {"id": "Sarah", "label": "Sarah", "type": "person", "pagerank": 0.4, "cluster": 1},
        {"id": "Mei", "label": "Mei", "type": "person", "pagerank": 0.3, "cluster": 1},
        {"id": "Apollo", "label": "Apollo", "type": "project", "pagerank": 0.2, "cluster": 2},
    ],
    "edges": [
        {"source": "Sarah", "target": "Mei", "relation": "reports_to",
         "source_quote": "Sarah reports to Mei.", "note_id": "n1", "confidence": 0.9},
        {"source": "Sarah", "target": "Apollo", "relation": "works_on",
         "source_quote": "Sarah owns Apollo.", "note_id": "n1", "confidence": 0.7},
        {"source": "Mei", "target": "Apollo", "relation": "owns",
         "source_quote": "Mei owns Apollo.", "note_id": "n2", "confidence": 0.5},
    ],
}
STATS = {"nodes": 3, "edges": 3, "central_entities": [], "concept_clusters": []}


def test_sqlite_store_satisfies_the_contract():
    assert isinstance(get_store(), GraphStore)
    assert isinstance(get_store(), SQLiteStore)
    assert backend_name() == "sqlite"


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "mongo")
    reset_store()
    with pytest.raises(ValueError, match="Unknown GRAPH_BACKEND"):
        get_store()


def test_graph_round_trips():
    db.cache_graph(GRAPH, STATS)
    loaded = db.get_cached_graph()
    assert len(loaded["graph"]["nodes"]) == 3
    assert len(loaded["graph"]["edges"]) == 3
    assert loaded["stats"]["nodes"] == 3


def test_get_entities_returns_nodes_without_edges():
    db.cache_graph(GRAPH, STATS)
    ents = db.get_entities()
    assert {e["id"] for e in ents} == {"Sarah", "Mei", "Apollo"}
    # Nodes carry ranking data but never edge data — that is the whole point
    # of the separate call, so local search need not load the edge list.
    assert all("source" not in e and "target" not in e for e in ents)


def test_neighbourhood_returns_only_touching_facts():
    db.cache_graph(GRAPH, STATS)
    facts = db.neighbourhood({"Sarah"})
    texts = {f["text"] for f in facts}
    assert "Sarah reports_to Mei" in texts
    assert "Sarah works_on Apollo" in texts
    # Mei->Apollo does not touch Sarah and must not leak in.
    assert "Mei owns Apollo" not in texts


def test_neighbourhood_is_confidence_ranked_and_carries_citations():
    db.cache_graph(GRAPH, STATS)
    facts = db.neighbourhood({"Sarah"})
    confs = [f["confidence"] for f in facts]
    assert confs == sorted(confs, reverse=True)
    assert all(f["note_id"] for f in facts), "facts must stay citable"
    assert facts[0]["quote"]


def test_neighbourhood_respects_limit_and_empty_input():
    db.cache_graph(GRAPH, STATS)
    assert len(db.neighbourhood({"Sarah", "Mei"}, limit=1)) == 1
    assert db.neighbourhood(set()) == []


def test_neighbourhood_on_empty_graph():
    assert db.neighbourhood({"Sarah"}) == []
    assert db.get_entities() == []


def test_delete_note_removes_its_triples():
    db.upsert_note("n1", "T", "C")
    db.insert_triples([{
        "subject_text": "Sarah", "relation": "reports_to", "object_text": "Mei",
        "source_note_id": "n1",
    }])
    assert len(db.get_all_triples()) == 1
    db.delete_note("n1")
    assert db.get_note("n1") is None
    assert db.get_all_triples() == []


def test_set_note_status_rejects_invalid_status():
    db.upsert_note("n1", "T", "C")
    with pytest.raises(ValueError):
        db.set_note_status("n1", "nonsense")
