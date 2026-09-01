"""
Neo4j backend — the shared, network-reachable GraphStore.

Implements docs/NEO4J_DATA_MODEL.md:

    (:Note)                     documents
    (:Mention)-[:EXTRACTED_FROM]->(:Note)     raw surface forms + provenance
    (:Mention)-[:RESOLVES_TO]->(:Entity)      entity-resolution merge proof
    (:Entity)-[:<RELATION>]->(:Entity)        the resolved graph, 16 typed rels
    (:Entity)-[:IN_CLUSTER]->(:Cluster)       Louvain communities + summaries

Two notes on fidelity to the design doc:

* raw_triples are stored between :Mention nodes, not :Entity. That is what the
  table actually holds — subject_text/object_text are raw surface forms, and
  resolution happens in a later stage. Storing them as :Entity would assert a
  resolution that has not happened yet.

* The graph is stored ONCE, natively. save_graph() writes :Entity nodes and
  typed relationships; load_graph() reconstructs the frontend's JSON shape by
  reading them back, so the rendered graph and the queried graph cannot drift.
  Only the ANALYSIS output (stats: PageRank rankings, Louvain groupings,
  contradictions, link predictions) is stored on a singleton (:GraphMeta)
  node, because those are NetworkX results that reading the graph back cannot
  re-derive.

TLS: this machine has a TLS-intercepting root CA in its system trust store, so
the driver's default verification fails with "self-signed certificate in
certificate chain". We pass an explicit certifi-backed SSL context, which is
also what the Groq SDK does (and why that provider worked all along).
"""

from __future__ import annotations

import json
import os
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra.stores.base import GraphStore
from brahmastra.workspace import (
    DEFAULT_WORKSPACE, Workspace, current_workspace, validate_id,
)

# Load backend/.env so NEO4J_* are present no matter which entrypoint imports
# us (server, CLI, migration script, MCP server). Routed through env.py so
# the suite can switch every one of these reads off at once.
from brahmastra.env import load_env

load_env()

# The closed set of relation names is the injection boundary: relationship
# types cannot be parameterised in Cypher, so every type we interpolate must
# come from the ontology and nothing else.
from brahmastra.ontology import RELATION_NAMES

_VALID_RELATIONS = {r.lower() for r in RELATION_NAMES}


def relation_to_type(relation: str) -> str:
    """
    Map an ontology relation to a Neo4j relationship type.

    Rejects anything outside the ontology. This is the ONLY place a relation
    becomes interpolated Cypher, so it must stay the single gate.
    """
    key = (relation or "").strip().lower()
    if key not in _VALID_RELATIONS:
        raise ValueError(
            f"relation {relation!r} is not in the ontology; refusing to build Cypher"
        )
    return key.upper()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceIsolationError(RuntimeError):
    """A query touched partitioned data without scoping it to a workspace."""


# Labels that carry workspaceId. :Workspace itself is the registry and is
# global by definition, so it is not in this set.
_PARTITIONED = re.compile(r":(Note|Entity|Mention|Cluster|GraphMeta)\b")

# Schema and admin statements name labels but cannot read or write rows, so a
# workspace predicate would be meaningless in them.
_DDL = re.compile(
    r"^\s*(CREATE\s+(CONSTRAINT|INDEX|VECTOR|FULLTEXT|RANGE)|DROP\s+(CONSTRAINT|INDEX)"
    r"|SHOW\s+|CALL\s+db\.(index|awaitIndex|create)\b|MERGE\s*\(\s*\w*\s*:Workspace)",
    re.IGNORECASE,
)


def _requires_workspace_scope(cypher: str) -> bool:
    """
    True if this query reads or writes partitioned data without naming
    workspaceId.

    This is the structural half of the isolation guarantee. Binding the store
    to a workspace is not enough on its own: property-based partitioning fails
    OPEN, so a single query that forgets the predicate silently returns — or
    overwrites — another workspace's data, exactly as happened once here. A
    forgotten filter now raises instead.
    """
    if _DDL.match(cypher):
        return False
    if not _PARTITIONED.search(cypher):
        return False
    return "workspaceId" not in cypher


# Characters Lucene treats as operators. A natural-language question routinely
# contains them ("Sarah's", "what's the +1?"), and an unescaped one is a query
# parse error rather than zero results — so escape before searching.
_LUCENE_SPECIAL = r'+-&|!(){}[]^"~*?:\/'


def _escape_lucene(term: str) -> str:
    out = []
    for ch in term:
        if ch in _LUCENE_SPECIAL:
            out.append("\\")
        out.append(ch)
    return "".join(out)


# Superseded by the composite constraints below. A globally-unique note id
# would stop two workspaces holding notes with the same id — which they
# legitimately do, since each syncs from its own Notion source.
LEGACY_CONSTRAINTS = [
    "note_id", "entity_name", "mention_text", "cluster_id", "meta_id",
]

# Uniqueness is per workspace, not global. Two workspaces may each have an
# entity called "Sarah" and they are different people; entity resolution must
# never merge them.
CONSTRAINTS = [
    "CREATE CONSTRAINT note_ws_id IF NOT EXISTS FOR (n:Note) "
    "REQUIRE (n.workspaceId, n.id) IS UNIQUE",
    "CREATE CONSTRAINT entity_ws_name IF NOT EXISTS FOR (e:Entity) "
    "REQUIRE (e.workspaceId, e.name) IS UNIQUE",
    "CREATE CONSTRAINT mention_ws_text IF NOT EXISTS FOR (m:Mention) "
    "REQUIRE (m.workspaceId, m.text) IS UNIQUE",
    "CREATE CONSTRAINT cluster_ws_id IF NOT EXISTS FOR (c:Cluster) "
    "REQUIRE (c.workspaceId, c.id) IS UNIQUE",
    "CREATE CONSTRAINT meta_ws IF NOT EXISTS FOR (g:GraphMeta) "
    "REQUIRE g.workspaceId IS UNIQUE",
    "CREATE CONSTRAINT workspace_id IF NOT EXISTS FOR (w:Workspace) "
    "REQUIRE w.id IS UNIQUE",
]

INDEXES = [
    # workspaceId leads every composite index: it is the first predicate of
    # essentially every query, so it must be the leading column.
    "CREATE INDEX note_ws_status IF NOT EXISTS FOR (n:Note) ON (n.workspaceId, n.extractionStatus)",
    "CREATE INDEX entity_ws IF NOT EXISTS FOR (e:Entity) ON (e.workspaceId)",
    "CREATE INDEX mention_ws IF NOT EXISTS FOR (m:Mention) ON (m.workspaceId)",
    "CREATE INDEX note_status IF NOT EXISTS FOR (n:Note) ON (n.extractionStatus)",
    "CREATE INDEX entity_pagerank IF NOT EXISTS FOR (e:Entity) ON (e.pagerank)",
    "CREATE FULLTEXT INDEX noteSearch IF NOT EXISTS FOR (n:Note) ON EACH [n.title, n.content]",
    "CREATE FULLTEXT INDEX entitySearch IF NOT EXISTS FOR (e:Entity) ON EACH [e.name]",
]


def _vector_indexes(dim: int) -> list[str]:
    """
    Vector indexes for semantic search. Dimension must match the embedding
    model; changing models means dropping and rebuilding these.
    """
    return [
        f"""CREATE VECTOR INDEX noteVectors IF NOT EXISTS
            FOR (n:Note) ON (n.embedding)
            OPTIONS {{indexConfig: {{
                `vector.dimensions`: {dim},
                `vector.similarity_function`: 'cosine'
            }}}}""",
        f"""CREATE VECTOR INDEX entityVectors IF NOT EXISTS
            FOR (e:Entity) ON (e.embedding)
            OPTIONS {{indexConfig: {{
                `vector.dimensions`: {dim},
                `vector.similarity_function`: 'cosine'
            }}}}""",
    ]


class Neo4jStore(GraphStore):
    """Shared property-graph backend (Neo4j Aura)."""

    def __init__(self, workspace: str | None = None) -> None:
        # Bound at construction; every query adds the predicate itself, so a
        # caller cannot express — and therefore cannot forget — the filter.
        self.workspace = validate_id(workspace) if workspace else current_workspace()
        self._uri = os.environ.get("NEO4J_URI", "")
        self._user = os.environ.get("NEO4J_USER", "")
        self._password = os.environ.get("NEO4J_PASSWORD", "")
        # Default to None = the server's HOME database. Aura does not always
        # name it "neo4j": on this instance it is named after the instance id,
        # and asking for "neo4j" fails with DatabaseNotFound.
        self._database = os.environ.get("NEO4J_DATABASE") or None
        if not (self._uri and self._user and self._password):
            raise RuntimeError(
                "Neo4j backend selected but NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD "
                "are not all set in backend/.env"
            )
        self._driver = None

    # -- connection --------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        """
        Trust the certifi CA bundle rather than the system store.

        Set NEO4J_SYSTEM_TRUST=1 to use the OS trust store instead, on a machine
        without TLS interception.
        """
        if os.environ.get("NEO4J_SYSTEM_TRUST") == "1":
            return ssl.create_default_context()
        import certifi
        return ssl.create_default_context(cafile=certifi.where())

    def _encrypted(self) -> bool:
        """
        Whether to negotiate TLS at all.

        Aura requires it; a local container speaks plaintext and rejects the
        handshake. Forcing the SSL context unconditionally is why `docker
        compose up` could not reach its own neo4j service -- the failure
        surfaced as "Unable to retrieve routing information", which reads like
        the server being absent rather than a TLS mismatch, and a driver
        created by hand in the same container connected fine.

        Inferred from the host so neither case needs configuring: a hostname
        that is local to the deployment is plaintext, anything else is not.
        NEO4J_ENCRYPTED=0/1 overrides when the guess is wrong.
        """
        override = os.environ.get("NEO4J_ENCRYPTED", "").strip().lower()
        if override in ("0", "false", "no", "off"):
            return False
        if override in ("1", "true", "yes", "on"):
            return True
        host = (self._uri or "").split("//", 1)[-1].split(":", 1)[0].split("/", 1)[0]
        local = host in ("localhost", "127.0.0.1", "::1", "neo4j", "host.docker.internal")
        return not local

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            # A bare neo4j:// scheme (not neo4j+s://) because the +s schemes
            # lock TLS config and forbid supplying our own ssl_context.
            kwargs: dict[str, Any] = {"auth": (self._user, self._password)}
            # Bounded waits, for the same reason PostgresStore sets
            # connect_timeout: this store is REMOTE and can be legitimately
            # absent. Aura Free suspends an idle instance and its hostname then
            # stops resolving, so a call made while it is asleep is not a slow
            # call, it is one that will never be answered -- and an MCP server
            # or a request thread parked on it stays parked, which reads as the
            # whole system hanging rather than one dependency being down.
            #
            # Deliberately generous rather than aggressive: Aura is genuinely
            # slow on the first connection after a resume, and a timeout that
            # fires during a normal wake-up would turn a working instance into
            # an unreachable one.
            kwargs["connection_timeout"] = float(
                os.environ.get("NEO4J_CONNECT_TIMEOUT", "20")
            )
            kwargs["connection_acquisition_timeout"] = float(
                os.environ.get("NEO4J_ACQUIRE_TIMEOUT", "45")
            )
            kwargs["max_transaction_retry_time"] = float(
                os.environ.get("NEO4J_RETRY_TIME", "30")
            )
            # Only supply an SSL context when TLS is actually in play. Passing
            # one to a plaintext server does not downgrade gracefully -- the
            # handshake fails and the driver reports it as a routing problem.
            if self._encrypted():
                kwargs["ssl_context"] = self._ssl_context()
            # Silence DEPRECATION notices. 5.27-aura reports
            # db.index.vector.queryNodes as "replaced by SEARCH", but the
            # SEARCH clause is a syntax error on this version — the notice is
            # aimed at a future release. Left unsilenced it prints once per
            # vector query and drowns real output. Revisit when the server
            # supports SEARCH; the call sites are _vector_notes and
            # search_entities.
            try:
                self._driver = GraphDatabase.driver(
                    self._uri,
                    notifications_disabled_classifications=["DEPRECATION"],
                    # And every INFORMATION notice, which is pure volume. A
                    # schema setup that finds nothing to drop reports each
                    # no-op back as a multi-line GqlStatusObject, so one
                    # init_db emitted 552 log lines and 44KB of stderr.
                    #
                    # That is not merely noisy, it DEADLOCKS the MCP server.
                    # A stdio server is spawned with pipes, and a client that
                    # does not drain stderr lets the OS buffer fill -- 4KB to
                    # 64KB on Windows -- at which point the next write blocks
                    # forever, mid-call, and the client waits for a response
                    # that can never come. Two sessions lost 30 minutes each
                    # to what looked like a hung query and was a full pipe.
                    notifications_min_severity="WARNING",
                    **kwargs,
                )
            except TypeError:
                # Older driver without notification filtering.
                self._driver = GraphDatabase.driver(self._uri, **kwargs)
        return self._driver

    def _run(self, cypher: str, *, unscoped: bool = False, **params) -> list[dict[str, Any]]:
        """
        Run Cypher, refusing anything that touches partitioned data unscoped.

        Pass unscoped=True only for a query that genuinely spans workspaces —
        cross-workspace search, or the migration that tags untagged nodes.
        Making that explicit means an accidental omission is an error, while a
        deliberate one is visible at the call site.
        """
        if not unscoped and _requires_workspace_scope(cypher):
            raise WorkspaceIsolationError(
                "Refusing to run a query that touches partitioned data without "
                "a workspaceId predicate — it would read or write across "
                "workspaces. Add the filter, or pass unscoped=True if crossing "
                f"workspaces is intended.\n\n{cypher.strip()[:400]}"
            )
        with self.driver.session(database=self._database) as s:
            return [r.data() for r in s.run(cypher, **params)]

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    # -- lifecycle ---------------------------------------------------------

    def init_schema(self) -> None:
        # Existing nodes must be tagged BEFORE the composite constraints are
        # created: an untagged node has workspaceId = null, and a uniqueness
        # constraint over (null, id) would either reject the create or leave
        # rows outside every workspace.
        self._migrate_to_workspaces()

        # Drop the old single-property constraints, which would still force
        # note ids to be globally unique and so prevent two workspaces from
        # syncing notes with the same id.
        for name in LEGACY_CONSTRAINTS:
            try:
                self._run(f"DROP CONSTRAINT {name} IF EXISTS")
            except Exception:
                pass

        for stmt in CONSTRAINTS + INDEXES:
            try:
                self._run(stmt)
            except Exception:
                # An index that already exists under a different name, or a
                # constraint the tier does not support, must not stop the
                # store from working.
                pass

        from brahmastra.embeddings import DIM
        for stmt in _vector_indexes(DIM):
            try:
                self._run(stmt)
            except Exception:
                pass

        # Register the workspace so it can be listed before holding content.
        self._run(
            """
            MERGE (w:Workspace {id: $id})
            ON CREATE SET w.name = $id, w.description = '',
                          w.ontology = 'default', w.createdAt = $now
            """,
            id=self.workspace, now=_now(),
        )

    def _migrate_to_workspaces(self) -> None:
        """
        Put pre-workspace nodes into DEFAULT_WORKSPACE.

        Idempotent and batched: `workspaceId IS NULL` matches only untagged
        nodes, so a second run does nothing.
        """
        for label in ("Note", "Entity", "Mention", "Cluster", "GraphMeta"):
            while True:
                done = self._run(
                    f"""
                    MATCH (n:{label}) WHERE n.workspaceId IS NULL
                    WITH n LIMIT 5000
                    SET n.workspaceId = $ws
                    RETURN count(n) AS c
                    """,
                    ws=DEFAULT_WORKSPACE,
                )[0]["c"]
                if not done:
                    break

    # -- workspace registry ------------------------------------------------

    def list_workspaces(self) -> list[dict[str, Any]]:
        rows = self._run(
            "MATCH (w:Workspace) RETURN w.id AS id, w.name AS name, "
            "w.description AS description, w.notionDatabaseId AS notion_database_id, "
            "w.ontology AS ontology, w.createdAt AS created_at ORDER BY w.createdAt"
        )
        return rows

    def create_workspace(self, ws: Workspace) -> dict[str, Any]:
        self._run(
            """
            MERGE (w:Workspace {id: $id})
            SET w.name = $name, w.description = $description,
                w.notionDatabaseId = $notion, w.ontology = $ontology,
                w.createdAt = coalesce(w.createdAt, $created)
            """,
            id=ws.id, name=ws.name, description=ws.description,
            notion=ws.notion_database_id, ontology=ws.ontology,
            created=ws.created_at,
        )
        return ws.to_dict()

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (w:Workspace {id: $id}) RETURN w.id AS id, w.name AS name, "
            "w.description AS description, w.notionDatabaseId AS notion_database_id, "
            "w.ontology AS ontology, w.createdAt AS created_at",
            id=workspace_id,
        )
        return rows[0] if rows else None

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and every node partitioned under it."""
        while True:
            done = self._run(
                """
                MATCH (n) WHERE n.workspaceId = $ws
                WITH n LIMIT 5000 DETACH DELETE n
                RETURN count(n) AS c
                """,
                ws=workspace_id,
            )[0]["c"]
            if not done:
                break
        self._run("MATCH (w:Workspace {id: $id}) DETACH DELETE w", id=workspace_id)

    def search_notes_across(
        self, query: str, workspaces: list[str] | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search every workspace, or a named subset. Results carry their workspace."""
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        rows = self._run(
            """
            MATCH (n:Note)
            WHERE ($wss IS NULL OR n.workspaceId IN $wss)
            WITH n, toLower(coalesce(n.title,'') + ' ' + coalesce(n.content,'')) AS hay
            WITH n, [t IN $terms WHERE hay CONTAINS t] AS hits
            WHERE size(hits) > 0
            RETURN n, size(hits) AS matched
            ORDER BY matched DESC, n.lastEdited DESC
            LIMIT $limit
            """,
            terms=terms, wss=workspaces, limit=limit, unscoped=True,
        )
        return self._note_rows(rows)
        # Vector indexes are created separately: an older server without vector
        # support should still get a working store, just without semantic
        # search, rather than failing to initialise at all.
        from brahmastra.embeddings import DIM
        for stmt in _vector_indexes(DIM):
            try:
                self._run(stmt)
            except Exception:
                pass

    def describe(self) -> str:
        return f"neo4j:{self._uri}/{self._database or '<home>'}#{self.workspace}"

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
        # Mirrors the SQLite CASE: caller-requested pending wins, otherwise a
        # changed last_edited re-opens the note, otherwise keep current status.
        self._run(
            """
            MERGE (n:Note {id: $id, workspaceId: $ws})
            ON CREATE SET n.extractionStatus = $status
            SET n.title = $title,
                n.content = $content,
                n.lastSynced = $now,
                n.extractionStatus = CASE
                    WHEN $status = 'pending' THEN 'pending'
                    WHEN n.lastEdited IS NOT NULL AND n.lastEdited <> $lastEdited THEN 'pending'
                    ELSE n.extractionStatus
                END,
                n.lastEdited = $lastEdited,
                // NULL means "leave as is", so a sync cannot silently
                // unpublish a note somebody chose to publish.
                n.publish = CASE WHEN $publish IS NULL
                                 THEN coalesce(n.publish, false)
                                 ELSE $publish END,
                // Keep a known origin, upgrade an unknown one — matching
                // SQLite. A re-sync must not relabel where a note came from.
                n.source = CASE
                    WHEN n.source IS NULL OR n.source = 'unknown'
                        THEN coalesce($source, 'unknown')
                    ELSE n.source
                END
            """,
            id=id, ws=self.workspace, title=title, content=content,
            lastEdited=last_edited, now=_now(),
            status=("pending" if mark_pending else "done"),
            publish=(None if publish is None else bool(publish)),
            source=source,
        )
        self._embed_note(id, title, content)

    def _embed_note(self, note_id: str, title: str, content: str) -> None:
        """
        Store the note's embedding for semantic search.

        Fails soft: without sentence-transformers the note is still saved and
        searchable lexically, just not semantically. Embedding on write keeps
        the index current without a separate backfill pass.
        """
        from brahmastra.embeddings import embed_one
        vec = embed_one(f"{title}\n\n{content}")
        if vec is None:
            return
        try:
            self._run(
                """
                MATCH (n:Note {id: $id, workspaceId: $ws})
                CALL db.create.setNodeVectorProperty(n, 'embedding', $vec)
                RETURN n.id
                """,
                id=note_id, ws=self.workspace, vec=vec,
            )
        except Exception:
            pass

    def _note_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return notes in the snake_case shape the rest of the codebase expects."""
        out = []
        for r in rows:
            n = r["n"]
            out.append({
                "id": n.get("id"),
                "title": n.get("title"),
                "content": n.get("content"),
                "last_edited": n.get("lastEdited"),
                "last_synced": n.get("lastSynced"),
                "extraction_status": n.get("extractionStatus"),
                "extraction_error": n.get("extractionError"),
                "workspace_id": n.get("workspaceId"),
                "publish": bool(n.get("publish")),
                "notion_page_id": n.get("notionPageId"),
                "source": n.get("source") or "unknown",
            })
        return out

    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._run(
                "MATCH (n:Note) WHERE n.workspaceId = $ws "
                "AND n.extractionStatus = $status RETURN n ORDER BY n.lastEdited DESC",
                ws=self.workspace, status=status,
            )
        else:
            rows = self._run(
                "MATCH (n:Note) WHERE n.workspaceId = $ws RETURN n ORDER BY n.lastEdited DESC",
                ws=self.workspace,
            )
        return self._note_rows(rows)

    # Reciprocal Rank Fusion constant. 60 is the value from the original RRF
    # paper and the usual default; it damps the top ranks so one engine cannot
    # dominate purely by being confident.
    RRF_K = 60

    def _fulltext_notes(self, query: str, limit: int) -> list[str]:
        """Note ids by BM25 relevance. Returns [] if the query is unparseable."""
        # Lucene syntax: a stray quote or bare AND/OR from a natural-language
        # question is a parse error, so the terms are escaped and OR-joined.
        terms = [_escape_lucene(t) for t in (query or "").split() if t.strip()]
        if not terms:
            return []
        lucene = " OR ".join(terms)
        try:
            rows = self._run(
                """
                CALL db.index.fulltext.queryNodes('noteSearch', $q, {limit: $over})
                YIELD node, score
                WHERE node.workspaceId = $ws
                RETURN node.id AS id ORDER BY score DESC LIMIT $limit
                """,
                q=lucene, limit=limit, over=limit * 5, ws=self.workspace,
            )
        except Exception:
            return []
        return [r["id"] for r in rows]

    def _vector_notes(self, query: str, limit: int) -> list[str]:
        """Note ids by embedding similarity. [] when embeddings are unavailable."""
        from brahmastra.embeddings import embed_one
        vec = embed_one(query)
        if vec is None:
            return []
        try:
            rows = self._run(
                """
                CALL db.index.vector.queryNodes('noteVectors', $over, $vec)
                YIELD node, score
                WHERE node.workspaceId = $ws
                RETURN node.id AS id ORDER BY score DESC LIMIT $limit
                """,
                limit=limit, over=limit * 5, vec=vec, ws=self.workspace,
            )
        except Exception:
            return []
        return [r["id"] for r in rows]

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Hybrid search: BM25 relevance fused with embedding similarity.

        Lexical search finds exact terms and names; vector search finds notes
        that mean the same thing in different words ("boss" -> "reports_to").
        Neither alone is enough, so both run and their rankings are combined
        with Reciprocal Rank Fusion, which needs no score normalisation —
        BM25 scores and cosine similarities are not on comparable scales.

        Degrades cleanly: without embeddings this is pure BM25, and if the
        fulltext index is missing too it falls back to the substring scan.
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
                scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (self.RRF_K + rank + 1)

        ordered = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]
        if not ordered:
            return []
        rows = self._run(
            "MATCH (n:Note) WHERE n.workspaceId = $ws AND n.id IN $ids RETURN n",
            ids=ordered, ws=self.workspace
        )
        by_id = {r["n"].get("id"): r for r in rows}
        # Preserve fusion order; the Cypher IN does not guarantee it.
        return self._note_rows([by_id[i] for i in ordered if i in by_id])

    def _search_notes_substring(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Last-resort scan, matching the SQLite backend's semantics."""
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        rows = self._run(
            """
            MATCH (n:Note) WHERE n.workspaceId = $ws
            WITH n, toLower(coalesce(n.title,'') + ' ' + coalesce(n.content,'')) AS hay
            WITH n, hay, [t IN $terms WHERE hay CONTAINS t] AS hits
            WHERE size(hits) > 0
            RETURN n, size(hits) AS matched
            ORDER BY matched DESC, n.lastEdited DESC
            """,
            terms=terms, ws=self.workspace,
        )
        full = [r for r in rows if r["matched"] == len(terms)]
        pool = full if full else rows
        return self._note_rows(pool[:limit])

    def get_note(self, id: str) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (n:Note {id: $id, workspaceId: $ws}) RETURN n",
            id=id, ws=self.workspace,
        )
        got = self._note_rows(rows)
        return got[0] if got else None

    def get_notes_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """One round trip instead of one per id -- the point of overriding this."""
        if not ids:
            return {}
        rows = self._run(
            "MATCH (n:Note) WHERE n.workspaceId = $ws AND n.id IN $ids RETURN n",
            ws=self.workspace, ids=list(dict.fromkeys(ids)),
        )
        return {n["id"]: n for n in self._note_rows(rows) if n.get("id")}

    def set_notion_page_id(self, note_id: str, page_id: str) -> None:
        """Remember the Notion page created for this note."""
        self._run(
            "MATCH (n:Note {id: $id, workspaceId: $ws}) SET n.notionPageId = $pid",
            id=note_id, ws=self.workspace, pid=page_id,
        )

    def capabilities(self) -> frozenset[str]:
        """
        Fulltext, vectors, and the RRF fusion of the two. Implemented in
        _fulltext_notes / _vector_notes / search_notes; declared here so a
        composite can check before trusting this store with note search.
        """
        from brahmastra.stores.base import (
            CAP_FULLTEXT_SEARCH, CAP_HYBRID_SEARCH,
            CAP_LEXICAL_SEARCH, CAP_VECTOR_SEARCH,
        )
        return frozenset({
            CAP_LEXICAL_SEARCH, CAP_FULLTEXT_SEARCH,
            CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH,
        })

    def set_note_status(self, id: str, status: str, error: str | None = None) -> None:
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid extraction status: {status!r}")
        self._run(
            "MATCH (n:Note {id: $id, workspaceId: $ws}) "
            "SET n.extractionStatus = $status, n.extractionError = $error",
            id=id, ws=self.workspace, status=status,
            error=error if status == "error" else None,
        )

    def delete_note(self, id: str) -> None:
        self.delete_triples_for_note(id)
        self._run(
            "MATCH (n:Note {id: $id, workspaceId: $ws}) DETACH DELETE n",
            id=id, ws=self.workspace,
        )

    # -- triples -----------------------------------------------------------

    def delete_triples_for_note(self, note_id: str) -> None:
        # Drop the asserted facts from this note, then any mention left with no
        # remaining provenance (mentions exist only to carry triples).
        self._run(
            "MATCH (a:Mention)-[r:ASSERTS]->(b:Mention) "
            "WHERE a.workspaceId = $ws AND r.sourceNoteId = $nid DELETE r",
            ws=self.workspace, nid=note_id,
        )
        self._run(
            "MATCH (m:Mention)-[:EXTRACTED_FROM]->(n:Note {id: $nid, workspaceId: $ws}) "
            "WHERE NOT (m)-[:ASSERTS]-() DETACH DELETE m",
            ws=self.workspace, nid=note_id,
        )

    def insert_triples(self, triples: list[dict[str, Any]]) -> None:
        if not triples:
            return
        now = _now()
        rows = []
        for t in triples:
            rows.append({
                "subject": t["subject_text"],
                "subjectType": t.get("subject_type", "unknown"),
                "relation": (t["relation"] or "").strip().lower(),
                "object": t["object_text"],
                "objectType": t.get("object_type", "unknown"),
                "confidence": float(t.get("confidence", 1.0)),
                "sourceQuote": t.get("source_quote"),
                "sourceNoteId": t.get("source_note_id"),
                "extractedAt": now,
            })
        # MERGE, never CREATE. The normal extraction path deletes a note's
        # triples before re-inserting, so CREATE looked safe — but migration
        # does not delete, and re-running it appended a second copy of every
        # triple: 633 in SQLite became 1028 in Neo4j. Identity is
        # (subject, relation, object, sourceNoteId); confidence and quote are
        # mutable and get overwritten on match.
        # One generic :ASSERTS relationship carrying the relation name. The
        # ontology-typed relationships are created in save_graph() between
        # resolved :Entity nodes; raw mentions are pre-resolution and would
        # otherwise assert typed edges between surface forms.
        self._run(
            """
            UNWIND $rows AS row
            MERGE (s:Mention {text: row.subject, workspaceId: $ws})
              ON CREATE SET s.type = row.subjectType
            MERGE (o:Mention {text: row.object, workspaceId: $ws})
              ON CREATE SET o.type = row.objectType
            MERGE (s)-[a:ASSERTS {
                relation: row.relation,
                sourceNoteId: row.sourceNoteId
            }]->(o)
            SET a.confidence = row.confidence,
                a.sourceQuote = row.sourceQuote,
                a.extractedAt = row.extractedAt
            WITH s, o, row
            MATCH (n:Note {id: row.sourceNoteId, workspaceId: $ws})
            MERGE (s)-[:EXTRACTED_FROM]->(n)
            MERGE (o)-[:EXTRACTED_FROM]->(n)
            """,
            rows=rows, ws=self.workspace,
        )

    def get_all_triples(self) -> list[dict[str, Any]]:
        rows = self._run(
            """
            MATCH (s:Mention)-[r:ASSERTS]->(o:Mention)
            WHERE s.workspaceId = $ws
            RETURN s.text AS subject_text, s.type AS subject_type,
                   r.relation AS relation,
                   o.text AS object_text, o.type AS object_type,
                   r.confidence AS confidence, r.sourceQuote AS source_quote,
                   r.sourceNoteId AS source_note_id, r.extractedAt AS extracted_at
            ORDER BY r.extractedAt DESC
            """,
            ws=self.workspace,
        )
        return rows

    # -- entity resolution -------------------------------------------------

    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        # Wholesale replacement, matching the SQLite backend: resolution is
        # recomputed every run and a partial map would corrupt the graph.
        self._run(
            "MATCH (m:Mention)-[r:RESOLVES_TO]->(:Entity) WHERE m.workspaceId = $ws DELETE r",
            ws=self.workspace,
        )
        self._run("MATCH (e:Entity) WHERE e.workspaceId = $ws DETACH DELETE e",
                  ws=self.workspace)
        if not clusters:
            return
        self._run(
            """
            UNWIND $clusters AS c
            MERGE (e:Entity {name: c.canonical_name, workspaceId: $ws})
              SET e.clusterId = c.cluster_id
            WITH e, c
            UNWIND c.mentions AS mention
            MERGE (m:Mention {text: mention, workspaceId: $ws})
            MERGE (m)-[:RESOLVES_TO]->(e)
            """,
            ws=self.workspace,
            clusters=[
                {
                    "cluster_id": str(c["cluster_id"]),
                    "canonical_name": c["canonical_name"],
                    "mentions": c["mentions"],
                }
                for c in clusters
            ],
        )

    def get_canonical_map(self) -> dict[str, str]:
        rows = self._run(
            "MATCH (m:Mention)-[:RESOLVES_TO]->(e:Entity) WHERE m.workspaceId = $ws "
            "RETURN m.text AS mention, e.name AS canonical",
            ws=self.workspace,
        )
        return {r["mention"]: r["canonical"] for r in rows}

    def get_entity_clusters(self) -> list[dict[str, Any]]:
        rows = self._run(
            """
            MATCH (e:Entity) WHERE e.workspaceId = $ws
            OPTIONAL MATCH (m:Mention)-[:RESOLVES_TO]->(e)
            RETURN e.clusterId AS cluster_id, e.name AS canonical_name,
                   collect(m.text) AS mentions
            """,
            ws=self.workspace,
        )
        return [
            {
                "cluster_id": r["cluster_id"],
                "canonical_name": r["canonical_name"],
                "mentions": [m for m in r["mentions"] if m is not None],
            }
            for r in rows
        ]

    # -- built graph -------------------------------------------------------

    def save_graph(self, graph: dict[str, Any], stats: dict[str, Any]) -> None:
        """
        Write the resolved graph natively, then store the render projection.

        The native write is what makes indexed traversal and multi-hop Cypher
        possible; the projection keeps the existing frontend contract intact.
        """
        nodes = graph.get("nodes", []) or []
        edges = graph.get("edges", []) or []

        # Node properties (PageRank/cluster come from NetworkX, not the DB).
        if nodes:
            self._run(
                """
                UNWIND $nodes AS n
                MERGE (e:Entity {name: n.id, workspaceId: $ws})
                SET e.label = n.label, e.type = n.type,
                    e.pagerank = n.pagerank, e.clusterId = toString(n.cluster)
                """,
                ws=self.workspace,
                nodes=[
                    {
                        "id": str(n.get("id")),
                        "label": n.get("label"),
                        "type": n.get("type"),
                        "pagerank": float(n.get("pagerank") or 0.0),
                        "cluster": n.get("cluster"),
                    }
                    for n in nodes
                ],
            )

        # Entity embeddings, for semantic entity matching in GraphRAG. Batched
        # in one encode call — encoding 200 names individually would dominate
        # the build time.
        self._embed_entities([str(n.get("id")) for n in nodes])

        # Typed relationships, one Cypher statement per relation type because
        # the type cannot be parameterised. relation_to_type() rejects anything
        # outside the ontology, so nothing unvalidated reaches the query text.
        by_type: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            try:
                rel_type = relation_to_type(e.get("relation", ""))
            except ValueError:
                continue  # skip rather than fail the whole build
            by_type.setdefault(rel_type, []).append({
                "source": str(e.get("source")),
                "target": str(e.get("target")),
                "confidence": float(e.get("confidence") or 1.0),
                "sourceQuote": e.get("source_quote") or e.get("sourceQuote"),
                "sourceNoteId": e.get("note_id") or e.get("source_note_id"),
            })

        # Clear previously written typed edges so a rebuild does not duplicate.
        self._run(
            "MATCH (s:Entity)-[r]->(:Entity) WHERE s.workspaceId = $ws "
            "AND type(r) <> 'IN_CLUSTER' DELETE r",
            ws=self.workspace,
        )

        for rel_type, rows in by_type.items():
            self._run(
                f"""
                UNWIND $rows AS row
                MATCH (s:Entity {{name: row.source, workspaceId: $ws}})
                MATCH (t:Entity {{name: row.target, workspaceId: $ws}})
                CREATE (s)-[:{rel_type} {{
                    confidence: row.confidence,
                    sourceQuote: row.sourceQuote,
                    sourceNoteId: row.sourceNoteId
                }}]->(t)
                """,
                rows=rows, ws=self.workspace,
            )

        # Clusters, including any LLM summaries carried on the projection.
        clusters = (stats or {}).get("clusters") or graph.get("clusters") or []
        if clusters:
            self._run(
                """
                UNWIND $clusters AS c
                MERGE (cl:Cluster {id: c.id, workspaceId: $ws})
                SET cl.summary = c.summary, cl.size = c.size, cl.builtAt = $now
                WITH cl, c
                UNWIND c.members AS member
                MATCH (e:Entity {name: member, workspaceId: $ws})
                MERGE (e)-[:IN_CLUSTER]->(cl)
                """,
                now=_now(), ws=self.workspace,
                clusters=[
                    {
                        "id": str(c.get("cluster_id", c.get("id"))),
                        "summary": c.get("summary"),
                        "size": c.get("size") or len(c.get("members", []) or []),
                        "members": [str(m) for m in (c.get("members") or [])],
                    }
                    for c in clusters
                ],
            )

        # Only the ANALYSIS output is stored, not the graph itself. stats holds
        # PageRank rankings, Louvain groupings, contradictions and link
        # predictions — results of NetworkX runs that cannot be re-derived by
        # reading the graph back. The graph structure is no longer duplicated
        # here; load_graph() reconstructs it from the nodes and edges above.
        self._run(
            """
            MERGE (g:GraphMeta {workspaceId: $ws})
            SET g.builtAt = $now, g.statsJson = $stats
            REMOVE g.graphJson
            """,
            ws=self.workspace, now=_now(), stats=json.dumps(stats),
        )

    def load_graph(self) -> dict[str, Any] | None:
        """
        Rebuild the frontend graph shape from the native graph.

        Reads :Entity nodes and their typed relationships rather than a stored
        blob, so what the UI renders is the graph itself and the two can no
        longer drift apart.
        """
        meta = self._run(
            "MATCH (g:GraphMeta {workspaceId: $ws}) RETURN g.builtAt AS builtAt, "
            "g.statsJson AS statsJson",
            ws=self.workspace,
        )
        if not meta:
            return None

        node_rows = self._run(
            """
            MATCH (e:Entity) WHERE e.workspaceId = $ws
            RETURN e.name AS id, coalesce(e.label, e.name) AS label,
                   coalesce(e.type, 'unknown') AS type,
                   coalesce(e.pagerank, 0.0) AS pagerank,
                   e.clusterId AS cluster
            ORDER BY e.pagerank DESC
            """,
            ws=self.workspace,
        )
        nodes = []
        for r in node_rows:
            # cluster is stored as a string; the frontend expects the integer
            # Louvain id it was built with.
            raw = r["cluster"]
            try:
                cluster = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                cluster = 0
            nodes.append({
                "id": r["id"],
                "label": r["label"],
                "type": r["type"],
                "pagerank": round(float(r["pagerank"] or 0.0), 6),
                "cluster": cluster,
            })

        edge_rows = self._run(
            """
            MATCH (s:Entity)-[r]->(t:Entity)
            WHERE s.workspaceId = $ws AND type(r) <> 'IN_CLUSTER'
            RETURN s.name AS source, t.name AS target, type(r) AS relation,
                   coalesce(r.sourceQuote, '') AS source_quote,
                   coalesce(r.sourceNoteId, '') AS note_id,
                   coalesce(r.confidence, 1.0) AS confidence
            """,
            ws=self.workspace,
        )
        edges = [
            {
                "source": r["source"],
                "target": r["target"],
                # Relationship types are UPPER_SNAKE on the wire; the rest of
                # the app speaks the ontology's lowercase relation names.
                "relation": r["relation"].lower(),
                "source_quote": r["source_quote"],
                "note_id": r["note_id"],
                "confidence": round(float(r["confidence"] or 1.0), 3),
            }
            for r in edge_rows
        ]

        return {
            "built_at": meta[0]["builtAt"],
            "graph": {"nodes": nodes, "edges": edges},
            "stats": json.loads(meta[0]["statsJson"] or "{}"),
        }

    def _embed_entities(self, names: list[str]) -> None:
        """Embed entity names in one batch. Fails soft, like note embedding."""
        from brahmastra.embeddings import embed
        names = [n for n in names if n]
        if not names:
            return
        vecs = embed(names)
        if vecs is None:
            return
        try:
            self._run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {name: row.name, workspaceId: $ws})
                CALL db.create.setNodeVectorProperty(e, 'embedding', row.vec)
                RETURN count(*)
                """,
                ws=self.workspace,
                rows=[{"name": n, "vec": v} for n, v in zip(names, vecs)],
            )
        except Exception:
            pass

    def search_entities(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """
        Find entities a question is about, by meaning as well as by spelling.

        Fuses fulltext over entity names with embedding similarity, the same
        way search_notes does. This replaces token-overlap matching, which
        could only find an entity whose words literally appeared in the
        question — so "who is Sarah's boss" missed `reports_to` entirely.
        """
        if not (query or "").strip():
            return []
        pool = max(limit * 5, 20)

        lexical: list[str] = []
        terms = [_escape_lucene(t) for t in query.split() if t.strip()]
        if terms:
            try:
                lexical = [
                    r["name"] for r in self._run(
                        """
                        CALL db.index.fulltext.queryNodes('entitySearch', $q, {limit: $over})
                        YIELD node, score
                        WHERE node.workspaceId = $ws
                        RETURN node.name AS name ORDER BY score DESC LIMIT $limit
                        """,
                        q=" OR ".join(terms), limit=pool, over=pool * 5,
                        ws=self.workspace,
                    )
                ]
            except Exception:
                lexical = []

        semantic: list[str] = []
        from brahmastra.embeddings import embed_one
        vec = embed_one(query)
        if vec is not None:
            try:
                semantic = [
                    r["name"] for r in self._run(
                        """
                        CALL db.index.vector.queryNodes('entityVectors', $over, $vec)
                        YIELD node, score
                        WHERE node.workspaceId = $ws
                        RETURN node.name AS name ORDER BY score DESC LIMIT $limit
                        """,
                        limit=pool, over=pool * 5, vec=vec, ws=self.workspace,
                    )
                ]
            except Exception:
                semantic = []

        if not lexical and not semantic:
            return []

        scores: dict[str, float] = {}
        for ranking in (lexical, semantic):
            for rank, name in enumerate(ranking):
                scores[name] = scores.get(name, 0.0) + 1.0 / (self.RRF_K + rank + 1)

        ordered = sorted(scores, key=lambda n: scores[n], reverse=True)[:limit]
        rows = self._run(
            """
            MATCH (e:Entity) WHERE e.workspaceId = $ws AND e.name IN $names
            RETURN e.name AS id, coalesce(e.label, e.name) AS label,
                   coalesce(e.type,'unknown') AS type,
                   coalesce(e.pagerank, 0.0) AS pagerank, e.clusterId AS cluster
            """,
            names=ordered, ws=self.workspace,
        )
        by_name = {r["id"]: r for r in rows}
        out = []
        for name in ordered:
            r = by_name.get(name)
            if not r:
                continue
            try:
                cluster = int(r["cluster"]) if r["cluster"] is not None else 0
            except (TypeError, ValueError):
                cluster = 0
            out.append({
                "id": r["id"], "label": r["label"], "type": r["type"],
                "pagerank": round(float(r["pagerank"] or 0.0), 6),
                "cluster": cluster,
            })
        return out

    def find_path(
        self, source: str, target: str, max_hops: int = 5
    ) -> list[dict[str, Any]]:
        """
        Shortest path between two entities, as a list of hops.

        Native shortestPath: the server walks the graph and returns only the
        path, instead of loading every edge into Python to run a BFS. This is
        the question "how are these two things connected?", which nothing in
        the product could previously answer.
        """
        h = max(1, min(int(max_hops), 10))
        rows = self._run(
            f"""
            MATCH (a:Entity {{name: $source, workspaceId: $ws}}),
                  (b:Entity {{name: $target, workspaceId: $ws}})
            MATCH p = shortestPath((a)-[*..{h}]-(b))
            WHERE none(r IN relationships(p) WHERE type(r) = 'IN_CLUSTER')
            RETURN [n IN nodes(p) | n.name] AS names,
                   [r IN relationships(p) | type(r)] AS rels,
                   [r IN relationships(p) | coalesce(r.sourceNoteId,'')] AS notes,
                   [r IN relationships(p) | startNode(r).name] AS starts
            LIMIT 1
            """,
            source=source, target=target, ws=self.workspace,
        )
        if not rows:
            return []
        r = rows[0]
        names, rels, notes, starts = r["names"], r["rels"], r["notes"], r["starts"]
        hops = []
        for i, rel in enumerate(rels):
            a, b = names[i], names[i + 1]        # walk order
            forward = starts[i] == a
            # from/to always state the fact as stored, so a hop reads as a true
            # sentence: walking Mei -> Raj across "Raj reports_to Mei" must not
            # render as "Mei reports_to Raj". walk_from/walk_to keep the
            # traversal order for anyone drawing the path.
            subject, obj = (a, b) if forward else (b, a)
            hops.append({
                "from": subject,
                "relation": rel.lower(),
                "to": obj,
                "direction": "forward" if forward else "reverse",
                "walk_from": a,
                "walk_to": b,
                "note_id": notes[i],
            })
        return hops

    def get_entities(self) -> list[dict[str, Any]]:
        """Nodes only — no edge scan, unlike the SQLite backend."""
        rows = self._run(
            """
            MATCH (e:Entity) WHERE e.workspaceId = $ws
            RETURN e.name AS id, coalesce(e.label, e.name) AS label,
                   coalesce(e.type, 'unknown') AS type,
                   coalesce(e.pagerank, 0.0) AS pagerank, e.clusterId AS cluster
            ORDER BY e.pagerank DESC
            """,
            ws=self.workspace,
        )
        out = []
        for r in rows:
            try:
                cluster = int(r["cluster"]) if r["cluster"] is not None else 0
            except (TypeError, ValueError):
                cluster = 0
            out.append({
                "id": r["id"], "label": r["label"], "type": r["type"],
                "pagerank": round(float(r["pagerank"] or 0.0), 6),
                "cluster": cluster,
            })
        return out

    MAX_DEPTH = 3

    def neighbourhood(
        self, names: set[str], limit: int = 40, depth: int = 1
    ) -> list[dict[str, Any]]:
        """
        Native traversal — the operation this backend exists for.

        Matches on the :Entity name index and walks only the edges reachable
        from those nodes, instead of scanning every edge in the graph. At
        depth>1 it follows chains, which is what makes multi-hop questions
        answerable at all.
        """
        if not names:
            return []
        # depth is interpolated because Cypher cannot parameterise the bounds of
        # a variable-length pattern. It is coerced to an int and clamped, so
        # nothing caller-controlled reaches the query text. MAX_DEPTH exists
        # because path counts grow sharply per hop.
        d = max(1, min(int(depth), self.MAX_DEPTH))

        if d == 1:
            rows = self._run(
                """
                MATCH (e:Entity)-[r]-(o:Entity)
                WHERE e.workspaceId = $ws AND e.name IN $names
                  AND type(r) <> 'IN_CLUSTER'
                WITH startNode(r) AS s, endNode(r) AS t, r
                RETURN DISTINCT s.name AS subject, type(r) AS relation, t.name AS object,
                       coalesce(r.sourceQuote, '') AS quote,
                       coalesce(r.sourceNoteId, '') AS note_id,
                       coalesce(r.confidence, 1.0) AS confidence,
                       1 AS hops
                ORDER BY confidence DESC
                LIMIT $limit
                """,
                names=sorted(names), limit=limit, ws=self.workspace,
            )
        else:
            # Every relationship along any path of length <= d from a seed,
            # tagged with the shortest path length it appeared on so direct
            # facts can outrank distant ones.
            rows = self._run(
                f"""
                MATCH p = (e:Entity)-[*1..{d}]-(o:Entity)
                WHERE e.workspaceId = $ws AND e.name IN $names
                  AND none(rel IN relationships(p) WHERE type(rel) = 'IN_CLUSTER')
                UNWIND relationships(p) AS r
                WITH r, startNode(r) AS s, endNode(r) AS t, min(length(p)) AS hops
                RETURN DISTINCT s.name AS subject, type(r) AS relation, t.name AS object,
                       coalesce(r.sourceQuote, '') AS quote,
                       coalesce(r.sourceNoteId, '') AS note_id,
                       coalesce(r.confidence, 1.0) AS confidence,
                       hops
                ORDER BY hops ASC, confidence DESC
                LIMIT $limit
                """,
                names=sorted(names), limit=limit, ws=self.workspace,
            )

        facts: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for r in rows:
            rel = r["relation"].lower()
            key = (r["subject"], rel, r["object"])
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "text": f'{r["subject"]} {rel} {r["object"]}',
                "quote": r["quote"],
                "note_id": r["note_id"],
                "confidence": float(r["confidence"] or 1.0),
                "hops": int(r["hops"]),
            })
        return facts

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        r = self._run(
            """
            OPTIONAL MATCH (n:Note) WHERE n.workspaceId = $ws
              WITH count(n) AS notes
            OPTIONAL MATCH (p:Note) WHERE p.workspaceId = $ws
              AND p.extractionStatus = 'pending'
              WITH notes, count(p) AS pending
            OPTIONAL MATCH (a:Mention)-[t:ASSERTS]->() WHERE a.workspaceId = $ws
              WITH notes, pending, count(t) AS triples
            OPTIONAL MATCH (e:Entity) WHERE e.workspaceId = $ws
              WITH notes, pending, triples, count(e) AS entities
            OPTIONAL MATCH (g:GraphMeta) WHERE g.workspaceId = $ws
              RETURN notes, pending, triples, entities, count(g) AS meta
            """,
            ws=self.workspace,
        )[0]
        return {
            "workspace": self.workspace,
            "notes_total": r["notes"],
            "notes_pending": r["pending"],
            "triples_total": r["triples"],
            "entity_clusters": r["entities"],
            "graph_cached": bool(r["meta"]),
        }
