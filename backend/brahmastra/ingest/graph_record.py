"""
A meeting's decisions, actions, risks and questions -- written straight into the graph.

WHY, MEASURED. Every transcript chunk already becomes a prose note that
extraction reads ("Decisions: - The release moves to April 15th. Sarah is
accountable for it."). On the one meeting ingested so far, that bridge carried
the CONTENT well and the STRUCTURE badly:

                                        through the prose    written here
    content reaches the graph               15 / 16             16 / 16
    owner tied to their item                 5 / 13             13 / 13
    decision / risk / question kept          0 / 16             16 / 16
    evidence                            generated prose      the speaker's words

"Mei will update the roadmap" arrived as `Mei --related_to--> roadmap update`.
A decision to "revisit in planning for Q4" arrived as a FUNCTIONAL
`scheduled_for Q4` -- a date nobody set, and a false contradiction waiting for
the day a real one is.

The artifacts already carry everything that was lost -- kind, owner, verbatim
quote, all past the grounding check -- so this module declares them as graph
structure directly. Deterministic: no model call, nothing to drift run to run.

THE SHAPE, and where it came from. cocoindex's meeting example builds Meeting,
Task and Person nodes with ATTENDED, DECIDED and ASSIGNED_TO. Adopted because
it measured better here, not because it was theirs:

    <person>    --attended-->      <meeting>
    <decision>  --decided_by-->    <person>
    <action>    --assigned_to-->   <person>
    <risk>      --raised_by-->     <person>      raised it; does NOT own it
    <question>  --asked_by-->      <person>
    <item>      --discussed_in-->  <meeting>

The vocabulary is SYSTEM vocabulary (ontology.SYSTEM_RELATIONS): code writes
it, the extraction prompt never lists it, so no cached extraction is
invalidated and no model is invited to invent a decision.

WHAT IS LEFT OUT. A superseded item -- decided and then reversed later in the
same meeting -- is not written: the graph holds what the meeting ended on. The
reversal stays in the artifacts table, where `superseded_by` records it.

PURE. `record_triples` takes artifacts and returns triples; the only I/O is in
the caller, so every rule above is testable without a database.
"""

from __future__ import annotations

from typing import Any, Iterable

# The note that carries a transcript's meeting record. Its triples are written
# by code; extraction skips its `source` (extraction.CODE_WRITTEN_SOURCES).
SOURCE = "meeting-record"

# Artifact kind -> (entity type of the item, relation to the person behind it)
_BY_KIND = {
    "decision": ("decision", "decided_by"),
    "action_item": ("action_item", "assigned_to"),
    "risk": ("risk", "raised_by"),
    "open_question": ("question", "asked_by"),
}

# Node names are the statements themselves; a runaway one should not become a
# paragraph-long node label.
MAX_NAME = 160


def record_note_id(transcript_id: str) -> str:
    return f"{transcript_id}-record"


def meeting_name(title: str, occurred_at: str | None) -> str:
    """
    The meeting node's name. Dated when the date is known, so a weekly
    "Standup" is fifty meetings and not one node that every week's decisions
    pile onto.
    """
    title = (title or "Meeting").strip()
    day = (occurred_at or "").strip()[:10]
    return f"{title} ({day})" if day else title


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())[:MAX_NAME].strip()


def _field(artifact: Any, name: str) -> Any:
    if isinstance(artifact, dict):
        return artifact.get(name)
    return getattr(artifact, name, None)


def record_triples(meeting: str, participants: Iterable[str],
                   artifacts: Iterable[Any]) -> list[dict[str, Any]]:
    """Every triple the meeting record declares. Deterministic, ordered."""
    triples: list[dict[str, Any]] = []

    def add(subject, s_type, relation, obj, o_type, quote=""):
        triples.append({
            "subject_text": subject, "subject_type": s_type,
            "relation": relation,
            "object_text": obj, "object_type": o_type,
            # Verified by the grounding check before it became an artifact, and
            # written by code rather than inferred: nothing here is a guess.
            "confidence": 1.0,
            "source_quote": _clean(quote),
        })

    for person in sorted({_clean(p) for p in participants if _clean(p)}):
        add(person, "person", "attended", meeting, "meeting")

    for artifact in artifacts:
        kind = _field(artifact, "kind")
        if kind not in _BY_KIND or _field(artifact, "superseded_by"):
            continue
        statement = _clean(_field(artifact, "statement"))
        if not statement:
            continue
        item_type, to_person = _BY_KIND[kind]
        quote = _field(artifact, "quote") or ""
        add(statement, item_type, "discussed_in", meeting, "meeting", quote)
        owner = _clean(_field(artifact, "owner"))
        if owner:
            add(statement, item_type, to_person, owner, "person", quote)
    return triples


def record_body(meeting: str, participants: Iterable[str],
                artifacts: Iterable[Any]) -> str:
    """
    The note a person reads. Search finds it, and it says what the triples say
    -- each item with its owner and the words actually spoken.
    """
    lines = [f"Meeting record: {meeting}."]
    people = sorted({_clean(p) for p in participants if _clean(p)})
    if people:
        lines.append(f"Attended by {', '.join(people)}.")
    headings = {"decision": "Decisions", "action_item": "Action items",
                "risk": "Risks raised", "open_question": "Open questions"}
    who = {"decision": "decided by", "action_item": "assigned to",
           "risk": "raised by", "open_question": "asked by"}
    items = [a for a in artifacts if not _field(a, "superseded_by")]
    for kind, heading in headings.items():
        of_kind = [a for a in items if _field(a, "kind") == kind]
        if not of_kind:
            continue
        lines.append(f"\n{heading}:")
        for a in of_kind:
            line = f"- {_clean(_field(a, 'statement'))}"
            if _clean(_field(a, "owner")):
                line += f" ({who[kind]} {_clean(_field(a, 'owner'))})"
            if _field(a, "quote"):
                line += f' -- "{_clean(_field(a, "quote"))}"'
            lines.append(line)
    return "\n".join(lines).strip()
