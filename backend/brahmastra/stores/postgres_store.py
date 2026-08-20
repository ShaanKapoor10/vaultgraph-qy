"""
PostgresStore -- the system of record, with hybrid search intact.

Why this exists
---------------
Notes and workspaces cannot be recomputed; triples and the cached graph can
(see SOURCE_DATA / DERIVED_DATA in base.py). Keeping both in whichever backend
GRAPH_BACKEND happened to name meant an engine decision put irreplaceable data
at risk. This store holds the half that matters, so the engine underneath can
be chosen, replaced or rebuilt freely.

Why not SQLite for that half
----------------------------
Two reasons, one per requirement:

* It is networked, so several machines share one system of record. A file is
  not a multi-device story.
* It can do hybrid search. Neo4j finds notes by fusing BM25 relevance with
  embedding similarity, and routing note search to a lexical-only store is an
  invisible downgrade: every query still succeeds and quietly returns worse
  results. Postgres has `tsvector` for the lexical half and `pgvector` for the
  semantic half, so the feature moves rather than dies -- which is why this
  class declares CAP_HYBRID_SEARCH and CompositeStore accepts it.

The fusion here is deliberately the SAME arithmetic as the Neo4j backend --
Reciprocal Rank Fusion, K=60, both engines over-fetched to `limit * 5` -- so
switching where notes live changes the storage, not the ranking.

pgvector is required
--------------------
A stock PostgreSQL install does not have it (the Windows build offers only
pg_trgm), so `docker compose up postgres` uses the pgvector image. Without the
extension this store still runs and still stores notes, but reports itself as
lexical-only, and CompositeStore then refuses to take it as a note store rather
than degrading search behind your back.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from brahmastra.stores.base import (
    CAP_FULLTEXT_SEARCH,
    CAP_HYBRID_SEARCH,
    CAP_LEXICAL_SEARCH,
    CAP_VECTOR_SEARCH,
    GraphStore,
)

DEFAULT_WORKSPACE = "default"

# Reciprocal Rank Fusion constant. 60 is the value from the original RRF paper
# and the same one the Neo4j backend uses; the two must agree or moving notes
# between backends would silently reorder every result list.
RRF_K = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dsn() -> str:
    """
    Connection string, resolved at call time.

    Read per call rather than at import so tests can point at a throwaway
    database. Binding it at import is the same mistake that once had the test
    suite running against the production SQLite file.
    """
    explicit = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "brahmastra")
    password = os.environ.get("POSTGRES_PASSWORD", "brahmastra")
    database = os.environ.get("POSTGRES_DB", "brahmastra")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    ontology     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notes (
    workspace_id      TEXT NOT NULL,
    id                TEXT NOT NULL,
    title             TEXT NOT NULL,
    content           TEXT NOT NULL,
    last_edited       TEXT,
    last_synced       TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN ('pending','done','error')),
    extraction_error  TEXT,
    publish           BOOLEAN NOT NULL DEFAULT FALSE,
    notion_page_id    TEXT,
    source            TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (workspace_id, id)
);

CREATE INDEX IF NOT EXISTS idx_notes_status
    ON notes (workspace_id, extraction_status);

-- The lexical half of hybrid search. Generated rather than maintained by
-- trigger: a trigger can be missed by a bulk load, and a stale search index is
-- the kind of failure that returns plausible results instead of an error.
-- Title is weighted above body so a note named for the query outranks one that
-- merely mentions it.
ALTER TABLE notes ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_notes_search
    ON notes USING GIN (search_vector);
"""


class PostgresStore(GraphStore):
    """
    Notes and workspaces in PostgreSQL. Pair it with a graph engine.

    Deliberately covers only the system of record. The derived half -- triples,
    canonical map, clusters, the cached graph -- belongs to whatever engine is
    selected, and answering those calls here would create a second copy of data
    that is supposed to have exactly one home.
    """

    def __init__(self, workspace: str | None = None, dsn_override: str | None = None):
        self.workspace = workspace or os.environ.get(
            "BRAHMASTRA_WORKSPACE", DEFAULT_WORKSPACE
        )
        self._dsn = dsn_override or dsn()
        self._conn = None
        self._has_vector: bool | None = None
        self._extensions_ready = False

    # -- connection --------------------------------------------------------

    def _connect(self):
        """
        One long-lived connection, reopened if it has died.

        Autocommit: the call sites are single statements and the pipeline is
        already serialised by its own lock, so an implicit open transaction
        would only hold locks across unrelated work.
        """
        import psycopg

        if self._conn is None or self._conn.closed:
            # connect_timeout is not optional. A networked system of record
            # introduces a failure mode a file cannot have: an unreachable
            # server that never refuses, just never answers. Without this the
            # connect blocks indefinitely -- measured at over 300 seconds
            # against a filtered port, with no output and no error.
            #
            # That matters most where the caller has a deadline it cannot
            # extend: the Stop hook runs on every assistant turn with a 15s
            # budget, and a hung store would stall the session it exists to
            # protect. Failing fast turns an invisible hang into an error
            # somebody can act on.
            self._conn = psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "10")),
            )
            self._conn.row_factory = __import__(
                "psycopg.rows", fromlist=["dict_row"]
            ).dict_row
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def describe(self) -> str:
        # Never render the password: describe() lands in logs, health payloads
        # and the composite's own describe().
        safe = self._dsn
        if "@" in safe and "//" in safe:
            scheme, rest = safe.split("//", 1)
            creds, host = rest.split("@", 1)
            user = creds.split(":", 1)[0]
            safe = f"{scheme}//{user}:***@{host}"
        return f"postgres:{safe}#{self.workspace}"

    # -- capabilities ------------------------------------------------------

    def _ensure_extensions(self) -> None:
        """
        Install the extensions this store needs. Idempotent, once per process.

        Called from has_vector() rather than only from init_schema() because of
        the ORDER things happen in: the store factory builds CompositeStore,
        which asks about capabilities, and only afterwards does anything call
        init_schema(). Against a fresh database that meant the capability check
        ran before `CREATE EXTENSION vector` ever had, so pgvector looked
        absent and the composite refused to build -- `docker compose up` failed
        on first boot every time, with an error blaming the image for lacking
        an extension it actually had.

        Failure is not fatal: a restricted user simply cannot install it, and
        capabilities() then reports lexical-only, which is true.
        """
        if self._extensions_ready:
            return
        self._extensions_ready = True
        for ext in ("vector", "pg_trgm"):
            try:
                with self._connect().cursor() as cur:
                    cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            except Exception:
                continue

    def has_vector(self) -> bool:
        """
        Whether pgvector is installed. Cached: it cannot change mid-process.

        Checked rather than assumed because a stock PostgreSQL build does not
        ship it, and claiming vector search without it would mean answering
        semantic queries with nothing and calling the result hybrid.
        """
        if self._has_vector is None:
            try:
                self._ensure_extensions()
                with self._connect().cursor() as cur:
                    cur.execute("SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'")
                    self._has_vector = cur.fetchone() is not None
            except Exception:
                # A failure to ASK is not an answer. Caching False here made a
                # transient outage permanent: the store would report
                # lexical-only for the rest of the process even once the server
                # came back, so CompositeStore would refuse it -- or, if the
                # downgrade were accepted, semantic search would stay switched
                # off with nothing indicating why. Only a definitive reply is
                # cached; an unreachable server is re-asked next time.
                return False
        return self._has_vector

    def capabilities(self) -> frozenset[str]:
        caps = {CAP_LEXICAL_SEARCH, CAP_FULLTEXT_SEARCH}
        if self.has_vector():
            caps |= {CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH}
        return frozenset(caps)

    # -- lifecycle ---------------------------------------------------------

    def init_schema(self) -> None:
        # Extensions first: the vector column below depends on one. Shared with
        # has_vector() so a capability check on a fresh database installs them
        # too -- the factory asks about capabilities before anything calls this.
        self._ensure_extensions()
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(SCHEMA)

            if self.has_vector():
                from brahmastra.embeddings import DIM

                cur.execute(
                    f"ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding vector({DIM})"
                )
                # HNSW over cosine distance, matching the Neo4j index's
                # similarity function so the two rank alike.
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notes_embedding "
                    "ON notes USING hnsw (embedding vector_cosine_ops)"
                )

            cur.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (self.workspace, self.workspace),
            )

    # -- workspaces --------------------------------------------------------

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._connect().cursor() as cur:
            cur.execute(
                "SELECT id, name, description, ontology, created_at FROM workspaces "
                "ORDER BY created_at"
            )
            return [self._workspace_row(r) for r in cur.fetchall()]

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self._connect().cursor() as cur:
            cur.execute(
                "SELECT id, name, description, ontology, created_at FROM workspaces "
                "WHERE id = %s",
                (workspace_id,),
            )
            row = cur.fetchone()
        return self._workspace_row(row) if row else None

    def create_workspace(self, ws: Any) -> dict[str, Any]:
        ontology = getattr(ws, "ontology", None)
        with self._connect().cursor() as cur:
            cur.execute(
                "INSERT INTO workspaces (id, name, description, ontology) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    ws.id,
                    getattr(ws, "name", ws.id),
                    getattr(ws, "description", None),
                    json.dumps(ontology) if isinstance(ontology, (dict, list)) else ontology,
                ),
            )
        return self.get_workspace(ws.id) or {"id": ws.id, "name": getattr(ws, "name", ws.id)}

    def delete_workspace(self, workspace_id: str) -> None:
        with self._connect().cursor() as cur:
            cur.execute("DELETE FROM notes WHERE workspace_id = %s", (workspace_id,))
            cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))

    @staticmethod
    def _workspace_row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        created = out.get("created_at")
        if isinstance(created, datetime):
            out["created_at"] = created.isoformat()
        return out

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
        with self._connect().cursor() as cur:
            cur.execute(
                """
                INSERT INTO notes (id, workspace_id, title, content, last_edited,
                                   last_synced, extraction_status, publish, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    last_edited = EXCLUDED.last_edited,
                    last_synced = EXCLUDED.last_synced,
                    -- NULL means "leave as is", so a sync cannot silently
                    -- unpublish a note somebody chose to publish.
                    publish = COALESCE(%s, notes.publish),
                    -- Keep a known origin, but allow 'unknown' to be upgraded.
                    -- COALESCE alone prefers the NEW value, so a Notion sync
                    -- re-upserting an MCP note would relabel it and provenance
                    -- would decay to whichever job ran last.
                    source = CASE
                        WHEN notes.source IS NULL OR notes.source = 'unknown'
                            THEN COALESCE(%s, 'unknown')
                        ELSE notes.source
                    END,
                    extraction_status = CASE
                        WHEN EXCLUDED.extraction_status = 'pending' THEN 'pending'
                        WHEN EXCLUDED.last_edited IS DISTINCT FROM notes.last_edited
                            THEN 'pending'
                        ELSE notes.extraction_status
                    END
                """,
                (id, self.workspace, title, content, last_edited, _now(), status,
                 bool(publish), source or "unknown",
                 None if publish is None else bool(publish),
                 source),
            )
        self._embed_note(id, title, content)

    def _embed_note(self, note_id: str, title: str, content: str) -> None:
        """
        Store the note's embedding for semantic search.

        Fails soft: without sentence-transformers or pgvector the note is still
        saved and searchable lexically, just not semantically. Embedding on
        write keeps the index current without a separate backfill pass.
        """
        if not self.has_vector():
            return
        from brahmastra.embeddings import embed_one

        vec = embed_one(f"{title}\n\n{content}")
        if vec is None:
            return
        try:
            with self._connect().cursor() as cur:
                cur.execute(
                    "UPDATE notes SET embedding = %s::vector "
                    "WHERE workspace_id = %s AND id = %s",
                    (str(list(vec)), self.workspace, note_id),
                )
        except Exception:
            pass

    def get_note(self, id: str) -> dict[str, Any] | None:
        with self._connect().cursor() as cur:
            cur.execute(
                f"SELECT {self._NOTE_COLS} FROM notes WHERE workspace_id = %s AND id = %s",
                (self.workspace, id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_notes_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """One round trip instead of one per id -- the point of overriding this."""
        if not ids:
            return {}
        with self._connect().cursor() as cur:
            cur.execute(
                f"SELECT {self._NOTE_COLS} FROM notes "
                "WHERE workspace_id = %s AND id = ANY(%s)",
                (self.workspace, list(dict.fromkeys(ids))),
            )
            return {r["id"]: dict(r) for r in cur.fetchall()}

    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect().cursor() as cur:
            if status:
                cur.execute(
                    f"SELECT {self._NOTE_COLS} FROM notes "
                    "WHERE workspace_id = %s AND extraction_status = %s "
                    "ORDER BY last_edited DESC NULLS LAST",
                    (self.workspace, status),
                )
            else:
                cur.execute(
                    f"SELECT {self._NOTE_COLS} FROM notes WHERE workspace_id = %s "
                    "ORDER BY last_edited DESC NULLS LAST",
                    (self.workspace,),
                )
            return [dict(r) for r in cur.fetchall()]

    # Explicit column list: SELECT * would drag the tsvector and the 384-float
    # embedding into every note dict, where nothing wants them and JSON
    # serialisation of the vector fails.
    _NOTE_COLS = (
        "workspace_id, id, title, content, last_edited, last_synced, "
        "extraction_status, extraction_error, publish, notion_page_id, source"
    )

    def set_note_status(self, id: str, status: str, error: str | None = None) -> None:
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid extraction status: {status!r}")
        with self._connect().cursor() as cur:
            cur.execute(
                "UPDATE notes SET extraction_status = %s, extraction_error = %s "
                "WHERE workspace_id = %s AND id = %s",
                (status, error if status == "error" else None, self.workspace, id),
            )

    def set_notion_page_id(self, note_id: str, page_id: str) -> None:
        with self._connect().cursor() as cur:
            cur.execute(
                "UPDATE notes SET notion_page_id = %s WHERE workspace_id = %s AND id = %s",
                (page_id, self.workspace, note_id),
            )

    def delete_note(self, id: str) -> None:
        with self._connect().cursor() as cur:
            cur.execute(
                "DELETE FROM notes WHERE workspace_id = %s AND id = %s",
                (self.workspace, id),
            )

    # -- hybrid search -----------------------------------------------------

    def _fulltext_notes(self, query: str, limit: int) -> list[str]:
        """
        Note ids by lexical relevance. Returns [] if the query has no usable terms.

        websearch_to_tsquery is used rather than to_tsquery because the input is
        a natural-language question: a bare `and`, a stray quote or a hyphen is
        a syntax error to the latter, and an exception here would take out the
        whole search rather than one half of it.
        """
        if not (query or "").strip():
            return []
        try:
            with self._connect().cursor() as cur:
                cur.execute(
                    """
                    SELECT id, ts_rank_cd(search_vector, q) AS score
                    FROM notes, websearch_to_tsquery('english', %s) q
                    WHERE workspace_id = %s AND search_vector @@ q
                    ORDER BY score DESC
                    LIMIT %s
                    """,
                    (query, self.workspace, limit),
                )
                return [r["id"] for r in cur.fetchall()]
        except Exception:
            return []

    def _vector_notes(self, query: str, limit: int) -> list[str]:
        """Note ids by embedding similarity. [] when embeddings are unavailable."""
        if not self.has_vector():
            return []
        from brahmastra.embeddings import embed_one

        vec = embed_one(query)
        if vec is None:
            return []
        try:
            with self._connect().cursor() as cur:
                # <=> is cosine distance, so ascending order is most-similar
                # first. Rows without an embedding are excluded rather than
                # sorted as maximally distant.
                cur.execute(
                    """
                    SELECT id FROM notes
                    WHERE workspace_id = %s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (self.workspace, str(list(vec)), limit),
                )
                return [r["id"] for r in cur.fetchall()]
        except Exception:
            return []

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Hybrid search: lexical relevance fused with embedding similarity.

        Deliberately identical in shape and arithmetic to the Neo4j backend --
        same over-fetch, same RRF with K=60 -- so moving notes between the two
        changes where they are stored, not what a search returns.

        Lexical search finds exact terms and names; vector search finds notes
        that mean the same thing in different words. Reciprocal Rank Fusion
        combines the rankings without normalising scores, which matters because
        ts_rank_cd and cosine distance are not on comparable scales.

        Degrades cleanly: without pgvector or sentence-transformers this is
        pure full-text, and if that finds nothing it falls back to a substring
        scan so a query never returns empty merely because the indexes are cold.
        """
        if not (query or "").strip():
            return []

        pool = max(limit * 5, 20)  # over-fetch so fusion has room to reorder
        lexical = self._fulltext_notes(query, pool)
        semantic = self._vector_notes(query, pool)

        if not lexical and not semantic:
            return self._search_notes_substring(query, limit)

        scores: dict[str, float] = {}
        for ranking in (lexical, semantic):
            for rank, note_id in enumerate(ranking):
                scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        ordered = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]
        if not ordered:
            return []
        by_id = self.get_notes_by_ids(ordered)
        # Preserve fusion order; the SQL ANY() does not guarantee it.
        return [by_id[i] for i in ordered if i in by_id]

    def _search_notes_substring(self, query: str, limit: int) -> list[dict[str, Any]]:
        """
        Last-resort scan, matching the other backends' semantics exactly.

        Those semantics are: match ANY term, score by how many matched, prefer
        notes containing all of them but FALL BACK to partial matches when none
        does. Joining the terms with AND instead -- the obvious way to write
        this -- silently changes the contract: a two-word query where no single
        note holds both words returns nothing here while SQLite and Neo4j both
        return the partial hits. A fallback that returns less than the thing it
        is a fallback for is worse than no fallback.
        """
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []

        hay = "lower(coalesce(title,'') || ' ' || coalesce(content,''))"
        score = " + ".join([f"(CASE WHEN {hay} LIKE %s THEN 1 ELSE 0 END)"] * len(terms))

        # Scored in a subquery so the expression -- and its parameters -- appear
        # exactly once. Repeating it in SELECT and WHERE means two interleaved
        # copies of the term list, which is correct only until someone edits one
        # of them.
        with self._connect().cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT {self._NOTE_COLS}, ({score}) AS matched
                    FROM notes WHERE workspace_id = %s
                ) scored
                WHERE matched > 0
                ORDER BY matched DESC, last_edited DESC NULLS LAST
                """,
                [f"%{t}%" for t in terms] + [self.workspace],
            )
            rows = [dict(r) for r in cur.fetchall()]

        full = [r for r in rows if r["matched"] == len(terms)]
        pool = full if full else rows
        for row in pool:
            row.pop("matched", None)
        return pool[:limit]

    def search_notes_across(
        self, query: str, workspaces: list[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Cross-workspace search. Explicit by design -- results carry their origin.

        One store serving every workspace is exactly why this is safe here: no
        second connection, and the workspace filter is a WHERE clause rather
        than a different database.
        """
        targets = workspaces or [w["id"] for w in self.list_workspaces()]
        out: list[dict[str, Any]] = []
        for ws in targets:
            scoped = PostgresStore(workspace=ws, dsn_override=self._dsn)
            scoped._conn = self._conn  # reuse the connection; do not close it
            for note in scoped.search_notes(query, limit):
                out.append({**note, "workspace_id": ws})
        return out[:limit]

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._connect().cursor() as cur:
            cur.execute(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE extraction_status = 'pending') AS pending "
                "FROM notes WHERE workspace_id = %s",
                (self.workspace,),
            )
            row = cur.fetchone() or {"total": 0, "pending": 0}
        # The key names must match the other backends exactly. They did not at
        # first -- this returned `notes` while SQLite and Neo4j return
        # `notes_total` -- and the composite's note-store precedence silently
        # failed to overwrite anything: a merged report showed Neo4j's 54 stale
        # note stubs as `notes_total` beside Postgres's real `notes`, with both
        # numbers looking authoritative.
        return {
            "workspace": self.workspace,
            "notes_total": row["total"],
            "notes_pending": row["pending"],
        }

    # -- the derived half is not ours --------------------------------------

    def _not_the_system_of_record(self, what: str):
        return NotImplementedError(
            f"PostgresStore does not implement {what}: it holds the system of "
            f"record (notes and workspaces), not the derived graph. Pair it "
            f"with a graph engine -- NOTE_BACKEND=postgres GRAPH_BACKEND=neo4j "
            f"-- so triples, clusters and the cached graph have exactly one home."
        )

    def insert_triples(self, triples: list[dict[str, Any]]) -> None:
        raise self._not_the_system_of_record("triple storage")

    def get_all_triples(self) -> list[dict[str, Any]]:
        raise self._not_the_system_of_record("triple storage")

    def delete_triples_for_note(self, note_id: str) -> None:
        raise self._not_the_system_of_record("triple storage")

    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        raise self._not_the_system_of_record("entity resolution storage")

    def get_canonical_map(self) -> dict[str, str]:
        raise self._not_the_system_of_record("entity resolution storage")

    def get_entity_clusters(self) -> list[dict[str, Any]]:
        raise self._not_the_system_of_record("entity resolution storage")

    def save_graph(self, graph: dict[str, Any], stats: dict[str, Any]) -> None:
        raise self._not_the_system_of_record("the graph cache")

    def load_graph(self) -> dict[str, Any] | None:
        raise self._not_the_system_of_record("the graph cache")

    def get_entities(self) -> list[dict[str, Any]]:
        raise self._not_the_system_of_record("entity queries")

    def search_entities(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        raise self._not_the_system_of_record("entity queries")

    def find_path(self, *args: Any, **kwargs: Any) -> Any:
        raise self._not_the_system_of_record("path finding")

    def neighbourhood(self, *args: Any, **kwargs: Any) -> Any:
        raise self._not_the_system_of_record("neighbourhood queries")
