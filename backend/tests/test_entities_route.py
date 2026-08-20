"""
Entity lookup over REST.

Documented in AI_AGENTS_INTEGRATION.md long before it existed: every agent
example there called /entities/search and got a 404. Implemented rather than
deleted, because deleting left the REST API with no way to ask about an entity
at all -- a strange hole in a knowledge graph, and one MCP did not have.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ents.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_mod


GRAPH = {
    "nodes": [
        {"id": "Sarah", "label": "Sarah", "type": "person", "pagerank": 0.10, "cluster": 1},
        {"id": "Mei", "label": "Mei", "type": "person", "pagerank": 0.30, "cluster": 1},
        {"id": "llm.py", "label": "llm.py", "type": "tool", "pagerank": 0.05, "cluster": 2},
    ],
    "edges": [
        {"source": "Sarah", "target": "Mei", "relation": "reports_to"},
        {"source": "Mei", "target": "llm.py", "relation": "has_component"},
    ],
}


def _seed(db):
    db.cache_graph(GRAPH, {"concept_clusters": []})


def test_no_cached_graph_says_so_instead_of_returning_empty(client):
    """
    "No graph yet" and "no entity matched" are different answers. Returning []
    for both sends the caller hunting a spelling mistake instead of running the
    pipeline.
    """
    c, db = client
    r = c.get("/entities", params={"q": "Sarah"})
    assert r.status_code == 503
    assert "pipeline/run" in r.json()["detail"]


def test_search_matches_a_substring(client):
    c, db = client
    _seed(db)
    assert [e["label"] for e in c.get("/entities", params={"q": "sar"}).json()] == ["Sarah"]


def test_search_can_filter_by_type(client):
    c, db = client
    _seed(db)
    labels = [e["label"] for e in c.get("/entities", params={"type": "person"}).json()]
    assert set(labels) == {"Sarah", "Mei"}


def test_a_bare_search_ranks_by_centrality(client):
    """
    With no query this answers "what is this graph mostly about?", which is
    usually an agent's first question -- so the order has to mean something.
    """
    c, db = client
    _seed(db)
    assert [e["label"] for e in c.get("/entities").json()] == ["Mei", "Sarah", "llm.py"]


def test_details_keep_direction(client):
    """
    "Sarah reports_to Mei" and "Mei reports_to Sarah" are different facts. A
    single merged list would make them indistinguishable to the caller.
    """
    c, db = client
    _seed(db)
    body = c.get("/entities/Mei").json()

    assert [e["source"] for e in body["incoming"]] == ["Sarah"]
    assert [e["target"] for e in body["outgoing"]] == ["llm.py"]
    assert body["degree"] == 2


def test_details_fall_back_to_a_substring_like_the_mcp_tool(client):
    """Same two-step match, so an agent gets the same entity on either transport."""
    c, db = client
    _seed(db)
    assert c.get("/entities/sar").json()["label"] == "Sarah"


def test_an_unknown_entity_is_a_404(client):
    c, db = client
    _seed(db)
    assert c.get("/entities/nobody").status_code == 404
