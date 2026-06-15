"""
Tests for the DB layer.
Uses a temporary SQLite file so tests never touch backend/data/.
"""
from __future__ import annotations

import os
import tempfile
import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Redirect the DB to a fresh temp file for every test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("BRAHMASTRA_DB", str(db_file))
    # Reload the module so _DB_PATH picks up the new env var
    import importlib
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    yield db_mod


def test_upsert_and_get_note(temp_db):
    db = temp_db
    db.upsert_note("n1", "My Note", "Hello world", mark_pending=True)
    note = db.get_note("n1")
    assert note is not None
    assert note["title"] == "My Note"
    assert note["extraction_status"] == "pending"


def test_upsert_idempotent(temp_db):
    db = temp_db
    db.upsert_note("n1", "Title", "Content A", mark_pending=True)
    db.upsert_note("n1", "Title", "Content B", mark_pending=False)
    notes = db.get_notes()
    assert len(notes) == 1
    assert notes[0]["content"] == "Content B"


def test_mark_note_done(temp_db):
    db = temp_db
    db.upsert_note("n2", "T", "C", mark_pending=True)
    db.mark_note_done("n2")
    assert db.get_note("n2")["extraction_status"] == "done"


def test_mark_note_error(temp_db):
    db = temp_db
    db.upsert_note("n3", "T", "C", mark_pending=True)
    db.mark_note_error("n3")
    assert db.get_note("n3")["extraction_status"] == "error"


def test_insert_and_get_triples(temp_db):
    db = temp_db
    db.upsert_note("n4", "T", "C", mark_pending=True)
    triples = [
        {
            "subject_text": "Alice",
            "subject_type": "person",
            "relation": "reports_to",
            "object_text": "Bob",
            "object_type": "person",
            "confidence": 0.9,
            "source_quote": "Alice reports to Bob",
            "source_note_id": "n4",
        }
    ]
    db.insert_triples(triples)
    all_t = db.get_all_triples()
    assert len(all_t) == 1
    assert all_t[0]["subject_text"] == "Alice"


def test_delete_triples_for_note(temp_db):
    db = temp_db
    db.upsert_note("n5", "T", "C", mark_pending=True)
    db.insert_triples([
        {
            "subject_text": "X", "subject_type": "concept",
            "relation": "related_to",
            "object_text": "Y", "object_type": "concept",
            "confidence": 1.0,
            "source_note_id": "n5",
        }
    ])
    db.delete_triples_for_note("n5")
    assert db.get_all_triples() == []


def test_canonical_map_roundtrip(temp_db):
    db = temp_db
    clusters = [
        {"cluster_id": "c0001", "canonical_name": "Alice Smith", "mentions": ["Alice Smith", "Alice", "A. Smith"]},
        {"cluster_id": "c0002", "canonical_name": "Acme Corp", "mentions": ["Acme Corp", "Acme"]},
    ]
    db.replace_canonical_map(clusters)
    cmap = db.get_canonical_map()
    assert cmap["Alice"] == "Alice Smith"
    assert cmap["Acme"] == "Acme Corp"


def test_graph_cache_roundtrip(temp_db):
    db = temp_db
    graph = {"nodes": [{"id": "A", "label": "A"}], "edges": []}
    stats = {"nodes": 1, "edges": 0}
    db.cache_graph(graph, stats)
    cached = db.get_cached_graph()
    assert cached is not None
    assert cached["graph"]["nodes"][0]["id"] == "A"


def test_db_stats(temp_db):
    db = temp_db
    db.upsert_note("n6", "T", "C", mark_pending=True)
    db.upsert_note("n7", "T2", "C2", mark_pending=False)
    stats = db.get_db_stats()
    assert stats["notes_total"] == 2
    assert stats["notes_pending"] == 1
    assert stats["graph_cached"] is False
