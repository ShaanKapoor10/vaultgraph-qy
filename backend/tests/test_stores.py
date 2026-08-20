"""
Tests for the GraphStore contract.

Covers the SQLite backend only — the Neo4j backend needs live credentials, so
its parity with these results is verified manually against Aura. What is
locked in here is the contract itself: any backend must return these shapes,
which is what lets rag.py stay backend-agnostic.
"""

from __future__ import annotations

import pytest

from brahmastra import db
from brahmastra.stores import backend_name, get_store, reset_store
from brahmastra.stores.base import GraphStore
from brahmastra.stores.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Fresh DB per test, and a store cache that cannot leak between them."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    reset_store()
    db.init_db()
    yield
    reset_store()


GRAPH = {
    "nodes": [
        {"id": "Sarah", "label": "Sarah", "type": "person", "pagerank": 0.4, "cluster": 1},
        {"id": "Mei", "label": "Mei", "type": "person", "pagerank": 0.3, "cluster": 1},
        {"id": "Apollo", "label": "Apollo", "type": "project", "pagerank": 0.2, "cluster": 2},
    ],
    "edges": [
        {"source": "Sarah", "target": "Mei", "relation": "reports_to",
         "source_quote": "Sarah reports to Mei.", "note_id": "n1", "confidence": 0.9},
        {"source": "Sarah", "target": "Apollo", "relation": "works_on",
         "source_quote": "Sarah owns Apollo.", "note_id": "n1", "confidence": 0.7},
        {"source": "Mei", "target": "Apollo", "relation": "owns",
         "source_quote": "Mei owns Apollo.", "note_id": "n2", "confidence": 0.5},
    ],
}
STATS = {"nodes": 3, "edges": 3, "central_entities": [], "concept_clusters": []}


def test_sqlite_store_satisfies_the_contract():
    assert isinstance(get_store(), GraphStore)
    assert isinstance(get_store(), SQLiteStore)
    assert backend_name() == "sqlite"


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "mongo")
    reset_store()
    with pytest.raises(ValueError, match="Unknown GRAPH_BACKEND"):
        get_store()


def test_graph_round_trips():
    db.cache_graph(GRAPH, STATS)
    loaded = db.get_cached_graph()
    assert len(loaded["graph"]["nodes"]) == 3
    assert len(loaded["graph"]["edges"]) == 3
    assert loaded["stats"]["nodes"] == 3


def test_get_entities_returns_nodes_without_edges():
    db.cache_graph(GRAPH, STATS)
    ents = db.get_entities()
    assert {e["id"] for e in ents} == {"Sarah", "Mei", "Apollo"}
    # Nodes carry ranking data but never edge data — that is the whole point
    # of the separate call, so local search need not load the edge list.
    assert all("source" not in e and "target" not in e for e in ents)


def test_neighbourhood_returns_only_touching_facts():
    db.cache_graph(GRAPH, STATS)
    facts = db.neighbourhood({"Sarah"})
    texts = {f["text"] for f in facts}
    assert "Sarah reports_to Mei" in texts
    assert "Sarah works_on Apollo" in texts
    # Mei->Apollo does not touch Sarah and must not leak in.
    assert "Mei owns Apollo" not in texts


def test_neighbourhood_is_confidence_ranked_and_carries_citations():
    db.cache_graph(GRAPH, STATS)
    facts = db.neighbourhood({"Sarah"})
    confs = [f["confidence"] for f in facts]
    assert confs == sorted(confs, reverse=True)
    assert all(f["note_id"] for f in facts), "facts must stay citable"
    assert facts[0]["quote"]


def test_neighbourhood_depth_2_reaches_what_depth_1_cannot():
    """The multi-hop case: Mei's other report is invisible one hop from Sarah."""
    db.cache_graph(GRAPH, STATS)
    one = {f["text"] for f in db.neighbourhood({"Sarah"}, depth=1)}
    two = {f["text"] for f in db.neighbourhood({"Sarah"}, depth=2)}

    assert "Mei owns Apollo" not in one, "not reachable in one hop from Sarah"
    assert "Mei owns Apollo" in two, "reachable via Sarah -> Mei"
    # Widening must never lose facts, only add.
    assert one <= two


def test_neighbourhood_tags_hop_distance_and_ranks_nearest_first():
    db.cache_graph(GRAPH, STATS)
    facts = db.neighbourhood({"Sarah"}, depth=2)
    by_text = {f["text"]: f for f in facts}
    assert by_text["Sarah reports_to Mei"]["hops"] == 1
    assert by_text["Mei owns Apollo"]["hops"] == 2
    # Direct facts must outrank inferred ones regardless of confidence:
    # "Mei owns Apollo" has lower confidence AND is further away.
    hops = [f["hops"] for f in facts]
    assert hops == sorted(hops), "nearest facts must come first"


def test_neighbourhood_depth_is_clamped_not_unbounded():
    db.cache_graph(GRAPH, STATS)
    # A caller asking for depth 99 must not trigger an unbounded traversal.
    deep = db.neighbourhood({"Sarah"}, depth=99)
    assert all(f["hops"] <= get_store().MAX_DEPTH for f in deep)


def test_neighbourhood_respects_limit_and_empty_input():
    db.cache_graph(GRAPH, STATS)
    assert len(db.neighbourhood({"Sarah", "Mei"}, limit=1)) == 1
    assert db.neighbourhood(set()) == []


def test_neighbourhood_on_empty_graph():
    assert db.neighbourhood({"Sarah"}) == []
    assert db.get_entities() == []


def test_find_path_returns_hops_stating_facts_truthfully():
    """
    A hop must read as a true sentence even when the walk went against the
    edge. Walking Mei -> Apollo across "Mei owns Apollo" is fine; walking
    Apollo -> Mei must still say "Mei owns Apollo", not the reverse.
    """
    db.cache_graph(GRAPH, STATS)
    hops = db.find_path("Apollo", "Sarah")
    assert hops, "Apollo and Sarah are connected"
    for h in hops:
        # from/to are the stored fact; walk order lives in walk_from/walk_to.
        assert (h["from"], h["relation"], h["to"]) in {
            (e["source"], e["relation"], e["target"]) for e in GRAPH["edges"]
        }, f"hop {h} does not correspond to a real edge"
        assert h["direction"] in ("forward", "reverse")
        assert {h["walk_from"], h["walk_to"]} == {h["from"], h["to"]}


def test_find_path_is_shortest_and_contiguous():
    db.cache_graph(GRAPH, STATS)
    hops = db.find_path("Sarah", "Apollo")
    # Sarah works_on Apollo directly — one hop, not routed via Mei.
    assert len(hops) == 1

    two = db.find_path("Mei", "Sarah")
    assert two[0]["walk_from"] == "Mei"
    assert two[-1]["walk_to"] == "Sarah"
    # Each hop must start where the previous one ended — a path with a gap is
    # not a path.
    for a, b in zip(two, two[1:]):
        assert a["walk_to"] == b["walk_from"], f"gap between {a} and {b}"


def test_find_path_absent_or_unconnected_returns_empty():
    db.cache_graph(GRAPH, STATS)
    assert db.find_path("Sarah", "NoSuchEntity") == []
    assert db.find_path("Sarah", "Sarah") == []


def test_search_entities_finds_by_name():
    db.cache_graph(GRAPH, STATS)
    hits = db.search_entities("what is Apollo about")
    assert any(h["id"] == "Apollo" for h in hits)
    assert db.search_entities("") == []


def test_delete_note_removes_its_triples():
    db.upsert_note("n1", "T", "C")
    db.insert_triples([{
        "subject_text": "Sarah", "relation": "reports_to", "object_text": "Mei",
        "source_note_id": "n1",
    }])
    assert len(db.get_all_triples()) == 1
    db.delete_note("n1")
    assert db.get_note("n1") is None
    assert db.get_all_triples() == []


def test_set_note_status_rejects_invalid_status():
    db.upsert_note("n1", "T", "C")
    with pytest.raises(ValueError):
        db.set_note_status("n1", "nonsense")


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

def test_workspaces_are_isolated_in_both_directions():
    """
    The whole safety property: one workspace must never read another's data.

    Property-based partitioning fails open — a forgotten filter leaks silently
    rather than erroring — so this is asserted explicitly in both directions.
    """
    db.upsert_note("shared-id", "Personal note", "Sarah is my neighbour.")
    db.insert_triples([{
        "subject_text": "Sarah", "subject_type": "person", "relation": "related_to",
        "object_text": "neighbour", "object_type": "unknown", "source_note_id": "shared-id",
    }])

    other = db.for_workspace("office")
    other.init_schema()
    # Deliberately the SAME note id: ids are unique per workspace, not globally.
    other.upsert_note("shared-id", "Office note", "Sarah is my manager.")

    assert db.get_note("shared-id")["title"] == "Personal note"
    assert other.get_note("shared-id")["title"] == "Office note"

    assert db.get_db_stats()["notes_total"] == 1
    assert other.stats()["notes_total"] == 1
    # The office workspace must not inherit the default's triples.
    assert other.get_all_triples() == []


def test_new_workspace_starts_empty_and_is_registered():
    db.upsert_note("n1", "T", "C")
    created = db.create_workspace("apollo", name="Apollo", description="project graph")
    assert created["id"] == "apollo"

    ids = {w["id"] for w in db.list_workspaces()}
    assert {"default", "apollo"} <= ids

    apollo = db.for_workspace("apollo")
    assert apollo.stats()["notes_total"] == 0, "a new workspace must inherit nothing"


def test_invalid_workspace_ids_are_rejected():
    from brahmastra.workspace import InvalidWorkspaceId
    for bad in (
        "all",          # reserved: already means "every workspace" at the API
        "",             # no partition key
        "Has Spaces",   # not a slug — would be ambiguous in a URL
        "-leading",     # must start alphanumeric
        "a" * 64,       # too long for an index key
    ):
        with pytest.raises((InvalidWorkspaceId, ValueError)):
            db.create_workspace(bad)


def test_workspace_ids_are_case_normalised_not_rejected():
    """
    Case is normalised rather than refused, so "Office" and "office" cannot
    become two graphs that look identical in a list.
    """
    created = db.create_workspace("Office")
    assert created["id"] == "office"
    assert db.get_workspace("office") is not None


def test_default_workspace_cannot_be_deleted():
    # Deleting it would destroy every pre-workspace install's data, since that
    # is where the migration puts it.
    with pytest.raises(ValueError, match="default"):
        db.delete_workspace("default")


def test_delete_workspace_removes_only_its_own_data():
    db.upsert_note("keep", "Keep", "stays in default")
    db.create_workspace("temp")
    tmp = db.for_workspace("temp")
    tmp.upsert_note("gone", "Gone", "lives in temp")

    db.delete_workspace("temp")

    assert db.get_note("keep") is not None, "deleting one workspace touched another"
    assert {w["id"] for w in db.list_workspaces()} == {"default"}


def test_cross_workspace_search_is_explicit_and_tagged():
    db.upsert_note("p1", "Personal", "Sarah plays cricket on Sunday.")
    db.create_workspace("work")
    db.for_workspace("work").upsert_note("w1", "Work", "Sarah ships the cricket feature.")

    # Scoped search sees only its own workspace...
    assert [n["id"] for n in db.search_notes("cricket")] == ["p1"]

    # ...crossing the partition takes an explicit call.
    hits = db.search_notes_across("cricket")
    assert {n["id"] for n in hits} == {"p1", "w1"}
    # Every result says where it came from, so a caller can never confuse them.
    assert {n["workspace_id"] for n in hits} == {"default", "work"}


def test_store_factory_forwards_the_requested_workspace():
    """
    Regression: the factory built the Neo4j store WITHOUT passing the
    workspace, so every requested workspace silently bound to the process
    default and writes meant for one graph landed in another. It overwrote a
    real note before the isolation test caught it.

    Asserted at the factory rather than through a backend, so it holds for any
    backend added later.
    """
    from brahmastra.stores import get_store
    for wid in ("office", "apollo"):
        assert get_store(workspace=wid).workspace == wid

    # And an explicit request must not disturb the cached default store.
    default_before = get_store().workspace
    get_store(workspace="office")
    assert get_store().workspace == default_before


def test_workspace_binding_survives_env_change():
    """A store bound explicitly ignores BRAHMASTRA_WORKSPACE afterwards."""
    import os
    from brahmastra.stores import get_store
    bound = get_store(workspace="apollo")
    os.environ["BRAHMASTRA_WORKSPACE"] = "something-else"
    try:
        assert bound.workspace == "apollo"
    finally:
        os.environ.pop("BRAHMASTRA_WORKSPACE", None)


def test_factory_refuses_a_store_bound_to_the_wrong_workspace(monkeypatch):
    """
    The structural fix for the leak: a backend that ignores the workspace it
    was given fails at construction, before it can write to the wrong graph.
    """
    from brahmastra.stores import WorkspaceBindingError, _build
    import brahmastra.stores as stores

    class Disobedient(SQLiteStore):
        def __init__(self, workspace=None):
            super().__init__(workspace="default")  # ignores the request

    monkeypatch.setattr(stores, "SQLiteStore", Disobedient)
    with pytest.raises(WorkspaceBindingError, match="office"):
        _build("sqlite", "office")


# ---------------------------------------------------------------------------
# Per-workspace Notion source
# ---------------------------------------------------------------------------

def test_notion_source_prefers_the_workspace_over_the_global(monkeypatch):
    from brahmastra.sync import _notion_target_for_current_workspace as target

    monkeypatch.setenv("NOTION_DATABASE_ID", "GLOBAL")
    # No per-workspace value -> the global one, so existing setups are unaffected.
    assert target() == "GLOBAL"

    db.create_workspace("office", name="Office", notion_database_id="OFFICE")
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "office")
    reset_store()
    assert target() == "OFFICE", "a workspace must pull from its own Notion source"

    # A workspace without its own source still falls back.
    db.create_workspace("apollo", name="Apollo")
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "apollo")
    reset_store()
    assert target() == "GLOBAL"


def test_pipeline_runs_sync_when_only_a_per_workspace_source_exists(monkeypatch):
    """
    Checking only the env var would skip sync for a workspace that has its own
    source, leaving it never pulling anything.
    """
    from brahmastra.pipeline import _missing_notion_config

    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)

    db.create_workspace("office", name="Office", notion_database_id="OFFICE")
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "office")
    reset_store()
    assert _missing_notion_config(need_database=True) == []

    db.create_workspace("apollo", name="Apollo")
    monkeypatch.setenv("BRAHMASTRA_WORKSPACE", "apollo")
    reset_store()
    assert _missing_notion_config(need_database=True), "no source anywhere -> skip"


def test_the_same_fact_from_one_note_is_stored_once():
    """
    Neo4j MERGEs :ASSERTS on (relation, sourceNoteId), so re-extracting a note
    collapses repeats. SQLite had no such constraint and every repeat became a
    row: 742 triples here against 623 there for identical data, so the two
    backends disagreed about their own counts and neither could be trusted
    without knowing which store produced it.
    """
    from brahmastra import db
    db.upsert_note("n1", "T", "C")
    fact = {
        "subject_text": "Brahmastra", "subject_type": "concept",
        "relation": "has_component", "object_text": "llm.py",
        "object_type": "concept", "confidence": 0.9, "source_note_id": "n1",
    }
    # The extractor genuinely emits the same fact twice from one note.
    db.insert_triples([fact, dict(fact)])
    assert len(db.get_all_triples()) == 1

    # And a re-extraction must not accumulate either.
    db.insert_triples([dict(fact)])
    assert len(db.get_all_triples()) == 1


def test_the_same_fact_from_a_different_note_is_kept():
    """
    Uniqueness is per NOTE, not global. Two notes independently asserting a
    fact is corroboration, and collapsing them would erase the evidence that
    provenance and contradiction detection rely on.
    """
    from brahmastra import db
    db.upsert_note("n1", "T", "C")
    db.upsert_note("n2", "T", "C")
    base = {
        "subject_text": "Sarah", "subject_type": "person",
        "relation": "reports_to", "object_text": "Mei",
        "object_type": "person", "confidence": 0.9,
    }
    db.insert_triples([
        {**base, "source_note_id": "n1"},
        {**base, "source_note_id": "n2"},
    ])
    assert len(db.get_all_triples()) == 2


def test_an_existing_database_is_deduplicated_when_the_constraint_arrives(tmp_path, monkeypatch):
    """
    The index cannot simply be added: creating it fails on exactly the
    databases that need it. Existing rows are collapsed first.
    """
    import sqlite3
    import importlib

    path = tmp_path / "legacy.db"
    monkeypatch.setenv("BRAHMASTRA_DB", str(path))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    # Write duplicates behind the store's back, as the old code path did.
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX IF EXISTS idx_triples_unique")
    for _ in range(3):
        raw.execute(
            "INSERT INTO raw_triples (workspace_id, subject_text, subject_type, "
            "relation, object_text, object_type, confidence, source_note_id, "
            "extracted_at) VALUES ('default','A','concept','related_to','B',"
            "'concept',1.0,'n1','2026-01-01')"
        )
    raw.commit()
    assert raw.execute("select count(*) from raw_triples").fetchone()[0] == 3
    raw.close()

    importlib.reload(db_mod)
    db_mod.init_db()          # migration runs here

    assert len(db_mod.get_all_triples()) == 1, "legacy duplicates must be collapsed"
