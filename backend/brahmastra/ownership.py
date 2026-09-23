"""
Every derived row knows which source item owns it -- and dies with it.

THE FAILURE THIS EXISTS FOR, measured rather than imagined. Ingest a 40-turn
transcript: it segments into 19 chunks and writes 19 notes into the graph. Edit
the transcript down to 4 turns and re-ingest: it now segments into 1 chunk and
writes 1 note.

    chunks in the table   19 -> 1     (clear_derived deleted them)
    artifacts             19 -> 1     (clear_derived deleted them)
    notes in the graph    19 -> 19    (nothing deleted them)

Eighteen notes survived, still holding triples, still answering searches, still
feeding entities -- sourced from sentences that no longer exist anywhere. The
reason is not carelessness: `clear_derived` deletes from the two tables it
happens to know about, and the notes live in a different store written through
a different module. Nothing recorded that the transcript OWNED them, so nothing
could clean them up.

That is the general shape of the problem. Every new kind of source -- a
transcript, a code file, a dropped PDF -- invents its own cleanup, each one
knows about a different subset of what it wrote, and they disagree silently. A
knowledge base serving several use cases from one brain cannot be built on that.

BORROWED FROM COCOINDEX, and this is the piece worth taking. There, every unit
of work has a component path, and that path OWNS the target states it declares.
When the source item disappears the path is no longer mounted, and the engine
deletes what it owned -- recursively, without the pipeline containing a single
line of cleanup code. The user-facing contract is a table with three columns:

    on first declaration | when declared differently | when no longer declared
    insert the row       | update the row            | delete the row

This module is that table, for this system.

WHAT IS STORED, and what deliberately is not. The ledger holds a FINGERPRINT
per derived row, never the row itself. That is what makes "declared identically
to last time" answerable without reading the target, and it is what makes the
third column possible at all: a key present in the ledger and absent from this
run's declaration is an orphan, by definition, with no need to ask the target
what it contains.

NO ROLLBACK. ROLL FORWARD.
--------------------------
A run interrupted halfway must not leave a hole, and must not need a
transaction spanning two databases to avoid one. So a sync is three phases:

    1. INTEND   record the fingerprint we are about to write, as `pending`,
                keeping the confirmed one. Both are now possible.
    2. APPLY    do the idempotent write or delete.
    3. CONFIRM  collapse to one fingerprint; a delete drops its ledger row.

Crash after 1, before 2: the next run sees two possible fingerprints for that
key, cannot prove the target matches either, and redoes the write. Crash after
2, before 3: identical, and the write is idempotent, so redoing it costs one
upsert and changes nothing. Convergence, in both directions, without anything
being undone -- which is cocoindex's stance too, and the reason its
`prev_possible_records` is a COLLECTION rather than a record.

LOSING THE LEDGER IS NOT LOSING DATA -- IT IS LOSING CLEANUP.
Every fingerprint here is recomputable by re-declaring, so this table is
DERIVED and never migrated. But an empty ledger does not mean "nothing was
written"; it means "nothing is known to have been written", and the orphans
from before it was lost stay orphans. That is the same trade cocoindex makes,
and it is why the ledger lives beside the notes rather than in a temp file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from brahmastra.sidecar import SidecarStore

# Bump to force every owner to re-declare from scratch. Only a change to what a
# fingerprint MEANS needs it -- not a change to what is done with one.
LEDGER_VERSION = "1"

TABLE = "derived_ownership"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS derived_ownership (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    owner_kind   TEXT NOT NULL,
    owner_id     TEXT NOT NULL,
    target_kind  TEXT NOT NULL,
    target_key   TEXT NOT NULL,
    confirmed    TEXT,
    pending      TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, owner_kind, owner_id, target_kind, target_key)
);

CREATE INDEX IF NOT EXISTS idx_ownership_owner
    ON derived_ownership (workspace_id, owner_kind, owner_id);
"""

# A tombstone: "we intend this key to be gone". Distinct from NULL, which means
# "no intent recorded", and from a fingerprint, which means "we intend this
# content". Empty rather than a sentinel word so it can never collide with a
# real digest, which is always 64 hex characters.
GONE = ""


def fingerprint(*parts: Any) -> str:
    """
    A digest of everything that would change the derived row.

    Separated by a NUL byte so ("ab", "c") and ("a", "bc") cannot collide --
    the same reason memo.key_for does it. None is distinguished from the empty
    string, because a row whose owner went from "" to unset genuinely changed.
    """
    h = hashlib.sha256()
    h.update(LEDGER_VERSION.encode("utf-8"))
    h.update(bytes([0]))
    for part in parts:
        if part is None:
            h.update(bytes([1]))
        else:
            h.update(str(part).encode("utf-8", "replace"))
        h.update(bytes([0]))
    return h.hexdigest()


@dataclass(frozen=True)
class Declared:
    """One derived row this run says should exist."""

    key: str
    fingerprint: str
    payload: Any = None


@dataclass(frozen=True)
class Record:
    """
    What the ledger remembers about one key.

    Two fingerprints rather than one, because an interrupted run leaves the
    target in a state we cannot name. `possible` is what that uncertainty looks
    like to a caller.
    """

    confirmed: str | None = None
    pending: str | None = None

    @property
    def possible(self) -> frozenset[str]:
        """
        Every state the target might actually be in.

        More than one member means an update was interrupted, and the only safe
        conclusion is that we do not know -- so the row is rewritten.

        ABSENCE IS A STATE, and leaving it out was a real bug caught by a test
        rather than by reading the code. A row with no `confirmed` and a
        `pending` fingerprint is a FIRST write that never finished: the only
        state it has ever been confirmed in is "not there". Treating that as
        the single possibility {fingerprint} made the next run call it
        unchanged and skip it -- so the one case the two-phase protocol exists
        to survive was the one case it silently lost.
        """
        # GONE doubles as the marker for absent: it is the empty string, and a
        # fingerprint is always 64 hex characters, so the two cannot collide.
        states = {self.confirmed if self.confirmed is not None else GONE}
        if self.pending is not None:
            states.add(self.pending)
        return frozenset(states)


@dataclass
class Plan:
    """What a sync decided, before any of it happened."""

    upserts: list[Declared] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.upserts or self.deletes)

    def summary(self) -> dict[str, int]:
        return {"written": len(self.upserts), "deleted": len(self.deletes),
                "unchanged": len(self.unchanged)}


def plan(declared: Iterable[Declared], stored: dict[str, Record],
         force: bool = False) -> Plan:
    """
    Declared against remembered. PURE -- no I/O, nothing written, nothing read.

    Kept separate from `sync` on purpose, and cocoindex draws the same line by
    requiring `reconcile()` to be non-blocking: deciding what should happen is
    the part with all the edge cases and none of the infrastructure, so it is
    the part that must be testable without a database.

    The outcomes are the three columns of the contract:

        in declared, not in stored                 -> write   (first declaration)
        in both, and the stored state is certainly
            this fingerprint                       -> nothing (unchanged)
        in both, otherwise                         -> write   (changed, OR an
                                                       interrupted run left the
                                                       target unidentifiable)
        in stored, not in declared                 -> delete  (orphan)

    `force` answers "unchanged since when?" with "since never". The ledger can
    only say what THIS system last wrote; it cannot see a row deleted out from
    under it by hand, and cocoindex names the same gap `prev_may_be_missing`.
    Both halves of that risk are small here because the ledger lives in the
    same database as the rows it tracks, so they are lost together -- but a
    rebuild has to be expressible, and this is it.
    """
    out = Plan()
    seen: set[str] = set()
    for item in declared:
        seen.add(item.key)
        record = stored.get(item.key)
        if (not force and record is not None
                and record.possible == frozenset({item.fingerprint})):
            out.unchanged.append(item.key)
        else:
            out.upserts.append(item)
    for key in stored:
        if key not in seen:
            out.deletes.append(key)
    out.deletes.sort()
    return out


# -- storage ----------------------------------------------------------------
#
# Bookkeeping about the notes, so it lives in whichever database holds them and
# stays off the GraphStore contract -- adding it there would oblige Neo4j to
# store ownership records about rows it does not have. The connection code is
# shared with every other table like this one; see brahmastra/sidecar.py.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger(SidecarStore):
    """One workspace's ownership records. Bound at construction, never filtered."""

    SCHEMA = _SCHEMA

    def describe(self) -> str:
        return f"ownership:{self.backend}#{self.workspace}"

    # -- reading -----------------------------------------------------------

    def read(self, owner_kind: str, owner_id: str,
             target_kind: str) -> dict[str, Record]:
        """Everything this owner is known to have written of one kind."""
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT target_key, confirmed, pending FROM derived_ownership "
                "WHERE workspace_id = ? AND owner_kind = ? AND owner_id = ? "
                "AND target_kind = ?"),
                (self.workspace, owner_kind, owner_id, target_kind))
            rows = cur.fetchall()
        out: dict[str, Record] = {}
        for row in rows:
            data = self._dict(row)
            out[data["target_key"]] = Record(confirmed=data["confirmed"],
                                             pending=data["pending"])
        return out

    def target_kinds(self, owner_kind: str, owner_id: str) -> list[str]:
        """
        Which kinds of thing this owner ever wrote.

        Needed to clean up an owner that is gone entirely: the code that
        declared its notes may no longer run, so the only record of what kinds
        existed is this one. cocoindex has the same property -- an unmounted
        path is cleaned up without the mounting code executing.
        """
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "SELECT DISTINCT target_kind FROM derived_ownership "
                "WHERE workspace_id = ? AND owner_kind = ? AND owner_id = ?"),
                (self.workspace, owner_kind, owner_id))
            rows = cur.fetchall()
        return sorted(self._dict(r)["target_kind"] for r in rows)

    # -- writing -----------------------------------------------------------

    def intend(self, owner_kind: str, owner_id: str, target_kind: str,
               intents: Sequence[tuple[str, str]]) -> None:
        """
        Phase 1. Record what we are ABOUT to do, keeping what we know.

        `intents` is (target_key, fingerprint), where GONE means "about to be
        deleted". The confirmed column is deliberately untouched: until phase 3
        says otherwise, BOTH fingerprints describe a state the target might be
        in, and `Record.possible` is what makes the next run redo the write
        rather than trusting either.
        """
        if not intents:
            return
        self.init_schema()
        rows = [(self.workspace, owner_kind, owner_id, target_kind, key,
                 fp, _now()) for key, fp in intents]
        # Spelled the same either way: both engines take ON CONFLICT ... DO
        # UPDATE, and both spell the proposed row `excluded`.
        with self._cursor() as cur:
            cur.executemany(self._ph(
                "INSERT INTO derived_ownership (workspace_id, owner_kind, "
                "owner_id, target_kind, target_key, pending, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, owner_kind, owner_id, "
                "target_kind, target_key) DO UPDATE "
                "SET pending = excluded.pending, "
                "    updated_at = excluded.updated_at"), rows)

    def confirm(self, owner_kind: str, owner_id: str, target_kind: str,
                written: Sequence[str], removed: Sequence[str]) -> None:
        """
        Phase 3. Collapse each key to one known state.

        A written key becomes certain: confirmed = pending, pending cleared. A
        removed key loses its row entirely -- the ledger says what EXISTS, and
        keeping tombstones would make it grow forever with the history of
        things nobody can reference any more.
        """
        self.init_schema()
        with self._cursor() as cur:
            if written:
                cur.executemany(self._ph(
                    "UPDATE derived_ownership SET confirmed = pending, "
                    "pending = NULL, updated_at = ? "
                    "WHERE workspace_id = ? AND owner_kind = ? AND owner_id = ? "
                    "AND target_kind = ? AND target_key = ?"),
                    [(_now(), self.workspace, owner_kind, owner_id,
                      target_kind, key) for key in written])
            if removed:
                cur.executemany(self._ph(
                    "DELETE FROM derived_ownership "
                    "WHERE workspace_id = ? AND owner_kind = ? AND owner_id = ? "
                    "AND target_kind = ? AND target_key = ?"),
                    [(self.workspace, owner_kind, owner_id, target_kind, key)
                     for key in removed])

    def forget_owner(self, owner_kind: str, owner_id: str) -> None:
        """Drop every record for an owner, across all target kinds."""
        self.init_schema()
        with self._cursor() as cur:
            cur.execute(self._ph(
                "DELETE FROM derived_ownership WHERE workspace_id = ? "
                "AND owner_kind = ? AND owner_id = ?"),
                (self.workspace, owner_kind, owner_id))

    def count(self, owner_kind: str | None = None) -> int:
        """Reporting only. Returns 0 if the ledger cannot be reached."""
        try:
            self.init_schema()
            sql = "SELECT count(*) AS n FROM derived_ownership WHERE workspace_id = ?"
            params: list[Any] = [self.workspace]
            if owner_kind is not None:
                sql += " AND owner_kind = ?"
                params.append(owner_kind)
            with self._cursor() as cur:
                cur.execute(self._ph(sql), tuple(params))
                row = cur.fetchone()
            return int(self._dict(row)["n"])
        except Exception:
            return 0


# -- the sync ---------------------------------------------------------------


def sync(
    ledger: Ledger,
    owner_kind: str,
    owner_id: str,
    target_kind: str,
    declared: Iterable[Declared],
    write: Callable[[Sequence[Declared]], None],
    delete: Callable[[Sequence[str]], None],
    force: bool = False,
) -> Plan:
    """
    Make the target match what this owner declares, and nothing more.

    `write` and `delete` MUST be idempotent. Both can be called again after an
    interrupted run, and the whole design rests on that costing an identical
    upsert rather than a duplicate or an error -- the same requirement
    cocoindex places on its action sinks, for the same reason.

    Raises whatever `write` or `delete` raises, having already recorded the
    intent. That is deliberate: a failure mid-sync leaves the ledger saying
    "this key is uncertain", which is exactly true, and the next run resolves it.
    """
    items = list(declared)
    stored = ledger.read(owner_kind, owner_id, target_kind)
    decided = plan(items, stored, force=force)
    if not decided:
        return decided

    # 1. INTEND
    ledger.intend(owner_kind, owner_id, target_kind,
                  [(d.key, d.fingerprint) for d in decided.upserts]
                  + [(key, GONE) for key in decided.deletes])

    # 2. APPLY. Deletions first, so a key that a re-segmentation moved from one
    #    kind to another cannot be removed a moment after being written.
    if decided.deletes:
        delete(decided.deletes)
    if decided.upserts:
        write(decided.upserts)

    # 3. CONFIRM
    ledger.confirm(owner_kind, owner_id, target_kind,
                   [d.key for d in decided.upserts], decided.deletes)
    return decided


class Streaming:
    """
    A sync for a producer that writes as it goes.

    `sync` suits a caller that can name everything it declares before writing
    any of it. Ingestion cannot: each note costs an LLM call, and a run that
    dies at chunk 35 of 40 must keep the 35 notes it already has. Holding them
    all until the end to reconcile in one shot would trade a real failure mode
    for a tidier API.

    So the phases are the same three, taken one key at a time:

        begin(key, fp)  records the intent, and answers whether a write is
                        needed at all -- False means the ledger can prove the
                        target already holds exactly this.
        commit(key)     the write landed; collapse the key to one state.
        finish(delete)  everything this owner used to have and did not declare
                        this time is an orphan. Delete it.

    `finish` MUST be reached for the deletions to happen, which is the one
    thing this shape gives up next to `sync`: a run that dies mid-document
    leaves last run's extra rows in place for the next run to remove. They were
    there before, so nothing is made worse, and the alternative -- deleting
    them up front -- is the delete-all-then-rewrite this module exists to end.
    """

    def __init__(self, ledger: Ledger, owner_kind: str, owner_id: str,
                 target_kind: str, force: bool = False) -> None:
        self.ledger = ledger
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.target_kind = target_kind
        self.force = force
        # Read ONCE. Every answer below comes from this snapshot, so the
        # decisions a run makes cannot drift under it while the run is going.
        self.stored = ledger.read(owner_kind, owner_id, target_kind)
        self.declared: list[Declared] = []
        self.written: list[str] = []
        self.unchanged: list[str] = []

    def begin(self, key: str, fp: str, payload: Any = None) -> bool:
        """Declare a row. True if it must be written, False if provably current."""
        self.declared.append(Declared(key, fp, payload))
        record = self.stored.get(key)
        if (not self.force and record is not None
                and record.possible == frozenset({fp})):
            self.unchanged.append(key)
            return False
        self.ledger.intend(self.owner_kind, self.owner_id, self.target_kind,
                           [(key, fp)])
        return True

    def commit(self, key: str) -> None:
        """The write landed."""
        self.written.append(key)
        self.ledger.confirm(self.owner_kind, self.owner_id, self.target_kind,
                            [key], [])

    def finish(self, delete: Callable[[Sequence[str]], None]) -> Plan:
        """Remove what this owner no longer declares, and report the whole run."""
        orphans = plan(self.declared, self.stored).deletes
        if orphans:
            self.ledger.intend(self.owner_kind, self.owner_id, self.target_kind,
                               [(key, GONE) for key in orphans])
            delete(orphans)
            self.ledger.confirm(self.owner_kind, self.owner_id,
                                self.target_kind, [], orphans)
        return Plan(
            upserts=[d for d in self.declared if d.key in set(self.written)],
            deletes=orphans,
            unchanged=list(self.unchanged),
        )


def drop_owner(
    ledger: Ledger,
    owner_kind: str,
    owner_id: str,
    deleters: dict[str, Callable[[Sequence[str]], None]],
) -> dict[str, int]:
    """
    The source item is gone. Remove everything it owned.

    Declaring nothing would do the same, but only for the kinds the caller
    still knows about -- and a source that no longer exists has no code left to
    enumerate them. So the KINDS come from the ledger too. A kind with no
    deleter is left alone and reported as zero, rather than being dropped from
    the ledger: forgetting we own something is how the orphans got there.
    """
    removed: dict[str, int] = {}
    for kind in ledger.target_kinds(owner_kind, owner_id):
        deleter = deleters.get(kind)
        if deleter is None:
            removed[kind] = 0
            continue
        keys = sorted(ledger.read(owner_kind, owner_id, kind))
        if keys:
            ledger.intend(owner_kind, owner_id, kind, [(k, GONE) for k in keys])
            deleter(keys)
            ledger.confirm(owner_kind, owner_id, kind, [], keys)
        removed[kind] = len(keys)
    return removed
