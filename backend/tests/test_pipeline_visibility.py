"""
"Has the pipeline run?" must be answerable by anyone, at any time.

The API tracked its own runs in a module-level dict. That answered the question
only for runs THIS process started, so a run kicked off by the scheduler, the
CLI or MCP reported "idle" while it was genuinely in flight, a restart erased
the answer, and nothing anywhere recorded that a run had ever finished.

The dashboard could therefore show a spinner and then nothing at all, leaving
"I hope it ran" as the only conclusion available to the person watching.
"""
from __future__ import annotations

import time

import pytest

from brahmastra import pipeline


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "vis.db"))
    from brahmastra.stores import reset_store
    reset_store()
    yield
    reset_store()


FINISHED = {
    "started_at": "2026-08-31T10:00:00+00:00",
    "finished_at": "2026-08-31T10:02:00+00:00",
    "mode": "incremental",
    "status": "partial",
    "failed_stages": ["extract"],
    "stages": {
        "extract": {"extracted": 3, "triples_added": 11, "errors": ["429"]},
        "graph": {"nodes": 519, "edges": 663, "contradictions": 3},
    },
}


# ---------------------------------------------------------------------------
# Nothing has run
# ---------------------------------------------------------------------------

def test_a_store_with_no_history_says_so_rather_than_guessing():
    state = pipeline.run_state()
    assert state["running"] is False
    assert state["last"] is None


# ---------------------------------------------------------------------------
# Something ran
# ---------------------------------------------------------------------------

def test_a_finished_run_is_recorded_with_its_verdict():
    pipeline._write_record(FINISHED)
    last = pipeline.last_run()

    assert last["status"] == "partial"
    assert last["failed_stages"] == ["extract"]
    assert last["extracted"] == 3
    assert last["nodes"] == 519
    assert last["finished_at"] == "2026-08-31T10:02:00+00:00"


def test_the_record_survives_a_restart():
    """
    The whole point of a file. An in-memory answer is erased by the restart
    that most often prompts the question.
    """
    pipeline._write_record(FINISHED)
    import importlib
    importlib.reload(pipeline)
    assert pipeline.last_run()["status"] == "partial"


def test_the_record_does_not_carry_per_note_error_text():
    """It is read on every poll; the full result holds unbounded error strings."""
    pipeline._write_record(FINISHED)
    assert "errors" not in pipeline.last_run()


# ---------------------------------------------------------------------------
# Something is running, started by anyone
# ---------------------------------------------------------------------------

def test_a_run_started_by_another_process_is_visible():
    """
    The regression that made this necessary. /pipeline/status reported "idle"
    while the scheduler held the lock, because it only knew its own runs.
    """
    assert pipeline._acquire_lock() is True      # stands in for the scheduler
    try:
        state = pipeline.run_state()
        assert state["running"] is True
        assert state["active"]["stale"] is False
    finally:
        pipeline._release_lock()

    assert pipeline.run_state()["running"] is False


def test_a_crashed_run_is_not_reported_as_still_running(monkeypatch):
    """
    A stale lock read as "running" is how a dashboard spins forever on a run
    that died a quarter of an hour ago. Age, never existence.
    """
    pipeline._acquire_lock()
    lock = pipeline._lock_path()
    old = time.time() - (pipeline._LOCK_STALE_SECS + 60)
    import os
    os.utime(lock, (old, old))

    state = pipeline.run_state()
    assert state["running"] is False
    assert state["active"]["stale"] is True
    lock.unlink()


def test_a_run_in_progress_does_not_erase_the_previous_verdict():
    """Someone watching a new run still needs to see how the last one went."""
    pipeline._write_record(FINISHED)
    pipeline._acquire_lock()
    try:
        state = pipeline.run_state()
        assert state["running"] is True
        assert state["last"]["status"] == "partial"
    finally:
        pipeline._release_lock()


# ---------------------------------------------------------------------------
# Not being the reason a run fails
# ---------------------------------------------------------------------------

def test_an_unwritable_record_does_not_fail_the_run(monkeypatch):
    """Reporting is not the work. A run must not die because it could not
    write down that it happened."""
    from pathlib import Path

    def refuse(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", refuse)
    pipeline._write_record(FINISHED)      # must not raise


def test_a_corrupt_record_reads_as_no_record():
    pipeline._record_path().parent.mkdir(parents=True, exist_ok=True)
    pipeline._record_path().write_text("{not json", encoding="utf-8")
    assert pipeline.last_run() is None


# ---------------------------------------------------------------------------
# Whether the graph is BEHIND the notes, which is a different question
# ---------------------------------------------------------------------------
#
# "When did the pipeline last run" and "is the graph current" are not the same,
# and only the first had an answer. Extraction is reachable without a full run:
# the MCP add_note extracts inline, POST /pipeline/extract calls run_extraction
# directly, and a script can call extract_note itself. Triples then land while
# resolve, build-graph and the cache do not.
#
# The status reported a clean run and looked healthy while the graph was
# missing everything stored since -- and /ask and /graph read that cache, so
# they answered confidently from a graph that predated the note just stored.

def test_a_store_with_nothing_extracted_is_not_stale():
    assert pipeline.run_state()["stale"] is False


def test_new_triples_mark_the_graph_as_behind():
    pipeline.mark_dirty("extracted note-1")
    state = pipeline.run_state()

    assert state["stale"] is True
    assert "note-1" in state["dirty_since"]["reason"]


def test_a_clean_run_from_yesterday_does_not_make_a_stale_graph_look_current():
    """The exact case that prompted this: a good run, then notes added through
    the MCP tool, and a chip still reporting 'ran 4h ago, ok'."""
    pipeline._write_record(FINISHED)
    pipeline.mark_dirty("extracted note-2")

    state = pipeline.run_state()
    assert state["last"]["status"] == "partial", "the run record is still there"
    assert state["stale"] is True, "but the graph is behind the notes"


def test_rebuilding_the_graph_makes_it_current_again():
    pipeline.mark_dirty("extracted note-3")
    pipeline.clear_dirty(time.time())
    assert pipeline.run_state()["stale"] is False


def test_work_that_arrived_during_the_run_stays_marked():
    """
    A note stored WHILE a run is in progress is genuinely not in the graph that
    run produced. Clearing unconditionally at the end would erase a true
    staleness signal and leave that note invisible until something else
    happened to trigger a rebuild.
    """
    began = time.time()
    time.sleep(0.01)
    pipeline.mark_dirty("extracted mid-run")

    pipeline.clear_dirty(began)
    assert pipeline.run_state()["stale"] is True


def test_marking_staleness_never_raises(monkeypatch):
    """Reporting is not the work: a failure here must not fail an extraction."""
    monkeypatch.setattr(pipeline, "_dirty_path",
                        lambda: (_ for _ in ()).throw(OSError("nope")))
    pipeline.mark_dirty("boom")
    assert pipeline.dirty_since() is None


def test_extracting_a_note_marks_the_graph_behind(monkeypatch, tmp_path):
    """
    The chokepoint, and the reason the flag lives in extract_note rather than
    in the pipeline: every path to new triples goes through this function --
    run_pipeline, POST /pipeline/extract, the MCP add_note that extracts
    inline, and any script calling it directly. Marking it in run_pipeline
    would have missed every path that prompted this.
    """
    from brahmastra import db, extraction

    db.init_db()
    db.upsert_note("n1", "A note", "Sarah owns the release.", mark_pending=True)

    monkeypatch.setattr(
        extraction, "_extract_with_llm",
        lambda title, content: [{
            "subject_text": "Sarah", "subject_type": "Person",
            "relation": "employed_by",
            "object_text": "Veraxion", "object_type": "Organization",
            "confidence": 0.9,
        }],
    )

    assert pipeline.run_state()["stale"] is False
    result = extraction.extract_note(db.get_note("n1"))

    assert result["triples_added"] >= 1
    assert pipeline.run_state()["stale"] is True, (
        "extraction added triples and nothing recorded that the graph is behind"
    )


def test_a_note_that_yields_no_triples_does_not_mark_it_behind(monkeypatch):
    """Nothing changed, so nothing is out of date."""
    from brahmastra import db, extraction

    db.init_db()
    db.upsert_note("n2", "Empty", "...", mark_pending=True)
    monkeypatch.setattr(extraction, "_extract_with_llm", lambda title, content: [])

    extraction.extract_note(db.get_note("n2"))
    assert pipeline.run_state()["stale"] is False


# ---------------------------------------------------------------------------
# ... asked of the STORE, which host and container actually share
# ---------------------------------------------------------------------------
#
# The file stamp alone is blind in the one deployment that matters. data_dir()
# resolves to backend/data for a host process and /data inside a container, so
# a stamp written by the MCP server's inline extraction is invisible to the
# dashboard that has to report it, and vice versa. The cached graph lives in
# the store, which both halves genuinely share.

def test_a_graph_built_from_the_current_triples_is_not_behind(monkeypatch):
    from brahmastra import db
    monkeypatch.setattr(db, "get_cached_graph",
                        lambda: {"built_at": "x", "stats": {"triples_total": 764}})
    monkeypatch.setattr(db, "get_db_stats", lambda: {"triples_total": 764})
    assert pipeline.graph_is_behind() is None


def test_triples_added_after_the_build_make_the_graph_behind(monkeypatch):
    from brahmastra import db
    monkeypatch.setattr(db, "get_cached_graph",
                        lambda: {"built_at": "x", "stats": {"triples_total": 691}})
    monkeypatch.setattr(db, "get_db_stats", lambda: {"triples_total": 764})

    behind = pipeline.graph_is_behind()
    assert behind["built_from_triples"] == 691
    assert behind["triples_now"] == 764
    assert pipeline.run_state()["stale"] is True


def test_a_cache_written_before_the_count_existed_defers_to_the_stamp(monkeypatch):
    """Backwards compatible: an older cache cannot answer, so it says nothing
    rather than guessing that the graph is fine."""
    from brahmastra import db
    monkeypatch.setattr(db, "get_cached_graph",
                        lambda: {"built_at": "x", "stats": {"nodes": 554}})
    monkeypatch.setattr(db, "get_db_stats", lambda: {"triples_total": 764})
    assert pipeline.graph_is_behind() is None


def test_an_unreachable_store_does_not_fail_the_status(monkeypatch):
    """A status endpoint that raises is worse than one that admits ignorance."""
    from brahmastra import db

    def boom():
        raise RuntimeError("neo4j unreachable")

    monkeypatch.setattr(db, "get_cached_graph", boom)
    assert pipeline.graph_is_behind() is None
    assert pipeline.run_state()["stale"] is False
