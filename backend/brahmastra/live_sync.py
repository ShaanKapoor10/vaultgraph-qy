"""
Stage 9 — Live sync watcher (the "go live" loop).

Polls Notion on an interval. Whenever pages change (or notes are pending),
it runs the full pipeline — which pulls from Notion, extracts, resolves,
builds the graph, and writes insights BACK into Notion.

This makes Brahmastra feel alive: edit a page in Notion, and within one poll
interval the graph updates and the page's "🧠 Brahmastra Insights" toggle
refreshes automatically.

Run it:
    python -m brahmastra.live_sync                # default 120s interval
    POLL_INTERVAL=60 python -m brahmastra.live_sync

Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

# Ensure env is loaded no matter how this is launched
from brahmastra.env import load_env

load_env()

from brahmastra import db


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def tick() -> dict:
    """One watch iteration: sync Notion, run pipeline if anything is new."""
    from brahmastra.sync import run_sync
    from brahmastra.pipeline import run_pipeline

    summary: dict = {}
    # Notion is one SOURCE of notes, not the reason to run. Agents write through
    # MCP, the dashboard writes through the REST route, and the checkpoint hook
    # writes from sessions -- all of which leave notes needing extraction whether
    # or not Notion is connected. Skipping the pull when it is not configured
    # keeps the rest of the loop working, exactly as run_pipeline already does.
    if os.environ.get("NOTION_TOKEN"):
        sync_res = run_sync()
        summary["synced"] = sync_res.get("synced", 0)
    else:
        summary["synced"] = 0
        summary["notion"] = "not configured"

    pending = db.get_notes(status="pending")

    # Errored notes are work too, and forgetting that stranded them forever.
    #
    # run_extraction already retries status='error' on every run, precisely so
    # a transient provider outage does not park a note permanently. But this
    # tick decided whether to CALL it by counting only 'pending', so the
    # recovery was unreachable from the one process that runs unattended: 53
    # notes failed extraction with no LLM key configured, flipped to 'error',
    # and every subsequent tick reported "no changes (heartbeat)" while there
    # was a backlog it was built to clear.
    #
    # Mirrors EXTRACT_RETRY_ERRORS so the trigger and the stage cannot disagree
    # about what counts as work.
    retryable = (
        db.get_notes(status="error")
        if os.environ.get("EXTRACT_RETRY_ERRORS", "1") != "0"
        else []
    )
    summary["pending"] = len(pending)
    summary["retryable"] = len(retryable)

    # Keep the graph engine awake. This has to be here rather than left to the
    # tick's normal work, because the check just above -- get_notes(pending) --
    # is a SOURCE read: under NOTE_BACKEND=postgres it is answered entirely by
    # Postgres. An idle loop could therefore tick every fifteen minutes for a
    # week without sending Neo4j one query, and Aura Free suspends an instance
    # after about three days of exactly that.
    #
    # Before the pipeline, so an idle tick still touches the engine; skipped
    # cheaply when something already has, and it never raises.
    from brahmastra.keepalive import touch_if_idle

    summary["keepalive"] = touch_if_idle()

    if summary["synced"] > 0 or pending or retryable:
        pipe = run_pipeline(full=False)

        # Read every stage defensively. run_pipeline DOCUMENTS returning
        # {"skipped": ...} with an empty `stages` when another run holds the
        # lock, and a stage that failed is a dict carrying `error` rather than
        # the counts. Indexing straight into ["stages"]["extract"] therefore
        # raised KeyError('extract') on a perfectly ordinary skipped run, and
        # the watcher logged "ERROR in tick #1 after 0s: 'extract'" -- a bare
        # key name, which says nothing about a lock and reads like the extract
        # stage itself blew up.
        stages = pipe.get("stages") or {}
        graph = stages.get("graph") or {}

        summary["skipped"] = pipe.get("skipped")
        summary["status"] = pipe.get("status")
        summary["failed_stages"] = pipe.get("failed_stages") or []
        summary["extracted"] = (stages.get("extract") or {}).get("extracted", 0)
        summary["nodes"] = graph.get("nodes")
        summary["contradictions"] = graph.get("contradictions")
        summary["wrote_back"] = stages.get("notion_writeback", {})
        # A skipped run did nothing, and reporting it as work made the next
        # heartbeat claim an extraction that never happened.
        summary["did_work"] = pipe.get("skipped") is None
    else:
        summary["did_work"] = False
    return summary


def watch(interval: int | None = None) -> None:
    """Poll forever with health logging. interval secs (default POLL_INTERVAL or 120)."""
    if interval is None:
        interval = int(os.environ.get("POLL_INTERVAL", "120"))

    # Deliberately NOT a hard requirement any more. This refused to start without
    # Notion, which made "run the pipeline on a timer" impossible for anyone not
    # using Notion -- and the notes needing extraction mostly do not come from
    # Notion at all. It now runs the pipeline regardless and simply skips the
    # pull, which is what the pipeline itself already does.
    notion = "with Notion sync" if os.environ.get("NOTION_TOKEN") else "pipeline only (no NOTION_TOKEN)"
    print(f"[{_stamp()}] Brahmastra live sync started (every {interval}s, {notion}). "
          f"Ctrl-C to stop.", flush=True)

    n = 0
    while True:
        n += 1
        # "starting" line — if a tick hangs (e.g. Ollama stall), this is the last
        # line in the log, making the stall visible instead of silent death.
        print(f"[{_stamp()}] tick #{n} starting…", flush=True)
        t0 = time.monotonic()
        try:
            s = tick()
            dt = time.monotonic() - t0
            if s["did_work"]:
                wb = s.get("wrote_back", {})
                # Say when a run finished `partial`. The verdict existed but
                # never reached this log, so a scheduler quietly extracting
                # nothing looked identical to one doing fine.
                verdict = ""
                if s.get("status") and s["status"] != "ok":
                    verdict = f" status={s['status']} failed={s.get('failed_stages')}"
                print(
                    f"[{_stamp()}] tick #{n} OK in {dt:.0f}s | synced={s['synced']} "
                    f"extracted={s.get('extracted')} nodes={s.get('nodes')} "
                    f"contradictions={s.get('contradictions')} "
                    f"notion_pushed={wb.get('pushed') if isinstance(wb, dict) else wb}"
                    f"{verdict}",
                    flush=True,
                )
            elif s.get("skipped"):
                # Not idleness. Saying "no changes" here would claim there was
                # nothing to do while a backlog sat behind a held lock.
                print(f"[{_stamp()}] tick #{n} in {dt:.0f}s | skipped: {s['skipped']} "
                      f"(pending={s.get('pending')} retryable={s.get('retryable')})",
                      flush=True)
            else:
                # heartbeat even when idle, so liveness is always visible
                ka = s.get("keepalive") or {}
                note = ""
                if ka.get("pinged"):
                    note = f" | keepalive touched the engine ({ka.get('latency_ms')}ms)"
                elif ka.get("error"):
                    note = f" | keepalive FAILED: {ka['error']}"
                print(f"[{_stamp()}] tick #{n} OK in {dt:.0f}s | no changes (heartbeat){note}",
                      flush=True)
            if dt > interval:
                print(f"[{_stamp()}] WARNING: tick #{n} took {dt:.0f}s, longer than the "
                      f"{interval}s interval — watcher is falling behind.", flush=True)
        except Exception as e:
            dt = time.monotonic() - t0
            print(f"[{_stamp()}] ERROR in tick #{n} after {dt:.0f}s: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    watch()
