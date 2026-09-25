"""
The whole-meeting reconcile pass (experimental): the model proposes, code
decides what may change. These pin the guardrails with a model that proposes
every wrong thing it could.
"""
from __future__ import annotations

import json

from brahmastra.ingest.comprehend import Artifact
from brahmastra.ingest.reconcile import reconcile

ARTS = [
    Artifact("action_item", "Take the reconciliation job", owner=None,
             quote="I can take the reconciliation job", chunk_index=0),
    Artifact("decision", "Raj owns reconciliation", quote="Raj, you own reconciliation",
             chunk_index=1),
    Artifact("action_item", "Own reconciliation, target the 27th", owner="Raj",
             quote="Raj, you own reconciliation, target the 27th", chunk_index=1),
    Artifact("open_question", "Should legal review the Acme contract?",
             quote="Should legal review the Acme contract before we tell them"),
]
PEOPLE = ["Mei", "Raj", "Sarah"]


def _model(items, overview=None, insights=None):
    def chat(system, user, **kw):
        return json.dumps({"items": items,
                           "overview": overview or {"headline": "", "summary": ""},
                           "insights": insights or []})
    return chat


def _item(fid, **kw):
    base = {"id": fid, "duplicate_of": None, "drop": None, "reversed_by": None, "owner": None,
            "due": "", "status": "open", "priority": "normal"}
    return {**base, **kw}


def test_an_owner_named_later_lands_on_the_task():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F1", owner="Raj")]),
                         speaker_of=lambda a: "Raj")
    assert out[0].owner == "Raj" and rep["owners_changed"] == ["F1: None -> Raj"]


def test_a_later_duplicate_folds_into_the_earlier_one():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F3", duplicate_of="F1")]))
    assert len(out) == 3 and out[0].mentions == 2


def test_a_duplicate_of_a_different_kind_is_refused():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F3", duplicate_of="F2")]))
    assert len(out) == 4 and rep["rejected_proposals"]


def test_a_duplicate_pointing_forward_is_refused():
    out, _ = reconcile(ARTS, PEOPLE, chat=_model([_item("F1", duplicate_of="F3")]))
    assert len(out) == 4


def test_answered_is_a_status_never_a_deletion():
    """Measured: 'Let's not answer that in this meeting' was read as an answer."""
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F4", drop="answered")]))
    assert len(out) == 4 and rep["rejected_proposals"]


def test_someone_merely_mentioned_cannot_become_the_owner():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F1", owner="Priya")]))
    assert out[0].owner is None and "not a participant" in rep["rejected_proposals"][0]


def test_a_first_person_quote_keeps_its_speaker():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F1", owner="Sarah")]),
                         speaker_of=lambda a: "Raj")
    assert out[0].owner is None and "first-person" in rep["rejected_proposals"][0]


def test_a_due_date_nobody_said_is_refused():
    out, rep = reconcile(ARTS, PEOPLE, chat=_model([_item("F3", due="next Monday")]))
    assert out[2].due is None
    out, _ = reconcile(ARTS, PEOPLE, chat=_model([_item("F3", due="the 27th")]))
    assert out[2].due == "the 27th"


def test_it_cannot_add_a_finding():
    out, _ = reconcile(ARTS, PEOPLE, chat=_model([_item("F9", owner="Raj")]))
    assert [a.statement for a in out] == [a.statement for a in ARTS]


def test_insights_must_point_at_findings():
    _, rep = reconcile(ARTS, PEOPLE, chat=_model([], insights=[
        {"kind": "gap", "text": "real", "about": ["F4"]},
        {"kind": "gap", "text": "floating", "about": ["F77"]}]))
    assert [i["text"] for i in rep["insights"]] == ["real"]


def test_a_failed_pass_changes_nothing():
    def down(*a, **k):
        raise RuntimeError("quota")
    out, rep = reconcile(ARTS, PEOPLE, chat=down)
    assert out == ARTS and "quota" in rep["error"]


# -- a drop has to stand on its own (measured: every wrong drop came with a
#    refused cross-kind duplicate on the same finding) --------------------------

DECS = [
    Artifact("decision", "Cut over on Friday", quote="we cut over on Friday"),
    Artifact("action_item", "Rebuild the invoice export", owner="Omar",
             quote="I'll rebuild the invoice export by Wednesday"),
    Artifact("decision", "Cut over Monday the 3rd instead", quote="We cut over Monday the 3rd instead"),
    Artifact("risk", "Refunds untested", quote="nobody has tested refunds"),
]


def test_a_reversal_names_the_later_decision_that_replaced_it():
    out, rep = reconcile(DECS, ["Omar"], chat=_model([_item("F1", drop="reversed", reversed_by="F3")]))
    assert [a.statement for a in out][0] == "Rebuild the invoice export"
    assert rep["dropped"] == ["F1 (reversed by F3): Cut over on Friday"]


def test_a_reversal_by_nothing_later_is_refused():
    out, _ = reconcile(DECS, ["Omar"], chat=_model([_item("F3", drop="reversed", reversed_by="F1")]))
    assert len(out) == 4


def test_a_reversal_by_a_different_kind_is_refused():
    out, _ = reconcile(DECS, ["Omar"], chat=_model([_item("F2", drop="reversed", reversed_by="F3")]))
    assert len(out) == 4


def test_a_drop_riding_on_a_refused_duplicate_is_refused():
    out, rep = reconcile(DECS, ["Omar"], chat=_model(
        [_item("F2", duplicate_of="F1", drop="not_a_commitment")]))
    assert len(out) == 4 and any("refused duplicate" in r for r in rep["rejected_proposals"])


def test_risks_and_questions_are_never_dropped():
    out, _ = reconcile(DECS, ["Omar"], chat=_model([_item("F4", drop="declined")]))
    assert len(out) == 4
