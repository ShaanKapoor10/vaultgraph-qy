"""
Shared sentence embeddings.

One loader for the whole project. Entity resolution already used
all-MiniLM-L6-v2 for mention similarity; semantic search and entity matching
need the same vectors, and loading the model twice would cost a second copy of
the weights in memory.

Model: all-MiniLM-L6-v2, 384 dimensions, L2-normalised so cosine similarity is
a plain dot product. The dimension is baked into the Neo4j vector index, so
changing the model means reindexing — see DIM below.

Everything degrades to None rather than raising: sentence-transformers is
optional, and a machine without it should fall back to lexical search rather
than fail.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# Must match the Neo4j vector index config. all-MiniLM-L6-v2 is 384-dim; if
# EMBEDDING_MODEL is changed to a model of a different width, the indexes must
# be dropped and rebuilt or writes will be rejected.
DIM = int(os.environ.get("EMBEDDING_DIM", "384"))

def cache_dir() -> Path:
    """
    Where the ~88 MB model tree lives.

    Absolute, never relative: this was once the string "backend/.cache", which
    resolved to backend/backend/.cache when anything ran from backend/ — the
    reason a model tree ended up committed at an unexpected path.

    Overridable with BRAHMASTRA_CACHE so a container can bake the model into
    the image at build time. Without that the download happens on the first
    request after every cold start, and fails outright with no network.
    """
    override = os.environ.get("BRAHMASTRA_CACHE")
    return Path(override) if override else Path(__file__).resolve().parent.parent / ".cache"

_model: Any | None = None
_load_failed = False


def embeddings_enabled() -> bool:
    """
    Whether semantic matching is wanted at all.

    Loading torch plus all-MiniLM-L6-v2 costs ~460 MB RSS, measured — which
    OOMs a 512 MB instance at startup rather than degrading. Setting
    EMBEDDINGS_ENABLED=0 keeps the process inside a small instance: entity
    resolution falls back to exact matching plus Jaro-Winkler, so "Sarah" and
    "sarah" still merge, but "payments integration" and "the payments work"
    no longer do. A deliberate, documented downgrade beats a crash loop.
    """
    return os.environ.get("EMBEDDINGS_ENABLED", "1").strip().lower() not in {
        "0", "false", "no",
    }


def get_model() -> Any | None:
    """Load the model once per process. Returns None if unavailable."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    if not embeddings_enabled():
        _load_failed = True
        return None
    try:
        from sentence_transformers import SentenceTransformer

        # Load from the cache WITHOUT asking the Hub whether it is current.
        #
        # The `except` below catches every way loading can fail, and a hang is
        # not one of them. A cached model still triggers an HTTP check against
        # huggingface.co, and on a stalled connection -- not a refused one --
        # that check does not raise, it waits. Whatever called embed() waits
        # with it: a request thread, or an MCP server, which then looks like
        # the whole system has hung rather than one lookup being slow.
        #
        # The model is a fixed 384-dim MiniLM pinned by MODEL_NAME, and
        # changing it means rebuilding every vector index by hand. So there is
        # nothing an update check could usefully tell us, and skipping it costs
        # nothing while removing a network call from the hot path entirely.
        try:
            _model = SentenceTransformer(
                MODEL_NAME, cache_folder=str(cache_dir()), local_files_only=True
            )
            return _model
        except TypeError:
            pass          # older sentence-transformers without the argument
        except Exception:
            pass          # not cached yet — fall through and fetch it

        _model = SentenceTransformer(MODEL_NAME, cache_folder=str(cache_dir()))
        return _model
    except Exception:
        # Missing package, no disk space, offline on first download — all mean
        # the same thing to callers: fall back to lexical matching.
        _load_failed = True
        return None


def available() -> bool:
    return get_model() is not None


def embed(texts: list[str]) -> list[list[float]] | None:
    """
    Embed a batch, L2-normalised. Returns None if the model is unavailable.

    Returns plain lists rather than a numpy array because these go straight
    into Cypher parameters, and the driver cannot serialise numpy types.
    """
    if not texts:
        return []
    model = get_model()
    if model is None:
        return None
    vecs = model.encode(texts, normalize_embeddings=True)
    return [[float(x) for x in v] for v in vecs]


def embed_one(text: str) -> list[float] | None:
    """Embed a single string. None if unavailable or the text is empty."""
    if not (text or "").strip():
        return None
    out = embed([text])
    return out[0] if out else None
