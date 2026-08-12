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
_store_backend: tuple[str, str] | None = None


def backend_name() -> str:
    """Configured backend, read at call time."""
    return os.environ.get("GRAPH_BACKEND", "sqlite").lower().strip() or "sqlite"


def _build(name: str, workspace: str | None) -> GraphStore:
    if name == "sqlite":
        return SQLiteStore(workspace=workspace)
    if name == "neo4j":
        # Imported lazily: the neo4j driver is an optional dependency, so a
        # sqlite-only install must not need it present.
        from brahmastra.stores.neo4j_store import Neo4jStore
        return Neo4jStore()
    raise ValueError(
        f"Unknown GRAPH_BACKEND {name!r}. Expected 'sqlite' or 'neo4j'."
    )


def get_store(workspace: str | None = None) -> GraphStore:
    """
    Return a store, constructing it on first use.

    Without `workspace` this returns the cached process-wide store for the
    current workspace. Passing one builds an uncached store bound to that
    workspace, so a one-off read of another graph cannot disturb the default
    every other module-level call is using.

    The cache is keyed on backend AND workspace, so changing either mid-process
    (which tests do) rebuilds rather than silently returning the previous one.
    """
    global _store, _store_backend
    name = backend_name()

    if workspace is not None:
        return _build(name, workspace)

    from brahmastra.workspace import current_workspace
    key = (name, current_workspace())
    if _store is not None and _store_backend == key:
        return _store

    store = _build(name, None)
    _store, _store_backend = store, key
    return store


def reset_store() -> None:
    """Drop the cached store. For tests that switch backend or DB path."""
    global _store, _store_backend
    _store = _store_backend = None


__all__ = ["GraphStore", "SQLiteStore", "backend_name", "get_store", "reset_store"]
