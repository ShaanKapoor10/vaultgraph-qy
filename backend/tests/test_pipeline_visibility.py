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
