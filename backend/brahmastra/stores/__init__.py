"""
Storage backend selection.

    GRAPH_BACKEND=sqlite   (default) local single-file store
    GRAPH_BACKEND=neo4j    shared property graph — see docs/NEO4J_DATA_MODEL.md

The store is resolved lazily and cached, so a backend switch in the
environment is picked up by the next process without any import-order
subtlety. Tests call reset_store() to drop the cache.
"""

from __future__ import annotations

import os

from brahmastra.stores.base import GraphStore
from brahmastra.stores.sqlite_store import SQLiteStore

_store: GraphStore | None = None
_store_backend: str | None = None


def backend_name() -> str:
    """Configured backend, read at call time."""
    return os.environ.get("GRAPH_BACKEND", "sqlite").lower().strip() or "sqlite"


def get_store() -> GraphStore:
    """
    Return the active store, constructing it on first use.

    The cache is keyed on the backend name so changing GRAPH_BACKEND mid-process
    (which tests do) rebuilds rather than silently returning the old backend.
    """
    global _store, _store_backend
    name = backend_name()
    if _store is not None and _store_backend == name:
        return _store

    if name == "sqlite":
        store: GraphStore = SQLiteStore()
    elif name == "neo4j":
        # Imported lazily: the neo4j driver is an optional dependency, so a
        # sqlite-only install must not need it present.
        from brahmastra.stores.neo4j_store import Neo4jStore
        store = Neo4jStore()
    else:
        raise ValueError(
            f"Unknown GRAPH_BACKEND {name!r}. Expected 'sqlite' or 'neo4j'."
        )

    _store, _store_backend = store, name
    return store


def reset_store() -> None:
    """Drop the cached store. For tests that switch backend or DB path."""
    global _store, _store_backend
    _store = _store_backend = None


__all__ = ["GraphStore", "SQLiteStore", "backend_name", "get_store", "reset_store"]
