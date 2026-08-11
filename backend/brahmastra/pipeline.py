"""
Stage 6 — Full pipeline orchestrator.

Wires together: (sync) → extract → resolve → build-graph.
Called by:
  - FastAPI   POST /pipeline/run
  - CLI       brahmastra run
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra import db

# Cross-process lock so the backend "run pipeline" button and the live_sync
# watcher can't run the pipeline simultaneously (concurrent SQLite writers
# otherwise cause "database is locked" 500s).
_LOCK = Path(__file__).resolve().parent.parent / "data" / ".pipeline.lock"
_LOCK_STALE_SECS = 900  # steal a lock older than 15 min (crashed run)


def _acquire_lock() -> bool:
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - _LOCK.stat().st_mtime > _LOCK_STALE_SECS:
                _LOCK.unlink()
                return _acquire_lock()
        except FileNotFoundError:
            return _acquire_lock()
        return False


def _release_lock() -> None:
    try:
        _LOCK.unlink()
    except FileNotFoundError:
        pass


def run_pipeline(full: bool = False) -> dict[str, Any]:
    """
    Run the full pipeline.

    full=True  → re-extract ALL notes (forces re-analysis).
    full=False → incremental: only extract notes with status='pending'.

    Returns a summary dict suitable for both the API response and CLI display.
    If another run holds the lock, returns immediately with {"skipped": ...}.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "started_at": started_at,
        "mode": "full" if full else "incremental",
        "stages": {},
    }

    if not _acquire_lock():
        result["skipped"] = "another pipeline run is already in progress"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

    try:
        return _run_pipeline_locked(full, result)
    finally:
        _release_lock()


def _run_pipeline_locked(full: bool, result: dict[str, Any]) -> dict[str, Any]:
    # ---------------------------------------------------------------
    # Stage 0: Notion sync (only when token is configured)
    # ---------------------------------------------------------------
    if os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID"):
        try:
            from brahmastra.sync import run_sync
            sync_result = run_sync()
            result["stages"]["sync"] = sync_result
        except Exception as exc:
            result["stages"]["sync"] = {"error": str(exc)}
    else:
        result["stages"]["sync"] = {"skipped": "NOTION_TOKEN / NOTION_DATABASE_ID not set"}

    # ---------------------------------------------------------------
    # Stage 1: extract
    # ---------------------------------------------------------------
    from brahmastra.extraction import run_extraction

    extract_result = run_extraction(full=full)
    result["stages"]["extract"] = extract_result

    # ---------------------------------------------------------------
    # Stage: resolve
    # ---------------------------------------------------------------
    from brahmastra.entity_resolution import run_resolution

    resolve_result = run_resolution()
    # Don't embed the full cluster list in the pipeline response — too large.
    result["stages"]["resolve"] = {
        "clusters": resolve_result["clusters"],
        "mentions": resolve_result["mentions"],
        "merge_edges": resolve_result["merge_edges"],
        "embedding_used": resolve_result["embedding_used"],
    }

    # ---------------------------------------------------------------
    # Stage: build-graph
    # ---------------------------------------------------------------
    from brahmastra.concept_graph import run_build_graph

    graph_result = run_build_graph()
    result["stages"]["graph"] = {
        "nodes": graph_result["stats"]["nodes"],
        "edges": graph_result["stats"]["edges"],
        "clusters": len(graph_result["stats"]["concept_clusters"]),
        "contradictions": len(graph_result["stats"]["contradictions"]),
        "predicted_links": len(graph_result["stats"]["predicted_links"]),
        "built_at": graph_result["built_at"],
    }

    # ---------------------------------------------------------------
    # Stage: cluster summaries (label each Louvain cluster via local LLM).
    # Runs after build-graph and re-caches the graph with summaries merged in.
    # Fails soft — if Ollama is down, clusters just keep empty summaries.
    # ---------------------------------------------------------------
    try:
        from brahmastra.cluster_summary import run_cluster_summaries
        result["stages"]["cluster_summaries"] = run_cluster_summaries()
    except Exception as exc:
        result["stages"]["cluster_summaries"] = {"error": str(exc)}

    # ---------------------------------------------------------------
    # Stage: Notion write-back (push insights BACK into Notion pages).
    # Only runs when Notion is connected. This closes the bidirectional
    # loop: Notion → graph → insights written back into Notion.
    # ---------------------------------------------------------------
    if os.environ.get("NOTION_TOKEN"):
        try:
            from brahmastra.notion_writeback import push_insights
            wb = push_insights()
            result["stages"]["notion_writeback"] = {
                "pushed": wb["pushed"],
                "skipped": wb["skipped"],
            }
        except Exception as exc:
            result["stages"]["notion_writeback"] = {"error": str(exc)}

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result
