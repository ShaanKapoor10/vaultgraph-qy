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

from brahmastra.env import load_env

load_env()

from mcp.server.fastmcp import FastMCP
from brahmastra import db

# Sent to every client in the MCP `initialize` response, so any agent that
# connects — Claude Code, Claude Desktop, Cursor, anything speaking MCP — knows
# this memory exists without being told again per session.
#
# This was empty for a long time, and that was the whole reason the tools went
# unused. They were present and reachable, but nothing said what they were FOR,
# so an agent scanning its toolset for "what solves this problem" saw
# "search the graph for entities matching a name substring" and moved on. The
# tools were never the missing piece; the sentence explaining when to reach for
# them was.
#
# Written as an affordance, not a protocol. An agent that is told it MUST record
# everything learns to treat it as ceremony and skips it under pressure; one
# that knows what the thing is good for reaches for it when it actually helps.
INSTRUCTIONS = """Brahmastra is a persistent knowledge graph you can use as MEMORY. It outlives
this session, this machine and this client, and it is separate from whatever
project you are working on - treat it as something you already know, not as the
code under your hands. Other agents and earlier sessions wrote into it; what you
write is read by whoever comes next.

WHEN TO REACH FOR IT

- Starting on unfamiliar or returning work. brahmastra_search_notes tells you
  what was already decided, tried, or found broken - including by sessions you
  had no part in. This is the highest-value moment and the easiest to miss,
  because nothing about a fresh task announces that memory exists.
- Something surprises you, or you are about to debug something odd. Check
  whether it is already known before spending an hour re-deriving it; past
  failures are stored together with their causes.
- You learn something durable. brahmastra_add_note keeps it, and the note is
  searchable the moment the call returns.

WHICH TOOL

  search_notes           Full text of every note. Recall what was WRITTEN about
                         a topic. START HERE - it is the one you usually want.
  search_entities        Entity NAMES only. Use when you know what a thing is
                         called and want its connections. Thin results mean the
                         NAME is absent, never that the knowledge is - fall back
                         to search_notes before concluding nothing is stored.
  get_entity_details     One entity with its relations, aliases and centrality.
  get_graph_stats        What this memory contains. Good first orientation: the
                         top entities summarise what it is actually about.
  get_contradictions     Facts recorded once and contradicted later. Worth a
                         look before trusting a single recalled fact about who
                         reports to whom, what status something has, or where
                         something lives.
  add_note               Remember something.
  run_pipeline           Rebuild the graph from all notes. Rarely needed, since
                         adding a note already extracts it; use after a bulk
                         import or an ontology change.
  list_workspaces        Which graphs exist, and which one you are writing to.
  create_workspace       A new, fully separate graph.
  search_all_workspaces  Explicit cross-workspace search; each hit says which
                         workspace it came from.

WRITING A NOTE THAT IS WORTH RECALLING

Notes are parsed into (subject, relation, object) triples, so plain
entity-rich sentences extract well and pronouns do not. Write "The file llm.py
raises LLMQuotaExhausted when the daily quota is spent", not "it throws when
that runs out". Name files, functions, services and people explicitly.

Record the WHY above all - the root cause, the option you rejected and what
ruled it out, the constraint discovered the hard way. What changed is usually
recoverable later from the code and the git history; why it changed is not, and
that is the part nobody can reconstruct.

WORKSPACES

Each workspace is a completely separate graph - notes and entities in one are
invisible to the others, and the same name may mean different things in each.
You write to the active one; list_workspaces says which that is. Cross-workspace
search is deliberately explicit, never implicit.

COST

Recall is one call and regularly saves an hour. Storing is one call and a few
seconds. Neither needs a follow-up step."""

mcp = FastMCP("brahmastra", instructions=INSTRUCTIONS)


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
    """
    See what this memory contains before relying on it — or on finding it empty.

    Node and edge counts, top entities by PageRank, cluster count. Useful as a
    first orientation on a returning project: the top entities are a fair
    summary of what the graph is actually about.
    """
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
    Find a NAMED thing in the graph — a person, file, service, project.

    Matches entity names only, so use it when you know what something is called
    and want its connections. To recall what was WRITTEN about a subject, use
    brahmastra_search_notes instead: an empty result here means the name is
    absent, never that the knowledge is.

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
    RECALL what is already known about a topic. Start here on unfamiliar work.

    Searches the full text of every stored note, so it finds decisions, root
    causes, rejected options and past breakages — including from sessions you
    had no part in. On a backend that supports it this is hybrid search, so a
    note phrased differently from your query still surfaces.

    Prefer this over brahmastra_search_entities, which matches entity NAMES only
    and comes up thin whenever the name happens to differ from your wording.
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
    """
    Facts that conflict — where something recorded once was later contradicted.

    Worth checking before trusting a single recalled fact about a functional
    relation (who reports to whom, what status something has, where it lives):
    the graph may hold both the old answer and the new one.
    """
    db.init_db()
    cached = db.get_cached_graph()
    if not cached:
        return "No graph cached. Run brahmastra_run_pipeline first."
    return json.dumps(cached["stats"].get("contradictions", []), indent=2)


@mcp.tool()
def brahmastra_add_note(
    title: str, content: str, note_id: str = "", publish: bool = False
) -> str:
    """
    REMEMBER something durable — a root cause, a decision and its reasoning,
    a constraint found the hard way.

    Write plain entity-rich sentences ("The file llm.py raises
    LLMQuotaExhausted", not "it throws when the quota runs out"); that is what
    extracts into a useful graph. Record the WHY above all: what changed is
    usually recoverable from the code later, and why it changed is not.

    The note is searchable when this returns — extraction runs here rather than
    waiting for a pipeline. That matters more than it sounds: while storing took
    a call PLUS a separate pipeline run, the second step was routinely skipped
    and the note sat `pending`, which means invisible to every search and to
    every other session.

    publish=True also gives the note a page in the workspace's Notion database,
    created on the next write-back. Use it for prose a human will want to
    re-read — decisions, design records. Leave it off for working memory such
    as session checkpoints, which belong in the graph, not in Notion.
    """
    db.init_db()
    nid = note_id or str(uuid.uuid4())[:8]
    db.upsert_note(nid, title, content, mark_pending=True,
                   publish=publish or None, source="mcp")

    # Extract immediately, and treat failure as a delay rather than an error.
    # A rate limit or a dead provider leaves the note `pending`, which is
    # exactly the old behaviour: the next pipeline run picks it up. So this can
    # only improve on what happened before, never lose the note.
    extracted: dict[str, Any] = {"extraction": "pending"}
    try:
        from brahmastra.extraction import extract_note
        result = extract_note(db.get_note(nid))
        if result.get("error"):
            extracted = {"extraction": "pending", "reason": str(result["error"])[:200]}
        else:
            extracted = {"extraction": "done", "triples": result.get("triples_added", 0)}
    except Exception as e:                       # noqa: BLE001
        extracted = {"extraction": "pending", "reason": f"{type(e).__name__}: {e}"[:200]}

    return json.dumps({"status": "added", "note_id": nid, "title": title,
                       "publish": bool(publish), **extracted}, indent=2)


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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
#
# MUST be the last thing in this file. mcp.run() blocks forever, so any @mcp.tool
# defined below it is never registered — which is exactly what happened: the
# three workspace tools sat after this block and were invisible to every client,
# while `from brahmastra.mcp_server import mcp` listed all ten, because an
# import skips __main__ and runs the whole module. That gap between "the code
# has the tool" and "the server serves it" cost a long hunt.

if __name__ == "__main__":
    mcp.run(transport="stdio")
