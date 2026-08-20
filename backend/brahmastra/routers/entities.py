"""
Entities router — look up what the graph knows about a thing.

These endpoints were documented in docs/AI_AGENTS_INTEGRATION.md long before
they existed: every agent example there called /entities/search or
/entities/{id} and got a 404. The choice was to delete the examples or make
them true, and deleting them would have left the REST API with no way to ask
about an entity at all -- a strange hole in a knowledge graph, and one the MCP
server did not have.

So this mirrors the MCP tools rather than inventing a second behaviour. Both
read the CACHED graph, because entity identity only exists after resolution has
merged the spellings and the graph build has scored them; querying raw_triples
instead would answer with unresolved mentions, which is a different and worse
question.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from brahmastra import db

router = APIRouter(prefix="/entities", tags=["entities"])


def _cached_nodes() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    (nodes, edges) from the cached graph.

    503 rather than an empty list when nothing is cached: "no graph yet" and
    "no entity matched" are different answers, and returning [] for both sends
    the caller looking for a spelling mistake instead of running the pipeline.
    """
    cached = db.get_cached_graph()
    if not cached:
        raise HTTPException(
            status_code=503,
            detail="No graph cached yet. Run POST /pipeline/run first.",
        )
    graph = cached.get("graph") or {}
    return graph.get("nodes") or [], graph.get("edges") or []


@router.get("")
async def search_entities(
    q: str = "",
    type: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Entities matching a name substring and/or type, most central first.

    Ordered by PageRank so a bare `?q=` is useful on its own -- it answers
    "what is this graph mostly about?", which is usually the first thing an
    agent wants to know.
    """
    nodes, _ = _cached_nodes()
    results = [
        n for n in nodes
        if (not q or q.lower() in (n.get("label") or "").lower())
        and (not type or (n.get("type") or "").lower() == type.lower())
    ]
    results.sort(key=lambda n: n.get("pagerank", 0), reverse=True)
    return results[:limit]


@router.get("/{name}")
async def get_entity(name: str) -> dict[str, Any]:
    """
    One entity with everything the graph knows about it.

    Matched exactly first, then by substring -- the same two-step the MCP tool
    uses, so an agent gets the same entity whichever transport it came in on.
    """
    nodes, edges = _cached_nodes()
    lowered = name.lower()
    node = next((n for n in nodes if (n.get("label") or "").lower() == lowered), None)
    if node is None:
        node = next((n for n in nodes if lowered in (n.get("label") or "").lower()), None)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Entity {name!r} not in the graph")

    label = node["label"]
    # Direction is kept rather than flattened: "Sarah reports_to Mei" and "Mei
    # reports_to Sarah" are different facts, and a single merged list would
    # make them indistinguishable to the caller.
    outgoing = [e for e in edges if e.get("source") == label]
    incoming = [e for e in edges if e.get("target") == label]
    return {
        **node,
        "outgoing": outgoing,
        "incoming": incoming,
        "degree": len(outgoing) + len(incoming),
    }
