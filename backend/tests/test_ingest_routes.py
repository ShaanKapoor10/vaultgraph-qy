"""
The HTTP surface, and the one thing about it that is easy to get silently wrong.

Processing runs as a background task, and a background task does NOT inherit
the request's ContextVar workspace binding. Without re-binding, work submitted
to `office` lands in whatever workspace the process defaults to -- no error, no
warning, just a meeting in the wrong knowledge base. That is the same shape as
the leak this system has already had.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

TRANSCRIPT = """\
Sarah: Let's settle the release date. I think March is too tight.
Mei: Then we're moving the release to April 15th.
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "routes.db"))
    monkeypatch.setenv("BRAHMASTRA_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    monkeypatch.delenv("BRAHMASTRA_WORKSPACE", raising=False)

    from brahmastra.stores import reset_store
    reset_store()

    # Comprehension is the only part that needs a provider; stub it so the
    # routes are tested rather than the model.
    from brahmastra.ingest import assemble
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding

    def fake(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            summary="The release moved to April.",
            participants=["Sarah", "Mei"],
            artifacts=[Artifact(
                "decision", "The release moves to April 15th", owner="Mei",
                quote="Then we're moving the release to April 15th",
                chunk_index=chunk.index, speakers=chunk.speakers)],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: fake)

    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    reset_store()


def _submit(client, title="Release planning", **params):
    return client.post("/ingest/transcripts",
                       json={"title": title, "content": TRANSCRIPT}, params=params)


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------

def test_a_transcript_is_accepted_and_processed(client):
    resp = _submit(client)
    assert resp.status_code == 200
    tid = resp.json()["transcript_id"]

    detail = client.get(f"/ingest/transcripts/{tid}").json()
    assert detail["status"] == "done"
    assert detail["artifact_counts"]["decision"] == 1


def test_a_status_poll_does_not_carry_the_whole_transcript(client):
    """It is the largest thing in the system; a poll should not ship it."""
    tid = _submit(client).json()["transcript_id"]
    assert "content" not in client.get(f"/ingest/transcripts/{tid}").json()
    assert "content" in client.get(f"/ingest/transcripts/{tid}",
                                   params={"include_text": True}).json()


def test_a_file_upload_works(client):
    resp = client.post(
        "/ingest/transcripts/upload",
        files={"file": ("standup.txt", TRANSCRIPT.encode(), "text/plain")},
    )
    assert resp.status_code == 200
    assert resp.json()["characters"] == len(TRANSCRIPT)


def test_an_unsupported_file_type_is_refused(client):
    """Ingesting the XML inside a .docx fills the knowledge base with markup
    that looks like speech."""
    resp = client.post(
        "/ingest/transcripts/upload",
        files={"file": ("notes.docx", b"PK\x03\x04binary", "application/octet-stream")},
    )
    assert resp.status_code == 415


def test_an_empty_file_is_refused(client):
    resp = client.post("/ingest/transcripts/upload",
                       files={"file": ("empty.txt", b"   \n", "text/plain")})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------

def test_decisions_are_queryable(client):
    _submit(client)
    rows = client.get("/ingest/artifacts", params={"kind": "decision"}).json()
    assert len(rows) == 1
    assert "April 15th" in rows[0]["statement"]


def test_action_items_are_queryable_by_owner(client):
    _submit(client)
    assert client.get("/ingest/artifacts",
                      params={"kind": "decision", "owner": "mei"}).json()
    assert client.get("/ingest/artifacts",
                      params={"kind": "decision", "owner": "nobody"}).json() == []


def test_transcripts_can_be_listed(client):
    _submit(client)
    rows = client.get("/ingest/transcripts").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"


def test_stats_report_what_is_held(client):
    _submit(client)
    body = client.get("/ingest/stats").json()
    assert body["transcripts"] == 1
    assert body["decision"] == 1


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_reprocessing_corrects_rather_than_duplicates(client):
    tid = _submit(client).json()["transcript_id"]
    assert client.post(f"/ingest/transcripts/{tid}/reprocess").status_code == 200
    assert len(client.get("/ingest/artifacts", params={"kind": "decision"}).json()) == 1


def test_deleting_removes_the_transcript_and_its_artifacts(client):
    tid = _submit(client).json()["transcript_id"]
    assert client.delete(f"/ingest/transcripts/{tid}").status_code == 200
    assert client.get(f"/ingest/transcripts/{tid}").status_code == 404
    assert client.get("/ingest/artifacts").json() == []


def test_an_unknown_transcript_is_a_404(client):
    assert client.get("/ingest/transcripts/nope").status_code == 404
    assert client.post("/ingest/transcripts/nope/reprocess").status_code == 404
    assert client.delete("/ingest/transcripts/nope").status_code == 404


# ---------------------------------------------------------------------------
# The workspace, which a background task does not inherit
# ---------------------------------------------------------------------------

def test_work_lands_in_the_workspace_the_request_named(client):
    """
    The binding is a ContextVar on the request. A BackgroundTask runs on a
    different context, so without re-binding, a transcript submitted to
    `office` is processed into `default` -- silently.
    """
    client.post("/workspaces", json={"id": "office", "name": "Office"})
    tid = _submit(client, workspace="office").json()["transcript_id"]

    scoped = client.get(f"/ingest/transcripts/{tid}", params={"workspace": "office"})
    assert scoped.status_code == 200
    assert scoped.json()["artifact_counts"]["decision"] == 1, (
        "the background task processed into the wrong workspace"
    )
    assert client.get(f"/ingest/transcripts/{tid}").status_code == 404


def test_artifacts_are_scoped_to_their_workspace(client):
    client.post("/workspaces", json={"id": "office", "name": "Office"})
    _submit(client, workspace="office")

    assert client.get("/ingest/artifacts", params={"workspace": "office"}).json()
    assert client.get("/ingest/artifacts").json() == []


# ---------------------------------------------------------------------------
# A half-record must not pass for a whole one
# ---------------------------------------------------------------------------

def test_a_partly_comprehended_transcript_is_not_reported_as_complete(
        client, monkeypatch):
    """
    Comprehension degrades on purpose: when the focused variant's concerns pass
    fails, the commitments it already found are still stored, because half a
    record beats none.

    That was invisible. A real ingestion hit the Groq daily cap on the second
    call and stored four decisions and four action items with ZERO risks and
    ZERO open questions, reporting `status: done, error: null` -- which is
    indistinguishable from a meeting that genuinely raised no concerns. The
    evidence existed, in `rejected`, and lived only in the report returned by
    process_transcript, which the HTTP path discards because it runs the work
    in a background task.
    """
    from brahmastra.ingest import assemble
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding

    def half(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            summary="Commitments only.",
            artifacts=[Artifact("decision", "The release moves to April 15th",
                                chunk_index=chunk.index)],
            rejected=["pass failed: LLMQuotaExhausted: daily cap"],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: half)

    tid = _submit(client, title="Half a meeting").json()["transcript_id"]
    body = client.get(f"/ingest/transcripts/{tid}").json()

    assert body["complete"] is False, "a half-comprehended record looked whole"
    assert body["incomplete_chunks"] == [0]
    assert "quota" in body["chunks"][0]["error"].lower()


def test_a_fully_comprehended_transcript_is_reported_as_complete(client):
    tid = _submit(client).json()["transcript_id"]
    body = client.get(f"/ingest/transcripts/{tid}").json()

    assert body["complete"] is True
    assert "incomplete_chunks" not in body


def test_a_refused_quote_does_not_make_a_transcript_incomplete(client, monkeypatch):
    """
    Only PASS failures count. The rest of `rejected` is the grounding check
    turning away ungrounded quotes, which is the system working as designed --
    flagging that as an incomplete record would cry wolf on every transcript.
    """
    from brahmastra.ingest import assemble
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding

    def grounded_only(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            artifacts=[Artifact("decision", "The release moves to April 15th",
                                chunk_index=chunk.index)],
            rejected=["decision: quote not in passage"],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: grounded_only)

    tid = _submit(client, title="Grounding did its job").json()["transcript_id"]
    assert client.get(f"/ingest/transcripts/{tid}").json()["complete"] is True


def test_a_degraded_chunk_does_not_report_the_whole_transcript_as_failed(
        client, monkeypatch):
    """
    A chunk that lost one comprehension pass still produced artifacts, so the
    transcript is partial, not failed. Counting it as a failure made a
    single-chunk transcript report `status: error` while holding three
    decisions and four action items -- understating the record as badly as the
    silent `done` overstated it.
    """
    from brahmastra.ingest import assemble
    from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding

    def half(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            artifacts=[Artifact("decision", "The release moves to April 15th",
                                chunk_index=chunk.index)],
            rejected=["pass failed: LLMQuotaExhausted: daily cap"],
        )

    monkeypatch.setattr(assemble, "comprehension_strategy", lambda: half)
    tid = _submit(client, title="Degraded, not dead").json()["transcript_id"]
    body = client.get(f"/ingest/transcripts/{tid}").json()

    assert body["status"] == "done", "a degraded chunk was reported as a failure"
    assert body["complete"] is False, "but the record is not whole"
    assert body["artifact_counts"]["decision"] == 1, "and its artifacts were kept"
