"""
Stage 8 — MCP server (stdio transport).

Exposes the Brahmastra knowledge graph as an MCP server that Claude Code
(and any other MCP client) can connect to.

Tools exposed:
  1. brahmastra_run_pipeline        — run extract → resolve → build-graph
  2. brahmastra_get_graph_stats     — db stats + graph summary
  3. brahmastra_search_entities     — find entities by name / type
  4. brahmastra_get_entity_details  — neighbourhood + relations for one entity
  5. brahmastra_get_contradictions  — list detected contradictions
  6. brahmastra_add_note            — upsert a note and mark it pending

Run:
  cd backend
  source .venv/bin/activate
  python -m brahmastra.mcp_server

Register in Claude Code's mcp.json (typically ~/.config/claude/mcp.json):
  {
    "brahmastra": {
      "command": "/path/to/backend/.venv/bin/python",
      "args": ["-m", "brahmastra.mcp_server"],
      "cwd": "/path/to/backend"
    }
  }
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Load .env so keys are available even when invoked via MCP client
_HERE = Path(__file__).resolve().parent.parent
_ENV = _HERE / ".env"
if _ENV.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types as mcp_types
except ImportError as e:
    print(
        f"[brahmastra-mcp] mcp package not installed: {e}\n"
        "Run: uv pip install mcp",
        file=sys.stderr,
    )
    sys.exit(1)

from brahmastra import db

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

server = Server("brahmastra")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    mcp_types.Tool(
        name="brahmastra_run_pipeline",
        description=(
            "Run the Brahmastra concept graph pipeline: "
            "extract triples from pending notes → resolve entities → build graph. "
            "Use mode='full' to re-process all notes from scratch."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["incremental", "full"],
                    "default": "incremental",
                    "description": "incremental=pending notes only, full=all notes",
                }
            },
        },
    ),
    mcp_types.Tool(
        name="brahmastra_get_graph_stats",
        description=(
            "Return current graph statistics: node count, edge count, "
            "top entities by PageRank, concept clusters, contradiction count, "
            "and link prediction count."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    mcp_types.Tool(
        name="brahmastra_search_entities",
        description=(
            "Search the graph for entities matching a name substring and/or type. "
            "Returns a ranked list of matching nodes with their PageRank score."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match against entity name (case-insensitive).",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Filter by entity type: person, project, concept, tool, organisation, event, date, unknown.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of results.",
                },
            },
        },
    ),
    mcp_types.Tool(
        name="brahmastra_get_entity_details",
        description=(
            "Return full details for a named entity: its canonical name, "
            "all aliases (resolved mentions), PageRank, cluster, "
            "and all relations (outgoing and incoming) with source quotes."
        ),
        inputSchema={
            "type": "object",
            "required": ["entity_name"],
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "Canonical entity name (case-insensitive match).",
                }
            },
        },
    ),
    mcp_types.Tool(
        name="brahmastra_get_contradictions",
        description=(
            "Return all detected contradictions: cases where the same entity "
            "has conflicting values for a functional relation (e.g. reports_to, "
            "scheduled_for) across different notes."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    mcp_types.Tool(
        name="brahmastra_add_note",
        description=(
            "Add or update a note in the database. "
            "Marks it as pending so the next pipeline run will extract triples from it."
        ),
        inputSchema={
            "type": "object",
            "required": ["title", "content"],
            "properties": {
                "title": {"type": "string", "description": "Note title."},
                "content": {"type": "string", "description": "Note body text."},
                "note_id": {
                    "type": "string",
                    "description": "Optional stable ID. Auto-generated if omitted.",
                },
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# list_tools handler
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return TOOLS


# ---------------------------------------------------------------------------
# call_tool handler
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    db.init_db()

    # ------------------------------------------------------------------
    # 1. brahmastra_run_pipeline
    # ------------------------------------------------------------------
    if name == "brahmastra_run_pipeline":
        from brahmastra.pipeline import run_pipeline
        mode = arguments.get("mode", "incremental")
        result = run_pipeline(full=(mode == "full"))
        return [mcp_types.TextContent(type="text", text=json.dumps(result, indent=2))]

    # ------------------------------------------------------------------
    # 2. brahmastra_get_graph_stats
    # ------------------------------------------------------------------
    if name == "brahmastra_get_graph_stats":
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
        return [mcp_types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    # ------------------------------------------------------------------
    # 3. brahmastra_search_entities
    # ------------------------------------------------------------------
    if name == "brahmastra_search_entities":
        query = (arguments.get("query") or "").lower()
        etype = (arguments.get("entity_type") or "").lower()
        limit = int(arguments.get("limit", 10))

        cached = db.get_cached_graph()
        if not cached:
            return [mcp_types.TextContent(type="text", text="No graph cached. Run brahmastra_run_pipeline first.")]

        nodes = cached["graph"].get("nodes", [])
        results = []
        for n in nodes:
            label_lower = n["label"].lower()
            if query and query not in label_lower:
                continue
            if etype and n.get("type", "").lower() != etype:
                continue
            results.append(n)

        results.sort(key=lambda x: x.get("pagerank", 0), reverse=True)
        return [mcp_types.TextContent(type="text", text=json.dumps(results[:limit], indent=2))]

    # ------------------------------------------------------------------
    # 4. brahmastra_get_entity_details
    # ------------------------------------------------------------------
    if name == "brahmastra_get_entity_details":
        entity_name = (arguments.get("entity_name") or "").strip().lower()
        if not entity_name:
            return [mcp_types.TextContent(type="text", text="entity_name is required.")]

        cached = db.get_cached_graph()
        if not cached:
            return [mcp_types.TextContent(type="text", text="No graph cached. Run brahmastra_run_pipeline first.")]

        # Find matching node (case-insensitive)
        nodes = cached["graph"].get("nodes", [])
        node = next((n for n in nodes if n["label"].lower() == entity_name), None)
        if not node:
            # Fuzzy: substring match
            node = next((n for n in nodes if entity_name in n["label"].lower()), None)
        if not node:
            return [mcp_types.TextContent(type="text", text=f"Entity '{entity_name}' not found in graph.")]

        canonical = node["label"]

        # Get aliases from entity clusters
        clusters = db.get_entity_clusters()
        aliases: list[str] = []
        for c in clusters:
            if c["canonical_name"].lower() == canonical.lower():
                aliases = [m for m in c["mentions"] if m != canonical]
                break

        # Collect relations
        edges = cached["graph"].get("edges", [])
        outgoing = [e for e in edges if e["source"] == canonical]
        incoming = [e for e in edges if e["target"] == canonical]

        detail = {
            "entity": canonical,
            "type": node.get("type"),
            "pagerank": node.get("pagerank"),
            "cluster": node.get("cluster"),
            "aliases": aliases,
            "outgoing_relations": outgoing,
            "incoming_relations": incoming,
        }
        return [mcp_types.TextContent(type="text", text=json.dumps(detail, indent=2))]

    # ------------------------------------------------------------------
    # 5. brahmastra_get_contradictions
    # ------------------------------------------------------------------
    if name == "brahmastra_get_contradictions":
        cached = db.get_cached_graph()
        if not cached:
            return [mcp_types.TextContent(type="text", text="No graph cached. Run brahmastra_run_pipeline first.")]
        contradictions = cached["stats"].get("contradictions", [])
        return [mcp_types.TextContent(type="text", text=json.dumps(contradictions, indent=2))]

    # ------------------------------------------------------------------
    # 6. brahmastra_add_note
    # ------------------------------------------------------------------
    if name == "brahmastra_add_note":
        import uuid
        title = arguments.get("title", "Untitled")
        content = arguments.get("content", "")
        note_id = arguments.get("note_id") or str(uuid.uuid4())[:8]
        db.upsert_note(note_id, title, content, mark_pending=True)
        return [mcp_types.TextContent(
            type="text",
            text=json.dumps({"status": "added", "note_id": note_id, "title": title}, indent=2),
        )]

    return [mcp_types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
