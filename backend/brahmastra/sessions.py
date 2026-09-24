"""
Raw session search -- the conversation itself, indexed, with no model in the loop.

checkpoint.py DISTILS a coding session into a note through an LLM. That note is
what the graph needs, and it is lossy on purpose. Two measured reasons it cannot
be the only record:

  * It keeps the TAIL. capture() and drain() both cut a stretch to its last
    MAX_TRANSCRIPT_CHARS (20,000) and delete the queue file once the note is
    stored. This project's one transcript holds ~1.2M characters of
    conversation; everything before the last 20k of a stretch is gone.
  * It can be WRONG. CLAUDE.md records the distiller inventing a whole note --
    a commit, a push, a reply from Shaan -- which is why it now fails closed,
    which in turn means it sometimes stores nothing.

cocoindex's `entire_session_search` indexes the raw per-turn transcript,
embedded, and finds "how did I fix the auth bug" by meaning. Adopted in that
shape because the raw index cannot hallucinate and does not truncate -- not
because it was theirs. The distilled note stays; this sits beside it.

THE UNIT is an EXCHANGE: one request and the work that answered it. A question
alone does not say what was done, and a reply alone does not say why. Long
exchanges are split into pieces sized to the embedding model's window
(all-MiniLM-L6-v2 truncates at 256 word-pieces, ~1,000 characters; anything
past that is invisible to the vector half), each piece carrying the start of
its request so it still says what it was for.

THE SOURCE is Claude Code's own JSONL transcript, re-read whole on each index.
Measured on 46 MB: 0.33 s. That is cheaper than keeping offsets correct across
compactions and rewinds, and it makes indexing idempotent: a piece whose text
is unchanged is not re-embedded, and a piece no longer in the transcript is
removed. Rows are keyed by the transcript's own message uuid, so the key of an
exchange never moves.

SECRETS ARE REDACTED BEFORE STORAGE. A transcript holds whatever was pasted
into it -- this one holds API keys -- and an index that stores them verbatim
would copy credentials into a database and hand them back in search results.

Search is hybrid, with the same arithmetic as note search: a lexical ranking
(BM25) fused with cosine similarity by Reciprocal Rank Fusion, K=60. It runs in
Python over the workspace's rows, which is fine at thousands of pieces and is
roadmap item 8's problem at hundreds of thousands.

  python -m brahmastra.sessions --index <transcript.jsonl> [...]
  python -m brahmastra.sessions --backfill <claude project dir>
  python -m brahmastra.sessions --search "how did we fix the notion leak"
  python -m brahmastra.sessions --status
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from brahmastra import hybrid
from brahmastra.sidecar import SidecarStore

PIECE_CHARS = 1000        # the embedding model's window, roughly
REQUEST_PREFIX_CHARS = 200


# -- redaction --------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),                       # Groq
    re.compile(r"ntn_[A-Za-z0-9]{20,}"),                       # Notion
    re.compile(r"secret_[A-Za-z0-9]{30,}"),                    # Notion (legacy)
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),                  # Anthropic
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),            # OpenAI
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),   # GitHub
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),                      # Google
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # AWS
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),               # Slack
]
# NAME=value for anything named like a credential. The name is kept -- it is
# what makes "where does NEO4J_PASSWORD come from" findable -- the value is not.
_SECRET_ASSIGNMENT = re.compile(
    r"\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEYS?|APIKEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(?!\[redacted\])([^\s,;'\"]{6,})")


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", text)


# -- reading a transcript ---------------------------------------------------

_IDE_TAG = re.compile(r"<ide_[a-z_]+>.*?</ide_[a-z_]+>", re.S)


@dataclass
class Exchange:
    uuid: str
    started_at: str
    request: str
    work: str


def read_exchanges(path: str | Path) -> tuple[str | None, list[Exchange]]:
    """(session_id, exchanges) from a Claude Code JSONL transcript."""
    from brahmastra.checkpoint import _block_text, _is_noise

    session: str | None = None
    out: list[Exchange] = []
    work: list[str] = []
    # Claude Code re-logs messages verbatim when a session resumes: the same
    # uuid, the same text, twice. Measured on this project's transcript: 29
    # exchanges. Counting them twice doubles their weight in search and,
    # worse, gives two pieces one key.
    seen: set[str] = set()

    def close() -> None:
        if out:
            out[-1].work = "\n\n".join(work)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            session = session or row.get("sessionId")
            if row.get("type") not in ("user", "assistant"):
                continue
            # Subagent traffic, harness meta-messages and the compaction
            # summary are not the conversation -- the summary is itself a
            # model's distillation, which is exactly what this index is not.
            if row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary"):
                continue
            text = _block_text(row.get("message", {}).get("content"))
            if _is_noise(text):
                continue
            uuid = row.get("uuid")
            if uuid:
                if uuid in seen:
                    continue
                seen.add(uuid)
            text = _IDE_TAG.sub("", text).strip()
            if not text:
                continue
            # A background task finishing arrives as a "user" message. Nobody
            # asked anything; what follows is still work on the last request.
            if row["type"] == "user" and text.startswith("<task-notification>"):
                continue
            if row["type"] == "user":
                close()
                work = []
                out.append(Exchange(str(row.get("uuid") or len(out)),
                                    str(row.get("timestamp") or ""), text, ""))
            elif out:
                work.append(text)
    close()
    return session, out


@dataclass
class Piece:
    key: str
    exchange_uuid: str
    started_at: str
    request: str
    text: str

    @property
    def fp(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


def pieces(exchange: Exchange) -> list[Piece]:
    """One exchange, cut to the embedding window, each piece saying what it was for."""
    request = redact(exchange.request)
    body = redact(f"{exchange.request}\n\n{exchange.work}".strip())
    head = request[:REQUEST_PREFIX_CHARS]
    out: list[Piece] = []
    start = 0
    while start < len(body):
        end = min(len(body), start + PIECE_CHARS)
        if end < len(body):
            # Break at a paragraph or sentence rather than mid-word, when one
            # is reasonably close.
            cut = max(body.rfind("\n\n", start, end), body.rfind(". ", start, end))
            if cut > start + PIECE_CHARS // 2:
                end = cut + 1
        chunk = body[start:end].strip()
        if chunk:
            text = chunk if start == 0 else f"[re: {head}]\n{chunk}"
            out.append(Piece(f"{exchange.uuid}:{len(out)}", exchange.uuid,
                             exchange.started_at, request[:REQUEST_PREFIX_CHARS], text))
        start = end
    return out


# -- the index ---------------------------------------------------------------

class SessionIndex(SidecarStore):
    """
    One row per piece. The session is the owner of its rows, and the rows live
    in ONE table, so re-indexing a session is its own ledger: write what is new
    or changed, then delete what the transcript no longer holds. Never the
    other way round -- a crash between the two leaves extra rows the next index
    removes, not a session with nothing.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS session_pieces (
        workspace_id   TEXT NOT NULL DEFAULT 'default',
        session_id     TEXT NOT NULL,
        piece_key      TEXT NOT NULL,
        exchange_uuid  TEXT NOT NULL,
        started_at     TEXT,
        request        TEXT,
        text           TEXT NOT NULL,
        fp             TEXT NOT NULL,
        embedding      TEXT,
        indexed_at     TEXT NOT NULL,
        PRIMARY KEY (workspace_id, session_id, piece_key)
    );
    CREATE INDEX IF NOT EXISTS idx_session_pieces_session
        ON session_pieces (workspace_id, session_id)
    """

    def stored(self, session_id: str) -> dict[str, dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT piece_key, fp, embedding FROM session_pieces "
                "WHERE workspace_id = ? AND session_id = ?"),
                (self.workspace, session_id))
            return {r["piece_key"]: r for r in map(self._dict, cur.fetchall())}

    def upsert(self, session_id: str, rows: list[tuple[Piece, str | None]]) -> None:
        if not rows:
            return
        self.init_schema()
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "INSERT INTO session_pieces (workspace_id, session_id, piece_key, "
                "exchange_uuid, started_at, request, text, fp, embedding, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, session_id, piece_key) DO UPDATE SET "
                "exchange_uuid = excluded.exchange_uuid, started_at = excluded.started_at, "
                "request = excluded.request, text = excluded.text, fp = excluded.fp, "
                "embedding = excluded.embedding, indexed_at = excluded.indexed_at"),
                [(self.workspace, session_id, p.key, p.exchange_uuid, p.started_at,
                  p.request, p.text, p.fp, emb, now) for p, emb in rows])

    def delete(self, session_id: str, keys: Iterable[str]) -> None:
        keys = list(keys)
        if not keys:
            return
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "DELETE FROM session_pieces "
                "WHERE workspace_id = ? AND session_id = ? AND piece_key = ?"),
                [(self.workspace, session_id, k) for k in keys])

    def all(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT session_id, piece_key, started_at, request, text, embedding "
                "FROM session_pieces WHERE workspace_id = ?"), (self.workspace,))
            return [self._dict(r) for r in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT session_id, COUNT(*) AS n FROM session_pieces "
                "WHERE workspace_id = ? GROUP BY session_id"), (self.workspace,))
            return {r["session_id"]: int(r["n"]) for r in map(self._dict, cur.fetchall())}


def index_transcript(path: str | Path, store: SessionIndex | None = None) -> dict[str, Any]:
    """Bring one transcript's rows up to date. Re-embeds only what changed."""
    from brahmastra import embeddings

    store = store or SessionIndex()
    session, exchanges = read_exchanges(path)
    if not session:
        return {"session": None, "pieces": 0}
    wanted = [p for ex in exchanges for p in pieces(ex)]
    have = store.stored(session)

    changed = [p for p in wanted if have.get(p.key, {}).get("fp") != p.fp]
    vectors = embeddings.embed([p.text for p in changed]) if changed else []
    if vectors is None:                         # no model: lexical half only
        vectors = [None] * len(changed)
    store.upsert(session, [(p, hybrid.pack(v)) for p, v in zip(changed, vectors)])

    gone = set(have) - {p.key for p in wanted}
    store.delete(session, gone)
    return {"session": session, "exchanges": len(exchanges), "pieces": len(wanted),
            "embedded": len(changed), "removed": len(gone),
            # None when nothing needed embedding: False would read as "no model".
            "vectors": (any(v is not None for v in vectors) if changed else None)}


# -- search ------------------------------------------------------------------

def search(query: str, limit: int = 8, store: SessionIndex | None = None) -> list[dict[str, Any]]:
    """Hybrid: BM25 and cosine, fused by RRF (K=60) -- note search's arithmetic."""
    store = store or SessionIndex()
    rows = store.all()
    if not rows:
        return []
    best = hybrid.rank(query, rows, text=lambda r: r["text"])
    out, seen = [], set()
    for i, score in best:
        r = rows[i]
        # One hit per exchange: its pieces overlap in meaning, and five slices
        # of one answer crowd out four other answers.
        exchange = r["piece_key"].rsplit(":", 1)[0]
        if (r["session_id"], exchange) in seen:
            continue
        seen.add((r["session_id"], exchange))
        out.append({"session_id": r["session_id"], "started_at": r["started_at"],
                    "request": r["request"], "text": r["text"], "score": round(score, 5)})
        if len(out) >= limit:
            break
    return out


# -- CLI ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    say = lambda obj: sys.stdout.buffer.write(
        (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    if argv[:1] == ["--index"]:
        say([index_transcript(p) for p in argv[1:]])
    elif argv[:1] == ["--backfill"] and len(argv) > 1:
        say([index_transcript(p) for p in sorted(Path(argv[1]).glob("*.jsonl"))])
    elif argv[:1] == ["--search"] and len(argv) > 1:
        say(search(" ".join(argv[1:])))
    elif argv[:1] == ["--status"]:
        store = SessionIndex()
        say({"workspace": store.workspace, "backend": store.backend,
             "sessions": store.counts()})
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
