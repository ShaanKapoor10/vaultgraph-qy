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
_ENV = Path(__file__).resolve().parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass  # python-dotenv not installed — rely on shell env

from typing import Any

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from brahmastra.auth import auth_status, require_api_key
from brahmastra.db import init_db
from brahmastra.routers import notes, pipeline, graph, ask, paths, workspaces


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise SQLite schema on startup."""
    init_db()
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

# Remote MCP. The same tools a local stdio server exposes, over HTTP, mounted
# inside this app on purpose: it inherits the auth middleware above, so there
# is ONE authentication boundary rather than a second unprotected door into the
# same graph. Enabled only when asked for, because a local stdio server needs
# nothing of the sort.
if os.environ.get("MCP_HTTP", "").strip().lower() in {"1", "true", "yes"}:
    from brahmastra.mcp_server import mcp as _mcp

    app.mount("/mcp", _mcp.streamable_http_app())

# Register routers
app.include_router(notes.router)
app.include_router(pipeline.router)
app.include_router(graph.router)
app.include_router(ask.router)
app.include_router(paths.router)
app.include_router(workspaces.router)


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
    from brahmastra.stores import backend_name

    detail: dict[str, Any] = {
        "service": "brahmastra",
        "backend": backend_name(),
        # Visible on purpose: "unconfigured" means the API is refusing all
        # traffic, and "anonymous" means it is open to anyone who can reach it.
        "auth": auth_status(),
    }
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
