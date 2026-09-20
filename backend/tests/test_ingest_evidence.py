"""
Is the quote attached to this artifact the RIGHT quote?

Two checks, and each exists because something cheaper was measured and failed.

Cosine scored the fabricated pairing and the correct one at 0.40 each, so no
threshold separates them. Strict entailment (DeBERTa on MNLI/FEVER/ANLI)
returned 0.00-0.02 for EVERY pair including the true ones, because a claim that
says "April 27th" where the quote says "the 27th" is not logically entailed. An
LLM asked in prose made the same mistake from the other direction.

Ranking works because it asks a relative question, and attribution is answered
with no model at all. These tests use a stub scorer so the suite stays offline
and fast -- the real reranker is exercised by the evaluation harness, not here.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest import evidence
from brahmastra.ingest.segment import segment

PASSAGE = (
    "Sarah: Right, let's get started. Agenda is the Q3 release date.\n"
    "Sarah: Okay. Then we're moving the release to April 15th. I'll own "
    "communicating that to the wider team today.\n"
    "Mei: Agreed. I'll update the roadmap by Friday so nobody's working off "
    "the March date.\n"
    "Raj: I can take the reconciliation job, so realistically that's the 27th.\n"
    "Sarah: That works with April 15th. Raj, you own reconciliation.\n"
)


@pytest.fixture
def chunk():
    return segment(PASSAGE)[0]


def _stub(scores_by_line):
    """A scorer with known scores, so the RULE is tested and not the model."""
    def scorer(statement, quote, chunk):
        lines = evidence._candidates(chunk)
        ranked = sorted(((scores_by_line.get(_key(ln), -20.0), ln) for ln in lines),
                        reverse=True)
        needle = evidence._normalise(quote)
        for position, (score, line) in enumerate(ranked, start=1):
            hay = evidence._normalise(line)
            if needle in hay or hay in needle:
                return {"score": score, "best": ranked[0][0], "rank": position,
                        "best_line": ranked[0][1]}
        return None
    return scorer


def _key(line: str) -> str:
    return evidence._normalise(line)[:30]


# ---------------------------------------------------------------------------
# Evidence ranking
# ---------------------------------------------------------------------------

def test_a_quote_that_supports_nothing_is_refused(monkeypatch, chunk):
    """
    The live failure: the action item "Move the Q3 release date to April 15th"
    citing "I'll update the roadmap by Friday". Real quote, present in the
    passage, about something else -- so it passed every earlier check and
    landed looking sourced.
    """
    roadmap = _key("I'll update the roadmap by Friday so nobody's")
    release = _key("Okay. Then we're moving the release to April 15th.")
    monkeypatch.setattr(evidence, "score_evidence",
                        _stub({roadmap: -10.25, release: 6.09}))

    assert not evidence.evidence_supports(
        "Move the Q3 release date to April 15th.",
        "I'll update the roadmap by Friday", chunk)


def test_the_best_evidence_in_the_passage_is_accepted(monkeypatch, chunk):
    release = _key("Okay. Then we're moving the release to April 15th.")
    monkeypatch.setattr(evidence, "score_evidence", _stub({release: 4.86}))

    assert evidence.evidence_supports(
        "The release date moved to April 15th.",
        "Then we're moving the release to April 15th", chunk)


def test_a_close_second_is_still_accepted(monkeypatch, chunk):
    """
    A passage often states the same commitment twice. Measured: the correct
    reconciliation artifact ranked SECOND, 0.48 behind the best line, and
    refusing it would have cost a true finding for nothing.
    """
    raj = _key("I can take the reconciliation job, so realistically")
    sarah = _key("That works with April 15th. Raj, you own")
    monkeypatch.setattr(evidence, "score_evidence",
                        _stub({raj: 4.07, sarah: 4.55}))

    assert evidence.evidence_supports(
        "Take the reconciliation job and complete it by April 27th.",
        "I can take the reconciliation job", chunk)


def test_it_fails_open_when_the_model_is_unavailable(monkeypatch, chunk):
    """
    Defence in depth, not a replacement. The deterministic checks in
    comprehend.py stay the floor, so a missing model costs a layer rather than
    the run -- and the suite must never download a model to pass.
    """
    monkeypatch.setattr(evidence, "score_evidence", lambda *a, **k: None)
    assert evidence.evidence_supports("anything at all", "a quote", chunk)


def test_no_quote_is_not_this_checks_problem(chunk):
    assert evidence.evidence_supports("a statement", None, chunk)


# ---------------------------------------------------------------------------
# Attribution, which ranking is structurally blind to
# ---------------------------------------------------------------------------

def test_a_first_person_quote_belongs_to_whoever_spoke_it(chunk):
    """
    "Mei communicates the new release date" cited to Sarah's "I'll own
    communicating that" ranks FIRST by relevance -- the quote really is about
    communicating the release date. Only the person is wrong, and no amount of
    relevance scoring can see that.
    """
    assert not evidence.attribution_is_consistent(
        "Mei", "I'll own communicating that to the wider team today", chunk)


def test_the_speaker_owning_their_own_commitment_is_fine(chunk):
    assert evidence.attribution_is_consistent(
        "Sarah", "I'll own communicating that to the wider team today", chunk)
    assert evidence.attribution_is_consistent(
        "Mei", "I'll update the roadmap by Friday", chunk)


def test_being_assigned_by_someone_else_must_not_fire(chunk):
    """
    The case that would make this rule useless if it fired. "Raj, you own
    reconciliation" is Sarah speaking and Raj owning, which is how work is
    handed out in every meeting. Only FIRST PERSON constrains the owner.
    """
    assert evidence.attribution_is_consistent(
        "Raj", "Raj, you own reconciliation", chunk)


def test_a_quote_that_cannot_be_located_is_not_judged(chunk):
    assert evidence.attribution_is_consistent(
        "Mei", "I'll do something said in a different meeting entirely", chunk)


def test_no_owner_means_nothing_to_check(chunk):
    assert evidence.attribution_is_consistent(
        None, "I'll own communicating that", chunk)


# ---------------------------------------------------------------------------
# Wired into acceptance
# ---------------------------------------------------------------------------

def test_a_misattributed_owner_loses_the_owner_not_the_finding(monkeypatch, chunk):
    """
    The statement is true of the meeting; only the attribution is wrong. So the
    owner is dropped and the finding survives -- dropping the artifact would
    lose a real commitment over a fixable field.
    """
    from brahmastra.ingest.comprehend import build_understanding

    monkeypatch.setattr(evidence, "score_evidence", lambda *a, **k: None)
    result = build_understanding({"action_items": [{
        "task": "Communicate the new release date to the wider team",
        "owner": "Mei",
        "quote": "I'll own communicating that to the wider team today",
    }]}, chunk)

    assert len(result.artifacts) == 1
    assert result.artifacts[0].owner is None
    assert any("did not speak" in r for r in result.rejected)
