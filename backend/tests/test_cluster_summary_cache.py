"""
Cluster summaries are reused when the cluster has not changed.

One LLM call per cluster, recomputed on every pipeline run however little had
changed: 25 calls a run, and the reason one incremental run exceeded a
30-minute timeout on this stage alone.

The subtle part is the cache KEY. Louvain numbers communities per run, so the
same set of entities routinely reappears under a different id -- keying on id
would hand a cached summary to the wrong cluster, which is worse than paying to
regenerate it.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "clusters.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    import brahmastra.concept_graph as cg
    importlib.reload(cg)
    return db_mod


def _cache(db, clusters):
    db.cache_graph({"nodes": [], "edges": []}, {"concept_clusters": clusters})


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

def test_membership_identifies_a_cluster_regardless_of_its_id(temp_db):
    from brahmastra.concept_graph import _membership_key

    assert _membership_key(["b", "a"]) == _membership_key(["a", "b"])
    assert _membership_key(["a", "b"]) != _membership_key(["a", "b", "c"])
    # An empty join would collide these two.
    assert _membership_key(["ab", "c"]) != _membership_key(["a", "bc"])


def test_a_summary_is_carried_across_a_rebuild_when_membership_is_unchanged(temp_db):
    from brahmastra.concept_graph import _previous_summaries_by_membership, _membership_key

    _cache(temp_db, [{"id": 7, "members": ["a", "b"], "size": 2, "summary": "About A and B."}])

    carried = _previous_summaries_by_membership()
    assert carried[_membership_key(["a", "b"])] == "About A and B."


def test_a_cluster_without_a_summary_carries_nothing(temp_db):
    from brahmastra.concept_graph import _previous_summaries_by_membership

    _cache(temp_db, [{"id": 1, "members": ["a"], "size": 1, "summary": ""}])
    assert _previous_summaries_by_membership() == {}


def test_no_cached_graph_is_not_an_error(temp_db):
    """A cold start just resummarises everything -- slow, not broken."""
    from brahmastra.concept_graph import _previous_summaries_by_membership

    assert _previous_summaries_by_membership() == {}


# ---------------------------------------------------------------------------
# The saving
# ---------------------------------------------------------------------------

def test_an_unchanged_cluster_costs_no_llm_call(temp_db, monkeypatch):
    from brahmastra import cluster_summary as cs

    calls = []
    monkeypatch.setattr(cs, "llm_available", lambda: True)
    monkeypatch.setattr(cs, "_summarise_one",
                        lambda m, e: calls.append(m) or "fresh summary")
    monkeypatch.setattr(cs, "MIN_CLUSTER_SIZE", 1)

    clusters = [
        {"id": 1, "members": ["a", "b"], "size": 2, "summary": "carried over"},
        {"id": 2, "members": ["c", "d"], "size": 2},          # never summarised
    ]
    out = cs.summarise_clusters(clusters, edges=[])

    assert out[1] == "carried over", "an unchanged cluster must reuse its summary"
    assert out[2] == "fresh summary"
    assert calls == [["c", "d"]], f"only the new cluster may cost a call, got {calls}"


def test_a_changed_cluster_is_resummarised(temp_db, monkeypatch):
    """
    A summary describes the members it saw, so any membership change
    invalidates it. concept_graph enforces this by not carrying the summary
    forward; here the cluster simply arrives without one.
    """
    from brahmastra import cluster_summary as cs

    calls = []
    monkeypatch.setattr(cs, "llm_available", lambda: True)
    monkeypatch.setattr(cs, "_summarise_one",
                        lambda m, e: calls.append(m) or "regenerated")
    monkeypatch.setattr(cs, "MIN_CLUSTER_SIZE", 1)

    out = cs.summarise_clusters([{"id": 9, "members": ["a", "b", "NEW"], "size": 3}], edges=[])

    assert out[9] == "regenerated"
    assert len(calls) == 1


def test_a_renumbered_cluster_still_reuses_its_summary(temp_db):
    """
    Louvain renumbers communities between runs. Keying the cache on id would
    both miss this reuse AND risk handing the summary to a different cluster
    that happened to inherit the number.
    """
    from brahmastra.concept_graph import _membership_key, _previous_summaries_by_membership

    _cache(temp_db, [{"id": 3, "members": ["x", "y"], "size": 2, "summary": "About X and Y."}])

    carried = _previous_summaries_by_membership()
    # Same members, new id 88 -- the lookup must still hit.
    assert carried.get(_membership_key(["y", "x"])) == "About X and Y."


# ---------------------------------------------------------------------------
# Running out of model must not cost a summary that needs no model
# ---------------------------------------------------------------------------
#
# summarise_clusters returned {} when no provider was usable, and the caller
# blanks every cluster it is not handed back -- so a run without quota ERASED
# every carried summary. The quota path's `break` did the same to every
# carried summary ranked below the cluster that hit the cap.

def _two_clusters(db):
    _cache(db, [
        {"id": 1, "members": ["a", "b", "c"], "size": 3, "summary": ""},
        {"id": 2, "members": ["d", "e"], "size": 2, "summary": "About D and E."},
    ])


def test_no_provider_keeps_carried_summaries(temp_db, monkeypatch):
    import brahmastra.cluster_summary as cs
    monkeypatch.setattr(cs, "llm_available", lambda: False)
    _two_clusters(temp_db)

    report = cs.run_cluster_summaries()

    by_id = {c["id"]: c["summary"] for c in temp_db.get_cached_graph()["stats"]["concept_clusters"]}
    assert by_id == {1: "", 2: "About D and E."}
    assert report["generated"] == 0 and report["reused"] == 1


def test_quota_running_out_keeps_the_carried_summaries_ranked_below(temp_db, monkeypatch):
    import brahmastra.cluster_summary as cs
    from brahmastra.llm import LLMQuotaExhausted

    def spent(*a, **k):
        raise LLMQuotaExhausted("daily cap")

    monkeypatch.setattr(cs, "llm_available", lambda: True)
    monkeypatch.setattr(cs, "_summarise_one", spent)
    _two_clusters(temp_db)              # the uncarried cluster ranks FIRST

    cs.run_cluster_summaries()

    by_id = {c["id"]: c["summary"] for c in temp_db.get_cached_graph()["stats"]["concept_clusters"]}
    assert by_id[2] == "About D and E."


def test_quota_stops_generation_after_the_first_refusal(temp_db, monkeypatch):
    import brahmastra.cluster_summary as cs
    from brahmastra.llm import LLMQuotaExhausted
    calls = []

    def spent(*a, **k):
        calls.append(1)
        raise LLMQuotaExhausted("daily cap")

    monkeypatch.setattr(cs, "llm_available", lambda: True)
    monkeypatch.setattr(cs, "_summarise_one", spent)
    _cache(temp_db, [{"id": i, "members": [f"x{i}", f"y{i}"], "size": 2, "summary": ""}
                     for i in range(5)])
    cs.run_cluster_summaries()
    assert len(calls) == 1
