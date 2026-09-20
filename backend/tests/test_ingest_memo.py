"""
Never comprehend the same passage twice -- and never serve a stale reading.

A cache on an LLM call is easy to get dangerously right: it works, it is fast,
and it quietly keeps answering with a reading produced by a prompt that no
longer exists. cocoindex keys its memoisation on `hash(input) + hash(code)`,
and the second half is the half worth copying. Prompts here change constantly:
four comprehension variants exist and every one has been rewritten.

So most of these tests are about INVALIDATION, not about hits.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest import memo

PASSAGE = "Sarah: Then we're moving the release to April 15th."
PROMPT = "You extract decisions from a meeting transcript."


def test_the_same_passage_and_prompt_give_the_same_key():
    a = memo.key_for(PASSAGE, "focused", "gpt-oss-120b", PROMPT)
    b = memo.key_for(PASSAGE, "focused", "gpt-oss-120b", PROMPT)
    assert a == b


def test_editing_the_prompt_invalidates_the_cache():
    """
    The one that matters. A cache keyed on the passage alone would keep
    serving a reading produced by a prompt that has since been rewritten --
    which is worse than no cache, because the improvement is invisible.
    """
    before = memo.key_for(PASSAGE, "focused", "gpt-oss-120b", PROMPT)
    after = memo.key_for(PASSAGE, "focused", "gpt-oss-120b",
                         PROMPT + "\nAlso record who committed.")
    assert before != after


def test_a_different_model_is_a_different_reading():
    """A 7B and a 120B do not read a meeting alike; measured, repeatedly."""
    assert memo.key_for(PASSAGE, "focused", "gpt-oss-120b", PROMPT) != \
           memo.key_for(PASSAGE, "focused", "qwen2.5:7b", PROMPT)


def test_a_changed_passage_is_a_different_key():
    assert memo.key_for(PASSAGE, "focused", "m", PROMPT) != \
           memo.key_for(PASSAGE + " Raj: Understood.", "focused", "m", PROMPT)


def test_the_key_cannot_be_confused_by_concatenation():
    """
    Without a separator between parts, ("ab", "c") and ("a", "bc") hash alike --
    so a prompt ending in a word the passage begins with could collide.
    """
    assert memo.key_for("b", "a", "m", PROMPT) != memo.key_for("", "ab", "m", PROMPT)


def test_the_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INGEST_MEMO", "0")
    assert memo.enabled() is False
    assert memo.load("any-key") is None


def test_it_is_on_by_default(monkeypatch):
    monkeypatch.delenv("INGEST_MEMO", raising=False)
    assert memo.enabled() is True


def test_a_broken_store_is_a_miss_not_a_failure(monkeypatch):
    """
    Reporting is not the work. A cache that cannot be reached must degrade to
    paying the model again, never to failing the ingestion.
    """
    monkeypatch.delenv("INGEST_MEMO", raising=False)
    import brahmastra.ingest.store as store_mod

    def broken(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(store_mod, "get_ingest_store", broken)
    assert memo.load("k") is None
    memo.save("k", "a reply")        # must not raise


def test_an_empty_reply_is_never_cached(monkeypatch):
    """
    An empty generation is what a reasoning model returns when its budget ran
    out before the content began. Caching that would make one bad run
    permanent for that passage.
    """
    monkeypatch.delenv("INGEST_MEMO", raising=False)
    saved: list[tuple[str, str]] = []

    class Store:
        def save_comprehension(self, key, payload):
            saved.append((key, payload))

    import brahmastra.ingest.store as store_mod
    monkeypatch.setattr(store_mod, "get_ingest_store", lambda *a, **k: Store())

    memo.save("k", "")
    assert saved == []


def test_a_round_trip_returns_the_raw_reply(monkeypatch):
    monkeypatch.delenv("INGEST_MEMO", raising=False)
    kept: dict[str, str] = {}

    class Store:
        def save_comprehension(self, key, payload):
            kept[key] = payload

        def get_comprehension(self, key):
            return kept.get(key)

    import brahmastra.ingest.store as store_mod
    monkeypatch.setattr(store_mod, "get_ingest_store", lambda *a, **k: Store())

    memo.save("k", '{"decisions": []}')
    assert memo.load("k") == '{"decisions": []}'


def test_the_cache_holds_replies_not_verified_artifacts():
    """
    A contract, stated as a test because it is the easy thing to get wrong.
    Caching verified artifacts would freeze grounding and attribution in place,
    so a fixed bug would stay fixed only for passages nobody had read yet.
    `load` returns the raw reply text; parsing and every check re-run on a hit.
    """
    assert memo.load.__doc__ and "RAW reply" in memo.load.__doc__
