"""
End to end, without an LLM: a transcript becomes artifacts AND notes.

The notes matter as much as the artifacts. Artifacts are typed rows because the
ontology has no `decided` or `action_item` to hold them, but if that were the
only output a decision would be findable only by someone who already knew to
look in the artifacts table. The generated note is the bridge into the existing
extract -> resolve -> graph path, which is what makes a transcript searchable
alongside everything else.
"""
from __future__ import annotations

import pytest

from brahmastra import db
from brahmastra.ingest import assemble
from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
from brahmastra.ingest.segment import segment
from brahmastra.ingest.store import IngestStore, Transcript

TRANSCRIPT = """\
Sarah: Let's settle the release date. I think March is too tight.
Mei: Agreed. The payments integration isn't done and Priya is out until the 20th.
Sarah: Then we're moving the release to April 15th. I'll own the comms.
Mei: I'll update the roadmap by Friday.
Sarah: One risk is that the Acme contract assumes a March delivery.
"""


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
    """A believable comprehension result, with no provider involved."""
    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            summary="The team moved the release from March to April.",
            participants=["Sarah", "Mei"],
            topics=["release date"],
            artifacts=[
                Artifact("decision", "The release moves to April 15th",
                         owner="Sarah", rationale="payments is not ready",
                         quote="Then we're moving the release to April 15th",
                         chunk_index=chunk.index, speakers=chunk.speakers),
                Artifact("action_item", "Update the roadmap", owner="Mei",
                         due="Friday", quote="I'll update the roadmap by Friday",
                         chunk_index=chunk.index, speakers=chunk.speakers),
            ],
            rejected=["decision: quote not found in the passage — 'invented'"],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)


# ---------------------------------------------------------------------------
# The whole path
# ---------------------------------------------------------------------------

def test_a_transcript_yields_artifacts_and_notes(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    report = assemble.process_transcript(tid, store=store)

    assert report["status"] == "ok"
    assert report["artifacts"] == 2
    assert report["notes"] == 1
    assert store.get_transcript(tid)["status"] == "done"


def test_the_note_reaches_the_existing_pipeline_as_pending(store, understood):
    """
    The bridge. Without a pending note nothing extracts, and the transcript
    never becomes entities in the graph.
    """
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    pending = db.get_notes(status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == assemble.note_id_for(tid, 0)


def test_the_note_is_marked_as_coming_from_a_transcript(store, understood):
    """A paragraph a model distilled from speech is not prose a person wrote,
    and retrieval should be able to tell them apart."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)
    assert db.get_note(assemble.note_id_for(tid, 0))["source"] == "transcript"


def test_the_note_body_is_entity_rich_prose_not_a_data_dump(store, understood):
    """
    "Owner: Mei" yields no triple. "Mei will update the roadmap by Friday"
    yields a person, an action and a date -- which is the entire reason the
    note is written as sentences.
    """
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)
    body = db.get_note(assemble.note_id_for(tid, 0))["content"]

    assert "Mei will update the roadmap" in body
    assert "due Friday" in body
    assert "Sarah is accountable" in body
    assert "Sarah, Mei" in body


# ---------------------------------------------------------------------------
# Querying it
# ---------------------------------------------------------------------------

def test_decisions_are_queryable_by_kind(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    decisions = store.get_artifacts(kind="decision")
    assert len(decisions) == 1
    assert "April 15th" in decisions[0]["statement"]
    assert decisions[0]["quote"]


def test_action_items_are_queryable_by_owner(store, understood):
    """"What are my action items?" is the question this has to answer."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    mine = store.get_artifacts(kind="action_item", owner="mei")
    assert len(mine) == 1
    assert mine[0]["due"] == "Friday"


def test_an_artifact_can_be_traced_back_to_what_was_said(store, understood):
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    art = store.get_artifacts(kind="decision")[0]
    assert art["transcript_id"] == tid
    assert art["quote"] in TRANSCRIPT
    assert "Sarah" in art["speakers"]


def test_what_was_rejected_is_reported(store, understood):
    """The evidence that the grounding check is doing something, and the first
    place to look when a transcript yields less than expected."""
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    report = assemble.process_transcript(tid, store=store)
    assert any("quote not found" in r for r in report["rejected"])


# ---------------------------------------------------------------------------
# Running it twice
# ---------------------------------------------------------------------------

def test_re_ingesting_replaces_rather_than_duplicates(store, understood):
    """
    Same contract extraction has when it deletes a note's triples before
    re-inserting. Without it, correcting a transcript doubles its decisions.
    """
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)
    assemble.process_transcript(tid, store=store)

    assert len(store.get_artifacts(kind="decision")) == 1
    assert len(store.get_chunks(tid)) == 1
    assert len(db.get_notes()) == 1


# ---------------------------------------------------------------------------
# One bad chunk must not cost the document
# ---------------------------------------------------------------------------

def test_a_failing_chunk_leaves_the_rest_intact(store, monkeypatch):
    long_transcript = "\n".join(
        f"{'Sarah' if i % 2 else 'Mei'}: {' '.join(['word'] * 40)} point {i}."
        for i in range(60)
    )

    def flaky(chunk, max_tokens=None):
        if chunk.index == 1:
            return ChunkUnderstanding(chunk_index=chunk.index, error="429 rate limit")
        return ChunkUnderstanding(
            chunk_index=chunk.index, summary=f"Part {chunk.index}.",
            participants=["Sarah"],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: flaky)
    tid = store.create_transcript(Transcript("", "Long meeting", long_transcript))
    report = assemble.process_transcript(tid, store=store)

    assert report["status"] == "partial"
    assert len(report["errors"]) == 1
    assert report["comprehended"] == report["chunks"] - 1
    assert store.get_transcript(tid)["status"] == "done"


def test_a_wholly_failed_transcript_says_so(store, monkeypatch):
    monkeypatch.setattr(
        assemble, "comprehension_strategy",
        lambda: (lambda chunk, max_tokens=None: ChunkUnderstanding(
            chunk_index=chunk.index, error="no LLM provider available")),
    )
    tid = store.create_transcript(Transcript("", "Release planning", TRANSCRIPT))
    report = assemble.process_transcript(tid, store=store)

    assert report["status"] == "error"
    assert store.get_transcript(tid)["status"] == "error"
    assert "no LLM provider" in store.get_transcript(tid)["error"]


def test_an_unknown_transcript_is_reported_not_raised(store):
    assert assemble.process_transcript("nope", store=store)["status"] == "error"


def test_an_empty_transcript_is_not_an_error(store, understood):
    tid = store.create_transcript(Transcript("", "Empty", "   \n\n  "))
    report = assemble.process_transcript(tid, store=store)
    assert report["status"] == "ok"
    assert report["chunks"] == 0


# ---------------------------------------------------------------------------
# Workspace isolation, which fails OPEN if forgotten
# ---------------------------------------------------------------------------

def test_transcripts_do_not_leak_across_workspaces(store, understood):
    """
    Property-based partitioning returns another workspace's data silently when
    a filter is forgotten. It has already happened once in this system.
    """
    office = IngestStore(workspace="office")
    office.init_schema()

    mine = store.create_transcript(Transcript("", "Home meeting", TRANSCRIPT))
    theirs = office.create_transcript(Transcript("", "Office meeting", TRANSCRIPT))

    assert [t["id"] for t in store.list_transcripts()] == [mine]
    assert [t["id"] for t in office.list_transcripts()] == [theirs]
    assert office.get_transcript(mine) is None


def test_artifacts_do_not_leak_across_workspaces(store, understood):
    office = IngestStore(workspace="office")
    office.init_schema()

    tid = store.create_transcript(Transcript("", "Home meeting", TRANSCRIPT))
    assemble.process_transcript(tid, store=store)

    assert store.get_artifacts(kind="decision")
    assert office.get_artifacts(kind="decision") == []


def test_it_creates_the_note_schema_it_writes_into(monkeypatch, tmp_path, understood):
    """
    Every test here passed while the first real CLI run died on "no such table:
    notes". The API path only worked because the app's lifespan calls init_db()
    at startup, so the CLI -- which has no lifespan -- was relying on somebody
    else having done it. A stage that writes notes owns being able to.
    """
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "virgin.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    reset_store()

    fresh = IngestStore(workspace="default")
    fresh.init_schema()                       # ingest tables only, deliberately
    tid = fresh.create_transcript(Transcript("", "Release planning", TRANSCRIPT))

    report = assemble.process_transcript(tid, store=fresh)

    assert report["status"] == "ok"
    assert report["notes"] == 1
    reset_store()


# ---------------------------------------------------------------------------
# The note is prose that goes into the knowledge base, so it must read true
# ---------------------------------------------------------------------------

def _understanding(*artifacts):
    return ChunkUnderstanding(chunk_index=0, summary="A meeting happened.",
                              participants=["Sarah"], artifacts=list(artifacts))


def test_a_question_is_not_given_two_terminators():
    """Observed on a real run: "...before we tell them about the slip?." """
    chunk = segment(TRANSCRIPT, max_tokens=4000)[0]
    body = assemble.build_note_body("Planning", _understanding(
        Artifact("open_question", "Should legal review the Acme contract?",
                 owner="Raj", quote="q")), chunk)

    assert "?." not in body
    assert "Should legal review the Acme contract?" in body


def test_whoever_raised_a_risk_is_not_called_accountable_for_it():
    """
    A falsehood in the knowledge base. `comprehend` collects the owner of a
    risk as "who raised it", so calling them accountable asserts something
    nobody said.
    """
    chunk = segment(TRANSCRIPT, max_tokens=4000)[0]
    body = assemble.build_note_body("Planning", _understanding(
        Artifact("risk", "The Acme contract assumes a March delivery",
                 owner="Mei", quote="q")), chunk)

    assert "Mei raised it" in body
    assert "accountable" not in body


def test_whoever_asked_a_question_has_not_been_assigned_it():
    chunk = segment(TRANSCRIPT, max_tokens=4000)[0]
    body = assemble.build_note_body("Planning", _understanding(
        Artifact("open_question", "Do the other contracts have the same clause",
                 owner="Mei", quote="q")), chunk)

    assert "Mei asked it" in body
    assert "accountable" not in body


def test_a_decision_owner_is_accountable():
    chunk = segment(TRANSCRIPT, max_tokens=4000)[0]
    body = assemble.build_note_body("Planning", _understanding(
        Artifact("decision", "The release moves to April 15th", owner="Sarah",
                 quote="q")), chunk)
    assert "Sarah is accountable for it" in body


def test_a_rationale_does_not_start_mid_sentence_with_a_capital():
    chunk = segment(TRANSCRIPT, max_tokens=4000)[0]
    body = assemble.build_note_body("Planning", _understanding(
        Artifact("decision", "Move the release", owner="Sarah",
                 rationale="Reconciliation is a hard requirement.", quote="q")), chunk)
    assert "because reconciliation is a hard requirement" in body


# ---------------------------------------------------------------------------
# The leak this shipped with
# ---------------------------------------------------------------------------

def test_notes_land_in_the_same_workspace_as_the_transcript(monkeypatch, tmp_path,
                                                            understood):
    """
    The `workspace=` argument reached the ingest store, so transcripts and
    artifacts landed correctly -- while db.upsert_note kept using the AMBIENT
    workspace, so a transcript processed into `office` wrote its notes into
    `default`. Silently. The same shape as the leak that once overwrote a real
    note belonging to another graph.
    """
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "leak.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    monkeypatch.delenv("BRAHMASTRA_WORKSPACE", raising=False)
    from brahmastra.stores import reset_store
    reset_store()
    db.init_db()

    office = IngestStore(workspace="office")
    office.init_schema()
    tid = office.create_transcript(Transcript("", "Office meeting", TRANSCRIPT))
    assemble.process_transcript(tid, store=office, workspace="office")

    from brahmastra.stores.sqlite_store import SQLiteStore
    assert [n["title"] for n in SQLiteStore(workspace="office").get_notes()] != []
    assert SQLiteStore(workspace="default").get_notes() == [], (
        "an office transcript wrote its notes into the default workspace"
    )
    reset_store()


def test_the_workspace_binding_is_restored_afterwards(monkeypatch, tmp_path, understood):
    """A binding left set hands the wrong graph to whatever runs next."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "restore.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    from brahmastra.stores import reset_store
    from brahmastra.workspace import current_workspace
    reset_store()
    db.init_db()

    before = current_workspace()
    office = IngestStore(workspace="office")
    office.init_schema()
    tid = office.create_transcript(Transcript("", "Office meeting", TRANSCRIPT))
    assemble.process_transcript(tid, store=office, workspace="office")

    assert current_workspace() == before
    reset_store()


# ---------------------------------------------------------------------------
# Which comprehension runs
# ---------------------------------------------------------------------------

def _model(monkeypatch, name):
    """Pin what a chat() would actually reach, without reaching it."""
    import brahmastra.llm as llm
    monkeypatch.setattr(llm, "active_model", lambda: name)


def test_the_strategy_defaults_to_the_focused_passes(monkeypatch):
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_a_small_model_is_given_the_single_pass(monkeypatch):
    """
    Measured over two cases and three runs each: on qwen2.5:7b the two
    variants score 19% [9-36] and 24% [14-36] recall -- indistinguishable,
    and the sign flipped between two measurements -- while focused bills twice
    the calls. On gpt-oss-120b focused wins outright, 69% [64-79] against
    43% [14-64]. So a flat default is wrong whichever value it takes.

    `focused` was the worse one to have defaulted to, because the small model
    is what this repository falls back to when the Groq quota runs out: the
    setting only matters on a bad day and had been tuned for a good one.
    """
    from brahmastra.ingest.comprehend import comprehend_chunk
    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    _model(monkeypatch, "qwen2.5:7b-instruct")
    assert assemble.comprehension_strategy() is comprehend_chunk


def test_a_large_model_is_given_the_focused_passes(monkeypatch):
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    _model(monkeypatch, "openai/gpt-oss-120b")
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_a_large_LOCAL_model_is_not_treated_as_a_small_one(monkeypatch):
    """
    The choice keys on size, not on provider. "Local" is not the same claim as
    "small", and a 70B on someone's own hardware has nothing in common with the
    7B the measurement was taken on.
    """
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    _model(monkeypatch, "llama3.3:70b")
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_an_unreadable_model_name_gets_the_better_measured_default(monkeypatch):
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    _model(monkeypatch, "claude-opus-5")
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_an_explicit_setting_beats_the_model(monkeypatch):
    """The heuristic is a default, not a policy: two measured points do not
    get to overrule someone who has measured a third."""
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.setenv("INGEST_COMPREHEND_PASSES", "focused")
    _model(monkeypatch, "qwen2.5:7b-instruct")
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_no_provider_at_all_does_not_break_the_choice(monkeypatch):
    """A heuristic that cannot be evaluated must not fail the ingestion."""
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    from brahmastra.llm import LLMUnavailable
    import brahmastra.llm as llm

    def dead():
        raise LLMUnavailable("nothing configured")

    monkeypatch.delenv("INGEST_COMPREHEND_PASSES", raising=False)
    monkeypatch.setattr(llm, "active_model", dead)
    assert assemble.comprehension_strategy() is comprehend_chunk_focused


def test_a_model_name_is_read_for_its_size_not_its_version():
    """"qwen2.5" must not read as a 2.5-billion-parameter model."""
    assert assemble.model_params_b("qwen2.5:7b-instruct") == 7.0
    assert assemble.model_params_b("openai/gpt-oss-120b") == 120.0
    assert assemble.model_params_b("llama-3.3-70b-versatile") == 70.0
    assert assemble.model_params_b("qwen2.5") is None
    assert assemble.model_params_b("") is None


def test_the_single_pass_can_be_selected(monkeypatch):
    """
    Configurable because the evaluation says the right answer DEPENDS ON THE
    MODEL: splitting the work lifted recall from 43% to 69% on gpt-oss-120b
    and bought nothing on qwen2.5:7b. That is not something to hardcode.
    """
    from brahmastra.ingest.comprehend import comprehend_chunk
    monkeypatch.setenv("INGEST_COMPREHEND_PASSES", "single")
    assert assemble.comprehension_strategy() is comprehend_chunk


def test_an_unrecognised_strategy_falls_back_rather_than_crashing(monkeypatch):
    from brahmastra.ingest.comprehend import comprehend_chunk_focused
    monkeypatch.setenv("INGEST_COMPREHEND_PASSES", "swarm")
    assert assemble.comprehension_strategy() is comprehend_chunk_focused
