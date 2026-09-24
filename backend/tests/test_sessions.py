"""
Raw session search: the conversation itself, indexed, no model in the loop.

The distilled checkpoint note keeps the last 20,000 characters of a stretch
and can be wrong; this index keeps every exchange verbatim, redacts secrets,
and re-indexes idempotently from Claude Code's own transcript.
"""
from __future__ import annotations

import json

import pytest

from brahmastra import sessions


def _row(kind, text, uuid, **extra):
    content = text if kind == "user" else [{"type": "text", "text": text}]
    return {"type": kind, "uuid": uuid, "sessionId": "sess-1",
            "timestamp": "2026-09-24T10:00:00Z",
            "message": {"role": kind, "content": content}, **extra}


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


CONVO = [
    _row("user", "Why did the Notion pages get overwritten?", "u1"),
    _row("assistant", "The live_sync fallback used the global database for every "
                      "workspace. Fixed in sync.py: only the home workspace may.", "a1"),
    _row("user", "Now make the scheduler tick every workspace.", "u2"),
    _row("assistant", "Done: tick_all binds each workspace in turn.", "a2"),
]


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    # A deterministic stand-in for the embedding model: bag of hashed words.
    import numpy as np
    from brahmastra import embeddings

    def fake(texts):
        out = []
        for t in texts:
            v = np.zeros(64, dtype="float32")
            for w in sessions._tokens(t):
                v[hash(w) % 64] += 1
            out.append(v.tolist())
        return out

    calls = []
    monkeypatch.setattr(embeddings, "embed", lambda texts: calls.append(len(texts)) or fake(texts))
    monkeypatch.setattr(embeddings, "embed_one", lambda t: fake([t])[0])
    s = sessions.SessionIndex(workspace="default")
    s.calls = calls
    return s


# -- reading -----------------------------------------------------------------

def test_an_exchange_is_a_request_and_the_work_that_answered_it(tmp_path):
    session, exchanges = sessions.read_exchanges(_write(tmp_path / "t.jsonl", CONVO))
    assert session == "sess-1"
    assert [e.request for e in exchanges] == [
        "Why did the Notion pages get overwritten?",
        "Now make the scheduler tick every workspace."]
    assert "only the home workspace may" in exchanges[0].work


def test_what_is_not_the_conversation_is_left_out(tmp_path):
    rows = CONVO + [
        _row("user", "a subagent's prompt", "s1", isSidechain=True),
        _row("user", "This session is being continued... (summary)", "c1", isCompactSummary=True),
        _row("user", "<system-reminder>injected</system-reminder>", "r1"),
        _row("user", "[tool output]", "m1", isMeta=True),
    ]
    _, exchanges = sessions.read_exchanges(_write(tmp_path / "t.jsonl", rows))
    assert len(exchanges) == 2


def test_ide_tags_are_stripped_but_the_request_is_kept(tmp_path):
    rows = [_row("user", "<ide_opened_file>The user opened README.md</ide_opened_file>"
                         "restart the server", "u1")]
    _, exchanges = sessions.read_exchanges(_write(tmp_path / "t.jsonl", rows))
    assert exchanges[0].request == "restart the server"


# -- redaction ---------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "gsk_" + "A1b2C3d4" * 6,
    "ntn_" + "FAKEnotARealToken" * 2,
    "sk-ant-" + "api03-" + "x" * 30,
])
def test_known_key_formats_never_reach_the_index(secret):
    assert secret not in sessions.redact(f"the key is {secret} ok")


def test_a_credential_assignment_keeps_its_name_and_loses_its_value():
    out = sessions.redact("set NEO4J_PASSWORD=hunter2hunter2 and GROQ_API_KEYS=gsk_" + "Q" * 40)
    assert "NEO4J_PASSWORD=[redacted]" in out
    assert "hunter2" not in out and "QQQQ" not in out


def test_prose_about_tokens_is_left_alone():
    text = "The token budget is 8000 tokens per minute."
    assert sessions.redact(text) == text


# -- pieces ------------------------------------------------------------------

def test_a_long_exchange_is_cut_to_the_embedding_window():
    work = " ".join(f"Sentence number {i} about the resolver." for i in range(300))
    ps = sessions.pieces(sessions.Exchange("u1", "", "Explain the resolver", work))
    assert len(ps) > 1
    assert all(len(p.text) <= sessions.PIECE_CHARS + sessions.REQUEST_PREFIX_CHARS + 20 for p in ps)
    # Every later piece still says what it was for.
    assert all(p.text.startswith("[re: Explain the resolver]") for p in ps[1:])
    assert len({p.key for p in ps}) == len(ps)


# -- indexing ----------------------------------------------------------------

def test_indexing_is_idempotent_and_embeds_only_what_changed(store, tmp_path):
    path = _write(tmp_path / "t.jsonl", CONVO)
    first = sessions.index_transcript(path, store)
    assert first["pieces"] == 2 and first["embedded"] == 2

    again = sessions.index_transcript(path, store)
    assert again["embedded"] == 0 and again["removed"] == 0

    _write(path, CONVO + [_row("user", "And the orphaned triples?", "u3"),
                          _row("assistant", "CompositeStore.delete_note now reaches Neo4j.", "a3")])
    grown = sessions.index_transcript(path, store)
    assert grown["embedded"] == 1


def test_an_exchange_gone_from_the_transcript_leaves_the_index(store, tmp_path):
    path = _write(tmp_path / "t.jsonl", CONVO)
    sessions.index_transcript(path, store)
    _write(path, CONVO[:2])                          # rewound
    report = sessions.index_transcript(path, store)
    assert report["removed"] == 1
    assert store.counts() == {"sess-1": 1}


def test_stored_text_is_redacted(store, tmp_path):
    key = "gsk_" + "Z9" * 26
    path = _write(tmp_path / "t.jsonl", [_row("user", f"use this key {key}", "u1"),
                                         _row("assistant", "added", "a1")])
    sessions.index_transcript(path, store)
    assert not any(key in r["text"] or key in (r["request"] or "") for r in store.all())


def test_workspaces_do_not_see_each_others_sessions(store, tmp_path):
    sessions.index_transcript(_write(tmp_path / "t.jsonl", CONVO), store)
    other = sessions.SessionIndex(workspace="office")
    assert other.all() == []
    assert sessions.search("Notion", store=other) == []


# -- search ------------------------------------------------------------------

def test_search_finds_the_exchange_by_its_words(store, tmp_path):
    sessions.index_transcript(_write(tmp_path / "t.jsonl", CONVO), store)
    hits = sessions.search("notion pages overwritten", store=store)
    assert hits[0]["request"].startswith("Why did the Notion pages")


def test_one_hit_per_exchange(store, tmp_path):
    work = " ".join("The resolver merges names with Jaro-Winkler." for _ in range(80))
    sessions.index_transcript(_write(tmp_path / "t.jsonl", [
        _row("user", "resolver question", "u1"), _row("assistant", work, "a1")]), store)
    hits = sessions.search("resolver Jaro-Winkler", store=store)
    assert len(hits) == 1


def test_search_on_an_empty_index_is_empty(store):
    assert sessions.search("anything", store=store) == []


def test_without_an_embedding_model_search_is_lexical(store, tmp_path, monkeypatch):
    from brahmastra import embeddings
    monkeypatch.setattr(embeddings, "embed", lambda texts: None)
    monkeypatch.setattr(embeddings, "embed_one", lambda t: None)
    sessions.index_transcript(_write(tmp_path / "t.jsonl", CONVO), store)
    assert sessions.search("scheduler tick", store=store)[0]["request"].startswith("Now make")


# -- the hook ----------------------------------------------------------------

def test_a_boundary_hook_indexes_the_transcript(monkeypatch, tmp_path):
    from brahmastra import checkpoint as cp
    spawned = []
    monkeypatch.setattr(cp, "_spawn", lambda args: spawned.append(args))
    monkeypatch.setattr(cp, "capture", lambda payload: None)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "SessionEnd", "session_id": "s",
        "transcript_path": str(tmp_path / "t.jsonl")})))
    cp.main([])
    assert ["brahmastra.sessions", "--index", str(tmp_path / "t.jsonl")] in spawned


def test_a_quiet_stop_hook_does_not_load_the_model(monkeypatch, tmp_path):
    from brahmastra import checkpoint as cp
    spawned = []
    monkeypatch.setattr(cp, "_spawn", lambda args: spawned.append(args))
    monkeypatch.setattr(cp, "capture", lambda payload: None)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": "s",
        "transcript_path": str(tmp_path / "t.jsonl")})))
    cp.main([])
    assert spawned == []


def test_a_message_relogged_on_resume_is_counted_once(tmp_path):
    """Claude Code writes the same uuid twice when a session resumes."""
    _, exchanges = sessions.read_exchanges(_write(tmp_path / "t.jsonl", CONVO + CONVO[:2]))
    assert len(exchanges) == 2
    assert exchanges[0].work.count("only the home workspace may") == 1


def test_a_task_notification_is_not_a_request(tmp_path):
    rows = CONVO[:2] + [_row("user", "<task-notification><task-id>x</task-id>done</task-notification>", "n1"),
                        _row("assistant", "The background build finished.", "a9")]
    _, exchanges = sessions.read_exchanges(_write(tmp_path / "t.jsonl", rows))
    assert len(exchanges) == 1
    assert "background build finished" in exchanges[0].work
