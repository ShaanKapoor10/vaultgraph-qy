"""
Entity resolution can run on its own model (RESOLUTION_LLM_MODEL), without
changing what extraction runs on -- ROADMAP item 7's precondition.
"""
from __future__ import annotations

from brahmastra import llm


def test_a_provider_prefix_is_split_off():
    assert llm.parse_model_setting("groq:qwen/qwen3.8-27b") == ("groq", "qwen/qwen3.8-27b")


def test_an_ollama_id_keeps_its_colon():
    assert llm.parse_model_setting("qwen2.5:7b-instruct") == (None, "qwen2.5:7b-instruct")
    assert llm.parse_model_setting("ollama:qwen2.5:7b-instruct") == ("ollama", "qwen2.5:7b-instruct")


def test_empty_means_the_default():
    assert llm.parse_model_setting("") is None


def test_the_override_applies_inside_the_block_only():
    before = llm.model_for("groq")
    with llm.using_model("groq:some/other-model"):
        assert llm.model_for("groq") == "some/other-model"
    assert llm.model_for("groq") == before


def test_the_override_never_leaks_to_another_provider():
    """A fallback from Groq to Ollama must not be handed a Groq model id."""
    ollama = llm.model_for("ollama")
    with llm.using_model("groq:some/other-model"):
        assert llm.model_for("ollama") == ollama


def test_the_judge_runs_on_the_resolution_model(monkeypatch):
    import brahmastra.entity_confirm as ec
    seen = []
    monkeypatch.setenv("RESOLUTION_LLM_MODEL", "groq:judge/model")
    monkeypatch.setattr(ec, "available", lambda: True)
    monkeypatch.setattr(ec, "_ask", lambda batch, model, context=None: seen.append(
        (llm.model_for("groq"), model)) or {i + 1: True for i in range(len(batch))})
    monkeypatch.setattr(ec, "_active_model", lambda: llm.model_for("groq"))
    ec.confirm([("SQLite", "SQLite database")])
    assert seen == [("judge/model", "judge/model")]
    assert llm.model_for("groq") != "judge/model"


def test_ollama_honours_the_override_too(monkeypatch):
    """ollama_chat used to read OLLAMA_MODEL itself and would have ignored it."""
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"message": {"content": "ok"}}'

    import json, urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: sent.update(json.loads(req.data)) or Resp())
    with llm.using_model("ollama:llama3.1:8b"):
        llm.ollama_chat("s", "u", retries=1)
    assert sent["model"] == "llama3.1:8b"
