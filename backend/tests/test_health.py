"""
Tests for the liveness and readiness probes.

These exist because the readiness endpoint shipped broken: it called a function
that does not exist on the db facade, so it returned 503 with an AttributeError
forever. A deployment wired to that probe would never have gone ready, and the
failure would have looked like a database problem rather than a typo.

The fastapi/httpx stack lives in backend/.venv, which is what serves the API;
the global interpreter runs the suite. Skip there and run explicitly with:
    .venv/Scripts/python.exe -m pytest tests/test_health.py -q
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi", reason="fastapi is installed in backend/.venv only")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "health.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    import main
    importlib.reload(main)
    return TestClient(main.app)


def test_liveness_does_not_touch_the_database(client, monkeypatch):
    """
    A liveness probe that fails when a dependency is down gets the container
    killed, and restarting cannot fix a sleeping database — it turns a degraded
    system into an unavailable one. So liveness must stay true even when the
    store is unreachable.
    """
    import brahmastra.db as db_mod

    def dead(*a, **k):
        raise RuntimeError("store is down")

    monkeypatch.setattr(db_mod, "get_db_stats", dead)

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_reports_the_store(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["backend"] == "sqlite"
    assert "notes" in body, "must actually query the store, not just return ok"


def test_readiness_is_503_when_the_store_is_unreachable(client, monkeypatch):
    """
    503 is the point: a load balancer stops routing here while the process
    stays alive to recover. Aura Free suspends after ~3 days idle, so this is
    a state the deployment will genuinely reach.
    """
    import brahmastra.db as db_mod

    def dead(*a, **k):
        raise RuntimeError("Failed to DNS resolve address")

    monkeypatch.setattr(db_mod, "get_db_stats", dead)

    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable"
    assert "Failed to DNS resolve" in body["error"], "must say what went wrong"


@pytest.mark.config_only
def test_readiness_names_the_system_of_record(monkeypatch, client):
    """
    `backend` alone stopped being the whole answer once notes and the graph
    could live apart: it names the ENGINE, and the engine is the half that can
    be rebuilt. Which store holds the irreplaceable half is the thing worth
    checking after a deploy.
    """
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "postgres")

    body = client.get("/health/ready").json()

    assert body["backend"] == "sqlite"
    assert body["system_of_record"] == "postgres"


def test_a_sqlite_system_of_record_is_flagged_but_still_serves(monkeypatch, client):
    """
    This misconfiguration looks perfectly healthy, which is exactly why it
    needs saying: SQLite answers every probe normally while being one file on
    one container's disk -- gone on redeploy, invisible to a second instance,
    and lexical-only so hybrid search quietly degrades.

    A warning, not a 503. Refusing traffic would turn a recoverable
    misconfiguration into an outage, and single-store SQLite is still a
    legitimate way to run this locally.
    """
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.delenv("NOTE_BACKEND", raising=False)

    resp = client.get("/health/ready")
    body = resp.json()

    assert resp.status_code == 200, "a warning must not take the service down"
    assert body["system_of_record"] == "sqlite"
    assert any("NOTE_BACKEND=postgres" in w for w in body["warnings"]), (
        "the warning must name the fix, not merely the problem"
    )


@pytest.mark.config_only
def test_a_postgres_system_of_record_raises_no_warning(monkeypatch, client):
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("NOTE_BACKEND", "postgres")
    body = client.get("/health/ready").json()
    assert "warnings" not in body
