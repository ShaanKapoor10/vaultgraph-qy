"""
A sleeping dependency must not take the API down with it.

The health probes already said this: /health deliberately does not touch the
database, because a liveness probe that fails when a dependency is down gets
the container killed and restarted, which cannot fix a dependency. The lifespan
then called init_db() unguarded and did exactly that.

It happened. Aura Free suspended, its hostname stopped resolving, init_schema
raised ServiceUnavailable, and uvicorn logged "Application startup failed.
Exiting." The API was down for a reason that had nothing to do with the API,
and being a crash loop it could not come back on its own -- while Postgres, the
system of record, held every note the entire time.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def unreachable(monkeypatch, tmp_path):
    """A store whose schema creation fails, the way a suspended Aura does."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "startup.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")

    import main
    importlib.reload(main)

    # Fails for the first TWO calls: one is consumed by startup itself, so a
    # single failure would leave the very first readiness probe succeeding and
    # the outage would never be observable from outside.
    calls = {"n": 0}
    real = main.init_db

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError(
                "Failed to DNS resolve address 208ed26a.databases.neo4j.io:7687"
            )
        return real()

    monkeypatch.setattr(main, "init_db", flaky)
    return main, calls


def test_the_app_starts_even_when_the_store_is_unreachable(unreachable):
    """
    The claim that matters. If this raises, the container exits and nothing
    reaches the API -- including the probe that would have explained why.
    """
    main, _ = unreachable
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"


def test_liveness_stays_ok_so_the_container_is_not_killed(unreachable):
    """Restarting a healthy process cannot resume somebody else's database."""
    main, _ = unreachable
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200


def test_readiness_reports_503_and_says_why(unreachable):
    main, _ = unreachable
    with TestClient(main.app) as client:
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "DNS resolve" in body["error"]


def test_it_recovers_without_a_restart(unreachable):
    """
    The whole reason for staying alive. A crash loop cannot notice that the
    engine came back; a live process can, and the schema creation is idempotent
    so retrying costs nothing.
    """
    main, calls = unreachable
    with TestClient(main.app) as client:
        assert client.get("/health/ready").status_code == 503     # first attempt fails
        assert client.get("/health/ready").status_code == 200     # engine is back
        assert calls["n"] >= 2, "readiness never retried the schema creation"


def test_a_healthy_start_does_not_retry_on_every_probe(monkeypatch, tmp_path):
    """Readiness is polled constantly; it must not redo startup work each time."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ok.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")

    import main
    importlib.reload(main)

    calls = {"n": 0}
    real = main.init_db

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(main, "init_db", counted)
    with TestClient(main.app) as client:
        client.get("/health/ready")
        client.get("/health/ready")
        assert calls["n"] == 1
