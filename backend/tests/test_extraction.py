"""
Tests for the extraction agent — mocks the LLM call so no API key needed.
"""
from __future__ import annotations

import importlib
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "extract_test.db"
    monkeypatch.setenv("BRAHMASTRA_DB", str(db_file))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


VALID_TRIPLES = [
    {
        "subject_text": "Alice",
        "subject_type": "person",
        "relation": "reports_to",
        "object_text": "Bob",
        "object_type": "person",
        "confidence": 0.92,
        "source_quote": "Alice reports to Bob",
    }
]

# Both of these are coercible: they carry a real subject and object, so the
# fact survives as `related_to`. Contrast with genuinely unusable input
# (empty endpoint, sub-threshold confidence), which is still dropped.
INVALID_TRIPLES = [
    {
        "subject_text": "Alice",
        "subject_type": "person",
        "relation": "invalid_relation",  # not in ontology
        "object_text": "Bob",
        "object_type": "person",
        "confidence": 0.9,
    },
    {
        "subject_text": "X",
        "subject_type": "person",
        "relation": "reports_to",
        "object_text": "Y",
        "object_type": "concept",  # reports_to range = person only
        "confidence": 0.9,
    },
]


def test_extract_note_happy_path(temp_db, monkeypatch):
    db = temp_db
    db.upsert_note("n1", "Meeting notes", "Alice reports to Bob.", mark_pending=True)
    note = db.get_note("n1")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from brahmastra import extraction
    importlib.reload(extraction)

    with patch.object(extraction, "_extract_with_llm", return_value=VALID_TRIPLES):
        result = extraction.extract_note(note)

    assert result["triples_added"] == 1
    assert result["triples_skipped"] == 0
    assert result["error"] is None
    assert db.get_note("n1")["extraction_status"] == "done"


def test_extract_note_coerces_rather_than_discarding(temp_db, monkeypatch):
    """
    An off-ontology relation must degrade, not delete the fact.

    Both fixtures used to be dropped outright: one has a relation outside the
    ontology, the other a real relation with an argument type it does not
    admit. Dropping them lost the connection entirely — the reason
    "Sapan works at Veraxion" left no Veraxion entity in the graph. They are
    now kept as `related_to`, which is defined over any types.
    """
    db = temp_db
    db.upsert_note("n2", "T", "C", mark_pending=True)
    note = db.get_note("n2")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from brahmastra import extraction
    importlib.reload(extraction)

    with patch.object(extraction, "_extract_with_llm", return_value=INVALID_TRIPLES):
        result = extraction.extract_note(note)

    assert result["triples_added"] == 2, "facts must survive, not be discarded"
    assert result["triples_skipped"] == 0

    stored = db.get_all_triples()
    assert {t["relation"] for t in stored} == {"related_to"}
    # The endpoints must be untouched — degrading the relation must not
    # silently alter who the fact is about.
    assert {(t["subject_text"], t["object_text"]) for t in stored} == {("Alice", "Bob"), ("X", "Y")}

    # And the coercion is reported, so an ontology gap is visible rather than
    # silent: a relation that keeps appearing here is evidence to add it.
    reasons = " ".join(result["coercions"])
    assert "unmapped_relation:invalid_relation" in reasons
    assert "domain_range:reports_to" in reasons


def test_extract_note_low_confidence_filtered(temp_db, monkeypatch):
    db = temp_db
    db.upsert_note("n3", "T", "C", mark_pending=True)
    note = db.get_note("n3")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    low_conf = [{**VALID_TRIPLES[0], "confidence": 0.1}]

    from brahmastra import extraction
    importlib.reload(extraction)

    with patch.object(extraction, "_extract_with_llm", return_value=low_conf):
        result = extraction.extract_note(note)

    assert result["triples_added"] == 0


def test_extract_note_llm_error_marks_note_error(temp_db, monkeypatch):
    db = temp_db
    db.upsert_note("n4", "T", "C", mark_pending=True)
    note = db.get_note("n4")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from brahmastra import extraction
    importlib.reload(extraction)

    with patch.object(extraction, "_extract_with_llm", side_effect=Exception("API timeout")):
        result = extraction.extract_note(note)

    assert result["error"] is not None
    assert db.get_note("n4")["extraction_status"] == "error"


def test_run_extraction_skips_when_nothing_pending(temp_db, monkeypatch):
    db = temp_db
    db.upsert_note("n5", "T", "C", mark_pending=False)  # status=done

    from brahmastra import extraction
    importlib.reload(extraction)
    result = extraction.run_extraction()
    assert result["extracted"] == 0
    assert result["total_pending"] == 0


# ---------------------------------------------------------------------------
# Provider quota exhaustion
# ---------------------------------------------------------------------------

def test_daily_quota_is_distinguished_from_a_transient_limit():
    """
    Both arrive as HTTP 429; only the wording separates them, and the right
    response is opposite. A per-minute limit clears in seconds and is worth
    retrying; a per-day limit does not, and retrying through it makes every
    remaining call fail too.
    """
    from brahmastra.llm import _is_quota_exhausted as spent

    assert spent(Exception(
        "Error code: 429 - Rate limit reached ... on tokens per day (TPD): "
        "Limit 100000, Used 99041. Please try again in 34m14s"
    ))
    assert spent(Exception("429 requests per day (RPD) exceeded"))

    assert not spent(Exception("Error code: 429 ... tokens per minute (TPM): Limit 12000"))
    assert not spent(Exception("Connection reset by peer"))


def test_a_retired_model_is_not_retried():
    """
    Groq decommissions hosted models. `llama-3.3-70b-versatile` served traffic
    one hour and 404ed the next, and three retries turned a one-line
    configuration problem into something that read like a network fault.
    """
    from brahmastra.llm import _is_model_missing as missing

    assert missing(Exception(
        "Error code: 404 - {'error': {'message': 'The model "
        "`llama-3.3-70b-versatile` does not exist or you do not have access to it.'}}"
    ))

    assert not missing(Exception("Error code: 429 - rate limit reached"))
    assert not missing(Exception("Connection reset by peer"))


def test_extraction_uses_the_model_llm_py_configures(monkeypatch):
    """
    Provider selection was centralised in llm.py so the stages could never
    disagree about which provider is live — but each call site kept its own
    hardcoded MODEL, so they disagreed about that instead. When Groq retired
    llama-3.3-70b, llm.py was fixed and extraction kept calling the dead model:
    every note failed while cluster summaries succeeded in the same run.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    monkeypatch.setenv("GROQ_MODEL", "test/model-from-env")

    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        message = MagicMock()
        message.content = '{"triples": []}'
        choice = MagicMock()
        choice.message = message
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = create
    fake_groq = types.ModuleType("groq")
    fake_groq.Groq = lambda **_: client
    monkeypatch.setitem(sys.modules, "groq", fake_groq)

    from brahmastra import extraction
    importlib.reload(extraction)
    extraction._extract_with_groq("T", "C", "key")

    assert captured["model"] == "test/model-from-env", (
        "extraction must use the model llm.py resolves, not a literal of its own"
    )


def test_a_retired_model_stops_the_run_like_a_spent_quota():
    """Retrying 50 notes against a model that no longer exists is the same grind."""
    from brahmastra.extraction import _is_quota_error

    assert _is_quota_error(
        "Error code: 404 - {'error': {'message': 'The model "
        "`llama-3.3-70b-versatile` does not exist', 'code': 'model_not_found'}}"
    )
    assert _is_quota_error("429 ... tokens per day (TPD): Limit 100000")
    assert not _is_quota_error("Connection reset by peer")


def test_extraction_aborts_on_quota_instead_of_grinding(temp_db, monkeypatch):
    """
    Observed for real: 15 notes retried against a spent daily quota took over
    ten minutes and extracted nothing. The run must stop at the first quota
    error and report what it did not get to.
    """
    db = temp_db
    for i in range(6):
        db.upsert_note(f"n{i}", f"Note {i}", "content", mark_pending=True)

    from brahmastra import extraction
    importlib.reload(extraction)

    calls = {"n": 0}
    quota = ("Error code: 429 - Rate limit reached ... tokens per day (TPD): "
             "Limit 100000, Used 99041")

    def flaky(title, content):
        calls["n"] += 1
        if calls["n"] == 1:
            return []          # first note succeeds
        raise RuntimeError(quota)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with patch.object(extraction, "_extract_with_llm", side_effect=flaky):
        result = extraction.run_extraction(full=False)

    # Stopped at the first quota failure rather than attempting all six.
    assert calls["n"] == 2, f"kept calling after quota was spent ({calls['n']} calls)"
    assert result["extracted"] == 1
    assert result["aborted_after"] == 2
    assert result["remaining"] == 4
    assert "per day" in result["quota_exhausted"].lower()


def test_a_per_minute_limit_is_retried_but_a_settled_failure_is_not(monkeypatch):
    """
    This path had no retry at all while llm.chat has always had one, so a TPM
    429 failed the note outright. Observed: a pipeline run failed all three
    pending notes and an immediate re-run succeeded with 55 triples — nothing
    lost, but the run reported error and skipped write-back for a condition
    that clears in seconds.

    A daily cap or a retired model is a settled fact, not congestion: backing
    off cannot make a spent quota refill or a deleted model exist, so those
    must propagate immediately for run_extraction to stop the whole run.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    monkeypatch.setattr("time.sleep", lambda *_: None)

    def client_raising(error: Exception, succeed_on: int | None = None):
        calls = {"n": 0}

        def create(**_kwargs):
            calls["n"] += 1
            if succeed_on is not None and calls["n"] >= succeed_on:
                msg = MagicMock(); msg.content = '{"triples": []}'
                choice = MagicMock(); choice.message = msg
                resp = MagicMock(); resp.choices = [choice]
                return resp
            raise error

        client = MagicMock()
        client.chat.completions.create = create
        fake = types.ModuleType("groq")
        fake.Groq = lambda **_: client
        monkeypatch.setitem(sys.modules, "groq", fake)
        return calls

    from brahmastra import extraction
    importlib.reload(extraction)

    # Per-minute: transient, so retry and succeed.
    calls = client_raising(RuntimeError("429 tokens per minute (TPM): Limit 12000"), succeed_on=2)
    assert extraction._extract_with_groq("T", "C", "key") == []
    assert calls["n"] == 2, "must retry a per-minute limit"

    # Per-day: settled. Must NOT be retried.
    calls = client_raising(RuntimeError("429 ... tokens per day (TPD): Limit 100000"))
    with pytest.raises(Exception, match="per day"):
        extraction._extract_with_groq("T", "C", "key")
    assert calls["n"] == 1, "a spent daily quota must not be retried"

    # Retired model: settled too.
    calls = client_raising(RuntimeError(
        "Error code: 404 - {'error': {'message': 'The model `x` does not exist'}}"))
    with pytest.raises(Exception, match="does not exist"):
        extraction._extract_with_groq("T", "C", "key")
    assert calls["n"] == 1, "a retired model must not be retried"


# ---------------------------------------------------------------------------
# Backing off for as long as the server actually asks
# ---------------------------------------------------------------------------

def test_the_backoff_honours_the_delay_groq_states():
    """
    Groq puts the wait in the 429: "Please try again in 7.5s".

    Guessing instead is what made retries useless. A blind 2s + 4s covers about
    six seconds of a limit the server says needs thirty, so all three attempts
    land inside the same closed window and the note fails as though the outage
    were permanent. Observed exactly that: one note failed two consecutive
    pipeline runs, then succeeded on a direct call minutes later.
    """
    from brahmastra.extraction import _retry_delay

    assert _retry_delay(Exception("Rate limit ... Please try again in 7.456s"), 0) == pytest.approx(7.556)
    # Minutes are parsed too. Kept under EXTRACT_MAX_BACKOFF so this tests the
    # parsing rather than the cap -- the cap has its own test below.
    assert _retry_delay(Exception("Rate limit ... Please try again in 0m32s"), 0) == pytest.approx(32.1)


def test_a_hint_shorter_than_the_fallback_does_not_shorten_the_wait():
    """A suspiciously brief hint must not retry sooner than we otherwise would."""
    from brahmastra.extraction import _retry_delay

    assert _retry_delay(Exception("try again in 0.2s"), 0) == 2.0
    assert _retry_delay(Exception("try again in 0.2s"), 1) == 4.0


def test_without_a_hint_it_still_backs_off_exponentially():
    from brahmastra.extraction import _retry_delay

    assert _retry_delay(Exception("Connection reset by peer"), 0) == 2.0
    assert _retry_delay(Exception("Connection reset by peer"), 1) == 4.0


def test_a_very_long_wait_is_capped_rather_than_blocking_the_run(monkeypatch):
    """
    Sleeping out a twenty-minute window blocks every remaining note for one
    that the NEXT run retries for free -- errored notes are re-queued
    automatically. Better to fail this note fast and keep going.
    """
    from brahmastra.extraction import EXTRACT_MAX_BACKOFF, _retry_delay

    assert _retry_delay(Exception("try again in 20m5s"), 0) == EXTRACT_MAX_BACKOFF


def test_the_retry_actually_waits_the_advised_time(monkeypatch):
    """The delay is computed correctly AND reaches time.sleep."""
    import sys
    import types
    from unittest.mock import MagicMock

    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    calls = {"n": 0}

    def create(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(
                "Error code: 429 - Rate limit reached ... on tokens per minute "
                "(TPM): Limit 12000. Please try again in 8.5s"
            )
        msg = MagicMock(); msg.content = '{"triples": []}'
        choice = MagicMock(); choice.message = msg
        resp = MagicMock(); resp.choices = [choice]
        return resp

    client = MagicMock()
    client.chat.completions.create = create
    fake = types.ModuleType("groq")
    fake.Groq = lambda **_: client
    monkeypatch.setitem(sys.modules, "groq", fake)

    from brahmastra import extraction
    importlib.reload(extraction)
    monkeypatch.setattr(extraction.time, "sleep", lambda s: slept.append(s))

    assert extraction._extract_with_groq("T", "C", "key") == []
    assert slept and slept[0] == pytest.approx(8.6), (
        f"waited {slept} instead of the 8.5s the server asked for"
    )


def test_the_last_attempt_does_not_sleep_before_giving_up():
    """Sleeping after the final failure delays the error and changes nothing."""
    import sys
    import types
    from unittest.mock import MagicMock, patch

    def create(**_kwargs):
        raise RuntimeError("429 tokens per minute (TPM). Please try again in 5s")

    client = MagicMock()
    client.chat.completions.create = create
    fake = types.ModuleType("groq")
    fake.Groq = lambda **_: client

    slept: list[float] = []
    with patch.dict(sys.modules, {"groq": fake}):
        from brahmastra import extraction
        importlib.reload(extraction)
        with patch.object(extraction.time, "sleep", lambda s: slept.append(s)):
            with pytest.raises(Exception, match="after 3 attempts"):
                extraction._extract_with_groq("T", "C", "key")

    assert len(slept) == 2, f"3 attempts need 2 sleeps, not {len(slept)}"


def test_an_oversized_request_is_not_retried_and_does_not_stop_the_run():
    """
    A 413 is settled: waiting cannot make the request smaller, so three
    attempts spend the backoff to fail identically.

    But unlike a spent quota or a retired model it must NOT abort the run --
    one oversized note says nothing about the next one. Found only because
    extraction_error recorded the message; the failure had been read as a rate
    limit until then.
    """
    import sys
    import types
    from unittest.mock import MagicMock, patch

    from brahmastra.extraction import _is_quota_error, _is_too_large

    msg = ("Error code: 413 - {'error': {'message': 'Request too large for model "
           "`openai/gpt-oss-120b`', 'code': 'request_too_large'}}")

    assert _is_too_large(Exception(msg))
    assert not _is_too_large(Exception("429 tokens per minute"))

    # Groq words a 413 as "Request too large ... on tokens per minute (TPM):
    # Limit 8000, Requested 9338" -- a RATE limit in an error that reads like a
    # size limit. The numbers decide which it is, and treating every 413 as
    # permanent abandons notes that a few seconds would have fixed.
    over = ("413 - Request too large for model `x` on tokens per minute (TPM): "
            "Limit 8000, Requested 9338, please reduce your message size")
    under = ("413 - Request too large for model `x` on tokens per minute (TPM): "
             "Limit 8000, Requested 6000, please reduce your message size")
    assert _is_too_large(Exception(over)), "cannot fit in a whole minute: permanent"
    assert not _is_too_large(Exception(under)), "fits once the minute rolls: retry it"
    assert not _is_quota_error(msg), "a 413 must not stop the whole run"

    calls = {"n": 0}

    def create(**_kwargs):
        calls["n"] += 1
        raise RuntimeError(msg)

    client = MagicMock()
    client.chat.completions.create = create
    fake = types.ModuleType("groq")
    fake.Groq = lambda **_: client

    slept = []
    with patch.dict(sys.modules, {"groq": fake}):
        from brahmastra import extraction
        importlib.reload(extraction)
        with patch.object(extraction.time, "sleep", lambda s: slept.append(s)):
            with pytest.raises(Exception, match="413"):
                extraction._extract_with_groq("T", "C", "key")

    assert calls["n"] == 1, f"an oversized request must not be retried ({calls['n']} calls)"
    assert slept == [], "and must not sleep before failing"


# ---------------------------------------------------------------------------
# Fitting inside a per-minute token budget
# ---------------------------------------------------------------------------

def test_the_output_reservation_scales_with_the_note():
    """
    max_tokens is RESERVED output and providers bill the reservation, not what
    is produced. A fixed 8192 made every request cost ~9.3k against an 8000 TPM
    tier and 413 before the note was even read -- extraction was impossible
    there whatever the note said.
    """
    from brahmastra.extraction import (
        EXTRACTION_MAX_TOKENS, SYSTEM_PROMPT, _build_user_message, _output_budget,
    )

    short = _output_budget(SYSTEM_PROMPT, _build_user_message("T", "a short note."))
    long_ = _output_budget(SYSTEM_PROMPT, _build_user_message("T", "a long note. " * 400))

    assert short < long_, "a bigger note earns a bigger reply"
    assert long_ <= EXTRACTION_MAX_TOKENS, "and is still capped"


def test_a_short_note_never_reserves_below_the_truncation_floor():
    """Truncated JSON is unparseable, so the note loses every triple, not some."""
    from brahmastra.extraction import MIN_OUTPUT_TOKENS, SYSTEM_PROMPT, _output_budget

    assert _output_budget(SYSTEM_PROMPT, "x") >= MIN_OUTPUT_TOKENS


def test_a_stated_limit_is_learned_and_then_constrains_the_reservation(monkeypatch):
    """
    The provider states its allowance in the error. Learning it turns a 413
    from a dead note into a self-correcting one -- the first oversized request
    teaches every request after it.
    """
    import brahmastra.extraction as ex

    monkeypatch.setattr(ex, "_LEARNED_TPM", None)
    prompt, msg = ex.SYSTEM_PROMPT, ex._build_user_message("T", "note. " * 300)
    before = ex._output_budget(prompt, msg)

    assert ex._learn_tpm(Exception("TPM: Limit 2000, Requested 9338")) is True
    assert ex._LEARNED_TPM == 2000
    after = ex._output_budget(prompt, msg)

    assert after < before, "a tight limit must shrink the reservation"
    # Learning the same value twice is not new information, so it must not
    # trigger another retry of a request that already failed.
    assert ex._learn_tpm(Exception("TPM: Limit 2000, Requested 9338")) is False


def test_an_error_without_numbers_teaches_nothing(monkeypatch):
    import brahmastra.extraction as ex

    monkeypatch.setattr(ex, "_LEARNED_TPM", None)
    assert ex._learn_tpm(Exception("Connection reset by peer")) is False
    assert ex._LEARNED_TPM is None
