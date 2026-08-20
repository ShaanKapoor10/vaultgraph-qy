"""
GET /notes?q= must actually search.

It previously accepted `q` only in the sense that FastAPI ignored it, so the
route returned every note in last_edited order. That is indistinguishable from
a search that matched everything weakly -- the feature looked present and
broken rather than absent, and it only surfaced when the deployed stack
returned the same ordering for two unrelated queries.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "routes.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    monkeypatch.delenv("NOTE_BACKEND", raising=False)
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_mod


def test_q_searches_rather_than_listing_everything(client):
    c, db = client
    db.upsert_note("a", "Certifi and TLS", "The driver needs an explicit certifi context.")
    db.upsert_note("b", "Pasta", "Drain after nine minutes.")

    ids = [n["id"] for n in c.get("/notes", params={"q": "certifi"}).json()]

    assert ids == ["a"], f"expected only the matching note, got {ids}"


def test_no_q_still_lists_everything(client):
    c, db = client
    db.upsert_note("a", "One", "x")
    db.upsert_note("b", "Two", "y")

    assert len(c.get("/notes").json()) == 2


def test_a_blank_q_lists_rather_than_searching_for_nothing(client):
    """`?q=` with an empty value is a caller quirk, not a request for zero results."""
    c, db = client
    db.upsert_note("a", "One", "x")

    assert len(c.get("/notes", params={"q": "   "}).json()) == 1


def test_relevance_order_survives_the_route(client, monkeypatch):
    """
    On the hybrid backends the order IS the answer -- the fused BM25-plus-vector
    ranking. A route that re-sorted by date would discard it while still
    returning the right set, which no assertion on membership would catch.
    """
    c, db = client
    import brahmastra.db as db_mod
    monkeypatch.setattr(db_mod, "search_notes",
                        lambda q, limit=10: [{"id": "second"}, {"id": "first"}])

    ids = [n["id"] for n in c.get("/notes", params={"q": "anything"}).json()]
    assert ids == ["second", "first"], "the store's ranking must reach the client intact"


def test_status_filter_is_unaffected(client):
    c, db = client
    db.upsert_note("a", "One", "x", mark_pending=True)
    db.upsert_note("b", "Two", "y", mark_pending=False)

    ids = [n["id"] for n in c.get("/notes", params={"status": "pending"}).json()]
    assert ids == ["a"]
