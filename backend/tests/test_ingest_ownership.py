"""
A transcript owns its notes, and a shorter re-ingestion takes them with it.

THE MEASUREMENT THAT PUT THIS HERE, run against the real path before anything
was changed:

    a 40-turn transcript    -> 19 chunks, 19 notes
    edited down to 4 turns  ->  1 chunk,  1 note, 19 notes still in the graph

    chunks in the table   19 -> 1     (clear_derived deleted them)
    artifacts             19 -> 1     (clear_derived deleted them)
    notes in the graph    19 -> 19    (nothing deleted them)

Eighteen orphans, each still holding triples, each still answering searches,
each sourced from sentences that no longer exist anywhere. Not carelessness:
`clear_derived` deletes from the two tables beside it, the notes live in
another store reached through another module, and nothing recorded that the
transcript OWNED them.
"""
from __future__ import annotations

import pytest

from brahmastra import db, ownership
from brahmastra.ingest import assemble
from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
from brahmastra.ingest.segment import segment
from brahmastra.ingest.store import IngestStore, Transcript


def talk(turns: int) -> str:
    """Long enough that the segmenter makes several chunks of it."""
    padding = ("We discussed the release at length and considered the payments "
               "integration and the Acme contract. " * 12)
    return "\n".join(
        f"{'Sarah' if i % 2 == 0 else 'Mei'}: Point number {i}. {padding}"
        for i in range(turns)
    )


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ingest.db"))
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
    """Comprehension with no provider involved, and stable per chunk."""
    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            summary=f"Part {chunk.index} of the discussion.",
            participants=["Sarah", "Mei"],
            topics=["release"],
            artifacts=[
                Artifact("decision", f"Decision number {chunk.index}",
                         owner="Sarah", chunk_index=chunk.index,
                         speakers=chunk.speakers),
            ],
        )
    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)


def _rewrite(store, transcript_id, content):
    """Edit the transcript in place, as a person editing the source would."""
    with store._cursor() as cur:
        cur.execute(store._ph(
            "UPDATE transcripts SET content = ? WHERE workspace_id = ? AND id = ?"),
            (content, store.workspace, transcript_id))


def _notes_for(transcript_id):
    return sorted(n["id"] for n in db.get_notes()
                  if n["id"].startswith(transcript_id))


# -- the bug -----------------------------------------------------------------

def test_a_shorter_re_ingestion_leaves_no_orphan_notes(store, understood):
    long_text, short_text = talk(40), talk(4)
    assert len(segment(long_text)) > len(segment(short_text)) > 0

    tid = store.create_transcript(Transcript("", "Release planning", long_text))
    first = assemble.process_transcript(tid, store=store)
    assert len(_notes_for(tid)) == first["notes"] > 1

    _rewrite(store, tid, short_text)
    second = assemble.process_transcript(tid, store=store)

    assert len(_notes_for(tid)) == second["notes"]
    assert second["notes_removed"] == first["notes"] - second["notes"]


def test_the_orphans_triples_go_with_them(store, understood):
    """
    The reason an orphan note matters. It is not one extra row; it is a set of
    entities and relations asserting things about deleted sentences.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(40)))
    assemble.process_transcript(tid, store=store)
    doomed = _notes_for(tid)[-1]
    db.insert_triples([{
        "subject_text": "Sarah", "relation": "related_to",
        "object_text": "the April release", "confidence": 0.9,
        "source_note_id": doomed,
    }])
    assert [t for t in db.get_all_triples() if t["source_note_id"] == doomed]

    _rewrite(store, tid, talk(4))
    assemble.process_transcript(tid, store=store)

    assert doomed not in _notes_for(tid)
    assert [t for t in db.get_all_triples() if t["source_note_id"] == doomed] == []


def test_emptying_a_transcript_removes_every_note(store, understood):
    """The extreme case: declaring nothing must delete everything owned."""
    tid = store.create_transcript(Transcript("", "Release planning", talk(40)))
    first = assemble.process_transcript(tid, store=store)
    assert first["notes"] > 1

    _rewrite(store, tid, "")
    second = assemble.process_transcript(tid, store=store)

    assert second["chunks"] == 0
    assert second["notes_removed"] == first["notes"]
    assert _notes_for(tid) == []


# -- and the other direction -------------------------------------------------

def test_a_longer_re_ingestion_keeps_what_it_still_declares(store, understood):
    """
    Deletion must be driven by what is DECLARED, not by "anything that looks
    old". A transcript that grew keeps every note it still produces.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(4)))
    first = assemble.process_transcript(tid, store=store)
    before = _notes_for(tid)

    _rewrite(store, tid, talk(40))
    second = assemble.process_transcript(tid, store=store)

    assert second["notes"] > first["notes"]
    assert second["notes_removed"] == 0
    assert set(before) <= set(_notes_for(tid))


def test_another_transcripts_notes_are_never_touched(store, understood):
    tid_a = store.create_transcript(Transcript("", "Planning", talk(40)))
    tid_b = store.create_transcript(Transcript("", "Retro", talk(8)))
    assemble.process_transcript(tid_a, store=store)
    assemble.process_transcript(tid_b, store=store)
    b_notes = _notes_for(tid_b)
    assert b_notes

    _rewrite(store, tid_a, "")
    assemble.process_transcript(tid_a, store=store)

    assert _notes_for(tid_b) == b_notes


# -- incrementality ----------------------------------------------------------

def test_an_unchanged_re_ingestion_rewrites_nothing(store, understood):
    """
    The other half of ownership, and the one with a cost attached. An upsert
    re-marks a note pending, and a pending note buys a fresh extraction call
    per note on a rate-limited tier. Re-running a transcript nobody edited
    should cost none of that.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    first = assemble.process_transcript(tid, store=store)
    assert first["notes_written"] == first["notes"] > 1

    # Extraction has since run over them.
    for note_id in _notes_for(tid):
        db.mark_note_done(note_id)

    second = assemble.process_transcript(tid, store=store)
    assert second["notes"] == first["notes"]
    assert second["notes_written"] == 0
    assert second["notes_removed"] == 0
    assert db.get_notes(status="pending") == []


def test_force_rewrites_notes_the_ledger_calls_current(store, understood):
    """
    The ledger knows what THIS system last wrote, not what the store holds. A
    note deleted by hand looks unchanged to it, so a rebuild has to be sayable.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    first = assemble.process_transcript(tid, store=store)

    again = assemble.process_transcript(tid, store=store, force=True)
    assert again["notes_written"] == first["notes"]


def test_only_the_chunk_that_changed_is_rewritten(store, monkeypatch):
    """
    Incrementality at the row, not at the document. Editing the end of a
    meeting must not re-extract the beginning of it.
    """
    bodies = {}

    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            summary=bodies.get(chunk.index, f"Part {chunk.index}."),
            participants=["Sarah"],
            topics=["release"],
            artifacts=[],
        )
    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)

    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    first = assemble.process_transcript(tid, store=store)
    assert first["notes"] > 2

    bodies[1] = "Part 1, and Priya has now joined the call."
    second = assemble.process_transcript(tid, store=store)
    assert second["notes_written"] == 1
    assert second["notes"] == first["notes"]


# -- chunks and artifacts, under the same rule -------------------------------

def test_a_shorter_re_ingestion_removes_chunks_and_artifacts(store, understood):
    """
    What `clear_derived` used to do by deleting everything first. The outcome
    is the same; the difference is that nothing is destroyed on the way.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(40)))
    first = assemble.process_transcript(tid, store=store)
    assert len(store.get_chunks(tid)) == first["chunks"] > 1

    _rewrite(store, tid, talk(4))
    second = assemble.process_transcript(tid, store=store)

    assert len(store.get_chunks(tid)) == second["chunks"]
    assert second["chunks_removed"] == first["chunks"] - second["chunks"]
    assert (len(store.get_artifacts(transcript_id=tid, limit=500))
            == second["artifacts"])


def test_an_interrupted_run_no_longer_empties_the_transcript(store, understood,
                                                             monkeypatch):
    """
    THE REASON clear_derived HAD TO GO. It deleted every chunk and artifact
    before the run produced their replacements, so a run that died in between
    left the transcript holding nothing -- not an older version, nothing. A
    reconciliation writes over the old rows instead, so a failed run leaves
    exactly what the last good one left.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(40)))
    first = assemble.process_transcript(tid, store=store)
    assert first["chunks"] > 1

    def die(chunk, max_tokens=None):
        raise RuntimeError("the provider went away")

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: die)
    with pytest.raises(RuntimeError):
        assemble.process_transcript(tid, store=store)

    assert len(store.get_chunks(tid)) == first["chunks"]
    assert len(store.get_artifacts(transcript_id=tid, limit=500)) \
        == first["artifacts"]


def test_an_unchanged_re_ingestion_rewrites_no_artifacts(store, understood):
    """
    An artifact's id is derived from its statement, so "already exactly this"
    is a real answer rather than a guess -- and a re-run over an unedited
    meeting stops rewriting every decision it ever recorded.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    first = assemble.process_transcript(tid, store=store)
    assert first["artifacts_written"] == first["artifacts"] > 1

    second = assemble.process_transcript(tid, store=store)
    assert second["artifacts"] == first["artifacts"]
    assert second["artifacts_written"] == 0
    assert second["artifacts_removed"] == 0


def test_an_artifact_that_changed_is_rewritten_in_place(store, monkeypatch):
    owner = ["Sarah"]

    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index, summary=f"Part {chunk.index}.",
            participants=["Sarah"], topics=["release"],
            artifacts=[Artifact("decision", "The release moves to April 15th",
                                owner=owner[0], chunk_index=chunk.index,
                                speakers=chunk.speakers)],
        )
    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)

    tid = store.create_transcript(Transcript("", "Release planning", talk(4)))
    assemble.process_transcript(tid, store=store)
    before = store.get_artifacts(transcript_id=tid, limit=50)
    assert len(before) == 1

    owner[0] = "Mei"
    report = assemble.process_transcript(tid, store=store)
    after = store.get_artifacts(transcript_id=tid, limit=50)

    # Same identity -- the statement did not change -- with a new owner.
    assert report["artifacts_written"] == 1
    assert report["artifacts_removed"] == 0
    assert len(after) == 1
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["owner"] == "Mei"


# -- deleting the transcript outright ---------------------------------------

def test_deleting_a_transcript_abandons_its_notes_by_default(store, understood):
    """
    ABANDON, the default. Deleting a transcript deletes the SOURCE, so unlike a
    re-ingestion nothing can recompute those notes afterwards -- and CLAUDE.md's
    first storage rule is that source data is not owed the treatment derived
    data gets. The claim is released all the same: a ledger holding records for
    an owner that no longer exists is a lie about what is being tracked.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    report = assemble.process_transcript(tid, store=store)

    out = assemble.drop_transcript(tid, store=store)

    assert out["notes_kept"] is True
    assert len(_notes_for(tid)) == report["notes"]
    assert store.get_transcript(tid) is None
    ledger = ownership.Ledger(workspace="default")
    assert ledger.target_kinds(assemble.OWNER_KIND, tid) == []


def test_purging_a_transcript_takes_its_notes_with_it(store, understood):
    """
    DESTROY. Coherent with every other path: shortening a transcript already
    deletes the notes it stopped declaring, so deleting the whole thing should
    not be the one case that leaves nineteen behind.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    report = assemble.process_transcript(tid, store=store)

    out = assemble.drop_transcript(tid, store=store, purge_notes=True)

    assert out["notes_removed"] == report["notes"]
    assert _notes_for(tid) == []
    ledger = ownership.Ledger(workspace="default")
    assert ledger.target_kinds(assemble.OWNER_KIND, tid) == []


def test_purging_one_transcript_spares_another(store, understood):
    tid_a = store.create_transcript(Transcript("", "Planning", talk(8)))
    tid_b = store.create_transcript(Transcript("", "Retro", talk(8)))
    assemble.process_transcript(tid_a, store=store)
    assemble.process_transcript(tid_b, store=store)
    b_notes = _notes_for(tid_b)

    assemble.drop_transcript(tid_a, store=store, purge_notes=True)
    assert _notes_for(tid_b) == b_notes


# -- the ledger itself -------------------------------------------------------

def test_the_ledger_records_what_the_transcript_owns(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", talk(8)))
    report = assemble.process_transcript(tid, store=store)

    ledger = ownership.Ledger(workspace="default")
    owned = ledger.read(assemble.OWNER_KIND, tid, "note")
    assert sorted(owned) == _notes_for(tid)
    assert len(owned) == report["notes"]
    # Every key settled: nothing left saying "an update was in flight".
    assert all(record.pending is None for record in owned.values())


def test_a_cleanup_failure_is_reported_and_not_fatal(store, understood, monkeypatch):
    """
    Reported, never raised, and never silent. A run that comprehended forty
    chunks must not be thrown away because one note would not delete -- and a
    swallowed failure is how an outage once produced four invalid measurements.
    """
    tid = store.create_transcript(Transcript("", "Release planning", talk(40)))
    assemble.process_transcript(tid, store=store)

    real_delete, refusing = db.delete_note, [True]

    def refuse(note_id):
        if refusing[0]:
            raise RuntimeError("the store said no")
        real_delete(note_id)

    monkeypatch.setattr(db, "delete_note", refuse)
    _rewrite(store, tid, talk(4))
    report = assemble.process_transcript(tid, store=store)

    assert report["status"] != "error"
    assert any(e.get("stage") == "ownership" for e in report["errors"])

    # Still owned, so the next run tries again rather than forgetting them.
    refusing[0] = False
    again = assemble.process_transcript(tid, store=store)
    assert again["notes_removed"] > 0
    assert len(_notes_for(tid)) == again["notes"]
