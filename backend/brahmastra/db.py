"""
SQLite persistence layer.

Schema is created on first call to init_db().
All reads/writes go through the helpers here — no raw SQL elsewhere.

DB path: backend/data/concept_graph.db
Override with BRAHMASTRA_DB env var.
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

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent  # backend/
_DEFAULT_DB = _HERE / "data" / "concept_graph.db"


def db_path() -> Path:
    """
    Resolve the DB location at CALL time, not import time.

    Import-time resolution silently ignored any BRAHMASTRA_DB set after the
    first `import brahmastra.db` — which meant tests that monkeypatch the env
    var ran against the real production database (and, via the pipeline's
    write-back stage, the real Notion pages).
    """
    return Path(os.environ.get("BRAHMASTRA_DB", str(_DEFAULT_DB)))


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    Yield a connection, commit/rollback on exit, then CLOSE it.

    `with sqlite3.connect(...) as conn` only manages the transaction — it never
    closes the handle. Returning a bare connection therefore leaked one per
    call, which on Windows keeps the file locked (tests could not delete their
    own temp DB) and holds WAL sidecars open longer than needed.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout + busy_timeout: if another process holds a write lock, WAIT up to
    # 10s for it to clear instead of instantly raising "database is locked".
    # This makes concurrent writers (backend pipeline + live_sync watcher) safe.
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:  # preserve the commit-on-success / rollback-on-error semantics
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

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


def init_db() -> None:
    """Create all tables if they don't exist (idempotent)."""
    with _connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Notes helpers
# ---------------------------------------------------------------------------

def upsert_note(
    id: str,
    title: str,
    content: str,
    last_edited: str | None = None,
    mark_pending: bool = True,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "pending" if mark_pending else "done"
    with _connect() as conn:
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
            (id, title, content, last_edited, now, status),
        )


def get_notes(status: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
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


def search_notes(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Term-based search over note title + content (case-insensitive).

    Splits the query into words and ranks notes by how many terms appear in the
    title+content. Prefers notes containing ALL terms; falls back to ANY. This
    complements entity-graph search: it finds notes by what they actually SAY,
    even when the LLM extraction produced few/weak triples for them.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY last_edited DESC").fetchall()

    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        d = dict(r)
        hay = f"{d.get('title','')} {d.get('content','')}".lower()
        matched = sum(1 for t in terms if t in hay)
        if matched:
            scored.append((matched, d))

    # Prefer notes that contain ALL terms; otherwise rank by # of terms matched.
    all_terms = [s for s in scored if s[0] == len(terms)]
    pool = all_terms if all_terms else scored
    pool.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in pool[:limit]]


def get_note(id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (id,)).fetchone()
    return dict(row) if row else None


def mark_note_done(id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notes SET extraction_status = 'done' WHERE id = ?", (id,)
        )


def mark_note_error(id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notes SET extraction_status = 'error' WHERE id = ?", (id,)
        )


# ---------------------------------------------------------------------------
# Raw triples helpers
# ---------------------------------------------------------------------------

def delete_triples_for_note(note_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM raw_triples WHERE source_note_id = ?", (note_id,))


def insert_triples(triples: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
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


def get_all_triples() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM raw_triples ORDER BY extracted_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Canonical map helpers
# ---------------------------------------------------------------------------

def replace_canonical_map(clusters: list[dict[str, Any]]) -> None:
    """Atomically replace the full canonical map from a resolved cluster list."""
    with _connect() as conn:
        conn.execute("DELETE FROM canonical_map")
        conn.execute("DELETE FROM entity_clusters")
        for cluster in clusters:
            cluster_id = cluster["cluster_id"]
            canonical = cluster["canonical_name"]
            mentions = cluster["mentions"]
            conn.execute(
                "INSERT INTO entity_clusters (cluster_id, canonical_name, all_mentions) VALUES (?, ?, ?)",
                (cluster_id, canonical, json.dumps(mentions)),
            )
            for mention in mentions:
                conn.execute(
                    "INSERT OR REPLACE INTO canonical_map (mention_text, canonical_name, cluster_id) VALUES (?, ?, ?)",
                    (mention, canonical, cluster_id),
                )


def get_canonical_map() -> dict[str, str]:
    """Return {mention_text: canonical_name} dict."""
    with _connect() as conn:
        rows = conn.execute("SELECT mention_text, canonical_name FROM canonical_map").fetchall()
    return {r["mention_text"]: r["canonical_name"] for r in rows}


def get_entity_clusters() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM entity_clusters").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["mentions"] = json.loads(d["all_mentions"])
        del d["all_mentions"]
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Graph cache helpers
# ---------------------------------------------------------------------------

def cache_graph(graph_json: dict[str, Any], stats_json: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO graph_cache (id, built_at, graph_json, stats_json)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                built_at = excluded.built_at,
                graph_json = excluded.graph_json,
                stats_json = excluded.stats_json
            """,
            (now, json.dumps(graph_json), json.dumps(stats_json)),
        )


def get_cached_graph() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM graph_cache WHERE id = 1").fetchone()
    if not row:
        return None
    return {
        "built_at": row["built_at"],
        "graph": json.loads(row["graph_json"]),
        "stats": json.loads(row["stats_json"]),
    }


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def get_db_stats() -> dict[str, int]:
    with _connect() as conn:
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
