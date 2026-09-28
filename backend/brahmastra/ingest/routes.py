"""
HTTP surface for transcript ingestion.

Mounted inside the existing app rather than served separately, so it inherits
the auth middleware and the per-request workspace binding. A second door into
the same data with its own idea of who may open it is how isolation gets lost.

Processing runs in the background for the same reason the pipeline does: a
transcript is dozens of LLM calls on a rate-limited tier and takes minutes,
which is far longer than any proxy will hold a request open. The caller gets an
id immediately and polls.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from brahmastra.ingest import assemble
from brahmastra.ingest.assemble import process_transcript
from brahmastra.ingest.modes import MODES, get_mode, is_mode
from brahmastra.ingest.store import Transcript, get_ingest_store
from brahmastra.workspace import current_workspace

router = APIRouter(prefix="/ingest", tags=["ingest"])

# Text formats only. A .docx or .pdf is a parsing problem with its own failure
# modes, and silently ingesting the XML inside a .docx would fill the knowledge
# base with markup that looks like speech.
ALLOWED_SUFFIXES = {".txt", ".md", ".vtt", ".srt", ".text", ".log"}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class TranscriptIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    source: str = "upload"
    source_ref: str | None = None
    occurred_at: str | None = None
    # How to read it (ingest/modes.py): "meeting" or "lecture". Unset is a meeting.
    mode: str | None = None


def _checked_mode(mode: str | None) -> str | None:
    """A named mode must exist: a typo would otherwise read a lecture as a meeting."""
    if mode is None or not mode.strip():
        return None
    if not is_mode(mode):
        raise HTTPException(status_code=422,
                            detail=f"unknown mode {mode!r}; expected one of {sorted(MODES)}")
    return mode.strip().lower()


@router.get("/modes")
async def list_modes() -> list[dict[str, Any]]:
    """The kinds of session a transcript can be read as, and what each yields."""
    return [m.as_dict() for m in MODES.values()]


def _launch(transcript_id: str, workspace: str) -> None:
    """
    Run in the background, in the workspace the REQUEST named.

    The binding is a ContextVar on the request, and a background task does not
    inherit it -- so without re-binding here the work would land in whatever
    workspace the process defaults to. That is precisely the failure this
    system has had before, and it is silent.
    """
    from brahmastra.workspace import reset_request_workspace, set_request_workspace

    token = set_request_workspace(workspace)
    try:
        process_transcript(transcript_id, workspace=workspace)
    finally:
        reset_request_workspace(token)


@router.post("/transcripts")
async def submit_transcript(
    body: TranscriptIn, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Accept a transcript as JSON and start processing it."""
    workspace = current_workspace()
    store = get_ingest_store(workspace)
    tid = store.create_transcript(Transcript(
        id="", title=body.title, content=body.content, source=body.source,
        source_ref=body.source_ref, occurred_at=body.occurred_at,
        mode=_checked_mode(body.mode),
    ))
    background_tasks.add_task(_launch, tid, workspace)
    return {"transcript_id": tid, "status": "pending", "workspace": workspace,
            "mode": get_mode(body.mode).id}


@router.post("/transcripts/upload")
async def upload_transcript(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    occurred_at: str = Form(""),
    mode: str = Form(""),
) -> dict[str, Any]:
    """Accept a transcript file. The same path, with a file on the front."""
    chosen = _checked_mode(mode)
    name = file.filename or "transcript"
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix and suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type {suffix!r}; expected one of "
                   f"{sorted(ALLOWED_SUFFIXES)}",
        )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"transcript is {len(raw) // 1024}KB, over the "
                   f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 never fails, so this is a real fallback rather than a guess
        # that can leave the caller with an opaque 500.
        text = raw.decode("latin-1", errors="replace")

    if not text.strip():
        raise HTTPException(status_code=400, detail="the file is empty")

    workspace = current_workspace()
    store = get_ingest_store(workspace)
    tid = store.create_transcript(Transcript(
        id="", title=title.strip() or name, content=text,
        source="upload", source_ref=name, occurred_at=occurred_at or None,
        mode=chosen,
    ))
    background_tasks.add_task(_launch, tid, workspace)
    return {"transcript_id": tid, "status": "pending", "workspace": workspace,
            "characters": len(text)}


@router.get("/transcripts")
async def list_transcripts(
    status: Literal["pending", "processing", "done", "error"] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    store = get_ingest_store()
    rows = store.list_transcripts(status=status, limit=min(limit, 200))
    # Only for meetings still running: a poll every few seconds should not
    # read every chunk of every finished meeting.
    for r in rows:
        if r.get("status") in ("pending", "processing"):
            r["progress"] = progress(r, store.get_chunks(r["id"]))
    return rows


@router.get("/transcripts/{transcript_id}")
async def get_transcript(transcript_id: str, include_text: bool = False) -> dict[str, Any]:
    """
    One transcript and how its chunks fared.

    The raw text is omitted unless asked for: it is the largest thing in the
    system and a status poll should not carry a megabyte.
    """
    store = get_ingest_store()
    record = store.get_transcript(transcript_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    if not include_text:
        record.pop("content", None)

    chunks = store.get_chunks(transcript_id)
    for c in chunks:
        c.pop("text", None)
    record["chunks"] = chunks
    record["artifact_counts"] = {
        kind: len(store.get_artifacts(kind=kind, transcript_id=transcript_id))
        for kind in ("decision", "action_item", "risk", "open_question")
    }
    # Whether this record is WHOLE, which `status` alone cannot say.
    #
    # Comprehension degrades on purpose: when the focused variant's concerns
    # pass fails, the commitments it already found are still stored, because
    # half a record beats none. A real ingestion hit the Groq daily cap on that
    # second call and stored four decisions and four action items with zero
    # risks and zero open questions -- reporting `status: done, error: null`,
    # which is indistinguishable from a meeting that raised no concerns.
    #
    # `status` cannot carry this: the table has a CHECK constraint on it and
    # the deployed Postgres already holds that constraint, so widening the
    # vocabulary is a migration on live data. Derived here instead, from the
    # error the chunks now record.
    incomplete = [c["idx"] for c in chunks if c.get("error")]
    record["complete"] = not incomplete and not record.get("error")
    if incomplete:
        record["incomplete_chunks"] = incomplete
    return record


@router.post("/transcripts/{transcript_id}/reprocess")
async def reprocess(transcript_id: str,
                    background_tasks: BackgroundTasks,
                    mode: str | None = None) -> dict[str, Any]:
    """
    Run it again. Derived rows are cleared first, so this corrects rather than
    duplicates -- which is what makes it safe to retry a partial run.

    `?mode=lecture` reads it as a different kind of session from now on: a
    lecture first read as a meeting is corrected by processing it again.
    """
    workspace = current_workspace()
    store = get_ingest_store(workspace)
    record = store.get_transcript(transcript_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    chosen = _checked_mode(mode)
    if chosen:
        store.set_transcript_mode(transcript_id, chosen)
    background_tasks.add_task(_launch, transcript_id, workspace)
    return {"transcript_id": transcript_id, "status": "pending",
            "mode": get_mode(chosen or record.get("mode")).id}


@router.delete("/transcripts/{transcript_id}")
async def delete_transcript(transcript_id: str,
                            purge_notes: bool = False) -> dict[str, Any]:
    """
    Removes the transcript, its chunks and its artifacts.

    `purge_notes=true` takes the generated notes and their triples with it.
    The default leaves them and RELEASES the claim on them -- they become
    ordinary notes nothing will rewrite or remove. Deleting a transcript
    deletes the source, so unlike a re-ingestion nothing can recompute them
    afterwards; see `assemble.drop_transcript` for the two shapes.
    """
    store = get_ingest_store()
    if store.get_transcript(transcript_id) is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    return assemble.drop_transcript(transcript_id, store=store,
                                    purge_notes=purge_notes)


@router.get("/artifacts")
async def list_artifacts(
    kind: Literal["decision", "action_item", "risk", "open_question"] | None = None,
    owner: str | None = None,
    transcript_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    The questions an organisation actually asks.

        /ingest/artifacts?kind=decision            what did we decide
        /ingest/artifacts?kind=action_item&owner=Mei   what is Mei on the hook for
    """
    return get_ingest_store().get_artifacts(
        kind=kind, owner=owner, transcript_id=transcript_id, limit=min(limit, 500),
    )


@router.get("/stats")
async def ingest_stats() -> dict[str, Any]:
    store = get_ingest_store()
    return {"workspace": store.workspace, "target": store.describe(), **store.counts()}


# -- the meeting view ------------------------------------------------------------

class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.get("/transcripts/{transcript_id}/meeting")
async def meeting_view(transcript_id: str) -> dict[str, Any]:
    """
    One meeting as a person reads it: every finding with its kind, owner, the
    person who SAID it, the verbatim quote and when in the meeting it was said,
    and whether someone has rejected it.

    `said_by` is recomputed from the transcript (segmentation is deterministic
    and naming diarized voices is memoised), so it is exactly what the graph
    record used -- see graph_record._person_for.
    """
    from brahmastra.ingest.assemble import _segment_with_speakers
    from brahmastra.ingest.evidence import speaker_of
    from brahmastra.ingest.speakers import is_anonymous

    store = get_ingest_store()
    record = store.get_transcript(transcript_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    report: dict[str, Any] = {"errors": []}
    chunks = _segment_with_speakers(record, report)
    by_index = {c.index: c for c in chunks}
    rejected = store.rejected_ids(transcript_id)
    items = []
    for a in store.get_artifacts(transcript_id=transcript_id, limit=1_000_000):
        said_by = speaker_of(a.get("quote") or "", by_index.get(a.get("chunk_index")))
        items.append({**a, "said_by": said_by, "rejected": a["id"] in rejected})
    order = {"decision": 0, "action_item": 1, "risk": 2, "open_question": 3}
    items.sort(key=lambda a: (order.get(a["kind"], 9), a.get("start_time") or "", a["statement"]))
    speakers = sorted({s for c in chunks for s in (c.speakers or [])})
    stored = store.get_chunks(transcript_id)
    points_by_part: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        if item["kind"] == "point" and not item.get("rejected"):
            points_by_part.setdefault(item.get("chunk_index") or 0, []).append(
                {"id": item["id"], "statement": item["statement"], "said_by": item.get("said_by")})
    parts = [{"idx": c["idx"], "status": c.get("status"), "error": c.get("error"),
              "topic": c.get("topic"), "summary": c.get("summary"),
              "start_time": c.get("start_time"), "end_time": c.get("end_time"),
              "speakers": c.get("speakers"), "points": points_by_part.get(c["idx"], [])}
             for c in stored if c["idx"] < (record.get("chunk_count") or len(stored))]
    mode = get_mode(record.get("mode"))
    for p in parts:
        if isinstance(p["speakers"], str):
            try:
                p["speakers"] = json.loads(p["speakers"])
            except ValueError:
                p["speakers"] = []
    return {
        "id": transcript_id,
        "title": record["title"],
        "occurred_at": record.get("occurred_at"),
        "status": record.get("status"),
        "error": record.get("error"),
        "participants": [s for s in speakers if not is_anonymous(s)],
        "unnamed_voices": [s for s in speakers if is_anonymous(s)],
        "speaker_identification": report.get("speakers"),
        "chunks": len(chunks),
        "mode": mode.as_dict(),
        "overview": store.get_overview(transcript_id),
        "progress": progress(record, stored),
        "parts": parts,
        "items": items,
    }


def progress(record: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Where a run is, from what it has already stored -- no extra bookkeeping.

    Findings are only written once EVERY part is read (consolidation needs the
    whole meeting), so while a meeting processes its counts are honestly zero.
    This is what says how far it has got instead.
    """
    status = record.get("status")
    total = int(record.get("chunk_count") or 0)
    current = [c for c in chunks if c["idx"] < total]
    read = sum(1 for c in current if c.get("status") in ("done", "error"))
    failed = sum(1 for c in current if c.get("status") == "error")
    if status == "pending":
        stage = "queued"
    elif status == "processing" and total == 0:
        stage = "splitting the transcript into parts"
    elif status == "processing" and read < total:
        stage = f"reading part {read + 1} of {total}"
    elif status == "processing":
        stage = "merging findings and writing them to the graph"
    else:
        stage = status or "unknown"
    return {"status": status, "stage": stage, "parts_total": total,
            "parts_read": read, "parts_failed": failed}


@router.post("/artifacts/{artifact_id}/reject")
async def reject_artifact(artifact_id: str, body: RejectIn | None = None) -> dict[str, Any]:
    """
    "This finding is wrong." It leaves the graph now, and stays out through every
    re-processing -- the rejection is a person's judgement and is kept as source
    data. Undo with DELETE on the same path.
    """
    store = get_ingest_store()
    row = store.reject_artifact(artifact_id, (body.reason if body else None))
    if row is None:
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id!r}")
    rebuilt = assemble.rebuild_record(row["transcript_id"], store=store)
    return {"artifact_id": artifact_id, "rejected": True, "record": rebuilt}


@router.delete("/artifacts/{artifact_id}/reject")
async def unreject_artifact(artifact_id: str) -> dict[str, Any]:
    store = get_ingest_store()
    row = next((a for a in store.get_artifacts(limit=1_000_000) if a["id"] == artifact_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id!r}")
    store.unreject_artifact(artifact_id)
    rebuilt = assemble.rebuild_record(row["transcript_id"], store=store)
    return {"artifact_id": artifact_id, "rejected": False, "record": rebuilt}


@router.get("/passages")
async def search_passages(q: str, transcript_id: str | None = None,
                          limit: int = 8) -> list[dict[str, Any]]:
    """
    Search what was actually SAID -- raw transcript passages with speaker and
    time (ingest/passages.py). Hybrid, like note search. `transcript_id`
    narrows it to one session.
    """
    from brahmastra.ingest.passages import PassageIndex, search

    store = get_ingest_store()
    return search(q, limit=min(max(limit, 1), 30), transcript_id=transcript_id,
                  store=PassageIndex(workspace=store.workspace))
