"""
Ingest transcripts from the filesystem.

    python -m brahmastra.ingest.cli meeting.txt notes/*.vtt
    python -m brahmastra.ingest.cli --watch ./transcripts
    python -m brahmastra.ingest.cli --list
    python -m brahmastra.ingest.cli --artifacts decision

The bulk path. Loading an archive of past meetings through an HTTP endpoint one
file at a time is the wrong shape: this runs in-process, reports per file, and
survives a failure partway through a directory.

The watcher is deliberately a poller over a directory rather than a filesystem
event API. Events are unreliable across platforms and network shares, and a
missed event means a meeting silently never enters the knowledge base -- the
kind of failure nobody notices until they search for something that should be
there. Polling cannot miss a file that is still sitting on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from brahmastra.env import load_env

load_env()

from brahmastra.ingest.assemble import process_transcript
from brahmastra.ingest.store import Transcript, get_ingest_store

SUFFIXES = {".txt", ".md", ".vtt", ".srt", ".text", ".log"}

# How long a file must be unchanged before it is read. A file still being
# written -- copied in, or flushed by a recording tool -- would otherwise be
# ingested half-finished, and the truncation is invisible afterwards.
SETTLE_SECONDS = 5.0


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _transcript_id_for(path: Path, text: str) -> str:
    """
    Stable for the same file with the same contents.

    So re-running the CLI over a directory REPLACES rather than duplicates,
    while an edited file becomes a new transcript rather than silently
    overwriting the record of what the previous version said.
    """
    digest = hashlib.sha1(f"{path.name}:{text}".encode("utf-8")).hexdigest()
    return f"f{digest[:11]}"


def ingest_file(path: Path, workspace: str | None = None,
                title: str | None = None) -> dict:
    """Read one file and process it. Reports rather than raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1", errors="replace")
    except OSError as exc:
        return {"path": str(path), "status": "error", "error": str(exc)}

    if not text.strip():
        return {"path": str(path), "status": "skipped", "error": "empty file"}

    store = get_ingest_store(workspace)
    tid = _transcript_id_for(path, text)

    if store.get_transcript(tid) is None:
        store.create_transcript(Transcript(
            id=tid,
            title=title or path.stem.replace("_", " ").replace("-", " "),
            content=text,
            source="cli",
            source_ref=str(path),
            occurred_at=datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).isoformat(),
        ))

    report = process_transcript(tid, store=store)
    report["path"] = str(path)
    return report


def _candidates(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in SUFFIXES
    )


def _settled(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) >= SETTLE_SECONDS
    except OSError:
        return False


def watch(directory: Path, interval: float, workspace: str | None = None) -> None:
    """Poll a directory forever, ingesting anything new that has settled."""
    store = get_ingest_store(workspace)
    store.init_schema()
    print(f"[{_stamp()}] watching {directory} every {interval:.0f}s "
          f"(workspace={store.workspace}). Ctrl-C to stop.", flush=True)

    while True:
        for path in _candidates(directory):
            if not _settled(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Seen already? Keyed on contents, so an edited file is re-ingested
            # and an untouched one is not.
            if store.get_transcript(_transcript_id_for(path, text)) is not None:
                continue

            print(f"[{_stamp()}] ingesting {path.name} "
                  f"({len(text) // 1000}k chars)…", flush=True)
            report = ingest_file(path, workspace=workspace)
            print(f"[{_stamp()}] {path.name}: {report.get('status')} — "
                  f"{report.get('chunks', 0)} chunks, "
                  f"{report.get('artifacts', 0)} artifacts, "
                  f"{report.get('notes', 0)} notes", flush=True)
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brahmastra.ingest.cli",
        description="Ingest meeting transcripts into the knowledge base.",
    )
    parser.add_argument("paths", nargs="*", help="transcript files or directories")
    parser.add_argument("--watch", metavar="DIR", help="poll a directory forever")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between polls with --watch")
    parser.add_argument("--workspace", default=None, help="which graph to write to")
    parser.add_argument("--title", default=None, help="title (single file only)")
    parser.add_argument("--list", action="store_true", help="list known transcripts")
    parser.add_argument("--artifacts", metavar="KIND", nargs="?", const="",
                        help="list artifacts, optionally of one kind")
    args = parser.parse_args(argv)

    if args.workspace:
        os.environ["BRAHMASTRA_WORKSPACE"] = args.workspace

    store = get_ingest_store(args.workspace)

    if args.list:
        rows = store.list_transcripts()
        if not rows:
            print("no transcripts ingested yet")
        for r in rows:
            print(f"{r['id']}  {r['status']:<10} {r['chunk_count']:>3} chunks  "
                  f"{r['title'][:60]}")
        return 0

    if args.artifacts is not None:
        rows = store.get_artifacts(kind=args.artifacts or None)
        if not rows:
            print("no artifacts")
        for r in rows:
            owner = f" [{r['owner']}]" if r.get("owner") else ""
            due = f" (due {r['due']})" if r.get("due") else ""
            print(f"{r['kind']:<14}{owner}{due} {r['statement'][:88]}")
        return 0

    if args.watch:
        directory = Path(args.watch)
        if not directory.is_dir():
            print(f"not a directory: {directory}", file=sys.stderr)
            return 2
        watch(directory, args.interval, args.workspace)
        return 0

    if not args.paths:
        parser.error("give a file or directory, or --watch DIR, or --list")

    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            targets.extend(_candidates(path))
        elif path.is_file():
            targets.append(path)
        else:
            print(f"skipping {path}: not found", file=sys.stderr)

    if not targets:
        print("nothing to ingest", file=sys.stderr)
        return 1

    failures = 0
    for path in targets:
        print(f"[{_stamp()}] {path.name} …", flush=True)
        report = ingest_file(path, args.workspace,
                             args.title if len(targets) == 1 else None)
        status = report.get("status")
        if status in ("error",):
            failures += 1
        print(f"    {status} — {report.get('chunks', 0)} chunks, "
              f"{report.get('artifacts', 0)} artifacts, "
              f"{report.get('notes', 0)} notes"
              + (f" — {report.get('error')}" if report.get("error") else ""))
        for err in (report.get("errors") or [])[:3]:
            print(f"      chunk {err['chunk']}: {err['error'][:110]}")

    print(f"\n{len(targets) - failures}/{len(targets)} ingested. "
          f"Run the pipeline to fold the new notes into the graph.")
    return 1 if failures == len(targets) else 0


if __name__ == "__main__":
    raise SystemExit(main())
