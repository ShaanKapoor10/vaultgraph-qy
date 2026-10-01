"""
Session modes (ingest/modes.py): a lecture is read for what was taught and what
the audience asked, a meeting for what was decided. Every mode's output is held
to the transcript by the same checks. No provider: the chat is faked.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from brahmastra.ingest import comprehend, modes
from brahmastra.ingest.reader import SessionReader
from brahmastra.ingest.segment import segment

pytestmark = pytest.mark.real_session_passes

LECTURE = """\
[00:20:59] Priya: The definition of buy swap is that you pay fixed and receive float. Buyer of the swap is always paying fixed and receiving float.
[00:24:42] Dev: Probably I'm jumping a little ahead, but what is there for Tom and Sam to get this?
[00:24:50] Priya: They have a market view better than Leo and Max, and they will bake in the margin.
[00:37:23] Priya: So there is going to be a questionnaire that will be released on Friday.
"""


def _fake(replies: dict[str, dict], seen: list | None = None):
    """Route on the system prompt: each pass gets its own reply."""
    def chat(system, user, **kw):
        if seen is not None:
            seen.append((system, user))
        for marker, reply in replies.items():
            if marker in system:
                return json.dumps(reply)
        return json.dumps({})
    return chat


NOTES = {"topic": "Buying and selling swaps", "summary": "Swaps were defined.",
         "points": [
             {"point": "The buyer of a swap pays fixed and receives floating",
              "quote": "Buyer of the swap is always paying fixed and receiving float"},
             {"point": "Swaps were invented in 1981 by IBM and the World Bank",
              "quote": "swaps were first done between IBM and the World Bank"}]}
AUDIENCE = {
    "questions": [
        {"question": "What is in the swap for Tom and Sam?", "asked_by": "Dev",
         "quote": "what is there for Tom and Sam to get this",
         "answer": "A better market view, and a margin",
         "answer_quote": "They have a market view better than Leo and Max"},
        {"question": "Is this all making sense?", "asked_by": "Priya",
         "quote": "Is this all making sense to everybody here",
         "answer": None, "answer_quote": None}],
    "action_items": [
        {"task": "Release the questionnaire", "owner": "Priya", "due": "Friday",
         "quote": "there is going to be a questionnaire that will be released on Friday"}]}


def _lecture_reader(monkeypatch, seen=None):
    monkeypatch.setattr(comprehend, "_cached_chat",
                        _fake({"take notes": NOTES, "AUDIENCE side": AUDIENCE}, seen))
    return SessionReader("lecture")


def test_unknown_or_missing_mode_is_a_meeting():
    assert modes.get_mode(None).id == "meeting"
    assert modes.get_mode("nonsense").id == "meeting"
    assert modes.get_mode("Lecture").id == "lecture"


def test_a_lecture_yields_points_questions_and_follow_ups(monkeypatch):
    chunk = segment(LECTURE)[0]
    u = _lecture_reader(monkeypatch)(chunk)
    kinds = sorted({a.kind for a in u.artifacts})
    assert kinds == ["action_item", "point", "question"]
    assert u.topic == "Buying and selling swaps" and u.calls == 2


def test_a_point_whose_quote_is_not_in_the_transcript_is_dropped(monkeypatch):
    u = _lecture_reader(monkeypatch)(segment(LECTURE)[0])
    points = [a.statement for a in u.artifacts if a.kind == "point"]
    assert points == ["The buyer of a swap pays fixed and receives floating"]
    assert any("IBM" in r for r in u.rejected)


def test_an_answer_is_kept_only_with_its_own_quote(monkeypatch):
    u = _lecture_reader(monkeypatch)(segment(LECTURE)[0])
    q = next(a for a in u.artifacts if a.kind == "question")
    assert q.rationale == "A better market view, and a margin"
    assert q.owner == "Dev"


def test_a_question_quoting_words_never_said_is_dropped(monkeypatch):
    u = _lecture_reader(monkeypatch)(segment(LECTURE)[0])
    assert not any("making sense" in a.statement for a in u.artifacts)


def test_each_part_is_told_what_the_part_before_noted(monkeypatch):
    seen: list = []
    reader = _lecture_reader(monkeypatch, seen)
    chunks = segment(LECTURE, max_tokens=40)
    assert len(chunks) >= 2
    reader(chunks[0])
    reader(chunks[1])
    notes_calls = [user for system, user in seen if "take notes" in system]
    assert "first part" in notes_calls[0]
    part_two = next(u for u in notes_calls if "Part 2 of the transcript" in u)
    assert "The buyer of a swap pays fixed" in part_two


def test_a_meeting_keeps_its_measured_passes_and_gains_points(monkeypatch):
    from brahmastra.ingest.comprehend import ChunkUnderstanding

    calls = []

    def strategy(chunk):
        calls.append(chunk.index)
        return ChunkUnderstanding(chunk_index=chunk.index, summary="kept", calls=2)

    monkeypatch.setattr(comprehend, "_cached_chat", _fake({"take notes": NOTES}))
    u = SessionReader("meeting", meeting_strategy=strategy)(segment(LECTURE)[0])
    assert calls == [0] and u.summary == "kept" and u.calls == 3
    assert [a.kind for a in u.artifacts] == ["point"]


def test_a_failed_notes_pass_degrades_a_meeting_part_rather_than_failing_it(monkeypatch):
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding

    def strategy(chunk):
        return ChunkUnderstanding(chunk_index=chunk.index, artifacts=[Artifact("decision", "x")])

    def down(*a, **k):
        raise RuntimeError("quota")

    monkeypatch.setattr(comprehend, "_cached_chat", down)
    u = SessionReader("meeting", meeting_strategy=strategy)(segment(LECTURE)[0])
    assert u.error is None and len(u.artifacts) == 1
    assert any(r.startswith("pass failed: notes") for r in u.rejected)


def test_the_overview_reads_notes_and_fails_softly():
    from brahmastra.ingest.overview import write_overview

    parts = [{"idx": 0, "start_time": "00:20:59", "topic": "Swaps", "summary": "Defined.",
              "points": ["The buyer of a swap pays fixed"]}]
    seen = []

    def chat(system, user, **kw):
        seen.append(user)
        return json.dumps({"headline": "Swaps", "summary": "s",
                           "themes": [{"title": "Buying", "points": ["pays fixed"]}, {"title": ""}]})

    out = write_overview(modes.get_mode("lecture"), "Trading 101", parts, chat=chat)
    assert out["headline"] == "Swaps" and [t["title"] for t in out["themes"]] == ["Buying"]
    assert "The buyer of a swap pays fixed" in seen[0]

    def down(*a, **k):
        raise RuntimeError("quota")
    assert "quota" in write_overview(modes.get_mode("lecture"), "t", parts, chat=down)["error"]


def test_old_tables_gain_the_new_columns_in_place(monkeypatch, tmp_path):
    """transcripts is SOURCE data: the mode column is added, never by recreating it."""
    db = tmp_path / "old.db"
    monkeypatch.setenv("BRAHMASTRA_DB", str(db))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE transcripts (id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT 'default', "
                 "title TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'upload', "
                 "source_ref TEXT, occurred_at TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT "
                 "'pending', error TEXT, chunk_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (workspace_id, id))")
    conn.execute("INSERT INTO transcripts (id, title, content, created_at) VALUES ('t1', 'Old', 'x', 'now')")
    conn.commit()
    conn.close()

    from brahmastra.ingest.store import IngestStore
    store = IngestStore("default")
    row = store.get_transcript("t1")
    assert row["title"] == "Old" and row["mode"] is None
    store.set_transcript_mode("t1", "lecture")
    assert store.get_transcript("t1")["mode"] == "lecture"


def test_a_lecture_question_reaches_the_graph_record_as_asked_by():
    from brahmastra.ingest.graph_record import record_triples

    triples = record_triples("Trading 101", ["Priya", "Dev"], [
        {"kind": "question", "statement": "What is in it for the dealers?", "said_by": "Dev"},
    ])
    rels = {(t["subject_text"], t["relation"], t["object_text"]) for t in triples}
    assert ("What is in it for the dealers?", "asked_by", "Dev") in rels


def test_a_point_becomes_a_whole_statement_with_its_speaker_and_what_it_is_about():
    from brahmastra.ingest.graph_record import record_triples

    long = ("Payments is maybe sixty percent done: the card flow is basically done, "
            "while refunds and the reconciliation job have not been started, and Priya is out "
            "until the 20th, so the March date is not real any more")
    triples = record_triples("Q3 planning", ["Mei"], [
        {"kind": "point", "statement": long, "said_by": "Mei", "quote": "sixty percent done",
         "about": [{"name": "payments integration", "type": "project"},
                   {"name": "Priya", "type": "person"}, {"name": "Mei", "type": "person"}]},
    ])
    rels = {(t["subject_text"], t["relation"], t["object_text"], t["object_type"]) for t in triples}
    assert (long, "said_by", "Mei", "person") in rels                 # kept whole, not cut at 160
    assert (long, "mentions", "payments integration", "project") in rels
    assert (long, "mentions", "Priya", "person") in rels
    assert not any(r[1] == "mentions" and r[2] == "Mei" for r in rels)  # the speaker is said_by
    assert all(t["subject_type"] == "statement" for t in triples if t["subject_text"] == long)


def test_a_point_is_about_only_what_the_passage_names(monkeypatch):
    from brahmastra.ingest.comprehend import build_understanding, POINT_SPECS

    chunk = segment(LECTURE)[0]
    u = build_understanding({"points": [{
        "point": "The buyer of a swap pays fixed and receives floating",
        "quote": "Buyer of the swap is always paying fixed and receiving float",
        "about": [{"name": "the buyer of the swap", "type": "concept"},
                  {"name": "Stripe", "type": "organisation"},
                  {"name": "swap", "type": "weird-type"}]}]}, chunk, specs=POINT_SPECS)
    about = u.artifacts[0].about
    assert {a["name"] for a in about} == {"buyer of the swap", "swap"}   # Stripe was never said
    assert next(a for a in about if a["name"] == "swap")["type"] == "concept"


def test_the_presenters_own_questions_are_not_audience_questions():
    from brahmastra.ingest.comprehend import Artifact
    from brahmastra.ingest.reader import drop_presenter_questions, presenter_of

    talk = LECTURE + "[00:40:00] Priya: " + "The exchange lists every specification. " * 30 + "\n"
    chunks = segment(talk)
    assert presenter_of(chunks) == "Priya"
    arts = [Artifact("question", "Does this time work?", quote="The definition of buy swap is that you pay fixed",
                     chunk_index=0),
            Artifact("question", "What is in it for Tom and Sam?",
                     quote="what is there for Tom and Sam to get this", chunk_index=0)]
    kept, dropped = drop_presenter_questions(arts, chunks)
    assert [a.statement for a in kept] == ["What is in it for Tom and Sam?"] and len(dropped) == 1


def test_a_discussion_has_no_presenter():
    from brahmastra.ingest.reader import presenter_of
    assert presenter_of(segment(
        "Sarah: One two three four five.\nMei: Six seven eight nine ten.\nRaj: Eleven twelve.\n")) is None


def test_part_notes_can_stay_out_of_extraction_and_are_settled(monkeypatch, tmp_path):
    """INGEST_EXTRACT_PART_NOTES=0: the summary note is kept, never re-read into triples."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra import db, extraction

    db.init_db()
    db.upsert_note("t1-c0", "Part 1", "A summary a model wrote.", mark_pending=True, source="transcript")
    db.insert_triples([{"subject_text": "A", "subject_type": "concept", "relation": "related_to",
                        "object_text": "B", "object_type": "concept", "confidence": 0.5,
                        "source_quote": "", "source_note_id": "t1-c0"}])
    monkeypatch.setenv("INGEST_EXTRACT_PART_NOTES", "0")
    monkeypatch.setattr(extraction, "extract_note", lambda *a, **k: pytest.fail("summary was extracted"))
    out = extraction.run_extraction()
    assert out["extracted"] == 0
    assert db.get_note("t1-c0")["extraction_status"] == "done"
    assert not [t for t in db.get_all_triples() if t.get("source_note_id") == "t1-c0"]


def test_two_readings_of_a_part_are_merged_without_duplicates(monkeypatch):
    from brahmastra.ingest import comprehend
    from brahmastra.ingest.comprehend import ChunkUnderstanding

    replies = iter([
        {"topic": "Swaps", "points": [
            {"point": "The buyer of a swap pays fixed and receives floating",
             "quote": "Buyer of the swap is always paying fixed and receiving float"}]},
        {"topic": "", "points": [
            {"point": "The buyer of a swap pays fixed and receives floating",
             "quote": "Buyer of the swap is always paying fixed and receiving float"},
            {"point": "Dealers bake in a margin",
             "quote": "they will bake in the margin"}]}])
    seen = []

    def chat(system, user, **kw):
        seen.append(user)
        return json.dumps(next(replies))

    monkeypatch.setattr(comprehend, "_cached_chat", chat)
    monkeypatch.setenv("INGEST_NOTES_READINGS", "2")
    reader = SessionReader("meeting", meeting_strategy=lambda c: ChunkUnderstanding(chunk_index=c.index))
    u = reader(segment(LECTURE)[0])
    points = [a.statement for a in u.artifacts if a.kind == "point"]
    assert points == ["The buyer of a swap pays fixed and receives floating", "Dealers bake in a margin"]
    assert "Independent reading 2" in seen[1] and "Independent reading" not in seen[0]


def test_an_unreadable_reply_is_never_served_from_the_cache(monkeypatch):
    """A non-JSON reply to a JSON request is not cached, and a cached one is ignored."""
    from brahmastra.ingest import memo

    store: dict = {}
    monkeypatch.setattr(memo, "load", lambda key: store.get(key))
    monkeypatch.setattr(memo, "save", lambda key, reply: store.__setitem__(key, reply))
    replies = iter(["# Meeting minutes\n- not json", '{"decisions": []}'])
    monkeypatch.setattr("brahmastra.llm.chat", lambda *a, **k: next(replies))
    assert comprehend._cached_chat("sys", "user", json_mode=True).startswith("#")
    assert store == {}
    assert comprehend._cached_chat("sys", "user", json_mode=True) == '{"decisions": []}'


def test_two_notes_readings_are_the_default(monkeypatch):
    from brahmastra.ingest.reader import notes_readings

    monkeypatch.delenv("INGEST_NOTES_READINGS", raising=False)
    assert notes_readings() == 2
