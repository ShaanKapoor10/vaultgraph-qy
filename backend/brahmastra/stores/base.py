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


class GraphStore(ABC):
    """Persistence for notes, extracted triples, resolved entities and the graph."""

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def init_schema(self) -> None:
        """Create whatever the backend needs. Must be idempotent."""

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

    @abstractmethod
    def set_note_status(self, id: str, status: str) -> None:
        """Set extraction status: 'pending' | 'done' | 'error'."""

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

    # -- stats -------------------------------------------------------------

    @abstractmethod
    def stats(self) -> dict[str, int]: ...
