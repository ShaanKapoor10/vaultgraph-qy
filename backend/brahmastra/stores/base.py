"""
GraphStore — the storage contract every backend must satisfy.

Extracted so the graph can move off local SQLite onto a shared, networked
store (Neo4j Aura) without touching the ~100 call sites that use `db.*`.
`db.py` stays the public API and delegates here, so adding a backend means
implementing this class and nothing else.

The contract is deliberately expressed in the domain's terms (notes, triples,
clusters) rather than in SQL, so a non-relational backend can satisfy it
honestly. See docs/NEO4J_DATA_MODEL.md for how these map onto a property graph.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# ---------------------------------------------------------------------------
# What is authoritative, and what is a cache
# ---------------------------------------------------------------------------
#
# The contract below covers two kinds of data, and the difference decides how
# much care each one is owed.
#
# SOURCE data cannot be recomputed. If the only copy of a note is lost, it is
# gone -- no amount of running the pipeline brings it back.
#
# DERIVED data is a cache. Every triple, cluster, canonical mapping and cached
# graph is a function of the notes; `run_pipeline(full=True)` regenerates all
# of it. Losing it costs time and LLM calls, never information.
#
# This asymmetry is why they must not share a fate. Selecting a backend is a
# decision about the ENGINE, and an engine decision must never put source data
# at risk. Two real incidents came from treating them alike: a backend switch
# left 61 notes in one store and 54 in another (the triple counts differed too,
# but only the note gap was actual divergence), and a store built without its
# workspace overwrote a note in `default` that belonged to `office`.
#
# Anything added here belongs in exactly one of these sets.
SOURCE_DATA = ("notes", "workspaces")
DERIVED_DATA = ("raw_triples", "canonical_map", "entity_clusters", "graph_cache")

# Contract methods that read or write SOURCE data. A store may hold only these
# (a note store), only the rest (a graph engine), or both. Kept as data rather
# than prose so a composite store can route on it instead of hardcoding a list
# that silently rots as the contract grows.
SOURCE_METHODS = frozenset({
    "upsert_note", "get_note", "get_notes_by_ids", "get_notes", "search_notes",
    "search_notes_across",
    "set_note_status", "set_notion_page_id", "delete_note",
    "list_workspaces", "create_workspace", "get_workspace", "delete_workspace",
})


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------
#
# Backends are not interchangeable in what they can SEARCH, only in what they
# can store. Neo4j fuses BM25 fulltext with vector similarity (RRF) and so
# finds notes sharing no keywords with the query; SQLite is lexical only.
#
# This matters the moment notes and graph can live in different stores: routing
# note search to a lexical store is a silent, invisible downgrade -- the same
# queries keep returning results, just worse ones. Declaring the capability
# lets the composite refuse rather than quietly degrade.
CAP_LEXICAL_SEARCH = "lexical_search"
CAP_FULLTEXT_SEARCH = "fulltext_search"
CAP_VECTOR_SEARCH = "vector_search"
CAP_HYBRID_SEARCH = "hybrid_search"

# What a store must offer to be trusted with the notes. Hybrid search is a
# product feature, not an optimisation, so losing it is a regression rather
# than a trade-off.
REQUIRED_NOTE_CAPABILITIES = frozenset({CAP_HYBRID_SEARCH})


class GraphStore(ABC):
    """
    Persistence for notes, extracted triples, resolved entities and the graph.

    See SOURCE_DATA / DERIVED_DATA above: the note and workspace methods carry
    data that cannot be regenerated, the rest is a rebuildable cache.
    """

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def init_schema(self) -> None:
        """Create whatever the backend needs. Must be idempotent."""

    def capabilities(self) -> frozenset[str]:
        """
        What this backend can do beyond storing rows.

        Defaults to the weakest honest answer: every store can match a
        substring. A backend that overstates this does not fail -- it silently
        returns worse results -- so the default is deliberately pessimistic and
        each store opts in to what it has actually implemented.
        """
        return frozenset({CAP_LEXICAL_SEARCH})

    @abstractmethod
    def describe(self) -> str:
        """Human-readable target, e.g. a file path or a bolt:// URI. For diagnostics."""

    # -- notes -------------------------------------------------------------

    @abstractmethod
    def upsert_note(
        self,
        id: str,
        title: str,
        content: str,
        last_edited: str | None = None,
        mark_pending: bool = True,
        publish: bool | None = None,
        source: str | None = None,
    ) -> None:
        """
        Insert or update a note.

        Re-extraction is triggered (status -> 'pending') when the caller asks
        for it, or when last_edited changed. An unchanged note keeps its
        current status so a sync does not re-extract the whole vault.
        """

    @abstractmethod
    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Term search over title + content, newest first.

        Complements entity search: it finds notes by what they SAY, even when
        extraction produced few triples for them.
        """

    @abstractmethod
    def get_note(self, id: str) -> dict[str, Any] | None: ...

    def get_notes_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """
        Fetch several notes at once, keyed by id. Missing ids are simply absent.

        Concrete rather than abstract, with a correct-but-slow default, so an
        existing backend keeps working without change. It exists because the
        citation path asks for one note per cited fact: fine against a local
        file, N network round trips once the notes live on a remote store.
        A backend that can answer in one query should override it.
        """
        found = {}
        for note_id in ids:
            note = self.get_note(note_id)
            if note is not None:
                found[note_id] = note
        return found

    @abstractmethod
    def set_notion_page_id(self, note_id: str, page_id: str) -> None:
        """
        Record the Notion page a Brahmastra-born note was published to.

        Not abstract: a backend without a Notion projection simply never
        publishes. Persisting the id is what stops write-back creating a fresh
        page on every run instead of updating the one it made.
        """
        raise NotImplementedError

    def set_note_status(self, id: str, status: str, error: str | None = None) -> None:
        """
        Set extraction status: 'pending' | 'done' | 'error'.

        `error` is the failure message, stored only alongside 'error' and
        cleared by any other status, so a message can never outlive the
        failure it describes.
        """

    @abstractmethod
    def delete_note(self, id: str) -> None:
        """
        Delete a note and everything derived from it.

        Exists because routers/notes.py previously reached past this API into
        raw SQL to do it, which no non-SQL backend could have served.
        """

    # -- triples -----------------------------------------------------------

    @abstractmethod
    def delete_triples_for_note(self, note_id: str) -> None: ...

    @abstractmethod
    def insert_triples(self, triples: list[dict[str, Any]]) -> None:
        """
        Persist extracted triples.

        Each carries provenance: confidence, source_quote, source_note_id.
        Implementations must stamp extracted_at themselves.
        """

    @abstractmethod
    def get_all_triples(self) -> list[dict[str, Any]]: ...

    # -- entity resolution -------------------------------------------------

    @abstractmethod
    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        """
        Atomically replace the whole mention -> canonical mapping.

        Resolution is recomputed wholesale each run, so this must be
        all-or-nothing: a partial map would silently corrupt the graph.
        """

    @abstractmethod
    def get_canonical_map(self) -> dict[str, str]:
        """{mention_text: canonical_name}"""

    @abstractmethod
    def get_entity_clusters(self) -> list[dict[str, Any]]: ...

    # -- built graph -------------------------------------------------------

    @abstractmethod
    def save_graph(self, graph: dict[str, Any], stats: dict[str, Any]) -> None:
        """
        Persist the built graph.

        Named save_graph, not cache_graph: on a native graph backend this is
        the primary store, not a cache. On SQLite it stays a serialised blob.
        """

    @abstractmethod
    def load_graph(self) -> dict[str, Any] | None:
        """Return {built_at, graph, stats} or None if nothing has been built."""

    @abstractmethod
    def get_entities(self) -> list[dict[str, Any]]:
        """
        Graph nodes only — {id, label, type, pagerank, cluster} — no edges.

        Entity matching needs names, not the whole graph. Loading edges for it
        would undo the point of the traversal below.
        """

    @abstractmethod
    def search_entities(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """
        Entities a question is about, best first, in get_entities() shape.

        Backends differ in quality here on purpose: SQLite can only match
        entities whose words literally appear in the query, while Neo4j fuses
        that with embedding similarity so "who is Sarah's boss" can reach
        `reports_to`. Same shape, better recall.
        """

    @abstractmethod
    def find_path(
        self, source: str, target: str, max_hops: int = 5
    ) -> list[dict[str, Any]]:
        """
        Shortest path between two entities as ordered hops, [] if unconnected.

        Each hop is {from, relation, to, direction, note_id}. Direction is
        reported because connection-finding ignores edge direction while the
        underlying fact still has one: "Sarah reports_to Mei" read backwards
        is not "Mei reports_to Sarah".
        """

    @abstractmethod
    def neighbourhood(
        self, names: set[str], limit: int = 40, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        Facts reachable from `names` within `depth` hops.

        Each fact is {text, quote, note_id, confidence, hops} — the shape
        GraphRAG cites from. This is on the contract rather than done in Python
        by the caller because it is the one operation a graph backend does
        natively: SQLite must scan every edge, Neo4j matches an index.

        depth=1 is the direct neighbourhood. depth>1 follows chains, which is
        what lets a question like "who does Sarah's manager also manage?" be
        answered — the answer is two hops away and invisible at depth 1.

        Results are ordered nearest-first then by confidence, so direct facts
        always outrank inferred context. Deduplicated on
        (subject, relation, object) so parallel edges asserting the same thing
        from different notes collapse to one.
        """

    # -- stats -------------------------------------------------------------

    @abstractmethod
    def stats(self) -> dict[str, int]: ...

    # -- workspaces --------------------------------------------------------
    #
    # Not abstract: a backend without workspace support stays usable as a
    # single-graph store rather than failing to instantiate. Anything that
    # needs several graphs must implement these.

    workspace: str = "default"

    def _unsupported(self, what: str):
        return NotImplementedError(
            f"{type(self).__name__} does not support {what}. "
            "Use GRAPH_BACKEND=sqlite, or implement it for this backend."
        )

    def list_workspaces(self) -> list[dict[str, Any]]:
        raise self._unsupported("listing workspaces")

    def create_workspace(self, ws: Any) -> dict[str, Any]:
        raise self._unsupported("creating workspaces")

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        raise self._unsupported("reading workspaces")

    def delete_workspace(self, workspace_id: str) -> None:
        raise self._unsupported("deleting workspaces")

    def search_notes_across(
        self, query: str, workspaces: list[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search several workspaces at once; None means all of them.

        Separate from search_notes deliberately: crossing the partition must be
        something a caller asks for, never something that happens because a
        filter was forgotten.
        """
        raise self._unsupported("cross-workspace search")
