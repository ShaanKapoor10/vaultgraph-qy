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

# Absolute, so the cache lands in backend/.cache no matter the working
# directory. This was previously the relative string "backend/.cache", which
# resolved to backend/backend/.cache when anything ran from backend/ — the
# reason an 87MB model tree ended up committed at an unexpected path.
_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"

_model: Any | None = None
_load_failed = False


def get_model() -> Any | None:
    """Load the model once per process. Returns None if unavailable."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, cache_folder=str(_CACHE_DIR))
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
