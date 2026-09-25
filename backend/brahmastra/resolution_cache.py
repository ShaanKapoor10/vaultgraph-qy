"""
What the last resolution run decided about each pair -- so the next run only
has to decide about pairs involving a NEW mention.

Measured 2026-09-25 (ROADMAP item 8): resolution is quadratic in mentions and
the two halves cost about the same --

    mentions   embedding   heuristic
      5,000       34 s        27 s
     10,000      115 s       121 s
     20,000      395 s       444 s

-- so an ANN index for the embedding half alone would halve it. But a pair of
mentions that were both present last run was already judged, by functions that
are pure given the key below, so re-judging it is pure waste. A run that adds
twenty mentions to ten thousand compares 20 x 10,000 pairs instead of 50 million.

THE KEY is everything a pair's verdict depends on: the code (version.py's
fingerprint of the whole package), the thresholds, which mentions are meetings
(the meeting guard depends on it), and whether the judge runs and on what. Any
change means a full run -- correctness first, and a full run is always right.

What is NOT recomputed from the key: the judge's verdict on a pair, once given.
A new note can add a sentence about an old name, and asking again might answer
differently -- which is exactly the churn that kept the judge off the first
time. A verdict stands until the key changes. A pair the judge did NOT answer
(outage, quota) is asked again next run.

Derived data: a cache of decisions, rebuilt by any full run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from brahmastra.sidecar import SidecarStore


class ResolutionCache(SidecarStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS resolution_cache (
        workspace_id  TEXT NOT NULL PRIMARY KEY,
        cache_key     TEXT NOT NULL,
        payload       TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """

    def load(self, key: str) -> dict[str, Any] | None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT cache_key, payload FROM resolution_cache WHERE workspace_id = ?"),
                (self.workspace,))
            row = cur.fetchone()
        if not row:
            return None
        row = self._dict(row)
        if row["cache_key"] != key:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def save(self, key: str, payload: dict[str, Any]) -> None:
        self.init_schema()
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "INSERT INTO resolution_cache (workspace_id, cache_key, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (workspace_id) DO UPDATE SET "
                "cache_key = excluded.cache_key, payload = excluded.payload, "
                "updated_at = excluded.updated_at"),
                (self.workspace, key, json.dumps(payload), now))

    def clear(self) -> None:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph("DELETE FROM resolution_cache WHERE workspace_id = ?"),
                        (self.workspace,))


def cache_key(meetings: frozenset[str], judge_on: bool, judge_model: str,
              thresholds: tuple[float, ...]) -> str:
    from brahmastra import version

    parts = [version.LOADED, repr(thresholds), json.dumps(sorted(meetings)),
             str(judge_on), judge_model]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
