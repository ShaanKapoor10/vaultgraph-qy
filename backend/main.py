"""
Brahmastra backend — FastAPI application.

Routes are registered under /api/* by vercel.json experimentalServices.
FastAPI only sees the path after the prefix (e.g. GET /health).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env so GROQ_API_KEY / ANTHROPIC_API_KEY are available when the
# server is started directly with uvicorn (outside of the MCP process).
from brahmastra.env import load_env

load_env()

from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from brahmastra.auth import auth_status, require_api_key
from brahmastra.db import init_db
from brahmastra.routers import notes, pipeline, graph, ask, paths, workspaces, entities


def _mcp_http_enabled() -> bool:
    return os.environ.get("MCP_HTTP", "").strip().lower() in {"1", "true", "yes"}


# Whether the schema was ever created. Not a cache of "is the store up" -- it
# only records that this work still needs doing, so readiness can finish it.
_schema_ready = False


def _try_init_db() -> str | None:
    """
    Create the schema, and survive not being able to.

    A store that is unreachable at boot must NOT take the process down with it.
    That intent is already written into the health probes -- /health refuses to
    touch the database precisely so a sleeping dependency cannot get the
    container killed and restarted, which cannot fix a dependency -- and the
    lifespan then contradicted it by calling init_db() unguarded.

    That is not hypothetical. Aura Free suspended, its hostname stopped
    resolving, init_schema raised ServiceUnavailable, and uvicorn reported
    "Application startup failed. Exiting." The API was then down for a reason
    that had nothing to do with the API, and it stayed down: a crash loop
    cannot recover, whereas a live process becomes ready the moment the
    dependency returns. Postgres still held every note the whole time.

    Returns None on success, or the reason it could not, for readiness to
    report. The error is not stored: the point is to try again.
    """
    global _schema_ready
    try:
        init_db()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    _schema_ready = True
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialise the store, and run the MCP session manager when serving MCP.

    Mounting a sub-application does NOT run its lifespan — Starlette only runs
    the outermost one. So the mounted MCP app came up with its task group
    uninitialised and every call died on "Task group is not initialized",
    which reads like an MCP bug rather than a wiring mistake. The session
    manager has to be entered here, by the app that actually owns the lifespan.
    """
    _try_init_db()
    if _mcp_http_enabled():
        from brahmastra.mcp_server import mcp as _mcp

        async with _mcp.session_manager.run():
            yield
    else:
        yield


app = FastAPI(
    title="Brahmastra API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # "*" is right for local development and wrong for a public deployment:
    # combined with allow_credentials it lets any site call the API as the
    # user. Set CORS_ORIGINS to a comma-separated allowlist when deploying.
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication. Registered after CORS so a rejected request still carries
# CORS headers — otherwise a browser reports an opaque network error instead of
# the 401 that would tell you what is wrong.
app.middleware("http")(require_api_key)

# Header wins over query string: a header is the request's own metadata, while
# a query parameter can be pasted into a link and shared, and a shared link
# quietly writing into someone else's graph is the failure worth avoiding.
# Both are supported because the dashboard's proxy forwards the query string
# untouched, so `?workspace=` works with no plumbing on that side.
WORKSPACE_HEADER = "X-Brahmastra-Workspace"


@app.middleware("http")
async def bind_workspace(request, call_next):
    """
    Resolve the workspace for THIS request, not for the process.

    The API previously read BRAHMASTRA_WORKSPACE once and served that one
    workspace for its whole life. The store and MCP layers had supported many
    graphs for a long time; every HTTP caller could still reach exactly one, so
    the dashboard had no way to show a second and no reason to offer a picker.

    Bound as a ContextVar rather than a parameter threaded through every route,
    so the ~104 db.* call sites keep working unchanged — that indirection is
    what the facade is for. Reset in a finally, because a task that keeps the
    binding hands it to whatever runs next on the same context, and in a system
    whose isolation is "every row carries a workspaceId" that would mean serving
    one graph in place of another.
    """
    from brahmastra.workspace import (
        InvalidWorkspaceId,
        reset_request_workspace,
        set_request_workspace,
        validate_id,
    )

    requested = (
        request.headers.get(WORKSPACE_HEADER)
        or request.query_params.get("workspace")
        or ""
    ).strip()

    if requested:
        try:
            requested = validate_id(requested)
        except InvalidWorkspaceId as e:
            # Refuse rather than fall back to `default`. Silently serving a
            # different graph than the one asked for is how a caller ends up
            # writing into the wrong one and never learning it happened.
            return JSONResponse(
                {"detail": f"invalid workspace: {e}"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    token = set_request_workspace(requested or None)
    try:
        return await call_next(request)
    finally:
        reset_request_workspace(token)

# Remote MCP. The same tools a local stdio server exposes, over HTTP, mounted
# inside this app on purpose: it inherits the auth middleware above, so there
# is ONE authentication boundary rather than a second unprotected door into the
# same graph. Enabled only when asked for, because a local stdio server needs
# nothing of the sort.
if _mcp_http_enabled():
    from brahmastra.mcp_server import mcp as _mcp

    # The sub-app serves at its own `streamable_http_path`, which defaults to
    # "/mcp" — mounting that at "/mcp" puts the endpoint at /mcp/mcp, so a
    # client pointed at /mcp gets a 404 and reports the server as unreachable.
    # Serve it at the mount root instead, so the URL is exactly /mcp.
    _mcp.settings.streamable_http_path = "/"
    app.mount("/mcp", _mcp.streamable_http_app())

# Register routers
app.include_router(notes.router)
app.include_router(pipeline.router)
app.include_router(graph.router)
app.include_router(ask.router)
app.include_router(paths.router)
app.include_router(workspaces.router)
app.include_router(entities.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """
    Liveness. Answers "is this process running?" and nothing more.

    Deliberately does not touch the database: a liveness probe that fails when
    a dependency is down gets the container killed and restarted, which cannot
    fix a dependency. Restarting a healthy process because Neo4j is asleep just
    turns a degraded system into an unavailable one.
    """
    return {"status": "ok", "service": "brahmastra"}


@app.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    """
    Readiness. Answers "can this instance serve traffic?" — which means the
    graph store must actually respond, not merely be configured.

    Returns 503 when it cannot, so a load balancer stops routing here while
    leaving the process alive to recover. This is the probe worth wiring up:
    the store is remote and pausable when GRAPH_BACKEND=neo4j, and Aura Free
    suspends after roughly three days idle.
    """
    from brahmastra import db
    from brahmastra.stores import backend_name, note_backend_name

    notes_on = note_backend_name() or backend_name()
    detail: dict[str, Any] = {
        "service": "brahmastra",
        "backend": backend_name(),
        # Which store holds the data that cannot be recomputed. Reported
        # separately from `backend` because they are now separable, and because
        # the answer is the one thing worth checking after a deploy.
        "system_of_record": notes_on,
        # Visible on purpose: "unconfigured" means the API is refusing all
        # traffic, and "anonymous" means it is open to anyone who can reach it.
        "auth": auth_status(),
    }

    # A misconfiguration that looks completely healthy is the one worth
    # surfacing. SQLite as the system of record answers every probe normally
    # while being one file on one container's disk -- lost on redeploy, invisible
    # to a second instance, and lexical-only, so hybrid search silently
    # degrades. It stays a warning rather than a 503: refusing traffic would
    # turn a recoverable misconfiguration into an outage, and single-store
    # SQLite remains a legitimate way to run this locally.
    if notes_on == "sqlite":
        detail["warnings"] = [
            "system of record is SQLite: a single file on this container's "
            "disk, discarded on redeploy unless it is on a volume, unreadable "
            "by any other instance, and lexical-only (no hybrid search). Set "
            "NOTE_BACKEND=postgres for anything deployed."
        ]
    # Finish what startup could not. The schema is created idempotently, so
    # retrying here is what lets an instance that booted against a suspended
    # engine become ready on its own the moment that engine returns -- without
    # a restart, and without an operator noticing.
    if not _schema_ready:
        failure = _try_init_db()
        if failure is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            detail.update(status="unavailable", error=failure,
                          schema="not initialised")
            return detail

    try:
        stats = db.get_db_stats()
        detail.update(
            status="ready",
            workspace=stats.get("workspace"),
            notes=stats.get("notes_total"),
            graph_cached=stats.get("graph_cached"),
        )
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        detail.update(status="unavailable", error=f"{type(exc).__name__}: {exc}"[:300])
    return detail
