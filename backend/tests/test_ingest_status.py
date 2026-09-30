"""
Where each action item stands when the meeting ends (ingest/status.py).
The model proposes; "done" and "blocked" stand only on words that were said.
"""
from __future__ import annotations

import json

import pytest

from brahmastra.ingest.comprehend import Artifact
from brahmastra.ingest.passages import turns_of
from brahmastra.ingest.segment import segment
from brahmastra.ingest.status import assign

pytestmark = pytest.mark.real_session_passes

STANDUP = """\
[00:00:04] Nadia: The login fix shipped yesterday afternoon, it's live for everyone.
[00:00:15] Tomas: I'm stuck on the export until the data team publishes the new schema.
[00:01:12] Ivo: The release notes for 2.4 are going to be done by Thursday.
"""

ITEMS = [
    Artifact("action_item", "Nadia ships the login fix", owner="Nadia",
             quote="The login fix shipped yesterday afternoon"),
    Artifact("action_item", "Tomas builds the export job", owner="Tomas",
             quote="I'm stuck on the export until the data team publishes"),
    Artifact("action_item", "Ivo finishes the release notes", owner="Ivo",
             quote="The release notes for 2.4 are going to be done by Thursday"),
    Artifact("decision", "Keep the standup short", quote="Quick standup"),
]


def _model(entries):
    return lambda system, user, **kw: json.dumps({"items": entries})


def _turns():
    return turns_of(segment(STANDUP))


def test_done_and_blocked_stand_on_the_words_that_say_so():
    out, rep = assign(ITEMS, _turns(), chat=_model([
        {"id": "A1", "status": "done", "evidence": "The login fix shipped yesterday afternoon"},
        {"id": "A2", "status": "blocked", "evidence": "stuck on the export until the data team publishes",
         "blocked_on": "the data team's new schema"},
        {"id": "A3", "status": "open", "evidence": None}]))
    assert [a.status for a in out[:3]] == ["done", "blocked", "open"]
    assert out[1].blocked_on == "the data team's new schema"
    assert out[3].status is None                      # only action items carry a status


def test_a_status_with_no_words_behind_it_stays_open():
    out, rep = assign(ITEMS, _turns(), chat=_model([
        {"id": "A3", "status": "done", "evidence": None}]))
    assert out[2].status == "open" and rep["refused"]


def test_evidence_nobody_said_is_refused():
    out, _ = assign(ITEMS, _turns(), chat=_model([
        {"id": "A3", "status": "done", "evidence": "Ivo confirmed the release notes are published"}]))
    assert out[2].status == "open"


def test_a_failed_pass_leaves_everything_open():
    def down(*a, **k):
        raise RuntimeError("quota")
    out, rep = assign(ITEMS, _turns(), chat=down)
    assert [a.status for a in out[:3]] == ["open"] * 3 and "quota" in rep["error"]


def test_done_and_blocked_reach_the_graph_record():
    from brahmastra.ingest.graph_record import record_triples

    out, _ = assign(ITEMS, _turns(), chat=_model([
        {"id": "A2", "status": "blocked", "evidence": "stuck on the export until the data team publishes"}]))
    rels = {(t["subject_text"], t["relation"], t["object_text"]) for t in record_triples("Standup", [], out)}
    assert ("Tomas builds the export job", "has_status", "blocked") in rels
    assert not any(r[1] == "has_status" and r[2] == "open" for r in rels)
