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
    memo.reset()              # or the in-process layer answers and the store
                              # is never asked, which is not what this tests
    assert memo.load("k") == '{"decisions": []}'


def test_the_cache_holds_replies_not_verified_artifacts():
    """
    A contract, stated as a test because it is the easy thing to get wrong.
    Caching verified artifacts would freeze grounding and attribution in place,
    so a fixed bug would stay fixed only for passages nobody had read yet.
    `load` returns the raw reply text; parsing and every check re-run on a hit.
    """
    assert memo.load.__doc__ and "RAW reply" in memo.load.__doc__


# -- reaching the store ----------------------------------------------------
#
# The first version built a store per call, so every lookup re-ran the schema
# DDL before the SELECT it wanted. On the deployed Postgres that was 15.8ms of
# the 35.6ms a load cost, twice per comprehension call -- 5.8s of pure
# bookkeeping on a 40-chunk transcript, scaling with exactly the document
# length this module exists to make cheap.


class _CountingStore:
    """A store that says how often it was built and how often it was asked."""

    built = 0

    def __init__(self):
        type(self).built += 1
        self.gets = 0
        self.rows: dict[str, str] = {}

    def get_comprehension(self, key):
        self.gets += 1
        return self.rows.get(key)

    def save_comprehension(self, key, payload):
        self.rows[key] = payload


@pytest.fixture
def counting(monkeypatch):
    monkeypatch.delenv("INGEST_MEMO", raising=False)
    memo.reset()
    _CountingStore.built = 0
    store = _CountingStore()

    import brahmastra.ingest.store as store_mod
    monkeypatch.setattr(store_mod, "get_ingest_store", lambda *a, **k: store)
    yield store
    memo.reset()


def test_the_store_is_built_once_not_once_per_lookup(counting, monkeypatch):
    """
    The fix, stated as the measurement that prompted it: a cache whose LOOKUP
    costs a connection and a schema rebuild is a smaller copy of the problem
    it was built to solve.
    """
    calls = {"n": 0}

    def factory(*args, **kwargs):
        calls["n"] += 1
        return counting

    import brahmastra.ingest.store as store_mod
    monkeypatch.setattr(store_mod, "get_ingest_store", factory)

    for i in range(10):
        memo.load(f"absent-{i}")
    assert calls["n"] == 1


def test_a_replaced_factory_is_obeyed(counting):
    """
    Module state that outlives a monkeypatch makes the patch silently
    ineffective -- the exact shape of bug this cache could otherwise hide.
    """
    memo.save("k", "from the first store")
    memo.reset()
    assert memo.load("k") == "from the first store"

    import brahmastra.ingest.store as store_mod
    replacement = _CountingStore()
    store_mod.get_ingest_store = lambda *a, **k: replacement
    try:
        memo.reset()
        assert memo.load("k") is None          # the new store holds nothing
    finally:
        memo.reset()


def test_a_second_read_of_one_passage_does_not_ask_the_database(counting):
    """
    Chunks overlap on purpose, so one document asks for the same passage more
    than once, and a load straight after a save is a round trip for something
    this process is already holding.
    """
    memo.save("k", "a reading")
    assert memo.load("k") == "a reading"
    assert memo.load("k") == "a reading"
    assert counting.gets == 0

    memo.reset()
    assert memo.load("k") == "a reading"
    assert counting.gets == 1


def test_the_in_process_layer_is_bounded(counting):
    """
    It sits in a long-lived uvicorn process. Sized for one long document, not
    for a corpus -- the store is the durable half.
    """
    for i in range(memo.LOCAL_MAX + 50):
        memo.save(f"k{i}", "x")
    assert len(memo._local) == memo.LOCAL_MAX


def test_the_in_process_layer_is_scoped_by_workspace(counting, monkeypatch):
    """
    A reading does not actually depend on the workspace -- the key already
    digests the passage, the prompt and the model. Scoped anyway, because
    isolation in this project fails OPEN and every leak it has had came from a
    filter that was fine to omit right up until it was not.
    """
    monkeypatch.setattr(memo, "_workspace", lambda: "office")
    memo.save("k", "read for office")

    monkeypatch.setattr(memo, "_workspace", lambda: "personal")
    counting.rows.clear()
    assert memo.load("k") is None
