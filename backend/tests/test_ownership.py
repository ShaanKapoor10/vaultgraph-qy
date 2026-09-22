"""
A derived row that nobody owns is a row nobody can delete.

The measurement this module exists for: a 40-turn transcript segments into 19
chunks and writes 19 notes. Edited down to 4 turns and re-ingested it segments
into 1 chunk and writes 1 note -- and the other 18 notes stayed in the graph,
because `clear_derived` knows about the two tables next to it and nothing knew
about the notes. See tests/test_ingest_ownership.py for that path end to end;
this file is about the mechanism on its own.

Most of these tests are about the THIRD column of the contract -- "when no
longer declared" -- because the first two happen anyway and that one only ever
happens if something recorded who owned what.
"""
from __future__ import annotations

import pytest

from brahmastra import ownership as own
from brahmastra.ownership import Declared, Record

FP1 = own.fingerprint("one")
FP2 = own.fingerprint("two")


# -- the plan: pure, and where all the edge cases are ------------------------

def test_a_first_declaration_is_written():
    decided = own.plan([Declared("a", FP1)], {})
    assert [d.key for d in decided.upserts] == ["a"]
    assert decided.deletes == []


def test_an_identical_declaration_writes_nothing():
    """
    The incrementality half. Re-running over an unchanged source must not
    rewrite the target, or every run churns rows that did not change -- and for
    a note, an upsert re-marks it pending and buys a fresh round of extraction.
    """
    decided = own.plan([Declared("a", FP1)], {"a": Record(confirmed=FP1)})
    assert decided.upserts == []
    assert decided.unchanged == ["a"]
    assert not decided


def test_a_changed_declaration_is_rewritten():
    decided = own.plan([Declared("a", FP2)], {"a": Record(confirmed=FP1)})
    assert [d.key for d in decided.upserts] == ["a"]


def test_a_row_no_longer_declared_is_deleted():
    """THE ONE THIS EXISTS FOR. 18 notes survived a re-ingestion without it."""
    decided = own.plan([Declared("a", FP1)],
                       {"a": Record(confirmed=FP1), "b": Record(confirmed=FP2)})
    assert decided.deletes == ["b"]
    assert decided.unchanged == ["a"]


def test_declaring_nothing_deletes_everything_owned():
    decided = own.plan([], {"a": Record(confirmed=FP1), "b": Record(confirmed=FP2)})
    assert decided.deletes == ["a", "b"]
    assert decided.upserts == []


def test_an_unknown_owner_deletes_nothing():
    """An empty ledger means "nothing known", never "delete the target"."""
    decided = own.plan([Declared("a", FP1)], {})
    assert decided.deletes == []


def test_force_rewrites_what_is_already_correct():
    decided = own.plan([Declared("a", FP1)], {"a": Record(confirmed=FP1)},
                       force=True)
    assert [d.key for d in decided.upserts] == ["a"]
    assert decided.unchanged == []


# -- an interrupted run ------------------------------------------------------

def test_an_interrupted_write_is_redone_not_trusted():
    """
    Crash between recording the intent and doing the write. Two fingerprints
    are possible, the target matches we-cannot-say-which, so it is rewritten.
    This is why the record holds a SET of possible states rather than one --
    cocoindex's `prev_possible_records` is a Collection for the same reason.
    """
    interrupted = Record(confirmed=FP1, pending=FP2)
    assert interrupted.possible == frozenset({FP1, FP2})

    decided = own.plan([Declared("a", FP2)], {"a": interrupted})
    assert [d.key for d in decided.upserts] == ["a"]

    # ...and re-declaring the OLD content is equally uncertain, so equally
    # rewritten. There is no fingerprint that lets this row be skipped.
    decided = own.plan([Declared("a", FP1)], {"a": interrupted})
    assert [d.key for d in decided.upserts] == ["a"]


def test_a_first_write_that_never_confirmed_is_redone():
    """
    Nothing confirmed, an intent recorded: the row may or may not exist.

    ABSENCE IS A STATE, and this test is here because leaving it out was a real
    bug. Counting only the fingerprints present made this record's possible
    states {FP1} -- a single certainty -- so a re-run called the row unchanged
    and skipped the write. The one case the two-phase protocol exists to
    survive was the one case it silently lost.
    """
    interrupted = Record(pending=FP1)
    assert len(interrupted.possible) == 2, "absent must count as a possibility"

    decided = own.plan([Declared("a", FP1)], {"a": interrupted})
    assert [d.key for d in decided.upserts] == ["a"]


def test_an_interrupted_delete_is_redone():
    """A tombstone is not a fingerprint, so the key can never look unchanged."""
    decided = own.plan([Declared("a", FP1)], {"a": Record(confirmed=FP1,
                                                          pending=own.GONE)})
    assert [d.key for d in decided.upserts] == ["a"]

    # And when it is genuinely gone from the declaration, it is deleted again.
    decided = own.plan([], {"a": Record(confirmed=FP1, pending=own.GONE)})
    assert decided.deletes == ["a"]


# -- fingerprints ------------------------------------------------------------

def test_the_same_content_fingerprints_alike():
    assert own.fingerprint("title", "body") == own.fingerprint("title", "body")


def test_the_fingerprint_cannot_be_confused_by_concatenation():
    assert own.fingerprint("ab", "c") != own.fingerprint("a", "bc")


def test_an_absent_field_differs_from_an_empty_one():
    """A note whose owner went from "" to unset genuinely changed."""
    assert own.fingerprint("a", None) != own.fingerprint("a", "")


# -- the sync, against a real ledger ----------------------------------------


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "own.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store()
    yield own.Ledger(workspace="default")
    reset_store()


class Target:
    """A target that records what was done to it."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.writes: list[str] = []
        self.deletes: list[str] = []

    def write(self, items):
        for item in items:
            self.rows[item.key] = item.payload
            self.writes.append(item.key)

    def delete(self, keys):
        for key in keys:
            self.rows.pop(key, None)
            self.deletes.append(key)


def _declare(*pairs):
    return [Declared(key, own.fingerprint(body), body) for key, body in pairs]


def test_a_shorter_second_run_removes_what_it_no_longer_declares(ledger):
    """The ingestion failure, in miniature."""
    target = Target()
    own.sync(ledger, "transcript", "t1", "note",
             _declare(("c0", "first"), ("c1", "second"), ("c2", "third")),
             target.write, target.delete)
    assert sorted(target.rows) == ["c0", "c1", "c2"]

    own.sync(ledger, "transcript", "t1", "note", _declare(("c0", "first")),
             target.write, target.delete)
    assert sorted(target.rows) == ["c0"]
    assert target.deletes == ["c1", "c2"]


def test_an_unchanged_rerun_touches_nothing(ledger):
    target = Target()
    declared = _declare(("c0", "first"), ("c1", "second"))
    own.sync(ledger, "transcript", "t1", "note", declared,
             target.write, target.delete)
    target.writes.clear()

    decided = own.sync(ledger, "transcript", "t1", "note", declared,
                       target.write, target.delete)
    assert target.writes == [] and target.deletes == []
    assert decided.summary() == {"written": 0, "deleted": 0, "unchanged": 2}


def test_only_the_row_that_changed_is_rewritten(ledger):
    target = Target()
    own.sync(ledger, "transcript", "t1", "note",
             _declare(("c0", "first"), ("c1", "second")),
             target.write, target.delete)
    target.writes.clear()

    own.sync(ledger, "transcript", "t1", "note",
             _declare(("c0", "first"), ("c1", "SECOND, revised")),
             target.write, target.delete)
    assert target.writes == ["c1"]


def test_one_owner_does_not_delete_another_owners_rows(ledger):
    target = Target()
    own.sync(ledger, "transcript", "t1", "note", _declare(("a", "x")),
             target.write, target.delete)
    own.sync(ledger, "transcript", "t2", "note", _declare(("b", "y")),
             target.write, target.delete)

    own.sync(ledger, "transcript", "t1", "note", [], target.write, target.delete)
    assert target.deletes == ["a"]
    assert sorted(target.rows) == ["b"]


def test_kinds_do_not_delete_each_other(ledger):
    target = Target()
    own.sync(ledger, "transcript", "t1", "note", _declare(("k", "note body")),
             target.write, target.delete)
    own.sync(ledger, "transcript", "t1", "artifact", _declare(("k", "artifact")),
             target.write, target.delete)

    own.sync(ledger, "transcript", "t1", "note", [], target.write, target.delete)
    assert ledger.target_kinds("transcript", "t1") == ["artifact"]


def test_a_failed_write_leaves_the_row_marked_uncertain(ledger):
    """
    The crash-safety property, exercised rather than argued. The intent is
    recorded BEFORE the write, so a write that dies leaves a record whose two
    possible fingerprints force the next run to do it again.
    """
    target = Target()

    def explode(items):
        raise RuntimeError("the database went away")

    with pytest.raises(RuntimeError):
        own.sync(ledger, "transcript", "t1", "note", _declare(("c0", "first")),
                 explode, target.delete)

    stored = ledger.read("transcript", "t1", "note")
    assert stored["c0"].confirmed is None
    assert stored["c0"].pending == own.fingerprint("first")

    # The next run finishes the job.
    decided = own.sync(ledger, "transcript", "t1", "note",
                       _declare(("c0", "first")), target.write, target.delete)
    assert [d.key for d in decided.upserts] == ["c0"]
    assert target.rows == {"c0": "first"}
    assert ledger.read("transcript", "t1", "note")["c0"].pending is None


def test_a_failed_delete_leaves_the_orphan_owned(ledger):
    """
    A delete that fails must NOT drop the ledger row. Forgetting we own
    something is precisely how an orphan becomes permanent.
    """
    target = Target()
    own.sync(ledger, "transcript", "t1", "note",
             _declare(("c0", "first"), ("c1", "second")),
             target.write, target.delete)

    def explode(keys):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        own.sync(ledger, "transcript", "t1", "note", _declare(("c0", "first")),
                 target.write, explode)

    assert "c1" in ledger.read("transcript", "t1", "note")
    own.sync(ledger, "transcript", "t1", "note", _declare(("c0", "first")),
             target.write, target.delete)
    assert target.deletes == ["c1"]


# -- dropping an owner entirely ---------------------------------------------

def test_dropping_an_owner_removes_every_kind_it_wrote(ledger):
    """
    Nothing re-declares here, and nothing needs to: the KINDS come from the
    ledger. That is what lets a deleted source clean up after itself when the
    code that produced it never runs again.
    """
    notes, artifacts = Target(), Target()
    own.sync(ledger, "transcript", "t1", "note",
             _declare(("n0", "a"), ("n1", "b")), notes.write, notes.delete)
    own.sync(ledger, "transcript", "t1", "artifact", _declare(("a0", "c")),
             artifacts.write, artifacts.delete)

    removed = own.drop_owner(ledger, "transcript", "t1",
                             {"note": notes.delete, "artifact": artifacts.delete})
    assert removed == {"note": 2, "artifact": 1}
    assert notes.rows == {} and artifacts.rows == {}
    assert ledger.target_kinds("transcript", "t1") == []


def test_a_kind_with_no_deleter_stays_owned(ledger):
    """
    Reported as zero and LEFT IN THE LEDGER. Quietly forgetting a kind nobody
    supplied a deleter for would turn a missing argument into a permanent
    orphan, which is the bug this module was written to end.
    """
    notes = Target()
    own.sync(ledger, "transcript", "t1", "note", _declare(("n0", "a")),
             notes.write, notes.delete)
    own.sync(ledger, "transcript", "t1", "sketch", _declare(("s0", "b")),
             notes.write, notes.delete)

    removed = own.drop_owner(ledger, "transcript", "t1", {"note": notes.delete})
    assert removed == {"note": 1, "sketch": 0}
    assert ledger.target_kinds("transcript", "t1") == ["sketch"]


# -- isolation ---------------------------------------------------------------

def test_the_ledger_is_bound_to_one_workspace(ledger, monkeypatch, tmp_path):
    """
    Same discipline as every other store here: bound at construction, never
    filtered by a caller. Isolation in this project fails OPEN, and an
    ownership record leaking across workspaces would delete another graph's
    rows -- the worst possible direction for that failure.
    """
    target = Target()
    own.sync(ledger, "transcript", "t1", "note", _declare(("n0", "a")),
             target.write, target.delete)

    other = own.Ledger(workspace="office")
    assert other.read("transcript", "t1", "note") == {}
    assert other.target_kinds("transcript", "t1") == []
