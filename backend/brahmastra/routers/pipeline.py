"""
Pipeline router — trigger pipeline stages via HTTP.
Actual implementation lives in the stage modules (extraction, entity_resolution, etc.)
and will be wired in as each step is built.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from fastapi import APIRouter, BackgroundTasks, HTTPException

from brahmastra import db

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# In-memory status for the last /pipeline/run, so the frontend can poll
# instead of holding a single HTTP request open for the whole run (which,
# for a multi-minute run, gets killed by the Next.js dev proxy with
# "socket hang up" / ECONNRESET well before the backend finishes).
_STATUS: dict[str, Any] = {"state": "idle"}


def _run_pipeline_background(full: bool) -> None:
    from brahmastra.pipeline import run_pipeline

    _STATUS["state"] = "running"
    _STATUS["started_at"] = datetime.now(timezone.utc).isoformat()
    _STATUS.pop("result", None)
    _STATUS.pop("error", None)
    try:
        result = run_pipeline(full=full)
        _STATUS["result"] = result
        _STATUS["state"] = "skipped" if result.get("skipped") else "done"
    except Exception as exc:
        _STATUS["error"] = str(exc)
        _STATUS["state"] = "error"
    finally:
        _STATUS["finished_at"] = datetime.now(timezone.utc).isoformat()


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


@router.post("/cluster-summaries")
async def cluster_summaries() -> dict[str, Any]:
    """
    Summarise each Louvain concept cluster via the local LLM (Stage 5b).
    Reads the cached graph, merges a one-line summary into each cluster,
    and re-caches. Safe to call standalone after a graph build.
    """
    try:
        from brahmastra.cluster_summary import run_cluster_summaries
        return run_cluster_summaries()
    except ImportError:
        raise HTTPException(status_code=501, detail="Cluster summaries not yet implemented")


@router.post("/run")
async def run_full_pipeline(
    background_tasks: BackgroundTasks, mode: Literal["incremental", "full"] = "incremental"
) -> dict[str, Any]:
    """
    Kick off the full pipeline: (sync) -> extract -> resolve -> build-graph.
    mode=full forces re-extraction of all notes.

    Runs in the background and returns immediately — a full run (Ollama
    extraction + embeddings + graph build) can take minutes, which is longer
    than the Next.js dev proxy will hold a single request open for. Poll
    GET /pipeline/status for progress/result.
    """
    if _STATUS.get("state") == "running":
        return {"state": "running", "started_at": _STATUS.get("started_at")}
    try:
        from brahmastra.pipeline import run_pipeline  # noqa: F401  (import check)
    except ImportError:
        raise HTTPException(status_code=501, detail="Full pipeline not yet implemented (Step 6)")

    background_tasks.add_task(_run_pipeline_background, mode == "full")
    return {"state": "started"}


@router.get("/status")
async def pipeline_status() -> dict[str, Any]:
    """Poll the state of the most recently started /pipeline/run."""
    return _STATUS
