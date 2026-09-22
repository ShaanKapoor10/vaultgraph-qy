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

# Same KIND on purpose. `confirm` groups a batch by inferred entity type so it
# can carry guidance that fits, so two pairs of different kinds are two calls --
# see test_pairs_of_different_kinds_are_asked_separately.
PAIRS = [("SQLite", "SQLite database"), ("Postgres", "the Postgres store")]


@pytest.fixture(autouse=True)
def reachable(monkeypatch):
    """
    A model that is reachable, and one that is NOT the real one.

    The stub is not politeness -- it caught a genuine leak here. `available()`
    is patched to True, so a test that never replaced `llm.chat` went on to
    call it for real: no key, a blanked Ollama host, and a network round trip
    per attempt. The tests still passed, because a failed call becomes
    `unanswered` and that was what they asserted. Adding a retry budget
    tripled the cost of the leak before anything pointed at it -- 18 tests
    taking 29 seconds, all of it spent failing to reach a provider.

    So the default is a call that FAILS THE TEST. Anything wanting a reply says
    so, and anything reaching a provider by accident says so too.
    """
    import brahmastra.llm as llm

    monkeypatch.setenv("ENTITY_CONFIRM", "1")
    monkeypatch.setattr(ec, "available", lambda: True)

    def unstubbed(*args, **kwargs):
        raise AssertionError(
            "this test reached llm.chat without stubbing it -- it would have "
            "made a real call. Patch brahmastra.llm.chat.")

    monkeypatch.setattr(llm, "chat", unstubbed)

    # And the provider probe, which is the other thing on this path that
    # touches the network. conftest points OLLAMA_HOST at a closed port, and on
    # Windows a refused connection to one costs about two seconds rather than
    # being instant -- so resolving the model was 2.0s per call, swallowed by
    # the try/except around it. Named here so the number never comes back
    # looking like the code under test.
    monkeypatch.setattr(ec, "_active_model", lambda: "test-model")

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


# -- typed guidance ----------------------------------------------------------
#
# cocoindex's LlmPairResolver takes an `entity_type` hint and weaves it into the
# prompt -- its docs give "be more conservative with personal names" as the
# example. It takes ONE type per resolver because a caller there resolves one
# column of one table. Here candidates arrive mixed, so the type is inferred per
# pair and batches are grouped by it.
#
# UNMEASURED against the live corpus, and deliberately so: the numbers that
# turned this judge off were taken with one generic question, and whether typed
# guidance moves them needs quota and a labelled set.

def test_the_kind_of_thing_is_inferred_from_the_names():
    assert ec.entity_type("a/b.py", "a/c.py") == "path"
    assert ec.entity_type("Shaan Kapoor", "Sarah Chen") == "person"
    assert ec.entity_type("threshold 0.55", "threshold 0.60") == "versioned"
    assert ec.entity_type("run_pipeline", "run_full_pipeline") == "identifier"
    assert ec.entity_type("GraphRAG", "Microsoft GraphRAG") == "general"


def test_a_mixed_pair_is_not_claimed_as_either():
    """Both sides must look like the kind, or the guidance would be wrong for
    one of them. Abstaining to 'general' costs a sentence, not a node."""
    assert ec.entity_type("Shaan Kapoor", "shaan_kapoor.py") == "general"


def test_the_prompt_carries_the_guidance_for_that_kind():
    people = ec._prompt_for("person")
    paths = ec._prompt_for("path")
    assert "handle" in people and "handle" not in paths
    assert "tail of the other" in paths
    # ...and the base prompt survives in both.
    assert "Return ONLY JSON" in people and "Return ONLY JSON" in paths


def test_domain_rules_can_be_added_without_touching_the_format(monkeypatch):
    monkeypatch.setenv("ENTITY_CONFIRM_GUIDANCE",
                       "'Amazon' is not the same as 'AWS'.")
    prompt = ec._prompt_for("general")
    assert "'Amazon' is not the same as 'AWS'." in prompt
    assert "Return ONLY JSON" in prompt


def test_pairs_of_different_kinds_are_asked_separately(monkeypatch):
    """
    A file pair and a person pair need opposite advice -- "a shared directory
    means nothing" against "a handle is not a name" -- and a batch holding both
    can be given neither.
    """
    asked = []

    import brahmastra.llm as llm

    def record(system, user, **kwargs):
        asked.append(system)
        return '{"verdicts":[{"pair":1,"same":true}]}'

    monkeypatch.setattr(llm, "chat", record)
    ec.confirm([("a/b.py", "a/c.py"), ("Shaan Kapoor", "Sarah Chen")])

    assert len(asked) == 2
    assert any("tail of the other" in s for s in asked)
    assert any("handle" in s for s in asked)


# -- validate, then re-ask with the reason -----------------------------------

def test_a_rejected_reply_is_asked_again_with_the_reason(monkeypatch):
    """
    cocoindex re-prompts with explicit feedback rather than giving up. Raising
    on the first bad reply throws away a model that would have got it right on
    being told what was wrong.
    """
    seen = []

    import brahmastra.llm as llm

    def flaky(system, user, **kwargs):
        seen.append(user)
        if len(seen) == 1:
            return "I think the first one is probably the same?"
        return '{"verdicts":[{"pair":1,"same":true},{"pair":2,"same":false}]}'

    monkeypatch.setattr(llm, "chat", flaky)
    verdicts, unanswered = ec.confirm(PAIRS)

    assert len(seen) == 2
    assert "rejected" in seen[1]           # it was told WHY
    assert unanswered == []
    assert verdicts[PAIRS[0]] is True and verdicts[PAIRS[1]] is False


def test_a_missing_verdict_is_asked_for_by_number(monkeypatch):
    seen = []

    import brahmastra.llm as llm

    def partial(system, user, **kwargs):
        seen.append(user)
        if len(seen) == 1:
            return '{"verdicts":[{"pair":1,"same":true}]}'
        return '{"verdicts":[{"pair":1,"same":true},{"pair":2,"same":true}]}'

    monkeypatch.setattr(llm, "chat", partial)
    verdicts, unanswered = ec.confirm(PAIRS)

    assert len(seen) == 2
    assert "pair(s) [2]" in seen[1]
    assert unanswered == []


def test_the_retry_budget_is_bounded(monkeypatch):
    """Exhausting it still raises Unanswered. A judge that cannot be reached
    must not return a verdict, however many times it was asked."""
    calls = []

    import brahmastra.llm as llm

    def never(system, user, **kwargs):
        calls.append(user)
        return "not json at all"

    monkeypatch.setattr(llm, "chat", never)
    verdicts, unanswered = ec.confirm(PAIRS)

    assert len(calls) == ec.RETRIES + 1
    assert verdicts == {}
    assert unanswered == PAIRS


def test_an_outage_is_not_retried(monkeypatch):
    """A quota failure is not a bad reply, and asking again will not fix it."""
    calls = []

    import brahmastra.llm as llm

    def dead(system, user, **kwargs):
        calls.append(user)
        raise llm.LLMQuotaExhausted("Groq daily quota exhausted")

    monkeypatch.setattr(llm, "chat", dead)
    verdicts, unanswered = ec.confirm(PAIRS)

    assert len(calls) == 1
    assert unanswered == PAIRS


def test_a_verdict_that_parsed_is_never_asked_again(monkeypatch):
    """
    A RETRY IS NOT A SECOND OPINION. Only an unreadable or incomplete reply is
    re-asked; otherwise this would quietly become "keep asking until it agrees".
    """
    calls = []

    import brahmastra.llm as llm

    def refuse_everything(system, user, **kwargs):
        calls.append(user)
        return '{"verdicts":[{"pair":1,"same":false},{"pair":2,"same":false}]}'

    monkeypatch.setattr(llm, "chat", refuse_everything)
    verdicts, unanswered = ec.confirm(PAIRS)

    assert len(calls) == 1
    assert verdicts == {PAIRS[0]: False, PAIRS[1]: False}
