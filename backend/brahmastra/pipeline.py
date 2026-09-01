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


# ---------------------------------------------------------------------------
# What happened, durably
# ---------------------------------------------------------------------------
#
# The API tracked its own runs in a module-level dict, which answered "did the
# pipeline run?" only for runs THIS process started. A run kicked off by the
# scheduler, the CLI or MCP was invisible -- /pipeline/status reported "idle"
# while a run was genuinely in flight -- a restart erased it, and nothing
# anywhere recorded that a run had ever finished. So the dashboard could show a
# spinner and then nothing, leaving "I hope it ran" as the only conclusion
# available.
#
# Recorded HERE instead, in the one function every caller goes through, and
# written beside the lock so any process can read it.

def _record_path() -> Path:
    from brahmastra import db
    digest = hashlib.sha1(db.describe().encode("utf-8")).hexdigest()[:12]
    return data_dir() / f".pipeline-last-{digest}.json"


# ---------------------------------------------------------------------------
# Whether the DERIVED data is behind the notes
# ---------------------------------------------------------------------------
#
# "When did the pipeline last run" and "is the graph current" are different
# questions, and only the first one had an answer. Extraction is reachable
# without run_pipeline -- brahmastra_add_note extracts inline, POST
# /pipeline/extract calls run_extraction directly, and a script can call
# extract_note itself -- so triples land while resolve, build-graph and the
# cache do not. The status then reports a clean run from yesterday and looks
# healthy, while the graph is missing everything stored since.
#
# That is the shape of a wrong answer rather than a slow one: /ask and /graph
# read the cache, so they answer confidently from a graph that predates the
# note you just stored, and nothing anywhere says so.
#
# A stamp beside the lock and the record, so any process sees it and it
# survives a restart -- the same pattern as the keepalive's touch file.

def _dirty_path() -> Path:
    from brahmastra import db
    digest = hashlib.sha1(db.describe().encode("utf-8")).hexdigest()[:12]
    return data_dir() / f".pipeline-dirty-{digest}.json"


def mark_dirty(reason: str = "") -> None:
    """
    Record that triples changed outside a completed rebuild.

    Called from `extract_note`, which is the single chokepoint every path to
    new triples goes through. Never raises: failing to note staleness must not
    fail the extraction that caused it.
    """
    import json
    try:
        path = _dirty_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "epoch": time.time(),
            "reason": reason,
        }), encoding="utf-8")
    except Exception:
        pass


def dirty_since() -> dict[str, Any] | None:
    """What is known about derived data being behind, or None if it is current."""
    import json
    try:
        return json.loads(_dirty_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def graph_is_behind() -> dict[str, Any] | None:
    """
    Ask the STORE whether the cached graph still reflects the triples.

    Preferred over the file stamp, and the reason is the deployment rather than
    elegance. `data_dir()` is a different place for a host process than for a
    container -- the MCP server writes backend/data, the containers share
    /data -- so a stamp written by an MCP `add_note` is invisible to the
    dashboard that needs to report it, and a stamp written by a containerised
    run is invisible to the CLI. The cache lives in the store, which both
    halves genuinely share, so a count recorded there is seen by everyone.

    Returns None when the graph is current, when nothing has been built yet, or
    when the cache predates this check and cannot answer -- callers fall back
    to the stamp. Reports rather than raises: a status endpoint must not fail
    because the store is briefly unreachable.
    """
    try:
        cached = db.get_cached_graph()
        if not cached:
            return None
        built_from = (cached.get("stats") or {}).get("triples_total")
        if built_from is None:
            return None                       # cache written before this existed
        now = db.get_db_stats().get("triples_total")
        if now is None or now == built_from:
            return None
        return {
            "built_from_triples": built_from,
            "triples_now": now,
            "built_at": cached.get("built_at"),
        }
    except Exception:                                  # noqa: BLE001
        return None


def clear_dirty(before: float) -> None:
    """
    Mark the graph current again, for work that happened before `before`.

    The timestamp matters. A note stored WHILE a run is in progress is
    genuinely not in the graph that run produced, so clearing unconditionally
    at the end would erase a true staleness signal and leave the note
    invisible until something else happened to trigger a rebuild.
    """
    try:
        stamp = dirty_since()
        if stamp is None or float(stamp.get("epoch") or 0) <= before:
            _dirty_path().unlink(missing_ok=True)
    except Exception:
        pass


def _write_record(result: dict[str, Any]) -> None:
    """Save a finished run's verdict. Never raises -- reporting is not the work."""
    import json
    try:
        stages = result.get("stages") or {}
        graph = stages.get("graph") or {}
        record = {
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "mode": result.get("mode"),
            "status": result.get("status"),
            "failed_stages": result.get("failed_stages") or [],
            # A summary, not the whole result: this is read on every poll, and
            # the full object carries per-note error text that can be large.
            "extracted": (stages.get("extract") or {}).get("extracted", 0),
            "triples_added": (stages.get("extract") or {}).get("triples_added", 0),
            "nodes": graph.get("nodes"),
            "edges": graph.get("edges"),
            "contradictions": graph.get("contradictions"),
        }
        path = _record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    except Exception:
        pass


def last_run() -> dict[str, Any] | None:
    """The most recent finished run against THIS store, or None."""
    import json
    try:
        return json.loads(_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def active_run() -> dict[str, Any] | None:
    """
    A run in progress against this store, started by ANY process.

    Derived from the lock, which is the only thing every caller already takes.
    Reports the lock's AGE rather than merely its existence, because a crashed
    run leaves the file behind -- and a stale lock read as "still running" is
    how a dashboard spins forever on a run that died fifteen minutes ago.
    """
    lock = _lock_path()
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return None
    return {
        "running": age <= _LOCK_STALE_SECS,
        "age_seconds": round(age, 1),
        "stale": age > _LOCK_STALE_SECS,
        "stale_after_seconds": _LOCK_STALE_SECS,
    }


def run_state() -> dict[str, Any]:
    """
    Everything a caller needs to answer "has the pipeline run?".

    Deliberately cross-process and durable, so the dashboard shows a scheduler
    run it did not start and still says what happened after a restart.
    """
    active = active_run()
    # The store first, because it is the half that host and container share.
    # The stamp is the fallback: it still catches a store that has never been
    # built, and a cache written before the count was recorded.
    behind = graph_is_behind()
    dirty = dirty_since()
    return {
        "running": bool(active and active["running"]),
        "active": active,
        "last": last_run(),
        # Two different questions. `last` says when a run finished; `stale`
        # says whether the graph reflects the notes as they are NOW. A store
        # can have a clean run from yesterday AND a stale graph, which is
        # exactly what happens when notes arrive through the MCP tool, and
        # reporting only the first is how /ask ends up answering confidently
        # from a graph that predates the note you just stored.
        "stale": behind is not None or dirty is not None,
        "behind": behind,
        "dirty_since": dirty,
        "target": db.describe(),
    }


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

    # Taken before any stage runs, so work that arrives DURING the run stays
    # marked stale -- it is genuinely not in the graph this run is building.
    began = time.time()

    try:
        outcome = _run_pipeline_locked(full, result)
        # Recorded before the lock is released, so a poll can never see the
        # lock gone AND no record of why -- which reads as "nothing ever ran".
        _write_record(outcome)
        # Only a run that actually rebuilt the graph makes it current again.
        # Clearing on any completion would report a graph as fresh when the
        # stage that builds it had failed, which is worse than reporting
        # nothing: the caller stops looking.
        if "graph" not in (outcome.get("failed_stages") or []):
            clear_dirty(began)
        return outcome
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
