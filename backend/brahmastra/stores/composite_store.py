"""
CompositeStore -- one GraphStore made of two, split along source/derived.

The problem it solves
---------------------
`GRAPH_BACKEND` used to decide where BOTH the notes and the graph live, so
choosing an engine also chose where the irreplaceable data sits. That is
backwards: triples, clusters and the cached graph are a function of the notes
(`run_pipeline(full=True)` regenerates every one of them), while a lost note is
lost information. An engine decision must not put source data at risk.

Two incidents came from the fusion. A backend switch left 61 notes in one store
and 54 in another -- the triple counts differed too, but only the note gap was
real divergence. And a store built without its workspace overwrote a note in
`default` that belonged to `office`.

How it routes
-------------
Note and workspace calls go to the note store; everything else to the graph
store. The split is read from `SOURCE_METHODS` in base.py rather than hardcoded
here, so a method added to the contract cannot quietly default to one side --
tests/test_store_authority.py fails until it is classified.

Why it can refuse to exist
--------------------------
Backends differ in what they can SEARCH, not just what they can store. Neo4j
fuses BM25 fulltext with vector similarity; SQLite matches substrings. Routing
note search to a lexical store is the worst kind of regression: every query
still returns results, just worse ones, with nothing in any log to say so.

So the note store must declare `REQUIRED_NOTE_CAPABILITIES`, and a composite
that would downgrade search raises `CapabilityDowngrade` at construction rather
than serving quietly degraded results. `ALLOW_SEARCH_DOWNGRADE=1` overrides it
for local work, and says out loud what is being given up.
"""

from __future__ import annotations

import os
from typing import Any

from brahmastra.stores.base import (
    REQUIRED_NOTE_CAPABILITIES,
    SOURCE_METHODS,
    GraphStore,
)


class CapabilityDowngrade(RuntimeError):
    """
    The note store cannot do something the notes need.

    Raised at construction, not at query time, because the failure it prevents
    is silent: a lexical store answers a hybrid query with a straight face.
    """


class CompositeStore(GraphStore):
    """
    Routes source data to one store and derived data to another.

    Both halves stay real GraphStores, so either can be used alone and neither
    learns it is half of something.
    """

    def __init__(self, notes: GraphStore, graph: GraphStore,
                 allow_downgrade: bool | None = None) -> None:
        self._notes = notes
        self._graph = graph

        if allow_downgrade is None:
            allow_downgrade = os.getenv("ALLOW_SEARCH_DOWNGRADE", "").strip().lower() in (
                "1", "true", "yes", "on"
            )

        missing = REQUIRED_NOTE_CAPABILITIES - notes.capabilities()
        if missing and not allow_downgrade:
            raise CapabilityDowngrade(
                f"note store {notes.describe()} cannot provide "
                f"{sorted(missing)}, which note search depends on. Splitting "
                f"notes onto it would downgrade hybrid search (BM25 + vector, "
                f"fused with RRF) to substring matching -- queries would keep "
                f"succeeding and quietly return worse results. Use a note "
                f"store that supports it, or set ALLOW_SEARCH_DOWNGRADE=1 to "
                f"accept lexical-only search."
            )
        self.degraded = frozenset(missing)

        # The workspace is a property of the data, so both halves must agree.
        # Disagreeing would partition one graph across two workspaces, which is
        # the isolation failure the three-layer guard exists to prevent.
        nw = getattr(notes, "workspace", None)
        gw = getattr(graph, "workspace", None)
        if nw is not None and gw is not None and nw != gw:
            raise ValueError(
                f"composite halves disagree on workspace: notes={nw!r} "
                f"graph={gw!r}. Both must be bound to the same one."
            )
        self.workspace = nw or gw

    # -- routing -----------------------------------------------------------

    def _for(self, method: str) -> GraphStore:
        return self._notes if method in SOURCE_METHODS else self._graph

    def __getattr__(self, name: str) -> Any:
        """
        Route anything the contract defines but this class does not override.

        Only reached for attributes not found normally, so the explicit methods
        below always win. Private names are refused rather than forwarded: a
        composite has two of every internal, and picking one silently is how a
        caller ends up talking to the half it did not mean.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        target = self._notes if name in SOURCE_METHODS else self._graph
        return getattr(target, name)

    # -- lifecycle, which is genuinely both --------------------------------

    def init_schema(self) -> None:
        self._notes.init_schema()
        self._graph.init_schema()

    def describe(self) -> str:
        return f"composite(notes={self._notes.describe()}, graph={self._graph.describe()})"

    def capabilities(self) -> frozenset[str]:
        """
        What the pair can do, which is not the union.

        Note capabilities come from the note half and graph capabilities from
        the graph half; claiming the union would advertise hybrid note search
        because the graph store happens to support it.
        """
        return self._notes.capabilities()

    # Counts that describe the system of record. Whatever the graph store says
    # about these is about its own provenance stubs, so it must not survive the
    # merge even if the note store omits the key -- a stub count presented as a
    # note count is worse than a missing number.
    _NOTE_STAT_KEYS = ("notes_total", "notes_pending", "notes")

    def stats(self) -> dict[str, int]:
        """
        Merged, with the note counts authoritative.

        Both halves report note counts, but only one holds notes -- the graph
        store keeps stubs so triples can point back at their source. Observed
        for real: Neo4j reported 54 stale stubs as `notes_total` while Postgres
        reported 3 as `notes`, and because the KEY NAMES differed, precedence
        overwrote nothing and both numbers appeared side by side looking
        equally authoritative. The keys are aligned across backends now; this
        strips them regardless, so a future backend inventing its own name
        cannot reintroduce the same lie.
        """
        merged = {
            k: v for k, v in self._graph.stats().items()
            if k not in self._NOTE_STAT_KEYS
        }
        merged.update(self._notes.stats())
        return merged

    def _close_halves(self) -> None:
        """
        Close whichever halves hold a connection.

        `close` is not on the contract -- only the networked backend has
        anything to release -- so this asks rather than assumes. Catching
        AttributeError instead would swallow a genuine failure inside a real
        close and report a clean shutdown.
        """
        first_error: Exception | None = None
        for half in (self._notes, self._graph):
            closer = getattr(half, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception as e:            # noqa: BLE001 - see below
                # The second half must still be closed: a leaked connection
                # outlives the error that prevented reporting it. The first
                # failure is re-raised once both have been attempted.
                first_error = first_error or e
        if first_error is not None:
            raise first_error

    close = _close_halves


# ---------------------------------------------------------------------------
# Routing the rest of the contract
# ---------------------------------------------------------------------------
#
# Every remaining method delegates by the same rule -- source methods to the
# note half, the rest to the graph half -- so writing them out by hand would be
# twenty near-identical bodies free to drift apart. They are generated instead,
# from the same SOURCE_METHODS the classification test enforces.
#
# They must be real attributes rather than __getattr__ fallbacks because
# ABCMeta decides what is still abstract when the class is created, and a
# method reachable only through __getattr__ does not count as implemented.

def _make_delegate(name: str):
    def delegate(self, *args, **kwargs):
        target = self._notes if name in SOURCE_METHODS else self._graph
        return getattr(target, name)(*args, **kwargs)

    delegate.__name__ = name
    side = "note" if name in SOURCE_METHODS else "graph"
    delegate.__doc__ = f"Delegates to the {side} store."
    return delegate


# Every public contract method, not merely the abstract ones. The workspace
# methods are concrete on GraphStore -- they raise a "not supported by this
# backend" NotImplementedError by default -- so delegating only the abstract
# set left CompositeStore inheriting that refusal and reporting that it could
# not list workspaces, while a perfectly capable note store sat underneath it.
for _name in sorted(
    name for name in dir(GraphStore)
    if not name.startswith("_") and callable(getattr(GraphStore, name, None))
):
    if _name not in vars(CompositeStore):
        setattr(CompositeStore, _name, _make_delegate(_name))

# Every abstract method now has a concrete implementation above; ABCMeta
# computed this set before they were attached, so it is restated rather than
# recomputed.
CompositeStore.__abstractmethods__ = frozenset()
del _name
