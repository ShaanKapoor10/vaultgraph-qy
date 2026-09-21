"""
Never pay a model twice for the same question -- and never serve a stale reply.

A cache on an LLM call is easy to get dangerously right: it works, it is fast,
and it quietly keeps answering with a reply produced by a prompt that no longer
exists. cocoindex keys its memoisation on `hash(input) + hash(code)`, and the
second half is the half worth copying. Prompts here change constantly, and for
extraction the "code" includes THE ONTOLOGY -- so most of these tests are about
INVALIDATION, not about hits.
"""
from __future__ import annotations

import pytest

from brahmastra import memo
from brahmastra.ingest import memo as ingest_memo

NOTE = "Sarah moved the release to April 15th."
PROMPT = "You extract subject-relation-object triples."


@pytest.fixture(autouse=True)
def clean():
    memo.reset()
    yield
    memo.reset()


# -- what is in the key -----------------------------------------------------

def test_the_same_input_and_prompt_give_the_same_key():
    assert memo.key_for(NOTE, "extract", "gpt-oss-120b", PROMPT) == \
           memo.key_for(NOTE, "extract", "gpt-oss-120b", PROMPT)


def test_editing_the_prompt_invalidates_the_cache():
    """
    The one that matters. A cache keyed on the text alone would keep serving
    replies produced by a prompt that has since been rewritten -- worse than no
    cache, because the improvement becomes invisible rather than merely absent.
    """
    assert memo.key_for(NOTE, "extract", "m", PROMPT) != \
           memo.key_for(NOTE, "extract", "m", PROMPT + "\nAlso record dates.")


def test_changing_the_ontology_invalidates_every_extraction():
    """
    The property that makes memoising extraction safe at all. SYSTEM_PROMPT
    carries the ontology, so adding a relation rewrites the prompt and every
    note correctly re-extracts -- while an edit to one unrelated note leaves
    the rest of the corpus alone.
    """
    from brahmastra.extraction import SYSTEM_PROMPT, _memo_key

    before = _memo_key("a note", "m")
    import brahmastra.extraction as extraction
    original = extraction.SYSTEM_PROMPT
    try:
        extraction.SYSTEM_PROMPT = SYSTEM_PROMPT + "\nemploys: person -> org"
        assert extraction._memo_key("a note", "m") != before
    finally:
        extraction.SYSTEM_PROMPT = original


def test_a_different_model_is_a_different_reply():
    assert memo.key_for(NOTE, "extract", "gpt-oss-120b", PROMPT) != \
           memo.key_for(NOTE, "extract", "qwen2.5:7b", PROMPT)


def test_a_different_job_on_the_same_text_is_a_different_key():
    """Comprehending a passage and extracting triples from it are not the same
    question, even when the text is identical."""
    assert memo.key_for(NOTE, "extract", "m", PROMPT) != \
           memo.key_for(NOTE, "comprehend", "m", PROMPT)


def test_the_key_cannot_be_confused_by_concatenation():
    """Without a separator, ("ab","c") and ("a","bc") hash alike."""
    assert memo.key_for("b", "a", "m", PROMPT) != memo.key_for("", "ab", "m", PROMPT)


# -- the switches -----------------------------------------------------------

def test_the_cache_can_be_switched_off_everywhere(monkeypatch):
    monkeypatch.setenv("LLM_MEMO", "0")
    assert memo.enabled() is False
    assert memo.load("any-key") is None


def test_it_is_on_by_default(monkeypatch):
    monkeypatch.delenv("LLM_MEMO", raising=False)
    assert memo.enabled() is True


def test_ingestion_can_be_switched_off_without_switching_off_extraction(monkeypatch):
    """
    Two switches because they answer different questions. Somebody comparing
    comprehension variants needs the thing under measurement re-read every
    time and everything around it still cached.
    """
    monkeypatch.delenv("LLM_MEMO", raising=False)
    monkeypatch.setenv("INGEST_MEMO", "0")

    assert ingest_memo.enabled() is False
    assert memo.enabled() is True


# -- behaviour --------------------------------------------------------------

class _Store:
    """A store that says how often it was built and how often it was asked."""

    built = 0

    def __init__(self, workspace="default"):
        type(self).built += 1
        self.workspace = workspace
        self.gets = 0
        self.rows: dict[str, str] = {}

    def get(self, key):
        self.gets += 1
        return self.rows.get(key)

    def put(self, key, variant, payload):
        self.rows[key] = payload


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("LLM_MEMO", raising=False)
    _Store.built = 0
    built = _Store()
    monkeypatch.setattr(memo, "MemoStore", lambda workspace: built)
    memo.reset()
    yield built
    memo.reset()


def test_a_round_trip_returns_the_raw_reply(store):
    memo.save("k", '{"triples": []}')
    memo.reset()                 # or the in-process layer answers instead
    assert memo.load("k") == '{"triples": []}'


def test_an_empty_reply_is_never_cached(store):
    """
    What a reasoning model returns when its budget ran out before the content
    began. Caching it would make one bad run permanent for that note.
    """
    memo.save("k", "")
    assert store.rows == {}


def test_a_broken_store_is_a_miss_not_a_failure(monkeypatch):
    """
    A cache that cannot be reached must degrade to paying the model again,
    never to failing the extraction.
    """
    monkeypatch.delenv("LLM_MEMO", raising=False)
    memo.reset()

    def broken(workspace):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(memo, "MemoStore", broken)
    assert memo.load("k") is None
    memo.save("k", "a reply")          # must not raise


def test_the_store_is_built_once_not_once_per_lookup(store):
    """
    A cache whose LOOKUP costs a connection and a schema rebuild is a smaller
    copy of the problem it was built to solve. Measured against the deployed
    Postgres before this was fixed: 15.8ms of a 35.6ms load was the store
    setting itself up, twice per call.
    """
    _Store.built = 0
    for i in range(10):
        memo.load(f"absent-{i}")
    assert _Store.built <= 1


def test_a_second_read_does_not_ask_the_database(store):
    memo.save("k", "a reply")
    assert memo.load("k") == "a reply"
    assert memo.load("k") == "a reply"
    assert store.gets == 0

    memo.reset()
    assert memo.load("k") == "a reply"
    assert store.gets == 1


def test_the_in_process_layer_is_bounded(store):
    """It sits in a long-lived uvicorn process."""
    for i in range(memo.LOCAL_MAX + 50):
        memo.save(f"k{i}", "x")
    assert len(memo._local) == memo.LOCAL_MAX


def test_the_in_process_layer_is_scoped_by_workspace(store, monkeypatch):
    """
    A reply does not actually depend on the workspace -- the key already
    digests the text, the prompt and the model. Scoped anyway, because
    isolation in this project fails OPEN and every leak it has had came from a
    filter that was fine to omit right up until it was not.
    """
    monkeypatch.setattr(memo, "_workspace", lambda: "office")
    memo.save("k", "read for office")

    monkeypatch.setattr(memo, "_workspace", lambda: "personal")
    store.rows.clear()
    assert memo.load("k") is None


def test_the_cache_holds_replies_not_parsed_triples():
    """
    A contract, stated as a test because it is the easy thing to get wrong.
    `_parse_llm_response` validates, coerces unmappable relations to
    related_to and records the coercions; caching its OUTPUT would freeze all
    of that at whichever version ran first, so a fixed bug would stay fixed
    only for notes nobody had extracted yet.
    """
    import inspect
    import brahmastra.extraction as extraction

    for name in ("_extract_with_groq", "_extract_with_ollama",
                 "_extract_with_anthropic"):
        source = inspect.getsource(getattr(extraction, name))
        assert "_parse_llm_response(cached)" in source, name
