"""
A run that could not load the embedding model must not reshape the graph.

Found 2026-09-25: Windows Smart App Control intermittently blocked a scipy DLL
in the venv, the embedding model failed to load, and resolution would have
rewritten the canonical map without its 85 meaning-based merges.
"""
from __future__ import annotations

import pytest

import brahmastra.entity_resolution as er

TRIPLES = [{"subject_text": "SQLite", "subject_type": "tool", "relation": "uses",
            "object_text": "SQLite database", "object_type": "tool"}]


@pytest.fixture
def wired(monkeypatch):
    written = []
    monkeypatch.setattr(er.db, "get_all_triples", lambda: TRIPLES)
    monkeypatch.setattr(er.db, "replace_canonical_map", lambda c: written.append(c))
    monkeypatch.setattr(er, "_get_embedder", lambda: None)      # the block
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "1")
    return written


def test_a_transient_failure_keeps_the_previous_map(wired, monkeypatch):
    monkeypatch.setattr(er.db, "get_canonical_map", lambda: {"sqlite": "SQLite"})
    report = er.run_resolution()
    assert wired == []
    assert report["kept_previous_map"] is True
    assert "unavailable" in report["embedding_error"]


def test_turning_embeddings_off_on_purpose_still_writes(wired, monkeypatch):
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "0")
    monkeypatch.setattr(er.db, "get_canonical_map", lambda: {"sqlite": "SQLite"})
    report = er.run_resolution()
    assert len(wired) == 1 and report["kept_previous_map"] is False


def test_a_first_run_has_nothing_to_keep_and_writes(wired, monkeypatch):
    monkeypatch.setattr(er.db, "get_canonical_map", lambda: {})
    er.run_resolution()
    assert len(wired) == 1
