"""
Ingestion was a MAP with no REDUCE, and that is a defect rather than a polish.

Chunks overlap by design: a decision is routinely proposed in one turn and
agreed to two turns later, so the tail of each chunk is repeated at the head of
the next. Every turn in that overlap is comprehended TWICE. Without this stage
the same decision is stored twice -- three times in a long meeting -- and the
knowledge base reports three decisions where one was made.

The error grows with document size, which is exactly the direction this module
is meant to scale in.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest.comprehend import Artifact
from brahmastra.ingest.consolidate import (
    consolidate,
    quotes_overlap,
    same_fact,
    similarity,
)


def art(kind="decision", statement="The release moves to April 15th", **kw):
    return Artifact(kind=kind, statement=statement, **kw)


# ---------------------------------------------------------------------------
# The duplication the overlap causes
# ---------------------------------------------------------------------------

def test_the_same_decision_from_two_overlapping_chunks_becomes_one():
    """The regression this module exists for."""
    result = consolidate([
        art(chunk_index=0, quote="Then we're moving the release to April 15th"),
        art(chunk_index=1, quote="Then we're moving the release to April 15th"),
    ])
    assert len(result["artifacts"]) == 1
    assert result["merged"] == 1


def test_a_repeated_fact_records_how_often_it_was_said():
    """Something said three times in a meeting is more load-bearing than
    something said once, and that is worth keeping."""
    result = consolidate([
        art(chunk_index=i, quote="Then we're moving the release to April 15th")
        for i in range(3)
    ])
    assert result["artifacts"][0].mentions == 3


def test_the_survivor_is_the_first_time_it_was_said():
    """Provenance should point at where a thing was decided, not where it was
    repeated."""
    result = consolidate([
        art(chunk_index=3, quote="Then we're moving the release to April 15th"),
        art(chunk_index=1, quote="Then we're moving the release to April 15th"),
    ])
    assert result["artifacts"][0].chunk_index == 1


def test_paraphrases_of_one_decision_are_merged():
    result = consolidate([
        art(statement="The release moves to April 15th", chunk_index=0, quote="q1 aaaa"),
        art(statement="Release date moved to April 15th", chunk_index=1, quote="q2 bbbb"),
    ])
    assert len(result["artifacts"]) == 1


# ---------------------------------------------------------------------------
# What must NOT be merged
# ---------------------------------------------------------------------------

def test_two_different_decisions_stay_two():
    result = consolidate([
        art(statement="Raj owns the reconciliation job", chunk_index=0),
        art(statement="Raj owns the card payment flow", chunk_index=0),
    ])
    assert len(result["artifacts"]) == 2


def test_a_risk_and_a_decision_about_the_same_thing_stay_separate():
    """Merging them would lose one of them entirely."""
    result = consolidate([
        art(kind="decision", statement="Move the release to April", chunk_index=0),
        art(kind="risk", statement="Move the release to April", chunk_index=0),
    ])
    assert len(result["artifacts"]) == 2


def test_two_decisions_in_one_passage_are_not_collapsed_by_quote_alone():
    """
    One passage can hold two distinct decisions, so overlapping quotes cannot
    be sufficient on their own.
    """
    shared = "Okay. Then we're moving the release and Raj takes reconciliation."
    result = consolidate([
        art(statement="Move the release to April 15th", chunk_index=0, quote=shared),
        art(statement="Raj owns the reconciliation job", chunk_index=0, quote=shared),
    ])
    assert len(result["artifacts"]) == 2


# ---------------------------------------------------------------------------
# Refinements
# ---------------------------------------------------------------------------

def test_a_fact_and_its_detail_become_one_complete_fact():
    """
    "Raj takes reconciliation" and "Raj takes reconciliation, by the 27th" are
    one commitment stated twice. Kept apart, they are an action with no date
    beside a date with no action.
    """
    result = consolidate([
        art(kind="action_item", statement="Complete the reconciliation job",
            chunk_index=0, quote="I can take the reconciliation job here"),
        art(kind="action_item", statement="Complete the reconciliation job",
            owner="Raj", due="the 27th", chunk_index=1,
            quote="I can take the reconciliation job here"),
    ])
    assert len(result["artifacts"]) == 1
    kept = result["artifacts"][0]
    assert kept.owner == "Raj"
    assert kept.due == "the 27th"


def test_merging_never_discards_a_richer_field():
    result = consolidate([
        art(chunk_index=0, owner="Sarah", rationale="payments is not ready",
            quote="Then we're moving the release to April 15th"),
        art(chunk_index=1, quote="Then we're moving the release to April 15th"),
    ])
    kept = result["artifacts"][0]
    assert kept.owner == "Sarah"
    assert kept.rationale == "payments is not ready"


def test_speakers_are_unioned_across_mentions():
    result = consolidate([
        art(chunk_index=0, speakers=["Sarah"],
            quote="Then we're moving the release to April 15th"),
        art(chunk_index=1, speakers=["Mei"],
            quote="Then we're moving the release to April 15th"),
    ])
    assert set(result["artifacts"][0].speakers) == {"Sarah", "Mei"}


# ---------------------------------------------------------------------------
# Changing your mind
# ---------------------------------------------------------------------------

def test_a_revised_decision_is_marked_not_deleted():
    """
    "We changed our minds" is itself worth knowing. Silently dropping the
    earlier decision makes the record less true, not tidier.
    """
    result = consolidate([
        art(statement="Ship the release on March 30th", due="March 30th", chunk_index=0),
        art(statement="Ship the release on April 15th", due="April 15th", chunk_index=4),
    ])
    assert len(result["artifacts"]) == 2
    assert result["superseded"] == 1
    earlier = next(a for a in result["artifacts"] if a.chunk_index == 0)
    assert "April 15th" in (earlier.superseded_by or "")
    assert result["notes"]


def test_a_restatement_with_the_same_date_is_a_merge_not_a_revision():
    result = consolidate([
        art(statement="Ship the release on April 15th", due="April 15th", chunk_index=0),
        art(statement="Ship the release on April 15th", due="April 15th", chunk_index=3),
    ])
    assert result["superseded"] == 0
    assert result["merged"] == 1


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_nothing_in_nothing_out():
    result = consolidate([])
    assert result["artifacts"] == []
    assert result["merged"] == 0


def test_it_can_be_switched_off(monkeypatch):
    """So a suspected over-merge can be diagnosed against the raw output."""
    monkeypatch.setenv("INGEST_CONSOLIDATE", "0")
    result = consolidate([
        art(chunk_index=0, quote="Then we're moving the release to April 15th"),
        art(chunk_index=1, quote="Then we're moving the release to April 15th"),
    ])
    assert len(result["artifacts"]) == 2


def test_the_helpers_are_directly_usable():
    assert similarity("move the release", "move the release") == 1.0
    assert similarity("move the release", "") == 0.0
    assert quotes_overlap("we are moving the release to April fifteenth now",
                          "okay we are moving the release to April fifteenth")
    assert not quotes_overlap("short", "short")
    assert same_fact(art(chunk_index=0), art(chunk_index=1))
    assert not same_fact(art(kind="risk"), art(kind="decision"))


def test_a_revision_never_keeps_the_abandoned_value():
    """
    The dangerous version of the bug above. A revision scores ABOVE the merge
    threshold, so a merge-first implementation folded the new decision into the
    old one and kept the March date -- leaving the knowledge base asserting a
    date the meeting had explicitly abandoned.
    """
    result = consolidate([
        art(statement="Ship the release on March 30th", due="March 30th", chunk_index=0),
        art(statement="Ship the release on April 15th", due="April 15th", chunk_index=4),
    ])
    dues = {a.due for a in result["artifacts"]}
    assert "April 15th" in dues, "the revised date was lost"
    survivor = next(a for a in result["artifacts"] if a.due == "March 30th")
    assert survivor.superseded_by, "the abandoned date is not marked as superseded"


def test_conflicting_numbers_are_a_revision_not_a_duplicate():
    result = consolidate([
        art(kind="action_item", statement="Reduce latency to 200 ms",
            due="Friday", chunk_index=0),
        art(kind="action_item", statement="Reduce latency to 120 ms",
            due="Monday", chunk_index=2),
    ])
    assert len(result["artifacts"]) == 2
    assert result["superseded"] == 1


def test_conflict_detection_is_directly_usable():
    from brahmastra.ingest.consolidate import conflicts
    assert conflicts(art(due="March 30th"), art(due="April 15th"))
    assert not conflicts(art(due="April 15th"), art(due="April 15th"))
    assert conflicts(art(statement="ship on March 30th"),
                     art(statement="ship on April 15th"))
    assert not conflicts(art(statement="ship the release"),
                         art(statement="ship the release"))


# ---------------------------------------------------------------------------
# A decision and its reversal, which every similarity measure here is blind to
# ---------------------------------------------------------------------------

def test_a_decision_and_its_reversal_are_never_merged():
    """
    The worst version of the revision bug, and it survived the fix for it.

    `conflicts` caught revisions by comparing DATES and NUMBERS, which works
    when a decision MOVED something. A decision and its negation carry
    identical dates and numbers, so there was nothing to compare: "Move the
    release to April 15th" and "Do not move the release to April 15th" score
    0.90 -- above the 0.82 merge threshold -- and merged into one artifact,
    whichever chunk happened to arrive first deciding what the organisation
    then believed.

    Grounding cannot catch this. Both statements are perfectly quoted from the
    transcript; the meaning is destroyed afterwards, in the reduce step.
    """
    result = consolidate([
        art(statement="Move the release to April 15th", chunk_index=0),
        art(statement="Do not move the release to April 15th", chunk_index=3),
    ])
    assert len(result["artifacts"]) == 2, (
        "a decision was merged with its own reversal"
    )


def test_a_reversal_reads_as_a_change_of_mind():
    """Which is what it is -- and 'we changed our minds' is worth keeping."""
    result = consolidate([
        art(statement="We will migrate the reporting service this quarter",
            chunk_index=0),
        art(statement="We are not migrating the reporting service this quarter",
            chunk_index=5),
    ])
    assert result["superseded"] == 1
    abandoned = next(a for a in result["artifacts"] if a.superseded_by)
    assert "not" not in abandoned.statement, "the wrong one was marked abandoned"


def test_a_negated_risk_still_merges_with_its_paraphrase():
    """
    The guard is scoped to decisions and commitments on purpose.

    In a decision, "not" reverses what will happen. In a risk it is usually
    descriptive: "refunds have not been started" and "refunds remain unstarted"
    are ONE risk seen from two chunks, and splitting them would report the same
    finding as both missed and invented -- exactly the failure the lexical
    scorer used to produce, arriving from the other direction.
    """
    result = consolidate([
        art(kind="risk", statement="Refunds have not been started yet",
            chunk_index=0),
        art(kind="risk", statement="Refunds have not been started",
            chunk_index=1),
    ])
    assert len(result["artifacts"]) == 1


def test_polarity_detection_is_directly_usable():
    from brahmastra.ingest.consolidate import negated, polarity_differs

    assert negated("We are not rewriting the consumer")
    assert negated("Leave the reporting service migration out of Q3")
    assert negated("We won't commit to it this quarter")
    assert not negated("Move the release to April 15th")
    # Bare "no" is deliberately not a cue: this is a positive assertion OF a
    # risk, and treating it as negative would split it from its paraphrase.
    assert not negated("There is no runbook for this service")

    assert polarity_differs("We will migrate", "We will not migrate")
    assert not polarity_differs("We will migrate", "We are migrating")
