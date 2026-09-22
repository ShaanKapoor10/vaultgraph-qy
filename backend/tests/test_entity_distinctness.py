"""
A third of every merge the resolver made was two different things.

MEASURED ON THE LIVE GRAPH -- 1007 mentions, 105 merges -- and every refusal
below was checked by hand:

    two different files     32 of 105
    two different numbers    3 of 105

Jaro-Winkler is the reason. It rewards a long shared prefix, and every file in
one directory has one, so `backend/brahmastra/llm.py` and
`backend/brahmastra/memo.py` score 0.961. Fifteen source files had fused into a
SINGLE entity -- among them the three ontology files CLAUDE.md exists to keep
in step, and the `.env`/`.venv` pair whose confusion it records as a real
incident.

None of this is a threshold problem. Two paths that are not tails of each other
are two files, and no similarity score is evidence against that.

The remaining hole was TRANSITIVITY: Union-Find merges A with C when A~B and
B~C are confirmed, and nothing ever judges that pair. One extensionless path
held seven distinct files together after the rules above had refused fifteen of
their twenty-one internal pairs.
"""
from __future__ import annotations

import pytest

from brahmastra.entity_resolution import (
    _different_files,
    _different_numbers,
    _split_incoherent,
    is_distinct,
)

# Verbatim from the live graph, with the score Jaro-Winkler gave each pair.
MEASURED_FILE_MERGES = [
    ("backend/brahmastra/llm.py", "backend/brahmastra/memo.py", 0.961),
    ("backend/brahmastra/ingest/memo.py", "backend/brahmastra/ingest/store.py", 0.964),
    ("backend/.env", "backend/.venv", 0.965),
    ("ontology.py", "ontology.yaml", 0.936),
    ("ontology.py", "ontology.ts", 0.927),
    ("checkpoint.log", "checkpoint.py", 0.926),
    ("backend/brahmastra/embeddings.py", "backend/brahmastra/ingest/assemble.py", 0.923),
]

MEASURED_NUMBER_MERGES = [
    ("2026-08-12", "2026-08-18", 0.960),
    ("notion-client version 2.2.1", "notion-client version 3.1.0", 0.962),
    ("threshold 0.55", "threshold 0.60", 0.943),
]


# -- files -------------------------------------------------------------------

@pytest.mark.parametrize("a,b,score", MEASURED_FILE_MERGES)
def test_two_different_paths_are_two_files(a, b, score):
    assert _different_files(a, b), f"{a!r} == {b!r} merged at {score}"
    assert is_distinct(a, b)


def test_a_path_that_is_a_tail_of_another_is_one_file():
    """
    The only way two different path strings can name the same thing, and the
    behaviour the old rule existed to protect. It must survive the widening.
    """
    assert not _different_files("page.tsx", "app/page.tsx")
    assert not _different_files("src/components/Graph.tsx", "Graph.tsx")


def test_the_same_file_written_two_ways_is_one_file():
    assert not _different_files("pipeline.py", "file pipeline.py")
    assert not _different_files("the file store.py", "store.py")


def test_a_name_that_is_not_a_path_is_not_judged():
    """Every guard here abstains rather than guessing. `.env` has no basename
    the old rule could compare, and prose is not a path at all."""
    assert not _different_files("Sarah", "Sarah Chen")
    assert not _different_files("the release", "backend/llm.py")


def test_the_same_basename_in_two_directories_is_still_refused():
    """The case the original rule was written for."""
    assert _different_files("backend/brahmastra/ingest/memo.py",
                            "backend/brahmastra/memo.py")
    assert _different_files("src/a/util.py", "src/b/util.py")


# -- numbers -----------------------------------------------------------------

@pytest.mark.parametrize("a,b,score", MEASURED_NUMBER_MERGES)
def test_two_different_numbers_are_two_facts(a, b, score):
    assert _different_numbers(a, b), f"{a!r} == {b!r} merged at {score}"
    assert is_distinct(a, b)


def test_the_version_pair_claude_md_records_as_a_real_bug():
    """
    notion-client 2.2.1 has `databases.query` and 3.1.0 does not, which is why
    the sync branches on the capability. Fusing those two nodes destroys the
    distinction the note exists to make.
    """
    assert is_distinct("notion-client version 2.2.1", "notion-client version 3.1.0")


def test_the_same_number_written_twice_is_not_a_difference():
    assert not _different_numbers("port 8001", "the backend on port 8001")
    assert not _different_numbers("Neo4j", "Neo4j Aura")


def test_the_rule_abstains_when_only_one_side_has_a_number():
    """A count and the thing counted are not two counts."""
    assert not _different_numbers("44 notes", "notes")
    assert not _different_numbers("Sarah", "Sarah 2")  # only one side, in reverse
    assert not _different_numbers("release", "release 2")


def test_a_model_size_is_a_number_too():
    assert is_distinct("gpt-oss-120b", "gpt-oss-20b")


# -- transitivity ------------------------------------------------------------

INGEST = [
    "backend/brahmastra/ingest/assemble.py",
    "backend/brahmastra/ingest/cases",          # no extension -> the bridge
    "backend/brahmastra/ingest/comprehend.py",
    "backend/brahmastra/ingest/evaluate.py",
    "backend/brahmastra/ingest/evidence.py",
    "backend/brahmastra/ingest/memo.py",
    "backend/brahmastra/ingest/store.py",
]


def test_the_bridge_really_does_bridge():
    """
    The premise. `_path_of` returns None for an extensionless path, so the file
    rule ABSTAINS on every pair involving it -- and six abstentions were enough
    to hold seven distinct files in one node.
    """
    bridge = "backend/brahmastra/ingest/cases"
    assert not any(is_distinct(bridge, m) for m in INGEST if m != bridge)
    refused = [(a, b) for i, a in enumerate(INGEST) for b in INGEST[i + 1:]
               if is_distinct(a, b)]
    assert len(refused) == 15


def test_a_cluster_never_keeps_a_pair_the_guards_refuse():
    parts = _split_incoherent(INGEST, {})
    assert len(parts) > 1
    for part in parts:
        for i, a in enumerate(part):
            for b in part[i + 1:]:
                assert not is_distinct(a, b), f"{a!r} still with {b!r}"


def test_a_coherent_cluster_is_left_alone():
    """Splitting must be driven by a refusal, not by size."""
    members = ["Sarah", "Sarah Chen", "Sarah C"]
    assert _split_incoherent(members, {}) == [sorted(members)]


def test_a_pair_is_never_split():
    """Two members cannot have been fused THROUGH anything."""
    assert _split_incoherent(["a.py", "b.py"], {}) == [["a.py", "b.py"]]


def test_the_split_does_not_depend_on_order():
    """
    Same property the cluster id and the canonical name needed. The greedy pass
    ranks edges by similarity and breaks ties on the names, so two orderings of
    the same component give the same answer.
    """
    forward = _split_incoherent(INGEST, {})
    backward = _split_incoherent(list(reversed(INGEST)), {})
    assert forward == backward


def test_the_strongest_evidence_survives_the_split():
    """
    Greedy, strongest first. Of two accepted edges that cannot both stand, the
    higher-scoring one is the one kept -- so the split loses the weaker claim,
    not an arbitrary one.
    """
    members = ["BRAHMASTRA_CACHE", "Brahmastra", "brahmastra_ask"]
    assert is_distinct("BRAHMASTRA_CACHE", "brahmastra_ask")

    keep_cache = _split_incoherent(members, {
        ("BRAHMASTRA_CACHE", "Brahmastra"): 0.99,
        ("Brahmastra", "brahmastra_ask"): 0.80,
    })
    assert ["BRAHMASTRA_CACHE", "Brahmastra"] in keep_cache

    keep_ask = _split_incoherent(members, {
        ("BRAHMASTRA_CACHE", "Brahmastra"): 0.80,
        ("Brahmastra", "brahmastra_ask"): 0.99,
    })
    assert ["Brahmastra", "brahmastra_ask"] in keep_ask
