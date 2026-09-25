"""
What the system is doing, what is stuck, and what will happen next -- in one
call, for the Diagnostics screen.

Every one of these used to be a terminal command somebody had to know about:
`python -m brahmastra.checkpoint --status`, reading `extraction_error` by hand,
`version --against`, the key pool's state, `coercions`. The questions they
answer are the ones asked most: "why is this note not in the graph?", "is
anything waiting?", "will it retry, or is it stuck?". So the answer to each is
computed here, including the VERDICT -- a failed note is never just "error",
it says whether the next run will fix it.

Read-only except for the actions at the bottom, which are the safe ones: mark
a note for another try, drain the checkpoint queue. Starting the pipeline and
re-processing a transcript already have endpoints of their own.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from brahmastra import db

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

# What an extraction_error means, and whether the next run can fix it.
_ERROR_KINDS = [
    ("quota", re.compile(r"quota|daily|tokens per day|TPD|rate.?limit|429", re.I),
     True, "Waiting for Groq quota. Retried on the next run once a key has budget."),
    ("too_large", re.compile(r"413|too large|request_too_large", re.I),
     False, "The note is too big for one request on this tier. Retrying will fail "
            "the same way -- split the note."),
    ("model", re.compile(r"does not exist|decommission|model_not_found|404", re.I),
     False, "The model was retired. Retrying will fail until GROQ model settings change."),
    ("bad_reply", re.compile(r"validate JSON|invalid JSON|empty response|malformed", re.I),
     True, "The model returned an unreadable reply. Usually fine on the next try."),
    ("unreachable", re.compile(r"connect|timeout|unreachable|Unavailable", re.I),
     True, "The provider could not be reached. Retried on the next run."),
]


def classify_error(text: str | None) -> dict[str, Any]:
    for kind, pattern, retries_help, advice in _ERROR_KINDS:
        if text and pattern.search(text):
            return {"kind": kind, "will_fix_itself": retries_help, "advice": advice}
    return {"kind": "other", "will_fix_itself": True,
            "advice": "Retried on the next run; if it keeps failing, read the error."}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:                                  # noqa: BLE001
        return default if default is not None else {"error": f"{type(exc).__name__}: {exc}"[:300]}


def _notes() -> dict[str, Any]:
    notes = db.get_notes()
    retry_on = os.environ.get("EXTRACT_RETRY_ERRORS", "1") != "0"
    errors = []
    for n in notes:
        if n.get("extraction_status") != "error":
            continue
        verdict = classify_error(n.get("extraction_error"))
        errors.append({"id": n["id"], "title": n["title"], "source": n.get("source"),
                       "error": (n.get("extraction_error") or "")[:400],
                       "will_retry": retry_on, **verdict})
    pending = [{"id": n["id"], "title": n["title"], "source": n.get("source"),
                "created_at": n.get("created_at")}
               for n in notes if n.get("extraction_status") == "pending"]
    return {
        "total": len(notes),
        "by_status": dict(Counter(n.get("extraction_status") or "unknown" for n in notes)),
        "by_source": dict(Counter(n.get("source") or "unknown" for n in notes)),
        "undated": sum(1 for n in notes if not (n.get("created_at") or n.get("last_edited"))),
        "retry_errors": retry_on,
        "errors": errors,
        "pending": pending[:100],
    }


def _llm() -> dict[str, Any]:
    from brahmastra import entity_confirm, groq_pool
    from brahmastra.llm import (model_for, provider_status, resolution_model_setting,
                                resolve_provider)

    provider = _safe(resolve_provider, "none")
    return {
        "provider": provider,
        "providers": _safe(provider_status, {}),
        "extraction_model": _safe(lambda: model_for(provider), "") if provider != "none" else "",
        "resolution_model": resolution_model_setting(),
        "judge_enabled": _safe(entity_confirm.enabled, False),
        "groq_keys": _safe(groq_pool.status, []),
    }


def _transcripts() -> list[dict[str, Any]]:
    from brahmastra.ingest.store import get_ingest_store

    store = get_ingest_store()
    out = []
    for t in store.list_transcripts(limit=200):
        chunks = store.get_chunks(t["id"])
        incomplete = [c["idx"] for c in chunks if c.get("error")]
        out.append({
            "id": t["id"], "title": t["title"], "status": t.get("status"),
            "error": t.get("error"), "created_at": t.get("created_at"),
            "chunks": len(chunks), "incomplete_chunks": incomplete,
            "complete": not incomplete and not t.get("error"),
            "artifacts": len(store.get_artifacts(transcript_id=t["id"], limit=1_000_000)),
            "rejected": len(store.rejected_ids(t["id"])),
        })
    return out


def _checkpoints() -> dict[str, Any]:
    from brahmastra import checkpoint

    tail: list[str] = []
    try:
        lines = checkpoint._log_path().read_text(encoding="utf-8").splitlines()
        tail = lines[-8:]
    except OSError:
        pass
    return {"queued": checkpoint.pending_count(), "queued_chars": checkpoint.queued_chars(),
            "queue_dir": str(checkpoint.queue_dir()), "log_tail": tail}


def _graph() -> dict[str, Any]:
    cached = db.get_cached_graph()
    if not cached:
        return {"built": False}
    stats = cached["stats"]
    contradictions = stats.get("contradictions", []) or []
    clusters = stats.get("concept_clusters", []) or []
    return {
        "built": True,
        "built_at": cached.get("built_at"),
        "nodes": stats.get("nodes"), "edges": stats.get("edges"),
        "clusters": len(clusters),
        "clusters_summarised": sum(1 for c in clusters if c.get("summary")),
        "contradictions": len(contradictions),
        "unresolved": [{"subject": c["subject"], "relation": c["relation"],
                        "values": c.get("conflicting_values"),
                        "why": c.get("resolution", "")}
                       for c in contradictions if not c.get("resolved_value")],
    }


def _indexes() -> dict[str, Any]:
    from brahmastra.code_index import CodeIndex
    from brahmastra.sessions import SessionIndex

    return {"sessions": _safe(lambda: SessionIndex().counts(), {}),
            "code": _safe(lambda: CodeIndex().counts(), {})}


def _coercions() -> dict[str, Any]:
    from brahmastra import coercions

    r = coercions.report()
    return {"total": r.get("total", 0), "by_kind": r.get("by_kind", {}),
            "notes_affected": r.get("notes_affected", 0),
            "top": [{"kind": g.get("kind"), "relation": g.get("raw_relation"),
                     "notes": g.get("notes"), "examples": (g.get("examples") or [])[:2]}
                    for g in (r.get("candidates") or [])[:6]]}


def attention(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The few things worth looking at first, most serious first."""
    items: list[dict[str, Any]] = []
    code = report.get("code") or {}
    if code.get("stale"):
        items.append({"level": "error", "text": "The server is running older code than is on disk.",
                      "hint": "Rebuild or restart the backend."})
    notes = report.get("notes") or {}
    stuck = [e for e in notes.get("errors", []) if not e.get("will_fix_itself")]
    if stuck:
        items.append({"level": "error",
                      "text": f"{len(stuck)} note(s) failed in a way retrying will not fix.",
                      "hint": stuck[0]["advice"]})
    waiting = [e for e in notes.get("errors", []) if e.get("will_fix_itself")]
    if waiting:
        items.append({"level": "warn",
                      "text": f"{len(waiting)} note(s) failed and will be retried on the next run.",
                      "hint": waiting[0]["advice"]})
    pending = len(notes.get("pending", []))
    if pending:
        items.append({"level": "info", "text": f"{pending} note(s) waiting to be extracted.",
                      "hint": "The scheduler runs the pipeline when anything is pending."})
    keys = (report.get("llm") or {}).get("groq_keys") or []
    resting = [k for k in keys if k.get("state") != "ready"]
    if keys and len(resting) == len(keys):
        items.append({"level": "warn", "text": "Every Groq key is resting or dead.",
                      "hint": "Model calls wait until a key's quota resets."})
    pipe = report.get("pipeline") or {}
    if pipe.get("stale"):
        items.append({"level": "info", "text": "The graph is behind the notes.",
                      "hint": "Run the pipeline to bring it up to date."})
    last = pipe.get("last") or {}
    if last.get("status") in ("error", "partial"):
        items.append({"level": "warn",
                      "text": f"The last pipeline run was {last['status']}: "
                              f"{', '.join(last.get('failed_stages') or []) or 'see details'}.",
                      "hint": "Open the Pipeline card for the failing stage."})
    for t in report.get("transcripts") or []:
        if not t.get("complete"):
            items.append({"level": "warn", "text": f"Meeting '{t['title']}' is incomplete.",
                          "hint": "Reprocess it once quota allows."})
    graph = report.get("graph") or {}
    if graph.get("unresolved"):
        items.append({"level": "info",
                      "text": f"{len(graph['unresolved'])} contradiction(s) the dates cannot settle.",
                      "hint": "Only a person can decide these."})
    cp = report.get("checkpoints") or {}
    if cp.get("queued"):
        items.append({"level": "info", "text": f"{cp['queued']} session capture(s) queued for distillation.",
                      "hint": "Drained by the next pipeline run, or drain them now."})
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(items, key=lambda i: order[i["level"]])


@router.get("")
async def diagnostics() -> dict[str, Any]:
    from brahmastra import version
    from brahmastra.pipeline import run_state
    from brahmastra.workspace import current_workspace

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": current_workspace(),
        "code": _safe(version.status),
        "pipeline": _safe(run_state),
        "notes": _safe(_notes),
        "llm": _safe(_llm),
        "transcripts": _safe(_transcripts, []),
        "checkpoints": _safe(_checkpoints),
        "graph": _safe(_graph),
        "indexes": _safe(_indexes),
        "coercions": _safe(_coercions),
    }
    report["attention"] = attention(report)
    return report


# -- safe actions -------------------------------------------------------------

@router.post("/notes/{note_id}/retry")
async def retry_note(note_id: str) -> dict[str, Any]:
    """Mark one note for extraction on the next run."""
    if db.get_note(note_id) is None:
        raise HTTPException(status_code=404, detail=f"no note {note_id!r}")
    db.set_note_status(note_id, "pending")
    return {"note_id": note_id, "status": "pending"}


@router.post("/notes/retry-errors")
async def retry_errors() -> dict[str, Any]:
    """Mark every failed note for extraction on the next run."""
    failed = db.get_notes(status="error")
    for n in failed:
        db.set_note_status(n["id"], "pending")
    return {"marked": len(failed)}


def _drain(workspace: str) -> None:
    from brahmastra import checkpoint
    from brahmastra.workspace import reset_request_workspace, set_request_workspace

    token = set_request_workspace(workspace)
    try:
        checkpoint.drain()
    finally:
        reset_request_workspace(token)


@router.post("/checkpoints/drain")
async def drain_checkpoints(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Distil queued session captures into notes now (needs a model)."""
    from brahmastra import checkpoint
    from brahmastra.workspace import current_workspace

    queued = checkpoint.pending_count()
    if queued:
        background_tasks.add_task(_drain, current_workspace())
    return {"queued": queued, "started": bool(queued)}
