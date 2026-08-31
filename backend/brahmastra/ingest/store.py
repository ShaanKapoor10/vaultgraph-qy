"""
Where transcripts, chunks and artifacts live.

Its own tables, in the same database that holds the notes -- because that
database is the system of record, and a knowledge base the whole organisation
queries must be networked rather than a file on one container's disk.

DELIBERATELY NOT ON THE GraphStore CONTRACT
-------------------------------------------
Adding transcript methods there would oblige SQLite, Postgres AND Neo4j to
implement them, and Neo4j has no business holding raw transcripts: it is the
engine for the derived graph. The contract stays about the concept graph, and
this module carries its own storage, which is what "isolated module" has to
mean if it is to mean anything.

WORKSPACE ISOLATION IS NOT OPTIONAL HERE EITHER
-----------------------------------------------
Property-based partitioning fails OPEN: a forgotten filter does not error, it
silently returns another workspace's data. That has already happened once in
this system -- a store built without its workspace overwrote a note in
`default` belonging to `office`. So the same discipline applies: every row
carries `workspace_id`, the store is BOUND to one workspace at construction,
and callers never pass a filter because they never get the chance to forget one.

AUTHORITY
---------
  SOURCE   transcripts        the text as submitted; cannot be recomputed
  DERIVED  transcript_chunks  a function of the transcript and the segmenter
  DERIVED  meeting_artifacts  a function of the chunks and a model

Re-ingesting rebuilds both derived tables. The day a human can edit an action
item, that edit becomes source data and needs its own table -- a rebuild would
destroy it. Noted in ingest/__init__.py as well, because that is the boundary
most likely to be crossed without noticing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from brahmastra.workspace import current_workspace

# Postgres and SQLite differ in placeholder style and in how "insert or
# replace" is spelled. Everything else here is ordinary SQL.
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id            TEXT NOT NULL,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'upload',
    source_ref    TEXT,
    occurred_at   TEXT,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','done','error')),
    error         TEXT,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE IF NOT EXISTS transcript_chunks (
    transcript_id TEXT NOT NULL,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    idx           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    speakers      TEXT NOT NULL DEFAULT '[]',
    start_time    TEXT,
    end_time      TEXT,
    start_char    INTEGER NOT NULL DEFAULT 0,
    end_char      INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    note_id       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    PRIMARY KEY (workspace_id, transcript_id, idx)
);

CREATE TABLE IF NOT EXISTS meeting_artifacts (
    id            TEXT NOT NULL,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    transcript_id TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,
    statement     TEXT NOT NULL,
    owner         TEXT,
    due           TEXT,
    rationale     TEXT,
    quote         TEXT,
    speakers      TEXT NOT NULL DEFAULT '[]',
    start_time    TEXT,
    end_time      TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_kind
    ON meeting_artifacts (workspace_id, kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_transcript
    ON meeting_artifacts (workspace_id, transcript_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner
    ON meeting_artifacts (workspace_id, owner);
"""

_POSTGRES_SCHEMA = _SQLITE_SCHEMA.replace("INTEGER", "INTEGER")


@dataclass
class Transcript:
    id: str
    title: str
    content: str
    source: str = "upload"
    source_ref: str | None = None
    occurred_at: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backend() -> str:
    """
    Which database holds the notes -- transcripts follow them.

    NOTE_BACKEND when the arrangement is split, otherwise GRAPH_BACKEND. Neo4j
    is never a candidate: it holds the derived graph, and a transcript is not
    that. A deployment running the graph on Neo4j with no separate note store
    falls back to SQLite here rather than refusing, which keeps single-store
    local development working exactly as it does everywhere else.
    """
    name = (os.environ.get("NOTE_BACKEND") or "").strip().lower()
    if not name:
        name = (os.environ.get("GRAPH_BACKEND") or "sqlite").strip().lower()
    return "sqlite" if name in ("", "neo4j") else name


class IngestStore:
    """Storage for one workspace. Bound at construction, never filtered by callers."""

    def __init__(self, workspace: str | None = None) -> None:
        self.workspace = workspace or current_workspace()
        self.backend = _backend()
        self._ready = False

    # -- connection --------------------------------------------------------

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            from brahmastra.stores.postgres_store import dsn

            conn = psycopg.connect(
                dsn(),
                autocommit=True,
                connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "10")),
            )
            conn.row_factory = dict_row
            try:
                with conn.cursor() as cur:
                    yield cur
            finally:
                conn.close()
        else:
            from brahmastra.stores.sqlite_store import db_path

            path = db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                with conn:
                    yield conn.cursor()
            finally:
                conn.close()

    def _ph(self, sql: str) -> str:
        """`?` for SQLite, `%s` for Postgres. The only dialect difference here."""
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    def _rows(self, cur) -> list[dict[str, Any]]:
        return [dict(r) for r in cur.fetchall()]

    # -- schema ------------------------------------------------------------

    def init_schema(self) -> None:
        """Idempotent. Called before every operation rather than at import."""
        if self._ready:
            return
        schema = _POSTGRES_SCHEMA if self.backend == "postgres" else _SQLITE_SCHEMA
        with self._cursor() as cur:
            for statement in filter(None, (s.strip() for s in schema.split(";"))):
                cur.execute(statement)
        self._ready = True

    def describe(self) -> str:
        return f"ingest:{self.backend}#{self.workspace}"

    # -- transcripts (SOURCE) ---------------------------------------------

    def create_transcript(self, t: Transcript) -> str:
        self.init_schema()
        tid = t.id or uuid.uuid4().hex[:12]
        with self._cursor() as cur:
            cur.execute(self._ph(
                """
                INSERT INTO transcripts
                    (id, workspace_id, title, content, source, source_ref,
                     occurred_at, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """),
                (tid, self.workspace, t.title, t.content, t.source,
                 t.source_ref, t.occurred_at, _now()),
            )
        return tid

    def get_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT * FROM transcripts WHERE workspace_id = ? AND id = ?"),
                (self.workspace, transcript_id))
            rows = self._rows(cur)
        return rows[0] if rows else None

    def list_transcripts(self, status: str | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
        self.init_schema()
        sql = ("SELECT id, title, source, source_ref, occurred_at, created_at, "
               "status, error, chunk_count FROM transcripts WHERE workspace_id = ?")
        params: list[Any] = [self.workspace]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            return self._rows(cur)

    def set_transcript_status(self, transcript_id: str, status: str,
                              error: str | None = None,
                              chunk_count: int | None = None) -> None:
        self.init_schema()
        sql = "UPDATE transcripts SET status = ?, error = ?"
        params: list[Any] = [status, error]
        if chunk_count is not None:
            sql += ", chunk_count = ?"
            params.append(chunk_count)
        sql += " WHERE workspace_id = ? AND id = ?"
        params.extend([self.workspace, transcript_id])
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))

    def delete_transcript(self, transcript_id: str) -> None:
        """Removes the transcript and everything derived from it."""
        self.init_schema()
        with self._cursor() as cur:
            for table in ("meeting_artifacts", "transcript_chunks", "transcripts"):
                column = "id" if table == "transcripts" else "transcript_id"
                cur.execute(self._ph(
                    f"DELETE FROM {table} WHERE workspace_id = ? AND {column} = ?"),
                    (self.workspace, transcript_id))

    # -- chunks and artifacts (DERIVED) -----------------------------------

    def clear_derived(self, transcript_id: str) -> None:
        """
        Drop what a previous ingestion produced.

        Called before re-ingesting so a second run REPLACES rather than
        duplicates -- the same reason extraction deletes a note's triples
        before re-inserting them.
        """
        self.init_schema()
        with self._cursor() as cur:
            for table in ("meeting_artifacts", "transcript_chunks"):
                cur.execute(self._ph(
                    f"DELETE FROM {table} WHERE workspace_id = ? AND transcript_id = ?"),
                    (self.workspace, transcript_id))

    def save_chunk(self, transcript_id: str, idx: int, text: str,
                   speakers: list[str], start_time: str | None,
                   end_time: str | None, start_char: int, end_char: int) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                """
                INSERT INTO transcript_chunks
                    (transcript_id, workspace_id, idx, text, speakers,
                     start_time, end_time, start_char, end_char)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (transcript_id, self.workspace, idx, text, json.dumps(speakers),
                 start_time, end_time, start_char, end_char),
            )

    def set_chunk_result(self, transcript_id: str, idx: int, status: str,
                         summary: str | None = None, note_id: str | None = None,
                         error: str | None = None) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                """
                UPDATE transcript_chunks
                   SET status = ?, summary = ?, note_id = ?, error = ?
                 WHERE workspace_id = ? AND transcript_id = ? AND idx = ?
                """),
                (status, summary, note_id, error, self.workspace, transcript_id, idx),
            )

    def get_chunks(self, transcript_id: str) -> list[dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT * FROM transcript_chunks WHERE workspace_id = ? "
                "AND transcript_id = ? ORDER BY idx"),
                (self.workspace, transcript_id))
            return self._rows(cur)

    def save_artifacts(self, transcript_id: str, artifacts: list[Any]) -> int:
        """Store verified artifacts. Returns how many landed."""
        self.init_schema()
        if not artifacts:
            return 0
        rows = [
            (uuid.uuid4().hex[:12], self.workspace, transcript_id, a.chunk_index,
             a.kind, a.statement, a.owner, a.due, a.rationale, a.quote,
             json.dumps(a.speakers), a.start_time, a.end_time, _now())
            for a in artifacts
        ]
        with self._cursor() as cur:
            cur.executemany(self._ph(
                """
                INSERT INTO meeting_artifacts
                    (id, workspace_id, transcript_id, chunk_index, kind, statement,
                     owner, due, rationale, quote, speakers, start_time, end_time,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """), rows)
        return len(rows)

    def get_artifacts(self, kind: str | None = None, owner: str | None = None,
                      transcript_id: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        """
        The query surface the organisation actually uses: "what did we decide?",
        "what are my action items?".
        """
        self.init_schema()
        sql = "SELECT * FROM meeting_artifacts WHERE workspace_id = ?"
        params: list[Any] = [self.workspace]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if owner:
            sql += " AND lower(owner) = lower(?)"
            params.append(owner)
        if transcript_id:
            sql += " AND transcript_id = ?"
            params.append(transcript_id)
        sql += " ORDER BY created_at DESC, chunk_index ASC LIMIT ?"
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            rows = self._rows(cur)
        for r in rows:
            if isinstance(r.get("speakers"), str):
                try:
                    r["speakers"] = json.loads(r["speakers"])
                except ValueError:
                    r["speakers"] = []
        return rows

    def counts(self) -> dict[str, int]:
        self.init_schema()
        out: dict[str, int] = {}
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT count(*) AS n FROM transcripts WHERE workspace_id = ?"),
                (self.workspace,))
            out["transcripts"] = self._rows(cur)[0]["n"]
            cur.execute(self._ph(
                "SELECT kind, count(*) AS n FROM meeting_artifacts "
                "WHERE workspace_id = ? GROUP BY kind"), (self.workspace,))
            for row in self._rows(cur):
                out[row["kind"]] = row["n"]
        return out


def get_ingest_store(workspace: str | None = None) -> IngestStore:
    """Build a store bound to a workspace. Resolved here, never by the backend."""
    return IngestStore(workspace=workspace)
