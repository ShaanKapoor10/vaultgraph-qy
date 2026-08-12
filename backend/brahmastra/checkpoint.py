"""
Session checkpointing — turn a conversation into graph knowledge before the
context window drops it.

Brahmastra's whole premise is that knowledge should outlive the place it was
written. An AI coding session is the sharpest case of that: everything decided
in it is lost at compaction unless someone remembers to write it down, and
"someone remembers" is the least reliable trigger there is. The proof is in
this repo's own history — the workspace migration got stored, the quota fix
that immediately followed did not.

So the trigger moves off memory and onto Claude Code's PreCompact/SessionEnd
hooks. Two phases, deliberately split:

  capture  — read the transcript, write the new turns to a queue file on disk.
             Pure file I/O, no network, a few milliseconds. This is what the
             hook runs, so a slow or dead LLM can never delay compaction.
  drain    — distil each queued file into entity-rich prose and store it as a
             note. Needs an LLM, so it may fail; the queue file survives until
             it succeeds.

Splitting them is what makes this safe to hang off a hook. The queue is the
durable part; distillation is the part allowed to fail.

Usage:
  python -m brahmastra.checkpoint            # hook mode: reads hook JSON on stdin
  python -m brahmastra.checkpoint --drain    # distil and store whatever is queued
  python -m brahmastra.checkpoint --status   # what is waiting
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent


def queue_dir() -> Path:
    """
    Where captures wait to be distilled.

    Resolved per call, never at import, so a test can redirect it. Binding this
    at import time is how DB_PATH once let the suite run against the production
    database: the value was already fixed before any fixture could change it.
    Here the stakes are the same — a test that drains the real queue distils a
    genuine conversation into a temp database and then deletes the capture.
    """
    override = os.environ.get("BRAHMASTRA_CHECKPOINT_DIR")
    return Path(override) if override else _BACKEND / "data" / "checkpoints"


def _offsets_path() -> Path:
    return queue_dir() / ".offsets.json"


def _log_path() -> Path:
    return queue_dir() / "checkpoint.log"

# Bound the prompt: a long session can be megabytes, and only the tail is new
# knowledge anyway. Characters, taken from the END of the unseen turns.
MAX_TRANSCRIPT_CHARS = 40_000
# Below this there is nothing worth a note — a two-line exchange is noise.
MIN_TRANSCRIPT_CHARS = 400

DISTIL_SYSTEM = (
    "You convert an AI pair-programming transcript into durable knowledge for a "
    "knowledge graph.\n\n"
    "Write plain prose in explicit subject-relation-object sentences, because an "
    "extractor reads this into triples. Name real entities every time: say 'The file "
    "llm.py raises LLMQuotaExhausted', never 'it raises an exception there'. Never use "
    "pronouns for things that have names.\n\n"
    "Record only what stays true after the session ends: decisions and why they were "
    "made, bugs and their root causes, how components relate, constraints discovered. "
    "Skip the back-and-forth, the tool calls, the file listings, and anything already "
    "obvious from reading the code.\n\n"
    "You are a backstop for notes the assistant wrote deliberately during the session, "
    "so prefer what it did NOT get to. Anything already covered by the listed notes is "
    "a duplicate — leave it out.\n\n"
    "Start with a single '# ' title line naming the work. Then 1-4 short paragraphs. "
    "If the transcript holds nothing durable, reply with exactly: SKIP"
)


def _log(message: str) -> None:
    """
    Record why a checkpoint did not happen.

    A hook must never raise, which makes `except: pass` tempting and makes a
    broken checkpoint indistinguishable from a quiet one. This is the only
    place that difference is visible, so it must not itself throw.
    """
    try:
        queue_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reading a Claude Code transcript
# ---------------------------------------------------------------------------

def _block_text(content: Any) -> str:
    """Plain text of one message's content, dropping tool traffic."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        # tool_use / tool_result carry mechanics, not knowledge. Keeping them
        # floods the extractor with entities like "Bash" and "file_path".
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text", ""))
    return "\n".join(out)


def _is_noise(text: str) -> bool:
    """Injected scaffolding that was never part of the conversation."""
    stripped = text.strip()
    return (
        not stripped
        or stripped.startswith("<system-reminder>")
        or stripped.startswith("<local-command-")
        or stripped.startswith("<command-name>")
        or stripped.startswith("Caveat: The messages below")
    )


def read_transcript(path: str | Path, start_line: int = 0) -> tuple[str, int]:
    """
    Return (conversation text after start_line, total lines seen).

    The line count is returned so repeated checkpoints in one session resume
    where the last one stopped instead of re-storing the whole conversation.
    """
    lines_seen = 0
    parts: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                lines_seen = i + 1
                if i < start_line or not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") not in ("user", "assistant"):
                    continue
                if row.get("isSidechain"):
                    continue  # subagent chatter, not the main thread
                text = _block_text(row.get("message", {}).get("content"))
                if _is_noise(text):
                    continue
                role = "Shaan" if row["type"] == "user" else "Claude"
                parts.append(f"{role}: {text.strip()}")
    except OSError:
        return "", start_line

    convo = "\n\n".join(parts)
    if len(convo) > MAX_TRANSCRIPT_CHARS:
        convo = convo[-MAX_TRANSCRIPT_CHARS:]
    return convo, lines_seen


# ---------------------------------------------------------------------------
# Phase 1 — capture (fast, runs inside the hook)
# ---------------------------------------------------------------------------

def _load_offsets() -> dict[str, int]:
    try:
        return json.loads(_offsets_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_offsets(offsets: dict[str, int]) -> None:
    queue_dir().mkdir(parents=True, exist_ok=True)
    _offsets_path().write_text(json.dumps(offsets, indent=2), encoding="utf-8")


def capture(payload: dict[str, Any]) -> Path | None:
    """
    Queue the unseen part of this session's transcript. Returns the queue file,
    or None when there was nothing new worth keeping.
    """
    transcript = payload.get("transcript_path")
    if not transcript:
        return None

    session = str(payload.get("session_id") or "unknown")
    offsets = _load_offsets()
    convo, lines_seen = read_transcript(transcript, offsets.get(session, 0))

    # Advance the offset even when we skip: a stretch too short to be worth a
    # note should not be re-read into the next checkpoint either.
    offsets[session] = lines_seen
    _save_offsets(offsets)

    if len(convo) < MIN_TRANSCRIPT_CHARS:
        return None

    queue_dir().mkdir(parents=True, exist_ok=True)
    path = queue_dir() / f"{int(time.time())}-{session[:8]}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": session,
                "trigger": payload.get("hook_event_name") or payload.get("trigger"),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "workspace": os.environ.get("BRAHMASTRA_WORKSPACE"),
                "conversation": convo,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _spawn_drain() -> None:
    """
    Start the distillation in a detached process so the hook returns at once.

    Best effort by design: if the spawn fails the queue file is still on disk
    and the next drain — manual, or the one the pipeline runs — picks it up.
    """
    kwargs: dict[str, Any] = {
        "cwd": str(_BACKEND),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW — no console flash, no parent tie.
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, "-m", "brahmastra.checkpoint", "--drain"], **kwargs
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 2 — drain (needs an LLM, allowed to fail)
# ---------------------------------------------------------------------------

def _existing_titles(limit: int = 80) -> list[str]:
    """
    Titles already in the graph.

    The checkpoint is a safety net under deliberate note-taking, not a
    replacement for it — an assistant that judged something worth storing has
    usually already stored it, and better. Showing the distiller what exists
    keeps it filling gaps instead of writing a second, worse copy.
    """
    try:
        from brahmastra import db
        return [n["title"] for n in db.get_notes()][:limit]
    except Exception:
        return []


def _distil(conversation: str) -> str | None:
    """Conversation -> note text, or None if there is nothing durable in it."""
    from brahmastra.llm import chat, ollama_available

    # Prefer the local model: this runs unattended and often mid-session, so it
    # must not eat the cloud daily quota that extraction depends on.
    provider = "ollama" if ollama_available() else None

    known = _existing_titles()
    already = (
        "Notes already in the graph — do NOT restate what these cover:\n"
        + "\n".join(f"- {t}" for t in known)
        + "\n\n"
    ) if known else ""

    text = chat(
        DISTIL_SYSTEM,
        f"{already}Transcript:\n\n{conversation}\n\nKnowledge:",
        temperature=0.2,
        max_tokens=1200,
        timeout=180,
        provider=provider,
    ).strip()

    if not text or text.upper().startswith("SKIP"):
        return None
    return text


def _split_title(note: str) -> tuple[str, str]:
    """Pull the leading '# ' heading out as the note title."""
    lines = note.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        return lines[0].lstrip("# ").strip(), "\n".join(lines[1:]).strip()
    return "Session Checkpoint", note


def drain() -> dict[str, Any]:
    """
    Distil every queued capture into a note. A file is deleted only once its
    note is stored, so an LLM outage delays checkpointing instead of losing it.
    """
    queue = queue_dir()
    if not queue.exists():
        return {"stored": 0, "skipped": 0, "failed": [], "queued": 0}

    from brahmastra import db

    stored, skipped, failed = 0, 0, []
    files = sorted(_captures())

    for path in files:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)  # unreadable; nothing to recover
            continue

        if item.get("workspace"):
            os.environ["BRAHMASTRA_WORKSPACE"] = item["workspace"]

        try:
            note = _distil(item["conversation"])
        except Exception as e:
            # The drain usually runs detached with its output discarded, so the
            # log is the only trace. Without it a checkpoint that never lands
            # looks identical to one that had nothing to say.
            _log(f"drain failed for {path.name}: {type(e).__name__}: {str(e)[:300]}")
            failed.append({"file": path.name, "error": str(e)[:200]})
            continue  # keep the file; try again next drain

        if note is None:
            skipped += 1
            path.unlink(missing_ok=True)
            continue

        title, body = _split_title(note)
        captured = item.get("captured_at", "")[:10]
        db.init_db()
        db.upsert_note(
            id=f"checkpoint-{path.stem}",
            title=f"{title} ({captured})" if captured else title,
            content=f"{title}\n\n{body}",
            mark_pending=True,
        )
        stored += 1
        path.unlink(missing_ok=True)

    return {"stored": stored, "skipped": skipped, "failed": failed,
            "queued": pending_count()}


def _captures() -> list[Path]:
    """Queue files, excluding the offsets bookkeeping file."""
    queue = queue_dir()
    if not queue.exists():
        return []
    offsets = _offsets_path().name
    return [p for p in queue.glob("*.json") if p.name != offsets]


def pending_count() -> int:
    """Captures waiting to be distilled."""
    return len(_captures())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--drain" in argv:
        print(json.dumps(drain(), indent=2))
        return 0

    if "--status" in argv:
        print(json.dumps({"queued": pending_count(), "queue_dir": str(queue_dir())}, indent=2))
        return 0

    # Hook mode. Claude Code sends the event as JSON on stdin. Nothing here may
    # raise: a hook that fails is a hook that interrupts the user's session, and
    # a missed checkpoint is never worth that.
    #
    # But silence is the bug this whole feature exists to fix, so every swallowed
    # failure goes to the log instead of vanishing.
    raw = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw.lstrip("﻿") or "{}")  # tolerate a BOM
    except (json.JSONDecodeError, OSError) as e:
        _log(f"could not parse hook payload ({e}); first 80 chars: {raw[:80]!r}")
        return 0

    try:
        queued = capture(payload)
        if queued is None:
            _log(f"nothing new to checkpoint for session {payload.get('session_id')}")
        else:
            _spawn_drain()
    except Exception as e:
        _log(f"capture failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    # Runnable as a bare path, not just `-m`. A hook command should not depend
    # on the shell's working directory or on PYTHONPATH being set for it.
    if "brahmastra" not in sys.modules:
        sys.path.insert(0, str(_BACKEND))
    raise SystemExit(main())
