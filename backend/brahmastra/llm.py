"""
Shared local-LLM helper.

A single reusable Ollama chat call so features beyond extraction (cluster
summaries, GraphRAG question answering) don't each re-implement the HTTP +
retry logic. Extraction.py keeps its own copy on purpose — it predates this
module and its JSON-mode prompt is tuned; this helper is for new callers.

Config (same env vars as extraction.py):
  OLLAMA_MODEL  (default "qwen2.5:7b-instruct")
  OLLAMA_HOST   (default "http://localhost:11434")
"""

from __future__ import annotations

import json as _json
import os
import time
import urllib.request
from pathlib import Path

# Load backend/.env so OLLAMA_* / LLM config is present no matter which
# entrypoint imports us (server, CLI, fresh `python -c`).
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def ollama_available() -> bool:
    """Return True if a local Ollama server is reachable."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def ollama_chat(
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    num_ctx: int = 8192,
    timeout: int = 240,
    retries: int = 3,
) -> str:
    """
    Call the local Ollama chat endpoint and return the assistant message text.

    json_mode=True forces Ollama to emit valid JSON (use when you will json.loads
    the result). Retries with linear backoff to survive transient connection
    drops (Ollama can close a socket while loading the model into VRAM).

    Raises RuntimeError if all attempts fail.
    """
    payload: dict = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["format"] = "json"

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"]
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s

    raise RuntimeError(
        f"Ollama request failed after {retries} attempts "
        f"({OLLAMA_MODEL} @ {OLLAMA_HOST}): {last_err}"
    )
