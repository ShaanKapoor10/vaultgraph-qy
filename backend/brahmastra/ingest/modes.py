"""
What kind of session a transcript is, and so what is worth taking from it.

A meeting and a lecture are both people talking, and they are read for
different things. From a meeting: what was DECIDED, who committed to what, what
could go wrong, what is still open. From a lecture: what was TAUGHT, what the
audience asked and how it was answered, and the few real follow-ups.

Reading a lecture as a meeting is not a smaller version of the right answer.
It is noise. Measured on a real training session read in meeting mode: 15 of
its 22 items were risks that were really concepts being taught ("counterparty
risk"), the check-in "Is this all making sense?" became an open question, and
what was actually taught -- who pays fixed and who pays floating in a swap --
appeared in no item at all.

WHAT EVERY MODE SHARES, taken from meeting-scribe: each part of the transcript
is first written up as a TOPIC plus KEY POINTS carrying the substance (names,
numbers, reasons), and once every part is read, an OVERVIEW is written from
those notes (headline, summary, themes), never from the transcript. Unlike
meeting-scribe, every point must quote the words it came from, and a point
whose quote is not in the transcript is dropped, as items always have been.

WHAT A MODE DECIDES: which item kinds it extracts, with which prompts, and how
each kind is labelled on screen. Adding a mode is an entry here plus its
prompts in comprehend.py, not a change to the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MODE = "meeting"


@dataclass(frozen=True)
class Kind:
    """One kind of item a mode extracts, as the UI shows it."""
    id: str
    label: str          # section heading
    person: str         # how the person attached to it is described


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    description: str
    kinds: tuple[Kind, ...]
    # The kind that holds the notes pass's key points, if the mode shows them
    # as items (a lecture's points ARE what was taught). None: points are shown
    # with their part only.
    points_kind: str | None = None
    points_label: str = "Key points"
    examples: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kinds": [{"id": k.id, "label": k.label, "person": k.person} for k in self.kinds],
            "points_kind": self.points_kind,
            "points_label": self.points_label,
            "examples": list(self.examples),
        }


MODES: dict[str, Mode] = {
    "meeting": Mode(
        id="meeting",
        label="Meeting",
        description="People deciding and dividing up work: decisions, action items, risks, open questions.",
        kinds=(
            Kind("decision", "Decisions", "decided by"),
            Kind("action_item", "Action items", "owner"),
            Kind("risk", "Risks", "raised by"),
            Kind("open_question", "Open questions", "asked by"),
        ),
        points_label="Discussion points",
        examples=("planning", "standup", "incident review", "project sync"),
    ),
    "lecture": Mode(
        id="lecture",
        label="Lecture or training",
        description="Someone teaching: the concepts taught, questions the audience asked with their answers, and follow-ups.",
        kinds=(
            Kind("point", "What was taught", "explained by"),
            Kind("question", "Questions from the audience", "asked by"),
            Kind("action_item", "Follow-ups", "owner"),
        ),
        points_kind="point",
        points_label="What was taught",
        examples=("class", "training", "webinar", "walkthrough", "demo"),
    ),
}


def get_mode(mode_id: str | None) -> Mode:
    """The mode by id. An unknown or empty id is the default, never an error:
    transcripts stored before modes existed have none, and were meetings."""
    return MODES.get((mode_id or "").strip().lower(), MODES[DEFAULT_MODE])


def is_mode(mode_id: str | None) -> bool:
    return (mode_id or "").strip().lower() in MODES
