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
    # Was PROVIDERS / provider_status, which Jaro-Winkler no longer merges:
    # an extra word is not a spelling variant (test_entity_spelling.py).
    ("PostgreSQL", "Postgres", "jaro_winkler"),
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


# -- the embedding stage, which blocking does NOT help -----------------------
#
# Blocking cuts the heuristic path because it only needs CANDIDATES. The
# embedding path needs every pair's score, and `embeddings @ embeddings.T`
# builds an n x n matrix -- so the wall it hits first is memory, and it arrives
# suddenly:
#
#        1,000 mentions       1M entries      4 MB
#       10,000              100M            400 MB
#       50,000              2.5B             10 GB
#
# Row blocks bound the peak without changing the answer. An ANN index is the
# real fix above that, and is what cocoindex reaches for.

import numpy as np


class _FakeEmbedder:
    """Deterministic vectors, so the comparison is exact and needs no model."""

    def encode(self, texts, normalize_embeddings=True):
        rng = np.random.default_rng(7)
        v = rng.normal(size=(len(texts), 16)).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def _square_way(mentions, embeddings, threshold):
    """The n x n version this replaced, kept as the thing to agree with."""
    matrix = embeddings @ embeddings.T
    out = {}
    for i in range(len(mentions)):
        for j in range(i + 1, len(mentions)):
            score = float(matrix[i, j])
            if score >= threshold:
                out[(mentions[i], mentions[j])] = score
    return out


@pytest.fixture
def fake_embedder(monkeypatch):
    import brahmastra.entity_resolution as er

    model = _FakeEmbedder()
    monkeypatch.setattr(er, "_get_embedder", lambda: model)
    return model


@pytest.mark.parametrize("count,block", [(600, 512), (1300, 512), (1000, 7),
                                         (1000, 1)])
def test_row_blocks_find_exactly_the_same_pairs(monkeypatch, fake_embedder,
                                                count, block):
    """
    The pair SET must match exactly, at any block size -- including one that
    does not divide the corpus, and one of a single row.

    The scores differ in the last bits (about 1e-7 on float32), because a
    blocked matrix product sums in a different order. That is inherent to any
    blocking and is why this compares which pairs came back, not their
    floating-point values.
    """
    import brahmastra.entity_resolution as er

    monkeypatch.setattr(er, "_EMBED_BLOCK", block)
    mentions = [f"mention number {i}" for i in range(count)]

    blocked = er._embedding_sim(mentions)
    reference = _square_way(mentions, fake_embedder.encode(mentions),
                            er.EMBEDDING_THRESHOLD)

    assert set(blocked) == set(reference)
    assert reference, "pick data that actually produces pairs"
    assert max(abs(blocked[k] - reference[k]) for k in reference) < 1e-5


def test_a_lost_embedder_says_so(monkeypatch):
    """
    Losing embeddings must DEGRADE the run, never fail it -- the heuristics
    still work. But it must not degrade silently: without them the resolver
    stops finding every merge that needs meaning rather than spelling, and the
    only symptom is a graph with more nodes than it should have.
    """
    import brahmastra.entity_resolution as er

    monkeypatch.setattr(er, "_get_embedder", lambda: None)
    assert er._embedding_sim(["a", "b"]) == {}
    assert "unavailable" in er._embedding_error


def test_a_failure_mid_computation_says_why(monkeypatch):
    """MemoryError is the one this will actually meet, and it used to come
    back as 'no similar pairs'."""
    import brahmastra.entity_resolution as er

    class Broken:
        def encode(self, texts, normalize_embeddings=True):
            raise MemoryError("Unable to allocate 10.4 GiB")

    monkeypatch.setattr(er, "_get_embedder", lambda: Broken())
    assert er._embedding_sim(["a", "b"]) == {}
    assert "MemoryError" in er._embedding_error


def test_success_clears_the_last_failure(monkeypatch, fake_embedder):
    """A stale reason is worse than none -- it would report a healthy run as
    degraded forever."""
    import brahmastra.entity_resolution as er

    er._embedding_error = "something from before"
    er._embedding_sim([f"mention {i}" for i in range(20)])
    assert er._embedding_error == ""
