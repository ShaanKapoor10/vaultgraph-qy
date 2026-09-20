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
import json
import os
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


def load(key: str) -> str | None:
    """
    A previously cached raw reply, or None. Never raises.

    The RAW reply, not a parsed payload and not verified artifacts: parsing and
    the grounding checks re-run on every hit, so a fixed bug in them applies to
    cached passages too. Caching after verification would freeze a defence in
    place, which is the opposite of what this system needs.
    """
    if not enabled():
        return None
    try:
        from brahmastra.ingest.store import get_ingest_store
        return get_ingest_store().get_comprehension(key)
    except Exception:
        return None


def save(key: str, reply: str) -> None:
    """Cache a raw reply. Never raises: failing to cache is not failing."""
    if not enabled() or not reply:
        return
    try:
        from brahmastra.ingest.store import get_ingest_store
        get_ingest_store().save_comprehension(key, reply)
    except Exception:
        pass
