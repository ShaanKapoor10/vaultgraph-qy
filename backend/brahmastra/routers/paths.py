"""
Paths router — how two entities are connected.

GET /paths?source=Sarah&target=PostgreSQL&max_hops=5
  → {found, hops, path: [{from, relation, to, direction, walk_from, walk_to, note_id}]}

Answers "how are these two things related?", which the graph could not express
before: the serialised-blob design had no traversal, only a full edge scan.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from brahmastra import db

router = APIRouter(prefix="/paths", tags=["paths"])


@router.get("")
async def find_path(
    source: str = Query(..., description="Entity to start from"),
    target: str = Query(..., description="Entity to reach"),
    max_hops: int = Query(5, ge=1, le=10),
) -> dict[str, Any]:
    hops = db.find_path(source, target, max_hops=max_hops)
    return {
        "source": source,
        "target": target,
        "found": bool(hops),
        "hops": len(hops),
        # Each hop reads as a true fact; direction says whether the walk
        # followed the relationship or went against it.
        "path": hops,
    }
