"""
Notes router — CRUD for notes in SQLite.
"""

from __future__ import annotations

from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from brahmastra import db

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteCreate(BaseModel):
    id: str
    title: str
    content: str
    last_edited: Optional[str] = None
    extraction_status: Optional[str] = None  # "pending" | "done" | "error"


@router.get("")
async def list_notes(
    status: str | None = None,
    q: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    List notes, or search them when `q` is given.

    Search was reachable only over MCP until now: this route accepted `?q=` in
    the sense that FastAPI ignored it, returning every note in last_edited
    order. That looks exactly like a search returning everything as a weak
    match, so the flagship feature appeared present and broken rather than
    absent.

    Results come back in RELEVANCE order and must not be re-sorted -- on the
    hybrid backends this is the fused BM25-plus-vector ranking, and reordering
    by date discards the entire point of it.
    """
    if q and q.strip():
        return db.search_notes(q, limit=limit)
    return db.get_notes(status=status)


@router.get("/{note_id}")
async def get_note(note_id: str) -> dict[str, Any]:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.post("", status_code=201)
async def create_note(body: NoteCreate) -> dict[str, Any]:
    # Honor the caller's extraction_status if provided; default to pending
    # so the pipeline picks it up for LLM extraction.
    mark_pending = body.extraction_status != "done"
    db.upsert_note(
        id=body.id,
        title=body.title,
        content=body.content,
        last_edited=body.last_edited,
        mark_pending=mark_pending,
        # The REST route is what the Next.js dashboard posts to. A future
        # non-UI caller of POST /notes should send its own source instead.
        source="ui",
    )
    return db.get_note(body.id)  # type: ignore[return-value]


@router.delete("/{note_id}", status_code=204)
async def delete_note(note_id: str) -> None:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    # Goes through the store API rather than raw SQL, so this works on any
    # backend. delete_note removes the note and its derived triples together.
    db.delete_note(note_id)
