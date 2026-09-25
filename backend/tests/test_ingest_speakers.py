"""
Diarized transcripts ("Speaker A:", "SPEAKER_01:") get their voices named
before comprehension -- or stay "(Speaker A)" and are attributed to nobody.
Measured: on a diarized standup, 3 of 4 voices named correctly, twice; on the
two labelled meetings with their names removed, the one voice the text
identifies was named and none wrongly, the absent-but-mentioned never assigned.
"""
from __future__ import annotations

import json

import pytest

from brahmastra.ingest import speakers
from brahmastra.ingest.segment import parse_turns

STANDUP = """SPEAKER_00: Morning all, it's Sarah. Quick standup today.
SPEAKER_01: Morning.
SPEAKER_00: Raj, anything blocking you?
SPEAKER_01: Not really. The reconciliation job is nearly done, I need a review.
SPEAKER_02: I can review it this afternoon.
SPEAKER_00: Thanks Mei. And is Priya back yet?
SPEAKER_02: No, she's out until the 20th.
SPEAKER_03: Sorry I'm late. I'm not sure we covered the release date?"""


def _model(answers):
    """A stand-in model that answers with the given label -> (name, evidence)."""
    def chat(system, user, **kw):
        return json.dumps({"speakers": [
            {"label": lab, "name": name, "evidence": ev} for lab, (name, ev) in answers.items()]})
    return chat


@pytest.mark.parametrize("label", ["Speaker A", "SPEAKER_01", "Speaker 2", "spk_3",
                                   "(Speaker B)", "Unknown Speaker"])
def test_diarizer_labels_are_anonymous(label):
    assert speakers.is_anonymous(label)


@pytest.mark.parametrize("label", ["Sarah", "Mei", "Raj Patel", "Speakerman", None])
def test_names_are_not(label):
    assert not speakers.is_anonymous(label)


def test_a_self_introduction_needs_no_model():
    mapping, report = speakers.identify(parse_turns(STANDUP), use_model=False)
    assert mapping["SPEAKER_00"] == "Sarah"
    assert report["by_introduction"] == 1


def test_im_not_sure_is_not_a_name():
    mapping, _ = speakers.identify(parse_turns(STANDUP), use_model=False)
    assert mapping["SPEAKER_03"] is None


def test_the_model_fills_in_what_is_grounded():
    chat = _model({"SPEAKER_01": ("Raj", "Raj, anything blocking you?"),
                   "SPEAKER_02": ("Mei", "Thanks Mei."), "SPEAKER_03": (None, "")})
    mapping, report = speakers.identify(parse_turns(STANDUP), chat=chat)
    assert mapping == {"SPEAKER_00": "Sarah", "SPEAKER_01": "Raj",
                       "SPEAKER_02": "Mei", "SPEAKER_03": None}
    assert report["by_model"] == 2


def test_a_name_not_in_the_transcript_is_refused():
    chat = _model({"SPEAKER_03": ("Deepa", "Sorry I'm late.")})
    mapping, _ = speakers.identify(parse_turns(STANDUP), chat=chat)
    assert mapping["SPEAKER_03"] is None


def test_evidence_that_was_not_said_is_refused():
    chat = _model({"SPEAKER_03": ("Priya", "Priya here, sorry I'm late")})
    mapping, _ = speakers.identify(parse_turns(STANDUP), chat=chat)
    assert mapping["SPEAKER_03"] is None


def test_two_voices_never_become_one_person():
    chat = _model({"SPEAKER_01": ("Mei", "Thanks Mei."), "SPEAKER_02": ("Mei", "Thanks Mei.")})
    mapping, report = speakers.identify(parse_turns(STANDUP), chat=chat)
    assert mapping["SPEAKER_01"] is None and mapping["SPEAKER_02"] is None
    assert report["rejected"] == 2


def test_a_model_outage_leaves_voices_unnamed_not_the_run_failed():
    def down(*a, **k):
        raise RuntimeError("quota")
    mapping, report = speakers.identify(parse_turns(STANDUP), chat=down)
    assert mapping["SPEAKER_01"] is None and "model_error" in report


def test_named_transcripts_never_reach_the_model():
    def never(*a, **k):
        raise AssertionError("asked")
    mapping, report = speakers.identify(parse_turns("Mei: hi\nRaj: hello"), chat=never)
    assert mapping == {} and report["anonymous"] == 0


def test_unnamed_voices_are_shown_as_such():
    turns = speakers.apply(parse_turns(STANDUP), {"SPEAKER_03": None, "SPEAKER_00": "Sarah"})
    assert {t.speaker for t in turns} >= {"Sarah", "(SPEAKER_03)"}


# -- an unnamed voice owns nothing in the graph --------------------------------

def test_an_unnamed_voice_is_never_an_owner_or_attendee():
    from brahmastra.ingest import graph_record as gr
    triples = gr.record_triples("Standup", ["Sarah", "(Speaker D)"], [
        {"kind": "action_item", "statement": "Review the job", "owner": "(Speaker D)",
         "quote": "I can review it"}])
    edges = {(t["subject_text"], t["relation"], t["object_text"]) for t in triples}
    assert ("Sarah", "attended", "Standup") in edges
    assert not any("Speaker" in s or "Speaker" in o for s, _, o in edges)


def test_first_person_from_an_unnamed_voice_is_attributed_to_nobody():
    from brahmastra.ingest.evidence import owner_from_speaker
    chunk = type("C", (), {"turns": parse_turns("(Speaker D): I'll review it this afternoon.")})()
    assert owner_from_speaker("I'll review it this afternoon", chunk) is None


# -- through ingestion -----------------------------------------------------------

def test_ingesting_a_diarized_meeting_names_its_owners(monkeypatch, tmp_path):
    from brahmastra import db
    from brahmastra.ingest import assemble
    from brahmastra.ingest import graph_record as gr
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
    from brahmastra.ingest.store import IngestStore, Transcript
    import brahmastra.llm as llm

    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "d.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store(); db.init_db()
    store = IngestStore(workspace="default"); store.init_schema()

    monkeypatch.setattr(llm, "chat", _model({
        "SPEAKER_01": ("Raj", "Raj, anything blocking you?"),
        "SPEAKER_02": ("Mei", "Thanks Mei."), "SPEAKER_03": (None, "")}))
    seen_speakers = []

    def fake(chunk, max_tokens=None):
        seen_speakers.extend(chunk.speakers)
        return ChunkUnderstanding(chunk_index=chunk.index, summary="Standup.",
            participants=list(chunk.speakers), topics=["standup"], artifacts=[
                Artifact("action_item", "Review the reconciliation job", owner="Mei",
                         quote="I can review it this afternoon",
                         chunk_index=chunk.index, speakers=chunk.speakers)])
    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)

    tid = store.create_transcript(Transcript("", "Standup", STANDUP, occurred_at="2026-09-25"))
    report = assemble.process_transcript(tid, store=store)
    reset_store()

    assert report["speakers"]["mapping"]["SPEAKER_02"] == "Mei"
    assert "SPEAKER_00" not in seen_speakers and "Sarah" in seen_speakers
    edges = {(t["subject_text"], t["relation"], t["object_text"])
             for t in db.get_all_triples() if t["source_note_id"] == gr.record_note_id(tid)}
    assert ("Review the reconciliation job", "assigned_to", "Mei") in edges
    assert not any("SPEAKER" in s or "SPEAKER" in o for s, _, o in edges)
