"""
A table that lives beside the notes, for data that is ABOUT the notes.

Several things in this system need a small table of their own in whichever
database holds the notes -- cached model replies (memo.py), who owns which
derived row (ownership.py), which relations the ontology could not hold
(coercions.py). None of them belong on the GraphStore contract: that would
oblige SQLite, Postgres AND Neo4j to implement each one, and Neo4j has no
business holding a cache of model replies.

So each carried its own copy of the same forty lines -- pick the backend, open
a connection, translate placeholders, run the schema once -- and by the third
copy they had begun to be maintained separately. This is that code, once.

WHAT IT DECIDES, and why it decides it the same way everywhere:

  * The backend follows NOTE_BACKEND, falling back to GRAPH_BACKEND, and never
    Neo4j. Data about notes goes where the notes are, or a backend switch
    strands it -- which is the failure CLAUDE.md records twice, as 61 notes in
    one store and 54 in another.

  * `load_env()` first, explicitly. The same omission in ingest/store.py made
    a store built with an explicit workspace silently choose SQLite in a
    deployment whose notes were in Postgres, because the only thing reading
    .env on that path was a call the explicit argument short-circuited past.

  * Bound to one workspace at construction. Isolation in this project fails
    OPEN, so callers are never given the chance to forget a filter.

NOT USED BY ingest/store.py, deliberately. That store holds transcripts, which
are SOURCE data and cannot be recomputed. Its copy works, it is tested, and
moving source-data code onto a shared base to save forty lines is the wrong
trade.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator


def note_backend() -> str:
    """Whichever database holds the notes. Never Neo4j."""
    from brahmastra.env import load_env

    load_env()
    name = (os.environ.get("NOTE_BACKEND") or "").strip().lower()
    if not name:
        name = (os.environ.get("GRAPH_BACKEND") or "sqlite").strip().lower()
    return "sqlite" if name in ("", "neo4j") else name


class SidecarStore:
    """
    One workspace's rows in one sidecar table. Subclasses set SCHEMA.

    Holds no open connection between calls, so an instance is cheap to keep and
    safe to reuse -- which matters, because the first version of memo.py built
    one per lookup and spent 15.8ms of a 35.6ms load re-running the schema.
    """

    # Statements separated by ';'. Run once per instance.
    SCHEMA: str = ""

    def __init__(self, workspace: str | None = None) -> None:
        from brahmastra.workspace import current_workspace

        self.workspace = workspace or current_workspace()
        self.backend = note_backend()
        self._ready = False

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
        """SQLite's `?` placeholders, translated for Postgres."""
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    @staticmethod
    def _dict(row: Any) -> dict[str, Any]:
        return row if isinstance(row, dict) else dict(row)

    def init_schema(self) -> None:
        if self._ready:
            return
        with self._cursor() as cur:
            for statement in filter(None, (s.strip() for s in self.SCHEMA.split(";"))):
                cur.execute(statement)
        self._ready = True
