"""
Pipeline router — trigger pipeline stages via HTTP.
Actual implementation lives in the stage modules (extraction, entity_resolution, etc.)
and will be wired in as each step is built.
"""

from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, BackgroundTasks, HTTPException

from brahmastra import db

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/sync")
async def sync_notion() -> dict[str, Any]:
    """
    Trigger Notion sync (Step 7).
    Returns 501 until notion sync module is wired.
    """
    try:
        from brahmastra.sync import run_sync
        return run_sync()
    except ImportError:
        raise HTTPException(status_code=501, detail="Notion sync not yet implemented (Step 7)")


@router.post("/extract")
async def extract_pending() -> dict[str, Any]:
    """
    Extract pending notes (Step 3).
    """
    try:
        from brahmastra.extraction import run_extraction
        return run_extraction()
    except ImportError:
        raise HTTPException(status_code=501, detail="Extraction not yet implemented (Step 3)")


@router.post("/resolve")
async def resolve_entities() -> dict[str, Any]:
    """
    Run entity resolution over all raw triples (Step 4).
    """
    try:
        from brahmastra.entity_resolution import run_resolution
        return run_resolution()
    except ImportError:
        raise HTTPException(status_code=501, detail="Entity resolution not yet implemented (Step 4)")


@router.post("/build-graph")
async def build_graph() -> dict[str, Any]:
    """
    Build / rebuild concept graph from canonical triples (Step 5).
    """
    try:
        from brahmastra.concept_graph import run_build_graph
        return run_build_graph()
    except ImportError:
        raise HTTPException(status_code=501, detail="Graph builder not yet implemented (Step 5)")


@router.post("/run")
async def run_full_pipeline(mode: Literal["incremental", "full"] = "incremental") -> dict[str, Any]:
    """
    Run full pipeline: (sync) -> extract -> resolve -> build-graph.
    mode=full forces re-extraction of all notes.
    Step 6 wires the full orchestrator here.
    """
    try:
        from brahmastra.pipeline import run_pipeline
        return run_pipeline(full=mode == "full")
    except ImportError:
        raise HTTPException(status_code=501, detail="Full pipeline not yet implemented (Step 6)")
