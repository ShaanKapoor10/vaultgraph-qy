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
from difflib import SequenceMatcher
from typing import Any

from brahmastra.ingest import evidence
from brahmastra.ingest.segment import Chunk

# Kinds of thing worth pulling out of a meeting. Deliberately short: each one
# has to be independently useful to query, or it is just a tag.
ARTIFACT_KINDS = ("decision", "action_item", "risk", "open_question")

# How much of the quote must appear verbatim before the artifact is trusted.
# Not the whole thing: models normalise whitespace, drop filler words and fix
# obvious transcription noise, all of which are harmless. Long enough that a
# fabricated quote cannot pass by accident.
QUOTE_ANCHOR_CHARS = 24

# And it must cover this much of the quote. Without it, a fabricated sentence
# containing one real phrase would pass: sharing a phrase with the passage is
# not the same as having been drawn from it.
QUOTE_COVERAGE = 0.6


# A RULE THAT WAS TRIED, MEASURED AND REMOVED: "SELF-CONTAINED STATEMENTS"
#
# cocoindex's conversation_to_knowledge requires every extracted name to be
# self-contained and forbids anaphora, and there was a real artifact here that
# the rule should have fixed:
#
#     label      "Raj completes the reconciliation job by the 27th"
#     produced   "take the reconciliation job"
#
# 0.570 against a 0.60 threshold, so it scored as BOTH a miss and an
# unlabelled finding. So all five prompts were given a block forbidding bare
# verbs and pronouns, with that exact before-and-after as the example.
#
# It did not work. focused, gpt-oss-120b, three runs over each of two cases:
#
#                      recall            precision        traps
#     without      70% [64-79]       61% [50-73]      0.2 (worst 1)
#     with         64% [57-71]       59% [54-77]      0.2 (worst 1)
#
# The ranges overlap, so the six-point drop is not a finding either -- this
# harness has put the same configuration at 64% and 36% on consecutive runs.
# The honest reading is NO MEASURABLE EFFECT, for a block of prompt in every
# call. And the specific failure it targeted survived it: the same run still
# produced "Change the alert to trigger after fifteen minutes of queue depth
# growth", subject-less in exactly the way the rule forbade.
#
# WHAT THE MEASUREMENT DID FIND, and where to look instead. The pair that
# scored worst was never a prompt problem at all:
#
#     label      "Whether to move off the current webhook vendor"
#     produced   "Should we move off this vendor entirely?"      cosine 0.523
#
# The same open question, counted as a miss AND a fabrication. Attribution,
# meanwhile, was already perfect -- 100% [100%-100%] over six runs -- so the
# owner half of this idea had nothing to fix. The gap is in the MATCHER, not
# in the prompts, and lowering its threshold to fit five hand-picked pairs is
# how the cross-encoder in evidence.py came to be adopted and then reverted.

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
    # Filled by consolidate(): how many times the document said this, and what
    # replaced it if the meeting revisited the question. Something said three
    # times is more load-bearing than something said once, and a decision that
    # was later reversed is worth keeping as history rather than deleting.
    mentions: int = 1
    superseded_by: str | None = None
    # Filled by the store when the artifact is written, and DERIVED from the
    # artifact rather than drawn at random -- so the same decision keeps the
    # same id across re-ingestions. None until then: an artifact that has been
    # comprehended but not stored genuinely has no identity yet, and inventing
    # one here would make that indistinguishable. See store.artifact_id.
    id: str | None = None

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
    # LLM calls this chunk actually cost. Reported rather than inferred because
    # the evaluation compares a one-call variant against a two-call one, and it
    # counted chunks -- so `focused` and `single` both reported the same cost
    # and the comparison was "is it better?" when the question it exists to
    # answer is "is it better ENOUGH to be worth twice the calls?", which on a
    # rate-limited tier is the whole decision.
    calls: int = 1


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

    MATCHED ON THE LONGEST SHARED RUN, not on a prefix. Anchoring the first N
    characters looked equivalent and was not: a model that prepends one word --
    quoting "That's one thing that worries me" where the source says "One thing
    that worries me" -- fails a quote that is otherwise verbatim. That is
    reformatting, exactly what this check is meant to tolerate, and it was
    silently costing real findings rather than catching invented ones.

    The run must also be a decent FRACTION of the quote, so that a fabricated
    sentence which happens to contain one real phrase is still refused. Sharing
    a phrase is not the same as being drawn from the passage.
    """
    if not quote:
        return False
    needle = _normalise(quote)
    if len(needle) < QUOTE_ANCHOR_CHARS:
        # Too short to be evidence of anything. "Yes." occurs in every meeting.
        return False

    haystack = _normalise(source)
    if needle in haystack:
        return True

    # autojunk treats frequent characters as noise once a sequence passes 200
    # elements, which is every realistic passage -- and it would quietly shrink
    # the match it reports.
    match = SequenceMatcher(None, needle, haystack, autojunk=False).find_longest_match(
        0, len(needle), 0, len(haystack)
    )
    return match.size >= max(QUOTE_ANCHOR_CHARS, int(len(needle) * QUOTE_COVERAGE))


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
            if not quote_supports_statement(statement, quote):
                # Real quote, wrong claim. Dropped rather than kept with a
                # caveat, because an artifact whose own evidence contradicts it
                # is worse than a missing one: it reads as sourced, and the
                # transcript is still on disk to re-run.
                result.rejected.append(
                    f"{kind}: quote contradicts the dates in — {statement[:60]!r}"
                )
                continue
            if not evidence.evidence_supports(statement, quote, chunk):
                # The quote is real and shares the statement's dates, and a
                # DIFFERENT sentence in the same passage supports the claim
                # far better -- which is what a misattached citation looks
                # like. See ingest/evidence.py for why this is asked as a
                # ranking rather than as a score against a threshold.
                result.rejected.append(
                    f"{kind}: better evidence exists in the passage — {statement[:60]!r}"
                )
                continue
            # Existence before attribution. An owner who never appears in the
            # passage is invented, and saying they "did not speak the quote"
            # would be true but would name the symptom rather than the cause.
            if owner and not owner_is_named(owner, source, result.participants):
                result.rejected.append(
                    f"{kind}: owner {owner!r} is not named in the passage"
                )
                owner = None
            if owner and not evidence.attribution_is_consistent(owner, quote, chunk):
                # A real person, but not the one who made this commitment. A
                # first-person quote belongs to whoever spoke it, so it cannot
                # support an artifact owned by somebody else. The statement is
                # usually true, so the OWNER goes and the finding stays.
                result.rejected.append(
                    f"{kind}: owner {owner!r} did not speak the cited quote"
                )
                owner = None
            if not owner:
                # Recovered rather than left blank. A first-person quote names
                # its owner by who said it, so an artifact quoting "I'll update
                # the roadmap" belongs to whoever spoke that line -- including
                # the one whose wrong owner was just removed above.
                owner = evidence.owner_from_speaker(quote, chunk)

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


def quote_supports_statement(statement: str, quote: str | None) -> bool:
    """
    Does this quote actually back up what the artifact claims?

    `quote_is_grounded` proves the quote is REAL. It cannot prove the quote is
    the right one, and a model under pressure will attach a genuine sentence to
    a claim it does not support. Observed live, from a 7B: an action item
    reading "Move the Q3 release date to April 15th" carrying the quote "I'll
    update the roadmap by Friday so nobody's working off the March date."
    Verbatim, in the passage, and about something else -- so it passed every
    check and landed in the knowledge base as sourced.

    COSINE CANNOT DO THIS, measured on that exact run:

        0.40  "Move the Q3 release date to April 15th"     <- roadmap/Friday quote  (WRONG)
        0.40  "The release date was moved ... to April 15th" <- "moving the release to April 15th"  (RIGHT)

    The same number for the fabricated pairing and the correct one, so any
    threshold either keeps the bad artifact or discards a good decision. This
    is the second time an embedding has been unable to make a distinction this
    module depends on -- see MATCH_THRESHOLD in evaluate.py.

    Dates and numbers can. A statement that commits to "April 15th" while its
    evidence says "March" and "Friday" is not supported by that evidence, and
    those are exactly the tokens a meeting record turns on. Reusing
    `_key_facts` from consolidate.py, which already extracts them to detect a
    revision.

    Judged only when BOTH sides carry facts. A statement with no date and no
    number cannot be checked this way, and a quote with none is not evidence
    against anything -- in both cases this abstains rather than guessing, which
    keeps it from dropping the many true artifacts that never mention a number.
    """
    from brahmastra.ingest.consolidate import _key_facts

    claimed = _key_facts(statement)
    evidenced = _key_facts(quote)
    if not claimed or not evidenced:
        return True
    return bool(claimed & evidenced)


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
        raw = _cached_chat(
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


# ---------------------------------------------------------------------------
# The focused variant: two passes instead of one
# ---------------------------------------------------------------------------
#
# The single pass asks for a summary plus four artifact kinds in one reply, and
# the evaluation showed exactly what that costs: every decision found, and most
# of the risks, commitments and open questions dropped. Divided attention,
# concentrated in the kinds that are not decisions.
#
# So this splits the work by what the model is looking FOR, not by what it is
# looking AT -- both passes read the same chunk. Two calls rather than four,
# because the split that matters is commitments (settled, forward-looking) from
# concerns (unsettled, raised), and a pass per kind would double the cost again
# for a distinction the model does not seem to struggle with.
#
# Kept beside the single pass rather than replacing it, so `evaluate --variant`
# can put the two side by side on the same transcript. An architecture adopted
# without that comparison is a guess.

COMMITMENTS_PROMPT = """\
You record what a meeting SETTLED: decisions taken, and work people committed to.

Return ONLY a JSON object:

{
  "summary": "2-4 sentences of what happened in this passage, in plain prose",
  "topics": ["short topic labels"],
  "participants": ["names of people who spoke or were referred to"],
  "decisions": [
    {"statement": "what was decided", "rationale": "why, if stated",
     "owner": "person accountable, or null", "quote": "verbatim words"}
  ],
  "action_items": [
    {"task": "what will be done", "owner": "who committed, or null",
     "due": "date or timeframe as stated, or null", "quote": "verbatim words"}
  ]
}

RULES:
1. Every quote MUST be copied verbatim from the passage. Never compose one.
2. A decision is a SETTLED choice. "We should maybe look at X" is not one;
   "we're moving the date to April" is.
3. An action item is someone committing to do something. A wish with no owner
   and no commitment is not one. If a person says "I'll do X", that is one.
4. A decision and the action that carries it out are DIFFERENT things. Record
   both when both are present.
5. Use only names that appear in the passage. Never introduce a person.
6. Empty arrays are correct far more often than not, and always better than a
   plausible invention.
"""

CONCERNS_PROMPT = """\
You record what a meeting left UNSETTLED: risks raised, and questions unanswered.

Return ONLY a JSON object:

{
  "risks": [
    {"description": "the risk, blocker or exposure", "owner": "who raised it, or null",
     "quote": "verbatim words"}
  ],
  "open_questions": [
    {"question": "what was asked and not answered in this passage",
     "owner": "who asked, or null", "quote": "verbatim words"}
  ]
}

RULES:
1. Every quote MUST be copied verbatim from the passage. Never compose one.
2. A RISK is anything named as able to go wrong: a blocker, a dependency, an
   exposure, something flaky, a contract that may be breached. Include it even
   when nobody proposed a fix.
3. An OPEN QUESTION is asked and left hanging in this passage. If someone
   answers it here, it is not open. "Let's not answer that now" leaves it open.
4. Something can be both a risk and the subject of an open question. Record it
   in both lists when it genuinely is.
5. Use only names that appear in the passage. Never introduce a person.
6. Empty arrays are correct when the passage holds none.
"""

_COMMITMENT_KINDS = ("decision", "action_item")
_CONCERN_KINDS = ("risk", "open_question")



def _cached_chat(system: str, user: str, **kwargs: Any) -> str:
    """
    The model's reply, from cache when this exact reading has been done before.

    Keyed on the prompt, the model, and THE WHOLE USER MESSAGE -- cocoindex's
    `hash(input) + hash(code)`, where the prompt is the code. Editing a prompt
    therefore invalidates everything read under the old one, which is why the
    prompt is in the key rather than a version number somebody has to remember
    to bump.

    It used to key on `chunk.text` alone rather than on the message built from
    it. That was correct only by accident: the message happened to be a pure
    function of the chunk. The moment anything else reaches the model -- a
    speaker roster resolved across the whole document, a retrieved fact, the
    first pass's output -- the key would silently stop covering part of the
    input, and the cache would answer with a reading of something it was not
    asked about. Keying the message itself cannot drift that way.
    """
    from brahmastra.llm import active_model, chat
    from brahmastra.ingest import memo

    model = ""
    try:
        model = active_model()
    except Exception:
        pass

    key = memo.key_for(user, "chat", model, system)
    hit = memo.load(key)
    if hit is not None:
        return hit

    reply = chat(system, user, **kwargs)
    memo.save(key, reply)
    return reply


def _one_pass(chunk: Chunk, system: str, budget: int,
              json_schema: dict[str, Any] | None = None,
              ) -> tuple[dict[str, Any] | None, str | None]:
    """
    One call. Returns (payload, error) -- never raises.

    With a `json_schema` the provider ENFORCES the reply shape; without one it
    is merely asked for valid JSON and told the shape in prose.
    """
    from brahmastra.llm import chat

    try:
        raw = _cached_chat(
            system, f"Passage {chunk.index + 1} of the transcript:\n\n{chunk.text}",
            json_mode=json_schema is None, json_schema=json_schema,            temperature=0.1, max_tokens=budget)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
    try:
        return _parse_reply(raw), None
    except Exception as exc:
        return None, f"unparseable reply: {exc}"[:300]


def comprehend_chunk_focused(chunk: Chunk,
                             max_tokens: int | None = None) -> ChunkUnderstanding:
    """
    Two specialised passes over the same chunk, merged.

    Costs twice the calls, so it has to earn that on the evaluation rather than
    on plausibility. Degrades rather than fails: if the concerns pass errors,
    the commitments it already found are still returned, because half a record
    is worth more than none and the transcript can always be re-run.
    """
    budget = max_tokens or int(os.environ.get("INGEST_COMPREHEND_TOKENS", "") or 1600)

    commitments, err_a = _one_pass(chunk, COMMITMENTS_PROMPT, budget)
    concerns, err_b = _one_pass(chunk, CONCERNS_PROMPT, budget)

    if commitments is None and concerns is None:
        return ChunkUnderstanding(chunk_index=chunk.index, error=err_a or err_b,
                                  calls=2)

    merged: dict[str, Any] = dict(commitments or {})
    for key in ("risks", "open_questions"):
        merged[key] = (concerns or {}).get(key, [])

    result = build_understanding(merged, chunk)
    result.calls = 2
    # A pass that failed is reported without failing the chunk, so a partial
    # result is visibly partial rather than quietly thin.
    for err in (err_a, err_b):
        if err:
            result.rejected.append(f"pass failed: {err}")
    return result


# ---------------------------------------------------------------------------
# The per-kind variant: one specialist per artifact kind
# ---------------------------------------------------------------------------
#
# Shaan's hypothesis, and it already has evidence behind it on this data:
# splitting one broad pass into two narrower ones took gpt-oss-120b from 43% to
# 69% recall. If attention is the constraint, four specialists should beat two.
# If instead the two-way split captured the whole gain -- commitments and
# concerns are genuinely different reading tasks, while decisions and action
# items are nearly the same one -- then four calls buy nothing over two and
# cost double.
#
# Built from one template rather than four hand-written prompts, so the only
# thing that differs between specialists is what they are looking for. Four
# prompts drifting apart in wording would make the comparison meaningless.
#
# MEASURED 2026-09-21, AND THE RESULT IS INCONCLUSIVE ON QUALITY BUT CONCLUSIVE
# ON COST. Two attempts over two labelled cases, three runs each:
#
#     focused  (2 calls/chunk)   69% [64-79]   stable
#     per-kind (4 calls/chunk)   56% [45-64]   at a 1200-token budget
#     per-kind (4 calls/chunk)   31% [ 0-64]   at a matched 1600-token budget
#
# The second attempt contains 0% runs, and those are Groq's DAILY CAP rather
# than the architecture: four calls per chunk burns the free tier four times
# faster, and the run that scored zero had "LLMQuotaExhausted" on its only
# chunk. So the quality question is still open -- it needs a tier that will not
# run out mid-measurement -- while the operational answer is already clear: on
# this infrastructure, four specialists exhaust the budget and fail.
#
# What IS established is that the two-way split was not merely "more agents".
# Commitments and concerns are genuinely different reading tasks; decisions and
# action items are nearly the same one. Splitting along a seam that is not
# there costs calls and gains nothing obvious.

_KIND_BRIEF = {
    "decision": ("decisions", "statement",
                 "a settled choice the group actually made. "
                 "\"We should maybe look at X\" is not one; \"we're moving the "
                 "date to April\" is. A decision NOT to do something counts."),
    "action_item": ("action_items", "task",
                    "something a named person committed to doing. A wish with "
                    "no owner and no commitment is not one."),
    "risk": ("risks", "description",
             "anything named as able to go wrong: a blocker, a dependency, an "
             "exposure, something flaky, a contract that may be breached. "
             "Include it even when nobody proposed a fix."),
    "open_question": ("open_questions", "question",
                      "something asked and left hanging in this passage. If "
                      "someone answers it here it is not open. \"Let's not "
                      "answer that now\" leaves it open."),
}

_ONE_KIND_PROMPT = """\
You read part of a meeting transcript and extract ONE kind of thing. Ignore
everything else, however interesting.

You are looking for {plural}: {brief}

Return ONLY a JSON object of this shape:

{{
  "participants": ["names of people who spoke or were referred to"],
  "{plural}": [
    {{"{field}": "what it is, in one plain sentence",
     "owner": "the person, or null", "due": "date or timeframe as stated, or null",
     "quote": "verbatim words from the passage"}}
  ]
}}

RULES:
1. Every quote MUST be copied verbatim from the passage. Never compose one.
2. Use only names that appear in the passage. Never introduce a person.
3. An empty array is the correct answer when the passage holds none, and is
   always better than a plausible invention.
4. Extract ONLY {plural}. Anything of another kind is somebody else's job.
"""


def comprehend_chunk_per_kind(chunk: Chunk,
                              max_tokens: int | None = None) -> ChunkUnderstanding:
    """
    Four specialised passes, one per artifact kind, merged.

    Four times the calls of a single pass and twice the focused variant, so it
    has to earn that on the evaluation. Degrades the same way: a specialist
    that fails costs its own kind and nothing else.
    """
    budget = max_tokens or int(os.environ.get("INGEST_COMPREHEND_TOKENS", "") or 1200)

    merged: dict[str, Any] = {}
    errors: list[str] = []
    participants: list[str] = []

    for kind in ARTIFACT_KINDS:
        plural, field, brief = _KIND_BRIEF[kind]
        prompt = _ONE_KIND_PROMPT.format(plural=plural, field=field, brief=brief)
        payload, err = _one_pass(chunk, prompt, budget)
        if err:
            errors.append(err)
            continue
        merged[plural] = (payload or {}).get(plural, [])
        for name in _clean_strings((payload or {}).get("participants")):
            if name not in participants:
                participants.append(name)

    if not merged:
        return ChunkUnderstanding(chunk_index=chunk.index,
                                  error=errors[0] if errors else "no passes returned",
                                  calls=len(ARTIFACT_KINDS))

    merged["participants"] = participants
    result = build_understanding(merged, chunk)
    result.calls = len(ARTIFACT_KINDS)
    for err in errors:
        result.rejected.append(f"pass failed: {err}")
    return result


# ---------------------------------------------------------------------------
# The typed variant: let the provider ENFORCE the shape
# ---------------------------------------------------------------------------
#
# Borrowed from cocoindex's meeting-notes example, which declares Pydantic
# models and hands them to the model as a response schema rather than
# describing the shape in prose. Their phrasing: "instructor uses these as the
# response schema, so the model returns structured data that matches the Python
# types instead of free-form text."
#
# We do not need instructor or LiteLLM for it -- Groq accepts a strict JSON
# schema directly, verified against openai/gpt-oss-120b.
#
# Worth trying because the failure it removes is one we demonstrably have.
# `_parse_reply` strips markdown fences and hunts for the outermost braces, and
# there are three separate code paths reporting "unparseable reply". json_object
# only promises VALID json; a schema promises the RIGHT json, so a reply can no
# longer arrive well-formed and wrongly shaped. The secondary hope is quality:
# a model not spending attention on remembering the format may have more left
# for reading the meeting.

def _artifact_schema(kinds: tuple[str, ...] = ARTIFACT_KINDS) -> dict[str, Any]:
    """The reply shape, as a strict JSON schema rather than as prose."""
    plural = {"decision": "decisions", "action_item": "action_items",
              "risk": "risks", "open_question": "open_questions"}
    properties: dict[str, Any] = {
        "summary": {"type": "string"},
        "participants": {"type": "array", "items": {"type": "string"}},
    }
    for kind in kinds:
        item_props = {
            _FIELD_BY_KIND[kind]: {"type": "string"},
            "owner": {"type": ["string", "null"]},
            "due": {"type": ["string", "null"]},
            "quote": {"type": "string"},
        }
        properties[plural[kind]] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": item_props,
                # Every field required, nullable where optional: strict schemas
                # forbid a partial object, and a model that omits `owner`
                # entirely is harder to handle than one that sends null.
                "required": list(item_props),
                "additionalProperties": False,
            },
        }
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TYPED_PROMPT = """You extract a factual record from part of a meeting transcript.

The reply shape is enforced for you, so spend no effort on formatting and all
of it on reading the passage.

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
5. A risk is anything named as able to go wrong: a blocker, a dependency, an
   exposure, something flaky, a contract that may be breached.
6. An open question is asked and left hanging here. If someone answers it in
   this passage, it is not open.
7. Use only names that appear in the passage. Never introduce a person.
8. Use null for an owner or due date the passage does not give.
"""


def comprehend_chunk_typed(chunk: Chunk,
                           max_tokens: int | None = None) -> ChunkUnderstanding:
    """
    One pass, with the reply shape enforced by the provider rather than asked
    for in prose.

    The rules are the same as the single pass; only the JSON example is gone,
    because the schema now carries it. So the comparison is schema-enforcement
    against prose-description, not two different briefs.
    """
    from brahmastra.llm import chat

    # Far more headroom than the prose variants need, and not optional.
    # gpt-oss-120b is a REASONING model: its reasoning tokens are spent from
    # the same budget, and a budget exhausted before the content begins yields
    # an EMPTY generation, which a strict schema then rejects with
    # `json_validate_failed` and `failed_generation: ""`. At 1600 tokens every
    # call failed that way and the variant scored 0%; at 4000 it works. Same
    # trap CLAUDE.md records for qwen3.6-27b, arriving through the schema
    # rather than through json_object.
    budget = max_tokens or int(os.environ.get("INGEST_TYPED_TOKENS", "") or 4000)
    try:
        raw = _cached_chat(
            TYPED_PROMPT,
            f"Passage {chunk.index + 1} of the transcript:\n\n{chunk.text}",
            json_schema=_artifact_schema(),
            temperature=0.1,
            max_tokens=budget,
        )
    except Exception as exc:
        return ChunkUnderstanding(
            chunk_index=chunk.index, error=f"{type(exc).__name__}: {exc}"[:300])

    try:
        payload = _parse_reply(raw)
    except Exception as exc:
        return ChunkUnderstanding(
            chunk_index=chunk.index, error=f"unparseable reply: {exc}"[:300])

    return build_understanding(payload, chunk)


def comprehend_chunk_typed_focused(chunk: Chunk,
                                   max_tokens: int | None = None) -> ChunkUnderstanding:
    """
    The two winning ideas together: a schema-enforced reply, twice, split into
    commitments and concerns.

    Schema enforcement bought 15 points at one call (43% -> 58%) and the
    two-way split bought 26 at two calls (43% -> 69%). They address different
    things -- one removes the formatting burden, the other removes divided
    attention -- so the question is whether the gains stack or overlap.
    """
    budget = max_tokens or int(os.environ.get("INGEST_TYPED_TOKENS", "") or 4000)
    halves = ((_COMMITMENT_KINDS, "commitments: decisions taken and tasks people "
               "committed to"),
              (_CONCERN_KINDS, "concerns: risks raised and questions left open"))

    merged: dict[str, Any] = {}
    errors: list[str] = []
    participants: list[str] = []
    plural = {"decision": "decisions", "action_item": "action_items",
              "risk": "risks", "open_question": "open_questions"}

    for kinds, brief in halves:
        prompt = (TYPED_PROMPT.replace(
            "You extract a factual record from part of a meeting transcript.",
            f"You read part of a meeting transcript and extract only {brief}. "
            f"Ignore everything else, however interesting."))
        payload, err = _one_pass(chunk, prompt, budget,
                                 json_schema=_artifact_schema(kinds))
        if err:
            errors.append(err)
            continue
        for kind in kinds:
            merged[plural[kind]] = (payload or {}).get(plural[kind], [])
        for name in _clean_strings((payload or {}).get("participants")):
            if name not in participants:
                participants.append(name)

    if not merged:
        return ChunkUnderstanding(chunk_index=chunk.index,
                                  error=errors[0] if errors else "no passes returned",
                                  calls=2)
    merged["participants"] = participants
    result = build_understanding(merged, chunk)
    result.calls = 2
    for err in errors:
        result.rejected.append(f"pass failed: {err}")
    return result
