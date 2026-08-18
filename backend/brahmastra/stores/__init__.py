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
from pathlib import Path

from brahmastra.stores.base import GraphStore
from brahmastra.stores.sqlite_store import SQLiteStore

# Load backend/.env HERE. This module answers "which store?" for every process
# in the system, and it answered it before anything had read the config file —
# so GRAPH_BACKEND in .env was silently ignored and every process fell back to
# the sqlite default. That is why an MCP-added note could be invisible to a
# pipeline run: the two processes disagreed about where "the" database was.
#
# The backend name must resolve the same way whether the caller is uvicorn, the
# MCP server, a hook, the CLI or a bare `python -c`, and .env is the one place
# they can all agree on.
_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)  # never overrides an already-set var; tests keep control
    except ImportError:
        pass

_store: GraphStore | None = None
_store_backend: tuple[str, str] | None = None


def backend_name() -> str:
    """Configured backend, read at call time."""
    return os.environ.get("GRAPH_BACKEND", "sqlite").lower().strip() or "sqlite"


class WorkspaceBindingError(RuntimeError):
    """A backend did not bind to the workspace it was given."""


def _build(name: str, workspace: str) -> GraphStore:
    """
    Construct a store bound to `workspace`, and refuse to return one that is not.

    The workspace is always resolved by the caller and passed explicitly, never
    left for the backend to default. That defaulting is what made the original
    leak possible: Neo4jStore was constructed with no argument, quietly fell
    back to the process default, and writes meant for one graph landed in
    another — overwriting a real note before a test caught it.

    The post-check turns that failure from silent to loud. A backend that
    ignores or mishandles the argument now fails at construction, before it can
    write anything, rather than corrupting another workspace's data.
    """
    if name == "sqlite":
        store: GraphStore = SQLiteStore(workspace=workspace)
    elif name == "neo4j":
        # Imported lazily: the neo4j driver is an optional dependency, so a
        # sqlite-only install must not need it present.
        from brahmastra.stores.neo4j_store import Neo4jStore
        store = Neo4jStore(workspace=workspace)
    else:
        raise ValueError(
            f"Unknown GRAPH_BACKEND {name!r}. Expected 'sqlite' or 'neo4j'."
        )

    bound = getattr(store, "workspace", None)
    if bound != workspace:
        raise WorkspaceBindingError(
            f"{type(store).__name__} was asked for workspace {workspace!r} but "
            f"bound to {bound!r}. Refusing to return a store that would write "
            f"to the wrong graph."
        )
    return store


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
    from brahmastra.workspace import current_workspace

    name = backend_name()
    # Resolve here so a backend never has to decide what "no workspace" means.
    target = workspace if workspace is not None else current_workspace()

    if workspace is not None:
        return _build(name, target)

    key = (name, target)
    if _store is not None and _store_backend == key:
        return _store

    store = _build(name, target)
    _store, _store_backend = store, key
    return store


def reset_store() -> None:
    """Drop the cached store. For tests that switch backend or DB path."""
    global _store, _store_backend
    _store = _store_backend = None


__all__ = ["GraphStore", "SQLiteStore", "backend_name", "get_store", "reset_store"]
