"""
Comprehension's view of the shared reply cache.

The cache itself lives in `brahmastra.memo`, because it is not a transcript
concern: extracting triples from a note has exactly the same shape -- one
expensive call whose answer is a pure function of the text, the prompt and the
model -- and `run_pipeline(full=True)` re-reading every note is the more
expensive of the two. Keeping a second implementation here is how the retry
delay came to exist twice, correct in extraction.py and a blind guess
everywhere else.

So this module is a name and a switch, and nothing else:

  INGEST_MEMO=0   turns the cache off for INGESTION only, leaving note
                  extraction memoised. LLM_MEMO=0 turns it off everywhere.

Everything the cache does, and every decision behind it -- why the prompt is
in the key, why the RAW reply is cached rather than the verified artifacts,
why the store is reused -- is documented in brahmastra/memo.py.
"""

from __future__ import annotations

import os

from brahmastra import memo as _shared

# Re-exported so these constants keep one home.
CACHE_VERSION = _shared.CACHE_VERSION
LOCAL_MAX = _shared.LOCAL_MAX

# What comprehension's rows are called in the shared table, so a cache can be
# counted or cleared by job without knowing anything about keys.
VARIANT = "comprehend"


def key_for(chunk_text: str, variant: str, model: str, prompts: str) -> str:
    """The shared key, unchanged. See brahmastra.memo.key_for."""
    return _shared.key_for(chunk_text, variant, model, prompts)


def enabled() -> bool:
    """
    On unless switched off, by either switch.

    Two switches rather than one because they answer different questions.
    LLM_MEMO=0 says "cache no model replies anywhere", which is what a test
    suite wants. INGEST_MEMO=0 says "re-read every passage" while leaving note
    extraction memoised -- what somebody comparing comprehension variants
    wants, since the thing under measurement must not be served from a cache
    and everything around it still should be.
    """
    if os.environ.get("INGEST_MEMO", "").strip() == "0":
        return False
    return _shared.enabled()


def load(key: str) -> str | None:
    """A previously cached RAW reply, or None. Never raises."""
    if not enabled():
        return None
    return _shared.load(key)


def save(key: str, reply: str) -> None:
    """Cache a raw reply. Never raises: failing to cache is not failing."""
    if not enabled():
        return
    _shared.save(key, reply, VARIANT)


def reset() -> None:
    """Drop both layers of the shared cache."""
    _shared.reset()
