"""
The keepalive has to touch the ENGINE, not whatever the facade answers with.

Neo4j Aura Free suspends an instance after roughly three days without a query.
The scheduler was already running every fifteen minutes and would not have
prevented it: its idle check is `db.get_notes(status="pending")`, a SOURCE
read, which under NOTE_BACKEND=postgres never leaves Postgres. A keepalive
written against the facade would have looked correct, logged happily, and let
the instance suspend anyway — so the test that matters is the one that proves
which half was asked.
"""
from __future__ import annotations

import time

import pytest

from brahmastra import keepalive
from brahmastra.stores.base import CAP_HYBRID_SEARCH, CAP_VECTOR_SEARCH, CAP_FULLTEXT_SEARCH
from brahmastra.stores.composite_store import CompositeStore


class FakeStore:
    """Enough of a store to be routed to, and it records being asked."""

    def __init__(self, name: str, caps=frozenset(), fail: bool = False):
        self.name = name
        self.workspace = "default"
        self.stats_calls = 0
        self._caps = caps
        self._fail = fail

    def describe(self) -> str:
        return f"fake:{self.name}"

    def capabilities(self):
        return self._caps

    def stats(self):
        self.stats_calls += 1
        if self._fail:
            raise ConnectionError("hostname does not resolve")
        return {"notes_total": 61, "triples_total": 693, "entity_clusters": 481}


def _composite():
    notes = FakeStore(
        "postgres",
        caps=frozenset({CAP_FULLTEXT_SEARCH, CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH}),
    )
    graph = FakeStore("neo4j")
    return CompositeStore(notes=notes, graph=graph), notes, graph


@pytest.fixture(autouse=True)
def _scratch_state(monkeypatch, tmp_path):
    """Never write a touch stamp into the real data directory."""
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.delenv("GRAPH_KEEPALIVE", raising=False)
    monkeypatch.delenv("GRAPH_KEEPALIVE_HOURS", raising=False)


# ---------------------------------------------------------------------------
# The thing this exists for
# ---------------------------------------------------------------------------

def test_a_split_store_is_pinged_on_its_graph_half():
    """
    The bug this whole module answers. Every method that looks like a health
    check — get_notes, search_notes, get_db_stats — is answered by the note
    store under a split arrangement, so a keepalive built on any of them keeps
    Postgres awake and lets Aura suspend.
    """
    store, notes, graph = _composite()

    keepalive.ping(store)

    assert graph.stats_calls == 1, "the engine was never queried"
    assert notes.stats_calls == 0, "the note store answered instead of the engine"


def test_a_single_store_is_pinged_directly():
    """The unsplit arrangement is still fully supported and must not be special-cased."""
    store = FakeStore("neo4j")
    keepalive.ping(store)
    assert store.stats_calls == 1


def test_graph_half_of_a_plain_store_is_itself():
    store = FakeStore("neo4j")
    assert keepalive.graph_half(store) is store


# ---------------------------------------------------------------------------
# Only when nothing else has
# ---------------------------------------------------------------------------

def test_the_first_check_pings_because_nothing_has_vouched_for_it():
    store, _, graph = _composite()
    res = keepalive.touch_if_idle(store)
    assert res["pinged"] is True
    assert graph.stats_calls == 1


def test_a_recent_touch_is_not_repeated():
    store, _, graph = _composite()
    keepalive.touch_if_idle(store)
    res = keepalive.touch_if_idle(store)

    assert res["pinged"] is False
    assert graph.stats_calls == 1, "queried a remote instance that was touched moments ago"
    assert "under the" in res["reason"]


def test_an_old_touch_is_refreshed():
    store, _, graph = _composite()
    keepalive.record_contact(store, when=time.time() - 40 * 3600)

    res = keepalive.touch_if_idle(store)
    assert res["pinged"] is True
    assert graph.stats_calls == 1
    assert "idle for" in res["reason"]


def test_real_work_counts_as_a_touch():
    """
    A pipeline run reaches the engine by definition, so the keepalive must not
    then query it again. This is what `record_contact` in run_pipeline buys.
    """
    store, _, graph = _composite()
    keepalive.record_contact(store)

    assert keepalive.touch_if_idle(store)["pinged"] is False
    assert graph.stats_calls == 0


def test_two_engines_do_not_vouch_for_each_other():
    """The stamp is per engine; touching one must not mark the other fresh."""
    one, _, _ = _composite()
    two = FakeStore("other-aura")

    keepalive.record_contact(one)
    assert keepalive.last_contact(two) is None


def test_force_ignores_the_record():
    store, _, graph = _composite()
    keepalive.record_contact(store)
    res = keepalive.touch_if_idle(store, force=True)
    assert res["pinged"] is True
    assert graph.stats_calls == 1


# ---------------------------------------------------------------------------
# Not being the reason something breaks
# ---------------------------------------------------------------------------

def test_a_suspended_instance_is_reported_not_raised():
    """
    The caller is a loop whose job is to still be running in three days. A
    keepalive that raises into it is worse than one that misses a beat.
    """
    notes = FakeStore(
        "postgres",
        caps=frozenset({CAP_FULLTEXT_SEARCH, CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH}),
    )
    store = CompositeStore(notes=notes, graph=FakeStore("neo4j", fail=True))

    res = keepalive.touch_if_idle(store)
    assert res["pinged"] is False
    assert "does not resolve" in res["error"]


def test_a_failed_ping_does_not_record_a_touch():
    """Otherwise a suspended instance would look freshly touched for 12 hours."""
    notes = FakeStore(
        "postgres",
        caps=frozenset({CAP_FULLTEXT_SEARCH, CAP_VECTOR_SEARCH, CAP_HYBRID_SEARCH}),
    )
    store = CompositeStore(notes=notes, graph=FakeStore("neo4j", fail=True))

    keepalive.touch_if_idle(store)
    assert keepalive.last_contact(store) is None


def test_a_local_backend_is_left_alone(monkeypatch):
    """A file on this disk has no notion of idleness."""
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    store = FakeStore("sqlite")
    res = keepalive.touch_if_idle(store)
    assert res["pinged"] is False
    assert store.stats_calls == 0


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("GRAPH_KEEPALIVE", "0")
    store, _, graph = _composite()
    assert keepalive.touch_if_idle(store)["pinged"] is False
    assert graph.stats_calls == 0


# ---------------------------------------------------------------------------
# The interval
# ---------------------------------------------------------------------------

def test_the_limit_is_well_inside_auras_window():
    """
    Aura Free suspends after about 72 hours. A default at or above that would
    be a keepalive that arrives after the thing it was preventing.
    """
    assert keepalive.idle_limit() <= 24 * 3600


def test_the_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("GRAPH_KEEPALIVE_HOURS", "6")
    assert keepalive.idle_limit() == 6 * 3600


def test_a_nonsense_limit_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("GRAPH_KEEPALIVE_HOURS", "soon")
    assert keepalive.idle_limit() == keepalive.DEFAULT_IDLE_HOURS * 3600


def test_a_zero_limit_does_not_become_a_query_per_tick(monkeypatch):
    """Left unfloored, GRAPH_KEEPALIVE_HOURS=0 makes every scheduler tick a
    remote round trip — the opposite of the point."""
    monkeypatch.setenv("GRAPH_KEEPALIVE_HOURS", "0")
    assert keepalive.idle_limit() >= 60


# ---------------------------------------------------------------------------
# Where the stamp lives
# ---------------------------------------------------------------------------

def test_the_stamp_can_be_moved_onto_a_shared_volume(monkeypatch, tmp_path):
    """
    In a container the package sits on the image and the shared volume is
    mounted elsewhere, so a stamp written beside the code is invisible to the
    next process — every one of them then pings independently.
    """
    shared = tmp_path / "shared"
    monkeypatch.setenv("BRAHMASTRA_DATA_DIR", str(shared))
    store, _, _ = _composite()

    keepalive.record_contact(store)
    assert keepalive._state_path(store).parent == shared
    assert keepalive.last_contact(store) is not None
