"""
Hybrid ranking for the sidecar indexes -- raw sessions and source code.

Note search runs inside the database (Neo4j BM25 + vector index, or Postgres
tsvector + pgvector). These indexes are small and live in a plain sidecar
table, so they rank in Python -- with the SAME arithmetic: a lexical order and
a cosine order, fused by Reciprocal Rank Fusion with K=60. Moving something
between the two kinds of index changes where it is stored, never how it ranks.

Loads every row of the index per query. Fine at thousands of rows; ROADMAP
item 8 (an ANN index) is where that stops being true.
"""

from __future__ import annotations

import base64
import math
import re
from collections import Counter
from typing import Any, Callable, Sequence

RRF_K = 60

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def pack(vector: Sequence[float] | None) -> str | None:
    """float32 bytes, base64 -- one TEXT column that SQLite and Postgres both hold."""
    if vector is None:
        return None
    import numpy as np

    return base64.b64encode(np.asarray(vector, dtype="float32").tobytes()).decode("ascii")


def unpack(blob: str | None):
    if not blob:
        return None
    import numpy as np

    return np.frombuffer(base64.b64decode(blob), dtype="float32")


def bm25_order(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[int]:
    terms = set(tokens(query))
    if not terms or not docs:
        return []
    toks = [tokens(d) for d in docs]
    avg = (sum(map(len, toks)) / len(toks)) or 1.0
    df = Counter(t for ts in toks for t in set(ts) if t in terms)
    n = len(docs)
    scored = []
    for i, ts in enumerate(toks):
        tf = Counter(t for t in ts if t in terms)
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(ts) / avg))
        if s > 0:
            scored.append((s, i))
    return [i for _, i in sorted(scored, reverse=True)]


def vector_order(query: str, blobs: list[str | None]) -> list[int]:
    from brahmastra import embeddings
    import numpy as np

    q = embeddings.embed_one(query)
    if q is None:
        return []
    q = np.asarray(q, dtype="float32")
    qn = float(np.linalg.norm(q)) or 1.0
    scored = []
    for i, blob in enumerate(blobs):
        v = unpack(blob)
        if v is not None:
            scored.append((float(q @ v) / (qn * (float(np.linalg.norm(v)) or 1.0)), i))
    return [i for _, i in sorted(scored, reverse=True)]


def rank(query: str, rows: list[dict[str, Any]], text: Callable[[dict], str],
         blob: str = "embedding") -> list[tuple[int, float]]:
    """(row index, fused score), best first."""
    fused: dict[int, float] = {}
    for order in (bm25_order(query, [text(r) for r in rows]),
                  vector_order(query, [r.get(blob) for r in rows])):
        for position, i in enumerate(order):
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(fused.items(), key=lambda x: -x[1])
