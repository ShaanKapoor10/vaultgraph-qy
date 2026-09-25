"""
Incremental resolution gives EXACTLY what a full run gives.

The next run compares only pairs involving a new mention and carries the rest
forward (resolution_cache.py). That is only worth having if it is invisible:
same clusters, same merge edges, as resolving everything from scratch.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

import brahmastra.entity_resolution as er
from brahmastra import db

NAMES_A = ["Sarah Chen", "sarah chen", "Neo4j Aura", "Neo4j Aura instance", "SQLite",
           "SQLite database", "live sync watcher", "live_sync watcher", "pipeline.py",
           "backend/brahmastra/pipeline.py", "Groq key", "Groq API key",
           "entity resolution", "Brahmastra", "Brahmastra v3", "MCP",
           "Model Context Protocol", "run_pipeline", "run_pipeline function"]
NAMES_B = ["sarah Chen", "SQLite db", "Groq keys", "entity_resolution",
           "brahmastra", "pipeline stage", "global retrieval mode", "local retrieval mode"]


class FakeModel:
    """Deterministic, word-based vectors: names sharing words land close."""

    def encode(self, texts, normalize_embeddings=True):
        out = []
        for t in texts:
            v = np.zeros(32, dtype="float32")
            for w in er._spelling_words(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 32] += 1.0
            n = np.linalg.norm(v) or 1.0
            out.append(v / n)
        return np.array(out)


def _triples(names):
    return [{"subject_text": n, "subject_type": "concept", "relation": "related_to",
             "object_text": "anchor", "object_type": "concept", "source_note_id": "n1",
             "source_quote": n} for n in names]


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    monkeypatch.setenv("ENTITY_CONFIRM", "0")
    monkeypatch.delenv("RESOLUTION_INCREMENTAL", raising=False)
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()
    monkeypatch.setattr(er, "_get_embedder", lambda: FakeModel())
    state = {"triples": []}
    monkeypatch.setattr(er.db, "get_all_triples", lambda: state["triples"])
    yield state
    reset_store()


def _shape(result):
    clusters = sorted(tuple(c["mentions"]) for c in result["details"]["clusters"])
    edges = sorted((min(e["a"], e["b"]), max(e["a"], e["b"]))
                   for e in result["details"]["merge_edges"])
    return clusters, edges


def _full(state, names):
    from brahmastra.resolution_cache import ResolutionCache
    ResolutionCache().clear()
    state["triples"] = _triples(names)
    return er.run_resolution()


def test_adding_mentions_incrementally_equals_a_full_run(world):
    first = _full(world, NAMES_A)
    assert first["incremental"] == "full"

    world["triples"] = _triples(NAMES_A + NAMES_B)
    incremental = er.run_resolution()
    assert incremental["incremental"]["new_mentions"] == len(set(NAMES_B))

    full = _full(world, NAMES_A + NAMES_B)
    assert _shape(incremental) == _shape(full)


def test_removing_mentions_incrementally_equals_a_full_run(world):
    _full(world, NAMES_A + NAMES_B)
    kept = [n for n in NAMES_A + NAMES_B if n not in ("SQLite", "Brahmastra", "MCP")]
    world["triples"] = _triples(kept)
    incremental = er.run_resolution()
    assert incremental["incremental"]["new_mentions"] == 0
    assert _shape(incremental) == _shape(_full(world, kept))


def test_an_unchanged_corpus_compares_nothing(world, monkeypatch):
    _full(world, NAMES_A)
    calls = []
    real = er._heuristic_sim
    monkeypatch.setattr(er, "_heuristic_sim", lambda a, b: calls.append(1) or real(a, b))
    monkeypatch.setattr(er, "_embedding_sim", lambda m, only=None: calls.append("emb") or {})
    world["triples"] = _triples(NAMES_A)
    again = er.run_resolution()
    assert calls == []
    assert _shape(again) == _shape(_full(world, NAMES_A))


def test_a_changed_key_forces_a_full_run(world, monkeypatch):
    _full(world, NAMES_A)
    monkeypatch.setattr(er, "MERGE_THRESHOLD", er.MERGE_THRESHOLD + 0.01)
    world["triples"] = _triples(NAMES_A + NAMES_B)
    assert er.run_resolution()["incremental"] == "full"


def test_it_can_be_switched_off(world, monkeypatch):
    _full(world, NAMES_A)
    monkeypatch.setenv("RESOLUTION_INCREMENTAL", "0")
    world["triples"] = _triples(NAMES_A)
    assert er.run_resolution()["incremental"] == "full"


def test_a_run_whose_embeddings_failed_is_not_carried_forward(world, monkeypatch):
    from brahmastra.resolution_cache import ResolutionCache
    ResolutionCache().clear()
    monkeypatch.setattr(er, "_get_embedder", lambda: None)
    world["triples"] = _triples(NAMES_A)
    er.run_resolution()
    monkeypatch.setattr(er, "_get_embedder", lambda: FakeModel())
    assert er.run_resolution()["incremental"] == "full"
