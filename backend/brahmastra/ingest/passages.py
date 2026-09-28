"""
The raw transcript, searchable -- what was actually said, by whom and when.

Step 1 of the grounded-transcript plan. Everything else a transcript becomes
passes through a model: items, key points, part notes, then graph triples
extracted from those notes. Each step keeps less, and the end-to-end evaluation
(qa_eval.py) put a number on it. On the Q3 planning meeting, questions about
detail no item carries were answered 2 of 5 times from the graph and 5 of 5
from the transcript itself: "sixty percent done, card flow finished" and
"finance needs reconciliation for the audit" were said, and never reached the
graph at all.

So the transcript is indexed as it is, with no model in the loop -- the same
choice, for the same reason, as the raw session index (sessions.py), which
this mirrors: a raw index cannot hallucinate and does not truncate.

THE UNIT is a PASSAGE: consecutive whole turns, up to the embedding model's
window (all-MiniLM-L6-v2 reads ~1,000 characters; past that is invisible to
the vector half). Every line keeps its timestamp and speaker, so a hit can be
cited as "Mei, 00:00:44". A single turn longer than the window is split at
sentences, each piece re-stating who is speaking.

SECRETS ARE REDACTED before storage, with the session index's patterns: a
meeting can have a key read aloud or pasted into its chat as easily as a
coding session can.

Search is hybrid -- BM25 fused with cosine by RRF, K=60 -- the same arithmetic
as note and session search.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from brahmastra import hybrid
from brahmastra.sidecar import SidecarStore

PASSAGE_CHARS = 900
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Passage:
    key: str
    start_time: str | None
    end_time: str | None
    speakers: list[str]
    text: str

    @property
    def fp(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


def _lines(turns: Iterable[Any]) -> Iterable[tuple[Any, str]]:
    """Each turn as rendered lines no longer than a passage."""
    for turn in turns:
        line = turn.rendered
        if len(line) <= PASSAGE_CHARS:
            yield turn, line
            continue
        head = line[: len(line) - len(turn.text)]          # "[ts] Speaker: "
        part = ""
        for sentence in _SENTENCE.split(turn.text):
            if part and len(head) + len(part) + len(sentence) + 1 > PASSAGE_CHARS:
                yield turn, head + part
                part = ""
            part = f"{part} {sentence}".strip()
        if part:
            yield turn, head + part


def passages(turns: list[Any]) -> list[Passage]:
    """Consecutive whole turns, packed to the embedding window."""
    from brahmastra.sessions import redact

    out: list[Passage] = []
    lines: list[tuple[Any, str]] = []

    def close() -> None:
        if not lines:
            return
        first, last = lines[0][0], lines[-1][0]
        speakers = list(dict.fromkeys(t.speaker for t, _ in lines if t.speaker))
        text = redact("\n".join(line for _, line in lines))
        # Keyed by position in the transcript AND ordinal: stable across runs
        # for the same text, which is what lets re-indexing skip the unchanged.
        out.append(Passage(f"{first.start_char}-{last.end_char}:{len(out)}",
                           first.timestamp, last.timestamp, speakers, text))
        lines.clear()

    size = 0
    for turn, line in _lines(turns):
        if lines and size + len(line) + 1 > PASSAGE_CHARS:
            close()
            size = 0
        lines.append((turn, line))
        size += len(line) + 1
    close()
    return out


class PassageIndex(SidecarStore):
    """One row per passage; the transcript owns its rows, as a session does."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS transcript_passages (
        workspace_id   TEXT NOT NULL DEFAULT 'default',
        transcript_id  TEXT NOT NULL,
        passage_key    TEXT NOT NULL,
        title          TEXT,
        occurred_at    TEXT,
        start_time     TEXT,
        end_time       TEXT,
        speakers       TEXT,
        text           TEXT NOT NULL,
        fp             TEXT NOT NULL,
        embedding      TEXT,
        indexed_at     TEXT NOT NULL,
        PRIMARY KEY (workspace_id, transcript_id, passage_key)
    );
    CREATE INDEX IF NOT EXISTS idx_transcript_passages_transcript
        ON transcript_passages (workspace_id, transcript_id)
    """

    def stored(self, transcript_id: str) -> dict[str, dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT passage_key, fp FROM transcript_passages "
                "WHERE workspace_id = ? AND transcript_id = ?"),
                (self.workspace, transcript_id))
            return {r["passage_key"]: r for r in map(self._dict, cur.fetchall())}

    def upsert(self, transcript_id: str, title: str, occurred_at: str | None,
               rows: list[tuple[Passage, str | None]]) -> None:
        if not rows:
            return
        self.init_schema()
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "INSERT INTO transcript_passages (workspace_id, transcript_id, passage_key, "
                "title, occurred_at, start_time, end_time, speakers, text, fp, embedding, "
                "indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, transcript_id, passage_key) DO UPDATE SET "
                "title = excluded.title, occurred_at = excluded.occurred_at, "
                "start_time = excluded.start_time, end_time = excluded.end_time, "
                "speakers = excluded.speakers, text = excluded.text, fp = excluded.fp, "
                "embedding = excluded.embedding, indexed_at = excluded.indexed_at"),
                [(self.workspace, transcript_id, p.key, title, occurred_at, p.start_time,
                  p.end_time, ", ".join(p.speakers), p.text, p.fp, emb, now)
                 for p, emb in rows])

    def delete(self, transcript_id: str, keys: Iterable[str] | None = None) -> int:
        """Remove some of a transcript's rows, or all of them when `keys` is None."""
        self.init_schema()
        with self._cursor() as cur:
            if keys is None:
                cur.execute(self._ph(
                    "DELETE FROM transcript_passages WHERE workspace_id = ? AND transcript_id = ?"),
                    (self.workspace, transcript_id))
                return int(getattr(cur, "rowcount", 0) or 0)
            keys = list(keys)
            cur.executemany(self._ph(
                "DELETE FROM transcript_passages "
                "WHERE workspace_id = ? AND transcript_id = ? AND passage_key = ?"),
                [(self.workspace, transcript_id, k) for k in keys])
            return len(keys)

    def all(self, transcript_id: str | None = None) -> list[dict[str, Any]]:
        self.init_schema()
        sql = ("SELECT transcript_id, passage_key, title, occurred_at, start_time, end_time, "
               "speakers, text, embedding FROM transcript_passages WHERE workspace_id = ?")
        params: list[Any] = [self.workspace]
        if transcript_id:
            sql += " AND transcript_id = ?"
            params.append(transcript_id)
        with self._cursor() as cur:
            cur.execute(self._ph(sql), tuple(params))
            return [self._dict(r) for r in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT transcript_id, COUNT(*) AS n FROM transcript_passages "
                "WHERE workspace_id = ? GROUP BY transcript_id"), (self.workspace,))
            return {r["transcript_id"]: int(r["n"]) for r in map(self._dict, cur.fetchall())}


def index_transcript(transcript_id: str, title: str, occurred_at: str | None,
                     turns: list[Any], store: PassageIndex | None = None) -> dict[str, Any]:
    """Bring one transcript's passages up to date. Re-embeds only what changed."""
    from brahmastra import embeddings

    store = store or PassageIndex()
    wanted = passages(turns)
    have = store.stored(transcript_id)
    changed = [p for p in wanted if have.get(p.key, {}).get("fp") != p.fp]
    vectors = embeddings.embed([p.text for p in changed]) if changed else []
    if vectors is None:                          # no model: lexical half only
        vectors = [None] * len(changed)
    store.upsert(transcript_id, title, occurred_at,
                 [(p, hybrid.pack(v)) for p, v in zip(changed, vectors)])
    gone = set(have) - {p.key for p in wanted}
    store.delete(transcript_id, gone)
    return {"passages": len(wanted), "embedded": len(changed), "removed": len(gone)}


def search(query: str, limit: int = 6, transcript_id: str | None = None,
           store: PassageIndex | None = None) -> list[dict[str, Any]]:
    """Hybrid search over what was said. Best first."""
    store = store or PassageIndex()
    rows = store.all(transcript_id)
    if not rows or not (query or "").strip():
        return []
    out = []
    for i, score in hybrid.rank(query, rows, text=lambda r: r["text"])[:limit]:
        r = rows[i]
        out.append({"transcript_id": r["transcript_id"], "title": r["title"],
                    "occurred_at": r["occurred_at"], "start_time": r["start_time"],
                    "end_time": r["end_time"], "speakers": r["speakers"],
                    "text": r["text"], "score": round(score, 5)})
    return out


def turns_of(chunks: list[Any]) -> list[Any]:
    """Every turn once, in order. Chunks overlap, so the same turn recurs."""
    seen: set[tuple[int, int]] = set()
    out = []
    for chunk in chunks:
        for turn in chunk.turns:
            key = (turn.start_char, turn.end_char)
            if key not in seen:
                seen.add(key)
                out.append(turn)
    return out


def backfill(workspace: str | None = None) -> dict[str, Any]:
    """
    Index every transcript in a workspace. Idempotent. Speakers are named the
    way ingestion names them (memoised), so no model call is made for a
    transcript that has already been processed.
    """
    from brahmastra.ingest.assemble import _segment_with_speakers
    from brahmastra.ingest.store import get_ingest_store

    ingest = get_ingest_store(workspace)
    index = PassageIndex(workspace=ingest.workspace)
    done: dict[str, Any] = {}
    for row in ingest.list_transcripts(limit=10_000):
        record = ingest.get_transcript(row["id"])
        if not record:
            continue
        turns = turns_of(_segment_with_speakers(record, {}))
        done[row["id"]] = index_transcript(row["id"], record["title"], record.get("occurred_at"),
                                           turns, index)
    return done


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Search what was said in transcripts.")
    parser.add_argument("--backfill", action="store_true", help="index every transcript")
    parser.add_argument("--search", default=None)
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)
    out: Any = {}
    if args.backfill:
        out = backfill(args.workspace)
    elif args.search:
        out = search(args.search, store=PassageIndex(workspace=args.workspace))
    else:
        out = PassageIndex(workspace=args.workspace).counts()
    sys.stdout.buffer.write((json.dumps(out, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
