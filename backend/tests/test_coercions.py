"""
The ontology's growth rule finally has evidence to grow from.

`docs/ONTOLOGY_DESIGN.md`: "Add a relation when it keeps appearing as
`unmapped_relation:` -- that is data telling you." That data was computed for
every note, returned from `extract_note()`, and discarded by `run_extraction`,
which never read the key. These tests are about it being KEPT, being replaced
when the note is, and dying with the note -- and about the report ranking the
right things first.
"""
from __future__ import annotations

import pytest

from brahmastra import coercions as co
from brahmastra import db


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "coercions.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()
    yield co.CoercionStore(workspace="default")
    reset_store()


def _row(kind="unmapped_relation", rel="decided", s="Sarah", st="person",
         o="the April date", ot="event", stored="related_to"):
    return {"kind": kind, "raw_relation": rel, "stored_relation": stored,
            "subject_type": st, "object_type": ot,
            "subject_text": s, "object_text": o}


# -- describing a coercion ---------------------------------------------------

def test_an_unmapped_relation_keeps_what_the_model_actually_wrote():
    raw = {"subject_text": "Sarah", "subject_type": "person",
           "relation": "Decided On", "object_text": "April 15th",
           "object_type": "date"}
    stored = dict(raw, relation="related_to")
    row = co.describe(raw, stored, "unmapped_relation:decided on")

    assert row["kind"] == "unmapped_relation"
    assert row["raw_relation"] == "decided on"
    assert row["stored_relation"] == "related_to"
    assert (row["subject_text"], row["object_text"]) == ("Sarah", "April 15th")


def test_a_dropped_fact_is_still_described():
    """Nothing reached the graph, so the raw triple is all there is."""
    raw = {"subject_text": "Shaan", "subject_type": "person",
           "relation": "employed_by", "object_text": "Unknown",
           "object_type": "org"}
    row = co.describe(raw, None, "placeholder_entity")
    assert row["kind"] == "placeholder_entity"
    assert row["stored_relation"] is None
    assert row["object_text"] == "Unknown"


def test_an_inverted_alias_records_the_direction_the_graph_holds():
    """'Mei manages Sarah' is stored as 'Sarah reports_to Mei'. The types must
    describe the stored triple, or the report would propose a backwards range."""
    raw = {"subject_text": "Mei", "subject_type": "person", "relation": "manages",
           "object_text": "Acme", "object_type": "org"}
    stored = {"subject_text": "Acme", "subject_type": "org",
              "relation": "reports_to", "object_text": "Mei",
              "object_type": "person"}
    row = co.describe(raw, stored, "alias:manages->reports_to")
    assert (row["subject_type"], row["object_type"]) == ("org", "person")


# -- the store ---------------------------------------------------------------

def test_a_note_s_coercions_are_kept(store):
    store.replace("n1", [_row(), _row(rel="attended")])
    assert [r["raw_relation"] for r in store.all()] == ["decided", "attended"]


def test_re_extracting_a_note_replaces_its_coercions(store):
    """Same lifecycle as the triples, which are deleted and re-inserted."""
    store.replace("n1", [_row(rel="decided"), _row(rel="attended")])
    store.replace("n1", [_row(rel="blocked by")])
    assert [r["raw_relation"] for r in store.all()] == ["blocked by"]


def test_a_note_that_no_longer_needs_coercing_loses_its_old_rows(store):
    """
    Recorded even when empty. A note the ontology now handles must stop
    citing the sentence it used to struggle with, or the report argues for a
    relation from evidence that no longer exists.
    """
    store.replace("n1", [_row()])
    store.replace("n1", [])
    assert store.all() == []


def test_other_notes_are_untouched(store):
    store.replace("n1", [_row()])
    store.replace("n2", [_row(rel="attended")])
    store.replace("n1", [])
    assert [r["note_id"] for r in store.all()] == ["n2"]


def test_deleting_a_note_forgets_its_evidence(store):
    db.upsert_note("n1", "Planning", "Sarah decided on April.", mark_pending=True)
    co.record("n1", [_row()])
    assert co.report()["total"] == 1

    db.delete_note("n1")
    assert co.report()["total"] == 0


def test_the_store_is_bound_to_one_workspace(store):
    """Isolation fails OPEN here; one workspace's vocabulary evidence must not
    argue for relations in another's ontology."""
    store.replace("n1", [_row()])
    assert co.CoercionStore(workspace="office").all() == []


def test_recording_never_raises(monkeypatch):
    """The triples are already written by then. Failing to log the evidence is
    not failing to extract."""
    def broken(workspace=None):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(co, "_store", broken)
    co.record("n1", [_row()])
    co.forget("n1")


# -- the report --------------------------------------------------------------

def test_evidence_is_ranked_by_how_many_notes_asked():
    """
    ONTOLOGY_DESIGN's test is whether the data KEEPS asking. Twenty notes each
    using a relation once outrank one long note using another twenty times.
    """
    rows = ([dict(_row(rel="loquacious"), note_id="long")] * 20
            + [dict(_row(rel="decided"), note_id=f"n{i}") for i in range(5)])
    top = co.summarise(rows)
    assert top[0]["raw_relation"] == "decided"
    assert top[0]["notes"] == 5
    assert top[1]["occurrences"] == 20


def test_the_report_says_whether_the_uses_agree_on_their_types():
    """
    A relation whose uses share one (subject, object) pair has a domain and a
    range. One whose uses scatter is probably several things wearing one verb,
    and should not become a single ontology entry.
    """
    agreeing = [dict(_row(rel="decided", st="person", ot="date"), note_id=f"a{i}")
                for i in range(4)]
    scattered = [dict(_row(rel="has", st=s, ot=o), note_id=f"b{i}")
                 for i, (s, o) in enumerate([("person", "org"), ("project", "date"),
                                             ("org", "tool"), ("tool", "person")])]
    by_rel = {c["raw_relation"]: c for c in co.summarise(agreeing + scattered)}

    assert by_rel["decided"]["type_agreement"] == 1.0
    assert by_rel["decided"]["type_pairs"][0] == {"subject": "person",
                                                  "object": "date", "count": 4}
    assert by_rel["has"]["type_agreement"] == 0.25


def test_the_report_carries_examples():
    rows = [dict(_row(s="Sarah", o="April"), note_id="n1"),
            dict(_row(s="Mei", o="the roadmap"), note_id="n2")]
    [candidate] = co.summarise(rows)
    assert ["Sarah", "decided", "April"] in candidate["examples"]


def test_the_vocabulary_report_leaves_the_drops_out(store):
    """A malformed triple is evidence about the model, not the vocabulary. The
    default report must not be swamped by it."""
    store.replace("n1", [_row(), _row(kind="low_confidence", rel="maybe")])
    assert co.report()["by_kind"] == {"unmapped_relation": 1}
    assert co.report(kinds=list(co.KINDS))["total"] == 2


# -- end to end, through extraction -----------------------------------------

def test_extraction_now_keeps_what_it_used_to_discard(store, monkeypatch):
    """The whole point. Before this, the second assertion could not be written."""
    import brahmastra.extraction as extraction

    monkeypatch.setattr(extraction, "_extract_with_llm", lambda title, content: [
        {"subject_text": "Sarah", "subject_type": "person",
         "relation": "decided", "object_text": "April 15th",
         "object_type": "date", "confidence": 0.9},
        {"subject_text": "Sarah", "subject_type": "person",
         "relation": "employed_by", "object_text": "Acme",
         "object_type": "org", "confidence": 0.9},
    ])
    db.upsert_note("n1", "Planning", "Sarah decided on April 15th.",
                   mark_pending=True)

    result = extraction.extract_note(db.get_note("n1"))
    assert any(c.startswith("unmapped_relation:decided") for c in result["coercions"])

    kept = co.report()
    assert kept["by_kind"] == {"unmapped_relation": 1}
    assert kept["candidates"][0]["raw_relation"] == "decided"
    assert kept["candidates"][0]["examples"] == [["Sarah", "decided", "April 15th"]]


def test_the_extract_stage_reports_the_count(store, monkeypatch):
    import brahmastra.extraction as extraction

    monkeypatch.setattr(extraction, "_extract_with_llm", lambda title, content: [
        {"subject_text": "Sarah", "subject_type": "person", "relation": "decided",
         "object_text": "April", "object_type": "date", "confidence": 0.9},
    ])
    db.upsert_note("n1", "Planning", "Sarah decided on April.", mark_pending=True)

    out = extraction.run_extraction()
    assert out["unmapped"] == 1
    assert out["coercions"] == 1
