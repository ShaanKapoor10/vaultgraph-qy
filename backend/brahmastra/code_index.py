"""
Source code as a searchable part of the brain -- ROADMAP item 4.

Notes say what was decided about the code; the graph knows `_groq_chat` as an
entity. Neither can answer "where is the key rotation implemented" with a file
and a line. This indexes the repository's own files so it can.

WHAT IS INDEXED: files git TRACKS, nothing else. That is the guarantee `.env`
and every other ignored file stays out -- the index never decides for itself
what is secret. Redaction still runs on what is left, because tracked files
have held key fragments before (a test once carried the first 32 characters of
a real Groq key).

HOW FILES ARE CUT: by the language's own structure, so a hit is a unit a
reader recognises and points at LINES, not just a file (both details from
cocoindex's code_embedding example):

  python      top-level def/class via `ast`, classes split per method when
              large; `symbols` records what each chunk defines
  ts/tsx/js   top-level declarations (export, function, class, const, ...)
  markdown    headings
  other       fixed windows of lines

Anything still too long is cut into line windows, keeping line numbers.

IDENTITY IS CONTENT. A chunk's key is the hash of its text, not its line
number, so an edit near the top of a file does not re-embed everything below
it: a chunk that only MOVED keeps its key and gets new line numbers. The file
is the chunk's owner -- re-indexing a file writes what is new, re-lines what
moved, and deletes what is gone, in that order.

  python -m brahmastra.code_index --index [repo root]
  python -m brahmastra.code_index --search "where are groq keys rotated" [--scope tests|docs|all]
  python -m brahmastra.code_index --status
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brahmastra import hybrid
from brahmastra.sessions import redact
from brahmastra.sidecar import SidecarStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MAX_CHUNK_LINES = 60
MAX_CHUNK_CHARS = 2400
MAX_FILE_BYTES = 200_000

LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".mjs": "javascript", ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".json": "json", ".html": "html", ".ini": "ini",
}
# Generated, or data rather than code -- large and never what anyone asks for.
_SKIP = re.compile(r"(^|/)(pnpm-lock\.yaml|package-lock\.json|.*\.min\.js)$")


@dataclass
class Chunk:
    path: str
    start_line: int          # 1-based, inclusive
    end_line: int
    language: str
    text: str
    symbols: list[str] = field(default_factory=list)

    @property
    def fp(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def embed_text(self) -> str:
        """What the vector half sees: where it lives and what it defines, first,
        because the model's window is ~1,000 characters and the body is long."""
        head = f"{self.path} {' '.join(self.symbols)}".strip()
        return f"{head}\n{self.text}"


# -- cutting ----------------------------------------------------------------

def _windows(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """[start, end] (1-based, inclusive) cut into pieces within both limits."""
    out = []
    s = start
    while s <= end:
        e, size = s, 0
        while e <= end and e - s < MAX_CHUNK_LINES and size + len(lines[e - 1]) < MAX_CHUNK_CHARS:
            size += len(lines[e - 1]) + 1
            e += 1
        e = max(e - 1, s)            # at least one line, however long
        out.append((s, e))
        s = e + 1
    return out


def _spans_python(source: str, lines: list[str]) -> list[tuple[int, int, list[str]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[int, int, list[str]]] = []
    cursor = 1

    def start_of(node) -> int:
        decorators = getattr(node, "decorator_list", [])
        return min([node.lineno] + [d.lineno for d in decorators])

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        s, e = start_of(node), node.end_lineno or node.lineno
        if s > cursor:
            spans.append((cursor, s - 1, []))           # module-level code between
        if isinstance(node, ast.ClassDef) and (e - s + 1) > MAX_CHUNK_LINES:
            inner = cursor_c = s
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ss, se = start_of(sub), sub.end_lineno or sub.lineno
                    if ss > cursor_c:
                        spans.append((cursor_c, ss - 1, [node.name] if cursor_c == inner else []))
                    spans.append((ss, se, [f"{node.name}.{sub.name}", sub.name]))
                    cursor_c = se + 1
            if cursor_c <= e:
                spans.append((cursor_c, e, [node.name]))
        else:
            spans.append((s, e, [node.name]))
        cursor = e + 1
    if cursor <= len(lines):
        spans.append((cursor, len(lines), []))
    return spans


_TS_TOP = re.compile(r"^(export\s+(default\s+)?)?(async\s+)?"
                     r"(function|class|const|let|interface|type|enum)\s+([A-Za-z_$][\w$]*)")
_MD_HEADING = re.compile(r"^#{1,4}\s+(.*)")


def _spans_by_marker(lines: list[str], marker: re.Pattern, name_group: int) -> list[tuple[int, int, list[str]]]:
    starts = [(i + 1, m.group(name_group).strip()) for i, line in enumerate(lines)
              if (m := marker.match(line))]
    if not starts:
        return []
    spans = []
    if starts[0][0] > 1:
        spans.append((1, starts[0][0] - 1, []))
    for (s, name), nxt in zip(starts, starts[1:] + [(len(lines) + 1, "")]):
        spans.append((s, nxt[0] - 1, [name]))
    return spans


MERGE_LINES = 40
MERGE_CHARS = 1500


def _merge_small(spans: list[tuple[int, int, list[str]]],
                 lines: list[str]) -> list[tuple[int, int, list[str]]]:
    """
    Neighbouring small units joined, up to MERGE_LINES / MERGE_CHARS.

    Cut purely by structure, the median chunk was 367 characters -- often a
    two-line helper -- which is too little for a hit to say anything. A unit
    already over the limit is never merged, so a whole function is never split
    across two chunks just to fill one.
    """
    def size(s: int, e: int) -> int:
        return sum(len(line) + 1 for line in lines[s - 1:e])

    out: list[tuple[int, int, list[str]]] = []
    for s, e, symbols in spans:
        if out:
            ps, pe, psym = out[-1]
            if e - ps + 1 <= MERGE_LINES and size(ps, e) <= MERGE_CHARS:
                out[-1] = (ps, e, psym + symbols)
                continue
        out.append((s, e, list(symbols)))
    return out


def chunk_file(path: str, source: str) -> list[Chunk]:
    language = LANGUAGES.get(Path(path).suffix.lower(), "text")
    lines = source.splitlines()
    if not lines:
        return []
    if language == "python":
        spans = _spans_python(source, lines)
    elif language in ("typescript", "javascript"):
        spans = _spans_by_marker(lines, _TS_TOP, 5)
    elif language == "markdown":
        spans = _spans_by_marker(lines, _MD_HEADING, 1)
    else:
        spans = []
    spans = _merge_small(spans or [(1, len(lines), [])], lines)

    out: list[Chunk] = []
    for s, e, symbols in spans:
        for ws, we in _windows(lines, s, e):
            text = "\n".join(lines[ws - 1:we])
            if text.strip():
                out.append(Chunk(path, ws, we, language, redact(text), symbols))
    return out


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    """Paths git tracks, relative to root, forward slashes."""
    result = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    return [p for p in result.stdout.splitlines()
            if Path(p).suffix.lower() in LANGUAGES and not _SKIP.search(p)]


# -- the index ---------------------------------------------------------------

class CodeIndex(SidecarStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS code_chunks (
        workspace_id  TEXT NOT NULL DEFAULT 'default',
        path          TEXT NOT NULL,
        fp            TEXT NOT NULL,
        start_line    INTEGER NOT NULL,
        end_line      INTEGER NOT NULL,
        language      TEXT,
        symbols       TEXT,
        text          TEXT NOT NULL,
        embedding     TEXT,
        indexed_at    TEXT NOT NULL,
        PRIMARY KEY (workspace_id, path, fp)
    );
    CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON code_chunks (workspace_id, path)
    """

    def stored(self) -> dict[tuple[str, str], dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT path, fp, start_line, end_line FROM code_chunks "
                "WHERE workspace_id = ?"), (self.workspace,))
            return {(r["path"], r["fp"]): r for r in map(self._dict, cur.fetchall())}

    def insert(self, rows: list[tuple[Chunk, str | None]]) -> None:
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "INSERT INTO code_chunks (workspace_id, path, fp, start_line, end_line, "
                "language, symbols, text, embedding, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, path, fp) DO UPDATE SET "
                "start_line = excluded.start_line, end_line = excluded.end_line, "
                "symbols = excluded.symbols, embedding = excluded.embedding, "
                "indexed_at = excluded.indexed_at"),
                [(self.workspace, c.path, c.fp, c.start_line, c.end_line, c.language,
                  " ".join(c.symbols), c.text, emb, now) for c, emb in rows])

    def reline(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "UPDATE code_chunks SET start_line = ?, end_line = ?, symbols = ? "
                "WHERE workspace_id = ? AND path = ? AND fp = ?"),
                [(c.start_line, c.end_line, " ".join(c.symbols), self.workspace, c.path, c.fp)
                 for c in chunks])

    def delete(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "DELETE FROM code_chunks WHERE workspace_id = ? AND path = ? AND fp = ?"),
                [(self.workspace, p, f) for p, f in keys])

    def all(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT path, start_line, end_line, language, symbols, text, embedding "
                "FROM code_chunks WHERE workspace_id = ?"), (self.workspace,))
            return [self._dict(r) for r in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT path) AS files FROM code_chunks "
                "WHERE workspace_id = ?"), (self.workspace,))
            r = self._dict(cur.fetchone())
            return {"chunks": int(r["n"]), "files": int(r["files"])}


def index_repo(root: str | Path = REPO_ROOT, store: CodeIndex | None = None,
               files: dict[str, str] | None = None) -> dict[str, Any]:
    """
    Bring the index up to date with the tracked files. `files` ({path: text})
    replaces reading from disk, for tests.
    """
    from brahmastra import embeddings

    store = store or CodeIndex()
    root = Path(root)
    if files is None:
        files = {}
        for rel in tracked_files(root):
            p = root / rel
            try:
                if p.stat().st_size <= MAX_FILE_BYTES:
                    files[rel] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

    wanted: dict[tuple[str, str], Chunk] = {}
    for path, source in files.items():
        for c in chunk_file(path, source):
            wanted.setdefault((c.path, c.fp), c)     # identical twins: keep one
    have = store.stored()

    new = [c for k, c in wanted.items() if k not in have]
    moved = [c for k, c in wanted.items() if k in have and
             (have[k]["start_line"], have[k]["end_line"]) != (c.start_line, c.end_line)]
    gone = [k for k in have if k not in wanted]

    vectors = embeddings.embed([c.embed_text() for c in new]) if new else []
    if vectors is None:
        vectors = [None] * len(new)
    store.insert([(c, hybrid.pack(v)) for c, v in zip(new, vectors)])
    store.reline(moved)
    store.delete(gone)                     # last: never lose rows before new exist
    return {"files": len(files), "chunks": len(wanted), "embedded": len(new),
            "relined": len(moved), "removed": len(gone)}


def kind_of(path: str) -> str:
    """source | tests | docs -- by where the file lives and what it is."""
    name = Path(path).name
    if "/tests/" in f"/{path}" or name.startswith("test_") or ".test." in name:
        return "tests"
    if Path(path).suffix.lower() == ".md":
        return "docs"
    return "source"


SCOPES = ("source", "tests", "docs", "all")


def search(query: str, limit: int = 8, store: CodeIndex | None = None,
           scope: str = "source") -> list[dict[str, Any]]:
    """
    `scope` defaults to SOURCE, measured: on 13 questions with a known answer
    file, searching everything put a test or a doc first for "where is X done"
    (top-1 7/13, top-3 11/13); source only gave 10/13 and 13/13. A test
    describes the code in the same words, which is exactly why it outranks it.
    """
    store = store or CodeIndex()
    rows = [r for r in store.all() if scope == "all" or kind_of(r["path"]) == scope]
    if not rows:
        return []
    best = hybrid.rank(query, rows,
                       text=lambda r: f"{r['path']} {r['symbols'] or ''}\n{r['text']}")
    return [{"path": rows[i]["path"],
             "lines": f"{rows[i]['start_line']}-{rows[i]['end_line']}",
             "symbols": rows[i]["symbols"] or "",
             "text": rows[i]["text"], "score": round(score, 5)}
            for i, score in best[:limit]]


def where_defined(names: list[str], store: CodeIndex | None = None) -> list[str]:
    """`path:start-end` of every chunk that DEFINES one of these names."""
    wanted = {n.strip() for n in names if n and n.strip()}
    store = store or CodeIndex()
    spans: dict[str, list[list[int]]] = {}
    for r in sorted(store.all(), key=lambda r: (r["path"], r["start_line"])):
        symbols = set((r.get("symbols") or "").split())
        if symbols & wanted and kind_of(r["path"]) == "source":
            ranges = spans.setdefault(r["path"], [])
            # A long function is cut into windows that all carry its name;
            # report the definition once, as one range.
            if ranges and ranges[-1][1] + 1 >= r["start_line"]:
                ranges[-1][1] = max(ranges[-1][1], r["end_line"])
            else:
                ranges.append([r["start_line"], r["end_line"]])
    return [f"{p}:{s}-{e}" for p, rs in sorted(spans.items()) for s, e in rs]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    say = lambda obj: sys.stdout.buffer.write(
        (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    if argv[:1] == ["--index"]:
        say(index_repo(argv[1] if len(argv) > 1 else REPO_ROOT))
    elif argv[:1] == ["--search"] and len(argv) > 1:
        scope = "source"
        if len(argv) > 3 and argv[-2] == "--scope":
            scope, argv = argv[-1], argv[:-2]
        hits = search(" ".join(argv[1:]), scope=scope)
        for h in hits:
            h["text"] = h["text"][:300]
        say(hits)
    elif argv[:1] == ["--status"]:
        store = CodeIndex()
        say({"workspace": store.workspace, "backend": store.backend, **store.counts()})
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
