"""
The raw transcript, searchable (ingest/passages.py), and /ask using it
(rag._passages). No provider: embeddings may be absent, and the chat is faked.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest import passages
from brahmastra.ingest.segment import parse_turns

MEETING = """\
[00:00:44] Mei: I don't think March is real any more. The payments integration is maybe sixty percent done and Priya's out for two weeks.
[00:01:12] Mei: Card flow is basically done. Refunds and the reconciliation job haven't been started.
[00:01:34] Raj: Deepa was clear that we can't go live without it because of the audit.
[00:01:47] Sarah: Okay. Then we're moving the release to April 15th.
"""


@pytest.fixture
def index(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("NOTE_BACKEND", "")
    return passages.PassageIndex(workspace="default")


def test_passages_keep_speaker_and_time_on_every_line():
    ps = passages.passages(parse_turns(MEETING))
    assert all(line.startswith("[00:") for p in ps for line in p.text.splitlines())
    assert ps[0].start_time == "00:00:44" and "Mei" in ps[0].speakers


def test_a_long_turn_is_split_at_sentences_and_keeps_its_speaker():
    # Two speaker lines, or the format reads as prose and has no speakers.
    long = ("[00:09:00] Sam: Go ahead.\n[00:10:00] Khy: "
            + " ".join(f"Sentence number {i} is here." for i in range(80)) + "\n")
    ps = passages.passages(parse_turns(long))
    assert len(ps) > 1
    assert all(p.text.splitlines()[-1].startswith("[00:10:00] Khy: ") for p in ps)
    assert all(len(p.text) <= passages.PASSAGE_CHARS + 40 for p in ps)


def test_secrets_said_in_a_meeting_are_not_stored():
    text = "[00:00:01] Sam: the key is gsk_" + "Z" * 48 + " okay\n"
    assert "gsk_" + "Z" * 48 not in passages.passages(parse_turns(text))[0].text


def test_indexing_is_idempotent_and_search_finds_what_was_said(index):
    turns = parse_turns(MEETING)
    first = passages.index_transcript("t1", "Q3 planning", None, turns, index)
    again = passages.index_transcript("t1", "Q3 planning", None, turns, index)
    assert first["passages"] >= 1 and again["embedded"] == 0
    hit = passages.search("why can't we go live without reconciliation audit", store=index)[0]
    assert "audit" in hit["text"] and hit["title"] == "Q3 planning"


def test_deleting_a_transcript_removes_its_passages(index):
    passages.index_transcript("t1", "Q3", None, parse_turns(MEETING), index)
    index.delete("t1")
    assert passages.search("audit", store=index) == []


def test_ask_answers_from_what_was_said_when_no_entity_matches(monkeypatch):
    from brahmastra import rag

    monkeypatch.setattr(rag, "_passages", lambda q: [
        {"transcript_id": "t1", "title": "Q3", "start_time": "00:01:34", "end_time": None,
         "speakers": "Raj", "text": "[00:01:34] Raj: can't go live without it because of the audit."}])
    monkeypatch.setattr(rag, "_match_entities", lambda q, nodes: [])
    seen = {}

    def chat(system, user, **kw):
        seen["user"] = user
        return "Because of the audit [t:1]."

    monkeypatch.setattr(rag, "chat", chat)
    out = rag.local_search("Why can't they go live?", nodes=[{"id": "x"}])
    assert "audit" in out["answer"] and out["passages"][0]["start_time"] == "00:01:34"
    assert "[t:1]" in seen["user"]


def test_passages_can_be_switched_off(monkeypatch):
    from brahmastra import rag

    monkeypatch.setenv("RAG_PASSAGES", "0")
    assert rag._passages("anything") == []


# -- /ask is GRAPH FIRST: the transcript only when the graph has nothing ---------

def _facts(monkeypatch, rag):
    monkeypatch.setattr(rag, "_match_entities", lambda q, nodes: [{"id": "payments integration"}])
    monkeypatch.setattr(rag, "_match_statements", lambda q, nodes: [])
    monkeypatch.setattr(rag, "_subgraph_facts", lambda ids, depth=1: [
        {"text": "Payments is sixty percent done said_by Mei", "note_id": "n1", "quote": "sixty percent"}])
    monkeypatch.setattr(rag, "_citations", lambda ids: [{"note_id": i, "title": i} for i in ids])


def test_the_graph_answers_and_the_transcript_is_never_read(monkeypatch):
    from brahmastra import rag

    _facts(monkeypatch, rag)
    monkeypatch.setattr(rag, "_passages", lambda q: pytest.fail("read the transcript first"))
    monkeypatch.setattr(rag, "chat", lambda system, user, **k: "About sixty percent [n:n1].")
    out = rag.local_search("How far along is payments?", nodes=[{"id": "x"}])
    assert out["source"] == "graph" and "sixty" in out["answer"]


def test_the_transcript_is_read_only_when_the_graph_says_it_has_nothing(monkeypatch):
    from brahmastra import rag

    _facts(monkeypatch, rag)
    monkeypatch.setattr(rag, "_passages", lambda q: [
        {"transcript_id": "t1", "title": "Q3", "start_time": "00:01:34", "end_time": None,
         "speakers": "Raj", "text": "[00:01:34] Raj: because of the audit."}])
    replies = iter([rag.NOT_IN_GRAPH, "Because of the audit [t:1]."])
    monkeypatch.setattr(rag, "chat", lambda system, user, **k: next(replies))
    out = rag.local_search("Why is reconciliation required?", nodes=[{"id": "x"}])
    assert out["source"] == "transcript" and out["passages"][0]["start_time"] == "00:01:34"


def test_a_question_naming_no_entity_finds_a_statement_by_meaning(monkeypatch):
    from brahmastra import embeddings, rag

    film = "The trainer recommended watching The Big Short to see how a market view pays off"
    nodes = [{"id": film, "type": "statement"}, {"id": "Swaps settle on the difference", "type": "statement"},
             {"id": "Brent", "type": "concept"}]

    def fake_embed(texts):
        return [[1.0, 0.0] if ("film" in t.lower() or "Big Short" in t) else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    rag._statement_vectors.clear()
    assert [n["id"] for n in rag._match_statements("What film did the trainer recommend?", nodes)] == [film]


def test_no_match_is_not_sent_to_the_cluster_summaries():
    from brahmastra import rag

    assert rag._is_global("What film did the trainer recommend?", []) is False
    assert rag._is_global("What are the main themes overall?", []) is True


def test_a_matched_statement_brings_the_other_statements_about_what_it_mentions(monkeypatch):
    monkeypatch.setenv("RAG_RELATED_STATEMENTS", "1")          # off by default since measured
    from brahmastra import rag

    s1 = "Payments is sixty percent done and Priya will be out for two weeks"
    s2 = "Priya is out until the 20th"
    calls = []

    def neighbourhood(ids, depth=1):
        calls.append(set(ids))
        if s1 in ids:
            return [{"text": f"{s1} mentions Priya", "note_id": "r", "quote": ""},
                    {"text": f"{s1} said_by Mei", "note_id": "r", "quote": ""}]
        if ids == {"Priya"}:
            return [{"text": f"{s2} mentions Priya", "note_id": "r", "quote": ""},
                    {"text": f"{s1} mentions Priya", "note_id": "r", "quote": ""}]
        return []

    monkeypatch.setattr(rag, "_subgraph_facts", neighbourhood)
    facts = rag._with_related_statements([{"id": s1, "type": "statement"}], {s1},
                                         neighbourhood({s1}))
    texts = [f["text"] for f in facts]
    assert f"{s2} mentions Priya" in texts and len(texts) == len(set(texts))
    assert {"Priya"} in calls and {"Mei"} not in calls          # only what it MENTIONS


def test_no_statement_matched_means_no_extra_walk(monkeypatch):
    from brahmastra import rag

    monkeypatch.setattr(rag, "_subgraph_facts", lambda ids, depth=1: pytest.fail("walked"))
    facts = [{"text": "Raj owns reconciliation", "note_id": "", "quote": ""}]
    assert rag._with_related_statements([{"id": "Raj", "type": "person"}], {"Raj"}, facts) == facts
