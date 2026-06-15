"""
Tests for entity resolution — heuristics only (no sentence-transformers needed).
"""
from __future__ import annotations

import pytest
from brahmastra.entity_resolution import (
    _normalise,
    _tokens,
    _is_acronym_of,
    _heuristic_sim,
    _pick_canonical,
    _UnionFind,
)


def test_normalise():
    assert _normalise("Alice Smith!") == "alice smith"
    assert _normalise("  OpenAI  ") == "openai"


def test_tokens():
    assert _tokens("Alice Smith") == {"alice", "smith"}


def test_acronym_detection():
    assert _is_acronym_of("NLP", "natural language processing")
    assert _is_acronym_of("AI", "artificial intelligence")
    assert not _is_acronym_of("AI", "artificial intelligence research")  # 3 words ≠ 2 letters
    assert not _is_acronym_of("ai", "artificial intelligence")  # must be upper


def test_exact_match():
    sim, method = _heuristic_sim("OpenAI", "OpenAI")
    assert sim == 1.0
    assert method == "exact"


def test_case_insensitive_exact():
    sim, method = _heuristic_sim("openai", "OpenAI")
    assert sim == 1.0
    assert method == "exact"


def test_token_subset():
    sim, method = _heuristic_sim("Alice Smith", "Alice")
    assert method == "token_subset"
    assert sim >= 0.5


def test_acronym_sim():
    sim, method = _heuristic_sim("NLP", "natural language processing")
    assert method == "acronym"
    assert sim == 0.88


def test_unrelated_names():
    sim, method = _heuristic_sim("Alice", "Bob")
    assert sim < 0.85  # should NOT be merged


def test_pick_canonical_prefers_title_case():
    mentions = ["alice", "Alice Smith", "A. Smith"]
    assert _pick_canonical(mentions) == "Alice Smith"


def test_pick_canonical_longest_when_no_title_case():
    mentions = ["alice", "alice smith"]
    assert _pick_canonical(mentions) == "alice smith"


def test_union_find_components():
    uf = _UnionFind(["A", "B", "C", "D"])
    uf.union("A", "B")
    uf.union("C", "D")
    comps = uf.components()
    assert len(comps) == 2
    # flatten
    flat = [set(c) for c in comps]
    assert {"A", "B"} in flat
    assert {"C", "D"} in flat


def test_union_find_single_node():
    uf = _UnionFind(["X"])
    comps = uf.components()
    assert len(comps) == 1
    assert comps[0] == ["X"]


def test_union_find_all_connected():
    uf = _UnionFind(["A", "B", "C"])
    uf.union("A", "B")
    uf.union("B", "C")
    comps = uf.components()
    assert len(comps) == 1
