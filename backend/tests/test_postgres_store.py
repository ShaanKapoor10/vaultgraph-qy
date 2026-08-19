"""
PostgresStore against a real server.

Skipped when no Postgres is reachable, so the suite still runs on a machine
with nothing installed. They are integration tests on purpose: the thing worth
proving is that hybrid search SURVIVED moving the notes off Neo4j, and a mock
cannot demonstrate that -- it would assert that the SQL I wrote is the SQL I
wrote, while the actual risk is that tsvector and pgvector rank differently
from what the feature needs.

Run the server with:  docker compose --profile postgres up -d postgres
"""
from __future__ import annotations

import os
import uuid

import pytest

from brahmastra.stores.base import CAP_HYBRID_SEARCH, CAP_VECTOR_SEARCH


def _dsn() -> str | None:
    """A DSN only if something actually answers on it."""
    try:
        import psycopg
    except ImportError:
        return None

    from brahmastra.stores.postgres_store import dsn as resolve

    candidate = resolve()
    try:
        with psycopg.connect(candidate, connect_timeout=3):
            return candidate
    except Exception:
        return None


DSN = _dsn()
needs_pg = pytest.mark.skipif(DSN is None, reason="no Postgres reachable")


@pytest.fixture
def store():
    """A store in its own workspace, torn down afterwards."""
    from brahmastra.stores.postgres_store import PostgresStore

    ws = f"test-{uuid.uuid4().hex[:8]}"
    s = PostgresStore(workspace=ws, dsn_override=DSN)
    s.init_schema()
    yield s
    try:
        s.delete_workspace(ws)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The reason this store exists
# ---------------------------------------------------------------------------

@needs_pg
def test_it_declares_hybrid_search_only_when_pgvector_is_installed(store):
    """
    A stock PostgreSQL build has no pgvector. Claiming vector search without it
    would mean answering semantic queries with nothing and calling the result
    hybrid -- so the claim is checked against pg_extension, not assumed.
    """
    caps = store.capabilities()
    if store.has_vector():
        assert CAP_HYBRID_SEARCH in caps and CAP_VECTOR_SEARCH in caps
    else:
        assert CAP_HYBRID_SEARCH not in caps, (
            "without pgvector this must NOT claim hybrid search -- the composite "
            "relies on that claim to decide whether search would be downgraded"
        )


@needs_pg
def test_semantic_search_finds_a_note_sharing_no_words_with_the_query(store):
    """
    The whole point of the vector half. If this fails, the store is a lexical
    store wearing a hybrid label and CompositeStore's guard has been defeated
    by a false capability claim.
    """
    if not store.has_vector():
        pytest.skip("pgvector not installed on this server")

    store.upsert_note("n-mgr", "Team structure", "Sarah reports to Mei on the platform team.")
    store.upsert_note("n-food", "Dinner", "Boil water, add salt, drain the pasta after nine minutes.")

    # "boss" appears in neither note; only an embedding connects it to "reports to".
    assert store._fulltext_notes("who is Sarahs boss", 10) == [], (
        "precondition: the lexical half must find nothing, or this proves nothing"
    )
    assert store.search_notes("who is Sarahs boss", 1)[0]["id"] == "n-mgr"


@needs_pg
def test_lexical_search_still_finds_exact_terms(store):
    """Vectors are fuzzy; an exact identifier must still rank first."""
    store.upsert_note("n-tls", "TLS", "The driver needs an explicit certifi SSL context.")
    store.upsert_note("n-other", "Unrelated", "Cluster summaries are recomputed every run.")

    assert store._fulltext_notes("certifi", 10) == ["n-tls"]
    assert store.search_notes("certifi", 1)[0]["id"] == "n-tls"


@needs_pg
def test_an_unparseable_query_degrades_instead_of_raising(store):
    """
    The input is a natural-language question, so a stray quote or a bare AND is
    ordinary. A parse error must cost the lexical half, never the whole search.
    """
    store.upsert_note("n1", "Anything", "Some content about pipelines.")
    for hostile in ['"unclosed', "and or not", "!!!", "  "]:
        store.search_notes(hostile, 5)   # must not raise


# ---------------------------------------------------------------------------
# System-of-record semantics, matching the other backends
# ---------------------------------------------------------------------------

@needs_pg
def test_a_sync_cannot_unpublish_a_note_somebody_published(store):
    store.upsert_note("n1", "T", "C", publish=True)
    assert store.get_note("n1")["publish"] is True
    store.upsert_note("n1", "T", "C changed")          # publish not mentioned
    assert store.get_note("n1")["publish"] is True, "None must mean 'leave as is'"


@needs_pg
def test_a_known_origin_survives_a_later_sync(store):
    """
    COALESCE alone prefers the NEW value, so a Notion sync re-upserting an MCP
    note would relabel it and provenance would decay to whichever job ran last.
    """
    store.upsert_note("n1", "T", "C", source="mcp")
    store.upsert_note("n1", "T", "C", source="notion")
    assert store.get_note("n1")["source"] == "mcp"


@needs_pg
def test_an_unknown_origin_can_still_be_upgraded(store):
    store.upsert_note("n1", "T", "C")
    assert store.get_note("n1")["source"] == "unknown"
    store.upsert_note("n1", "T", "C", source="notion")
    assert store.get_note("n1")["source"] == "notion"


@needs_pg
def test_a_failed_note_records_why_and_forgets_on_success(store):
    store.upsert_note("n1", "T", "C")
    store.set_note_status("n1", "error", "429 tokens per minute")
    assert "per minute" in store.get_note("n1")["extraction_error"]

    store.set_note_status("n1", "done")
    assert store.get_note("n1")["extraction_error"] is None, (
        "a message must not outlive the failure it describes"
    )


@needs_pg
def test_bulk_fetch_returns_what_exists_and_omits_what_does_not(store):
    for i in range(3):
        store.upsert_note(f"b{i}", f"Title {i}", "c")
    found = store.get_notes_by_ids(["b0", "b2", "b2", "nope"])
    assert set(found) == {"b0", "b2"}
    assert found["b2"]["title"] == "Title 2"
    assert store.get_notes_by_ids([]) == {}


@needs_pg
def test_note_rows_carry_no_search_machinery(store):
    """
    SELECT * would drag the tsvector and a 384-float embedding into every note
    dict, where nothing wants them and JSON serialisation of the vector fails.
    """
    store.upsert_note("n1", "T", "C")
    note = store.get_note("n1")
    assert "search_vector" not in note and "embedding" not in note


@needs_pg
def test_workspaces_are_isolated(store):
    """Two workspaces may each have their own note with the same id."""
    from brahmastra.stores.postgres_store import PostgresStore

    other_ws = f"test-{uuid.uuid4().hex[:8]}"
    other = PostgresStore(workspace=other_ws, dsn_override=DSN)
    other.init_schema()
    try:
        store.upsert_note("shared-id", "Mine", "content A")
        other.upsert_note("shared-id", "Theirs", "content B")

        assert store.get_note("shared-id")["title"] == "Mine"
        assert other.get_note("shared-id")["title"] == "Theirs"
        assert len(store.get_notes()) == 1
    finally:
        other.delete_workspace(other_ws)
        other.close()


@needs_pg
def test_stats_use_the_same_key_names_as_the_other_backends(store):
    """
    They did not at first: this returned `notes` while SQLite and Neo4j return
    `notes_total`, so the composite's note-store precedence overwrote nothing
    and a merged report showed Neo4j's stale stub count beside the real one,
    both looking authoritative.
    """
    store.upsert_note("n1", "T", "C")
    assert set(store.stats()) >= {"workspace", "notes_total", "notes_pending"}


def test_the_password_is_not_in_describe():
    """
    describe() lands in logs, health payloads and the composite's own describe().

    Needs no server: it is a string transform. The password is deliberately
    distinct from the user and database name -- with the local setup's
    brahmastra/brahmastra/brahmastra a substring check cannot tell a leaked
    password from a legitimate mention of the username, and passes either way.
    """
    from brahmastra.stores.postgres_store import PostgresStore

    s = PostgresStore(
        workspace="ws",
        dsn_override="postgresql://someuser:hunter2swordfish@db.example:5432/notes",
    )
    shown = s.describe()

    assert "hunter2swordfish" not in shown, "the password must never be rendered"
    assert "someuser:***@" in shown, "mask in place, user still identifiable"
    assert "db.example:5432/notes" in shown, "the target must stay diagnosable"


@needs_pg
def test_the_derived_half_is_refused_rather_than_half_implemented(store):
    """
    Storing triples here too would create a second copy of data that is
    supposed to have exactly one home, and nothing would say which was right.
    """
    with pytest.raises(NotImplementedError, match="system of record"):
        store.get_all_triples()
    with pytest.raises(NotImplementedError, match="NOTE_BACKEND=postgres"):
        store.save_graph({}, {})
