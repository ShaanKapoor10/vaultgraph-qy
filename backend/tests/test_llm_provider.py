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


# -- backing off the way the server asked -----------------------------------
#
# This rule existed and was in the wrong file. extraction.py honoured the delay
# Groq states; `_groq_chat` in llm.py -- the path comprehension, cluster
# summaries, GraphRAG and checkpointing all take -- still slept 2s, 4s, 6s,
# which CLAUDE.md already records as useless: a blind 2+4 covers six seconds of
# a limit the server says needs thirty, so all three attempts land inside the
# same closed window.


def test_the_shared_path_waits_as_long_as_groq_asked():
    from brahmastra.llm import retry_delay

    delay = retry_delay(Exception("Rate limit reached. Please try again in 7.456s"), 0)
    assert delay == pytest.approx(7.556)          # a hair over, not just inside


def test_minutes_in_the_hint_are_read():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("try again in 0m32s"), 0) == pytest.approx(32.1)


def test_a_hint_that_would_block_the_run_is_capped():
    """Past the cap, sleeping blocks the caller for work the next run retries
    for free."""
    from brahmastra.llm import retry_delay, max_backoff

    assert retry_delay(Exception("try again in 1m14.2s"), 0) == max_backoff()


def test_a_suspiciously_short_hint_never_beats_the_fallback():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("try again in 0.2s"), 0) == 2.0


def test_no_hint_falls_back_to_exponential():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("connection reset"), 0) == 2.0
    assert retry_delay(Exception("connection reset"), 1) == 4.0


def test_groq_chat_uses_it_rather_than_a_guess():
    """
    The defect, pinned. A grep for the old constant is the cheapest way to
    stop it coming back, because nothing about `time.sleep(2 * (attempt + 1))`
    looks wrong on its own.
    """
    import inspect
    from brahmastra import llm

    source = inspect.getsource(llm._groq_chat)
    assert "retry_delay(e, attempt)" in source
    assert "2 * (attempt + 1)" not in source


def test_extraction_shares_the_one_implementation():
    """One rule should not have two implementations that can drift."""
    from brahmastra import extraction, llm

    assert extraction._retry_delay is llm.retry_delay
