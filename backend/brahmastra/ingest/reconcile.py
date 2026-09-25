"""
A whole-meeting pass over the findings, after every chunk has been read.

EXPERIMENTAL (branch feat/transcript-extraction-test), measured before adopted.

WHERE THE IDEA COMES FROM. Shaan's meeting-scribe runs three tiers: a note per
slice of speech, then two passes that read those NOTES -- never the transcript --
and rebuild an overview and a work list from scratch each time. Rebuilding is
what lets a task gain an owner three minutes after it was mentioned, or close
when someone says "did that already". Reading notes instead of speech is what
keeps those passes small however long the meeting runs.

Brahmastra reads each chunk well and then does almost nothing at the level of
the whole meeting: consolidate() merges near-duplicates and records reversed
decisions, and that is all. An owner named in a later chunk than the task stays
lost; a task offered in one chunk and declined in the next survives; nobody
says which items are done, blocked, or urgent.

WHAT THIS KEEPS FROM BRAHMASTRA, which meeting-scribe does not have: every
output is held to the findings, and the findings to their quotes. The model
PROPOSES; code decides what may change:

  duplicate_of   only an EARLIER finding of the SAME kind
  drop           only with a reason from a fixed list
  owner          only a named participant -- and a first-person quote still
                 belongs to its speaker, whatever the model says
  due            only if its words occur in some finding's quote
  status, priority   fixed vocabularies
  insights       must point at existing findings; they add no finding

It cannot invent a finding. The worst it can do is drop or relabel one, and the
evaluation's TRAP and recall columns are what say whether it does.

MEASURED (2026-09-25/26, qwen3.8-27b, paired: the same chunk findings scored
without and with the pass, 3 runs x 3 labelled meetings):

                                      recall   precision   traps   owners
    small chunks, without             74%        52%         6     27/27
    small chunks, first version       70%        54%         3     27/27
    small chunks, grounded drops      74%        56%         3     27/27
    whole meeting in one chunk        58% / 58%  70% / 70%   3 / 3  unchanged

It removes a decision reversed in a LATER chunk -- "cut over Friday" after
"Friday is off, Monday the 3rd" -- which chunk-by-chunk reading cannot see: 3
of 3 runs, each naming the replacing decision. The first version also deleted
true items: every wrong drop came with a refused cross-kind duplicate on the
same finding, so drops now stand on their own (see _drop_refusal).

Owners named later: never exercised. Chunk reading already got 27 of 27,
because the sentence that assigns an owner restates the task. Status,
priority, the overview and insights are new OUTPUT, not scored.

Still owed before adoption: the same on gpt-oss-120b, the production model
(its daily quota was spent when this ran).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable

KINDS = ("decision", "action_item", "risk", "open_question")
STATUSES = ("open", "done", "blocked", "answered")
PRIORITIES = ("high", "normal", "low")
# "answered" is deliberately NOT a reason to drop. Measured on the first run:
# the pass dropped "Should legal review the Acme contract?" as answered, on the
# strength of "Probably, yes. Let's not answer that in this meeting" -- a real
# open question lost. An answered question is history, not an error; it gets a
# status instead.
DROP_REASONS = ("reversed", "declined", "not_a_commitment")
INSIGHT_KINDS = ("gap", "risk", "blocker", "dependency")

SYSTEM_PROMPT = """You review the findings extracted from ONE meeting, chunk by chunk, and
reconcile them into the meeting's final record. You never see the transcript -
only the findings, each with the words actually spoken as its quote.

Chunks were read separately, so the same thing may appear twice, an owner may
be named in a later chunk than the task, and something offered early may have
been declined or reversed later. Fix exactly that, and nothing else.

For EVERY finding, return one entry:
- "id": the finding's id, e.g. "F3".
- "duplicate_of": the id of an EARLIER finding of the same kind that states the
  same thing, else null. Different wording of one commitment is a duplicate;
  two different commitments by the same person are not.
- "drop": null, or a reason when the finding should not stand as it is:
    "reversed"          a LATER finding of the same kind replaced it - give
                        that finding's id as "reversed_by"
    "declined"          the person explicitly refused it ("I'm not committing")
    "not_a_commitment"  a wish or suggestion nobody actually took on
  Only decisions and action items can be dropped. Never drop a question or a
  risk - answered or discussed is a status, below. A finding that restates
  another in different words is a duplicate, not a reversal. When unsure,
  null. A wrong drop loses something true.
- "reversed_by": with "reversed", the id of the later finding; otherwise null.
- "owner": for action items, who is accountable, if any finding shows a named
  participant taking it or being given it; else the current owner or null.
  Never assign someone who was only mentioned, and never guess.
- "due": for action items, when it is due, in the meeting's own words
  ("Friday", "the 27th"); "" if nobody said.
- "status": "open", "done" (a finding says it already happened), "blocked"
  (a finding says it is waiting on something), or for a question "answered"
  (a later finding actually answers it - deferring it is NOT an answer).
- "priority": "high" (blocks someone, or a near named deadline), "normal", "low".

Then:
- "overview": {"headline": one sentence on what the meeting was about,
   "summary": a short paragraph: what prompted it, what was decided, what is
   still open. Only what the findings support.}
- "insights": things the meeting left hanging that no finding owns - a decision
   with no owner, a risk nobody took, a dependency nobody picked up. Each
   {"kind": "gap"|"risk"|"blocker"|"dependency", "text": "...",
    "about": ["F2", ...]}. Empty if everything was owned. Do not pad.

Return JSON only:
{"items": [...], "overview": {...}, "insights": [...]}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "duplicate_of": {"type": ["string", "null"]},
                "drop": {"type": ["string", "null"]},
                "reversed_by": {"type": ["string", "null"]},
                "owner": {"type": ["string", "null"]},
                "due": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["id", "duplicate_of", "drop", "reversed_by", "owner", "due",
                         "status", "priority"],
            "additionalProperties": False,
        }},
        "overview": {"type": "object", "properties": {
            "headline": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["headline", "summary"], "additionalProperties": False},
        "insights": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "text": {"type": "string"},
                           "about": {"type": "array", "items": {"type": "string"}}},
            "required": ["kind", "text", "about"], "additionalProperties": False}},
    },
    "required": ["items", "overview", "insights"],
    "additionalProperties": False,
}

_FIRST_PERSON = re.compile(r"\b(I|I'll|I will|I'm|I am|I can|me|my)\b", re.I)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


DROPPABLE = ("decision", "action_item")


def _drop_refusal(drop: str, a: Any, i: int, reversed_by: Any, dup_refused: bool,
                  ids: dict[str, int], kinds: list[str]) -> str | None:
    """Why a proposed drop is refused, or None if it may stand."""
    if drop not in DROP_REASONS:
        return f"drop reason {drop!r}"
    if a.kind not in DROPPABLE:
        return f"drop of a {a.kind} (only decisions and action items can be dropped)"
    if dup_refused:
        return "drop alongside a refused duplicate (a restatement, not a reversal)"
    if drop == "reversed":
        j = ids.get(str(reversed_by or ""))
        if j is None or j <= i or kinds[j] != a.kind:
            return f"reversed_by {reversed_by!r} is not a later finding of the same kind"
    return None



def render(artifacts: list[Any], participants: list[str], title: str) -> str:
    lines = [f"Meeting: {title}", f"Participants: {', '.join(participants) or 'unknown'}", "",
             "Findings, in the order they were said:"]
    for i, a in enumerate(artifacts, start=1):
        who = f", owner={a.owner}" if a.owner else ""
        due = f", due={a.due}" if a.due else ""
        when = f" at {a.start_time}" if a.start_time else ""
        lines.append(f"F{i} [{a.kind}]{when}{who}{due}: {a.statement}")
        if a.quote:
            lines.append(f'    quote: "{a.quote}"')
    return "\n".join(lines)


def reconcile(artifacts: list[Any], participants: list[str], title: str = "",
              chat: Callable[..., str] | None = None,
              speaker_of: Callable[[Any], str | None] | None = None,
              ) -> tuple[list[Any], dict[str, Any]]:
    """
    (reconciled artifacts, report). On any failure the artifacts come back
    unchanged and the report says why -- a pass that cannot run must not cost
    the meeting what the chunks already found.
    """
    report: dict[str, Any] = {"dropped": [], "duplicates": [], "owners_changed": [],
                              "rejected_proposals": [], "overview": {}, "insights": []}
    if not artifacts:
        return artifacts, report
    if chat is None:
        from brahmastra.llm import chat as chat
    try:
        raw = chat(SYSTEM_PROMPT, render(artifacts, participants, title),
                   json_schema=_SCHEMA, temperature=0.0, max_tokens=3000)
        payload = json.loads(raw)
    except Exception as exc:                                  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return artifacts, report

    ids = {f"F{i}": i - 1 for i in range(1, len(artifacts) + 1)}
    kinds = [a.kind for a in artifacts]
    people = {p.lower(): p for p in participants}
    quotes = " ".join(a.quote or "" for a in artifacts)
    quote_words = _words(quotes)

    out = list(artifacts)
    removed: set[int] = set()
    extra: dict[str, Any] = {}          # per index: status/priority, for the report

    def reject(fid: str, what: str) -> None:
        report["rejected_proposals"].append(f"{fid}: {what}")

    for item in payload.get("items") or []:
        fid = str(item.get("id") or "")
        if fid not in ids:
            continue
        i = ids[fid]
        a = out[i]

        dup = item.get("duplicate_of")
        dup_refused = False
        if dup:
            j = ids.get(str(dup))
            if j is None or j >= i or artifacts[j].kind != a.kind:
                reject(fid, f"duplicate_of {dup} (not an earlier finding of the same kind)")
                dup_refused = True
            else:
                removed.add(i)
                out[j] = replace(out[j], mentions=(out[j].mentions or 1) + (a.mentions or 1))
                report["duplicates"].append(f"{fid} -> {dup}")
                continue

        drop = item.get("drop")
        if drop:
            # Measured (3 runs x 3 cases): every wrong drop came with a refused
            # cross-kind duplicate on the SAME finding -- the model, told it
            # could not call a decision a duplicate of an action item, deleted
            # it as "reversed" instead. So a drop must stand on its own:
            # decisions and action items only, and a reversal names the later
            # finding of the same kind that replaced it.
            why = _drop_refusal(drop, a, i, item.get("reversed_by"), dup_refused, ids, kinds)
            if why is None:
                removed.add(i)
                by = f" by {item.get('reversed_by')}" if drop == "reversed" else ""
                report["dropped"].append(f"{fid} ({drop}{by}): {a.statement}")
                continue
            reject(fid, why)

        if a.kind == "action_item":
            owner = item.get("owner")
            if owner and owner != a.owner:
                name = people.get(str(owner).strip().lower())
                spoke = speaker_of(a) if speaker_of else None
                if not name:
                    reject(fid, f"owner {owner!r} is not a participant")
                elif spoke and a.quote and _FIRST_PERSON.search(a.quote) and name != spoke:
                    reject(fid, f"owner {owner!r} contradicts the first-person quote by {spoke}")
                else:
                    report["owners_changed"].append(f"{fid}: {a.owner} -> {name}")
                    out[i] = a = replace(a, owner=name)
            due = str(item.get("due") or "").strip()
            if due and due != (a.due or ""):
                if _words(due) and _words(due) <= quote_words:
                    out[i] = a = replace(a, due=due)
                else:
                    reject(fid, f"due {due!r} is not in any quote")

        status = str(item.get("status") or "open").lower()
        priority = str(item.get("priority") or "normal").lower()
        extra[fid] = {"status": status if status in STATUSES else "open",
                      "priority": priority if priority in PRIORITIES else "normal"}

    report["status"] = extra
    ov = payload.get("overview") or {}
    report["overview"] = {"headline": str(ov.get("headline") or "").strip(),
                          "summary": str(ov.get("summary") or "").strip()}
    for ins in payload.get("insights") or []:
        about = [x for x in (ins.get("about") or []) if x in ids]
        kind = str(ins.get("kind") or "gap").lower()
        text = str(ins.get("text") or "").strip()
        if text and about:
            report["insights"].append({"kind": kind if kind in INSIGHT_KINDS else "gap",
                                       "text": text, "about": about})
    return [a for k, a in enumerate(out) if k not in removed], report
