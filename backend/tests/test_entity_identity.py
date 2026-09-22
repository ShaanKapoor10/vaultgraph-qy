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
