"""
Workspaces router — create and manage separate knowledge graphs.

    GET    /workspaces                 list
    POST   /workspaces                 create
    GET    /workspaces/{id}            one, with its stats
    DELETE /workspaces/{id}            delete it and everything under it
    GET    /workspaces/search?q=...    search across workspaces

Every other route keeps working against the workspace selected by
BRAHMASTRA_WORKSPACE (default: `default`), so single-graph use is unchanged.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from brahmastra import db
from brahmastra.workspace import DEFAULT_WORKSPACE, InvalidWorkspaceId, slugify

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    # id is optional: a UI can post just a display name and get a slug.
    id: str | None = Field(default=None, description="Slug; derived from name if omitted")
    name: str = Field(..., min_length=1, description="Display name")
    description: str = ""
    notion_database_id: str | None = Field(
        default=None,
        description="This workspace's Notion source; falls back to the global one",
    )


@router.get("")
async def list_workspaces() -> dict[str, Any]:
    return {"workspaces": db.list_workspaces(), "current": db.workspace()}


@router.post("", status_code=201)
async def create_workspace(body: CreateWorkspaceRequest) -> dict[str, Any]:
    wid = body.id or slugify(body.name)
    try:
        created = db.create_workspace(
            id=wid,
            name=body.name,
            description=body.description,
            notion_database_id=body.notion_database_id,
        )
    except InvalidWorkspaceId as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except NotImplementedError as e:
        # The active backend cannot partition — say so plainly rather than
        # pretending the workspace exists.
        raise HTTPException(status_code=501, detail=str(e)) from e
    return created


@router.get("/search")
async def search_across(
    q: str = Query(..., min_length=1),
    workspaces: str | None = Query(
        default=None, description="Comma-separated ids; omit to search all"
    ),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """Search several graphs at once. Results carry the workspace they came from."""
    targets = [w.strip() for w in workspaces.split(",")] if workspaces else None
    try:
        hits = db.search_notes_across(q, targets, limit)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    return {"query": q, "searched": targets or "all", "results": hits}


@router.get("/{workspace_id}")
async def get_workspace(workspace_id: str) -> dict[str, Any]:
    ws = db.get_workspace(workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail=f"No workspace {workspace_id!r}")
    # Stats come from a store bound to that workspace, so this reports the
    # requested graph rather than whichever one the process defaults to.
    return {**ws, "stats": db.for_workspace(workspace_id).stats()}


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(workspace_id: str) -> None:
    if workspace_id == DEFAULT_WORKSPACE:
        raise HTTPException(
            status_code=409,
            detail="The default workspace cannot be deleted; it holds pre-workspace data.",
        )
    if not db.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail=f"No workspace {workspace_id!r}")
    db.delete_workspace(workspace_id)
