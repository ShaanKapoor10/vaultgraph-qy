"""
A meeting's decisions, actions, risks and questions, declared straight into the graph.

Measured on the one ingested meeting, through the prose-note bridge:

    content reaches the graph        15 / 16
    owner tied to their item          5 / 13
    decision / risk / question kept   0 / 16

"Mei will update the roadmap" arrived as `Mei --related_to--> roadmap update`.
These tests pin what the direct record guarantees instead: every owner tied,
every kind kept, the speaker's own words as the evidence, no model involved.
"""
from __future__ import annotations

import pytest

from brahmastra import db
from brahmastra.ingest import assemble
from brahmastra.ingest import graph_record as gr
from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
from brahmastra.ingest.store import IngestStore, Transcript
from brahmastra.ontology import is_valid_triple

MEETING = "Q3 release planning (2026-09-01)"

ARTIFACTS = [
    {"kind": "decision", "statement": "The release moves to April 15th",
     "owner": "Sarah", "quote": "Then we're moving the release to April 15th"},
    {"kind": "action_item", "statement": "Update the roadmap", "owner": "Mei",
     "quote": "I'll update the roadmap by Friday"},
    {"kind": "risk", "statement": "Acme contract assumes a March delivery",
     "owner": "Sarah", "quote": "the Acme contract assumes a March delivery"},
    {"kind": "open_question", "statement": "Is the reporting move still on?",
     "owner": "Raj", "quote": "Are we still planning to move the reporting service"},
]


def _edges(triples):
    return {(t["subject_text"], t["relation"], t["object_text"]) for t in triples}


# -- the pure builder --------------------------------------------------------

def test_every_owner_is_tied_to_their_item_with_the_right_relation():
    """The 5-of-13 the prose bridge managed becomes all of them."""
    edges = _edges(gr.record_triples(MEETING, ["Sarah", "Mei"], ARTIFACTS))
    assert ("The release moves to April 15th", "decided_by", "Sarah") in edges
    assert ("Update the roadmap", "assigned_to", "Mei") in edges
    assert ("Is the reporting move still on?", "asked_by", "Raj") in edges


def test_whoever_raised_a_risk_is_not_made_its_owner():
    """Naming a risk is not being accountable for it; saying so would put a
    falsehood in the knowledge base."""
    edges = _edges(gr.record_triples(MEETING, [], ARTIFACTS))
    risk = "Acme contract assumes a March delivery"
    assert (risk, "raised_by", "Sarah") in edges
    assert not any(s == risk and r == "assigned_to" for s, r, _ in edges)


def test_every_item_is_tied_to_the_meeting_and_keeps_its_kind():
    triples = gr.record_triples(MEETING, [], ARTIFACTS)
    kinds = {t["subject_text"]: t["subject_type"] for t in triples
             if t["relation"] == "discussed_in"}
    assert kinds == {
        "The release moves to April 15th": "decision",
        "Update the roadmap": "action_item",
        "Acme contract assumes a March delivery": "risk",
        "Is the reporting move still on?": "question",
    }


def test_attendance_comes_from_the_speakers():
    edges = _edges(gr.record_triples(MEETING, ["Sarah", "Mei", "Sarah"], []))
    assert edges == {("Mei", "attended", MEETING), ("Sarah", "attended", MEETING)}


def test_the_evidence_is_the_speakers_own_words():
    triples = gr.record_triples(MEETING, [], ARTIFACTS)
    decided = next(t for t in triples if t["relation"] == "decided_by")
    assert decided["source_quote"] == "Then we're moving the release to April 15th"
    assert decided["confidence"] == 1.0


def test_every_triple_is_valid_in_the_ontology():
    """Written by code, and still held to the same domain/range as anything a
    model produced."""
    for t in gr.record_triples(MEETING, ["Sarah"], ARTIFACTS):
        assert is_valid_triple(t["subject_type"], t["relation"], t["object_type"]), t


def test_a_reversed_decision_is_not_recorded_as_current():
    """The graph holds what the meeting ended on; the reversal stays in the
    artifacts table, where `superseded_by` records it."""
    reversed_ = dict(ARTIFACTS[0], superseded_by="another-id")
    assert gr.record_triples(MEETING, [], [reversed_]) == []


def test_an_item_with_no_owner_is_still_recorded():
    orphan = {"kind": "decision", "statement": "Freeze the scope", "owner": None,
              "quote": "let's freeze the scope"}
    edges = _edges(gr.record_triples(MEETING, [], [orphan]))
    assert edges == {("Freeze the scope", "discussed_in", MEETING)}


def test_a_recurring_meeting_is_one_node_per_occurrence():
    assert gr.meeting_name("Standup", "2026-09-01T09:00") == "Standup (2026-09-01)"
    assert gr.meeting_name("Standup", "2026-09-08") != gr.meeting_name("Standup", "2026-09-01")
    assert gr.meeting_name("Standup", None) == "Standup"


def test_it_accepts_artifact_objects_as_well_as_rows():
    obj = Artifact("decision", "Ship it", owner="Sarah", quote="ship it")
    assert ("Ship it", "decided_by", "Sarah") in _edges(gr.record_triples(MEETING, [], [obj]))


# -- through ingestion -------------------------------------------------------

TRANSCRIPT = """\
Sarah: Then we're moving the release to April 15th.
Mei: I'll update the roadmap by Friday.
"""


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "record.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()
    s = IngestStore(workspace="default")
    s.init_schema()
    yield s
    reset_store()


@pytest.fixture
def understood(monkeypatch):
    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index, summary="Release moved.",
            participants=["Sarah", "Mei"], topics=["release"],
            artifacts=[
                Artifact("decision", "The release moves to April 15th", owner="Sarah",
                         quote="Then we're moving the release to April 15th",
                         chunk_index=chunk.index, speakers=chunk.speakers),
                Artifact("action_item", "Update the roadmap", owner="Mei",
                         quote="I'll update the roadmap by Friday",
                         chunk_index=chunk.index, speakers=chunk.speakers),
            ],
        )
    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)


def test_ingestion_writes_the_record_into_the_graph(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT,
                                             occurred_at="2026-09-01"))
    report = assemble.process_transcript(tid, store=store)

    note = db.get_note(gr.record_note_id(tid))
    assert note["source"] == gr.SOURCE
    assert note["extraction_status"] == "done"
    edges = _edges(t for t in db.get_all_triples()
                   if t["source_note_id"] == gr.record_note_id(tid))
    meeting = "Release planning (2026-09-01)"
    assert ("The release moves to April 15th", "decided_by", "Sarah") in edges
    assert ("Update the roadmap", "assigned_to", "Mei") in edges
    assert ("Sarah", "attended", meeting) in edges
    assert report["graph_record"] == len(edges)


def test_the_record_body_is_readable_and_carries_the_quotes(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)
    body = db.get_note(gr.record_note_id(tid))["content"]
    assert "decided by Sarah" in body
    assert "assigned to Mei" in body
    assert "I'll update the roadmap by Friday" in body


def test_extraction_never_reads_the_record(store, understood, monkeypatch):
    """
    Not even a FULL re-extraction, which re-marks every note pending. Reading
    it would replace every deterministic, owner-tied edge with a model's guess
    at the same prose -- the lossy path the record exists to avoid.
    """
    import brahmastra.extraction as extraction

    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    read = []
    monkeypatch.setattr(extraction, "_extract_with_llm",
                        lambda title, content: read.append(content) or [])
    extraction.run_extraction(full=True)

    # CONTENT, not titles: the record's title is the meeting name, which is
    # also the chunk note's title, so a title check could never fail.
    assert read, "the stub never ran -- this test would pass vacuously"
    assert not any(c.startswith("Meeting record:") for c in read)
    record_edges = [t for t in db.get_all_triples()
                    if t["source_note_id"] == gr.record_note_id(tid)]
    assert any(t["relation"] == "decided_by" for t in record_edges)


def test_an_unchanged_meeting_does_not_rewrite_its_record(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)
    first = [t["extracted_at"] for t in db.get_all_triples()
             if t["source_note_id"] == gr.record_note_id(tid)]

    assemble.process_transcript(tid, store=store)
    again = [t["extracted_at"] for t in db.get_all_triples()
             if t["source_note_id"] == gr.record_note_id(tid)]
    assert again == first
