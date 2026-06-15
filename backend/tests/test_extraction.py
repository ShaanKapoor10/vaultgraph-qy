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


def test_extract_note_filters_invalid_triples(temp_db, monkeypatch):
    db = temp_db
    db.upsert_note("n2", "T", "C", mark_pending=True)
    note = db.get_note("n2")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    from brahmastra import extraction
    importlib.reload(extraction)

    with patch.object(extraction, "_extract_with_llm", return_value=INVALID_TRIPLES):
        result = extraction.extract_note(note)

    assert result["triples_added"] == 0
    assert result["triples_skipped"] == 2


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
