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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from brahmastra.db import init_db
from brahmastra.routers import notes, pipeline, graph, ask


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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(notes.router)
app.include_router(pipeline.router)
app.include_router(graph.router)
app.include_router(ask.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "brahmastra"}
