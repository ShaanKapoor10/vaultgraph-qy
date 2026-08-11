"""
Pluggable LLM layer.

One place decides *which* provider runs; callers decide *how* they prompt.
Before this module, provider selection was duplicated in extraction.py and
this file only spoke Ollama — so a deployed backend (no local Ollama) kept
extracting but silently lost GraphRAG answers and cluster summaries.

Providers:
  groq      — cloud, default. Deployable; free tier is rate limited (~12k TPM).
  ollama    — local, free, no rate limits. The pluggable local option.
  anthropic — cloud fallback.

Config (backend/.env):
  LLM_PROVIDER    "groq" | "ollama" | "anthropic"; unset = auto
  GROQ_API_KEY / GROQ_MODEL          (default "llama-3.3-70b-versatile")
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL (default "claude-haiku-4-5-20251001")
  OLLAMA_MODEL    (default "qwen2.5:7b-instruct")
  OLLAMA_HOST     (default "http://localhost:11434")

Auto order is cloud-first (groq -> anthropic -> ollama) so the same code
deploys unchanged; set LLM_PROVIDER=ollama locally to stay off the network.
"""

from __future__ import annotations

import json as _json
import os
import time
import urllib.request
from pathlib import Path

# Load backend/.env so config is present no matter which entrypoint imports
# us (server, CLI, MCP server, a fresh `python -c`).
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass


def _env(name: str, default: str) -> str:
    """Read env at call time, not import time, so tests can monkeypatch."""
    return os.environ.get(name, default)


OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_HOST = _env("OLLAMA_HOST", "http://localhost:11434")

PROVIDERS = ("groq", "anthropic", "ollama")


class LLMUnavailable(RuntimeError):
    """No provider could serve the request. Carries the per-provider reasons."""


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def ollama_available() -> bool:
    """Return True if a local Ollama server is reachable."""
    try:
        host = _env("OLLAMA_HOST", OLLAMA_HOST)
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _installed(module: str) -> bool:
    """True if an SDK is importable, without paying the import cost."""
    from importlib.util import find_spec
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def provider_status() -> dict[str, bool]:
    """
    Which providers are usable right now. Cheap except for the Ollama ping.

    A cloud provider needs BOTH its key and its SDK: with only the key we
    would select it and then fail at call time instead of falling through
    to a provider that actually works.
    """
    return {
        "groq": bool(_env("GROQ_API_KEY", "")) and _installed("groq"),
        "anthropic": bool(_env("ANTHROPIC_API_KEY", "")) and _installed("anthropic"),
        "ollama": ollama_available(),
    }


def resolve_provider() -> str:
    """
    Decide which provider to use.

    An explicit LLM_PROVIDER wins, but only if it is actually usable — a
    stale LLM_PROVIDER=ollama pointing at a dead server should fall through
    to the cloud rather than fail the run.
    """
    status = provider_status()
    requested = _env("LLM_PROVIDER", "").lower().strip()

    if requested in PROVIDERS and status[requested]:
        return requested

    for name in PROVIDERS:  # groq -> anthropic -> ollama
        if status[name]:
            return name

    raise LLMUnavailable(
        "No LLM provider available. Set GROQ_API_KEY or ANTHROPIC_API_KEY in "
        "backend/.env, or start a local model with `ollama serve`."
    )


def llm_available() -> bool:
    """True if any provider can serve a request. For fail-soft callers."""
    return any(provider_status().values())


# ---------------------------------------------------------------------------
# Unified chat
# ---------------------------------------------------------------------------

def chat(
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    num_ctx: int = 8192,
    timeout: int = 240,
    retries: int = 3,
    provider: str | None = None,
) -> str:
    """
    Send a system+user prompt to the configured provider, return the reply text.

    json_mode asks the provider for valid JSON (native on Ollama and Groq).
    Pass `provider` to pin one explicitly; otherwise resolve_provider() picks.
    Raises LLMUnavailable if nothing can serve the request.
    """
    name = provider or resolve_provider()

    if name == "ollama":
        return ollama_chat(
            system, user, json_mode=json_mode, temperature=temperature,
            num_ctx=num_ctx, timeout=timeout, retries=retries,
        )
    if name == "groq":
        return _groq_chat(
            system, user, json_mode=json_mode, temperature=temperature,
            max_tokens=max_tokens, retries=retries,
        )
    if name == "anthropic":
        return _anthropic_chat(
            system, user, temperature=temperature, max_tokens=max_tokens,
        )
    raise LLMUnavailable(f"Unknown provider {name!r}; expected one of {PROVIDERS}")


def _groq_chat(
    system: str,
    user: str,
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
    retries: int,
) -> str:
    try:
        from groq import Groq
    except ImportError as e:
        raise LLMUnavailable(
            "groq package not installed — run: uv pip install groq"
        ) from e

    client = Groq(api_key=_env("GROQ_API_KEY", ""))
    kwargs: dict = {
        "model": _env("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    # The free tier is rate limited (~12k TPM); back off rather than fail the
    # whole pipeline run on a burst.
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s

    raise LLMUnavailable(f"Groq request failed after {retries} attempts: {last_err}")


def _anthropic_chat(
    system: str,
    user: str,
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailable(
            "anthropic package not installed — run: uv pip install anthropic"
        ) from e

    client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY", ""))
    resp = client.messages.create(
        model=_env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


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

    Retries with linear backoff to survive transient connection drops (Ollama
    can close a socket while loading the model into VRAM).
    """
    host = _env("OLLAMA_HOST", OLLAMA_HOST)
    payload: dict = {
        "model": _env("OLLAMA_MODEL", OLLAMA_MODEL),
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
            f"{host}/api/chat",
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

    raise LLMUnavailable(
        f"Ollama request failed after {retries} attempts "
        f"({_env('OLLAMA_MODEL', OLLAMA_MODEL)} @ {host}): {last_err}"
    )
