"""
The whole session in a few lines, written from the part notes.

meeting-scribe's second tier: once every part is read, one call turns the
parts' topics, summaries and key points into a headline, a summary and themes.
It reads the NOTES, never the transcript, so its prompt stays small however
long the session ran -- an hour of speech is a few dozen points here.

What it may not do is add anything: the notes are already held to the
transcript, and the prompt forbids going beyond them. Nothing here is checked
against quotes, because a summary has none, so the overview is shown as what
it is -- written from the notes -- and never becomes graph triples.

A failure costs the overview and nothing else: the parts and items stand.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from brahmastra.ingest.modes import Mode

OVERVIEW_PROMPT = """\
You summarise a whole {session} from the notes taken on it, part by part.
You never see the transcript, only the notes, in order.

Return ONLY a JSON object:

{{
  "headline": "one short sentence: what this {noun} was actually about",
  "summary": "plain prose covering the arc: {arc}",
  "themes": [{{"title": "...", "points": ["..."]}}]
}}

RULES:
1. Write it so someone who missed the {noun} could follow it without the notes.
   Let the {noun} decide the length: a short one gets a short paragraph.
2. Themes group the recurring subjects, as many as the {noun} genuinely had.
   Merge duplicates ruthlessly: two parts covering the same ground are ONE theme.
   Each theme's points carry the actual detail, not a restatement of its title.
3. Nothing that is not in the notes. No outside knowledge, no guessed context.
"""

_ARC = {
    "meeting": "what prompted it, what was argued, where it landed, what is still open",
    "lecture": "what was taught, in the order it built up, and what the audience asked",
}
_NOUN = {"meeting": "meeting", "lecture": "session"}
_SESSION = {"meeting": "meeting", "lecture": "lecture or training session"}


def render_notes(parts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for p in parts:
        when = f"[{p['start_time']}] " if p.get("start_time") else ""
        lines.append(f"{when}Part {p['idx'] + 1}: {p.get('topic') or ''}".rstrip())
        if p.get("summary"):
            lines.append(f"  {p['summary']}")
        lines += [f"  - {pt}" for pt in p.get("points") or []]
    return "\n".join(lines)


def write_overview(mode: Mode, title: str, parts: list[dict[str, Any]],
                   chat: Callable[..., str] | None = None) -> dict[str, Any]:
    """{"headline", "summary", "themes"} or {"error": ...}. Never raises."""
    notes = render_notes(parts)
    if not notes.strip():
        return {"error": "no notes to summarise"}
    if chat is None:
        from brahmastra.ingest.comprehend import _cached_chat as chat
    system = OVERVIEW_PROMPT.format(session=_SESSION.get(mode.id, "session"),
                                    noun=_NOUN.get(mode.id, "session"),
                                    arc=_ARC.get(mode.id, _ARC["meeting"]))
    try:
        raw = chat(system, f"Title: {title}\n\nNOTES, IN ORDER:\n{notes}",
                   json_mode=True, temperature=0.1,
                   max_tokens=min(6000, 2000 + 60 * notes.count("\n")))
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
    except Exception as exc:                                   # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    themes = []
    for t in data.get("themes") or []:
        if isinstance(t, dict) and str(t.get("title") or "").strip():
            themes.append({"title": str(t["title"]).strip(),
                           "points": [str(p).strip() for p in (t.get("points") or []) if str(p).strip()]})
    return {"headline": str(data.get("headline") or "").strip(),
            "summary": str(data.get("summary") or "").strip(),
            "themes": themes}
