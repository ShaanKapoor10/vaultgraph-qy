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


@router.get("")
async def list_notes(status: str | None = None) -> list[dict[str, Any]]:
    return db.get_notes(status=status)


@router.get("/{note_id}")
async def get_note(note_id: str) -> dict[str, Any]:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.post("", status_code=201)
async def create_note(body: NoteCreate) -> dict[str, Any]:
    db.upsert_note(
        id=body.id,
        title=body.title,
        content=body.content,
        last_edited=body.last_edited,
        mark_pending=True,
    )
    return db.get_note(body.id)  # type: ignore[return-value]


@router.delete("/{note_id}", status_code=204)
async def delete_note(note_id: str) -> None:
    note = db.get_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete_triples_for_note(note_id)
    with db._connect() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
