"""
No workspace may reach another workspace's Notion.

THE INCIDENT, 2026-09-24. The scheduler learned to keep every workspace
current. `office`, `work` and `transcripts-demo` named no Notion source of
their own, so `_notion_target_for_current_workspace` fell back to the GLOBAL
database for each of them -- and one tick copied four of the personal graph's
Notion pages into all three, then ran write-back in each, putting three foreign
graphs' insights onto the personal graph's real pages in turn.

The scheduler had a guard. It skipped its OWN pull; run_pipeline's Sync stage
pulled anyway, through the resolver. So the rule now lives in the resolver,
where every caller -- scheduler, API, MCP, CLI -- goes through it.
"""
from __future__ import annotations

import pytest

from brahmastra import db, pipeline
from brahmastra import sync as notion_sync
from brahmastra.workspace import reset_request_workspace, set_request_workspace


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("NOTION_DATABASE_ID", "GLOBAL-personal-db")
    monkeypatch.setenv("NOTION_TOKEN", "secret")
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "default")
    workspaces = {
        "default": {"id": "default"},
        "office": {"id": "office", "notion_database_id": "office-db"},
        "transcripts-demo": {"id": "transcripts-demo"},
    }
    monkeypatch.setattr(db, "get_workspace", lambda wid: workspaces.get(wid))


def _in(workspace, fn):
    token = set_request_workspace(workspace)
    try:
        return fn()
    finally:
        reset_request_workspace(token)


# -- the resolver ------------------------------------------------------------

def test_the_home_workspace_may_use_the_global_source(registry):
    assert _in("default", notion_sync._notion_target_for_current_workspace) \
        == "GLOBAL-personal-db"


def test_a_workspace_with_its_own_source_uses_it(registry):
    assert _in("office", notion_sync._notion_target_for_current_workspace) \
        == "office-db"


def test_a_workspace_with_no_source_gets_none_not_the_personal_graph(registry):
    """THE LEAK. This returned 'GLOBAL-personal-db'."""
    assert _in("transcripts-demo",
               notion_sync._notion_target_for_current_workspace) is None


def test_an_unreadable_registry_fails_closed_outside_home(monkeypatch, registry):
    def broken(wid):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(db, "get_workspace", broken)
    assert _in("transcripts-demo",
               notion_sync._notion_target_for_current_workspace) is None
    # ...while the home workspace keeps working exactly as before.
    assert _in("default", notion_sync._notion_target_for_current_workspace) \
        == "GLOBAL-personal-db"


def test_a_non_default_home_workspace_gets_the_fallback(monkeypatch, registry):
    """'Home' is whatever the deployment was started for, not the literal
    word default -- a single-workspace setup named 'work' must keep working."""
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "transcripts-demo")
    assert _in("transcripts-demo",
               notion_sync._notion_target_for_current_workspace) \
        == "GLOBAL-personal-db"


# -- the pipeline's two Notion stages ----------------------------------------

def test_a_sourceless_workspace_neither_pulls_nor_writes_back(registry):
    """
    Both stages ask `_missing_notion_config`. Write-back used to need only the
    token, so a workspace holding someone else's pages wrote onto them.
    """
    assert _in("transcripts-demo",
               lambda: pipeline._missing_notion_config(need_database=True))
    assert _in("transcripts-demo",
               lambda: pipeline._missing_notion_config(need_database=False))


def test_the_home_workspace_is_unchanged(registry):
    assert _in("default", lambda: pipeline._missing_notion_config(True)) == []
    assert _in("default", lambda: pipeline._missing_notion_config(False)) == []


def test_a_workspace_with_its_own_source_can_do_both(registry):
    assert _in("office", lambda: pipeline._missing_notion_config(True)) == []
    assert _in("office", lambda: pipeline._missing_notion_config(False)) == []


def test_the_import_failure_path_fails_closed_too(monkeypatch, registry):
    """`_notion_source` handed ANY workspace the global database if the
    resolver could not be imported -- the same leak in miniature."""
    import builtins

    real_import = builtins.__import__

    def no_sync(name, *args, **kwargs):
        if name == "brahmastra.sync":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sync)
    assert _in("transcripts-demo", pipeline._notion_source) is None
    assert _in("default", pipeline._notion_source) == "GLOBAL-personal-db"
