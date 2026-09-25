"""
When was each note written? -- the clock a contradiction is settled by.

A functional relation with two values is usually not a disagreement but a
CHANGE: "the test suite has 84 passing tests" was true in August and "510" in
September. The graph resolved those by the triple's `extracted_at`, which is
when extraction RAN -- and on 2026-09-24 a full re-extraction restamped every
triple with the same day, so 6 of the 7 live contradictions picked a winner by
accident. The note is what carries the time a fact was asserted, and until
then notes recorded no time at all.

Since that date every store stamps `created_at` on insert and `updated_at` when
the text changes. This module fills the notes written before that, from
evidence the data already holds -- strongest first, and never a guess:

  notion      Notion's own `last_edited`, which is when a person wrote it
  checkpoint  the capture time is in the id: checkpoint-<nanoseconds>-...
  session     the Claude Code transcript that called add_note with this exact
              title, and when (optional: pass the transcript directory)
  title       "(2026-09-25)", "(20 September 2026)", "June 15 2026",
              "(August 2026)" -- day precision before month precision
  opening     an ISO date in the note's first sentence ("On 2026-09-24, ...")

A note with none of these stays undated, and a contradiction that needs it
says "unresolved" rather than choosing.

  python -m brahmastra.note_times [--transcripts DIR]           # what would be set
  python -m brahmastra.note_times [--transcripts DIR] --apply   # set it, never overwriting
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Any

_ISO_DATE = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-([0-2]\d|3[01])\b")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_M = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})\s+" + _M + r",?\s+(20\d\d)\b", re.IGNORECASE)
_MONTH_DAY_YEAR = re.compile(r"\b" + _M + r"\s+(\d{1,2}),?\s+(20\d\d)\b", re.IGNORECASE)
_MONTH_YEAR = re.compile(r"\b" + _M + r"\s+(20\d\d)\b", re.IGNORECASE)
_CHECKPOINT_NS = re.compile(r"^checkpoint-(\d{18,20})-")
OPENING_CHARS = 200


def _day(y: str, mo: int, d: str) -> str | None:
    try:
        return datetime(int(y), mo, int(d), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def evidence_time(note: dict[str, Any],
                  sessions: dict[str, str] | None = None) -> tuple[str | None, str]:
    """(ISO time or None, which evidence gave it)."""
    if note.get("last_edited"):
        return str(note["last_edited"]), "notion"
    m = _CHECKPOINT_NS.match(str(note.get("id") or ""))
    if m:
        at = datetime.fromtimestamp(int(m.group(1)) / 1e9, tz=timezone.utc)
        return at.isoformat(), "checkpoint"
    title = str(note.get("title") or "")
    if sessions and title in sessions:
        return sessions[title], "session"
    m = _ISO_DATE.search(title)
    if m:
        return f"{m.group(0)}T00:00:00+00:00", "title"
    m = _DAY_MONTH_YEAR.search(title)
    if m and _day(m.group(3), _MONTHS[m.group(2).lower()], m.group(1)):
        return _day(m.group(3), _MONTHS[m.group(2).lower()], m.group(1)), "title"
    m = _MONTH_DAY_YEAR.search(title)
    if m and _day(m.group(3), _MONTHS[m.group(1).lower()], m.group(2)):
        return _day(m.group(3), _MONTHS[m.group(1).lower()], m.group(2)), "title"
    m = _MONTH_YEAR.search(title)
    if m:
        month = _MONTHS[m.group(1).lower()]
        return f"{m.group(2)}-{month:02d}-01T00:00:00+00:00", "title-month"
    m = _ISO_DATE.search(str(note.get("content") or "")[:OPENING_CHARS])
    if m:
        return f"{m.group(0)}T00:00:00+00:00", "opening"
    return None, "none"


def fact_time(note: dict[str, Any] | None) -> str | None:
    """
    When the statements in a note were last asserted. Notion's own edit time
    first (a person's clock), then when the text last changed here, then when
    it was first written.
    """
    if not note:
        return None
    return (note.get("last_edited") or note.get("updated_at")
            or note.get("created_at") or None)


def session_times(paths: list[str]) -> dict[str, str]:
    """{note title: when add_note was called with it}, from Claude Code JSONL."""
    import json

    found: dict[str, str] = {}
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "add_note" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for block in row.get("message", {}).get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and str(block.get("name", "")).endswith("add_note")):
                        title = (block.get("input") or {}).get("title")
                        if title and row.get("timestamp"):
                            found.setdefault(title, row["timestamp"])
    return found


def plan(notes: list[dict[str, Any]],
         sessions: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, int]]:
    times: dict[str, str] = {}
    counts: dict[str, int] = {}
    for note in notes:
        if note.get("created_at"):
            counts["already dated"] = counts.get("already dated", 0) + 1
            continue
        at, how = evidence_time(note, sessions)
        counts[how] = counts.get(how, 0) + 1
        if at:
            times[note["id"]] = at
    return times, counts


def main(argv: list[str] | None = None) -> int:
    from brahmastra import db

    argv = list(sys.argv[1:] if argv is None else argv)
    sessions: dict[str, str] = {}
    if "--transcripts" in argv:
        from pathlib import Path

        folder = Path(argv[argv.index("--transcripts") + 1])
        sessions = session_times([str(p) for p in folder.glob("*.jsonl")])
    times, counts = plan(db.get_notes(), sessions)
    print(f"evidence: {counts}")
    if "--apply" in argv:
        print(f"set created_at on {db.backfill_note_times(times)} notes")
    else:
        print(f"would set {len(times)}; run with --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
