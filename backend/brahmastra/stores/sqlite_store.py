"""
SQLite backend — the default GraphStore.

This is the implementation that used to live directly in db.py. Behaviour is
unchanged; it now sits behind the GraphStore contract so a networked backend
can replace it without touching callers.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra.stores.base import GraphStore

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
_DEFAULT_DB = _BACKEND_DIR / "data" / "concept_graph.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    content           TEXT NOT NULL,
    last_edited       TEXT,
    last_synced       TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(extraction_status IN ('pending','done','error'))
);

CREATE TABLE IF NOT EXISTS raw_triples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_text    TEXT NOT NULL,
    subject_type    TEXT NOT NULL,
    relation        TEXT NOT NULL,
    object_text     TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_quote    TEXT,
    source_note_id  TEXT REFERENCES notes(id) ON DELETE CASCADE,
    extracted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canonical_map (
    mention_text    TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    cluster_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_clusters (
    cluster_id      TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    all_mentions    TEXT NOT NULL  -- JSON array
);

CREATE TABLE IF NOT EXISTS graph_cache (
    id          INTEGER PRIMARY KEY CHECK(id = 1),  -- singleton row
    built_at    TEXT NOT NULL,
    graph_json  TEXT NOT NULL,
    stats_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_triples_note ON raw_triples(source_note_id);
CREATE INDEX IF NOT EXISTS idx_canonical_cluster ON canonical_map(cluster_id);
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


class SQLiteStore(GraphStore):
    """Local single-file store. Fast, zero-setup, single-machine."""

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
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def describe(self) -> str:
        return f"sqlite:{db_path()}"

    # -- notes -------------------------------------------------------------

    def upsert_note(
        self,
        id: str,
        title: str,
        content: str,
        last_edited: str | None = None,
        mark_pending: bool = True,
    ) -> None:
        status = "pending" if mark_pending else "done"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (id, title, content, last_edited, last_synced, extraction_status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    last_edited = excluded.last_edited,
                    last_synced = excluded.last_synced,
                    extraction_status = CASE
                        WHEN excluded.extraction_status = 'pending' THEN 'pending'
                        WHEN excluded.last_edited != notes.last_edited THEN 'pending'
                        ELSE notes.extraction_status
                    END
                """,
                (id, title, content, last_edited, _now(), status),
            )

    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE extraction_status = ? ORDER BY last_edited DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY last_edited DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notes ORDER BY last_edited DESC").fetchall()

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
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def set_note_status(self, id: str, status: str) -> None:
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid extraction status: {status!r}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET extraction_status = ? WHERE id = ?", (status, id)
            )

    def delete_note(self, id: str) -> None:
        with self._connect() as conn:
            # Triples cascade via the FK, but delete explicitly so behaviour
            # does not depend on PRAGMA foreign_keys being on.
            conn.execute("DELETE FROM raw_triples WHERE source_note_id = ?", (id,))
            conn.execute("DELETE FROM notes WHERE id = ?", (id,))

    # -- triples -----------------------------------------------------------

    def delete_triples_for_note(self, note_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM raw_triples WHERE source_note_id = ?", (note_id,))

    def insert_triples(self, triples: list[dict[str, Any]]) -> None:
        now = _now()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO raw_triples
                    (subject_text, subject_type, relation, object_text, object_type,
                     confidence, source_quote, source_note_id, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
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
                "SELECT * FROM raw_triples ORDER BY extracted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- entity resolution -------------------------------------------------

    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM canonical_map")
            conn.execute("DELETE FROM entity_clusters")
            for cluster in clusters:
                cluster_id = cluster["cluster_id"]
                canonical = cluster["canonical_name"]
                mentions = cluster["mentions"]
                conn.execute(
                    "INSERT INTO entity_clusters (cluster_id, canonical_name, all_mentions) "
                    "VALUES (?, ?, ?)",
                    (cluster_id, canonical, json.dumps(mentions)),
                )
                for mention in mentions:
                    conn.execute(
                        "INSERT OR REPLACE INTO canonical_map "
                        "(mention_text, canonical_name, cluster_id) VALUES (?, ?, ?)",
                        (mention, canonical, cluster_id),
                    )

    def get_canonical_map(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mention_text, canonical_name FROM canonical_map"
            ).fetchall()
        return {r["mention_text"]: r["canonical_name"] for r in rows}

    def get_entity_clusters(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM entity_clusters").fetchall()
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
                INSERT INTO graph_cache (id, built_at, graph_json, stats_json)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    built_at = excluded.built_at,
                    graph_json = excluded.graph_json,
                    stats_json = excluded.stats_json
                """,
                (_now(), json.dumps(graph), json.dumps(stats)),
            )

    def load_graph(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM graph_cache WHERE id = 1").fetchone()
        if not row:
            return None
        return {
            "built_at": row["built_at"],
            "graph": json.loads(row["graph_json"]),
            "stats": json.loads(row["stats_json"]),
        }

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            notes_total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            notes_pending = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE extraction_status = 'pending'"
            ).fetchone()[0]
            triples_total = conn.execute("SELECT COUNT(*) FROM raw_triples").fetchone()[0]
            clusters_total = conn.execute("SELECT COUNT(*) FROM entity_clusters").fetchone()[0]
            graph_cached = conn.execute("SELECT COUNT(*) FROM graph_cache").fetchone()[0]
        return {
            "notes_total": notes_total,
            "notes_pending": notes_pending,
            "triples_total": triples_total,
            "entity_clusters": clusters_total,
            "graph_cached": bool(graph_cached),
        }
