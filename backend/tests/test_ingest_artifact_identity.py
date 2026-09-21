"""
An artifact keeps its identity across re-ingestions.

`save_artifacts` used to write `uuid.uuid4().hex[:12]`. Because a re-run
clears a transcript's derived rows before re-inserting them, that gave
identical artifacts brand-new ids every time: nothing outside the table could
hold a reference to a decision across two runs, and "unchanged" was
indistinguishable from "new". cocoindex names this directly -- random ids make
every reprocessing run churn its target.

So the id is DERIVED. These tests are mostly about what it is derived FROM,
because that is the part with judgement in it.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest.comprehend import Artifact
from brahmastra.ingest.store import IngestStore, Transcript, artifact_id

TRANSCRIPT = "Sarah: Let's move the release to April 15th.\nRaj: Agreed."


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "ingest.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    s = IngestStore(workspace="default")
    s.init_schema()
    return s


def decision(statement: str, chunk: int = 0) -> Artifact:
    return Artifact(kind="decision", statement=statement, chunk_index=chunk)


# -- what the id is derived from -------------------------------------------

def test_the_same_artifact_has_the_same_id_every_time():
    a = artifact_id("t1", "decision", "Move the release to April 15th")
    b = artifact_id("t1", "decision", "Move the release to April 15th")
    assert a == b


def test_a_different_statement_is_a_different_artifact():
    assert artifact_id("t1", "decision", "Move the release to April 15th") != \
           artifact_id("t1", "decision", "Move the release to April 22nd")


def test_the_same_sentence_as_a_different_kind_is_a_different_artifact():
    """A risk and a decision that read alike are two records, not one."""
    assert artifact_id("t1", "decision", "We ship in Q3") != \
           artifact_id("t1", "risk", "We ship in Q3")


def test_two_transcripts_saying_the_same_thing_keep_separate_records():
    assert artifact_id("t1", "decision", "We ship in Q3") != \
           artifact_id("t2", "decision", "We ship in Q3")


def test_re_chunking_does_not_change_what_a_transcript_said(store):
    """
    The judgement call in the key. Chunks overlap and the segmenter is free to
    change; the same decision found at chunk 3 before and chunk 4 after is the
    SAME decision. Putting chunk_index in the key would make a segmenter tweak
    rewrite the identity of everything every transcript ever said.

    The chunk stays on the ROW, as provenance -- it is just not identity.
    """
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))

    store.save_artifacts(tid, [decision("We ship in Q3", chunk=3)])
    row = store.get_artifacts(transcript_id=tid)[0]
    at_three, provenance = row["id"], row["chunk_index"]

    store.clear_derived(tid)
    store.save_artifacts(tid, [decision("We ship in Q3", chunk=4)])
    row = store.get_artifacts(transcript_id=tid)[0]

    assert row["id"] == at_three
    assert provenance == 3 and row["chunk_index"] == 4


def test_casing_and_trailing_punctuation_do_not_split_a_record():
    """Not fuzzy matching -- just refusing to call formatting a new fact."""
    assert artifact_id("t1", "decision", "Ship on April 15th") == \
           artifact_id("t1", "decision", "  ship on April 15th.  ")


def test_a_reworded_statement_is_genuinely_a_new_record():
    """
    The other half of the previous test, and the more important half: this
    normalisation must never grow into similarity matching, or one record
    would silently overwrite another.
    """
    assert artifact_id("t1", "decision", "Ship on April 15th") != \
           artifact_id("t1", "decision", "Ship the release on April 15th")


def test_exact_repeats_get_distinct_ids():
    """
    Consolidation normally folds these together, but it can be switched off
    (INGEST_CONSOLIDATE=0) -- and a derived key that collided would turn that
    switch into a primary-key violation rather than a duplicate row.
    """
    assert artifact_id("t1", "decision", "We ship in Q3", occurrence=0) != \
           artifact_id("t1", "decision", "We ship in Q3", occurrence=1)


# -- through the store ------------------------------------------------------

def test_re_ingesting_keeps_every_artifact_id(store):
    """The whole point, end to end."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    artifacts = [decision("Move the release to April 15th"),
                 decision("Cut the reporting migration")]

    store.save_artifacts(tid, artifacts)
    first = {r["id"] for r in store.get_artifacts(transcript_id=tid)}

    store.clear_derived(tid)                      # exactly what a re-run does
    store.save_artifacts(tid, [decision("Move the release to April 15th"),
                               decision("Cut the reporting migration")])
    second = {r["id"] for r in store.get_artifacts(transcript_id=tid)}

    assert first == second
    assert len(first) == 2


def test_an_edited_artifact_gets_a_new_id_and_the_old_one_is_gone(store):
    """Stability is not stickiness: a changed record must not keep an identity
    that claims it is the old one."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    store.save_artifacts(tid, [decision("Move the release to April 15th")])
    before = store.get_artifacts(transcript_id=tid)[0]["id"]

    store.clear_derived(tid)
    store.save_artifacts(tid, [decision("Move the release to April 22nd")])
    after = store.get_artifacts(transcript_id=tid)[0]["id"]

    assert before != after


def test_the_artifact_is_told_its_own_id(store):
    """
    An id nothing can observe is a primary key, not an identity. Storing one
    that the caller cannot read back would make every downstream use -- a
    supersession pointer, a citation, an API response -- go back to the table
    to ask what it had just written.
    """
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    a = decision("Move the release to April 15th")
    assert a.id is None
    store.save_artifacts(tid, [a])
    assert a.id == artifact_id(tid, "decision", "Move the release to April 15th")


def test_identical_artifacts_in_one_batch_both_survive(store):
    """A collision here would look exactly like a duplicate row, so it is
    checked rather than assumed."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    store.save_artifacts(tid, [decision("We ship in Q3"), decision("We ship in Q3")])
    assert len(store.get_artifacts(transcript_id=tid)) == 2
