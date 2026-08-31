"""
Stage 6 — Full pipeline orchestrator.

Wires together: (sync) → extract → resolve → build-graph.
Called by:
  - FastAPI   POST /pipeline/run
  - CLI       brahmastra run
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra import db

# Load backend/.env HERE, not only as a side effect of importing a stage.
#
# Every stage module (llm, extraction, notion_writeback, ...) loads it on
# import, but the pipeline imports those lazily, inside the run. Stage 0 reads
# NOTION_TOKEN *before* any of them has been imported, so a run could skip the
# Notion pull for "NOTION_TOKEN not set" and then — after the extract stage
# imported llm and pulled .env in — happily push pages in the write-back stage
# of the SAME run. Observed exactly that: sync skipped, writeback pushed 3.
from brahmastra.env import data_dir, load_env

load_env()

# Cross-process lock so the backend "run pipeline" button and the live_sync
# watcher can't run the pipeline simultaneously (concurrent SQLite writers
# otherwise cause "database is locked" 500s).
#
# It lives under env.data_dir(), NOT beside the package. The package directory
# is root-owned inside the container while the process runs unprivileged, so
# the old location was unwritable there -- and since the lock is taken before
# any work, that made every containerised run fail instantly with
# "[Errno 13] Permission denied: '/app/data'". The dashboard's run button and
# the scheduler were both dead on arrival while looking perfectly healthy.
_LOCK_STALE_SECS = 900  # steal a lock older than 15 min (crashed run)


def _lock_path() -> Path:
    """
    One lock per store, resolved at call time.

    A single fixed lock made every pipeline run mutually exclusive regardless
    of which database it touched: a run against one store blocked an unrelated
    store, and a test with its own temp DB blocked on a real background run.
    Keying the lock to the store's identity keeps the guarantee that matters —
    no two runs against the SAME data — without serialising everything.
    """
    from brahmastra import db
    digest = hashlib.sha1(db.describe().encode("utf-8")).hexdigest()[:12]
    return data_dir() / f".pipeline-{digest}.lock"


def _acquire_lock() -> bool:
    _LOCK = _lock_path()
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
        _lock_path().unlink()
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
        # A pipeline run reaches the graph engine by definition — it writes
        # triples and saves the cache. Recording that here is what stops the
        # keepalive querying a remote instance that real work touched five
        # minutes ago. In the `finally`, because a run that died halfway still
        # hit the engine on its way there, and `record_contact` never raises.
        from brahmastra.keepalive import record_contact
        record_contact()


def _missing_notion_config(need_database: bool) -> list[str]:
    """
    Which Notion settings are absent.

    Both Notion stages ask this one function so they can never disagree. They
    used to check different things — sync required TOKEN *and* DATABASE_ID
    while write-back required only TOKEN — so a run with a token but no
    database id reported "NOTION_TOKEN / NOTION_DATABASE_ID not set" from sync
    and then pushed pages from write-back in the same run.
    """
    missing = []
    if not os.environ.get("NOTION_TOKEN"):
        missing.append("NOTION_TOKEN")
    if need_database and not _notion_source():
        # Not just the env var: a workspace may carry its own Notion source,
        # and skipping sync because the GLOBAL one is unset would leave that
        # workspace never pulling anything.
        missing.append("NOTION_DATABASE_ID (global or per-workspace)")
    return missing


def _notion_source() -> str | None:
    """This workspace's Notion source, falling back to the global setting."""
    try:
        from brahmastra.sync import _notion_target_for_current_workspace
        return _notion_target_for_current_workspace()
    except Exception:
        return os.environ.get("NOTION_DATABASE_ID") or None


def _run_pipeline_locked(full: bool, result: dict[str, Any]) -> dict[str, Any]:
    # ---------------------------------------------------------------
    # Stage 0: Notion sync (pull needs both the token and a target)
    # ---------------------------------------------------------------
    missing = _missing_notion_config(need_database=True)
    if not missing:
        try:
            from brahmastra.sync import run_sync
            sync_result = run_sync()
            result["stages"]["sync"] = sync_result
        except Exception as exc:
            result["stages"]["sync"] = {"error": str(exc)}
    else:
        # Name only what is actually absent, so the message cannot claim the
        # token is missing when it is set.
        result["stages"]["sync"] = {"skipped": f"not set: {', '.join(missing)}"}

    # ---------------------------------------------------------------
    # Stage 0b: drain session checkpoints
    #
    # The PreCompact hook queues conversations to disk but can only distil them
    # if an LLM answers at that moment. Draining here means a checkpoint taken
    # while Ollama was down and Groq was rate limited still lands, and lands
    # BEFORE extract so it is graphed in the same run.
    # ---------------------------------------------------------------
    try:
        from brahmastra.checkpoint import drain, pending_count
        if pending_count():
            result["stages"]["checkpoints"] = drain()
    except Exception as exc:
        result["stages"]["checkpoints"] = {"error": str(exc)}

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
    extract = result["stages"].get("extract") or {}
    # "Everything we tried, failed." A partial success still improves the
    # graph, but if nothing got in then the graph is stale and pushing
    # insights from it would overwrite good Notion content with old
    # conclusions — worse than not pushing at all.
    extraction_collapsed = bool(extract.get("errors")) and not extract.get("extracted")

    missing = _missing_notion_config(need_database=False)
    if missing:
        result["stages"]["notion_writeback"] = {"skipped": f"not set: {', '.join(missing)}"}
    elif extraction_collapsed:
        result["stages"]["notion_writeback"] = {
            "skipped": "extraction failed for every note; refusing to push a stale graph"
        }
    else:
        try:
            from brahmastra.notion_writeback import push_insights
            wb = push_insights()
            result["stages"]["notion_writeback"] = {
                "pushed": wb["pushed"],
                "skipped": wb["skipped"],
            }
        except Exception as exc:
            result["stages"]["notion_writeback"] = {"error": str(exc)}

    # ---------------------------------------------------------------
    # Run-level verdict. Without this a run exited 0 and looked healthy
    # while every note had failed to extract — the per-stage `errors` array
    # held the truth but nothing surfaced it, so callers had to know to
    # look. Callers can now branch on status alone.
    # ---------------------------------------------------------------
    stage_errors = [
        name for name, stage in result["stages"].items()
        if isinstance(stage, dict) and (stage.get("error") or stage.get("errors"))
    ]
    if extraction_collapsed:
        result["status"] = "error"
    elif stage_errors:
        result["status"] = "partial"
    else:
        result["status"] = "ok"
    result["failed_stages"] = stage_errors

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result
