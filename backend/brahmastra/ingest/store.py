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

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

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
    -- How many times the document said this, and what replaced it if the
    -- meeting revisited the question. See ingest/consolidate.py.
    mentions      INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_kind
    ON meeting_artifacts (workspace_id, kind);
CREATE INDEX IF NOT EXISTS idx_artifacts_transcript
    ON meeting_artifacts (workspace_id, transcript_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_owner
    ON meeting_artifacts (workspace_id, owner);

-- A person's "this is wrong" about a finding. SOURCE data, like the transcript:
-- nothing can recompute a human judgement, so re-processing never touches it.
-- Keyed by the artifact's id, which is derived from its statement, so the same
-- finding found again is still rejected -- and a reworded one is asked again.
CREATE TABLE IF NOT EXISTS artifact_rejections (
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    artifact_id   TEXT NOT NULL,
    transcript_id TEXT NOT NULL,
    statement     TEXT NOT NULL,
    reason        TEXT,
    rejected_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, artifact_id)
);
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


# Bump to re-key every artifact at once. Only a change to what IDENTIFIES an
# artifact needs it -- not a change to how one is scored, stored or rendered.
ARTIFACT_ID_VERSION = "1"

_WHITESPACE = re.compile(r"\s+")


def _identity_text(statement: str) -> str:
    """
    The part of a statement that decides WHICH artifact it is.

    Case and whitespace are normalised and trailing punctuation dropped, so a
    re-run that returns "Ship on April 15th." where the last one returned
    "ship on April 15th" is recognised as the same record rather than as a new
    one. Nothing fuzzier than that: a genuinely reworded statement IS a
    different statement, and pretending otherwise would silently overwrite one
    record with another.
    """
    return _WHITESPACE.sub(" ", statement.strip().lower()).rstrip(".!?;:, ")


def artifact_id(transcript_id: str, kind: str, statement: str,
                occurrence: int = 0) -> str:
    """
    A stable id for one artifact: derived from what it IS, never drawn at random.

    This used to be `uuid.uuid4().hex[:12]`, assigned at write time. Because
    `clear_derived` wipes a transcript's artifacts before a re-run re-inserts
    them, every re-ingestion handed identical artifacts brand-new identities --
    so nothing outside this table could hold a reference to a decision across
    two runs, and "unchanged" was indistinguishable from "new". cocoindex names
    this directly: random ids make every reprocessing run churn its target,
    deleting rows and inserting identical ones under new keys.

    CHUNK INDEX IS DELIBERATELY NOT IN THE KEY. Chunks overlap and the
    segmenter is free to change; the same decision found at chunk 3 before and
    chunk 4 after a re-segmentation is the SAME decision, and re-chunking
    should not rewrite the identity of everything a transcript ever said. The
    chunk stays on the row as provenance, where it belongs.

    `occurrence` separates exact repeats. Consolidation normally folds those
    together, but it can be switched off (INGEST_CONSOLIDATE=0), and a derived
    key that collides would turn that switch into a primary-key violation
    rather than a duplicate row. Stable given the same ordered artifacts, which
    is what a re-run produces.
    """
    h = hashlib.sha256()
    for part in (ARTIFACT_ID_VERSION, transcript_id, kind,
                 _identity_text(statement), str(occurrence)):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")          # so ("ab","c") and ("a","bc") differ
    # 64 bits. Collisions are the one failure this cannot report, because a
    # colliding INSERT looks exactly like a duplicate artifact.
    return h.hexdigest()[:16]


def identify_artifacts(transcript_id: str,
                       artifacts: Sequence[Any]) -> list[tuple[str, Any]]:
    """
    Pair each artifact with its derived id, and stamp the id onto it.

    Lifted out of `save_artifacts` because ownership has to DECLARE these rows
    before deciding which of them to write, and a declaration keyed on
    something other than the primary key would reconcile against the wrong
    thing. One implementation, called from both places.

    `occurrence` separates exact repeats within one transcript, so a document
    that genuinely says the same thing twice yields two rows rather than a
    primary-key collision. See `artifact_id` for why the chunk index is not in
    the key.
    """
    seen: dict[tuple[str, str], int] = {}
    out: list[tuple[str, Any]] = []
    for a in artifacts:
        slot = (a.kind, _identity_text(a.statement))
        occurrence = seen.get(slot, 0)
        seen[slot] = occurrence + 1
        aid = artifact_id(transcript_id, a.kind, a.statement, occurrence)
        # Stamped on the object too: an id nothing can observe is a primary
        # key, not an identity, and the point of deriving it is that something
        # downstream can hold on to it.
        try:
            a.id = aid
        except Exception:
            pass
        out.append((aid, a))
    return out


def _backend() -> str:
    """
    Which database holds the notes -- transcripts follow them.

    NOTE_BACKEND when the arrangement is split, otherwise GRAPH_BACKEND. Neo4j
    is never a candidate: it holds the derived graph, and a transcript is not
    that. A deployment running the graph on Neo4j with no separate note store
    falls back to SQLite here rather than refusing, which keeps single-store
    local development working exactly as it does everywhere else.
    """
    # Load the config before reading it, which sounds obvious and was not
    # happening. These variables came from backend/.env, and nothing on this
    # path read that file: it was loaded as a SIDE EFFECT of
    # `current_workspace()`, which `IngestStore.__init__` only calls when no
    # workspace was passed. So `get_ingest_store("office")` short-circuited the
    # `or`, never loaded .env, saw an empty NOTE_BACKEND and silently chose
    # SQLITE -- in a deployment whose notes live in Postgres.
    #
    # Observed in one process, which is what makes it unarguable:
    #     get_ingest_store("transcripts-demo") -> backend=sqlite
    #     get_ingest_store(None)               -> backend=postgres
    #
    # Same code, same environment, different store, decided by whether an
    # argument was passed. Transcripts are SOURCE data, so this is the failure
    # CLAUDE.md records twice already -- a backend switch that left 61 notes in
    # one store and 54 in another. The API never hit it because uvicorn loads
    # .env at import; a fresh CLI process with --workspace hits it every time.
    from brahmastra.env import load_env
    load_env()

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
        Drop every chunk and artifact this transcript produced.

        NO LONGER WHAT A RE-RUN DOES, and that is the point. Deleting
        everything and rewriting it is not a reconciliation: it is a hole for
        as long as the run takes, it destroys rows that did not change, and an
        interrupted run leaves the transcript with nothing at all. Re-ingestion
        now declares what it produces to `brahmastra.ownership`, which writes
        what differs and deletes what is no longer declared.

        Kept because "remove the lot" is still a real operation -- a wipe
        before a deliberate rebuild, and what `delete_transcript` does on its
        way out.
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
        # IDEMPOTENT, because ownership requires it: an interrupted run is
        # redone rather than undone, so this must be safe to apply twice. A
        # plain INSERT raised on the primary key the second time, which is why
        # the old path had to delete everything first.
        with self._cursor() as cur:
            cur.execute(self._ph(
                """
                INSERT INTO transcript_chunks
                    (transcript_id, workspace_id, idx, text, speakers,
                     start_time, end_time, start_char, end_char)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace_id, transcript_id, idx) DO UPDATE SET
                    text = excluded.text,
                    speakers = excluded.speakers,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    start_char = excluded.start_char,
                    end_char = excluded.end_char,
                    status = 'pending',
                    summary = NULL,
                    note_id = NULL,
                    error = NULL
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
        rows = []
        for aid, a in identify_artifacts(transcript_id, artifacts):
            rows.append(
                (aid, self.workspace, transcript_id, a.chunk_index,
                 a.kind, a.statement, a.owner, a.due, a.rationale, a.quote,
                 json.dumps(a.speakers), a.start_time, a.end_time,
                 getattr(a, "mentions", 1), getattr(a, "superseded_by", None),
                 _now())
            )
        # IDEMPOTENT, because ownership requires it. `created_at` is
        # deliberately NOT overwritten: it records when this system first knew
        # about the artifact, and a re-run that changes nothing else must not
        # make every decision in the corpus look newly taken.
        with self._cursor() as cur:
            cur.executemany(self._ph(
                """
                INSERT INTO meeting_artifacts
                    (id, workspace_id, transcript_id, chunk_index, kind, statement,
                     owner, due, rationale, quote, speakers, start_time, end_time,
                     mentions, superseded_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (workspace_id, id) DO UPDATE SET
                    transcript_id = excluded.transcript_id,
                    chunk_index = excluded.chunk_index,
                    kind = excluded.kind,
                    statement = excluded.statement,
                    owner = excluded.owner,
                    due = excluded.due,
                    rationale = excluded.rationale,
                    quote = excluded.quote,
                    speakers = excluded.speakers,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    mentions = excluded.mentions,
                    superseded_by = excluded.superseded_by
                """), rows)
        return len(rows)

    def delete_artifacts(self, ids: Sequence[str]) -> int:
        """Remove artifacts by id. Idempotent: an absent id is not an error."""
        if not ids:
            return 0
        self.init_schema()
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "DELETE FROM meeting_artifacts WHERE workspace_id = ? AND id = ?"),
                [(self.workspace, aid) for aid in ids])
        return len(ids)

    def delete_chunks(self, transcript_id: str, indexes: Sequence[int]) -> int:
        """Remove chunks by index. Idempotent: an absent index is not an error."""
        if not indexes:
            return 0
        self.init_schema()
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "DELETE FROM transcript_chunks WHERE workspace_id = ? "
                "AND transcript_id = ? AND idx = ?"),
                [(self.workspace, transcript_id, int(i)) for i in indexes])
        return len(indexes)

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

    # -- rejections (a person's verdict; SOURCE data) ------------------------

    def reject_artifact(self, artifact_id: str, reason: str | None = None) -> dict[str, Any] | None:
        """Mark a finding wrong. Returns the artifact, or None if unknown."""
        self.init_schema()
        row = next((a for a in self.get_artifacts(limit=1_000_000) if a["id"] == artifact_id), None)
        if row is None:
            return None
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO artifact_rejections (workspace_id, artifact_id, transcript_id, "
                "statement, reason, rejected_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, artifact_id) DO UPDATE SET "
                "reason = excluded.reason, rejected_at = excluded.rejected_at"),
                (self.workspace, artifact_id, row["transcript_id"], row["statement"],
                 reason, _now()))
        return row

    def unreject_artifact(self, artifact_id: str) -> bool:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "DELETE FROM artifact_rejections WHERE workspace_id = ? AND artifact_id = ?"),
                (self.workspace, artifact_id))
            return bool(cur.rowcount)

    def rejected_ids(self, transcript_id: str | None = None) -> set[str]:
        self.init_schema()
        sql = "SELECT artifact_id FROM artifact_rejections WHERE workspace_id = ?"
        params: list[Any] = [self.workspace]
        if transcript_id:
            sql += " AND transcript_id = ?"
            params.append(transcript_id)
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            return {r["artifact_id"] for r in self._rows(cur)}

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
