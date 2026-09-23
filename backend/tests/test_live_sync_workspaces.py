"""
The watcher keeps EVERY workspace current -- and never lets one read another's Notion.

Found by measurement: `office`, `work` and `transcripts-demo` each held a note
at `pending` with zero triples and no error. The scheduler ran one workspace
-- the process's BRAHMASTRA_WORKSPACE, which compose pins to `default` -- so
nothing else was ever extracted. That included the note a transcript produced,
so a meeting ingested into its own workspace never reached any graph.

The dangerous half is Notion. `run_sync` falls back to the global
NOTION_DATABASE_ID for a workspace that names no source of its own, so a
naive loop would pull the personal graph's pages into the office graph.
"""
from __future__ import annotations

import pytest

from brahmastra import live_sync


@pytest.fixture
def ticks(monkeypatch):
    """Record which workspace each tick ran in, and whether it pulled Notion."""
    from brahmastra.workspace import current_workspace

    seen: list[tuple[str, bool]] = []

    def fake_tick(pull_notion: bool = True):
        seen.append((current_workspace(), pull_notion))
        return {"did_work": False, "synced": 0}

    monkeypatch.setattr(live_sync, "tick", fake_tick)
    monkeypatch.setattr(live_sync.db, "list_workspaces", lambda: [
        {"id": "default"}, {"id": "office"}, {"id": "transcripts-demo"}])
    monkeypatch.setattr(live_sync.db, "get_workspace", lambda wid: {
        "office": {"id": "office", "notion_database_id": "office-db"},
    }.get(wid, {"id": wid}))
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "default")
    monkeypatch.delenv("LIVE_SYNC_WORKSPACES", raising=False)
    return seen


def test_every_workspace_gets_a_tick(ticks):
    live_sync.tick_all()
    assert [w for w, _ in ticks] == ["default", "office", "transcripts-demo"]


def test_each_tick_runs_inside_its_own_workspace(ticks):
    """
    Bound for the whole tick, not passed as an argument -- isolation here fails
    OPEN, and a store built outside the binding would write to `default`.
    """
    live_sync.tick_all()
    from brahmastra.workspace import current_workspace

    assert current_workspace() == "default"      # restored afterwards
    assert len({w for w, _ in ticks}) == 3


def test_only_the_home_workspace_may_use_the_global_notion_source(ticks):
    """
    THE LEAK THIS PREVENTS. `transcripts-demo` names no Notion source, so a pull
    there would fall back to the GLOBAL database and copy default's pages in.
    """
    live_sync.tick_all()
    pulls = dict(ticks)
    assert pulls["default"] is True               # home: global fallback is right
    assert pulls["transcripts-demo"] is False     # no source of its own: no pull


def test_a_workspace_with_its_own_notion_source_still_syncs(ticks):
    live_sync.tick_all()
    assert dict(ticks)["office"] is True


def test_one_failing_workspace_does_not_starve_the_rest(monkeypatch, ticks):
    from brahmastra.workspace import current_workspace

    def flaky(pull_notion=True):
        if current_workspace() == "office":
            raise RuntimeError("office store unreachable")
        ticks.append((current_workspace(), pull_notion))
        return {"did_work": False}

    monkeypatch.setattr(live_sync, "tick", flaky)
    results = live_sync.tick_all()
    assert "office store unreachable" in results["office"]["error"]
    assert "transcripts-demo" in results and "error" not in results["transcripts-demo"]


def test_the_list_can_be_narrowed(monkeypatch, ticks):
    monkeypatch.setenv("LIVE_SYNC_WORKSPACES", "default, office")
    live_sync.tick_all()
    assert [w for w, _ in ticks] == ["default", "office"]


def test_the_home_workspace_runs_even_if_unregistered(monkeypatch, ticks):
    """A registry that cannot be read must not stop the one workspace that
    always worked."""
    def broken():
        raise RuntimeError("no registry")

    monkeypatch.setattr(live_sync.db, "list_workspaces", broken)
    live_sync.tick_all()
    assert [w for w, _ in ticks] == ["default"]


def test_a_tick_without_a_pull_says_why(monkeypatch):
    """The summary distinguishes 'Notion is off' from 'this workspace has no
    source of its own', so a skipped pull is never mistaken for a broken one."""
    monkeypatch.setenv("NOTION_TOKEN", "x")
    monkeypatch.setattr(live_sync.db, "get_notes", lambda status=None: [])
    import brahmastra.keepalive as keepalive
    monkeypatch.setattr(keepalive, "touch_if_idle", lambda: {})
    import brahmastra.sync as sync

    def must_not_pull():
        raise AssertionError("pulled Notion for a workspace with no source")

    monkeypatch.setattr(sync, "run_sync", must_not_pull)
    out = live_sync.tick(pull_notion=False)
    assert out["notion"] == "no Notion source of its own"
