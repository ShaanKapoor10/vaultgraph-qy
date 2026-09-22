"""
The graph must not rename its own entities between two identical runs.

MEASURED ON THE LIVE GRAPH, 970 triples and 901 clusters, by running the
resolver twice in two PROCESSES over byte-identical input:

    cluster ids naming the same members       2 of 901
    mentions under a different canonical name 14 of 980

Two out of nine hundred and one. Mentions are collected into a SET, set
iteration order for strings depends on the hash seed, and the hash seed differs
per process -- so `f"c{i:04d}"` was numbering clusters by luck, and
`max(pool, key=len)` was breaking ties by the same luck.

Growing the corpus was the same story: clustering 70% of the notes and then all
of them left 1 of 667 positional ids intact, against 645 of 667 derived ones.

cocoindex's id_generation page states the consequence plainly -- ids that are
not derived from the data mean every reprocessing run produces different ids
for the same data, so the target churns: old rows deleted, identical rows
re-inserted under new keys.
"""
from __future__ import annotations

from brahmastra.entity_resolution import _pick_canonical, cluster_id_for

# Real pairs from the live graph, each of which flipped between two runs.
FLIPPED = [
    ["function run_pipeline", "run_pipeline function"],
    ["Apollo Project", "Apollo project"],
    ["function _ask", "_ask function"],
    ["GraphRAG question answering", "GraphRAG Question Answering"],
]


# -- the cluster id ----------------------------------------------------------

def test_the_id_comes_from_the_members_not_their_order():
    assert cluster_id_for(["b", "a", "c"]) == cluster_id_for(["c", "b", "a"])


def test_a_different_membership_is_a_different_cluster():
    """
    Correct, and deliberate. A cluster that gained a mention is not the cluster
    that existed before -- the same rule cluster summaries already follow, for
    the same reason.
    """
    assert cluster_id_for(["a", "b"]) != cluster_id_for(["a", "b", "c"])


def test_the_id_cannot_be_confused_by_concatenation():
    assert cluster_id_for(["ab", "c"]) != cluster_id_for(["a", "bc"])


def test_the_id_does_not_depend_on_this_process():
    """
    The one that matters, and the one a single-process test cannot see. Run in
    a FRESH interpreter with a different hash seed: a positional id changes,
    a derived id does not.
    """
    import json
    import os
    import subprocess
    import sys

    script = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "from brahmastra.entity_resolution import cluster_id_for;"
        "members={'Neo4j','neo4j','Neo4j Aura','Postgres','postgres','pgvector'};"
        "print(json.dumps([cluster_id_for(members), list(members)]))"
        % str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    )

    def run(seed):
        env = dict(os.environ, PYTHONHASHSEED=seed, BRAHMASTRA_NO_DOTENV="1")
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, env=env, timeout=120)
        assert out.returncode == 0, out.stderr[-800:]
        return json.loads(out.stdout.strip().splitlines()[-1])

    first_id, first_order = run("1")
    second_id, second_order = run("7")

    # The premise: the seed really does change the iteration order.
    assert first_order != second_order, "pick seeds that actually differ"
    # The property.
    assert first_id == second_id


# -- the canonical name ------------------------------------------------------

def test_a_tie_is_broken_the_same_way_every_time():
    """
    `max(pool, key=len)` returns the first longest item in ITERATION ORDER, and
    that order came from a set. Each of these pairs genuinely flipped between
    two runs over identical data.
    """
    for pair in FLIPPED:
        assert _pick_canonical(pair) == _pick_canonical(list(reversed(pair)))


def test_the_longest_specific_name_still_wins():
    """The tie-break must not have changed what happens when there is no tie."""
    assert _pick_canonical(["Sarah", "Sarah Chen"]) == "Sarah Chen"
    assert _pick_canonical(["pipeline.py", "file pipeline.py"]) == "file pipeline.py"


def test_a_proper_noun_still_beats_a_longer_lowercase_one():
    assert _pick_canonical(["Neo4j", "the graph database we use"]) == "Neo4j"


def test_a_cluster_with_no_proper_noun_still_gets_a_name():
    assert _pick_canonical(["cache", "the reply cache"]) == "the reply cache"


# -- PINNED: a name that already won keeps winning ---------------------------
#
# MEASURED by simulating corpus growth on the live graph -- cluster 70% of the
# notes, take that run's canonical map as `existing`, then cluster all of them:
#
#     renames after growth, heuristic alone   9 of 727 mentions
#     renames after growth, pinned            0
#
# Pinning changed the answer in 8 of 676 clusters, and kept the better name in
# every one. The pairs below are those eight, verbatim.

MEASURED_WINS = [
    ("Shaan Kapoor", "ShaanKapoor10"),          # a person renamed to a handle
    ("CocoIndex", "Cocoindex"),                 # correct casing, lost
    ("2026-08-12", "2026-08-18"),               # a DIFFERENT DATE
    ("embedding model", "embeddings.get_model"),
    ("decision", "decisions"),
    ("action_item", "action items"),
    ("SQLite deployment", "SQLite deployments"),
    ("function run_pipeline", "run_pipeline function"),
]


def test_the_heuristic_alone_renames_all_eight():
    """The premise. Without pinning every one of these flips on corpus growth."""
    for established, newcomer in MEASURED_WINS:
        assert _pick_canonical([established, newcomer]) == newcomer


def test_pinning_keeps_every_one_of_them():
    for established, newcomer in MEASURED_WINS:
        assert _pick_canonical([established, newcomer], {established}) == established


def test_a_fuller_form_of_the_same_name_still_wins():
    """
    The one escape hatch, and the case pinning alone would get wrong: a cluster
    first seen as "Sarah" that later gains "Sarah Chen" should take the fuller
    name. A STRICT word-superset only -- reasoned rather than observed, because
    corpus growth did not produce one, and deliberately narrow enough that it
    promotes none of the eight above.
    """
    assert _pick_canonical(["Sarah", "Sarah Chen"], {"Sarah"}) == "Sarah Chen"
    assert _pick_canonical(["pipeline.py", "file pipeline.py"],
                           {"pipeline.py"}) == "file pipeline.py"


def test_a_respelling_is_not_a_fuller_form():
    """Plurals, casing and reorderings are the same name written differently --
    which is exactly what pinning exists to stop flapping between."""
    assert _pick_canonical(["decision", "decisions"], {"decision"}) == "decision"
    assert _pick_canonical(["CocoIndex", "cocoindex", "Cocoindex"],
                           {"CocoIndex"}) == "CocoIndex"
    assert _pick_canonical(["function run_pipeline", "run_pipeline function"],
                           {"function run_pipeline"}) == "function run_pipeline"


def test_a_cluster_with_no_established_name_falls_back_to_the_heuristic():
    assert _pick_canonical(["Sarah", "Sarah Chen"], {"somebody else"}) == "Sarah Chen"
    assert _pick_canonical(["Sarah", "Sarah Chen"], set()) == "Sarah Chen"


def test_pinning_is_still_deterministic():
    """Two established names in one cluster is two entities merging. It never
    happened while this was measured, so it falls back to the heuristic among
    them -- but it must not fall back to luck."""
    both = {"function _ask", "_ask function"}
    a = _pick_canonical(["function _ask", "_ask function"], both)
    b = _pick_canonical(["_ask function", "function _ask"], both)
    assert a == b


def test_it_can_be_switched_off(monkeypatch):
    """
    ENTITY_PINNED=0, which is also how a name frozen by mistake gets re-picked:
    pinning reads the previous run's answer, so nothing else would ever let go
    of it.
    """
    from brahmastra.entity_resolution import pinned_enabled

    monkeypatch.setenv("ENTITY_PINNED", "0")
    assert pinned_enabled() is False
    assert _pick_canonical(["Shaan Kapoor", "ShaanKapoor10"],
                           {"Shaan Kapoor"}) == "ShaanKapoor10"
