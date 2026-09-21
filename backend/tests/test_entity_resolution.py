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


# -- a name that denies something is not a longer way of saying it ----------
#
# Found in the LIVE graph, not imagined. 907 mentions, 143 embedding merges,
# and among them:
#
#     0.952   "Do not move the release to April 15th"
#          == "Move the release to April 15th"
#
# One entity. The two halves of a reversed decision, fused in a graph whose
# whole job is recording what was decided.


def test_a_decision_and_its_denial_are_not_one_entity():
    from brahmastra.entity_resolution import _is_contrasting

    assert _is_contrasting("Do not move the release to April 15th",
                           "Move the release to April 15th")


def test_the_pair_rule_could_not_have_caught_it():
    """
    Why a second rule was needed rather than a longer antonym list. The
    original guard fires only when exactly ONE token differs on each side;
    here one side simply carries two extra words, so no list could have
    reached it.
    """
    from brahmastra.entity_resolution import _tokens

    a = _tokens("Do not move the release to April 15th")
    b = _tokens("Move the release to April 15th")
    assert len(a - b) == 2 and len(b - a) == 0


def test_the_antonym_rule_still_works():
    from brahmastra.entity_resolution import _is_contrasting

    assert _is_contrasting("Brahmastra backend", "Brahmastra frontend")


def test_a_longer_form_of_the_same_name_still_merges():
    """The guard must not fire on every pair that differs in length, or it
    would undo the resolution it sits inside."""
    from brahmastra.entity_resolution import _is_contrasting

    assert not _is_contrasting("SQLite", "SQLite database")
    assert not _is_contrasting("function run_pipeline", "run_pipeline")
    assert not _is_contrasting("test_checkpoint.py", "tests/test_checkpoint.py")


def test_it_is_wider_here_than_in_consolidation_on_purpose():
    """
    consolidate.py leaves bare "no" out, because "there is no runbook" is a
    positive assertion OF a risk and splitting it from its own paraphrase
    costs recall. That is about STATEMENTS being scored. These are entity
    NAMES, where the costs run the other way: a spurious extra node is visible
    in the graph and mergeable by hand, a fused assertion-and-denial is not
    visible at all.
    """
    from brahmastra.entity_resolution import _is_contrasting

    assert _is_contrasting("no runbook for the service", "runbook for the service")
