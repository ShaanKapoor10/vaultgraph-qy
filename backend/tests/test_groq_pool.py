"""
Several Groq keys, and the wait Groq itself names.

Groq says which limit it hit -- "tokens per minute" clears in seconds, "tokens
per day" in hours -- and for how long: "Please try again in 7.5s", "in
1h12m3.36s". These tests pin that the pool acts on both halves of that:
rotating away from a key that is out for the day, waiting out a window that
closes in seconds, dropping a key that is not a key, and handing back anything
that is not about the key at all.

No network. Every client here is a fake that fails the way Groq does, in the
words Groq uses.
"""
from __future__ import annotations

import pytest

from brahmastra import groq_pool as gp


# Looked up AT TEST TIME, never imported at collection. Other test files
# `importlib.reload(brahmastra.llm)`, which mints fresh exception classes; the
# pool raises whichever class is current when it runs, so a class captured at
# import stops matching -- these tests passed alone and failed in the full
# suite for exactly that reason.
class _Current:
    def __getattr__(self, name):
        import sys
        return getattr(sys.modules["brahmastra.llm"], name)


_errors = _Current()

TPM = ("Error code: 429 - {'error': {'message': 'Rate limit reached for model "
       "`openai/gpt-oss-120b` in organization `org_A` service tier `on_demand` "
       "on tokens per minute (TPM): Limit 8000, Used 7990, Requested 900. "
       "Please try again in 7.5s.'}}")
TPD = ("Error code: 429 - {'error': {'message': 'Rate limit reached for model "
       "`openai/gpt-oss-120b` in organization `org_A` service tier `on_demand` "
       "on tokens per day (TPD): Limit 200000, Used 199850, Requested 4213. "
       "Please try again in 1h12m3.36s.'}}")
INVALID = "Error code: 401 - {'error': {'message': 'Invalid API Key', 'code': 'invalid_api_key'}}"
MISSING = "Error code: 404 - {'error': {'message': 'The model `x` does not exist'}}"
TOO_LARGE = ("Error code: 413 - Request too large for model `x` on tokens per "
             "minute (TPM): Limit 8000, Requested 9338")


class Script:
    """Each key fails with its scripted errors in turn, then succeeds."""

    def __init__(self, **plans):
        self.plans = {k: list(v) for k, v in plans.items()}
        self.calls: list[str] = []

    def client(self, key):
        script = self

        class Client:
            def create(self):
                script.calls.append(key)
                plan = script.plans.get(key, [])
                if plan:
                    raise RuntimeError(plan.pop(0))
                return f"ok from {key}"

        return Client()


@pytest.fixture
def slept(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(gp.time, "sleep", lambda s: waits.append(s))
    return waits


def _pool(script, *keys):
    pool = gp.GroqKeyPool(list(keys))
    pool._clients = {k: script.client(k) for k in keys}
    return pool


# -- reading Groq's own numbers ----------------------------------------------

@pytest.mark.parametrize("text,seconds", [
    ("Please try again in 7.5s.", 7.5),
    ("Please try again in 1m14.2s.", 74.2),
    ("Please try again in 1h12m3.36s.", 4323.36),
    ("Please try again in 12m.", 720.0),
    ("no instruction at all", None),
])
def test_the_stated_wait_is_read_including_hours(text, seconds):
    """The regex this replaces matched none of the hour forms."""
    assert gp.parse_wait(text) == (pytest.approx(seconds) if seconds else None)


@pytest.mark.parametrize("message,kind", [
    (TPM, "minute"), (TPD, "daily"), (INVALID, "invalid"),
    (MISSING, "model"), (TOO_LARGE, "too_large"),
    ("Connection reset by peer", "transient"),
])
def test_each_failure_is_recognised_for_what_it_is(message, kind):
    assert gp.classify(RuntimeError(message)) == kind


def test_a_413_that_fits_in_a_minute_is_a_wait_not_a_failure():
    """Groq words a per-minute limit as a 413 when one request is large."""
    fits = ("413 - Request too large for model `x` on tokens per minute (TPM): "
            "Limit 8000, Requested 6000")
    assert gp.classify(RuntimeError(fits)) == "minute"


# -- rotation ----------------------------------------------------------------

def test_a_key_out_for_the_day_is_passed_over_at_once(slept):
    """No waiting: an hour's rest is not worth sleeping through when another
    key is ready now."""
    script = Script(a=[TPD])
    pool = _pool(script, "a", "b")

    assert pool.call(lambda c: c.create()) == "ok from b"
    assert script.calls == ["a", "b"]
    assert slept == []


def test_the_rested_key_is_skipped_on_the_next_call_too(slept):
    """The rest outlives the call that discovered it -- otherwise every call
    would pay one failed request to relearn the same thing."""
    script = Script(a=[TPD])
    pool = _pool(script, "a", "b")
    pool.call(lambda c: c.create())
    script.calls.clear()

    pool.call(lambda c: c.create())
    assert script.calls == ["b"]


def test_an_invalid_key_is_dropped_for_good(slept):
    script = Script(a=[INVALID])
    pool = _pool(script, "a", "b")
    pool.call(lambda c: c.create())
    assert pool.states[0].dead
    assert "dead" in pool.status()[0]["state"]


def test_calls_are_spread_across_keys(slept):
    """Round-robin, so two accounts' budgets drain together rather than one
    being spent before the other is touched."""
    script = Script()
    pool = _pool(script, "a", "b")
    for _ in range(4):
        pool.call(lambda c: c.create())
    assert script.calls == ["a", "b", "a", "b"]


# -- the watcher -------------------------------------------------------------

def test_when_every_key_is_briefly_limited_the_pool_waits_as_told(slept):
    """
    THE WATCHER. Every key inside a per-minute window: wait the seconds Groq
    named -- a hair more -- rather than fail work that succeeds shortly after.
    """
    script = Script(a=[TPM], b=[TPM])
    pool = _pool(script, "a", "b")

    assert pool.call(lambda c: c.create()).startswith("ok")
    assert slept == [pytest.approx(7.6)]


def test_the_watcher_gives_up_after_two_waits_without_calling_it_quota(slept):
    """
    Bounded, and NOT reported as a spent quota: LLMQuotaExhausted stops the
    whole extraction run, and a per-minute window must never do that.
    """
    script = Script(a=[TPM] * 10)
    pool = _pool(script, "a")

    with pytest.raises(_errors.LLMUnavailable) as err:
        pool.call(lambda c: c.create())
    assert not isinstance(err.value, _errors.LLMQuotaExhausted)
    assert len(slept) == gp.MAX_WAITS
    assert "after 3 attempts" in str(err.value)


def test_a_long_rest_is_not_slept_through(slept):
    """An hour is the next run's problem, not this call's."""
    script = Script(a=[TPD])
    pool = _pool(script, "a")
    with pytest.raises(_errors.LLMQuotaExhausted):
        pool.call(lambda c: c.create())
    assert slept == []


# -- when everything is spent ------------------------------------------------

def test_all_keys_spent_says_so_and_names_each_one(slept):
    script = Script(a=[TPD], b=[TPD], c=[INVALID])
    pool = _pool(script, "gsk_aaaaaaaaaaaa", "gsk_bbbbbbbbbbbb", "gsk_cccccccccccc")
    pool._clients = {"gsk_aaaaaaaaaaaa": script.client("a"),
                     "gsk_bbbbbbbbbbbb": script.client("b"),
                     "gsk_cccccccccccc": script.client("c")}

    with pytest.raises(_errors.LLMQuotaExhausted) as err:
        pool.call(lambda c: c.create())
    message = str(err.value)
    assert "all 3 Groq keys exhausted" in message
    assert "daily cap" in message and "dead" in message


def test_the_exhaustion_message_still_reads_as_a_spent_quota(slept):
    """
    run_extraction stops the run by recognising the WORDING -- "tokens per
    day" -- so the pool's summary must carry the underlying message.
    """
    from brahmastra.extraction import _is_quota_error

    script = Script(a=[TPD])
    pool = _pool(script, "a")
    with pytest.raises(_errors.LLMQuotaExhausted) as err:
        pool.call(lambda c: c.create())
    assert _is_quota_error(str(err.value))


def test_keys_are_never_shown_in_full(slept):
    script = Script()
    key = "gsk_FAKEtestKEYnotARealCredential0"
    pool = gp.GroqKeyPool([key])
    pool._clients = {key: script.client(key)}
    assert key not in str(pool.status())
    assert pool.status()[0]["key"] == "gsk_FAKE…"


# -- what the pool hands back ------------------------------------------------

def test_a_retired_model_is_not_the_keys_fault(slept):
    """Trying it on every key would fail every key identically."""
    script = Script(a=[MISSING])
    pool = _pool(script, "a", "b")
    with pytest.raises(RuntimeError, match="does not exist"):
        pool.call(lambda c: c.create())
    assert script.calls == ["a"]


def test_an_oversized_request_goes_back_to_the_caller(slept):
    """Extraction re-budgets a 413; the pool must not swallow it."""
    script = Script(a=[TOO_LARGE])
    pool = _pool(script, "a", "b")
    with pytest.raises(RuntimeError, match="413"):
        pool.call(lambda c: c.create())


def test_a_transient_error_is_retried_a_bounded_number_of_times(slept):
    script = Script(a=["Connection reset by peer"] * 10)
    pool = _pool(script, "a")
    with pytest.raises(_errors.LLMUnavailable, match="after 3 attempts"):
        pool.call(lambda c: c.create())
    assert len(script.calls) == gp.TRANSIENT_ATTEMPTS


# -- configuration -----------------------------------------------------------

def test_keys_come_from_the_list_then_the_single_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "k1, k2 ,k1,,k3")
    monkeypatch.setenv("GROQ_API_KEY", "k2")
    assert gp.configured_keys() == ["k1", "k2", "k3"]


def test_the_single_key_alone_still_works(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "")
    monkeypatch.setenv("GROQ_API_KEY", "only")
    assert gp.configured_keys() == ["only"]


def test_no_key_at_all_is_a_clear_error():
    with pytest.raises(_errors.LLMUnavailable, match="no Groq key configured"):
        gp.GroqKeyPool([]).call(lambda c: c)


def test_a_key_list_counts_as_groq_being_configured(monkeypatch):
    """A deployment that sets only GROQ_API_KEYS must not look Groq-less."""
    from brahmastra import llm

    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEYS", "k1,k2")
    monkeypatch.setattr(llm, "_installed", lambda sdk: True)
    monkeypatch.setattr(llm, "ollama_available", lambda: False)
    assert llm.provider_status()["groq"] is True


def test_the_pool_is_rebuilt_when_the_keys_change(monkeypatch):
    """A key added to .env and reloaded is picked up, with fresh state."""
    monkeypatch.setenv("GROQ_API_KEYS", "k1")
    first = gp.pool()
    monkeypatch.setenv("GROQ_API_KEYS", "k1,k2")
    second = gp.pool()
    assert second is not first
    assert second.keys == ["k1", "k2"]
