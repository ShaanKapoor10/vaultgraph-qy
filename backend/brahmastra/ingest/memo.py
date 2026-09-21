"""
Never comprehend the same passage twice.

Comprehension is the only expensive step in ingestion -- one or two LLM calls
per chunk -- and it is also the only one that is a pure function of its inputs.
The same passage, read by the same model under the same prompt, yields the same
reading. So a re-ingestion currently pays full price to learn what it already
knew, and that cost grows with exactly the thing this module was built for:
long documents, re-processed after a correction.

Borrowed from cocoindex, whose whole premise is "Target = F(Source)" and whose
memoisation keys on `hash(input) + hash(code)`. The second half of that key is
the part worth copying deliberately: the cache must be invalidated by a change
to the PROMPT, not only by a change to the text. Prompts here change often --
four comprehension variants exist and each has been rewritten -- so a cache
keyed on the passage alone would confidently serve a reading produced by a
prompt that no longer exists.

WHAT IS IN THE KEY

  the passage text        the input
  the variant name        single, focused, typed ... different readings
  the model               a 7B and a 120B do not read alike
  a digest of the prompts what `hash(code)` means here

WHAT THIS IS NOT
----------------
Not a store of artifacts. It caches the MODEL'S REPLY, before verification, so
that grounding, attribution and consolidation all re-run on a cache hit. Those
are cheap, deterministic, and the part most likely to be improved -- caching
their output would freeze a defence in place and mean a fixed bug stayed fixed
only for new documents.

DERIVED, and disposable. Every row is recomputable by paying the model again.
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from typing import Any

# One place to bump when a change should invalidate every cached reading at
# once -- a verification change does not need it, but a change to how a reply
# is parsed into a payload does.
CACHE_VERSION = "1"


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")          # so ("ab","c") and ("a","bc") differ
    return h.hexdigest()


def key_for(chunk_text: str, variant: str, model: str, prompts: str) -> str:
    """
    The cache key: the input, and the code that reads it.

    `prompts` carries the actual prompt text rather than a version someone has
    to remember to bump. A prompt edited in place would otherwise keep serving
    readings from the prompt it replaced -- which is the failure mode that
    makes a cache worse than no cache.
    """
    return _digest(CACHE_VERSION, variant, model, prompts, chunk_text)


def enabled() -> bool:
    """On unless switched off. INGEST_MEMO=0 forces every chunk to be re-read."""
    return os.environ.get("INGEST_MEMO", "").strip() != "0"


# -- reaching the cache ----------------------------------------------------
#
# A cache whose LOOKUP is expensive is a smaller version of the problem it was
# built to solve, and the first version of this module was exactly that:
# `get_ingest_store()` per call, so every lookup constructed a store with
# `_ready = False` and re-ran the whole schema DDL before the SELECT it wanted.
# Two connections per load, two per save. Measured against the deployed
# arrangement (Postgres on 5433), median over 15:
#
#     IngestStore().init_schema()   15.8 ms    <- paid again on every call
#     warm store.get_comprehension  13.4 ms
#     memo.load()                   35.6 ms
#     memo.save()                   37.4 ms
#
# So most of a cache lookup was the cache setting itself up. On a 40-chunk
# transcript read by the focused variant -- 80 loads and 80 saves -- that is
# 5.8s of bookkeeping, and it scales with document length, which is precisely
# the case this module exists for. SQLite hides it (2.7ms / 8.4ms) and the
# deployment does not use SQLite.

# One store per workspace. It holds no open connection -- `_cursor()` opens one
# per statement -- so keeping it is keeping the `_ready` flag and nothing else.
_stores: dict[str, Any] = {}
_factory: Any = None

# LRU capacity for the in-process layer. Sized for one long document rather
# than for a corpus: this exists so a run does not ask the database twice for a
# reply it is already holding, not to be a second cache.
# Keyed by (workspace, cache_key), not by cache_key alone. A reading does not
# depend on the workspace -- the key already digests the passage, the prompt
# and the model -- so sharing one would be harmless in fact. It is scoped
# anyway because isolation here fails OPEN: every leak this project has had
# came from a filter that was correct to omit right up until it was not, and
# the store layer this sits in front of is workspace-scoped. Matching it costs
# a tuple.
LOCAL_MAX = 256
_local: "OrderedDict[tuple[str, str], str]" = OrderedDict()


def _workspace() -> str:
    from brahmastra.workspace import current_workspace
    return current_workspace()


def _store() -> Any:
    """
    The ingest store, reused across calls.

    Resolved through `brahmastra.ingest.store.get_ingest_store` every time,
    never captured, so a test that replaces that factory is still obeyed -- and
    when it IS replaced the memoised stores are dropped, because they were
    built by a factory that is no longer the one in force. A module-level cache
    that outlived a monkeypatch would make the swap silently ineffective, which
    is the failure this whole module is trying not to reintroduce one layer up.
    """
    global _factory
    from brahmastra.ingest.store import get_ingest_store

    if get_ingest_store is not _factory:
        _stores.clear()
        _factory = get_ingest_store

    workspace = _workspace()
    store = _stores.get(workspace)
    if store is None:
        store = _stores[workspace] = get_ingest_store(workspace)
    return store


def _remember(key: str, reply: str) -> None:
    slot = (_workspace(), key)
    _local[slot] = reply
    _local.move_to_end(slot)
    while len(_local) > LOCAL_MAX:
        _local.popitem(last=False)


def reset() -> None:
    """Drop both layers. For tests, and for a process that changes backend."""
    _stores.clear()
    _local.clear()
    global _factory
    _factory = None


def load(key: str) -> str | None:
    """
    A previously cached raw reply, or None. Never raises.

    The RAW reply, not a parsed payload and not verified artifacts: parsing and
    the grounding checks re-run on every hit, so a fixed bug in them applies to
    cached passages too. Caching after verification would freeze a defence in
    place, which is the opposite of what this system needs.

    Answered from memory first. Chunks overlap on purpose, so one document can
    ask for the same passage twice, and a load straight after a save is a round
    trip for something this process is already holding. A key is a digest of
    the passage, the prompt and the model, so two replies under one key differ
    only by the model's own nondeterminism -- an in-process copy can be older
    than the row, never wrong about which passage it answers.
    """
    if not enabled():
        return None
    slot = (_workspace(), key)
    hit = _local.get(slot)
    if hit is not None:
        _local.move_to_end(slot)
        return hit
    try:
        reply = _store().get_comprehension(key)
    except Exception:
        return None
    if reply:
        _remember(key, reply)
    return reply


def save(key: str, reply: str) -> None:
    """Cache a raw reply. Never raises: failing to cache is not failing."""
    if not enabled() or not reply:
        return
    _remember(key, reply)
    try:
        _store().save_comprehension(key, reply)
    except Exception:
        pass
