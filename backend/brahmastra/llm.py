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
  GROQ_API_KEY / GROQ_MODEL          (default GROQ_DEFAULT_MODEL below)
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL (default "claude-haiku-4-5-20251001")
  OLLAMA_MODEL    (default "qwen2.5:7b-instruct")
  OLLAMA_HOST     (default "http://localhost:11434")

Auto order is cloud-first (groq -> anthropic -> ollama) so the same code
deploys unchanged; set LLM_PROVIDER=ollama locally to stay off the network.
"""

from __future__ import annotations

import json as _json
import sys
import os
import time
import urllib.request
from pathlib import Path

# Load backend/.env so config is present no matter which entrypoint imports
# us (server, CLI, MCP server, a fresh `python -c`).
from brahmastra.env import load_env

load_env()


def _env(name: str, default: str) -> str:
    """
    Read env at call time, not import time, so tests can monkeypatch.

    An EMPTY value counts as unset. os.environ.get(name, default) returns the
    default only when the variable is ABSENT, and docker compose writes
    `GROQ_MODEL: ${GROQ_MODEL:-}` -- present, empty -- so that an operator can
    override it. The container therefore asked Groq for a model named "" and
    got `404 The model `` does not exist`, which reads like a retired model
    rather than an unset one and sent the search to entirely the wrong place.

    Same shape as the dotenv trap in env.py: present-but-empty is not absent,
    and every place that treats the two alike is a defect waiting for a
    deployment to expose it.
    """
    value = os.environ.get(name, "")
    return value.strip() or default


OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_HOST = _env("OLLAMA_HOST", "http://localhost:11434")

# Groq retires hosted models, so this WILL go stale. It was
# llama-3.3-70b-versatile until Groq decommissioned it mid-session: the same
# model served traffic one hour and 404ed the next. Chosen because it is the
# largest current option (131k context) and honours response_format json_object,
# which extraction depends on — qwen3.6-27b does not, it emits reasoning tokens
# and fails JSON validation. Override per deployment with GROQ_MODEL.
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def groq_model() -> str:
    """
    The Groq model every caller must use.

    Provider selection was centralised here so extraction, GraphRAG and cluster
    summaries could never disagree about which provider is live — but the MODEL
    was left hardcoded in each call site, so they disagreed about that instead.
    When Groq retired llama-3.3-70b this module was fixed and extraction kept
    calling the dead model, failing every note while summaries succeeded in the
    same run.
    """
    return _env("GROQ_MODEL", GROQ_DEFAULT_MODEL)


def anthropic_model() -> str:
    """The Anthropic model every caller must use. See groq_model()."""
    return _env("ANTHROPIC_MODEL", ANTHROPIC_DEFAULT_MODEL)

PROVIDERS = ("groq", "anthropic", "ollama")


class LLMUnavailable(RuntimeError):
    """No provider could serve the request. Carries the per-provider reasons."""


class LLMQuotaExhausted(LLMUnavailable):
    """
    The provider's quota is spent and will not recover within this run.

    Distinct from a transient failure because the response differs: a
    per-minute limit clears in seconds and is worth retrying, but a per-DAY
    limit does not. Retrying through it means every remaining call fails too —
    a run that grinds for ten minutes to accomplish nothing.
    """


class LLMModelUnavailable(LLMUnavailable):
    """
    The configured model does not exist on this account.

    Providers retire hosted models. `llama-3.3-70b-versatile` was the default
    here and served traffic one hour, then 404ed the next — it had been
    decommissioned. Retrying cannot fix a model that no longer exists, and the
    raw 404 buried under three attempts reads like a network fault, so this is
    raised immediately with the actual remedy.
    """


def _is_quota_exhausted(exc: Exception) -> bool:
    """
    True for a limit that will not clear during this run.

    Groq reports both per-minute and per-day limits as HTTP 429; only the text
    distinguishes them, so match on the daily wording rather than the status.
    """
    text = str(exc).lower()
    if "429" not in text and "rate_limit" not in text and "rate limit" not in text:
        return False
    return any(
        marker in text
        for marker in ("per day", "tokens per day", "(tpd)", "requests per day", "rpd")
    )


def _is_model_missing(exc: Exception) -> bool:
    """True when the provider says the configured model does not exist."""
    text = str(exc).lower()
    return "404" in text and (
        "does not exist" in text or "model_not_found" in text or "not found" in text
    )


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


def active_model() -> str:
    """
    The model name that a `chat()` right now would actually reach.

    Provider selection and model selection are two different questions and only
    the first one had an answer. Callers that need to adapt to how capable the
    live model is -- see `ingest.assemble.comprehension_strategy` -- were
    otherwise left inferring it from the provider, which says "cloud or local"
    rather than "large or small" and is wrong for anyone running a 70B on their
    own hardware.

    Returns "" rather than raising when nothing is available, because every
    caller of this is making a heuristic choice and none of them should fail
    because a heuristic could not be evaluated.
    """
    try:
        provider = resolve_provider()
    except LLMUnavailable:
        return ""
    if provider == "groq":
        return groq_model()
    if provider == "anthropic":
        return anthropic_model()
    return OLLAMA_MODEL


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

    # A spent daily quota is not "no LLM available" while another provider is
    # sitting there working.
    #
    # LLMQuotaExhausted raises immediately and correctly -- retrying a spent
    # cap is as pointless as retrying a retired model. But the caller above it
    # then failed, and on this system that failure has a shape: comprehension
    # makes two calls per chunk, the cap landed between them, and a meeting was
    # stored with four decisions, four action items and ZERO risks. Half a
    # record, from a machine with a perfectly good local model running.
    #
    # Only on quota, and only to a provider that is actually usable. Not on a
    # transient 429, which the retry loop already handles, and not on a bad
    # request, which would fail identically everywhere. `provider=` pins a
    # choice explicitly, so an explicit pin is never second-guessed.
    if provider is None:
        try:
            return _dispatch(name, system, user, json_mode=json_mode,
                             temperature=temperature, max_tokens=max_tokens,
                             num_ctx=num_ctx, timeout=timeout, retries=retries)
        except LLMQuotaExhausted:
            fallback = _next_usable_provider(name)
            if fallback is None:
                raise
            # Said out loud. A silent downgrade to a smaller model is the kind
            # of change that shows up later as "the extraction got worse" with
            # nothing to explain it.
            print(f"{name} quota exhausted; falling back to {fallback}",
                  file=sys.stderr)
            name = fallback

    return _dispatch(name, system, user, json_mode=json_mode,
                     temperature=temperature, max_tokens=max_tokens,
                     num_ctx=num_ctx, timeout=timeout, retries=retries)


def _next_usable_provider(exclude: str) -> str | None:
    """The next provider that can actually serve a request, or None."""
    status = provider_status()
    return next((n for n in PROVIDERS if n != exclude and status.get(n)), None)


def _dispatch(
    name: str,
    system: str,
    user: str,
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
    num_ctx: int,
    timeout: int,
    retries: int,
) -> str:
    """Send to one named provider. No fallback, no selection."""
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
    model = groq_model()
    kwargs: dict = {
        "model": model,
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
            if _is_quota_exhausted(e):
                # A daily cap will not clear in 2-6 seconds. Backing off here
                # only wastes ~12s per note and still fails.
                raise LLMQuotaExhausted(f"Groq daily quota exhausted: {e}") from e
            if _is_model_missing(e):
                # Retrying a retired model is as pointless as retrying a daily
                # cap, and the raw 404 gives no hint that the fix is one env var.
                raise LLMModelUnavailable(
                    f"Groq model {model!r} is not available on this account. "
                    f"Groq retires hosted models; set GROQ_MODEL in backend/.env to a "
                    f"current one (list them with `client.models.list()`). "
                    f"Default is {GROQ_DEFAULT_MODEL!r}. Original error: {e}"
                ) from e
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
        model=anthropic_model(),
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
