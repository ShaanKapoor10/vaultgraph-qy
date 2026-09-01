"""
Reconcile what the chunks said, before any of it is stored.

Ingestion was a MAP with no REDUCE, and that is a real defect rather than a
refinement. Chunks overlap by design -- a decision is routinely proposed in one
turn and agreed to two turns later, so the tail of each chunk is repeated at the
head of the next. Every turn in that overlap is therefore comprehended TWICE,
and without this stage the same decision is stored twice, three times in a long
meeting, and the knowledge base reports three decisions where one was made.

That error compounds with document size, which is exactly the direction this
module exists to go.

WHAT IT RECONCILES

  DUPLICATES    the same fact seen from two overlapping chunks. Merged, keeping
                the earliest occurrence and the richest fields, and counting
                the mentions -- because something said three times in a meeting
                is genuinely more load-bearing than something said once.

  REFINEMENTS   "Raj takes reconciliation" and "Raj targets the 27th" are one
                commitment stated twice, and storing them separately produces
                an action item with no date beside a date with no action.

  SUPERSESSION  a decision revisited later in the same meeting. The later one
                wins, and the earlier is kept as history rather than deleted --
                "we changed our minds" is itself worth knowing, and silently
                dropping it makes the record less true, not tidier.

DETERMINISTIC ON PURPOSE

No LLM call. Not to save money -- though on a rate-limited tier that matters --
but because this stage decides what SURVIVES, and a stage that decides what
survives should be one you can test exhaustively and reason about when it is
wrong. The evidence it uses is already grounded: two artifacts drawn from the
same passage usually cite overlapping quotes, and a quote is verbatim source
text rather than model output.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any, Iterable

# How alike two statements must be to be treated as the same fact. Tuned to sit
# above genuine paraphrase of one fact and below two different facts about the
# same subject: "move the release to April 15th" and "move the release to April"
# are one decision; "Raj owns reconciliation" and "Raj owns the card flow" are
# two, and they score well below this.
SIMILARITY_THRESHOLD = 0.82

# Quotes are verbatim source text, so a shared run of this length is strong
# evidence that two artifacts were drawn from the same moment.
QUOTE_OVERLAP_CHARS = 30

_WORD = re.compile(r"[a-z0-9]+")


def _normalise(text: str | None) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def similarity(a: str | None, b: str | None) -> float:
    """0..1 on normalised text. Cheap, deterministic, no model."""
    left, right = _normalise(a), _normalise(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def quotes_overlap(a: str | None, b: str | None) -> bool:
    """
    Do two quotes come from the same passage?

    The strongest available signal, because a quote is source text rather than
    model output: if two artifacts cite a shared run of the transcript, they
    are describing the same moment even when the model phrased the statements
    quite differently.
    """
    left, right = _normalise(a), _normalise(b)
    if not left or not right:
        return False
    if len(left) < QUOTE_OVERLAP_CHARS or len(right) < QUOTE_OVERLAP_CHARS:
        return False
    match = SequenceMatcher(None, left, right).find_longest_match(
        0, len(left), 0, len(right)
    )
    return match.size >= QUOTE_OVERLAP_CHARS


def same_fact(a: Any, b: Any, threshold: float | None = None) -> bool:
    """
    Are these two artifacts the same thing said twice?

    Kind must match first: a risk and a decision about the same subject are two
    different facts, and merging them would lose one of them entirely.
    """
    if a.kind != b.kind:
        return False
    limit = SIMILARITY_THRESHOLD if threshold is None else threshold
    if similarity(a.statement, b.statement) >= limit:
        return True
    # Same moment in the transcript AND recognisably about the same thing. The
    # second half is required: one passage can contain two distinct decisions,
    # and quote overlap alone would collapse them.
    return quotes_overlap(a.quote, b.quote) and similarity(a.statement, b.statement) >= 0.55


def _merge(primary: Any, extra: Any) -> Any:
    """
    Fold `extra` into `primary`, preferring whichever is more informative.

    "Raj takes reconciliation" and "Raj targets the 27th" are one commitment
    stated twice; keeping them apart yields an action with no date beside a
    date with no action.
    """
    if not primary.owner and extra.owner:
        primary.owner = extra.owner
    if not primary.due and extra.due:
        primary.due = extra.due
    if not primary.rationale and extra.rationale:
        primary.rationale = extra.rationale
    # The longer statement usually carries the detail; the earliest chunk index
    # is kept regardless, so provenance still points at where it was first said.
    if len(extra.statement) > len(primary.statement) * 1.2:
        primary.statement = extra.statement
    for speaker in extra.speakers:
        if speaker not in primary.speakers:
            primary.speakers.append(speaker)
    return primary


# Whether a statement asserts something or its opposite.
#
# Every similarity measure in this module is blind to this, and so is the
# cosine one in evaluate.py -- embeddings place a sentence and its negation
# almost on top of each other, because they share every content word. That is a
# well-known property and normally a small annoyance; here it is the difference
# between a true record and a false one, since "we are migrating in Q3" and "we
# are not migrating in Q3" are what the meeting was FOR.
#
# THE SECOND TIME THIS SHAPE OF BUG HAS BEEN PAID FOR HERE. Entity resolution
# hit it first: "Brahmastra backend" and "Brahmastra frontend" embed at 0.94 and
# were being merged into one entity, fixed by `_is_contrasting` in
# entity_resolution.py, which refuses a merge when two names differ only by an
# antonym token. Same failure, different layer -- that guard compares NAMES for
# opposed words, this one compares STATEMENTS for opposed polarity, and a
# system that stores what people decided needs both. Anywhere else embedding
# similarity is used to decide that two things are THE SAME, assume it cannot
# see the difference between a thing and its opposite, and check.
#
# Deliberately narrow. A cue that fires too eagerly costs a duplicate artifact;
# one that misses costs a reversed decision, so the list covers explicit
# negation and explicit shelving, and stops there. Bare "no" is left out on
# purpose: "there is no runbook" is a positive assertion OF a risk, and
# treating it as negative would split every such risk from its own paraphrase.
_NEGATION = re.compile(
    r"\b(?:not|never|cannot|no longer|declin\w+|reject\w+|refus\w+|"
    r"defer\w+|postpon\w+|dropp?\w*|exclud\w+|abandon\w+|"
    r"(?:ca|wo|do|does|did|is|are|was|were|should|would|could|have|has|had)n[’']?t)\b"
    r"|\bout of (?:scope|q[1-4]|the (?:quarter|release|plan))\b"
    r"|\b(?:leav\w+|left|keep\w*|kept)\b[^.,;]{0,40}\bout\b",
    re.IGNORECASE,
)


# Where negation flips a COMMITMENT, and is therefore load-bearing.
#
# Not applied to risks and open questions, and that boundary is the whole
# subtlety. In a decision, "not" reverses what will happen: "we are migrating"
# and "we are not migrating" are opposite records. In a risk it is usually
# descriptive -- "refunds have not been started" and "refunds remain unstarted"
# are ONE risk, and guarding those would split a finding from its own
# paraphrase and report the model as having both missed and invented it. That
# is exactly the failure the lexical scorer used to produce, so it is worth not
# reintroducing from the other direction.
POLARITY_SENSITIVE = ("decision", "action_item")


def negated(text: str | None) -> bool:
    """Does this statement assert that something is NOT happening?"""
    return bool(_NEGATION.search(text or ""))


def polarity_differs(a: str | None, b: str | None) -> bool:
    """
    True when one statement asserts what the other denies.

    Used by `conflicts` to stop a reversal being merged into the thing it
    reversed, and by the evaluation scorer, where a trap and the real decision
    it inverts scored 0.72 by cosine -- so correctly recording "leave the
    migration out of Q3" was counted as fabricating "migrate this quarter".
    """
    return negated(a) != negated(b)


# Months and bare numbers -- the things a revision actually changes. A decision
# revisited in a meeting almost always moves a date or a quantity.
_MONTHS = (
    "january february march april may june july august september october "
    "november december"
).split()
_NUMERIC = re.compile(r"\b(\d{1,4})(?:st|nd|rd|th)?\b")


def _key_facts(text: str | None) -> set[str]:
    """The dates and numbers a statement commits to."""
    words = _normalise(text).split()
    facts = {w for w in words if w in _MONTHS}
    facts |= set(_NUMERIC.findall(_normalise(text)))
    return facts


def conflicts(a: Any, b: Any) -> bool:
    """
    Do these two say incompatible things about the same subject?

    This has to be checked BEFORE merging, and getting that order wrong is
    dangerous rather than untidy. "Ship on March 30th" and "Ship on April 15th"
    score above the merge threshold, so a merge-first implementation folded the
    revision into the superseded decision AND KEPT THE MARCH DATE -- leaving the
    knowledge base asserting a date the meeting had explicitly abandoned.

    Polarity is checked for the same reason, and it closes a strictly worse
    version of that hole. Dates and numbers only catch a revision that MOVED
    something; a decision and its reversal carry identical dates and numbers,
    so "Move the release to April 15th" and "Do NOT move the release to April
    15th" scored 0.90 here -- above the merge threshold, with nothing to
    separate them. They merged, and whichever chunk arrived first decided what
    the organisation believed. That is the exact false-record failure the
    grounding checks exist to prevent, arriving after grounding has passed:
    both statements are perfectly quoted, and the meaning is destroyed in the
    reduce step instead.

    It falls out that a reversal now reads as SUPERSESSION rather than a merge,
    which is what it is -- `_is_supersession` asks this same question.
    """
    if a.due and b.due and _normalise(a.due) != _normalise(b.due):
        return True
    if (a.kind in POLARITY_SENSITIVE
            and polarity_differs(a.statement, b.statement)):
        return True
    left, right = _key_facts(a.statement), _key_facts(b.statement)
    return bool(left and right and left != right)


def _is_supersession(earlier: Any, later: Any) -> bool:
    """
    Did the meeting revisit this and land somewhere else?

    Recognisably the same subject, stating something incompatible. Only
    decisions and commitments can be superseded: a risk raised twice is the
    same risk, and a question asked twice is still open.
    """
    if earlier.kind != later.kind or earlier.kind not in ("decision", "action_item"):
        return False
    if similarity(earlier.statement, later.statement) < 0.55:
        return False
    return conflicts(earlier, later)


def consolidate(artifacts: Iterable[Any],
                threshold: float | None = None) -> dict[str, Any]:
    """
    Reduce a document's artifacts to what was actually true by the end.

    Returns the survivors plus a report of what was folded together, so a
    caller can show "3 mentions" and so a surprising result can be explained
    rather than merely observed.
    """
    if os.environ.get("INGEST_CONSOLIDATE", "1").strip().lower() in {"0", "false", "no"}:
        items = list(artifacts)
        return {"artifacts": items, "merged": 0, "superseded": 0, "notes": []}

    # Earliest first, so the survivor is the first time a thing was said and
    # later refinements fold into it.
    ordered = sorted(artifacts, key=lambda a: (a.chunk_index, a.kind))

    kept: list[Any] = []
    merged = 0
    superseded = 0
    notes: list[str] = []

    for item in ordered:
        # Supersession is tested FIRST, and the order is load-bearing. A
        # revision scores above the merge threshold -- "ship on March 30th" and
        # "ship on April 15th" are nearly the same sentence -- so merging first
        # folds the new decision into the old one and keeps the ABANDONED date.
        revised = next((k for k in kept if _is_supersession(k, item)), None)
        if revised is None:
            match = next((k for k in kept if same_fact(k, item, threshold)
                          and not conflicts(k, item)), None)
            if match is not None:
                _merge(match, item)
                match.mentions = getattr(match, "mentions", 1) + 1
                merged += 1
                continue

        if revised is not None:
            # Kept as history rather than deleted: "we changed our minds" is
            # itself worth knowing, and dropping it makes the record less true.
            revised.superseded_by = item.statement
            superseded += 1
            notes.append(
                f"{item.kind}: {revised.statement[:60]!r} was revised to "
                f"{item.statement[:60]!r}"
            )

        item.mentions = getattr(item, "mentions", 1)
        kept.append(item)

    return {"artifacts": kept, "merged": merged, "superseded": superseded, "notes": notes}
