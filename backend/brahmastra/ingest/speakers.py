"""
Name the voices in a diarized transcript before anything reads it -- ROADMAP item 11.

A transcript typed or exported from a meeting tool carries names: "Mei: I'll
update the roadmap". One produced from AUDIO by a diarizer carries labels:
"Speaker B: I'll update the roadmap", "SPEAKER_01: ...". Everything downstream
attributes by speaker -- an action item's owner, a decision's `decided_by`,
who `attended` -- so without names the meeting record would say "Speaker B
owns the roadmap", which is true of nobody in the organisation.

cocoindex's conversation example runs this as its own step before extraction:
map labels to names using the metadata and the conversation, substitute, and
leave an unrecognised speaker as "(Speaker A)" whose statements are kept but
not attributed. Adopted in that shape, with this system's usual order:

  1. EVIDENCE THAT NEEDS NO MODEL. A speaker introducing themselves in their
     own turn ("I'm Sarah", "Sarah here", "this is Sarah") is decisive.
  2. A MODEL, for the rest -- people are identified by being ADDRESSED
     ("Raj, you own reconciliation") far more often than by introducing
     themselves. Its answer is held to the transcript: the name must occur in
     it, the quoted evidence must be verbatim, and two voices can never be the
     same person. Anything that fails is dropped, not repaired.
  3. FAIL CLOSED. A voice nobody could name stays "(Speaker A)", and
     `is_anonymous` keeps it out of every owner and attendance edge. A missing
     owner is recoverable from the transcript; a wrong one is a false record.

Mentioned is not present: "is Priya joining? -- She's out until the 20th"
names someone who is not a voice at all, and the prompt says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable

# Labels a diarizer emits: Speaker A, Speaker 1, SPEAKER_00, spk_2, S1, Unknown Speaker.
_ANONYMOUS = re.compile(
    r"^\(?\s*(?:(?:unknown\s+)?speaker|spk|s)\s*[_\- ]?\s*(?:[a-z]|\d{1,2})?\s*\)?$",
    re.IGNORECASE)

_SELF_INTRO = re.compile(
    r"(?:\b(?:I'm|I am|this is|it's|my name is)\s+(?P<a>[A-Z][a-z]+)\b"
    r"|^\s*(?P<b>[A-Z][a-z]+)\s+here\b)")
# Capitalised words that follow "I'm" and are not names.
_NOT_NAMES = frozenset("""
    Not Sure Just Happy Here Going Also Really Sorry Afraid Glad Okay Fine Good
    Still Only Actually Honestly Worried Curious Thinking Looking Done Back Away
    Joining Late Ready On In Out With The A An So Now Talking Wondering
""".split())


def is_anonymous(label: str | None) -> bool:
    """A diarizer's placeholder rather than a person's name."""
    return bool(label) and bool(_ANONYMOUS.match(label.strip()))


def display_label(label: str) -> str:
    """How an unnamed voice is shown: '(Speaker A)', never mistaken for a name."""
    label = label.strip()
    return label if label.startswith("(") else f"({label})"


def _self_introductions(turns: list[Any], labels: list[str]) -> dict[str, str]:
    found: dict[str, set[str]] = {}
    for turn in turns:
        if turn.speaker not in labels:
            continue
        for m in _SELF_INTRO.finditer(turn.text or ""):
            name = m.group("a") or m.group("b")
            if name and name not in _NOT_NAMES:
                found.setdefault(turn.speaker, set()).add(name)
    # One clear name per voice, and no name claimed by two voices.
    single = {lab: next(iter(ns)) for lab, ns in found.items() if len(ns) == 1}
    claimed: dict[str, int] = {}
    for name in single.values():
        claimed[name] = claimed.get(name, 0) + 1
    return {lab: n for lab, n in single.items() if claimed[n] == 1}


_SCHEMA = {
    "type": "object",
    "properties": {"speakers": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "name": {"type": ["string", "null"]},
            "evidence": {"type": "string"},
        },
        "required": ["label", "name", "evidence"],
        "additionalProperties": False,
    }}},
    "required": ["speakers"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You identify the speakers in a meeting transcript whose voices were
labelled by software ("Speaker A", "SPEAKER_01") instead of by name.

For each label, give the person's first name ONLY when the transcript shows it:
someone addresses that speaker by name and the speaker answers or acts on it,
or the speaker says who they are. Quote, verbatim and short, the words that
show it.

Rules that matter more than finding a name:
  - Someone MENTIONED is not necessarily speaking. "Is Priya joining? -- She's
    out until the 20th" names a person who is not in the meeting.
  - Being addressed is evidence about the NEXT speaker only when they answer.
  - Two labels are never the same person.
  - When the transcript does not settle it, answer null. A wrong name is worse
    than no name: it puts words and commitments in someone else's mouth.

Return JSON: {"speakers": [{"label": "...", "name": "..." or null, "evidence": "..."}]}"""

MAX_TRANSCRIPT_CHARS = 12_000


def _ask_model(turns: list[Any], labels: list[str], known: dict[str, str],
               title: str, chat: Callable[..., str] | None = None) -> dict[str, str]:
    if chat is None:
        from brahmastra.llm import chat as chat
    body = "\n".join(f"{t.speaker or '?'}: {t.text}" for t in turns)[:MAX_TRANSCRIPT_CHARS]
    already = "\n".join(f"  {lab} = {name}" for lab, name in known.items()) or "  (none)"
    user = (f"Meeting: {title}\nLabels to identify: {', '.join(labels)}\n"
            f"Already identified from self-introductions:\n{already}\n\n"
            f"Transcript:\n{body}")
    raw = chat(SYSTEM_PROMPT, user, json_schema=_SCHEMA, temperature=0.0, max_tokens=800)
    payload = json.loads(raw)
    return {str(s.get("label", "")).strip(): (s.get("name") or "").strip()
            for s in payload.get("speakers") or []
            if isinstance(s, dict) and s.get("name")
            and _grounded(s, turns)}


def _grounded(answer: dict[str, Any], turns: list[Any]) -> bool:
    """The name occurs in the transcript, and so does the evidence, verbatim."""
    text = "\n".join(t.text or "" for t in turns)
    name = str(answer.get("name") or "").strip()
    evidence = " ".join(str(answer.get("evidence") or "").split())
    if not name or not re.search(rf"\b{re.escape(name)}\b", text):
        return False
    return bool(evidence) and evidence.lower() in " ".join(text.split()).lower()


def identify(turns: list[Any], title: str = "", use_model: bool = True,
             chat: Callable[..., str] | None = None) -> tuple[dict[str, str | None], dict[str, Any]]:
    """
    ({label: name or None} for every anonymous label, report).

    Named speakers are left alone; only diarizer labels are considered.
    """
    labels = sorted({t.speaker for t in turns if is_anonymous(t.speaker)})
    report: dict[str, Any] = {"anonymous": len(labels), "by_introduction": 0,
                              "by_model": 0, "unresolved": 0, "rejected": 0}
    if not labels:
        return {}, report

    named = {t.speaker for t in turns if t.speaker and not is_anonymous(t.speaker)}
    mapping: dict[str, str | None] = dict.fromkeys(labels)
    intro = _self_introductions(turns, labels)
    for lab, name in intro.items():
        if name not in named:
            mapping[lab] = name
    report["by_introduction"] = sum(1 for v in mapping.values() if v)

    rest = [lab for lab in labels if not mapping[lab]]
    if rest and use_model:
        try:
            proposed = _ask_model(turns, rest, {k: v for k, v in mapping.items() if v},
                                  title, chat)
        except Exception as exc:                          # noqa: BLE001
            report["model_error"] = f"{type(exc).__name__}: {exc}"[:200]
            proposed = {}
        taken = {v for v in mapping.values() if v} | named
        counts: dict[str, int] = {}
        for lab in rest:
            if proposed.get(lab):
                counts[proposed[lab]] = counts.get(proposed[lab], 0) + 1
        for lab in rest:
            name = proposed.get(lab)
            if not name:
                continue
            # Two voices the same person, or a voice given a name that already
            # speaks under its own label: the answer contradicts itself. Drop.
            if counts[name] > 1 or name in taken:
                report["rejected"] += 1
                continue
            mapping[lab] = name
            report["by_model"] += 1
    report["unresolved"] = sum(1 for v in mapping.values() if not v)
    report["mapping"] = {k: v for k, v in mapping.items()}
    return mapping, report


def apply(turns: list[Any], mapping: dict[str, str | None]) -> list[Any]:
    """Turns with each label replaced by its name, or by '(Speaker A)'."""
    out = []
    for t in turns:
        if t.speaker in mapping:
            name = mapping[t.speaker]
            out.append(replace(t, speaker=name or display_label(t.speaker)))
        else:
            out.append(t)
    return out
