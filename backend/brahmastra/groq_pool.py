"""
Several Groq keys, used one after another, and a wait that Groq itself sets.

WHY THIS IS GROQ-SPECIFIC. Groq's free tier fails in a way that is both
frequent and informative. It returns HTTP 429 for two very different limits and
says which in the text -- "on tokens per minute (TPM)" clears in seconds, "on
tokens per day (TPD)" clears in hours -- and it says how long: "Please try again
in 7.5s", "in 1h12m3.36s". A pool built on that can do the right thing for
each: wait the seconds, or move on for the hours. Other providers report limits
differently, or rarely hit them at this scale, so this stays beside the Groq
call sites rather than inside the general provider code.

WHAT HAPPENS ON EACH FAILURE

    401 / invalid key        the key is dead for this process; try the next
    429, per DAY             the key rests for as long as Groq says (default an
                             hour); try the next key immediately
    429, per MINUTE          the key rests for the seconds Groq states; try the
                             next key. If EVERY key is resting briefly, wait
                             for the soonest -- the watcher
    413, retired model       not about the key at all: raised to the caller,
                             which already knows what to do with them
    anything else            transient: retried with backoff, a bounded number
                             of times

When every key is resting for longer than a short wait, `LLMQuotaExhausted` is
raised with each key's state, so the pipeline stops the run rather than grinding
through notes that will all fail -- the behaviour `run_extraction` already has
for a single spent key.

ONE ACCOUNT, SEVERAL KEYS. Groq applies its limits per ORGANIZATION, and its
429s name the organization. Two keys on one account share one daily budget, so
rotating between them does not double it. Measured when this was written: the
four configured keys' requests-per-day counters fell in pairs, which reads as
two accounts of two keys each. The pool does not need to know -- it finds a
shared exhaustion by trying, at the cost of one failed call.

KEYS. `GROQ_API_KEYS` (comma-separated, tried in order), falling back to
`GROQ_API_KEY`. Keys are never logged in full: `mask()` keeps the first eight
characters, which is enough to tell them apart and useless to anyone else.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# A daily cap with no stated wait still clears eventually. An hour is long
# enough not to hammer it and short enough that a long-running process notices.
DAILY_DEFAULT_REST = 3600.0

# Transient errors -- 5xx, a dropped connection -- get this many attempts in
# total across the pool, so a flapping network cannot loop forever.
TRANSIENT_ATTEMPTS = 3

# How many times one call may wait out a per-minute limit on EVERY key. A limit
# still shut after two of Groq's own stated waits is not congestion that a
# third will clear -- something else is spending the minute -- and the note is
# retried on the next run for free.
MAX_WAITS = 2

_DURATION = re.compile(
    r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?",
    re.IGNORECASE,
)
_ORG = re.compile(r"organization `([^`]+)`")


def parse_wait(message: str) -> float | None:
    """
    Seconds Groq asked us to wait, or None if it did not say.

    Handles every shape seen from it: "7.5s", "1m14.2s", "1h12m3.36s", "450ms"
    is not one of them (Groq does not use milliseconds in this message). The
    older regex in llm.py matched none of the hour forms -- harmless there,
    because a daily cap raised immediately, but a pool that rests a key has to
    know whether the rest is seconds or hours.
    """
    match = _DURATION.search(message or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return (float(hours or 0) * 3600 + float(minutes or 0) * 60
            + float(seconds or 0))


def mask(key: str) -> str:
    return (key[:8] + "…") if key else "(none)"


def classify(exc: Exception) -> str:
    """What kind of failure this is, for the pool's purposes."""
    from brahmastra.llm import _is_model_missing, _is_quota_exhausted

    text = str(exc).lower()
    if _is_model_missing(exc):
        return "model"
    if "413" in text or "request too large" in text:
        # Groq words a per-minute limit as a 413 when one request is large:
        # "Request too large ... on tokens per minute (TPM): Limit 8000,
        # Requested 6000". The numbers decide which it is -- extraction's
        # `_is_too_large` already reads them -- and a request that fits once
        # the minute rolls over is a wait, not a failure.
        from brahmastra.extraction import _is_too_large

        return "too_large" if _is_too_large(exc) else "minute"
    if ("401" in text or "invalid_api_key" in text or "invalid api key" in text
            or "authentication" in text):
        return "invalid"
    if _is_quota_exhausted(exc):
        return "daily"
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return "minute"
    return "transient"


@dataclass
class KeyState:
    key: str
    dead: bool = False
    resting_until: float = 0.0
    reason: str = ""
    organization: str = ""
    calls: int = 0
    failures: int = 0

    def ready(self, now: float) -> bool:
        return not self.dead and now >= self.resting_until

    def describe(self, now: float) -> dict[str, Any]:
        if self.dead:
            state = "dead"
        elif now < self.resting_until:
            state = f"resting {self.resting_until - now:.0f}s"
        else:
            state = "ready"
        return {"key": mask(self.key), "state": state, "reason": self.reason,
                "organization": self.organization, "calls": self.calls,
                "failures": self.failures}


@dataclass
class GroqKeyPool:
    keys: list[str]
    states: list[KeyState] = field(default_factory=list)
    _next: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _clients: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seen: list[str] = []
        for key in self.keys:
            key = key.strip()
            if key and key not in seen:
                seen.append(key)
        self.keys = seen
        self.states = [KeyState(k) for k in seen]

    # -- choosing a key ------------------------------------------------------

    def _pick(self, now: float) -> KeyState | None:
        """The next ready key, round-robin, so load spreads across accounts."""
        with self._lock:
            n = len(self.states)
            for step in range(n):
                state = self.states[(self._next + step) % n]
                if state.ready(now):
                    self._next = (self._next + step + 1) % n
                    return state
        return None

    def _soonest_rest(self, now: float) -> float | None:
        waits = [s.resting_until - now for s in self.states if not s.dead]
        return min(waits) if waits else None

    def _client(self, key: str) -> Any:
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = make_client(key)
        return client

    def _rest(self, state: KeyState, seconds: float, reason: str,
              exc: Exception) -> None:
        with self._lock:
            state.resting_until = max(state.resting_until, time.monotonic() + seconds)
            state.reason = reason
            state.failures += 1
            org = _ORG.search(str(exc))
            if org:
                state.organization = org.group(1)

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [s.describe(now) for s in self.states]

    # -- calling ------------------------------------------------------------

    def call(self, fn: Callable[[Any], T], *, max_wait: float | None = None) -> T:
        """
        Run `fn(client)` on a ready key, rotating and waiting as Groq directs.

        `max_wait` bounds a single WATCHER wait -- the sleep taken when every
        key is resting briefly. A longer rest than that means the budget is
        genuinely spent for now, and `LLMQuotaExhausted` says so.
        """
        from brahmastra.llm import (LLMQuotaExhausted, LLMUnavailable,
                                    max_backoff)

        if not self.states:
            raise LLMUnavailable("no Groq key configured: set GROQ_API_KEYS "
                                 "or GROQ_API_KEY in backend/.env")
        limit = max_backoff() if max_wait is None else max_wait
        transient_left = TRANSIENT_ATTEMPTS
        waits_left = MAX_WAITS
        attempts = 0
        last: Exception | None = None

        # Enough turns for every key to fail once in every way, plus waits.
        for _ in range(len(self.states) * 4 + TRANSIENT_ATTEMPTS + MAX_WAITS + 2):
            now = time.monotonic()
            state = self._pick(now)
            if state is None:
                soonest = self._soonest_rest(now)
                if soonest is not None and soonest <= limit and waits_left > 0:
                    # THE WATCHER. Every live key is inside a short per-minute
                    # window Groq told us the length of; wait it out rather
                    # than fail work that will succeed in seconds.
                    waits_left -= 1
                    time.sleep(max(soonest, 0.0) + 0.1)
                    # We waited as long as we were told, so the keys whose
                    # window was that short are ready now -- decided by what
                    # was waited, not by re-reading the clock, which a test (or
                    # a suspended laptop) may not have moved.
                    with self._lock:
                        for other in self.states:
                            if not other.dead and other.resting_until <= now + soonest + 0.1:
                                other.resting_until = 0.0
                    continue
                if soonest is not None and soonest <= limit:
                    # Short rests, but the wait budget is spent. NOT a quota
                    # failure: raising LLMQuotaExhausted would stop the whole
                    # extraction run over a per-minute window.
                    raise LLMUnavailable(
                        f"Groq request failed after {attempts} attempts: {last}")
                detail = "; ".join(
                    f"{d['key']} {d['state']}{' (' + d['reason'] + ')' if d['reason'] else ''}"
                    for d in self.status())
                # The last underlying message is kept in the text on purpose:
                # run_extraction recognises a spent quota by its wording
                # ("tokens per day"), and must go on doing so.
                raise LLMQuotaExhausted(
                    f"all {len(self.states)} Groq keys exhausted -- {detail}. "
                    f"Last error: {last}")

            try:
                attempts += 1
                with self._lock:
                    state.calls += 1
                return fn(self._client(state.key))
            except Exception as exc:              # noqa: BLE001
                last = exc
                kind = classify(exc)
                if kind in ("model", "too_large"):
                    raise
                if kind == "invalid":
                    with self._lock:
                        state.dead = True
                        state.reason = "invalid key"
                        state.failures += 1
                    continue
                if kind == "daily":
                    self._rest(state, parse_wait(str(exc)) or DAILY_DEFAULT_REST,
                               "daily cap", exc)
                    continue
                if kind == "minute":
                    from brahmastra.llm import retry_delay

                    self._rest(state, parse_wait(str(exc)) or retry_delay(exc, 0),
                               "per-minute limit", exc)
                    continue
                transient_left -= 1
                if transient_left <= 0:
                    raise LLMUnavailable(
                        f"Groq request failed after {attempts} attempts: {exc}") from exc
                from brahmastra.llm import retry_delay

                time.sleep(retry_delay(exc, TRANSIENT_ATTEMPTS - transient_left - 1))

        raise LLMUnavailable(f"Groq request did not settle: {last}")


# -- the process-wide pool ---------------------------------------------------
#
# One per process, so a key found spent by extraction is also skipped by
# GraphRAG and the cluster summaries a second later. Rebuilt when the configured
# keys change, so a key added to .env and reloaded is picked up.

_pool: GroqKeyPool | None = None
_pool_keys: tuple[str, ...] = ()
_pool_lock = threading.Lock()


def configured_keys() -> list[str]:
    """GROQ_API_KEYS, then GROQ_API_KEY -- de-duplicated, order kept."""
    import os

    listed = [k.strip() for k in (os.environ.get("GROQ_API_KEYS") or "").split(",")]
    single = (os.environ.get("GROQ_API_KEY") or "").strip()
    keys: list[str] = []
    for key in listed + [single]:
        if key and key not in keys:
            keys.append(key)
    return keys


def make_client(key: str) -> Any:
    """A Groq client for one key. Separate so tests can supply a fake."""
    from groq import Groq

    return Groq(api_key=key)


def pool() -> GroqKeyPool:
    global _pool, _pool_keys
    keys = tuple(configured_keys())
    with _pool_lock:
        if _pool is None or keys != _pool_keys:
            _pool = GroqKeyPool(list(keys))
            _pool_keys = keys
        return _pool


def reset() -> None:
    """Forget every key's state. For tests, and after rotating keys by hand."""
    global _pool, _pool_keys
    with _pool_lock:
        _pool = None
        _pool_keys = ()


def status() -> list[dict[str, Any]]:
    """Each configured key's state, masked. Never raises."""
    try:
        return pool().status()
    except Exception:
        return []
