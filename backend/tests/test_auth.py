"""
Tests for API authentication.

The API exposes the whole graph, can spend LLM tokens and can write into a
Notion workspace, so the interesting cases are the ones where it is NOT
configured. This module's job is to prove it fails closed: this codebase has
already shipped workspace isolation that leaked silently and a hardcoded
CORS "*", and both failed open.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "auth.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    def build(**env: str | None) -> TestClient:
        """
        Build the app, THEN set the auth environment.

        Order matters: importing the app loads backend/.env, which sets
        BRAHMASTRA_ALLOW_ANONYMOUS for local development and would otherwise
        undo anything cleared beforehand. auth.py reads the environment on
        every request rather than at import, so setting it afterwards is both
        valid and a closer match to how a deployment behaves — there is no
        .env file inside the image at all.
        """
        import main
        importlib.reload(main)
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return TestClient(main.app, raise_server_exceptions=False)

    return build


def test_unconfigured_refuses_everything_but_health(app_factory, monkeypatch):
    """
    The failure mode a personal deployment cannot afford is the silent one.
    Forgetting to configure auth must break the deployment, not publish it.
    """
    client = app_factory(BRAHMASTRA_API_KEY=None, BRAHMASTRA_ALLOW_ANONYMOUS=None)

    assert client.get("/notes").status_code == 503
    body = client.get("/notes").json()
    assert "BRAHMASTRA_API_KEY" in body["detail"], "must say how to fix it"

    # Probes stay open: an orchestrator's health checker cannot carry a token,
    # and a probe that 503s because auth is unset would mask the real cause.
    assert client.get("/health").status_code == 200


def test_a_key_is_required_and_checked(app_factory, monkeypatch):
    client = app_factory(BRAHMASTRA_API_KEY="s3cret-value",
                         BRAHMASTRA_ALLOW_ANONYMOUS=None)

    assert client.get("/notes").status_code == 401
    assert client.get("/notes", headers={"Authorization": "Bearer wrong"}).status_code == 401

    ok = client.get("/notes", headers={"Authorization": "Bearer s3cret-value"})
    assert ok.status_code == 200

    # x-api-key is accepted too: MCP clients and scripts often find a plain
    # header easier to set, and both are equally secret over TLS.
    assert client.get("/notes", headers={"x-api-key": "s3cret-value"}).status_code == 200


def test_anonymous_must_be_asked_for_explicitly(app_factory, monkeypatch):
    client = app_factory(BRAHMASTRA_API_KEY=None, BRAHMASTRA_ALLOW_ANONYMOUS="1")

    assert client.get("/notes").status_code == 200
    assert client.get("/health/ready").json()["auth"] == "anonymous"


def test_readiness_reports_which_state_it_is_in(app_factory, monkeypatch):
    """
    "unconfigured" means refusing all traffic; "anonymous" means open to anyone
    who can reach it. Both are worth seeing without reading the environment.
    """
    client = app_factory(BRAHMASTRA_API_KEY="k", BRAHMASTRA_ALLOW_ANONYMOUS=None)
    assert client.get("/health/ready").json()["auth"] == "enforced"
