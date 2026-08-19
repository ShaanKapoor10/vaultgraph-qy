"""
Public persistence API.

This module is the stable surface the rest of the codebase calls (`db.get_notes()`,
`db.insert_triples()`, ...). The actual storage lives behind the GraphStore
contract in brahmastra/stores/, selected by GRAPH_BACKEND:

    GRAPH_BACKEND=sqlite   (default) local single-file store
    GRAPH_BACKEND=neo4j    shared property graph

Keeping this facade means swapping backends touches no callers. Each function
resolves the store per call rather than binding one at import, so BRAHMASTRA_DB
and GRAPH_BACKEND stay changeable at runtime (which tests rely on).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from brahmastra.stores import backend_name, get_store, reset_store
from brahmastra.stores.sqlite_store import SCHEMA, db_path
from brahmastra.workspace import DEFAULT_WORKSPACE, Workspace

__all__ = [
    "backend_name", "reset_store", "db_path", "describe", "SCHEMA",
    "workspace", "for_workspace", "list_workspaces", "create_workspace",
    "get_workspace", "delete_workspace", "search_notes_across",
    "init_db", "upsert_note", "get_notes", "search_notes", "get_note",
    "mark_note_done", "mark_note_error", "set_note_status", "delete_note",
    "delete_triples_for_note", "insert_triples", "get_all_triples",
    "replace_canonical_map", "get_canonical_map", "get_entity_clusters",
    "cache_graph", "get_cached_graph", "get_entities", "search_entities",
    "find_path", "neighbourhood", "get_db_stats",
]


def describe() -> str:
    """Where data is actually going. Useful when a machine looks at the wrong store."""
    return get_store().describe()


# ---------------------------------------------------------------------------
# Workspaces — several independent graphs in one system
# ---------------------------------------------------------------------------

def workspace() -> str:
    """The workspace these module-level calls are reading and writing."""
    return getattr(get_store(), "workspace", DEFAULT_WORKSPACE)


def for_workspace(workspace_id: str) -> Any:
    """
    A store bound to another workspace, without changing the process default.

    Use this for a one-off read of a different graph. Long-lived selection
    belongs in BRAHMASTRA_WORKSPACE so every module-level call agrees.
    """
    return get_store(workspace=workspace_id)


def list_workspaces() -> list[dict[str, Any]]:
    return get_store().list_workspaces()


def create_workspace(
    id: str,
    name: str = "",
    description: str = "",
    notion_database_id: str | None = None,
) -> dict[str, Any]:
    """Create (or update) a workspace and make sure its schema exists."""
    ws = Workspace(
        id=id, name=name, description=description,
        notion_database_id=notion_database_id,
    )
    created = get_store().create_workspace(ws)
    # Initialise the new partition immediately so it can be written to without
    # a separate setup step.
    get_store(workspace=ws.id).init_schema()
    return created


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    return get_store().get_workspace(workspace_id)


def delete_workspace(workspace_id: str) -> None:
    """Delete a workspace and everything partitioned under it."""
    if workspace_id == DEFAULT_WORKSPACE:
        raise ValueError("refusing to delete the default workspace")
    get_store().delete_workspace(workspace_id)


def search_notes_across(
    query: str, workspaces: list[str] | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Search across workspaces; None means every workspace."""
    return get_store().search_notes_across(query, workspaces, limit)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the schema if absent (idempotent)."""
    get_store().init_schema()


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def upsert_note(
    id: str,
    title: str,
    content: str,
    last_edited: str | None = None,
    mark_pending: bool = True,
    publish: bool | None = None,
    source: str | None = None,
) -> None:
    get_store().upsert_note(
        id, title, content, last_edited, mark_pending, publish, source
    )


def set_notion_page_id(note_id: str, page_id: str) -> None:
    get_store().set_notion_page_id(note_id, page_id)


def get_notes(status: str | None = None) -> list[dict[str, Any]]:
    return get_store().get_notes(status)


def search_notes(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Term search over note title + content — finds notes by what they SAY."""
    return get_store().search_notes(query, limit)


def get_note(id: str) -> dict[str, Any] | None:
    return get_store().get_note(id)


def set_note_status(id: str, status: str, error: str | None = None) -> None:
    """Set extraction status: 'pending' | 'done' | 'error'."""
    get_store().set_note_status(id, status, error)


def mark_note_done(id: str) -> None:
    get_store().set_note_status(id, "done")


def mark_note_error(id: str, error: str | None = None) -> None:
    get_store().set_note_status(id, "error", error)


def delete_note(id: str) -> None:
    """Delete a note and its derived triples."""
    get_store().delete_note(id)


# ---------------------------------------------------------------------------
# Triples
# ---------------------------------------------------------------------------

def delete_triples_for_note(note_id: str) -> None:
    get_store().delete_triples_for_note(note_id)


def insert_triples(triples: list[dict[str, Any]]) -> None:
    get_store().insert_triples(triples)


def get_all_triples() -> list[dict[str, Any]]:
    return get_store().get_all_triples()


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------

def replace_canonical_map(clusters: list[dict[str, Any]]) -> None:
    """Atomically replace the full canonical map from a resolved cluster list."""
    get_store().replace_canonical_map(clusters)


def get_canonical_map() -> dict[str, str]:
    """{mention_text: canonical_name}"""
    return get_store().get_canonical_map()


def get_entity_clusters() -> list[dict[str, Any]]:
    return get_store().get_entity_clusters()


# ---------------------------------------------------------------------------
# Built graph
# ---------------------------------------------------------------------------

def cache_graph(graph_json: dict[str, Any], stats_json: dict[str, Any]) -> None:
    get_store().save_graph(graph_json, stats_json)


def get_cached_graph() -> dict[str, Any] | None:
    return get_store().load_graph()


def get_entities() -> list[dict[str, Any]]:
    """Graph nodes only, no edges — for entity matching."""
    return get_store().get_entities()


def search_entities(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """
    Entities a question is about, best first.

    On Neo4j this fuses fulltext with embedding similarity; on SQLite it is
    token overlap. Same shape either way.
    """
    return get_store().search_entities(query, limit)


def find_path(source: str, target: str, max_hops: int = 5) -> list[dict[str, Any]]:
    """Shortest path between two entities as ordered hops; [] if unconnected."""
    return get_store().find_path(source, target, max_hops)


def neighbourhood(
    names: set[str], limit: int = 40, depth: int = 1
) -> list[dict[str, Any]]:
    """
    Facts within `depth` hops of the given entities, nearest and most
    confident first.

    Backend-dependent by design: SQLite walks the edge list, Neo4j runs an
    indexed traversal. Callers get the same
    {text, quote, note_id, confidence, hops}.
    """
    return get_store().neighbourhood(names, limit, depth)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_db_stats() -> dict[str, int]:
    return get_store().stats()
