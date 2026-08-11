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

* save_graph() writes the resolved graph natively (:Entity nodes + typed
  relationships), which is what makes the Cypher traversals in the design doc
  possible. It ALSO stores the computed projection on a singleton (:GraphMeta)
  node, and load_graph() reads that back. The frontend expects an exact JSON
  shape including PageRank and Louvain output that NetworkX computes, so
  reconstructing it from the native graph is a follow-up, not a prerequisite.
  The native graph is authoritative for queries; the projection is a render
  cache. Dropping it is tracked in the design doc's open items.

TLS: this machine has a TLS-intercepting root CA in its system trust store, so
the driver's default verification fails with "self-signed certificate in
certificate chain". We pass an explicit certifi-backed SSL context, which is
also what the Groq SDK does (and why that provider worked all along).
"""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra.stores.base import GraphStore

# Load backend/.env so NEO4J_* are present no matter which entrypoint imports
# us (server, CLI, migration script, MCP server). Same pattern as llm.py.
# load_dotenv does not override already-set vars, so tests keep control.
_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass

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


CONSTRAINTS = [
    "CREATE CONSTRAINT note_id IF NOT EXISTS FOR (n:Note) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT entity_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE",
    "CREATE CONSTRAINT mention_text IF NOT EXISTS FOR (m:Mention) REQUIRE m.text IS UNIQUE",
    "CREATE CONSTRAINT cluster_id IF NOT EXISTS FOR (c:Cluster) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT meta_id IF NOT EXISTS FOR (g:GraphMeta) REQUIRE g.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX note_status IF NOT EXISTS FOR (n:Note) ON (n.extractionStatus)",
    "CREATE INDEX entity_pagerank IF NOT EXISTS FOR (e:Entity) ON (e.pagerank)",
    "CREATE FULLTEXT INDEX noteSearch IF NOT EXISTS FOR (n:Note) ON EACH [n.title, n.content]",
]


class Neo4jStore(GraphStore):
    """Shared property-graph backend (Neo4j Aura)."""

    def __init__(self) -> None:
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

    @property
    def driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            # A bare neo4j:// scheme (not neo4j+s://) because the +s schemes
            # lock TLS config and forbid supplying our own ssl_context.
            self._driver = GraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
                ssl_context=self._ssl_context(),
            )
        return self._driver

    def _run(self, cypher: str, **params) -> list[dict[str, Any]]:
        with self.driver.session(database=self._database) as s:
            return [r.data() for r in s.run(cypher, **params)]

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    # -- lifecycle ---------------------------------------------------------

    def init_schema(self) -> None:
        for stmt in CONSTRAINTS + INDEXES:
            self._run(stmt)

    def describe(self) -> str:
        return f"neo4j:{self._uri}/{self._database or '<home>'}"

    # -- notes -------------------------------------------------------------

    def upsert_note(
        self,
        id: str,
        title: str,
        content: str,
        last_edited: str | None = None,
        mark_pending: bool = True,
    ) -> None:
        # Mirrors the SQLite CASE: caller-requested pending wins, otherwise a
        # changed last_edited re-opens the note, otherwise keep current status.
        self._run(
            """
            MERGE (n:Note {id: $id})
            ON CREATE SET n.extractionStatus = $status
            SET n.title = $title,
                n.content = $content,
                n.lastSynced = $now,
                n.extractionStatus = CASE
                    WHEN $status = 'pending' THEN 'pending'
                    WHEN n.lastEdited IS NOT NULL AND n.lastEdited <> $lastEdited THEN 'pending'
                    ELSE n.extractionStatus
                END,
                n.lastEdited = $lastEdited
            """,
            id=id, title=title, content=content, lastEdited=last_edited,
            now=_now(), status=("pending" if mark_pending else "done"),
        )

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
            })
        return out

    def get_notes(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._run(
                "MATCH (n:Note) WHERE n.extractionStatus = $status "
                "RETURN n ORDER BY n.lastEdited DESC",
                status=status,
            )
        else:
            rows = self._run("MATCH (n:Note) RETURN n ORDER BY n.lastEdited DESC")
        return self._note_rows(rows)

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        # Rank by how many terms appear in title+content, preferring notes that
        # contain ALL terms — same semantics as the SQLite backend, so callers
        # cannot tell the two apart.
        rows = self._run(
            """
            MATCH (n:Note)
            WITH n, toLower(coalesce(n.title,'') + ' ' + coalesce(n.content,'')) AS hay
            WITH n, hay, [t IN $terms WHERE hay CONTAINS t] AS hits
            WHERE size(hits) > 0
            RETURN n, size(hits) AS matched
            ORDER BY matched DESC, n.lastEdited DESC
            """,
            terms=terms,
        )
        full = [r for r in rows if r["matched"] == len(terms)]
        pool = full if full else rows
        return self._note_rows(pool[:limit])

    def get_note(self, id: str) -> dict[str, Any] | None:
        rows = self._run("MATCH (n:Note {id: $id}) RETURN n", id=id)
        got = self._note_rows(rows)
        return got[0] if got else None

    def set_note_status(self, id: str, status: str) -> None:
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid extraction status: {status!r}")
        self._run(
            "MATCH (n:Note {id: $id}) SET n.extractionStatus = $status",
            id=id, status=status,
        )

    def delete_note(self, id: str) -> None:
        self.delete_triples_for_note(id)
        self._run("MATCH (n:Note {id: $id}) DETACH DELETE n", id=id)

    # -- triples -----------------------------------------------------------

    def delete_triples_for_note(self, note_id: str) -> None:
        # Drop the asserted facts from this note, then any mention left with no
        # remaining provenance (mentions exist only to carry triples).
        self._run(
            "MATCH ()-[r:ASSERTS]->() WHERE r.sourceNoteId = $nid DELETE r",
            nid=note_id,
        )
        self._run(
            "MATCH (m:Mention)-[:EXTRACTED_FROM]->(n:Note {id: $nid}) "
            "WHERE NOT (m)-[:ASSERTS]-() DETACH DELETE m",
            nid=note_id,
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
        # One generic :ASSERTS relationship carrying the relation name. The
        # ontology-typed relationships are created in save_graph() between
        # resolved :Entity nodes; raw mentions are pre-resolution and would
        # otherwise assert typed edges between surface forms.
        self._run(
            """
            UNWIND $rows AS row
            MERGE (s:Mention {text: row.subject})
              ON CREATE SET s.type = row.subjectType
            MERGE (o:Mention {text: row.object})
              ON CREATE SET o.type = row.objectType
            CREATE (s)-[:ASSERTS {
                relation: row.relation,
                confidence: row.confidence,
                sourceQuote: row.sourceQuote,
                sourceNoteId: row.sourceNoteId,
                extractedAt: row.extractedAt
            }]->(o)
            WITH s, o, row
            MATCH (n:Note {id: row.sourceNoteId})
            MERGE (s)-[:EXTRACTED_FROM]->(n)
            MERGE (o)-[:EXTRACTED_FROM]->(n)
            """,
            rows=rows,
        )

    def get_all_triples(self) -> list[dict[str, Any]]:
        rows = self._run(
            """
            MATCH (s:Mention)-[r:ASSERTS]->(o:Mention)
            RETURN s.text AS subject_text, s.type AS subject_type,
                   r.relation AS relation,
                   o.text AS object_text, o.type AS object_type,
                   r.confidence AS confidence, r.sourceQuote AS source_quote,
                   r.sourceNoteId AS source_note_id, r.extractedAt AS extracted_at
            ORDER BY r.extractedAt DESC
            """
        )
        return rows

    # -- entity resolution -------------------------------------------------

    def replace_canonical_map(self, clusters: list[dict[str, Any]]) -> None:
        # Wholesale replacement, matching the SQLite backend: resolution is
        # recomputed every run and a partial map would corrupt the graph.
        self._run("MATCH (:Mention)-[r:RESOLVES_TO]->(:Entity) DELETE r")
        self._run("MATCH (e:Entity) DETACH DELETE e")
        if not clusters:
            return
        self._run(
            """
            UNWIND $clusters AS c
            MERGE (e:Entity {name: c.canonical_name})
              SET e.clusterId = c.cluster_id
            WITH e, c
            UNWIND c.mentions AS mention
            MERGE (m:Mention {text: mention})
            MERGE (m)-[:RESOLVES_TO]->(e)
            """,
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
            "MATCH (m:Mention)-[:RESOLVES_TO]->(e:Entity) "
            "RETURN m.text AS mention, e.name AS canonical"
        )
        return {r["mention"]: r["canonical"] for r in rows}

    def get_entity_clusters(self) -> list[dict[str, Any]]:
        rows = self._run(
            """
            MATCH (e:Entity)
            OPTIONAL MATCH (m:Mention)-[:RESOLVES_TO]->(e)
            RETURN e.clusterId AS cluster_id, e.name AS canonical_name,
                   collect(m.text) AS mentions
            """
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
                MERGE (e:Entity {name: n.id})
                SET e.label = n.label, e.type = n.type,
                    e.pagerank = n.pagerank, e.clusterId = toString(n.cluster)
                """,
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
        self._run("MATCH (:Entity)-[r]->(:Entity) WHERE type(r) <> 'IN_CLUSTER' DELETE r")

        for rel_type, rows in by_type.items():
            self._run(
                f"""
                UNWIND $rows AS row
                MATCH (s:Entity {{name: row.source}})
                MATCH (t:Entity {{name: row.target}})
                CREATE (s)-[:{rel_type} {{
                    confidence: row.confidence,
                    sourceQuote: row.sourceQuote,
                    sourceNoteId: row.sourceNoteId
                }}]->(t)
                """,
                rows=rows,
            )

        # Clusters, including any LLM summaries carried on the projection.
        clusters = (stats or {}).get("clusters") or graph.get("clusters") or []
        if clusters:
            self._run(
                """
                UNWIND $clusters AS c
                MERGE (cl:Cluster {id: c.id})
                SET cl.summary = c.summary, cl.size = c.size, cl.builtAt = $now
                WITH cl, c
                UNWIND c.members AS member
                MATCH (e:Entity {name: member})
                MERGE (e)-[:IN_CLUSTER]->(cl)
                """,
                now=_now(),
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

        # Render projection (see module docstring).
        self._run(
            """
            MERGE (g:GraphMeta {id: 1})
            SET g.builtAt = $now, g.graphJson = $graph, g.statsJson = $stats
            """,
            now=_now(), graph=json.dumps(graph), stats=json.dumps(stats),
        )

    def load_graph(self) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (g:GraphMeta {id: 1}) RETURN g.builtAt AS builtAt, "
            "g.graphJson AS graphJson, g.statsJson AS statsJson"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "built_at": r["builtAt"],
            "graph": json.loads(r["graphJson"]),
            "stats": json.loads(r["statsJson"]),
        }

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        r = self._run(
            """
            OPTIONAL MATCH (n:Note)        WITH count(n) AS notes
            OPTIONAL MATCH (p:Note) WHERE p.extractionStatus = 'pending'
              WITH notes, count(p) AS pending
            OPTIONAL MATCH ()-[t:ASSERTS]->() WITH notes, pending, count(t) AS triples
            OPTIONAL MATCH (e:Entity)      WITH notes, pending, triples, count(e) AS entities
            OPTIONAL MATCH (g:GraphMeta)   RETURN notes, pending, triples, entities,
                                                  count(g) AS meta
            """
        )[0]
        return {
            "notes_total": r["notes"],
            "notes_pending": r["pending"],
            "triples_total": r["triples"],
            "entity_clusters": r["entities"],
            "graph_cached": bool(r["meta"]),
        }
