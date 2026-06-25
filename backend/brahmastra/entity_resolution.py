"""
Stage 4 — Entity Resolution.

Algorithm:
1. Collect all unique entity mentions from raw_triples (subject_text + object_text).
2. Compute pairwise similarity using a cascade of heuristics:
     a. Exact match (after normalisation)               → sim = 1.0
     b. Token-subset match (one name is subset of other) → sim = 0.9
     c. Acronym expansion                               → sim = 0.88
     d. Jaro-Winkler string distance                    → sim if ≥ threshold
     e. Cosine similarity of sentence-transformers embeddings → sim if ≥ threshold
3. Feed candidate pairs (sim ≥ MERGE_THRESHOLD) into Union-Find.
4. Each component becomes an entity cluster; the canonical name is the
   longest / most-specific mention in the cluster.
5. Write canonical_map + entity_clusters to SQLite.

sentence-transformers is imported lazily — if unavailable (e.g. first run
before the model downloads), heuristics-only mode is used automatically.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

# Quiet the noisy HF / transformers output ("unauthenticated requests to HF Hub",
# "Loading weights 100%") emitted when the sentence-transformers model loads.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
for _noisy in ("transformers", "sentence_transformers", "huggingface_hub"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

from brahmastra import db

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

JARO_THRESHOLD = 0.92          # jellyfish jaro_winkler_similarity
EMBEDDING_THRESHOLD = 0.82     # cosine similarity of sentence-transformer embeddings
MERGE_THRESHOLD = 0.85         # minimum sim to merge two mentions


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalise(text).split())


# ---------------------------------------------------------------------------
# Contrast guard — block merging entities distinguished only by antonym tokens
# (e.g. "Brahmastra backend" vs "Brahmastra frontend" embed at ~0.94 but are
#  distinct entities). Without this, embedding similarity over-merges them.
# ---------------------------------------------------------------------------

_CONTRAST_GROUPS: list[set[str]] = [
    {"backend", "frontend"},
    {"client", "server"},
    {"input", "output"},
    {"read", "write"},
    {"source", "target"},
    {"public", "private"},
    {"internal", "external"},
    {"dev", "development", "prod", "production", "staging", "test"},
    {"request", "response"},
    {"get", "set", "post", "put", "delete", "patch"},
    {"open", "close"},
    {"start", "stop", "end"},
    {"min", "max"},
    {"upload", "download"},
    {"encode", "decode"},
]


def _is_contrasting(a: str, b: str) -> bool:
    """True if a and b differ only by tokens that are known contrasts/antonyms."""
    ta, tb = _tokens(a), _tokens(b)
    only_a, only_b = ta - tb, tb - ta
    if len(only_a) == 1 and len(only_b) == 1:
        x, y = next(iter(only_a)), next(iter(only_b))
        for grp in _CONTRAST_GROUPS:
            if x in grp and y in grp:
                return True
    return False


# ---------------------------------------------------------------------------
# Acronym detection
# ---------------------------------------------------------------------------

def _is_acronym_of(short: str, full: str) -> bool:
    """Return True if `short` (uppercased) is an acronym formed from `full`."""
    if not short.isupper() or len(short) < 2:
        return False
    words = [w for w in _normalise(full).split() if w]
    if len(words) != len(short):
        return False
    return all(w[0] == c for w, c in zip(words, short.lower()))


# ---------------------------------------------------------------------------
# Pairwise heuristic similarity
# ---------------------------------------------------------------------------

def _heuristic_sim(a: str, b: str) -> tuple[float, str]:
    """
    Return (similarity, method_name).
    Returns (0.0, "none") if no heuristic triggers.
    """
    na, nb = _normalise(a), _normalise(b)

    # 1. Exact after normalisation
    if na == nb:
        return 1.0, "exact"

    # 2. Token subset (one is fully contained in the other)
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        if ta.issubset(tb) or tb.issubset(ta):
            score = min(len(ta), len(tb)) / max(len(ta), len(tb))
            if score >= 0.5:      # avoid merging single-token names too aggressively
                return 0.9 * score + 0.1, "token_subset"

    # 3. Acronym expansion
    if _is_acronym_of(a, b) or _is_acronym_of(b, a):
        return 0.88, "acronym"

    # 4. Jaro-Winkler
    try:
        import jellyfish
        jw = jellyfish.jaro_winkler_similarity(na, nb)
        if jw >= JARO_THRESHOLD:
            return float(jw), "jaro_winkler"
    except ImportError:
        pass

    return 0.0, "none"


# ---------------------------------------------------------------------------
# Embedding-based similarity (lazy load)
# ---------------------------------------------------------------------------

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("all-MiniLM-L6-v2", cache_folder="backend/.cache")
        return _embedder
    except Exception:
        return None


def _embedding_sim(mentions: list[str]) -> dict[tuple[str, str], float]:
    """
    Compute pairwise cosine similarity for all mention pairs.
    Returns a dict {(a, b): sim} for pairs whose sim ≥ EMBEDDING_THRESHOLD.
    Returns {} if sentence-transformers unavailable.
    """
    model = _get_embedder()
    if model is None or len(mentions) < 2:
        return {}

    try:
        import numpy as np

        embeddings = model.encode(mentions, normalize_embeddings=True)
        # Cosine similarity = dot product when embeddings are L2-normalised
        sim_matrix = embeddings @ embeddings.T

        result: dict[tuple[str, str], float] = {}
        n = len(mentions)
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sim_matrix[i, j])
                if s >= EMBEDDING_THRESHOLD:
                    result[(mentions[i], mentions[j])] = s
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent: dict[str, str] = {x: x for x in items}
        self._rank: dict[str, int] = {x: 0 for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = {}
        for x in self._parent:
            root = self.find(x)
            groups.setdefault(root, []).append(x)
        return list(groups.values())


# ---------------------------------------------------------------------------
# Canonical name selection
# ---------------------------------------------------------------------------

def _pick_canonical(mentions: list[str]) -> str:
    """
    Pick the best canonical name from a cluster:
    - Prefer title-cased names (likely proper nouns).
    - Among those, pick the longest (most specific).
    """
    titled = [m for m in mentions if m and m[0].isupper()]
    pool = titled if titled else mentions
    return max(pool, key=len)


# ---------------------------------------------------------------------------
# Edge list for the entity resolution panel
# ---------------------------------------------------------------------------

def _build_merge_edges(
    mentions: list[str],
    heuristic_pairs: list[tuple[str, str, float, str]],
    embedding_pairs: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    """Collect all pairs that were merged, with their similarity and method."""
    edges = []
    seen: set[tuple[str, str]] = set()

    for a, b, sim, method in heuristic_pairs:
        key = (min(a, b), max(a, b))
        if key not in seen:
            seen.add(key)
            edges.append({"a": a, "b": b, "similarity": round(sim, 3), "method": method})

    for (a, b), sim in embedding_pairs.items():
        key = (min(a, b), max(a, b))
        if key not in seen:
            seen.add(key)
            edges.append({"a": a, "b": b, "similarity": round(sim, 3), "method": "embedding"})

    return edges


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_resolution() -> dict[str, Any]:
    """
    Resolve entity mentions across all raw triples.
    Writes canonical_map + entity_clusters to SQLite.
    Returns a summary dict.
    """
    triples = db.get_all_triples()
    if not triples:
        return {"clusters": 0, "mentions": 0, "merge_edges": 0, "embedding_used": False}

    # 1. Collect unique mentions
    raw_mentions: set[str] = set()
    for t in triples:
        if t["subject_text"]:
            raw_mentions.add(t["subject_text"].strip())
        if t["object_text"]:
            raw_mentions.add(t["object_text"].strip())

    mentions = [m for m in raw_mentions if m]
    uf = _UnionFind(mentions)

    # 2. Heuristic pairs
    heuristic_merged: list[tuple[str, str, float, str]] = []
    n = len(mentions)
    for i in range(n):
        for j in range(i + 1, n):
            sim, method = _heuristic_sim(mentions[i], mentions[j])
            if sim >= MERGE_THRESHOLD:
                uf.union(mentions[i], mentions[j])
                heuristic_merged.append((mentions[i], mentions[j], sim, method))

    # 3. Embedding pairs (skip antonym/contrast pairs that embed deceptively high)
    raw_embedding_pairs = _embedding_sim(mentions)
    embedding_pairs = {
        (a, b): sim for (a, b), sim in raw_embedding_pairs.items()
        if not _is_contrasting(a, b)
    }
    embedding_used = bool(embedding_pairs)
    for (a, b), sim in embedding_pairs.items():
        uf.union(a, b)

    # 4. Build cluster list
    components = uf.components()
    clusters: list[dict[str, Any]] = []
    for i, component in enumerate(components):
        canonical = _pick_canonical(component)
        cluster_id = f"c{i:04d}"
        clusters.append({
            "cluster_id": cluster_id,
            "canonical_name": canonical,
            "mentions": sorted(component),
        })

    # 5. Persist
    db.replace_canonical_map(clusters)

    merge_edges = _build_merge_edges(mentions, heuristic_merged, embedding_pairs)

    return {
        "clusters": len(clusters),
        "mentions": len(mentions),
        "merge_edges": len(merge_edges),
        "embedding_used": embedding_used,
        "details": {
            "clusters": clusters,
            "merge_edges": merge_edges,
        },
    }
