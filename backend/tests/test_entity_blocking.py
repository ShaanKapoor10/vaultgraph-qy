"""
Blocking must not change the answer. It may only change how long it takes.

MEASURED on the live graph -- 1007 mentions, 506,521 pairs:

    all-pairs   5.11s  ->  111 merges
    blocked     0.85s  ->  111 merges     (7,228 candidates, 1.43% of pairs)

Identical merge set, 6x faster. That equivalence is the whole contract, and it
is what these tests exist to keep: a blocker that quietly covers three of the
four match methods loses a whole CLASS of merge, and a lost merge is invisible.

The cost is quadratic, which is why this exists at all:

       1,000 mentions        506,521 pairs         5s
       5,000              12,497,500           125s
      10,000              49,995,000           501s
      50,000           1,249,975,000        12,517s
"""
from __future__ import annotations

import pytest

from brahmastra.entity_resolution import (
    MERGE_THRESHOLD,
    _candidate_pairs,
    _heuristic_sim,
    _pairs_to_compare,
)

# One of each match method, plus names chosen to sit near the boundary.
CORPUS = [
    "Sarah", "Sarah Chen", "sarah chen",            # exact
    "Brahmastra knowledge graph engine for notes",   # token_subset: 6 of 7
    "the Brahmastra knowledge graph engine for notes",
    "MCP", "Model Context Protocol",                 # acronym
    "PROVIDERS", "provider_status",                  # jaro at exactly 0.92
    "backend/brahmastra/llm.py", "backend/brahmastra/memo.py",
    "pipeline.py", "file pipeline.py",
    "CLAUDE.md", "Claude Code",
    "Neo4j", "Neo4j Aura", "neo4j",
    "threshold 0.55", "threshold 0.60",
    "run_pipeline", "run_full_pipeline", "function run_pipeline",
    "GraphRAG", "Microsoft GraphRAG",
    "entity_resolution.py", "entity_resolution stage",
    "brahmastra-v3", "brahmastra.llm", "Brahmastra frontend",
    "a wholly unrelated sentence about nothing at all",
]


def _merges(mentions, pairs):
    found = {}
    for i, j in pairs:
        sim, method = _heuristic_sim(mentions[i], mentions[j])
        if sim >= MERGE_THRESHOLD:
            found[(mentions[i], mentions[j])] = (round(sim, 9), method)
    return found


def _all_pairs(mentions):
    return ((i, j) for i in range(len(mentions))
            for j in range(i + 1, len(mentions)))


def test_blocking_finds_exactly_what_all_pairs_finds():
    """The contract. Not 'nearly all', not 'the important ones'."""
    assert (_merges(CORPUS, _candidate_pairs(CORPUS))
            == _merges(CORPUS, _all_pairs(CORPUS)))


@pytest.mark.parametrize("a,b,method", [
    ("sarah chen", "Sarah Chen", "exact"),
    ("Brahmastra knowledge graph engine for notes",
     "the Brahmastra knowledge graph engine for notes", "token_subset"),
    ("MCP", "Model Context Protocol", "acronym"),
    ("PROVIDERS", "provider_status", "jaro_winkler"),
])
def test_every_match_method_survives_blocking(a, b, method):
    """
    One test per method, because a blocker covering three of four is the
    failure this is guarding against -- and it would pass any test that only
    counted how many merges came back.
    """
    mentions = sorted(set(CORPUS) | {a, b})
    sim, found = _heuristic_sim(a, b)
    assert found == method and sim >= MERGE_THRESHOLD, "the premise changed"

    pairs = {(mentions[i], mentions[j]) for i, j in _candidate_pairs(mentions)}
    assert (a, b) in pairs or (b, a) in pairs


def test_the_pair_that_sits_exactly_on_the_threshold():
    """
    'PROVIDERS' against 'provider_status' scores exactly JARO_THRESHOLD, needs
    exactly 9 matching characters and has exactly 9 -- and was dropped, because
    the bound computed 9.000000000000002 in floating point. Rounding must
    always err towards offering a candidate.
    """
    mentions = ["PROVIDERS", "provider_status"]
    assert list(_candidate_pairs(mentions)) == [(0, 1)]


def test_a_pair_that_cannot_match_is_not_offered():
    """It is a filter, so it has to filter something."""
    mentions = sorted(CORPUS)
    total = len(mentions) * (len(mentions) - 1) // 2
    assert len(list(_candidate_pairs(mentions))) < total


def test_the_candidate_list_is_stable():
    """Same property as everything else here: no dependence on iteration order."""
    forward = list(_candidate_pairs(CORPUS))
    assert forward == list(_candidate_pairs(CORPUS))
    assert forward == sorted(forward)


def test_a_small_corpus_compares_every_pair():
    """
    Below the cut nothing is blocked. 5 seconds at a thousand mentions is not
    worth trading for a filter that only ALMOST covers the methods, and keeping
    the simple path live keeps the equivalence test honest.
    """
    mentions = sorted(CORPUS)
    total = len(mentions) * (len(mentions) - 1) // 2
    assert len(list(_pairs_to_compare(mentions))) == total


def test_a_large_corpus_is_blocked(monkeypatch):
    import brahmastra.entity_resolution as er

    monkeypatch.setattr(er, "BLOCKING_MIN_MENTIONS", 2)
    mentions = sorted(CORPUS)
    total = len(mentions) * (len(mentions) - 1) // 2
    assert len(list(er._pairs_to_compare(mentions))) < total


def test_an_empty_name_does_not_break_the_bound():
    """Division by a zero length. Reached through a mention that normalises away."""
    assert list(_candidate_pairs(["!!!", "???"])) is not None
    assert list(_candidate_pairs([])) == []
