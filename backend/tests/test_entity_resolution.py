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


# -- two things an embedding cannot tell apart, and a rule can --------------
#
# Every pair below came out of the LIVE graph, read-only: 925 mentions, 250
# candidate merges before any guard. These are the ones the two new guards
# take away, each checked by hand. The other 237 merges are untouched -- which
# is the point. The resolver was not bad; it was wrong about a handful of
# things in a way nobody could see.


def test_it_refuses_exactly_what_it_should_on_the_real_corpus():
    from brahmastra.entity_resolution import _is_contrasting, is_distinct

    refused = [
        # negation
        ("Do not move the release to April 15th", "Move the release to April 15th"),
        ("nodes and edges", "nodes without edges"),
        # two different MCP tools, one of them merging at jaro 0.951
        ("brahmastra_search_entities", "brahmastra_search_notes"),
        ("brahmastra_create_workspace", "brahmastra_list_workspaces"),
        # two different functions
        ("comprehend_chunk", "comprehend_chunk_focused"),
        ("function run_pipeline", "run_full_pipeline function"),
        ("run_full_pipeline function", "run_pipeline"),
        # two different files
        ("backend/brahmastra/ingest/memo.py", "backend/brahmastra/memo.py"),
        ("mcp_server.py", "tests/test_mcp_server.py"),
        # a config variable and a tool
        ("BRAHMASTRA_API_KEY", "brahmastra_ask"),
        ("BRAHMASTRA_CACHE", "brahmastra_ask"),
    ]
    for a, b in refused:
        assert _is_contrasting(a, b) or is_distinct(a, b), f"{a!r} == {b!r}"


def test_it_leaves_the_merges_that_were_already_right():
    """
    The half that matters more. A guard that refused everything would score
    perfectly on the test above and destroy the resolver.
    """
    from brahmastra.entity_resolution import _is_contrasting, is_distinct

    kept = [
        ("test_checkpoint.py", "tests/test_checkpoint.py"),
        ("app/actions/extract.ts", "frontend/app/actions/extract.ts"),
        ("backend-adapter.ts", "frontend/lib/backend-adapter.ts"),
        ("app/page.tsx", "page.tsx"),
        ("file pipeline.py", "pipeline.py"),
        ("function run_pipeline", "run_pipeline"),
        ("extract_note", "the extract_note function"),
        ("llm_memo table", "llm_memo"),
        ("_retry_delay", "retry_delay"),
        ("SQLite", "SQLite database"),
        ("sentence-transformers", "sentence-transformers model"),
        ("brahmastra_add_note", "tool brahmastra_add_note"),
    ]
    for a, b in kept:
        assert not _is_contrasting(a, b) and not is_distinct(a, b), f"{a!r} == {b!r}"


def test_the_known_overreach_is_deliberate():
    """
    "brahmastra_add_note" and "add_note" are refused although they are
    plausibly one tool named short and long. The obvious fix -- allow it when
    one identifier is a suffix of the other -- was tried and rejected, because
    "mcp_server" is a suffix of "test_mcp_server" and those are a module and
    its test. Pinned so the next person finds the reasoning, not the symptom.
    """
    from brahmastra.entity_resolution import is_distinct

    assert is_distinct("brahmastra_add_note", "add_note")
    assert is_distinct("mcp_server.py", "tests/test_mcp_server.py")
