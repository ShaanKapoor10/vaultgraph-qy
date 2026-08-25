"""
The API serves the workspace the request asks for.

It used to read BRAHMASTRA_WORKSPACE once and serve that one workspace for the
life of the process. The store and MCP layers had supported many graphs for a
long time, but every HTTP caller could reach exactly one -- so the dashboard
had no way to show a second, and no reason to offer a picker.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ws.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    monkeypatch.delenv("BRAHMASTRA_WORKSPACE", raising=False)

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c, db_mod


# ---------------------------------------------------------------------------
# Selecting a workspace
# ---------------------------------------------------------------------------

def test_no_workspace_named_serves_the_default(client):
    c, _ = client
    assert c.get("/health/ready").json()["workspace"] == "default"


def test_a_query_parameter_selects_one(client):
    """
    The dashboard's proxy forwards the query string untouched, so this works
    with no plumbing on that side.
    """
    c, _ = client
    c.post("/workspaces", json={"id": "office", "name": "Office"})
    assert c.get("/health/ready", params={"workspace": "office"}).json()["workspace"] == "office"


def test_a_header_selects_one_and_beats_the_query(client):
    """
    A header is the request's own metadata; a query parameter can be pasted
    into a shared link, and a shared link quietly writing into someone else's
    graph is the failure worth avoiding.
    """
    c, _ = client
    c.post("/workspaces", json={"id": "office", "name": "Office"})
    body = c.get(
        "/health/ready",
        params={"workspace": "default"},
        headers={"X-Brahmastra-Workspace": "office"},
    ).json()
    assert body["workspace"] == "office"


def test_an_invalid_workspace_is_refused_not_silently_defaulted(client):
    """
    Falling back to `default` would serve a different graph than the one asked
    for -- which is how a caller writes into the wrong one and never finds out.
    """
    c, _ = client
    resp = c.get("/notes", params={"workspace": "Not A Workspace!"})
    assert resp.status_code == 400
    assert "invalid workspace" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# What the separation is actually for
# ---------------------------------------------------------------------------

def test_notes_are_segregated_and_ids_may_repeat(client):
    """
    Two workspaces may each hold a note with the same id and they are different
    notes. Uniqueness is per workspace, so this is the property that proves the
    request is reaching a genuinely separate graph rather than a filtered view.
    """
    c, _ = client
    c.post("/workspaces", json={"id": "office", "name": "Office"})

    c.post("/notes", json={"id": "n1", "title": "Home thing", "content": "Buy milk."})
    c.post("/notes", json={"id": "n1", "title": "Work thing", "content": "Sarah reports to Mei."},
           params={"workspace": "office"})

    assert [n["title"] for n in c.get("/notes").json()] == ["Home thing"]
    assert [n["title"] for n in c.get("/notes", params={"workspace": "office"}).json()] == ["Work thing"]

    assert c.get("/notes/n1").json()["title"] == "Home thing"
    assert c.get("/notes/n1", params={"workspace": "office"}).json()["title"] == "Work thing"


def test_a_workspace_does_not_leak_into_the_next_request(client):
    """
    The binding is a ContextVar, and a task that keeps it hands it to whatever
    runs next on the same context. In a system whose isolation is "every row
    carries a workspaceId", that would mean serving one graph in place of
    another -- so it is reset in a finally, and this is what proves it.
    """
    c, _ = client
    c.post("/workspaces", json={"id": "office", "name": "Office"})

    assert c.get("/health/ready", params={"workspace": "office"}).json()["workspace"] == "office"
    assert c.get("/health/ready").json()["workspace"] == "default", (
        "the previous request's workspace survived into this one"
    )


def test_a_failing_request_still_resets_the_binding(client):
    """A 400 must not leave the binding set for whatever runs next."""
    c, _ = client
    c.post("/workspaces", json={"id": "office", "name": "Office"})

    c.get("/notes", params={"workspace": "office"})
    c.get("/notes", params={"workspace": "!!bad!!"})     # 400 inside the middleware
    assert c.get("/health/ready").json()["workspace"] == "default"


def test_the_environment_default_still_applies_when_nothing_is_named(client, monkeypatch):
    """Existing single-workspace deployments must be unaffected."""
    c, _ = client
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "office")
    assert c.get("/health/ready").json()["workspace"] == "office"
    # And a request naming one still overrides it.
    assert c.get("/health/ready", params={"workspace": "default"}).json()["workspace"] == "default"
