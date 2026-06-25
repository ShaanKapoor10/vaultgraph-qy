"""
Graph router — serve the cached concept graph to the frontend.
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter
from pydantic import BaseModel

from brahmastra import db

router = APIRouter(prefix="/graph", tags=["graph"])


class TripleIn(BaseModel):
    subject_text: str
    subject_type: str = "unknown"
    relation: str
    object_text: str
    object_type: str = "unknown"
    confidence: float = 1.0
    source_quote: str | None = None
    source_note_id: str | None = None


class TriplesPayload(BaseModel):
    triples: list[TripleIn]


@router.get("")
async def get_graph() -> dict[str, Any]:
    """
    Return the most recently cached graph.
    If the cache is empty, returns an empty graph structure so the frontend
    knows to fall back to seed data.
    """
    cached = db.get_cached_graph()
    if cached:
        return cached

    # No graph cached yet — return empty scaffold
    return {
        "built_at": None,
        "graph": {
            "nodes": [],
            "edges": [],
        },
        "stats": {
            "nodes": 0,
            "edges": 0,
            "central_entities": [],
            "concept_clusters": [],
            "contradictions": [],
            "predicted_links": [],
            "entity_clusters": [],
        },
    }


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    return db.get_db_stats()


@router.get("/triples")
async def get_triples() -> list[dict[str, Any]]:
    return db.get_all_triples()


@router.post("/triples", status_code=201)
async def add_triples(body: TriplesPayload) -> dict[str, Any]:
    """Accept triples from the frontend and insert them directly into the DB."""
    triples = [t.model_dump() for t in body.triples]
    db.insert_triples(triples)
    return {"inserted": len(triples)}


@router.get("/clusters")
async def get_entity_clusters() -> list[dict[str, Any]]:
    return db.get_entity_clusters()
