"""
Workspaces — several independent knowledge graphs in one system.

A workspace is a partition key. Everything a store reads or writes is scoped
to one, so a personal graph, a work graph and a per-project graph coexist
without seeing each other.

Resolution order, first match wins:

  1. explicit argument   db.for_workspace("office")
  2. BRAHMASTRA_WORKSPACE environment variable
  3. DEFAULT_WORKSPACE ("default")

Existing single-graph installs keep working untouched: their data migrates
into `default`, which is also what resolves when nothing is set.

See docs/WORKSPACES_DESIGN.md for why isolation is a property rather than a
separate database (Aura Free cannot CREATE DATABASE) and how the leak risk is
contained.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_WORKSPACE = "default"

# A workspace id is used as a partition key, appears in URLs, and is part of
# index keys — so keep it to a slug rather than accepting arbitrary text.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

# Reserved because they read as commands rather than names at the API/MCP
# surface, where "all" already means "every workspace".
_RESERVED = frozenset({"all", "none", "null", "new", "list", "create", "_"})


class InvalidWorkspaceId(ValueError):
    """The id is not a usable partition key."""


def slugify(name: str) -> str:
    """Turn a display name into a candidate id ('My Office!' -> 'my-office')."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:63]


def validate_id(workspace_id: str) -> str:
    """Return the id if usable, else raise with the reason."""
    wid = (workspace_id or "").strip().lower()
    if not wid:
        raise InvalidWorkspaceId("workspace id cannot be empty")
    if wid in _RESERVED:
        raise InvalidWorkspaceId(f"{wid!r} is reserved")
    if not _SLUG_RE.match(wid):
        raise InvalidWorkspaceId(
            f"{wid!r} must be lowercase letters, digits, '-' or '_', "
            "start alphanumeric, and be at most 63 characters"
        )
    return wid


def current_workspace() -> str:
    """
    The workspace to use when a caller does not name one.

    Read at call time, not import time, so a request or test can set it and
    have the next store resolution pick it up.
    """
    wid = os.environ.get("BRAHMASTRA_WORKSPACE", "").strip().lower()
    if not wid:
        return DEFAULT_WORKSPACE
    try:
        return validate_id(wid)
    except InvalidWorkspaceId:
        # A malformed env var should not take the process down; fall back
        # rather than partition data under an id that cannot be queried back.
        return DEFAULT_WORKSPACE


@dataclass
class Workspace:
    """A named knowledge graph."""

    id: str
    name: str = ""
    description: str = ""
    # This workspace's own Notion source. Falls back to the global
    # NOTION_DATABASE_ID so existing single-workspace setups are unaffected.
    notion_database_id: str | None = None
    # Which ontology governs extraction here. Every workspace uses "default"
    # today; the field exists so a per-workspace vocabulary can be added later
    # without migrating anything. See docs/WORKSPACES_DESIGN.md §5.
    ontology: str = "default"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        self.id = validate_id(self.id)
        if not self.name:
            self.name = self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "notion_database_id": self.notion_database_id,
            "ontology": self.ontology,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Workspace:
        return cls(
            id=row["id"],
            name=row.get("name") or row["id"],
            description=row.get("description") or "",
            notion_database_id=row.get("notion_database_id"),
            ontology=row.get("ontology") or "default",
            created_at=row.get("created_at")
            or datetime.now(timezone.utc).isoformat(),
        )


def notion_database_for(ws: Workspace | None) -> str | None:
    """
    The Notion source this workspace syncs from.

    Per-workspace value wins; otherwise the global NOTION_DATABASE_ID, so a
    single-workspace install needs no configuration change at all.
    """
    if ws and ws.notion_database_id:
        return ws.notion_database_id
    return os.environ.get("NOTION_DATABASE_ID") or None
