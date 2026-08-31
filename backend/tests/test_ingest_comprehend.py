"""
A fabricated decision is a false record, not a bad summary.

This repository has already been burned by exactly this task shape: a 7B model
handed a conversation transcript invented an entire note -- a commit that never
happened, a push that never happened, a reply from someone who never said it --
because "write the next plausible turn" is the likeliest continuation of a
transcript. See docs/CHECKPOINTING_DESIGN.md.

A meeting transcript is the same trap with higher stakes, because an
organisation may ACT on a decision it believes it made. So comprehension fails
closed, and these tests are the proof: an artifact with no grounded quote never
reaches the knowledge base.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest.comprehend import (
    build_understanding,
    comprehend_chunk,
    owner_is_named,
    quote_is_grounded,
)
from brahmastra.ingest.segment import segment

TRANSCRIPT = """\
Sarah: Let's settle the release date. I think March is too tight.
Mei: Agreed. The payments integration isn't done and Priya is out until the 20th.
Sarah: Then we're moving the release to April 15th. I'll own the comms.
Mei: I'll update the roadmap by Friday.
Sarah: One risk is that the Acme contract assumes a March delivery.
Mei: Do we need legal to review the Acme contract before we tell them?
"""


@pytest.fixture
def chunk():
    return segment(TRANSCRIPT, max_tokens=4000)[0]


def _payload(**overrides):
    base = {
        "summary": "The team moved the release to April.",
        "topics": ["release date"],
        "participants": ["Sarah", "Mei"],
        "decisions": [], "action_items": [], "risks": [], "open_questions": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The defence that matters
# ---------------------------------------------------------------------------

def test_a_decision_with_a_real_quote_is_kept(chunk):
    result = build_understanding(_payload(decisions=[{
        "statement": "The release moves to April 15th",
        "rationale": "payments integration is not done",
        "owner": "Sarah",
        "quote": "Then we're moving the release to April 15th",
    }]), chunk)

    assert len(result.artifacts) == 1
    assert result.artifacts[0].kind == "decision"
    assert result.artifacts[0].owner == "Sarah"
    assert not result.rejected


def test_a_fabricated_decision_is_dropped(chunk):
    """
    The exact failure this stage exists to prevent: a plausible, well-formed,
    entirely invented decision. Nobody said anything about a budget.
    """
    result = build_understanding(_payload(decisions=[{
        "statement": "The team approved a 40% budget increase",
        "owner": "Sarah",
        "quote": "We are approving the budget increase of forty percent",
    }]), chunk)

    assert result.artifacts == []
    assert any("quote not found" in r for r in result.rejected)


def test_a_paraphrased_quote_is_dropped(chunk):
    """
    A paraphrase reads like evidence and is not. Once a paraphrase passes,
    the grounding check protects nothing.
    """
    result = build_understanding(_payload(decisions=[{
        "statement": "Release moved to April",
        "quote": "The team decided that the release would be moved to April",
    }]), chunk)
    assert result.artifacts == []


def test_a_trivially_short_quote_is_not_evidence(chunk):
    """"Agreed." occurs in every meeting and proves nothing."""
    result = build_understanding(_payload(decisions=[{
        "statement": "Everyone agreed to the plan", "quote": "Agreed.",
    }]), chunk)
    assert result.artifacts == []


def test_reformatted_whitespace_still_counts_as_grounded(chunk):
    """Models normalise whitespace. That is reformatting, not fabrication."""
    result = build_understanding(_payload(decisions=[{
        "statement": "Release moves to April 15th",
        "quote": "Then   we're  moving\n the release to April 15th",
    }]), chunk)
    assert len(result.artifacts) == 1


def test_every_kind_is_grounded_not_just_decisions(chunk):
    result = build_understanding(_payload(
        action_items=[{"task": "Invent a task", "quote": "I will do the invented thing"}],
        risks=[{"description": "Invented risk", "quote": "There is an invented risk here"}],
        open_questions=[{"question": "Invented?", "quote": "Is this an invented question"}],
    ), chunk)
    assert result.artifacts == []
    assert len(result.rejected) == 3


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------

def test_an_invented_owner_is_stripped_but_the_artifact_survives(chunk):
    """
    An action assigned to a person who was never there looks actionable and is
    addressed to nobody. The commitment is real, though, so keep it unassigned
    rather than discarding what was genuinely said.
    """
    result = build_understanding(_payload(action_items=[{
        "task": "Update the roadmap",
        "owner": "Jonathan",
        "quote": "I'll update the roadmap by Friday",
    }]), chunk)

    assert len(result.artifacts) == 1
    assert result.artifacts[0].owner is None
    assert any("not named" in r for r in result.rejected)


def test_a_real_owner_and_due_date_are_kept(chunk):
    result = build_understanding(_payload(action_items=[{
        "task": "Update the roadmap", "owner": "Mei", "due": "Friday",
        "quote": "I'll update the roadmap by Friday",
    }]), chunk)
    assert result.artifacts[0].owner == "Mei"
    assert result.artifacts[0].due == "Friday"


def test_no_owner_is_allowed(chunk):
    assert owner_is_named(None, TRANSCRIPT, []) is True


# ---------------------------------------------------------------------------
# Shape and provenance
# ---------------------------------------------------------------------------

def test_an_empty_statement_is_dropped(chunk):
    result = build_understanding(_payload(decisions=[{
        "statement": "  ", "quote": "Then we're moving the release to April 15th",
    }]), chunk)
    assert result.artifacts == []


def test_artifacts_carry_where_they_came_from(chunk):
    result = build_understanding(_payload(decisions=[{
        "statement": "Release moves to April 15th",
        "quote": "Then we're moving the release to April 15th",
    }]), chunk)
    art = result.artifacts[0]
    assert art.chunk_index == chunk.index
    assert "Sarah" in art.speakers


def test_empty_arrays_are_a_valid_answer(chunk):
    """Small talk must produce nothing. A model that always finds a decision is
    a model that invents them."""
    result = build_understanding(_payload(summary="Scheduling chatter."), chunk)
    assert result.artifacts == []
    assert result.rejected == []
    assert result.summary == "Scheduling chatter."


def test_a_malformed_reply_does_not_crash(chunk):
    assert build_understanding({}, chunk).artifacts == []
    assert build_understanding({"decisions": "not a list"}, chunk).artifacts == []
    assert build_understanding({"decisions": ["a string"]}, chunk).artifacts == []


# ---------------------------------------------------------------------------
# One bad chunk must not cost the other forty
# ---------------------------------------------------------------------------

def test_a_provider_failure_is_reported_not_raised(chunk, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("429 rate limit")

    monkeypatch.setattr("brahmastra.llm.chat", boom)
    result = comprehend_chunk(chunk)

    assert result.error is not None
    assert "429" in result.error
    assert result.artifacts == []


def test_an_unparseable_reply_is_reported_not_raised(chunk, monkeypatch):
    monkeypatch.setattr("brahmastra.llm.chat", lambda *a, **k: "I'm afraid I can't do that")
    result = comprehend_chunk(chunk)
    assert result.error is not None
    assert "unparseable" in result.error


def test_a_fenced_json_reply_is_read(chunk, monkeypatch):
    monkeypatch.setattr(
        "brahmastra.llm.chat",
        lambda *a, **k: '```json\n{"summary": "ok", "decisions": []}\n```',
    )
    assert comprehend_chunk(chunk).summary == "ok"


def test_grounding_helper_is_directly_usable():
    assert quote_is_grounded("Then we're moving the release to April 15th", TRANSCRIPT)
    assert not quote_is_grounded("We approved a forty percent budget rise", TRANSCRIPT)
    assert not quote_is_grounded(None, TRANSCRIPT)
    assert not quote_is_grounded("", TRANSCRIPT)
