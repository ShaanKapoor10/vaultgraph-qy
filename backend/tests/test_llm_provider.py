"""
Which provider serves a request, and what happens when one stops being able to.

The quota case is here because it did real damage: a daily cap landing between
the two calls of one comprehension stored a meeting with its decisions and none
of its risks, on a machine that had a working local model the whole time.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# A spent daily quota, with another provider sitting there working
# ---------------------------------------------------------------------------

def test_a_spent_quota_falls_back_to_a_provider_that_works(monkeypatch):
    """
    LLMQuotaExhausted raises immediately and correctly -- retrying a spent cap
    is as pointless as retrying a retired model. But the caller above it then
    failed, and on this system that failure had a shape: comprehension makes
    two calls per chunk, the cap landed between them, and a meeting was stored
    with four decisions, four action items and ZERO risks. Half a record, from
    a machine with a working local model.
    """
    from brahmastra import llm

    calls: list[str] = []

    def dispatch(name, system, user, **kw):
        calls.append(name)
        if name == "groq":
            raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")
        return "{}"

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    assert llm.chat("s", "u") == "{}"
    assert calls == ["groq", "ollama"], "it did not fall back to the working provider"


def test_a_pinned_provider_is_never_second_guessed(monkeypatch):
    """`provider=` is an explicit choice; silently serving from another one
    would make a pinned comparison meaningless."""
    from brahmastra import llm

    def dispatch(name, system, user, **kw):
        raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    with pytest.raises(llm.LLMQuotaExhausted):
        llm.chat("s", "u", provider="groq")


def test_a_spent_quota_with_nowhere_to_go_still_raises(monkeypatch):
    from brahmastra import llm

    def dispatch(name, system, user, **kw):
        raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": False})

    with pytest.raises(llm.LLMQuotaExhausted):
        llm.chat("s", "u")


def test_a_transient_failure_does_not_trigger_a_downgrade(monkeypatch):
    """The retry loop handles those. Falling back on any error would quietly
    move traffic to a smaller model on a blip."""
    from brahmastra import llm

    calls: list[str] = []

    def dispatch(name, system, user, **kw):
        calls.append(name)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    with pytest.raises(RuntimeError):
        llm.chat("s", "u")
    assert calls == ["groq"]
