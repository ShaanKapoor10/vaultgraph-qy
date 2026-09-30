"""
The Claude Code gateway (brahmastra/claude_gateway.py) and the `gateway`
provider -- an experiment that must never serve production by accident.
No CLI is run: `run_claude` is faked.
"""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from brahmastra import claude_gateway, llm


def test_the_gateway_is_never_chosen_automatically(monkeypatch):
    monkeypatch.setattr(llm, "provider_status", lambda: {p: p == "gateway" for p in llm.PROVIDERS})
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(llm.LLMUnavailable):
        llm.resolve_provider()
    monkeypatch.setenv("LLM_PROVIDER", "gateway")
    assert llm.resolve_provider() == "gateway"


def test_the_gateway_is_never_a_quota_fallback(monkeypatch):
    monkeypatch.setattr(llm, "provider_status", lambda: {p: p in ("groq", "gateway") for p in llm.PROVIDERS})
    assert llm._next_usable_provider("groq") is None


def test_json_is_dug_out_of_a_chatty_reply():
    assert json.loads(claude_gateway._extract_json('Sure!\n```json\n{"a": 1}\n```')) == {"a": 1}


@pytest.fixture
def served(monkeypatch):
    calls = []

    def fake(system, user, model, schema, json_object):
        calls.append({"system": system, "user": user, "model": model, "schema": schema,
                      "json_object": json_object})
        return {"text": '{"ok": true}', "usage": {"input_tokens": 10, "output_tokens": 3},
                "cost": 0.001, "ms": 5}

    monkeypatch.setattr(claude_gateway, "run_claude", fake)
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(__import__("tempfile").mkdtemp()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), claude_gateway.make_handler(2, None))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", calls
    server.shutdown()


def test_brahmastra_chat_goes_through_the_gateway_in_the_openai_shape(served, monkeypatch):
    url, calls = served
    monkeypatch.setenv("CLAUDE_GATEWAY_URL", url)
    llm._gateway_seen.clear()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    out = llm.chat("sys", "user text", json_schema=schema, provider="gateway")
    assert json.loads(out) == {"ok": True}
    assert calls[0]["system"] == "sys" and calls[0]["user"] == "user text"
    assert calls[0]["schema"] == schema and calls[0]["model"] == "haiku"


def test_json_mode_is_passed_as_json_object(served, monkeypatch):
    url, calls = served
    monkeypatch.setenv("CLAUDE_GATEWAY_URL", url)
    llm.chat("sys", "u", json_mode=True, provider="gateway")
    assert calls[0]["json_object"] is True and calls[0]["schema"] is None
