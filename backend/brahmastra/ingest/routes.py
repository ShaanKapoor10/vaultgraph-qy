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

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from brahmastra.ingest.assemble import process_transcript
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
    ))
    background_tasks.add_task(_launch, tid, workspace)
    return {"transcript_id": tid, "status": "pending", "workspace": workspace}


@router.post("/transcripts/upload")
async def upload_transcript(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    occurred_at: str = Form(""),
) -> dict[str, Any]:
    """Accept a transcript file. The same path, with a file on the front."""
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
    ))
    background_tasks.add_task(_launch, tid, workspace)
    return {"transcript_id": tid, "status": "pending", "workspace": workspace,
            "characters": len(text)}


@router.get("/transcripts")
async def list_transcripts(
    status: Literal["pending", "processing", "done", "error"] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return get_ingest_store().list_transcripts(status=status, limit=min(limit, 200))


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
                    background_tasks: BackgroundTasks) -> dict[str, Any]:
    """
    Run it again. Derived rows are cleared first, so this corrects rather than
    duplicates -- which is what makes it safe to retry a partial run.
    """
    workspace = current_workspace()
    if get_ingest_store(workspace).get_transcript(transcript_id) is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    background_tasks.add_task(_launch, transcript_id, workspace)
    return {"transcript_id": transcript_id, "status": "pending"}


@router.delete("/transcripts/{transcript_id}")
async def delete_transcript(transcript_id: str) -> dict[str, Any]:
    """Removes the transcript and everything derived from it. Notes are left
    alone: they are in the graph now and deleting them is a separate decision."""
    store = get_ingest_store()
    if store.get_transcript(transcript_id) is None:
        raise HTTPException(status_code=404, detail=f"no transcript {transcript_id!r}")
    store.delete_transcript(transcript_id)
    return {"deleted": transcript_id}


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
