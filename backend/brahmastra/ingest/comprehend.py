"""
Read one chunk and say what actually happened in it.

This is the stage that turns speech into business meaning: what was DECIDED,
who COMMITTED to what and by when, what RISKS were raised, what was left
OPEN. Those are the questions an organisation asks of its meetings, and none
of them survive the existing extraction path -- the ontology's 18 relations
contain no `decided`, no `action_item` and no `attended`, so every one of them
degrades to `related_to` and the meaning is gone.

IT FAILS CLOSED, AND THAT IS THE WHOLE POINT
--------------------------------------------
This repository has already been burned by exactly this shape of task. A 7B
model handed a conversation transcript fabricated an entire note -- an invented
commit, a push that never happened, a reply from a person who never said it --
because "write the next plausible turn" is the likeliest continuation of a
transcript. See docs/CHECKPOINTING_DESIGN.md; the defences there were paid for.

A meeting transcript is the same trap with higher stakes, because a fabricated
DECISION is not a bad summary, it is a false record that an organisation may
act on. So every artifact must earn its place:

  * it must carry a `quote`, and that quote must OCCUR IN THE CHUNK. This is
    the single strongest defence: a model can invent a decision, but it cannot
    invent a quote that is already in the source.
  * an owner must be someone who actually spoke or was named in the chunk.
  * anything that fails either test is DROPPED, and the drop is reported.

A missing decision is recoverable -- the transcript is still there and can be
re-run. A false decision, once it is in the knowledge base and someone has
searched it, is not.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from brahmastra.ingest.segment import Chunk

# Kinds of thing worth pulling out of a meeting. Deliberately short: each one
# has to be independently useful to query, or it is just a tag.
ARTIFACT_KINDS = ("decision", "action_item", "risk", "open_question")

# How much of the quote must appear verbatim before the artifact is trusted.
# Not the whole thing: models normalise whitespace, drop filler words and fix
# obvious transcription noise, all of which are harmless. Long enough that a
# fabricated quote cannot pass by accident.
QUOTE_ANCHOR_CHARS = 24


SYSTEM_PROMPT = """\
You extract a factual record from part of a meeting transcript.

Return ONLY a JSON object with this exact shape:

{
  "summary": "2-4 sentences of what happened in this passage, in plain prose",
  "topics": ["short topic labels"],
  "participants": ["names of people who spoke or were referred to"],
  "decisions": [
    {"statement": "what was decided", "rationale": "why, if stated",
     "owner": "person accountable, or null", "quote": "verbatim words from the passage"}
  ],
  "action_items": [
    {"task": "what will be done", "owner": "who committed, or null",
     "due": "date or timeframe as stated, or null", "quote": "verbatim words"}
  ],
  "risks": [
    {"description": "the risk or blocker", "owner": "who raised it, or null",
     "quote": "verbatim words"}
  ],
  "open_questions": [
    {"question": "what was left unresolved", "owner": "who asked, or null",
     "quote": "verbatim words"}
  ]
}

RULES, IN ORDER OF IMPORTANCE:

1. Every quote MUST be copied verbatim from the passage. Never paraphrase a
   quote, never compose one, never quote text that is not in the passage.
2. Record only what the passage actually contains. If nothing was decided,
   return an empty decisions array. Empty arrays are the correct answer far
   more often than not, and are always better than a plausible invention.
3. A decision is a settled choice, not a suggestion. "We should maybe look at
   X" is not a decision. "We're moving the date to April" is.
4. An action item needs someone doing something. A wish with no owner and no
   commitment is not an action item.
5. Use only names that appear in the passage. Never introduce a person.
6. If the passage is small talk, scheduling, or noise, say so in the summary
   and return empty arrays for everything else.
"""


@dataclass
class Artifact:
    """One typed fact recovered from a chunk."""
    kind: str
    statement: str
    owner: str | None = None
    due: str | None = None
    rationale: str | None = None
    quote: str | None = None
    chunk_index: int = 0
    speakers: list[str] = field(default_factory=list)
    start_time: str | None = None
    end_time: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkUnderstanding:
    """Everything one chunk yielded, including what was thrown away and why."""
    chunk_index: int
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Collapse whitespace and case so harmless reformatting does not fail a quote."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_is_grounded(quote: str | None, source: str) -> bool:
    """
    Does this quote actually occur in the passage?

    The strongest signal available, and cheap. A model asked to invent a
    decision will happily do so, but it cannot invent a quote that is already
    in the source -- so an ungrounded quote is near-perfect evidence that the
    artifact around it was composed rather than observed.

    Matched on a prefix rather than the whole string because models normalise
    whitespace and trim filler; that is reformatting, not fabrication.
    """
    if not quote:
        return False
    needle = _normalise(quote)
    if len(needle) < QUOTE_ANCHOR_CHARS:
        # Too short to be evidence of anything. "Yes." occurs in every meeting.
        return False
    return needle[:QUOTE_ANCHOR_CHARS] in _normalise(source)


def owner_is_named(owner: str | None, source: str, participants: list[str]) -> bool:
    """
    An owner must be someone the passage actually mentions.

    An action item assigned to an invented person is worse than an unassigned
    one: it looks actionable and is addressed to nobody.
    """
    if not owner:
        return True                      # unassigned is honest, and allowed
    haystack = _normalise(source + " " + " ".join(participants))
    return _normalise(owner) in haystack


# ---------------------------------------------------------------------------
# Reading the reply
# ---------------------------------------------------------------------------

def _as_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clean_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


_FIELD_BY_KIND = {
    "decision": "statement",
    "action_item": "task",
    "risk": "description",
    "open_question": "question",
}


def build_understanding(payload: dict[str, Any], chunk: Chunk) -> ChunkUnderstanding:
    """
    Turn a parsed reply into verified artifacts, dropping anything unproven.

    Separated from the LLM call so the verification rules -- the part that
    actually protects the knowledge base -- can be tested exhaustively without
    a provider.
    """
    result = ChunkUnderstanding(chunk_index=chunk.index)
    result.summary = (payload.get("summary") or "").strip()
    result.topics = _clean_strings(payload.get("topics"))
    result.participants = _clean_strings(payload.get("participants"))

    source = chunk.text
    plural = {
        "decision": "decisions",
        "action_item": "action_items",
        "risk": "risks",
        "open_question": "open_questions",
    }

    for kind in ARTIFACT_KINDS:
        for item in _as_list(payload, plural[kind]):
            statement = (item.get(_FIELD_BY_KIND[kind]) or "").strip()
            quote = (item.get("quote") or "").strip() or None
            owner = (item.get("owner") or "").strip() or None

            if not statement:
                result.rejected.append(f"{kind}: empty statement")
                continue
            if not quote_is_grounded(quote, source):
                # The decisive check. See the module docstring: a fabricated
                # decision in a knowledge base is a false record, not a bad
                # summary, and a missing one is always recoverable.
                result.rejected.append(
                    f"{kind}: quote not found in the passage — {statement[:60]!r}"
                )
                continue
            if not owner_is_named(owner, source, result.participants):
                result.rejected.append(
                    f"{kind}: owner {owner!r} is not named in the passage"
                )
                owner = None

            result.artifacts.append(Artifact(
                kind=kind,
                statement=statement,
                owner=owner,
                due=(item.get("due") or "").strip() or None,
                rationale=(item.get("rationale") or "").strip() or None,
                quote=quote,
                chunk_index=chunk.index,
                speakers=chunk.speakers,
                start_time=chunk.start_time,
                end_time=chunk.end_time,
            ))

    return result


def _parse_reply(raw: str) -> dict[str, Any]:
    """Read the JSON, tolerating a fenced block. Mirrors extraction.py."""
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in reply: {raw[:200]!r}")
    return json.loads(text[start:end + 1])


def comprehend_chunk(chunk: Chunk, max_tokens: int | None = None) -> ChunkUnderstanding:
    """
    One LLM pass over one chunk. Never raises: a chunk that fails is reported
    and the document continues.

    A transcript is many chunks, and one bad chunk must not cost the other
    forty. This mirrors how extraction treats a note that fails: record it,
    move on, retry later.
    """
    from brahmastra.llm import chat

    budget = max_tokens or int(os.environ.get("INGEST_COMPREHEND_TOKENS", "") or 1600)
    try:
        raw = chat(
            SYSTEM_PROMPT,
            f"Passage {chunk.index + 1} of the transcript:\n\n{chunk.text}",
            json_mode=True,
            temperature=0.1,     # this is a record, not a composition
            max_tokens=budget,
        )
    except Exception as exc:
        return ChunkUnderstanding(
            chunk_index=chunk.index, error=f"{type(exc).__name__}: {exc}"[:300]
        )

    try:
        payload = _parse_reply(raw)
    except Exception as exc:
        return ChunkUnderstanding(
            chunk_index=chunk.index, error=f"unparseable reply: {exc}"[:300]
        )

    return build_understanding(payload, chunk)
