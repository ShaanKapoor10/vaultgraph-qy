"""
Tests for publishing Brahmastra-born notes into Notion.

Neo4j is the system of record and Notion is a human surface, so publishing is
opt-in per note: a design decision belongs in Notion, a session checkpoint is
working memory and belongs only in the graph.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "pub.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def test_publish_defaults_to_off(store):
    """Notion is a human workspace; nothing lands there unless asked for."""
    store.upsert_note("n1", "T", "C")
    assert not store.get_note("n1").get("publish")


def test_publish_is_sticky_across_a_sync(store):
    """
    A sync re-upserts every note without an opinion about publishing. If that
    counted as "publish=False" it would silently unpublish everything a person
    had chosen to publish.
    """
    store.upsert_note("n2", "T", "C", publish=True)
    assert store.get_note("n2")["publish"]

    store.upsert_note("n2", "T", "C v2")          # publish not mentioned
    assert store.get_note("n2")["publish"], "an unrelated update unpublished the note"

    store.upsert_note("n2", "T", "C v3", publish=False)  # explicit
    assert not store.get_note("n2")["publish"]


def test_page_id_is_persisted(store):
    """
    Without persisting the id, every write-back creates the page again and the
    workspace fills with duplicates — worse than not publishing at all.
    """
    store.upsert_note("n3", "T", "C", publish=True)
    assert store.get_note("n3").get("notion_page_id") is None

    store.set_notion_page_id("n3", "38a976bb-0000-0000-0000-000000000000")
    assert store.get_note("n3")["notion_page_id"] == "38a976bb-0000-0000-0000-000000000000"


def test_insights_target_the_right_page(store):
    """
    Triples are keyed by the note's OWN id, but the blocks go to its Notion
    page — a different id once a note was published rather than pulled.
    """
    from brahmastra.notion_writeback import _notion_target

    pulled = {"id": "38a976bb-9093-810f-8161-d2487b63b98e"}
    assert _notion_target(pulled) == pulled["id"]

    published = {"id": "1cc32f87", "notion_page_id": "3c0976bb-9093-81ff-a471-d97aec67537c"}
    assert _notion_target(published) == "3c0976bb-9093-81ff-a471-d97aec67537c"

    local_only = {"id": "abc123", "notion_page_id": None}
    assert _notion_target(local_only) is None


def test_sync_ignores_pages_brahmastra_published(store):
    """
    A published page lives in the same database sync reads from. Pulling it
    back would store the same content twice — once under the local id, once
    under the Notion page id — and double every triple extracted from it.
    """
    from brahmastra import sync

    counters = {"synced": 0, "unchanged": 0}
    page = {"id": "3c0976bb-9093-81ff-a471-d97aec67537c",
            "properties": {}, "last_edited_time": "2026-08-18T00:00:00Z"}

    def explode(*a, **k):  # must not even be read
        raise AssertionError("sync fetched the body of its own published page")

    sync._process_page(explode, page, {}, counters, [], projections={page["id"]})

    assert counters["synced"] == 0
    assert counters["unchanged"] == 1
