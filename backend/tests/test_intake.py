"""
Tests for note intake: which store a process writes to, and where a note came from.

Five writers reach the notes table — Notion sync, MCP, the REST route the
dashboard posts to, the CLI, and the checkpoint drain. There is deliberately no
staging area between them and the store: the "pending queue" is a column value,
and the same clients that write also read, so buffering writes would only mean
an agent could not see the note it just added.

What that design needs instead is agreement about WHERE the store is, and a
record of WHO wrote each note.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "intake.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


# ---------------------------------------------------------------------------
# Where the store is
# ---------------------------------------------------------------------------

def test_backend_is_resolved_from_the_environment_at_call_time(monkeypatch):
    """
    The processes that WRITE notes are not the process that runs the pipeline:
    the MCP server, uvicorn, the CLI and the session hooks all start
    independently. When they disagreed about GRAPH_BACKEND, an MCP-added note
    landed in SQLite and a pipeline run against Neo4j never saw it.
    """
    from brahmastra.stores import backend_name

    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    assert backend_name() == "neo4j"

    monkeypatch.setenv("GRAPH_BACKEND", "  SQLite  ")
    assert backend_name() == "sqlite", "must normalise case and whitespace"

    monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    assert backend_name() == "sqlite", "unset falls back to the local default"


def test_store_module_reads_dotenv():
    """
    Regression: GRAPH_BACKEND in backend/.env was silently ignored, because the
    module that answers "which store?" never read the config file — it resolved
    the name before anything had loaded it. Putting the setting in .env was a
    no-op, which is the worst kind of configuration bug.
    """
    import inspect

    from brahmastra import stores

    src = inspect.getsource(stores)
    assert "load_dotenv" in src, (
        "stores/__init__.py must load .env itself; every other process relies "
        "on it to agree where the database is"
    )


# ---------------------------------------------------------------------------
# Where a note came from
# ---------------------------------------------------------------------------

def test_source_defaults_to_unknown_not_a_guess(store):
    store.upsert_note("n1", "T", "C")
    assert store.get_note("n1")["source"] == "unknown"


def test_each_writer_can_declare_itself(store):
    for i, src in enumerate(("notion", "mcp", "ui", "cli", "checkpoint")):
        store.upsert_note(f"n{i}", "T", "C", source=src)
        assert store.get_note(f"n{i}")["source"] == src


def test_first_writer_wins(store):
    """
    A Notion sync re-upserts every page on every run. If the later write
    relabelled the note, provenance would decay to whichever job ran last.
    """
    store.upsert_note("n9", "T", "C", source="mcp")
    store.upsert_note("n9", "T", "C v2")            # no opinion
    assert store.get_note("n9")["source"] == "mcp"

    store.upsert_note("n9", "T", "C v3", source="notion")
    assert store.get_note("n9")["source"] == "mcp", "origin must not be rewritten"


def test_an_unknown_origin_can_still_be_upgraded(store):
    """
    "First writer wins" must not freeze a row at 'unknown'. The 51 notes that
    predate the column are all unknown, and a later write that does know where
    it came from should be allowed to say so.
    """
    store.upsert_note("n10", "T", "C")                     # -> unknown
    assert store.get_note("n10")["source"] == "unknown"

    store.upsert_note("n10", "T", "C", source="mcp")       # now known
    assert store.get_note("n10")["source"] == "mcp"


def test_backfill_infers_only_what_the_data_proves(monkeypatch, tmp_path):
    """
    Existing rows predate the column. A Notion-shaped id IS a Notion page id and
    the drain sets its own prefix, so those two are certain. An 8-char id could
    be mcp, ui or cli — guessing there would write a provenance that is simply
    wrong, which is worse than admitting ignorance.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE notes (
            id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL, content TEXT NOT NULL,
            last_edited TEXT, last_synced TEXT,
            extraction_status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (workspace_id, id)
        );
        """
    )
    conn.executemany(
        "INSERT INTO notes (id, title, content) VALUES (?, ?, ?)",
        [("38a976bb-9093-810f-8161-d2487b63b98e", "From Notion", "x"),
         ("checkpoint-1786543733-3989f74b", "A checkpoint", "x"),
         ("1cc32f87", "Added by an agent", "x")],
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("BRAHMASTRA_DB", str(path))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    got = {n["id"]: n["source"] for n in db_mod.get_notes()}
    assert got["38a976bb-9093-810f-8161-d2487b63b98e"] == "notion"
    assert got["checkpoint-1786543733-3989f74b"] == "checkpoint"
    assert got["1cc32f87"] == "unknown", "must not guess between mcp, ui and cli"
