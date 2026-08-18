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
import re
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
# Kept modest because the distiller is normally a local 7B model — at 40k it
# stopped summarising and started continuing the dialogue instead.
MAX_TRANSCRIPT_CHARS = 20_000
# Below this there is nothing worth a note — a two-line exchange is noise.
MIN_TRANSCRIPT_CHARS = 400
# Distillations to attempt before setting a capture aside as unsummarisable.
MAX_ATTEMPTS = 3
# On a per-turn Stop hook, capture every turn but only DISTIL once this much
# conversation has accumulated. Distilling each turn would spend an LLM call
# per reply and bury the graph in near-empty notes; waiting for a boundary
# loses everything when one never comes. This is the middle.
DRAIN_THRESHOLD_CHARS = 12_000
# Blank line between merged slices and between a note's title and body.
SEP = "\n\n"

DISTIL_SYSTEM = (
    "You extract durable facts from a record of a software work session. The records "
    "are labelled [REQUEST n] for what Shaan Kapoor asked and [WORK n] for what Claude "
    "Code did. You are an archivist reading a finished record, not a participant in a "
    "conversation.\n\n"
    "Write short standalone sentences in subject-relation-object form, because an "
    "extractor turns them into graph triples. Name real entities every time: say 'The "
    "file llm.py raises LLMQuotaExhausted', never 'it raises an exception there'.\n\n"
    "Record only what stays true after the session ends: decisions and why they were "
    "made, bugs and their root causes, how components relate, constraints discovered.\n\n"
    "ACCURACY BEATS COMPLETENESS. Every sentence must be supported by the record in "
    "front of you. If you are not certain a detail appears there — a file name, a "
    "commit, a number, an outcome — leave the whole sentence out. Three facts you can "
    "point at are worth more than ten that read well. Never state that something was "
    "committed, pushed, deployed or finished unless the record says so.\n\n"
    "You are a backstop for notes written deliberately during the session, so prefer "
    "what they do not already cover.\n\n"
    "Format, exactly: the FIRST line is '# ' followed by a short name for the work — "
    "two to six words, a name and not a sentence. Every following line is one plain "
    "factual sentence with no '#', no '-' and no numbering. Write 3 to 8 of them. "
    "No dialogue, no speaker names, no questions, no offers of help. "
    "If the record holds nothing durable, reply with exactly: SKIP"
)

# The instruction is repeated AFTER the record because the failure mode is
# recency-driven: a small model that has just read 20k characters carries on in
# whatever shape it just saw unless the last thing it reads says otherwise.
DISTIL_REMINDER = (
    "\n</record>\n\n"
    "The record above has ended. Write the facts worth keeping from it. Omit anything "
    "you cannot point to in the record — a shorter note is the correct answer when you "
    "are unsure. Begin with '# '."
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
                # Deliberately NOT "Shaan: ... Claude: ...". Speaker-colon
                # formatting is a chat template, and ending a prompt with
                # thousands of tokens of it makes "write the next turn" the
                # single most likely continuation — which is exactly what a 7B
                # model did, inventing a commit and a reply from Shaan. Framing
                # each turn as a labelled record instead removes the pattern
                # while keeping who-said-what.
                kind = "REQUEST" if row["type"] == "user" else "WORK"
                parts.append(f"[{kind} {len(parts) + 1}]\n{text.strip()}")
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
    # Nanoseconds, not seconds. With a per-turn Stop hook two captures land in
    # the same second routinely, and a whole-second name silently OVERWROTE the
    # earlier one — losing exactly the turns the hook exists to preserve. The
    # name still sorts chronologically, which drain() relies on to merge a
    # session's slices in order.
    path = queue_dir() / f"{time.time_ns()}-{session[:8]}.json"
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


class DistillationRejected(RuntimeError):
    """The model's output is not a summary, so it must not reach the graph."""


# Lines that begin like dialogue mean the model resumed the conversation
# instead of summarising it.
_SPEAKER = re.compile(r"^\s*(Shaan|Claude|Claude Code|User|Assistant|Human)\s*:", re.M)

# Identifiers are the tokens a model invents most confidently and that do the
# most damage in a code graph: a commit that was never made, a file that does
# not exist. Both are cheap to check — they must appear in the source text.
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b")
_FILENAME = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|md|json|ya?ml|toml|sql|env)\b")


def _ungrounded(text: str, source: str) -> list[str]:
    """
    Identifiers asserted in `text` that do not occur in `source`.

    A cheap, blunt grounding check. It cannot catch a wrong *claim* about a
    real file, but it catches invention outright — and invention is what
    happened: the fabricated note cited commit `30940a41`, which was actually a
    note ID the model had seen and reshaped into a plausible hash.
    """
    haystack = source.lower()
    claimed = set(_HASH.findall(text.lower())) | set(_FILENAME.findall(text.lower()))
    return sorted(token for token in claimed if token not in haystack)


def _validate(text: str, source: str = "") -> str:
    """
    Reject output that is not a faithful summary.

    This guard exists because the first real run produced a note that was
    entirely invented: a local 7B model continued the transcript, complete with
    a fabricated commit hash, a fabricated push and a fabricated "great,
    thanks" from Shaan. Extracting that would have written fiction into the
    graph as fact — strictly worse than checkpointing nothing, because a wrong
    note is indistinguishable from a right one once it is a triple.

    Every check here fails CLOSED: a rejection keeps the capture queued and
    stores nothing. Losing a checkpoint is recoverable, poisoning the graph is
    not, so anything doubtful is dropped rather than written.
    """
    if _SPEAKER.search(text):
        raise DistillationRejected("output continues the dialogue instead of summarising")
    if not text.lstrip().startswith("#"):
        raise DistillationRejected("output has no '# ' title line; format was ignored")
    if len(text) < 120:
        raise DistillationRejected(f"output too short to be a summary ({len(text)} chars)")
    if "?" in text:
        raise DistillationRejected("output asks a question; a record of facts contains none")

    if source:
        invented = _ungrounded(text, source)
        if invented:
            raise DistillationRejected(
                f"output cites identifiers absent from the record: {', '.join(invented[:5])}"
            )
    return text


def _ask(conversation: str, provider: str | None) -> str:
    from brahmastra.llm import chat

    known = _existing_titles()
    already = (
        "Notes already in the graph — do NOT restate what these cover:\n"
        + "\n".join(f"- {t}" for t in known)
        + "\n\n"
    ) if known else ""

    return chat(
        DISTIL_SYSTEM,
        f"{already}<record>\n{conversation}{DISTIL_REMINDER}",
        temperature=0.0,  # nothing here benefits from sampling variety
        max_tokens=900,
        timeout=180,
        provider=provider,
    ).strip()


def _distil(conversation: str) -> str | None:
    """Conversation -> note text, or None if there is nothing durable in it."""
    from brahmastra.llm import ollama_available, resolve_provider

    # Prefer the local model: this runs unattended and often mid-session, so it
    # must not eat the cloud daily quota that extraction depends on. Override
    # with CHECKPOINT_PROVIDER when a stronger summariser is worth the tokens.
    pinned = os.environ.get("CHECKPOINT_PROVIDER")
    first = pinned or ("ollama" if ollama_available() else None)

    text = _ask(conversation, first)
    if not text or text.upper().startswith("SKIP"):
        return None

    try:
        return _validate(text, conversation)
    except DistillationRejected as rejected:
        # A small local model failing this is expected, and it is the ONLY
        # reason to spend cloud tokens here: one retry, on a stronger model,
        # rather than discarding a session's knowledge over a format slip.
        if pinned:
            raise
        try:
            fallback = resolve_provider()
        except Exception:
            raise rejected
        if fallback == first:
            raise

        _log(f"{rejected}; retrying on {fallback}")
        retry = _ask(conversation, fallback)
        if not retry or retry.upper().startswith("SKIP"):
            return None
        return _validate(retry, conversation)


MAX_TITLE_CHARS = 70


def _split_title(note: str) -> tuple[str, str]:
    """
    Pull the leading '# ' heading out as the note title and tidy the body.

    Formatting slips are normalised rather than rejected. Rejection is for
    output that might be untrue; punishing an accurate note for putting '#' on
    every line would throw away good knowledge over cosmetics — which happened:
    a run produced eight correct facts, each prefixed with '#' because the
    model read "one per line" as "one heading per line".
    """
    lines = note.splitlines()
    title = "Session Checkpoint"
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("#").strip() or title
        lines = lines[1:]

    # A title is a name, not a sentence — long ones read badly in recall lists.
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"

    body = "\n".join(line.lstrip("#").lstrip("-").strip() for line in lines)
    return title.rstrip("."), body.strip()


def drain() -> dict[str, Any]:
    """
    Distil queued captures into notes, ONE NOTE PER SESSION.

    Captures are merged before distillation rather than handled one by one.
    With a per-turn Stop hook a single session produces many small slices, and
    distilling each separately would bury the graph in near-empty notes that
    each restate the same work. Merging also gives the model the whole arc of
    a session, which is what makes a summary worth keeping.

    A capture is deleted only once its note is stored, so an LLM outage delays
    checkpointing instead of losing it.
    """
    queue = queue_dir()
    if not queue.exists():
        return {"stored": 0, "skipped": 0, "failed": [], "queued": 0}

    from brahmastra import db

    # Group by session, oldest first, so the merged record reads in order.
    sessions: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in sorted(_captures()):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)  # unreadable; nothing to recover
            continue
        sessions.setdefault(str(item.get("session_id") or "unknown"), []).append((path, item))

    stored, skipped, failed = 0, 0, []

    for session, batch in sessions.items():
        paths = [p for p, _ in batch]
        items = [i for _, i in batch]

        if items[0].get("workspace"):
            os.environ["BRAHMASTRA_WORKSPACE"] = items[0]["workspace"]

        conversation = SEP.join(i.get("conversation", "") for i in items)
        # Keep the tail: the same reason capture bounds itself, and the end of
        # a session is where its conclusions are.
        if len(conversation) > MAX_TRANSCRIPT_CHARS:
            conversation = conversation[-MAX_TRANSCRIPT_CHARS:]

        try:
            note = _distil(conversation)
        except Exception as e:
            _log(f"drain failed for session {session[:8]} "
                 f"({len(paths)} captures): {type(e).__name__}: {str(e)[:250]}")
            failed.append({"session": session[:8], "captures": len(paths),
                           "error": str(e)[:200]})
            for path, item in batch:
                _record_attempt(path, item, str(e))
            continue

        if note is None:
            skipped += 1
            for path in paths:
                path.unlink(missing_ok=True)
            continue

        title, body = _split_title(note)
        captured = items[-1].get("captured_at", "")[:10]
        db.init_db()
        db.upsert_note(
            id=f"checkpoint-{paths[-1].stem}",
            title=f"{title} ({captured})" if captured else title,
            content=f"{title}{SEP}{body}",
            mark_pending=True,
            source="checkpoint",
        )
        stored += 1
        for path in paths:
            path.unlink(missing_ok=True)

    return {"stored": stored, "skipped": skipped, "failed": failed,
            "queued": pending_count()}


def _record_attempt(path: Path, item: dict[str, Any], error: str) -> None:
    """
    Count a failed distillation and set the capture aside once it looks hopeless.

    A transient failure (LLM down, quota spent) clears on its own, so retrying
    is right. A capture the model cannot summarise would otherwise be retried
    on every pipeline run forever, so after MAX_ATTEMPTS it moves to
    `rejected/` — out of the queue, but still on disk to inspect rather than
    deleted.
    """
    item["attempts"] = int(item.get("attempts", 0)) + 1
    item["last_error"] = error[:500]
    try:
        path.write_text(json.dumps(item, indent=2), encoding="utf-8")
        if item["attempts"] >= MAX_ATTEMPTS:
            rejected = queue_dir() / "rejected"
            rejected.mkdir(parents=True, exist_ok=True)
            path.replace(rejected / path.name)
            _log(f"gave up on {path.name} after {MAX_ATTEMPTS} attempts; moved to rejected/")
    except OSError:
        pass


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


def queued_chars() -> int:
    """How much unqueued conversation is waiting, across all captures."""
    total = 0
    for path in _captures():
        try:
            total += len(json.loads(path.read_text(encoding="utf-8")).get("conversation", ""))
        except (OSError, json.JSONDecodeError):
            continue
    return total


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

    event = str(payload.get("hook_event_name") or payload.get("trigger") or "")

    try:
        queued = capture(payload)
        if queued is None:
            # Only worth logging at a boundary. On a per-turn Stop hook this is
            # the common case and would drown the log it exists to make useful.
            if event != "Stop":
                _log(f"nothing new to checkpoint for session {payload.get('session_id')}")
            return 0

        # Stop fires after EVERY assistant turn, which is what closes the two
        # gaps that lost a day of work: a session long enough never to compact,
        # and a crash — a killed process never fires SessionEnd, so everything
        # since the last boundary went unrecorded.
        #
        # Capturing every turn is safe because capture is pure file I/O. What
        # must not happen every turn is DISTILLING: that is an LLM call per
        # reply and a graph full of near-empty notes. So on Stop the capture
        # simply accumulates, and is drained once there is enough of it to be
        # worth summarising. A boundary event always drains, whatever the size.
        if event == "Stop" and queued_chars() < DRAIN_THRESHOLD_CHARS:
            return 0
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
