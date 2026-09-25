"""
A contradiction between an old and a new statement is settled by WHEN each was
written -- the note's time, not extraction's.

Before: resolved by `extracted_at`, which a full re-extraction restamped, so 6
of the 7 live contradictions chose a winner by accident. After: 4 resolved to
the newest statement, 3 honestly "unresolved" (both values in one note).
"""
from __future__ import annotations

import pytest

from brahmastra import db
from brahmastra.concept_graph import _detect_contradictions
from brahmastra.note_times import evidence_time, fact_time, plan


# -- the stores stamp times ----------------------------------------------------

@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()
    yield
    reset_store()


def _note(note_id):
    return next(n for n in db.get_notes() if n["id"] == note_id)


def test_a_new_note_is_stamped_once(store):
    db.upsert_note("n1", "T", "body")
    n = _note("n1")
    assert n["created_at"] and n["updated_at"] == n["created_at"]


def test_changing_the_text_moves_updated_but_never_created(store):
    db.upsert_note("n1", "T", "body")
    first = _note("n1")
    db.upsert_note("n1", "T", "a different body")
    after = _note("n1")
    assert after["created_at"] == first["created_at"]
    assert after["updated_at"] >= first["updated_at"]


def test_a_resync_of_the_same_text_leaves_updated_alone(store):
    db.upsert_note("n1", "T", "body")
    first = _note("n1")["updated_at"]
    db.upsert_note("n1", "T", "body")
    assert _note("n1")["updated_at"] == first


def test_backfill_never_overwrites_a_recorded_time(store):
    db.upsert_note("n1", "T", "body")
    recorded = _note("n1")["created_at"]
    assert db.backfill_note_times({"n1": "2020-01-01T00:00:00+00:00"}) == 0
    assert _note("n1")["created_at"] == recorded


# -- evidence for notes written before times existed ---------------------------

@pytest.mark.parametrize("note,expected,how", [
    ({"last_edited": "2026-06-25T09:49:00"}, "2026-06-25T09:49:00", "notion"),
    ({"id": "checkpoint-1788179374600632400-000-3989f74b"}, "2026-08-31", "checkpoint"),
    ({"title": "Memoised Comprehension (2026-09-21)"}, "2026-09-21", "title"),
    ({"title": "Restoring Brahmastra (20 September 2026)"}, "2026-09-20", "title"),
    ({"title": "Recent UI Changes - June 15 2026"}, "2026-06-15", "title"),
    ({"title": "Quota Fail-Fast Fix (August 2026)"}, "2026-08-01", "title-month"),
    ({"title": "x", "content": "On 2026-09-24, the audit found"}, "2026-09-24", "opening"),
    ({"title": "Quick note", "content": "no date anywhere"}, None, "none"),
])
def test_evidence(note, expected, how):
    at, source = evidence_time(note)
    assert source == how
    assert at is None if expected is None else at.startswith(expected)


def test_a_transcript_timestamp_beats_a_month_in_the_title():
    at, how = evidence_time({"title": "Fix (August 2026)"},
                            sessions={"Fix (August 2026)": "2026-08-19T10:00:00Z"})
    assert (at, how) == ("2026-08-19T10:00:00Z", "session")


def test_plan_skips_notes_that_are_already_dated():
    times, counts = plan([{"id": "a", "created_at": "x", "title": "(2026-01-01)"},
                          {"id": "b", "title": "(2026-01-02)"}])
    assert list(times) == ["b"] and counts["already dated"] == 1


def test_a_notion_edit_is_the_time_the_fact_was_asserted():
    assert fact_time({"last_edited": "L", "updated_at": "U", "created_at": "C"}) == "L"
    assert fact_time({"updated_at": "U", "created_at": "C"}) == "U"
    assert fact_time(None) is None


# -- contradictions --------------------------------------------------------------

def _t(obj, note):
    return {"subject_text": "test suite", "relation": "has_status", "object_text": obj,
            "source_note_id": note, "source_quote": obj,
            "extracted_at": "2026-09-24T00:00:00"}   # all restamped alike


def test_the_newest_statement_wins_whatever_extraction_says():
    c, = _detect_contradictions(
        [_t("517 passing", "new"), _t("84 passing", "old")], {},
        {"new": "2026-09-21T00:00:00", "old": "2026-08-12T00:00:00"})
    assert c["resolved_value"] == "517 passing" and c["resolution"] == "newest"
    assert c["evidence"][0]["asserted_at"] == "2026-09-21T00:00:00"


def test_undated_notes_leave_it_unresolved_rather_than_guessed():
    c, = _detect_contradictions([_t("517", "a"), _t("84", "b")], {}, {})
    assert c["resolved_value"] == "" and c["resolution"].startswith("unresolved")


def test_both_values_in_one_note_is_unresolved():
    """A before-and-after in one note: 500 before the fix, 200 after."""
    c, = _detect_contradictions([_t("500", "n"), _t("200", "n")], {},
                                {"n": "2026-06-01T00:00:00"})
    assert c["resolved_value"] == "" and "same time" in c["resolution"]
