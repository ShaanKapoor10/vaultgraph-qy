"""
Suite-wide isolation.

The tests already point BRAHMASTRA_DB at a temp file and clear the Notion and
LLM credentials, because they once ran against the production database and
pushed to the real Notion workspace. Session checkpointing added a third piece
of real state they can reach: the capture queue on disk.

That one is worse than it looks. `run_pipeline` drains the queue, so a test
calling it distils a genuine conversation into a throwaway database and then
DELETES the capture — the queue file is removed once its note is stored, and
the note lives in a temp file that vanishes at teardown. It only failed loudly
here because the suite clears the API keys, leaving no LLM to distil with.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_storage_choice(monkeypatch):
    """
    Decide the backend here, not in backend/.env.

    `stores/__init__.py` loads that file, so whatever a developer happens to
    have configured became the suite's backend. That was harmless while it said
    sqlite; the moment it named the deployed arrangement, tests started
    building a CompositeStore against a real Postgres and a real Neo4j, and
    seven of them failed on the wrong error entirely.

    This is the same class of leak as the ones already guarded here -- the
    suite once ran against the production database and pushed to the real
    Notion workspace. A test that reaches live infrastructure because of a
    local config file is not a test.

    Tests that WANT another backend set it themselves; monkeypatch restores
    these afterwards either way.
    """
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.delenv("NOTE_BACKEND", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True, scope="session")
def _isolate_checkpoint_queue(tmp_path_factory):
    """Redirect the capture queue away from backend/data/checkpoints."""
    import os

    queue = tmp_path_factory.mktemp("checkpoints")
    previous = os.environ.get("BRAHMASTRA_CHECKPOINT_DIR")
    os.environ["BRAHMASTRA_CHECKPOINT_DIR"] = str(queue)
    yield queue
    if previous is None:
        os.environ.pop("BRAHMASTRA_CHECKPOINT_DIR", None)
    else:
        os.environ["BRAHMASTRA_CHECKPOINT_DIR"] = previous
