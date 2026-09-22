"""
A judge that cannot be reached must not silently return a verdict.

This is the lesson that cost a whole measurement. An earlier version of
`entity_confirm` swallowed every exception and returned "not confirmed", so
when Groq's DAILY quota ran out mid-probe an entire batch came back refused
and the numbers read as a model being cautious. The first eight pairs of a
sixteen-pair probe looked rejected; they had simply never been asked.

A CACHE that cannot be reached degrades to paying again, which is why
memo.load swallows. A JUDGE is the opposite: absence of an answer is not an
answer, and the caller has to be able to tell.
"""
from __future__ import annotations

import pytest

from brahmastra import entity_confirm as ec

PAIRS = [("SQLite", "SQLite database"), ("memo.py", "src/memo.py")]


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    monkeypatch.setenv("ENTITY_CONFIRM", "1")
    monkeypatch.setattr(ec, "available", lambda: True)
    from brahmastra import memo
    memo.reset()
    yield
    memo.reset()


def _reply(monkeypatch, text: str):
    import brahmastra.llm as llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: text)


# -- the distinction that matters -------------------------------------------

def test_a_verdict_of_no_is_not_the_same_as_no_verdict(monkeypatch):
    _reply(monkeypatch, '{"verdicts":[{"pair":1,"same":true},'
                        '{"pair":2,"same":false}]}')

    verdicts, unanswered = ec.confirm(PAIRS)
    assert unanswered == []
    assert verdicts[PAIRS[0]] is True
    assert verdicts[PAIRS[1]] is False


def test_an_unreachable_model_answers_for_nobody(monkeypatch):
    """
    The failure that invalidated a measurement. A quota outage must come back
    as "I could not ask", never as "the answer is no".
    """
    import brahmastra.llm as llm

    def dead(*args, **kwargs):
        raise llm.LLMQuotaExhausted("Groq daily quota exhausted")

    monkeypatch.setattr(llm, "chat", dead)

    verdicts, unanswered = ec.confirm(PAIRS)
    assert verdicts == {}
    assert unanswered == PAIRS


def test_an_unreadable_reply_is_unanswered_not_refused(monkeypatch):
    _reply(monkeypatch, "I think the first one is probably the same?")

    verdicts, unanswered = ec.confirm(PAIRS)
    assert verdicts == {}
    assert unanswered == PAIRS


def test_a_pair_the_model_skipped_is_unanswered(monkeypatch):
    """Asked, and not answered. Still not a verdict."""
    _reply(monkeypatch, '{"verdicts":[{"pair":1,"same":true}]}')

    verdicts, unanswered = ec.confirm(PAIRS)
    assert verdicts == {PAIRS[0]: True}
    assert unanswered == [PAIRS[1]]


def test_it_is_off_unless_asked_for(monkeypatch):
    """
    Off by DEFAULT, and "off" means nothing was asked -- not "everything
    refused". Four runs over 21 labelled pairs said it reliably prevents two
    wrong merges and reliably costs one or two right ones, and that the ones
    it costs change between runs at temperature 0. A resolver whose clusters
    differ run to run makes the graph churn for no reason.
    """
    monkeypatch.delenv("ENTITY_CONFIRM", raising=False)
    assert ec.enabled() is False
    verdicts, unanswered = ec.confirm(PAIRS)
    assert verdicts == {}
    assert unanswered == PAIRS


# -- reading the reply ------------------------------------------------------

def test_a_verdict_about_a_pair_nobody_asked_about_is_ignored(monkeypatch):
    """A model that invents a pair 9 in a batch of 2 must not decide anything."""
    _reply(monkeypatch, '{"verdicts":[{"pair":9,"same":true},'
                        '{"pair":1,"same":true}]}')

    verdicts, unanswered = ec.confirm(PAIRS)
    assert verdicts == {PAIRS[0]: True}
    assert unanswered == [PAIRS[1]]


def test_the_question_carries_both_names_verbatim():
    rendered = ec._render([("Neo4j Aura", "Neo4j Aura Free")])
    assert "Neo4j Aura" in rendered and "Neo4j Aura Free" in rendered
    assert rendered.startswith("1.")


def test_nothing_is_asked_when_there_is_nothing_to_ask(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a call was made for an empty pair list")

    import brahmastra.llm as llm
    monkeypatch.setattr(llm, "chat", explode)
    assert ec.confirm([]) == ({}, [])
