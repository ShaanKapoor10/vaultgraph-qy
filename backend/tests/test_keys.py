"""
Groq keys managed from the dashboard (brahmastra/keys.py). Fake keys only --
never a fragment of a real one.
"""
from __future__ import annotations

import json

import pytest

from brahmastra import groq_pool, keys

ENV_A = "gsk_" + "A" * 48
ENV_B = "gsk_" + "B" * 48
NEW = "gsk_" + "C" * 48


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEYS", f"{ENV_A},{ENV_B}")
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(keys, "test_groq_key", lambda key: {"ok": True, "models": 3})
    groq_pool.reset()
    yield
    groq_pool.reset()


def test_the_environment_alone_is_what_it_always_was():
    assert groq_pool.configured_keys() == [ENV_A, ENV_B]


def test_an_added_key_joins_the_pool_at_once():
    keys.add_groq_key(NEW)
    assert groq_pool.configured_keys() == [ENV_A, ENV_B, NEW]
    assert NEW in groq_pool.pool().keys


def test_a_key_from_env_is_disabled_not_deleted():
    out = keys.remove_groq_key(keys.key_id(ENV_A))
    assert "disabled" in out["action"]
    assert groq_pool.configured_keys() == [ENV_B]
    keys.enable_groq_key(keys.key_id(ENV_A))
    assert groq_pool.configured_keys() == [ENV_A, ENV_B]


def test_a_dashboard_key_is_deleted():
    keys.add_groq_key(NEW)
    assert keys.remove_groq_key(keys.key_id(NEW))["action"] == "deleted"
    assert NEW not in groq_pool.configured_keys()


def test_something_that_is_not_a_key_is_refused_with_a_reason():
    with pytest.raises(keys.KeyError_, match="does not look like a Groq key"):
        keys.add_groq_key("my key is gsk_short")


def test_a_key_groq_refuses_is_not_stored(monkeypatch):
    monkeypatch.setattr(keys, "test_groq_key", lambda key: {"ok": False, "error": "invalid key"})
    with pytest.raises(keys.KeyError_, match="refused"):
        keys.add_groq_key(NEW)
    assert NEW not in groq_pool.configured_keys()


def test_the_list_never_carries_a_whole_key():
    keys.add_groq_key(NEW)
    listing = json.dumps(keys.list_groq_keys())
    for k in (ENV_A, ENV_B, NEW):
        assert k not in listing
    assert {r["source"] for r in keys.list_groq_keys()} == {"env", "dashboard"}


def test_a_broken_key_file_leaves_the_environment_keys_working(tmp_path):
    (tmp_path / keys.FILE_NAME).write_text("{not json", encoding="utf-8")
    assert groq_pool.configured_keys() == [ENV_A, ENV_B]


def test_changing_keys_over_http_is_off_unless_switched_on(monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    client = TestClient(main.app)
    monkeypatch.delenv("BRAHMASTRA_KEY_ADMIN", raising=False)
    assert client.post("/diagnostics/keys", json={"key": NEW}).status_code == 403
    assert client.get("/diagnostics/keys").json()["admin"] is False

    monkeypatch.setenv("BRAHMASTRA_KEY_ADMIN", "1")
    added = client.post("/diagnostics/keys", json={"key": NEW})
    assert added.status_code == 200 and NEW not in added.text
    assert client.delete(f"/diagnostics/keys/{added.json()['id']}").json()["action"] == "deleted"
