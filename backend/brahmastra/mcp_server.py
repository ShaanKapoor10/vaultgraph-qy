"""
MCP server — exposes the Brahmastra knowledge graph to Claude Code.

Run:
  cd backend && python -m brahmastra.mcp_server

Tools exposed:
  brahmastra_run_pipeline        — run extract → resolve → build-graph
  brahmastra_get_graph_stats     — db stats + graph summary
  brahmastra_search_entities     — find entities by name / type
  brahmastra_get_entity_details  — neighbourhood + relations for one entity
  brahmastra_get_contradictions  — list detected contradictions
  brahmastra_add_note            — upsert a note and mark it pending
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# Load .env so keys are available when invoked via MCP client
_HERE = Path(__file__).resolve().parent.parent
_ENV = _HERE / ".env"
if _ENV.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV)

from mcp.server.fastmcp import FastMCP
from brahmastra import db

mcp = FastMCP("brahmastra")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def brahmastra_run_pipeline(mode: str = "incremental") -> str:
    """
    Run the Brahmastra concept graph pipeline.
    mode: 'incremental' (pending notes only) or 'full' (all notes).
    """
    db.init_db()
    from brahmastra.pipeline import run_pipeline
    result = run_pipeline(full=(mode == "full"))
    return json.dumps(result, indent=2)


@mcp.tool()
def brahmastra_get_graph_stats() -> str:
    """Return current graph statistics: node count, edge count, top entities, clusters, etc."""
    db.init_db()
    cached = db.get_cached_graph()
    stats = db.get_db_stats()
    payload: dict[str, Any] = {"db": stats}
    if cached:
        payload["built_at"] = cached["built_at"]
        payload["graph"] = {
            "nodes": cached["stats"].get("nodes"),
            "edges": cached["stats"].get("edges"),
            "concept_clusters": len(cached["stats"].get("concept_clusters", [])),
            "contradictions": len(cached["stats"].get("contradictions", [])),
            "predicted_links": len(cached["stats"].get("predicted_links", [])),
            "top_entities": cached["stats"].get("central_entities", [])[:5],
        }
    else:
        payload["graph"] = None
    return json.dumps(payload, indent=2)


@mcp.tool()
def brahmastra_search_entities(query: str = "", entity_type: str = "", limit: int = 10) -> str:
    """
    Search the graph for entities matching a name substring and/or type.
    entity_type options: person, project, concept, tool, organisation, event, date, unknown.
    """
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."

    nodes = cached["graph"].get("nodes", [])
    results = []
    for n in nodes:
        if query and query.lower() not in n["label"].lower():
            continue
        if entity_type and n.get("type", "").lower() != entity_type.lower():
            continue
        results.append(n)

    results.sort(key=lambda x: x.get("pagerank", 0), reverse=True)
    return json.dumps(results[:limit], indent=2)


@mcp.tool()
def brahmastra_get_entity_details(entity_name: str) -> str:
    """Return full details for a named entity: aliases, PageRank, cluster, and all relations."""
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."

    nodes = cached["graph"].get("nodes", [])
    node = next((n for n in nodes if n["label"].lower() == entity_name.lower()), None)
    if not node:
        node = next((n for n in nodes if entity_name.lower() in n["label"].lower()), None)
    if not node:
        return f"Entity '{entity_name}' not found in graph."

    canonical = node["label"]
    clusters = db.get_entity_clusters()
    aliases: list[str] = []
    for c in clusters:
        if c["canonical_name"].lower() == canonical.lower():
            aliases = [m for m in c["mentions"] if m != canonical]
            break

    edges = cached["graph"].get("edges", [])
    detail = {
        "entity": canonical,
        "type": node.get("type"),
        "pagerank": node.get("pagerank"),
        "cluster": node.get("cluster"),
        "aliases": aliases,
        "outgoing_relations": [e for e in edges if e["source"] == canonical],
        "incoming_relations": [e for e in edges if e["target"] == canonical],
    }
    return json.dumps(detail, indent=2)


@mcp.tool()
def brahmastra_get_contradictions() -> str:
    """Return all detected contradictions in the knowledge graph."""
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."
    return json.dumps(cached["stats"].get("contradictions", []), indent=2)


@mcp.tool()
def brahmastra_add_note(title: str, content: str, note_id: str = "") -> str:
    """Add or update a note. Marks it pending so the next pipeline run extracts triples from it."""
    db.init_db()
    nid = note_id or str(uuid.uuid4())[:8]
    db.upsert_note(nid, title, content, mark_pending=True)
    return json.dumps({"status": "added", "note_id": nid, "title": title}, indent=2)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
