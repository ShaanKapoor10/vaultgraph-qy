"""
Never pay a model twice for the same question.

A memoised call is one whose answer is a pure function of its inputs: the same
text, read by the same model under the same prompt, yields the same reply. Two
places in this system have that shape and both were paying full price to learn
what they already knew -- comprehension of a transcript chunk, and extraction
of triples from a note.

Extraction is the expensive one. `run_pipeline(full=True)` re-extracts every
note, and CLAUDE.md records what that costs: "a full=True re-extraction of ~44
notes typically errors on a third of them" against Groq's free tier. Most of
those notes had not changed. With a memo, a full re-extraction after an
unrelated edit costs nothing for every note that is the same as it was -- and
an ONTOLOGY change correctly re-runs all of them, because the prompt carries
the ontology and the prompt is in the key.

Borrowed from cocoindex, whose memoisation keys on `hash(input) + hash(code)`.
The second half is the half worth copying deliberately: the cache must be
invalidated by a change to the PROMPT, not only by a change to the text.

WHAT IS IN THE KEY

  the input text      the note, or the passage
  the variant         which job this is -- "extract", "chat", ...
  the model           a 7B and a 120B do not answer alike
  a digest of the prompts        what `hash(code)` means here

WHAT THIS IS NOT
----------------
Not a store of results. It caches the MODEL'S REPLY, before anything is
validated, parsed, coerced or grounded. Those steps are cheap, deterministic,
and the part most likely to be improved -- caching their output would freeze a
defence in place and mean a fixed bug stayed fixed only for new documents.

Not on the GraphStore contract, for the same reason ingest/store.py is not:
adding it would oblige SQLite, Postgres AND Neo4j to implement it, and Neo4j
has no business holding cached replies. It carries its own table in whichever
database holds the notes.

DERIVED, and disposable. Every row is recomputable by paying the model again,
so it is never migrated and never backed up.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

# One place to bump when a change should invalidate every cached reply at once.
# A change to how a reply is PARSED needs it; a change to how the parsed result
# is validated does not, because validation re-runs on every hit.
CACHE_VERSION = "1"

TABLE = "llm_memo"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_memo (
    cache_key     TEXT NOT NULL,
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    variant       TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, cache_key)
);
"""

# LRU capacity for the in-process layer. Sized for one long document rather
# than for a corpus: this exists so a run does not ask the database twice for a
# reply it is already holding, not to be a second cache.
LOCAL_MAX = 256


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(bytes([0]))        # so ("ab","c") and ("a","bc") differ
    return h.hexdigest()


def key_for(text: str, variant: str, model: str, prompts: str) -> str:
    """
    The cache key: the input, and the code that reads it.

    `prompts` carries the actual prompt text rather than a version number
    somebody has to remember to bump. A prompt edited in place would otherwise
    keep serving replies from the prompt it replaced -- the failure mode that
    makes a cache worse than no cache, because the improvement becomes
    invisible rather than merely absent.
    """
    return _digest(CACHE_VERSION, variant, model, prompts, text)


def enabled() -> bool:
    """On unless switched off. LLM_MEMO=0 forces every call to be paid again."""
    return os.environ.get("LLM_MEMO", "").strip() != "0"


# -- storage ----------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backend() -> str:
    """
    Whichever database holds the notes. Cached replies follow the corpus.

    load_env() first, and not as a side effect of something else: the same
    omission in ingest/store.py made `get_ingest_store("office")` silently
    choose SQLite in a deployment whose notes live in Postgres, because the
    only thing reading .env on that path was a call it short-circuited past.
    """
    from brahmastra.env import load_env

    load_env()
    name = (os.environ.get("NOTE_BACKEND") or "").strip().lower()
    if not name:
        name = (os.environ.get("GRAPH_BACKEND") or "sqlite").strip().lower()
    return "sqlite" if name in ("", "neo4j") else name


class MemoStore:
    """One workspace's cached replies. Bound at construction, never filtered."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.backend = _backend()
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
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    def init_schema(self) -> None:
        if self._ready:
            return
        with self._cursor() as cur:
            for statement in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                cur.execute(statement)
        self._ready = True

    def get(self, cache_key: str) -> str | None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(
                self._ph("SELECT payload FROM llm_memo "
                         "WHERE workspace_id = ? AND cache_key = ?"),
                (self.workspace, cache_key))
            row = cur.fetchone()
        if not row:
            return None
        return row["payload"] if isinstance(row, dict) else row[0]

    def put(self, cache_key: str, variant: str, payload: str) -> None:
        self.init_schema()
        with self._cursor() as cur:
            if self.backend == "postgres":
                cur.execute(self._ph(
                    "INSERT INTO llm_memo "
                    "(cache_key, workspace_id, variant, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (workspace_id, cache_key) DO UPDATE "
                    "SET payload = EXCLUDED.payload"),
                    (cache_key, self.workspace, variant, payload, _now()))
            else:
                cur.execute(self._ph(
                    "INSERT OR REPLACE INTO llm_memo "
                    "(cache_key, workspace_id, variant, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)"),
                    (cache_key, self.workspace, variant, payload, _now()))

    def count(self, variant: str | None = None) -> int:
        self.init_schema()
        sql = "SELECT count(*) AS n FROM llm_memo WHERE workspace_id = ?"
        params: list[Any] = [self.workspace]
        if variant is not None:
            sql += " AND variant = ?"
            params.append(variant)
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            row = cur.fetchone()
        return int(row["n"] if isinstance(row, dict) else row[0])


# -- reaching the cache -----------------------------------------------------
#
# A cache whose LOOKUP is expensive is a smaller version of the problem it was
# built to solve. The first version of this, in ingest/memo.py, built a store
# per call -- so every lookup constructed one with `_ready = False` and re-ran
# the whole schema DDL before the SELECT it wanted. Two connections per load,
# two per save. Measured against the deployed Postgres: 15.8ms of a 35.6ms
# load, which is 5.8s of pure bookkeeping on a 40-chunk transcript, scaling
# with exactly the document length memoisation exists to make cheap.

_stores: dict[str, MemoStore] = {}
_local: "OrderedDict[tuple[str, str], str]" = OrderedDict()


def _workspace() -> str:
    from brahmastra.workspace import current_workspace

    return current_workspace()


def _store() -> MemoStore:
    """The store for this workspace, reused. It holds no open connection."""
    workspace = _workspace()
    store = _stores.get(workspace)
    if store is None:
        store = _stores[workspace] = MemoStore(workspace)
    return store


def _remember(key: str, reply: str) -> None:
    slot = (_workspace(), key)
    _local[slot] = reply
    _local.move_to_end(slot)
    while len(_local) > LOCAL_MAX:
        _local.popitem(last=False)


def reset() -> None:
    """Drop both layers. For tests, and for a process that changes backend."""
    _stores.clear()
    _local.clear()


def load(key: str) -> str | None:
    """
    A previously cached RAW reply, or None. Never raises.

    Not a parsed payload and not a validated result: parsing, coercion and
    every grounding check re-run on a hit, so a fixed bug in them applies to
    cached inputs too.

    Answered from memory first. A key is a digest of the text, the prompt and
    the model, so two replies under one key differ only by the model's own
    nondeterminism -- an in-process copy can be older than the row, never wrong
    about which input it answers.
    """
    if not enabled():
        return None
    slot = (_workspace(), key)
    hit = _local.get(slot)
    if hit is not None:
        _local.move_to_end(slot)
        return hit
    try:
        reply = _store().get(key)
    except Exception:
        return None
    if reply:
        _remember(key, reply)
    return reply


def save(key: str, reply: str, variant: str = "") -> None:
    """
    Cache a raw reply. Never raises: failing to cache is not failing.

    An empty reply is never cached. That is what a reasoning model returns
    when its token budget ran out before the content began, and storing it
    would make one bad run permanent for that input.
    """
    if not enabled() or not reply:
        return
    _remember(key, reply)
    try:
        _store().put(key, variant, reply)
    except Exception:
        pass


def count(variant: str | None = None) -> int:
    """How many replies are cached. Reporting only; returns 0 if unreachable."""
    try:
        return _store().count(variant)
    except Exception:
        return 0
