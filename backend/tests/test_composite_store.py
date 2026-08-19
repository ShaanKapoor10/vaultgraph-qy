"""
CompositeStore: notes on one backend, the derived cache on another.

The point of the split is that choosing an engine stops being a decision about
irreplaceable data. These tests pin the two ways that can go wrong quietly --
a call reaching the wrong half, and a search capability disappearing without
anything saying so.
"""
from __future__ import annotations

import pytest

from brahmastra.stores.base import (
    CAP_FULLTEXT_SEARCH,
    CAP_HYBRID_SEARCH,
    CAP_LEXICAL_SEARCH,
    CAP_VECTOR_SEARCH,
    SOURCE_METHODS,
    GraphStore,
)
from brahmastra.stores.composite_store import CapabilityDowngrade, CompositeStore


class FakeStore:
    """
    Records which methods were called on it, and answers nothing.

    Deliberately NOT a GraphStore subclass. The workspace methods are concrete
    on the contract -- they raise "not supported by this backend" -- so a
    subclass would answer them itself and __getattr__ would never see the call,
    making a routing bug look like a backend limitation.
    """

    def __init__(self, label: str, caps: frozenset[str], workspace: str = "default"):
        self.label = label
        self._caps = caps
        self.workspace = workspace
        self.calls: list[str] = []
        self.closed = False

    def capabilities(self) -> frozenset[str]:
        return self._caps

    def describe(self) -> str:
        return self.label

    def init_schema(self) -> None:
        self.calls.append("init_schema")

    def stats(self) -> dict[str, int]:
        self.calls.append("stats")
        return {"notes": 1} if "note" in self.label else {"notes": 99, "triples": 7}

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append(name)
            return f"{self.label}:{name}"

        return record


HYBRID = frozenset({CAP_LEXICAL_SEARCH, CAP_FULLTEXT_SEARCH,
                    CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH})
LEXICAL = frozenset({CAP_LEXICAL_SEARCH})


def _pair(note_caps=HYBRID, **kw):
    notes = FakeStore("notes-store", note_caps)
    graph = FakeStore("graph-store", HYBRID)
    return notes, graph, CompositeStore(notes=notes, graph=graph, **kw)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_note_calls_reach_the_note_store_and_graph_calls_the_graph_store():
    notes, graph, c = _pair()

    c.upsert_note(id="n1", title="T", content="C")
    c.get_note("n1")
    c.search_notes("q")
    c.list_workspaces()

    c.insert_triples([])
    c.get_all_triples()
    c.search_entities("q")
    c.find_path("a", "b")

    assert notes.calls == ["upsert_note", "get_note", "search_notes", "list_workspaces"]
    assert graph.calls == ["insert_triples", "get_all_triples", "search_entities", "find_path"]


def test_every_contract_method_routes_somewhere():
    """
    A method reachable on neither half is an AttributeError in production and
    nowhere in the tests, since each one is exercised only by the feature that
    happens to use it.
    """
    notes, graph, c = _pair()
    missing = [
        name for name in GraphStore.__abstractmethods__
        if not callable(getattr(c, name, None))
    ]
    assert not missing, f"contract methods not routed: {sorted(missing)}"


def test_the_split_follows_the_declared_classification():
    """
    Routing reads SOURCE_METHODS rather than a list of its own, so the
    classification test is what keeps this honest as the contract grows.
    """
    # Minimal arguments per method; the values are irrelevant, only which half
    # receives the call.
    args: dict[str, tuple] = {
        "upsert_note": ("n1", "T", "C"),
        "get_note": ("n1",),
        "search_notes": ("q",),
        "search_notes_across": ("q", ["default"]),
        "set_note_status": ("n1", "done"),
        "set_notion_page_id": ("n1", "p1"),
        "delete_note": ("n1",),
        "create_workspace": (object(),),
        "get_workspace": ("default",),
        "delete_workspace": ("default",),
    }
    notes, graph, c = _pair()
    for name in sorted(SOURCE_METHODS):
        if not hasattr(GraphStore, name):
            continue
        notes.calls.clear()
        graph.calls.clear()
        getattr(c, name)(*args.get(name, ()))
        assert notes.calls == [name], f"{name} is source data but reached the graph store"
        assert graph.calls == []


# ---------------------------------------------------------------------------
# The downgrade guard
# ---------------------------------------------------------------------------

def test_a_note_store_without_hybrid_search_is_refused():
    """
    The failure this prevents is invisible: a lexical store answers a hybrid
    query successfully and returns worse results, with nothing in any log.
    """
    with pytest.raises(CapabilityDowngrade) as e:
        _pair(note_caps=LEXICAL)

    msg = str(e.value)
    assert "hybrid_search" in msg
    assert "ALLOW_SEARCH_DOWNGRADE" in msg, "the message must name the way out"


def test_the_downgrade_can_be_accepted_explicitly_and_is_recorded():
    """Opting in is allowed; forgetting what it cost is not."""
    notes, graph, c = _pair(note_caps=LEXICAL, allow_downgrade=True)
    assert c.degraded == frozenset({CAP_HYBRID_SEARCH})


def test_the_environment_can_accept_the_downgrade(monkeypatch):
    monkeypatch.setenv("ALLOW_SEARCH_DOWNGRADE", "1")
    notes, graph, c = _pair(note_caps=LEXICAL)
    assert c.degraded == frozenset({CAP_HYBRID_SEARCH})


def test_capabilities_are_the_note_halfs_not_the_union():
    """
    The graph store supports hybrid search over ENTITIES; that says nothing
    about whether notes can be searched that way. Reporting the union would
    advertise a capability the note path does not have.
    """
    notes, graph, c = _pair(note_caps=LEXICAL, allow_downgrade=True)
    assert CAP_HYBRID_SEARCH not in c.capabilities()


# ---------------------------------------------------------------------------
# Workspace and lifecycle
# ---------------------------------------------------------------------------

def test_halves_bound_to_different_workspaces_are_refused():
    """
    Isolation fails open: two halves on different workspaces would split one
    graph across two, with no error at any call site.
    """
    notes = FakeStore("notes-store", HYBRID, workspace="office")
    graph = FakeStore("graph-store", HYBRID, workspace="default")
    with pytest.raises(ValueError, match="workspace"):
        CompositeStore(notes=notes, graph=graph)


def test_init_schema_reaches_both_halves():
    notes, graph, c = _pair()
    c.init_schema()
    assert "init_schema" in notes.calls and "init_schema" in graph.calls


def test_stats_prefer_the_note_store_for_note_counts():
    """
    Both halves report `notes`, but only one holds them -- the graph keeps
    stubs for provenance. Graph precedence would report the stub count.
    """
    notes, graph, c = _pair()
    merged = c.stats()
    assert merged["notes"] == 1, "note counts must come from the note store"
    assert merged["triples"] == 7, "graph-only keys must survive the merge"


def test_close_closes_both_halves():
    notes, graph, c = _pair()
    c.close()
    assert notes.closed and graph.closed


def test_a_failing_close_still_closes_the_other_half():
    """A leaked connection outlives the error that stopped it being reported."""
    notes, graph, c = _pair()

    def boom():
        raise RuntimeError("connection already gone")

    notes.close = boom
    with pytest.raises(RuntimeError, match="already gone"):
        c.close()
    assert graph.closed, "the second half must be closed regardless"


def test_describe_names_both_halves():
    notes, graph, c = _pair()
    d = c.describe()
    assert "notes-store" in d and "graph-store" in d
