"""
SQLite backend — the default GraphStore.

This is the implementation that used to live directly in db.py. Behaviour is
unchanged; it now sits behind the GraphStore contract so a networked backend
can replace it without touching callers.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra.stores.base import GraphStore
from brahmastra.workspace import (
    DEFAULT_WORKSPACE, Workspace, current_workspace, validate_id,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
_DEFAULT_DB = _BACKEND_DIR / "data" / "concept_graph.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    notion_database_id TEXT,
    ontology           TEXT NOT NULL DEFAULT 'default',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id                TEXT NOT NULL,
    workspace_id      TEXT NOT NULL DEFAULT 'default',
    title             TEXT NOT NULL,
    content           TEXT NOT NULL,
    last_edited       TEXT,
    last_synced       TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(extraction_status IN ('pending','done','error')),
    extraction_error TEXT,
    -- Notion projection. `publish` is opt-in curation: a note is only given a
    -- Notion page when something asks for one, so session checkpoints and
    -- working memory stay in the graph instead of filling a human workspace.
    -- `notion_page_id` is the page created for it, and MUST be persisted —
    -- without it every run creates the page again instead of updating it.
    -- For notes pulled FROM Notion this stays NULL: their own id is the page id.
    publish           INTEGER NOT NULL DEFAULT 0,
    notion_page_id    TEXT,
    -- Where this note came from: notion | mcp | ui | cli | checkpoint |
    -- migration | unknown. Five different writers reach this table and, once
    -- written, a paragraph distilled from a transcript by a 7B model is
    -- indistinguishable from prose a person typed in Notion. Recording origin
    -- is what lets retrieval weight them differently later.
    source            TEXT NOT NULL DEFAULT 'unknown',
    -- Ids are unique WITHIN a workspace, not globally: two workspaces may
    -- legitimately hold notes with the same id from different Notion sources.
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE IF NOT EXISTS raw_triples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    TEXT NOT NULL DEFAULT 'default',
    subject_text    TEXT NOT NULL,
    subject_type    TEXT NOT NULL,
    relation        TEXT NOT NULL,
    object_text     TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_quote    TEXT,
    source_note_id  TEXT,
    extracted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_map (
    workspace_id    TEXT NOT NULL DEFAULT 'default',
    mention_text    TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    cluster_id      TEXT NOT NULL,
    -- Per workspace: a "Sarah" at work and a "Sarah" at home are different
    -- people and must never resolve to one another.
    PRIMARY KEY (workspace_id, mention_text)
);

CREATE TABLE IF NOT EXISTS entity_clusters (
    workspace_id    TEXT NOT NULL DEFAULT 'default',
    cluster_id      TEXT NOT NULL,
    canonical_name  TEXT NOT NULL,
    all_mentions    TEXT NOT NULL,  -- JSON array
    PRIMARY KEY (workspace_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS graph_cache (
    -- Was a singleton (CHECK(id = 1)); now one built graph per workspace.
    workspace_id TEXT PRIMARY KEY,
    built_at     TEXT NOT NULL,
    graph_json   TEXT NOT NULL,
    stats_json   TEXT NOT NULL
);

"""

# Indexes are applied AFTER the workspace migration, never with the tables.
# CREATE TABLE IF NOT EXISTS is a no-op on a pre-workspace database, so its
# raw_triples still lacks workspace_id — and an index naming that column then
# fails with "no such column". Tables, then migrate, then index.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_triples_note ON raw_triples(workspace_id, source_note_id);
CREATE INDEX IF NOT EXISTS idx_triples_ws ON raw_triples(workspace_id);
CREATE INDEX IF NOT EXISTS idx_canonical_cluster ON canonical_map(workspace_id, cluster_id);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(workspace_id, extraction_status);
"""


def db_path() -> Path:
    """
    Resolve the DB location at CALL time, not import time.

    Import-time resolution silently ignored any BRAHMASTRA_DB set afterwards,
    so tests that monkeypatched it ran against the real production database.
    """
    return Path(os.environ.get("BRAHMASTRA_DB", str(_DEFAULT_DB)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_migration(path: Path) -> None:
    """
    Run the workspace migration on its own connection, foreign keys OFF.

    Both details are load-bearing:

    * Rebuilding `notes` means dropping it, and the pre-workspace schema had
      `raw_triples.source_note_id REFERENCES notes(id) ON DELETE CASCADE`.
      With foreign keys enforced, that drop CASCADES and deletes every triple
      in the database — 395 of them, silently, on a migration that otherwise
      looks successful.
    * `PRAGMA foreign_keys` is a no-op inside a transaction, so it cannot be
      set on the normal pooled connection, which is always mid-transaction.

    SQLite's own documented table-rebuild procedure is exactly this: disable
    foreign keys, rebuild, re-enable.
    """
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        _migrate_to_workspaces(conn)
        _add_missing_columns(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Add columns introduced after a database was created.

    Purely additive, so unlike the workspace migration this needs no table
    rebuild — which is the whole point. Rebuilding `notes` is what once
    cascaded and deleted every triple; ALTER TABLE ADD COLUMN touches nothing
    else. Adding a column is also the one schema change SQLite does cheaply.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
    if "publish" not in have:
        conn.execute("ALTER TABLE notes ADD COLUMN publish INTEGER NOT NULL DEFAULT 0")
    if "notion_page_id" not in have:
        conn.execute("ALTER TABLE notes ADD COLUMN notion_page_id TEXT")
    if "extraction_error" not in have:
        # Status alone said a note failed but never why. Diagnosing one meant
        # re-running extraction by hand to see the exception, by which point a
        # transient rate limit had usually cleared and left no trace at all.
        conn.execute("ALTER TABLE notes ADD COLUMN extraction_error TEXT")
    if "source" not in have:
        conn.execute(
            "ALTER TABLE notes ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
        )
        # Backfill only what the data itself proves. A Notion-shaped id IS a
        # Notion page id, and the checkpoint drain sets its own id prefix.
        # Everything else could be mcp, ui or cli, so it stays 'unknown'
        # rather than being guessed — a wrong provenance is worse than none.
        conn.execute(
            "UPDATE notes SET source = 'notion' "
            "WHERE length(id) - length(replace(id, '-', '')) = 4"
        )
        conn.execute(
            "UPDATE notes SET source = 'checkpoint' WHERE id LIKE 'checkpoint-%'"
        )


def _migrate_to_workspaces(conn: sqlite3.Connection) -> None:
    """
    Bring a pre-workspace database up to the partitioned schema, in place.

    Existing rows land in DEFAULT_WORKSPACE, so a single-graph install keeps
    working with no visible change. Idempotent: safe to run on every startup.

    Adding a column is cheap; graph_cache needs a rebuild because its old
    definition pinned it to a single row (CHECK(id = 1)) and SQLite cannot
    drop a constraint in place.
    """
    def columns(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def has_fk(table: str) -> bool:
        try:
            return bool(list(conn.execute(f"PRAGMA foreign_key_list({table})")))
        except sqlite3.Error:
            return False

    # These three need their PRIMARY KEY widened to include workspace_id, and
    # SQLite cannot alter a primary key in place — ALTER TABLE ADD COLUMN
    # leaves the old single-column key, so an upsert with a composite
    # ON CONFLICT finds no matching constraint and fails at runtime. Rebuild
    # each: create the new shape, copy rows in under the default workspace,
    # swap.
    rebuilds = {
        # raw_triples must be rebuilt rather than column-added: its
        # source_note_id referenced notes(id), and notes' key is now
        # (workspace_id, id), so the single-column reference no longer resolves
        # — every insert would fail with "foreign key mismatch". The rebuilt
        # table drops the constraint; delete_note removes triples explicitly,
        # so the cascade is not needed (and its cascade is what destroyed the
        # triples during an earlier attempt at this migration).
        "raw_triples": (
            """
            CREATE TABLE raw_triples__new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id    TEXT NOT NULL DEFAULT 'default',
                subject_text    TEXT NOT NULL,
                subject_type    TEXT NOT NULL,
                relation        TEXT NOT NULL,
                object_text     TEXT NOT NULL,
                object_type     TEXT NOT NULL,
                confidence      REAL NOT NULL DEFAULT 1.0,
                source_quote    TEXT,
                source_note_id  TEXT,
                extracted_at    TEXT NOT NULL
            )
            """,
            "INSERT INTO raw_triples__new (workspace_id, subject_text, subject_type, "
            "relation, object_text, object_type, confidence, source_quote, "
            "source_note_id, extracted_at) SELECT ?, subject_text, subject_type, "
            "relation, object_text, object_type, confidence, source_quote, "
            "source_note_id, extracted_at FROM raw_triples",
        ),
        "notes": (
            """
            CREATE TABLE notes__new (
                id                TEXT NOT NULL,
                workspace_id      TEXT NOT NULL DEFAULT 'default',
                title             TEXT NOT NULL,
                content           TEXT NOT NULL,
                last_edited       TEXT,
                last_synced       TEXT,
                extraction_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(extraction_status IN ('pending','done','error')),
                PRIMARY KEY (workspace_id, id)
            )
            """,
            "INSERT INTO notes__new (id, workspace_id, title, content, last_edited, "
            "last_synced, extraction_status) SELECT id, ?, title, content, "
            "last_edited, last_synced, extraction_status FROM notes",
        ),
        "canonical_map": (
            """
            CREATE TABLE canonical_map__new (
                workspace_id    TEXT NOT NULL DEFAULT 'default',
                mention_text    TEXT NOT NULL,
                canonical_name  TEXT NOT NULL,
                cluster_id      TEXT NOT NULL,
                PRIMARY KEY (workspace_id, mention_text)
            )
            """,
            "INSERT INTO canonical_map__new (workspace_id, mention_text, "
            "canonical_name, cluster_id) SELECT ?, mention_text, canonical_name, "
            "cluster_id FROM canonical_map",
        ),
        "entity_clusters": (
            """
            CREATE TABLE entity_clusters__new (
                workspace_id    TEXT NOT NULL DEFAULT 'default',
                cluster_id      TEXT NOT NULL,
                canonical_name  TEXT NOT NULL,
                all_mentions    TEXT NOT NULL,
                PRIMARY KEY (workspace_id, cluster_id)
            )
            """,
            "INSERT INTO entity_clusters__new (workspace_id, cluster_id, "
            "canonical_name, all_mentions) SELECT ?, cluster_id, canonical_name, "
            "all_mentions FROM entity_clusters",
        ),
    }

    for table, (create_sql, copy_sql) in rebuilds.items():
        cols = columns(table)
        if not cols:
            continue  # table absent — the fresh schema already made it
        # Rebuild when the partition column is missing, and ALSO when a stale
        # foreign key survives from the pre-workspace schema: a database
        # migrated by an earlier attempt has workspace_id but still points at
        # notes(id), which no longer resolves against the composite key.
        if "workspace_id" in cols and not has_fk(table):
            continue
        # Preserve existing partitioning when re-migrating a table that
        # already has the column; only genuinely unpartitioned rows default.
        ws_expr = "workspace_id" if "workspace_id" in cols else "?"
        params = () if ws_expr == "workspace_id" else (DEFAULT_WORKSPACE,)
        conn.execute(create_sql)
        conn.execute(copy_sql.replace("SELECT ?,", f"SELECT {ws_expr},"), params)
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")

    cache_cols = columns("graph_cache")
    if cache_cols and "workspace_id" not in cache_cols:
        # Old singleton table: carry its one row over as the default workspace.
        conn.execute("ALTER TABLE graph_cache RENAME TO graph_cache_legacy")
        conn.execute(
            """
            CREATE TABLE graph_cache (
                workspace_id TEXT PRIMARY KEY,
                built_at     TEXT NOT NULL,
                graph_json   TEXT NOT NULL,
                stats_json   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO graph_cache "
            "(workspace_id, built_at, graph_json, stats_json) "
            "SELECT ?, built_at, graph_json, stats_json FROM graph_cache_legacy",
            (DEFAULT_WORKSPACE,),
        )
        conn.execute("DROP TABLE graph_cache_legacy")


class SQLiteStore(GraphStore):
    """
    Local single-file store, scoped to one workspace.

    The workspace is bound at construction and every query adds the filter
    itself. Callers cannot pass a workspace predicate, so there is no code
    path where one can be forgotten — the containment described in
    docs/WORKSPACES_DESIGN.md §2.
    """

    def __init__(self, workspace: str | None = None) -> None:
        self.workspace = validate_id(workspace) if workspace else current_workspace()

    # -- connection --------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Yield a connection, commit/rollback on exit, then CLOSE it.

        `with sqlite3.connect(...) as conn` manages only the transaction and
        never closes the handle, which leaked a connection per call and kept
        the file locked on Windows.
        """
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout + busy_timeout: if another process holds a write lock, WAIT
        # up to 10s rather than instantly raising "database is locked". Keeps
        # concurrent writers (pipeline + live_sync watcher) safe.
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- lifecycle ---------------------------------------------------------

    def init_schema(self) -> None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        # Separate connection with foreign keys off — see _run_migration.
        _run_migration(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA_INDEXES)
            # The workspace must exist as a row so it can be listed before it
            # holds any content.
            conn.execute(
                "INSERT OR IGNORE INTO workspaces (id, name, description, ontology, created_at) "
                "VALUES (?, ?, '', 'default', ?)",
                (self.workspace, self.workspace, _now()),
            )

    def describe(self) -> str:
        # The workspace is part of the identity: two workspaces in one file are
        # different stores, and the pipeline lock keys off this string.
        return f"sqlite:{db_path()}#{self.workspace}"

    # -- workspace registry ------------------------------------------------

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def create_workspace(self, ws: Workspace) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspaces
                    (id, name, description, notion_database_id, ontology, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    notion_database_id = excluded.notion_database_id,
                    ontology = excluded.ontology
                """,
                (ws.id, ws.name, ws.description, ws.notion_database_id,
                 ws.ontology, ws.created_at),
            )
        return ws.to_dict()

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_workspace(self, workspace_id: str) -> None:
        """Remove a workspace and everything partitioned under it."""
        with self._connect() as conn:
            for table in ("notes", "raw_triples", "canonical_map",
                          "entity_clusters", "graph_cache"):
                conn.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,))
            conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))

    # -- notes -------------------------------------------------------------

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
        status = "pending" if mark_pending else "done"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (id, workspace_id, title, content, last_edited,
                                   last_synced, extraction_status, publish, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, id) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    last_edited = excluded.last_edited,
                    last_synced = excluded.last_synced,
                    -- None means "leave as is", so a sync cannot silently
                    -- unpublish a note somebody chose to publish.
                    publish = COALESCE(?, notes.publish),
                    -- Keep a known origin, but allow 'unknown' to be
                    -- upgraded. COALESCE alone is wrong here: it prefers the
                    -- NEW value, so a Notion sync re-upserting an MCP note
                    -- would relabel it and provenance would decay to whichever
                    -- job ran last.
                    source = CASE
                        WHEN notes.source IS NULL OR notes.source = 'unknown'
                            THEN COALESCE(?, 'unknown')
                        ELSE notes.source
                    END,
                    extraction_status = CASE
                        WHEN excluded.extraction_status = 'pending' THEN 'pending'
                        WHEN excluded.last_edited != notes.last_edited THEN 'pending'
                        ELSE notes.extraction_status
                    END
                """,
                (id, self.workspace, title, content, last_edited, _now(), status,
                 1 if publish else 0, source or "unknown",
                 None if publish is None else (1 if publish else 0),
                 source),
            )

    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE workspace_id = ? AND extraction_status = ? "
                    "ORDER BY last_edited DESC",
                    (self.workspace, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE workspace_id = ? ORDER BY last_edited DESC",
                    (self.workspace,),
                ).fetchall()
        return [dict(r) for r in rows]

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes WHERE workspace_id = ? ORDER BY last_edited DESC",
                (self.workspace,),
            ).fetchall()

        scored: list[tuple[int, dict[str, Any]]] = []
        for r in rows:
            d = dict(r)
            hay = f"{d.get('title','')} {d.get('content','')}".lower()
            matched = sum(1 for t in terms if t in hay)
            if matched:
                scored.append((matched, d))

        # Prefer notes containing ALL terms; otherwise rank by # matched.
        all_terms = [s for s in scored if s[0] == len(terms)]
        pool = all_terms if all_terms else scored
        pool.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in pool[:limit]]

    def get_note(self, id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE workspace_id = ? AND id = ?",
                (self.workspace, id),
            ).fetchone()
        return dict(row) if row else None

    def get_notes_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """One statement instead of one per id. Missing ids are simply absent."""
        if not ids:
            return {}
        # Deduplicated so a fact cited twice does not widen the query, and
        # chunked because SQLite caps host parameters (999 on older builds).
        unique = list(dict.fromkeys(ids))
        found: dict[str, dict[str, Any]] = {}
        with self._connect() as conn:
            for i in range(0, len(unique), 500):
                chunk = unique[i:i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT * FROM notes WHERE workspace_id = ? AND id IN ({marks})",
                    (self.workspace, *chunk),
                ).fetchall()
                for row in rows:
                    found[row["id"]] = dict(row)
        return found

    def set_notion_page_id(self, note_id: str, page_id: str) -> None:
        """Remember the Notion page created for this note."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET notion_page_id = ? WHERE workspace_id = ? AND id = ?",
                (page_id, self.workspace, note_id),
            )

    def set_note_status(self, id: str, status: str, error: str | None = None) -> None:
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid extraction status: {status!r}")
        # The message is written and cleared with the status, never separately.
        # A stale error left on a note that has since succeeded is worse than
        # no message at all: it reads as a live failure and sends the next
        # reader diagnosing something that was already fixed.
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET extraction_status = ?, extraction_error = ? "
                "WHERE workspace_id = ? AND id = ?",
                (status, error if status == "error" else None, self.workspace, id),
            )

    def delete_note(self, id: str) -> None:
        with self._connect() as conn:
            # Triples cascade via the FK, but delete explicitly so behaviour
            # does not depend on PRAGMA foreign_keys being on.
            conn.execute(
                "DELETE FROM raw_triples WHERE workspace_id = ? AND source_note_id = ?",
                (self.workspace, id),
            )
            conn.execute(
                "DELETE FROM notes WHERE workspace_id = ? AND id = ?", (self.workspace, id)
            )

    # -- triples -----------------------------------------------------------

    def delete_triples_for_note(self, note_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM raw_triples WHERE workspace_id = ? AND source_note_id = ?",
                (self.workspace, note_id),
            )

    def insert_triples(self, triples: list[dict[str, Any]]) -> None:
        now = _now()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO raw_triples
                    (workspace_id, subject_text, subject_type, relation, object_text,
                     object_type, confidence, source_quote, source_note_id, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self.workspace,
                        t["subject_text"],
                        t.get("subject_type", "unknown"),
                        t["relation"],
                        t["object_text"],
                        t.get("object_type", "unknown"),
                        float(t.get("confidence", 1.0)),
                        t.get("source_quote"),
                        t.get("source_note_id"),
                        now,
                    )
                    for t in triples
                ],
            )

    def get_all_triples(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM raw_triples WHERE workspace_id = ? ORDER BY extracted_at DESC",
                (self.workspace,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- entity resolution -------------------------------------------------

    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM canonical_map WHERE workspace_id = ?", (self.workspace,))
            conn.execute("DELETE FROM entity_clusters WHERE workspace_id = ?", (self.workspace,))
            for cluster in clusters:
                cluster_id = cluster["cluster_id"]
                canonical = cluster["canonical_name"]
                mentions = cluster["mentions"]
                conn.execute(
                    "INSERT INTO entity_clusters "
                    "(workspace_id, cluster_id, canonical_name, all_mentions) "
                    "VALUES (?, ?, ?, ?)",
                    (self.workspace, cluster_id, canonical, json.dumps(mentions)),
                )
                for mention in mentions:
                    conn.execute(
                        "INSERT OR REPLACE INTO canonical_map "
                        "(workspace_id, mention_text, canonical_name, cluster_id) "
                        "VALUES (?, ?, ?, ?)",
                        (self.workspace, mention, canonical, cluster_id),
                    )

    def get_canonical_map(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mention_text, canonical_name FROM canonical_map "
                "WHERE workspace_id = ?", (self.workspace,)
            ).fetchall()
        return {r["mention_text"]: r["canonical_name"] for r in rows}

    def get_entity_clusters(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM entity_clusters WHERE workspace_id = ?", (self.workspace,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["mentions"] = json.loads(d["all_mentions"])
            del d["all_mentions"]
            result.append(d)
        return result

    # -- built graph -------------------------------------------------------

    def save_graph(self, graph: dict[str, Any], stats: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO graph_cache (workspace_id, built_at, graph_json, stats_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    built_at = excluded.built_at,
                    graph_json = excluded.graph_json,
                    stats_json = excluded.stats_json
                """,
                (self.workspace, _now(), json.dumps(graph), json.dumps(stats)),
            )

    def load_graph(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_cache WHERE workspace_id = ?", (self.workspace,)
            ).fetchone()
        if not row:
            return None
        return {
            "built_at": row["built_at"],
            "graph": json.loads(row["graph_json"]),
            "stats": json.loads(row["stats_json"]),
        }

    def get_entities(self) -> list[dict[str, Any]]:
        """Nodes come out of the same JSON blob; there is nothing cheaper here."""
        cached = self.load_graph()
        return (cached or {}).get("graph", {}).get("nodes", []) or []

    def search_entities(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """
        Token-overlap matching over entity names.

        No index and no vectors here, so this stays lexical: an entity is only
        found when its words appear in the question. The Neo4j backend fuses
        this with embedding similarity, which is the actual upgrade — this
        exists so callers get the same shape on either backend.
        """
        if not (query or "").strip():
            return []
        q = query.lower()
        q_tokens = {t for t in re.split(r"\W+", q) if len(t) >= 3}
        scored: list[tuple[float, dict[str, Any]]] = []
        for n in self.get_entities():
            label = str(n.get("id", "")).lower()
            if not label:
                continue
            tokens = {t for t in re.split(r"\W+", label) if len(t) >= 3}
            if not tokens:
                continue
            if label in q:
                score = 1.0 + len(tokens)
            else:
                overlap = tokens & q_tokens
                score = len(overlap) / len(tokens) if overlap else 0.0
                if score < 0.5:
                    score = 0.0
            if score > 0:
                scored.append((score + float(n.get("pagerank", 0.0)), n))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored[:limit]]

    def find_path(
        self, source: str, target: str, max_hops: int = 5
    ) -> list[dict[str, Any]]:
        """
        Breadth-first shortest path over the serialised edge list.

        Same result as the Neo4j backend, but it must load and walk the whole
        graph in Python to get there.
        """
        cached = self.load_graph()
        if not cached or not source or not target:
            return []
        edges = cached["graph"].get("edges", [])
        if source == target:
            return []

        # Undirected adjacency: "how are these connected" does not care which
        # way an edge points, but the direction is reported back.
        adj: dict[str, list[tuple[str, dict[str, Any], bool]]] = {}
        for e in edges:
            s, t = e["source"], e["target"]
            adj.setdefault(s, []).append((t, e, True))
            adj.setdefault(t, []).append((s, e, False))

        h = max(1, min(int(max_hops), 10))
        prev: dict[str, tuple[str, dict[str, Any], bool]] = {}
        seen = {source}
        frontier = [source]
        for _ in range(h):
            nxt = []
            for node in frontier:
                for neighbour, edge, forward in adj.get(node, []):
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    prev[neighbour] = (node, edge, forward)
                    if neighbour == target:
                        frontier = []
                        nxt = []
                        break
                    nxt.append(neighbour)
                else:
                    continue
                break
            if target in seen or not nxt:
                break
            frontier = nxt

        if target not in prev:
            return []

        chain = []
        cur = target
        while cur != source:
            node, edge, forward = prev[cur]
            # from/to state the fact as stored; walk_from/walk_to give the
            # traversal order. Same convention as the Neo4j backend.
            chain.append({
                "from": node if forward else cur,
                "relation": edge["relation"],
                "to": cur if forward else node,
                "direction": "forward" if forward else "reverse",
                "walk_from": node,
                "walk_to": cur,
                "note_id": edge.get("note_id", "") or "",
            })
            cur = node
        chain.reverse()
        return chain

    MAX_DEPTH = 3

    def neighbourhood(
        self, names: set[str], limit: int = 40, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        Breadth-first walk over the serialised graph's edges, in Python.

        There is no index to exploit — the graph lives as one JSON blob — so
        each hop rescans every edge. That cost is exactly what the Neo4j
        backend removes; the behaviour is kept identical so callers cannot
        tell the two apart.
        """
        cached = self.load_graph()
        if not cached or not names:
            return []
        edges = cached["graph"].get("edges", [])
        d = max(1, min(int(depth), self.MAX_DEPTH))

        facts: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        frontier = set(names)
        reached = set(names)

        for hop in range(1, d + 1):
            next_frontier: set[str] = set()
            for e in edges:
                src, tgt = e["source"], e["target"]
                if src not in frontier and tgt not in frontier:
                    continue
                key = (src, e["relation"], tgt)
                if key not in seen:
                    seen.add(key)
                    facts.append({
                        "text": f'{src} {e["relation"]} {tgt}',
                        "quote": e.get("source_quote", "") or "",
                        "note_id": e.get("note_id", "") or "",
                        "confidence": float(e.get("confidence", 1.0)),
                        "hops": hop,
                    })
                for end in (src, tgt):
                    if end not in reached:
                        next_frontier.add(end)
                        reached.add(end)
            if not next_frontier:
                break
            frontier = next_frontier

        # Nearest first, then most confident — direct facts outrank context.
        facts.sort(key=lambda f: (f["hops"], -f["confidence"]))
        return facts[:limit]

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        ws = (self.workspace,)
        with self._connect() as conn:
            notes_total = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE workspace_id = ?", ws).fetchone()[0]
            notes_pending = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE workspace_id = ? "
                "AND extraction_status = 'pending'", ws).fetchone()[0]
            triples_total = conn.execute(
                "SELECT COUNT(*) FROM raw_triples WHERE workspace_id = ?", ws).fetchone()[0]
            clusters_total = conn.execute(
                "SELECT COUNT(*) FROM entity_clusters WHERE workspace_id = ?", ws).fetchone()[0]
            graph_cached = conn.execute(
                "SELECT COUNT(*) FROM graph_cache WHERE workspace_id = ?", ws).fetchone()[0]
        return {
            "workspace": self.workspace,
            "notes_total": notes_total,
            "notes_pending": notes_pending,
            "triples_total": triples_total,
            "entity_clusters": clusters_total,
            "graph_cached": bool(graph_cached),
        }

    # -- cross-workspace ---------------------------------------------------

    def search_notes_across(
        self, query: str, workspaces: list[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Search several workspaces at once. `None` means every workspace.

        Separate from search_notes on purpose: crossing the partition must be
        something a caller asks for explicitly, never something that happens
        because a filter was forgotten. Every result carries its workspace_id
        so the caller can always tell where a hit came from.
        """
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        with self._connect() as conn:
            if workspaces:
                marks = ",".join("?" * len(workspaces))
                rows = conn.execute(
                    f"SELECT * FROM notes WHERE workspace_id IN ({marks}) "
                    "ORDER BY last_edited DESC", tuple(workspaces),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY last_edited DESC"
                ).fetchall()

        scored: list[tuple[int, dict[str, Any]]] = []
        for r in rows:
            d = dict(r)
            hay = f"{d.get('title','')} {d.get('content','')}".lower()
            matched = sum(1 for t in terms if t in hay)
            if matched:
                scored.append((matched, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]
