"""
Read a session part by part, according to its mode (ingest/modes.py).

Two things here come from Shaan's meeting-scribe, and one thing does not.

FROM MEETING-SCRIBE: each part is written up as a topic and KEY POINTS that
carry the substance -- names, numbers, reasons, definitions -- and each part is
told what the previous one already noted, so it reports only what it adds.
Brahmastra used to keep 2-4 sentences of summary per part, so the substance of
a session never reached the graph: a lecture's concepts appeared in no item and
in no note.

NOT FROM MEETING-SCRIBE: every point quotes the words it came from, and the
same checks that hold decisions to the transcript hold points to it. A point
whose quote is not in the passage is dropped and reported. meeting-scribe's
notes are unchecked, and its own saved output has "Action (Unassigned): None".

WHAT A MEETING STILL GETS UNCHANGED: its measured passes (comprehend.py,
`comprehension_strategy`) run exactly as before -- same prompts, so the same
memo keys and the same findings. The notes pass is added beside them.
"""

from __future__ import annotations

from typing import Any, Callable

from brahmastra.ingest.comprehend import (
    AUDIENCE_SPECS,
    POINT_SPECS,
    ChunkUnderstanding,
    _one_pass,
    build_understanding,
)
from brahmastra.ingest.modes import Mode, get_mode
from brahmastra.ingest.segment import Chunk

NOTES_BUDGET = 2400
AUDIENCE_BUDGET = 1800
# How much of the previous part is repeated to the next. One part back is what
# meeting-scribe found enough to stop repetition; more makes every prompt grow.
RECAP_POINTS = 15

NOTES_PROMPT = """\
You take notes on one part of {session}. You get the notes already taken for
the part before it, and the new part of the transcript. Write down only what
the new part adds.

Return ONLY a JSON object:

{{
  "topic": "a short label for what this part is about",
  "summary": "2-4 sentences on what happened in this part, in plain prose",
  "points": [
    {{"point": "one full, specific statement",
      "quote": "verbatim words from the passage that back it",
      "about": [{{"name": "a person, product, organisation or idea the point is about",
                 "type": "person | project | concept | tool | organisation | event | location | feature"}}]}}
  ]
}}

WHAT A POINT IS: {brief}

RULES:
1. Each point is a full, specific statement that carries the detail: names,
   numbers, reasons, definitions. Never a headline, never "they discussed X".
2. Every quote MUST be copied verbatim from the new part. Never compose one.
3. Cover everything the new part adds, as many points as that takes, and no
   more. Never repeat or reword a point from the earlier notes.
4. Use only names that appear in the passage. Never introduce a person.
5. Small talk, greetings, logistics, sound checks and screen-sharing trouble
   are not points. If the part is only that, return an empty list.
6. "about" names what the point is ABOUT, as it is named in the passage:
   "Priya", "the payments integration", "Brent", "swap". Not the speaker
   unless the point is about them. Two to four names is usual; never a
   pronoun, never a whole sentence.
"""

_NOTES_BRIEF = {
    "meeting": (
        "something said that matters for understanding the discussion: a position "
        "someone took and why, a fact or number stated, an option weighed, the "
        "context behind a choice. Attribute a view to its speaker when that matters "
        "(\"Priya prefers option B because it ships sooner\")."
    ),
    "lecture": (
        "something TAUGHT: a definition, how something works, a rule, a worked "
        "example with its numbers, a comparison between two things. Write it so it "
        "is true on its own and could be studied from -- \"The buyer of a swap pays "
        "fixed and receives floating\", not \"She explained swaps\". A resource the "
        "presenter recommends -- a film, a book, a site, a tool -- is a point too, with "
        "why it was recommended. The presenter's check-ins (\"Is this making sense?\"), "
        "quiz questions put to the class, and stories that teach nothing are not points."
    ),
}

_SESSION_NAME = {"meeting": "a meeting", "lecture": "a lecture or training session"}

AUDIENCE_PROMPT = """\
You record the AUDIENCE side of one part of a lecture or training session:
the questions attendees asked, and the follow-ups someone committed to.

Return ONLY a JSON object:

{
  "questions": [
    {"question": "the question as the attendee asked it", "asked_by": "name, or null",
     "quote": "verbatim words of the question",
     "answer": "how it was answered in this passage, or null",
     "answer_quote": "verbatim words of the answer, or null"}
  ],
  "action_items": [
    {"task": "what will be done", "owner": "who committed, or null",
     "due": "date or timeframe as stated, or null", "quote": "verbatim words"}
  ]
}

RULES:
1. A QUESTION here is one someone in the AUDIENCE asked. The presenter's own
   questions -- quizzes put to the class, "Is this making sense?", "Everybody
   understood?", questions they go on to answer themselves -- are NOT audience
   questions, however many there are.
2. Give an answer only if this passage contains it, with its own verbatim quote.
3. An ACTION ITEM is a real follow-up someone committed to: a quiz or
   assignment to be released, material to share, a session to schedule.
   A suggestion ("try to watch this film") is not one, and neither is an
   instruction that is part of the lesson itself.
4. Every quote MUST be copied verbatim from the passage. Never compose one.
5. Use only names that appear in the passage. Never introduce a person.
6. Empty arrays are the correct answer when the passage holds none.
"""


def _recap(topic: str, points: list[str]) -> str:
    if not topic and not points:
        return "(this is the first part)"
    lines = [f"Topic: {topic}"] if topic else []
    lines += [f"- {p}" for p in points[-RECAP_POINTS:]]
    return "\n".join(lines)


class SessionReader:
    """
    Reads one session's parts IN ORDER, remembering the previous part's notes.

    One reader per document: the recap makes each part's reading depend on the
    part before, so a reader must not be shared between transcripts. The memo
    still applies -- the recap is part of the message, so it is in the key.
    """

    def __init__(self, mode: str | Mode | None = None,
                 meeting_strategy: Callable[[Chunk], ChunkUnderstanding] | None = None):
        self.mode = mode if isinstance(mode, Mode) else get_mode(mode)
        self._meeting_strategy = meeting_strategy
        self._topic = ""
        self._points: list[str] = []

    # -- passes --------------------------------------------------------------

    def _notes(self, chunk: Chunk) -> tuple[ChunkUnderstanding | None, str | None]:
        system = NOTES_PROMPT.format(session=_SESSION_NAME.get(self.mode.id, "a session"),
                                     brief=_NOTES_BRIEF.get(self.mode.id, _NOTES_BRIEF["meeting"]))
        # The recap goes in the same message as the passage, so the memo key
        # covers it: a different previous part is a different reading.
        payload, err = _one_pass_with_prefix(
            chunk, system, NOTES_BUDGET,
            f"NOTES FROM THE PART BEFORE:\n{_recap(self._topic, self._points)}\n\n")
        if payload is None:
            return None, err
        return build_understanding(payload, chunk, specs=POINT_SPECS), None

    def _audience(self, chunk: Chunk) -> tuple[ChunkUnderstanding | None, str | None]:
        payload, err = _one_pass(chunk, AUDIENCE_PROMPT, AUDIENCE_BUDGET)
        if payload is None:
            return None, err
        return build_understanding(payload, chunk, specs=AUDIENCE_SPECS), None

    # -- one part -----------------------------------------------------------

    def __call__(self, chunk: Chunk) -> ChunkUnderstanding:
        if self.mode.id == "lecture":
            result = self._read_lecture(chunk)
        else:
            result = self._read_meeting(chunk)
        if not result.error:
            self._topic = result.topic or self._topic
            self._points = [a.statement for a in result.artifacts if a.kind == "point"]
        return result

    def _read_meeting(self, chunk: Chunk) -> ChunkUnderstanding:
        from brahmastra.ingest.assemble import comprehension_strategy

        strategy = self._meeting_strategy or comprehension_strategy()
        result = strategy(chunk)
        if result.error:
            return result
        notes, err = self._notes(chunk)
        result.calls += 1
        if notes is None:
            # Degraded, not failed: the decisions are real without the notes.
            result.rejected.append(f"pass failed: notes: {err}")
            return result
        result.topic = notes.topic
        result.artifacts.extend(notes.artifacts)
        result.rejected.extend(notes.rejected)
        return result

    def _read_lecture(self, chunk: Chunk) -> ChunkUnderstanding:
        notes, err_a = self._notes(chunk)
        audience, err_b = self._audience(chunk)
        if notes is None and audience is None:
            return ChunkUnderstanding(chunk_index=chunk.index, error=err_a or err_b, calls=2)
        result = ChunkUnderstanding(chunk_index=chunk.index, calls=2)
        for part in (notes, audience):
            if part is None:
                continue
            result.artifacts.extend(part.artifacts)
            result.rejected.extend(part.rejected)
        if notes is not None:
            result.summary, result.topic = notes.summary, notes.topic
        result.participants = list(chunk.speakers)
        for name, err in (("notes", err_a), ("audience", err_b)):
            if err:
                result.rejected.append(f"pass failed: {name}: {err}")
        return result


# The presenter holds the floor: in the one real lecture measured, one person
# spoke 95% of the words. Required well above half, so a panel or a session
# that turns into a discussion names no presenter and nothing is dropped.
PRESENTER_SHARE = 0.6


def presenter_of(chunks: list[Chunk]) -> str | None:
    """Whoever spoke most of the session's words, if one person clearly did."""
    words: dict[str, int] = {}
    seen: set[tuple[int, int]] = set()
    for chunk in chunks:
        for turn in chunk.turns:
            key = (turn.start_char, turn.end_char)
            if turn.speaker and key not in seen:        # overlap repeats turns
                seen.add(key)
                words[turn.speaker] = words.get(turn.speaker, 0) + len(turn.text.split())
    total = sum(words.values())
    if not total:
        return None
    name, count = max(words.items(), key=lambda kv: kv[1])
    return name if count / total >= PRESENTER_SHARE else None


def drop_presenter_questions(artifacts: list[Any], chunks: list[Chunk]) -> tuple[list[Any], list[str]]:
    """
    A lecture's questions are the AUDIENCE's. The prompt says so and a model
    still kept the presenter's "Does this time doesn't work?" in one of two
    runs. Who spoke a quote is known from the transcript, so this is decided
    there rather than asked again: a question the presenter asked is dropped.
    """
    from brahmastra.ingest.evidence import speaker_of

    presenter = presenter_of(chunks)
    if not presenter:
        return artifacts, []
    by_index = {c.index: c for c in chunks}
    kept, dropped = [], []
    for a in artifacts:
        if a.kind == "question" and speaker_of(a.quote or "", by_index.get(a.chunk_index)) == presenter:
            dropped.append(f"question: asked by the presenter {presenter} — {a.statement[:60]!r}")
            continue
        kept.append(a)
    return kept, dropped


def _one_pass_with_prefix(chunk: Chunk, system: str, budget: int,
                          prefix: str) -> tuple[dict[str, Any] | None, str | None]:
    """`_one_pass`, with context placed before the passage in the same message."""
    from brahmastra.ingest.comprehend import _cached_chat, _parse_reply

    try:
        raw = _cached_chat(
            system,
            f"{prefix}Part {chunk.index + 1} of the transcript (the NEW part):\n\n{chunk.text}",
            json_mode=True, temperature=0.1, max_tokens=budget)
    except Exception as exc:                                   # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"[:300]
    try:
        return _parse_reply(raw), None
    except Exception as exc:                                   # noqa: BLE001
        return None, f"unparseable reply: {exc}"[:300]
