"""
The migration has two phases and they are not owed the same care.

Notes are the system of record: if one does not arrive, information is gone.
Triples and the cached graph are a function of the notes, so a short copy costs
a re-run and nothing else. These tests pin that asymmetry, because the natural
failure mode is treating both as "rows that got copied".
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from brahmastra import migrate_to_neo4j as mig


def _source(notes: list[dict]) -> MagicMock:
    src = MagicMock()
    src.describe.return_value = "sqlite:test#default"
    src.get_notes.return_value = notes
    src.get_all_triples.return_value = [{"subject_text": "A", "relation": "related_to",
                                         "object_text": "B", "source_note_id": "n1"}]
    src.get_entity_clusters.return_value = [{"canonical": "A", "members": ["A"]}]
    src.load_graph.return_value = {"graph": {"nodes": [], "edges": []}, "stats": {}}
    return src


def _target(landed: list[dict]) -> MagicMock:
    dst = MagicMock()
    dst.describe.return_value = "neo4j:test#default"
    dst.get_notes.return_value = landed
    dst.stats.return_value = {"notes": len(landed)}
    return dst


NOTES = [
    {"id": "n1", "title": "One", "content": "c", "extraction_status": "done", "source": "mcp"},
    {"id": "n2", "title": "Two", "content": "c", "extraction_status": "done", "source": "notion"},
]


def test_a_short_note_copy_fails_and_writes_nothing_derived(monkeypatch):
    """
    The dangerous ordering is copy-everything-then-check: a graph rebuilt on a
    partial corpus looks healthy and is confidently wrong. Verification has to
    sit between the two phases.
    """
    dst = _target(landed=[NOTES[0]])          # only one of two arrived
    monkeypatch.setattr(mig, "SQLiteStore", lambda *a, **k: _source(NOTES))
    monkeypatch.setattr(mig, "Neo4jStore", lambda *a, **k: dst)

    with pytest.raises(mig.MigrationIncomplete) as e:
        mig.migrate(apply=True)

    assert "1 of 2" in str(e.value)
    dst.insert_triples.assert_not_called()
    dst.replace_canonical_map.assert_not_called()
    dst.save_graph.assert_not_called()
    dst.close.assert_called_once()


def test_a_complete_note_copy_proceeds_to_the_cache(monkeypatch):
    dst = _target(landed=NOTES)
    monkeypatch.setattr(mig, "SQLiteStore", lambda *a, **k: _source(NOTES))
    monkeypatch.setattr(mig, "Neo4jStore", lambda *a, **k: dst)

    counts = mig.migrate(apply=True)

    assert counts["notes"] == 2 and counts["notes_landed"] == 2
    dst.insert_triples.assert_called_once()
    dst.replace_canonical_map.assert_called_once()
    # Mirror, not union: each note's triples are cleared before re-insert.
    assert dst.delete_triples_for_note.call_count == 2


def test_rebuild_recomputes_instead_of_copying(monkeypatch):
    """
    --rebuild exists for a stale source cache or a changed ontology. It must
    not also copy: writing the old triples and then recomputing them would
    leave whichever finished last, which is not a decision anyone made.
    """
    dst = _target(landed=NOTES)
    monkeypatch.setattr(mig, "SQLiteStore", lambda *a, **k: _source(NOTES))
    monkeypatch.setattr(mig, "Neo4jStore", lambda *a, **k: dst)
    monkeypatch.setattr(mig, "_rebuild_cache_in_target",
                        lambda: {"status": "ok", "failed_stages": []})

    counts = mig.migrate(apply=True, rebuild=True)

    assert counts["rebuild_status"] == "ok"
    dst.insert_triples.assert_not_called()
    dst.replace_canonical_map.assert_not_called()


def test_a_dry_run_writes_nothing_at_all(monkeypatch):
    dst = _target(landed=[])
    monkeypatch.setattr(mig, "SQLiteStore", lambda *a, **k: _source(NOTES))
    monkeypatch.setattr(mig, "Neo4jStore", lambda *a, **k: dst)

    counts = mig.migrate(apply=False)

    assert counts["notes"] == 2
    dst.upsert_note.assert_not_called()
    dst.insert_triples.assert_not_called()


def test_rebuild_restores_the_backend_it_switched(monkeypatch):
    """
    The rebuild repoints the process at Neo4j to recompute there. Leaving it
    repointed would silently redirect every later call in the same process --
    including, in a test run, the ones writing to a temp database.
    """
    import os

    from brahmastra import db

    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    reset_calls = []
    monkeypatch.setattr(db, "reset_store", lambda: reset_calls.append(1))

    seen = {}

    def fake_pipeline(full=False):
        seen["backend_during"] = os.environ.get("GRAPH_BACKEND")
        return {"status": "ok", "failed_stages": []}

    import brahmastra.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "run_pipeline", fake_pipeline)

    mig._rebuild_cache_in_target()

    assert seen["backend_during"] == "neo4j", "rebuild must target Neo4j"
    assert os.environ["GRAPH_BACKEND"] == "sqlite", "must restore the caller's backend"
    assert len(reset_calls) == 2, "the store cache must be dropped on both switches"
