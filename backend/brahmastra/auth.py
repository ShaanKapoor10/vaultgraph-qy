"""
API authentication.

The API exposes the entire graph, can spend LLM tokens, and can write into a
Notion workspace. Exposing it unauthenticated is not a degraded mode, it is a
different product.

Deliberately FAILS CLOSED. This codebase has already been bitten twice by
security that fails open — workspace isolation leaked silently when a filter
was forgotten, and CORS was a hardcoded "*" — so the default when nothing is
configured is to refuse, not to allow:

  BRAHMASTRA_API_KEY set          -> require `Authorization: Bearer <key>`
  nothing set                     -> 503 on everything except health probes
  BRAHMASTRA_ALLOW_ANONYMOUS=1    -> open, for local development

Forgetting to configure a deployment therefore breaks it loudly, rather than
publishing the graph to the internet quietly. The one failure mode a personal
deployment cannot afford is the silent one.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request, status
from fastapi.responses import JSONResponse

# Probes must answer before a client could possibly hold a token, and an
# orchestrator's health checker has no way to carry one.
_OPEN_PATHS = frozenset({"/health", "/health/ready"})


def _configured_key() -> str:
    return os.environ.get("BRAHMASTRA_API_KEY", "").strip()


def _anonymous_allowed() -> bool:
    return os.environ.get("BRAHMASTRA_ALLOW_ANONYMOUS", "").strip().lower() in {
        "1", "true", "yes",
    }


def _presented_key(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # MCP clients and simple scripts often find a plain header easier to set
    # than an Authorization header, and both are equally secret over TLS.
    return request.headers.get("x-api-key", "").strip()


def auth_status() -> str:
    """For /health/ready, so a deployment can be seen to be protected."""
    if _configured_key():
        return "enforced"
    return "anonymous" if _anonymous_allowed() else "unconfigured"


async def require_api_key(request: Request, call_next):
    """ASGI middleware enforcing the rules in the module docstring."""
    if request.url.path in _OPEN_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    key = _configured_key()

    if not key:
        if _anonymous_allowed():
            return await call_next(request)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": (
                    "API authentication is not configured. Set BRAHMASTRA_API_KEY "
                    "to require a bearer token, or BRAHMASTRA_ALLOW_ANONYMOUS=1 to "
                    "run without authentication (local development only)."
                )
            },
        )

    # compare_digest: a plain == leaks the key one character at a time to an
    # attacker who can measure response timing.
    if not hmac.compare_digest(_presented_key(request), key):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or missing API key."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await call_next(request)
