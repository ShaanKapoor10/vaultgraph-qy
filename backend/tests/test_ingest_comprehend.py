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


# ---------------------------------------------------------------------------
# The anchor bug: rejecting real quotes rather than catching invented ones
# ---------------------------------------------------------------------------

def test_a_quote_with_a_word_prepended_is_still_grounded(chunk):
    """
    Found against a live 7B model. The source says "One thing that worries me.
    The Acme contract assumes a March delivery" and the model quoted "That's
    one thing that worries me. The Acme contract assumes a March delivery" --
    verbatim apart from a two-word lead-in. Prefix anchoring failed it, so a
    real finding was thrown away as a fabrication.
    """
    source = ("Sarah: One thing that worries me. The Acme contract assumes a "
              "March delivery and we may be in breach.")
    assert quote_is_grounded(
        "That's one thing that worries me. The Acme contract assumes a March delivery",
        source,
    )


def test_a_quote_with_a_trailing_addition_is_still_grounded():
    source = "Mei: I'll update the roadmap by Friday so nobody works off the March date."
    assert quote_is_grounded(
        "I'll update the roadmap by Friday so nobody works off the March date, she said",
        source,
    )


def test_a_fabrication_containing_one_real_phrase_is_still_refused():
    """
    The reason the shared run must cover a FRACTION of the quote and not merely
    clear a character count. Sharing a phrase with the passage is not the same
    as having been drawn from it, and without the coverage rule this is exactly
    how an invented decision would smuggle itself in.
    """
    source = ("Sarah: One thing that worries me. The Acme contract assumes a "
              "March delivery and we may be in breach.")
    assert not quote_is_grounded(
        "The Acme contract assumes a March delivery, so the board approved a "
        "forty percent budget increase and authorised immediate hiring across "
        "every team for the remainder of the financial year",
        source,
    )


def test_a_wholly_invented_quote_is_still_refused(chunk):
    """The defence the loosening must not weaken."""
    assert not quote_is_grounded(
        "We are approving the budget increase of forty percent", chunk.text
    )
    assert not quote_is_grounded(
        "Everyone agreed that the new vendor should be onboarded next quarter",
        chunk.text,
    )


def test_the_short_quote_rule_still_holds():
    source = "Sarah: How bad is that? Mei: I genuinely don't know."
    assert not quote_is_grounded("How bad is that?", source)


# ---------------------------------------------------------------------------
# The focused variant
# ---------------------------------------------------------------------------

def test_the_focused_variant_merges_both_passes(chunk, monkeypatch):
    import json as _json
    from brahmastra.ingest.comprehend import comprehend_chunk_focused

    def fake(system, user, **kw):
        if "open_questions" not in system:
            return _json.dumps({
                "summary": "The team moved the release.",
                "participants": ["Sarah", "Mei"],
                "decisions": [{"statement": "Move the release to April 15th",
                               "quote": "Then we're moving the release to April 15th"}],
                "action_items": [],
            })
        return _json.dumps({
            "risks": [{"description": "Acme assumes a March delivery",
                       "quote": "One risk is that the Acme contract assumes a March delivery"}],
            "open_questions": [],
        })

    monkeypatch.setattr("brahmastra.llm.chat", fake)
    result = comprehend_chunk_focused(chunk)

    kinds = sorted(a.kind for a in result.artifacts)
    assert kinds == ["decision", "risk"]
    assert result.summary == "The team moved the release."


def test_one_failed_pass_still_returns_what_the_other_found(chunk, monkeypatch):
    """
    Half a record is worth more than none, and the transcript can be re-run.
    Twice the calls means twice the chances to hit a rate limit, so this path
    is ordinary rather than exceptional.
    """
    import json as _json
    from brahmastra.ingest.comprehend import comprehend_chunk_focused

    def flaky(system, user, **kw):
        if "open_questions" not in system:
            return _json.dumps({
                "summary": "ok", "participants": ["Sarah"],
                "decisions": [{"statement": "Move the release to April 15th",
                               "quote": "Then we're moving the release to April 15th"}],
                "action_items": [],
            })
        raise RuntimeError("429 rate limit")

    monkeypatch.setattr("brahmastra.llm.chat", flaky)
    result = comprehend_chunk_focused(chunk)

    assert result.error is None
    assert len(result.artifacts) == 1
    assert any("pass failed" in r for r in result.rejected)


def test_both_passes_failing_is_an_error(chunk, monkeypatch):
    from brahmastra.ingest.comprehend import comprehend_chunk_focused

    def dead(*a, **k):
        raise RuntimeError("no provider")

    monkeypatch.setattr("brahmastra.llm.chat", dead)
    assert comprehend_chunk_focused(chunk).error is not None


def test_the_focused_variant_grounds_quotes_like_the_single_pass(chunk, monkeypatch):
    """The extra recall must not come at the cost of the fabrication defence."""
    import json as _json
    from brahmastra.ingest.comprehend import comprehend_chunk_focused

    def inventive(system, user, **kw):
        if "open_questions" not in system:
            return _json.dumps({
                "summary": "s", "participants": [],
                "decisions": [{"statement": "Approve a 40% budget increase",
                               "quote": "We hereby approve the forty percent budget increase"}],
                "action_items": [],
            })
        return _json.dumps({"risks": [], "open_questions": []})

    monkeypatch.setattr("brahmastra.llm.chat", inventive)
    result = comprehend_chunk_focused(chunk)

    assert result.artifacts == []
    assert any("quote not found" in r for r in result.rejected)
