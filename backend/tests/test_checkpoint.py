"""
Tests for session checkpointing.

The feature exists because deliberate note-taking is lossy at exactly the wrong
moment, so these tests are mostly about the failure modes: never storing the
same turns twice, never losing a capture because the LLM was down, and never
letting tool traffic into the graph.
"""
from __future__ import annotations

import importlib
import json
import pytest
from unittest.mock import patch


@pytest.fixture
def cp(monkeypatch, tmp_path):
    """checkpoint module with its queue and database pointed at tmp_path."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "cp.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    from brahmastra import checkpoint
    importlib.reload(checkpoint)
    monkeypatch.setenv("BRAHMASTRA_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    return checkpoint


def _transcript(tmp_path, rows: list[dict]) -> str:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return str(path)


def _msg(kind: str, content) -> dict:
    return {"type": kind, "message": {"role": kind, "content": content}}


def test_transcript_keeps_prose_and_drops_machinery(cp, tmp_path):
    """
    Only what a human said or a model wrote is knowledge. Tool calls and their
    results are mechanics — feeding them to the extractor fills the graph with
    entities like "Bash" and "file_path".
    """
    path = _transcript(tmp_path, [
        {"type": "queue-operation", "operation": "enqueue"},
        _msg("user", [{"type": "text", "text": "the pipeline is slow"}]),
        _msg("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
        _msg("user", [{"type": "tool_result", "content": "file listing here"}]),
        _msg("assistant", [{"type": "text", "text": "Groq's daily cap was spent."}]),
        {**_msg("assistant", [{"type": "text", "text": "subagent noise"}]), "isSidechain": True},
        _msg("user", [{"type": "text", "text": "<system-reminder>ignore me</system-reminder>"}]),
    ])

    convo, lines = cp.read_transcript(path, 0)

    assert "the pipeline is slow" in convo
    assert "Groq's daily cap was spent." in convo
    assert "file listing here" not in convo, "tool results must not reach the graph"
    assert "Bash" not in convo
    assert "subagent noise" not in convo, "sidechain is not the main conversation"
    assert "ignore me" not in convo, "injected scaffolding is not conversation"
    assert lines == 7


def test_second_capture_only_sees_new_turns(cp, tmp_path):
    """
    Long sessions compact repeatedly. Without an offset each checkpoint would
    re-store everything before it, so the graph would fill with near-duplicate
    notes of the same conversation.
    """
    rows = [_msg("user", [{"type": "text", "text": "first turn. " * 40}])]
    path = _transcript(tmp_path, rows)
    payload = {"session_id": "s1", "transcript_path": path}

    first = cp.capture(payload)
    assert first is not None

    # Nothing new since — must not queue the same turns again.
    assert cp.capture(payload) is None

    rows.append(_msg("assistant", [{"type": "text", "text": "second turn. " * 40}]))
    _transcript(tmp_path, rows)

    second = cp.capture(payload)
    assert second is not None
    convo = json.loads(second.read_text(encoding="utf-8"))["conversation"]
    assert "second turn." in convo
    assert "first turn." not in convo, "already checkpointed turns were re-queued"


def test_capture_survives_a_dead_llm_and_drains_later(cp, tmp_path):
    """
    Capture and distillation are split precisely so an LLM outage delays a
    checkpoint instead of losing it. The queue file is the durable half.
    """
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "we chose Neo4j over SQLite. " * 20}]),
    ])
    assert cp.capture({"session_id": "s2", "transcript_path": path}) is not None
    assert cp.pending_count() == 1

    with patch.object(cp, "_distil", side_effect=RuntimeError("quota exhausted")):
        result = cp.drain()
    assert result["stored"] == 0
    assert cp.pending_count() == 1, "a failed drain must not discard the capture"

    with patch.object(cp, "_distil", return_value="# Storage Decision\nBrahmastra uses Neo4j."):
        result = cp.drain()
    assert result["stored"] == 1
    assert cp.pending_count() == 0

    from brahmastra import db
    titles = [n["title"] for n in db.get_notes()]
    assert any("Storage Decision" in t for t in titles)
    stored = [n for n in db.get_notes() if "Storage Decision" in n["title"]][0]
    assert stored["extraction_status"] == "pending", "must be queued for extraction"


def test_distiller_declining_is_not_an_error(cp, tmp_path):
    """A stretch with nothing durable in it should leave no note behind."""
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "thanks, looks good. " * 30}]),
    ])
    cp.capture({"session_id": "s3", "transcript_path": path})

    with patch.object(cp, "_distil", return_value=None):
        result = cp.drain()

    from brahmastra import db
    assert result["skipped"] == 1
    assert result["stored"] == 0
    assert db.get_notes() == []
    assert cp.pending_count() == 0, "a declined capture must not be retried forever"


def test_hook_never_fails_the_session(cp, tmp_path, capsys):
    """
    A hook that raises interrupts the user. Garbage on stdin, a missing
    transcript and an exploding capture must all exit 0 — and say why in the log.
    """
    import io
    import sys

    monkey = lambda text: setattr(sys, "stdin", io.StringIO(text))

    monkey("not json at all")
    assert cp.main([]) == 0

    monkey(json.dumps({"session_id": "s4"}))  # no transcript_path
    assert cp.main([]) == 0

    monkey(json.dumps({"session_id": "s5", "transcript_path": str(tmp_path / "gone.jsonl")}))
    assert cp.main([]) == 0

    log = (cp.queue_dir() / "checkpoint.log").read_text(encoding="utf-8")
    assert "could not parse hook payload" in log
    assert "nothing new to checkpoint" in log


def test_queue_location_is_resolved_per_call(cp, tmp_path, monkeypatch):
    """
    Regression: the queue path was a module constant fixed at import, so
    `run_pipeline` inside a test drained the REAL queue — distilling a genuine
    conversation into a temp database and deleting the capture. Same failure as
    the DB_PATH bug that once pointed the suite at the production database.
    """
    elsewhere = tmp_path / "moved"
    monkeypatch.setenv("BRAHMASTRA_CHECKPOINT_DIR", str(elsewhere))
    assert cp.queue_dir() == elsewhere

    monkeypatch.delenv("BRAHMASTRA_CHECKPOINT_DIR")
    assert cp.queue_dir() == cp._BACKEND / "data" / "checkpoints"
