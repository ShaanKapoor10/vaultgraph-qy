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


def test_the_bridge_no_longer_bridges():
    """
    THIS TEST USED TO PIN THE OPPOSITE, and the history is the point.

    `_path_of` returns None for an extensionless path, so the file rule
    abstained on every pair involving "ingest/cases" -- and six abstentions held
    seven distinct files in one node. `_split_incoherent` was written to clean
    that up after the fact.

    `_file_and_not_file` now refuses those six edges directly: a path merging
    with a name that is not one is a file merging with something that is not a
    file. The cluster is never formed, so there is nothing to split. The split
    stays, because the next bridge will not look like this one.
    """
    bridge = "backend/brahmastra/ingest/cases"
    assert all(is_distinct(bridge, m) for m in INGEST if m != bridge)
    refused = [(a, b) for i, a in enumerate(INGEST) for b in INGEST[i + 1:]
               if is_distinct(a, b)]
    assert len(refused) == 21            # every pair, where it used to be 15


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


# -- the mirror: one file, written short and long ----------------------------
#
# `_different_files` proves two paths are different; the tail exception inside
# it proves two paths are the SAME -- and nothing acted on the second half.
# Measured on the live graph, 18 pairs sat in the knowledge base as TWO NODES
# FOR ONE FILE:
#
#     backend/brahmastra/ingest/memo.py  and  ingest/memo.py        0.640
#     backend/brahmastra/llm.py          and  llm.py                0.550
#     tests/test_checkpoint.py           and  test_checkpoint.py    0.700
#     backend/brahmastra/ingest/evaluate.py and evaluate.py         0.000
#
# Every one below MERGE_THRESHOLD, several scoring nothing at all, because a
# full path and a bare filename share almost no text. The similarity cascade
# was never going to find these, and it does not have to: the path says so.

from brahmastra.entity_resolution import _same_file_groups


def _grouped(mentions):
    return [sorted(mentions[i] for i in g) for g in _same_file_groups(mentions)]


def test_a_bare_filename_joins_its_full_path():
    mentions = sorted(["backend/brahmastra/llm.py", "llm.py"])
    assert _grouped(mentions) == [["backend/brahmastra/llm.py", "llm.py"]]


def test_the_cascade_could_never_have_found_these():
    """The premise, and the reason this is a separate pass."""
    from brahmastra.entity_resolution import MERGE_THRESHOLD, _heuristic_sim

    for a, b in [("backend/brahmastra/llm.py", "llm.py"),
                 ("backend/brahmastra/ingest/evaluate.py", "evaluate.py"),
                 ("tests/test_checkpoint.py", "test_checkpoint.py")]:
        sim, _ = _heuristic_sim(a, b)
        assert sim < MERGE_THRESHOLD, f"{a!r}/{b!r} already merged at {sim}"


def test_an_ambiguous_short_name_is_refused():
    """
    THE REASON THIS IS A CORPUS-LEVEL PASS. 'memo.py' is a tail of BOTH
    'brahmastra/memo.py' and 'brahmastra/ingest/memo.py', which
    `_different_files` proves are two files -- so merging on the bare name
    would fuse them through it. A pairwise rule cannot see that.
    """
    mentions = sorted(["memo.py", "brahmastra/memo.py",
                       "brahmastra/ingest/memo.py"])
    assert _grouped(mentions) == []


def test_an_ambiguity_that_resolves_is_still_merged():
    """
    'extract.ts' matches two longer paths on the live corpus, and those two are
    themselves tail-related -- one file, three spellings. The guard must refuse
    only genuine ambiguity, or it would undo the whole point.

    Groups OVERLAP by design: the short path forms one and the middle path
    forms another. The caller feeds them all to Union-Find, so what matters is
    that the three end up connected, not that one group holds them.
    """
    mentions = sorted(["extract.ts", "app/actions/extract.ts",
                       "frontend/app/actions/extract.ts"])
    groups = _grouped(mentions)
    assert groups, "the resolvable ambiguity must not be refused"

    reachable = set(groups[0])
    changed = True
    while changed:
        changed = False
        for group in groups:
            if reachable & set(group) and not set(group) <= reachable:
                reachable |= set(group)
                changed = True
    assert reachable == set(mentions)


def test_two_files_sharing_a_basename_are_never_grouped():
    mentions = sorted(["src/a/util.py", "src/b/util.py"])
    assert _grouped(mentions) == []


def test_a_group_never_contains_a_pair_the_guards_refuse():
    """The same invariant `_split_incoherent` enforces, checked at the source."""
    mentions = sorted([
        "backend/brahmastra/llm.py", "llm.py",
        "backend/brahmastra/memo.py", "brahmastra/memo.py",
        "backend/brahmastra/ingest/memo.py", "ingest/memo.py",
        "tests/test_checkpoint.py", "test_checkpoint.py",
        "app/page.tsx", "page.tsx",
    ])
    for group in _grouped(mentions):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                assert not is_distinct(a, b), f"{a!r} grouped with {b!r}"


def test_a_mention_with_no_path_is_ignored():
    assert _grouped(sorted(["Sarah", "the release", "a decision"])) == []


# -- a file is not the thing it implements -----------------------------------
#
# cocoindex's conversation example resolves each entity TYPE separately, so a
# person can never merge with an org. Measured here and rejected as-is: 29 of 73
# merges cross a type boundary and about half are RIGHT, because our types are
# assigned by the model per triple ('Groq' is an organisation in one triple and
# a tool in the next). What IS reliable is syntax -- so the separation is drawn
# on the one type boundary structure proves. 12 merges paired a path with a
# non-path on the live graph; all 12 were wrong.

from brahmastra.entity_resolution import _file_and_not_file

MEASURED_FILE_VS_NAME = [
    ("entity_resolution.py", "entity resolution", 0.948),   # the concept it implements
    ("sqlite_store.py", "SQLiteStore", 0.947),              # the class it defines
    ("neo4j_store.py", "Neo4jStore", 0.943),
    ("entity_resolution.py", "entity_resolution stage", 0.937),
    ("brahmastra.llm", "brahmastra-v3", 0.926),             # a module and a branch
    ("CLAUDE.md", "Claude Code", 0.923),                    # a file and a product
]


@pytest.mark.parametrize("path,name,score", MEASURED_FILE_VS_NAME)
def test_a_file_is_not_the_thing_it_is_about(path, name, score):
    assert _file_and_not_file(path, name), f"{path!r} == {name!r} merged at {score}"
    assert is_distinct(path, name)


def test_the_rule_is_symmetric():
    assert _file_and_not_file("SQLiteStore", "sqlite_store.py")


def test_two_paths_are_left_to_the_path_rules():
    """Path against path is `_different_files` and `_same_file_groups` --
    this rule must not second-guess either of them."""
    assert not _file_and_not_file("app/page.tsx", "page.tsx")
    assert not _file_and_not_file("llm.py", "memo.py")


def test_two_names_are_left_alone():
    """No path on either side: nothing structural to say."""
    assert not _file_and_not_file("Groq", "groq")
    assert not _file_and_not_file("Sarah", "Sarah Chen")


def test_the_right_cross_type_merges_still_happen():
    """
    The reason per-type resolution was rejected. These pairs carry DIFFERENT
    model-assigned types on the live graph and are the same thing all the
    same -- a syntactic rule must leave them alone.
    """
    for a, b in [("Groq", "groq"),                              # organisation / tool
                 ("_ask function", "function _ask"),            # concept / tool
                 ("Multi-hop GraphRAG", "multi-hop GraphRAG"),  # concept / feature
                 ("SQLite deployment", "SQLite deployments")]:  # concept / project
        assert not is_distinct(a, b), f"{a!r} and {b!r} are one thing"
