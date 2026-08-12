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
def brahmastra_search_notes(query: str, limit: int = 10) -> str:
    """
    Search the FULL TEXT of stored notes (title + content), not just the entity graph.

    Use this to recall what was written about a topic — decisions, bug fixes, changes —
    when entity search comes up empty. Entity search only matches entity names; this
    matches the actual prose of every note, so it surfaces content even when the LLM
    extraction produced few triples for it.
    """
    db.init_db()
    notes = db.search_notes(query, limit=limit)
    if not notes:
        return f"No notes matching '{query}'."
    out = []
    for n in notes:
        content = n.get("content", "")
        snippet = content if len(content) <= 400 else content[:400] + "…"
        out.append({
            "id": n["id"],
            "title": n["title"],
            "status": n.get("extraction_status"),
            "snippet": snippet,
        })
    return json.dumps(out, indent=2, ensure_ascii=False)


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


# ---------------------------------------------------------------------------
# Workspaces — several independent graphs
# ---------------------------------------------------------------------------

@mcp.tool()
def brahmastra_list_workspaces() -> str:
    """
    List the knowledge graphs available, and which one is currently active.

    Each workspace is a fully separate graph: notes, entities and relations in
    one are invisible to the others. Use this before adding a note to confirm
    which graph it will land in.
    """
    db.init_db()
    return json.dumps({
        "current": db.workspace(),
        "workspaces": db.list_workspaces(),
    }, indent=2)


@mcp.tool()
def brahmastra_create_workspace(
    name: str,
    id: str = "",
    description: str = "",
    notion_database_id: str = "",
) -> str:
    """
    Create a new, empty knowledge graph.

    Use for a separate area of knowledge that should not mix with the current
    one — work versus personal, or a single project. The new workspace starts
    empty and inherits nothing.

    name: display name, e.g. "Office"
    id:   optional slug; derived from name when omitted
    description: what this graph is for
    notion_database_id: this workspace's own Notion source (optional)
    """
    from brahmastra.workspace import InvalidWorkspaceId, slugify
    db.init_db()
    try:
        created = db.create_workspace(
            id=id or slugify(name),
            name=name,
            description=description,
            notion_database_id=notion_database_id or None,
        )
    except InvalidWorkspaceId as e:
        return json.dumps({"error": f"invalid workspace id: {e}"}, indent=2)
    except NotImplementedError as e:
        return json.dumps({"error": str(e)}, indent=2)
    return json.dumps({
        "created": created,
        # Creating does not switch: an agent should not silently redirect
        # where the user's next note gets written.
        "note": (
            f"Created but NOT switched to. The active workspace is still "
            f"{db.workspace()!r}. Set BRAHMASTRA_WORKSPACE={created['id']} to "
            f"make it active, or pass the workspace explicitly."
        ),
    }, indent=2)


@mcp.tool()
def brahmastra_search_all_workspaces(query: str, limit: int = 10) -> str:
    """
    Search the full text of notes across EVERY workspace.

    Normal search only sees the active graph. Use this when you do not know
    which workspace holds something. Each result reports the workspace it came
    from.
    """
    db.init_db()
    try:
        hits = db.search_notes_across(query, None, limit)
    except NotImplementedError as e:
        return json.dumps({"error": str(e)}, indent=2)
    return json.dumps({
        "query": query,
        "results": [
            {
                "workspace": h.get("workspace_id"),
                "id": h.get("id"),
                "title": h.get("title"),
                "snippet": (h.get("content") or "")[:280],
            }
            for h in hits
        ],
    }, indent=2)
