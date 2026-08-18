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
