"""
Stage 6 — Full pipeline orchestrator.

Wires together: (sync) → extract → resolve → build-graph.
Called by:
  - FastAPI   POST /pipeline/run
  - CLI       brahmastra run
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from brahmastra import db


def run_pipeline(full: bool = False) -> dict[str, Any]:
    """
    Run the full pipeline.

    full=True  → re-extract ALL notes (forces re-analysis).
    full=False → incremental: only extract notes with status='pending'.

    Returns a summary dict suitable for both the API response and CLI display.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "started_at": started_at,
        "mode": "full" if full else "incremental",
        "stages": {},
    }

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

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result
