"""
Jaro-Winkler judges SPELLING, word by word -- not a shared prefix.

Scored over a whole string it merged two different things that shared a first
word. From the live graph on 2026-09-24, 12 of its 30 merges were refused by
this rule; 11 were plainly wrong, and nothing was merged that was not before.
"""
from __future__ import annotations

import pytest

import brahmastra.entity_resolution as er


@pytest.mark.parametrize("a,b", [
    ("Brahmastra engine", "Brahmastra pipeline"),
    ("NOTION_DATABASE_ID", "Notion database"),
    ("BRAHMASTRA_CACHE", "Brahmastra"),
    ("GRAPH_KEEPALIVE_HOURS", "graph keepalive"),
    ("ANLI dataset", "MNLI dataset"),
    ("artifact", "artifact_id"),
    ("Brahmastra frontend", "brahmastra.stores"),
])
def test_a_shared_first_word_is_not_a_spelling_variant(a, b):
    assert er._heuristic_sim(a, b)[1] != "jaro_winkler"


@pytest.mark.parametrize("a,b", [
    ("live sync watcher", "live_sync watcher"),
    ("decision", "decisions"),
    ("PostgreSQL", "Postgres"),
    ("Ollama chat helper", "ollama_chat helper"),
    ("active model", "active_model"),
    ("Shaan Kapoor", "ShaanKapoor10"),       # a handle: the same words run together
    ("GraphStore", "graph_store"),
])
def test_the_same_words_spelled_differently_still_merge(a, b):
    sim, method = er._heuristic_sim(a, b)
    assert sim >= er.MERGE_THRESHOLD, (a, b, sim, method)


@pytest.mark.parametrize("a,b", [
    ("global retrieval mode", "local retrieval mode"),
    ("Global search", "local search"),
])
def test_global_and_local_are_opposites(a, b):
    """GraphRAG's two modes; the embedding path merged them once spelling stopped."""
    assert er._is_contrasting(a, b)


def test_the_helper_is_not_shadowed():
    """The first version of this rule was silently replaced by another `_words`
    further down the module, and was measured that way. Pin the real one."""
    assert er._spelling_words("live_sync.watcher-x") == ["live", "sync", "watcher", "x"]
    assert isinstance(er._spelling_words("a b"), list)


# -- a version of a thing is not the thing ----------------------------------
#
# ROADMAP item 6's live instance: `Brahmastra`, `brahmastra-v3` and
# `Brahmastra v3` in one node, joined by embedding. `_different_numbers` needs a
# number on BOTH sides; this is the one-sided case.

@pytest.mark.parametrize("a,b", [
    ("Brahmastra", "Brahmastra v3"),
    ("brahmastra", "brahmastra-v3"),
    ("notion-client", "notion-client 3.1.0"),
])
def test_a_release_is_not_the_product(a, b):
    assert er.is_distinct(a, b)


@pytest.mark.parametrize("a,b", [
    ("Brahmastra v3", "brahmastra-v3"),       # the same release, spelled twice
    ("qwen2.5:7b", "qwen2.5:7b-instruct"),    # a variant, not a version
    ("gpt-oss", "gpt-oss-120b"),              # a size is not a version
    ("Python", "Python 3"),                   # a bare number is not a version
])
def test_only_a_version_token_counts(a, b):
    assert not er._version_and_product(a, b)
