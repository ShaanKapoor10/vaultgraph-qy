"""
The scheduler must treat an errored note as work.

run_extraction already retries status='error' on every run, with a comment
explaining that selecting only 'pending' "stranded them forever". The tick that
decides whether to CALL it counted only 'pending', so that recovery was
unreachable from the one process meant to run unattended.

It happened exactly as described: 53 notes failed with no LLM key configured,
flipped to 'error', and every tick afterwards logged "no changes (heartbeat)"
while sitting on a backlog it existed to clear.
"""
from __future__ import annotations

import pytest

from brahmastra import db, live_sync


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ticks.db"))
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()
    yield
    reset_store()


@pytest.fixture
def ran(monkeypatch):
    """Record whether the tick decided there was work, without doing any."""
    calls: list[bool] = []

    def fake_pipeline(full: bool = False):
        calls.append(full)
        return {"stages": {
            "extract": {"extracted": 0},
            "graph": {"nodes": 0, "contradictions": 0},
        }}

    monkeypatch.setattr("brahmastra.pipeline.run_pipeline", fake_pipeline)
    monkeypatch.setattr(live_sync, "touch_if_idle", lambda *a, **k: {"pinged": False},
                        raising=False)
    return calls


def test_an_errored_note_makes_the_tick_run(ran):
    """The regression. Without this the backlog is never touched again."""
    db.upsert_note("n1", "Failed one", "Sarah reports to Mei.", mark_pending=True)
    db.set_note_status("n1", "error", "No LLM provider available")

    summary = live_sync.tick()

    assert summary["did_work"] is True, "a note in error was treated as nothing to do"
    assert ran == [False]


def test_a_pending_note_still_makes_it_run(ran):
    db.upsert_note("n2", "New one", "Mei works at Veraxion.", mark_pending=True)
    assert live_sync.tick()["did_work"] is True


def test_an_idle_graph_still_does_nothing(ran):
    """The heartbeat must stay cheap when there is genuinely no work."""
    db.upsert_note("n3", "Done one", "Something.", mark_pending=True)
    db.set_note_status("n3", "done")

    assert live_sync.tick()["did_work"] is False
    assert ran == []


def test_opting_out_of_retries_is_honoured(ran, monkeypatch):
    """
    The trigger and the extraction stage must agree. If errors are not retried,
    running the pipeline for them would be a no-op every tick forever.
    """
    monkeypatch.setenv("EXTRACT_RETRY_ERRORS", "0")
    db.upsert_note("n4", "Failed one", "Sarah reports to Mei.", mark_pending=True)
    db.set_note_status("n4", "error", "permanently too large")

    assert live_sync.tick()["did_work"] is False
    assert ran == []


def test_the_tick_reports_the_backlog_it_saw(ran):
    """So a heartbeat line can say why it did or did not run."""
    db.upsert_note("n5", "Failed", "x", mark_pending=True)
    db.set_note_status("n5", "error", "boom")
    db.upsert_note("n6", "Waiting", "y", mark_pending=True)

    summary = live_sync.tick()
    assert summary["pending"] == 1
    assert summary["retryable"] == 1
