"""
Where each action item stands when the meeting ends: open, done or blocked.

meeting-scribe's third tier carries a status on every task, and it is the one
part of it not yet here. Its way -- rebuild the whole task list from notes --
was tried in this repository as the reconcile pass and NOT adopted: on the
production model it had nothing to fix and its merges cost true items
(ingest/reconcile.py). So status is asked on its own, narrowly, and held to the
transcript like everything else:

  * one call per meeting, after consolidation, over the ACTION ITEMS only;
  * each item is shown with what was actually SAID about it -- its own quote
    and the raw passages that best match it (ingest/passages.py), read from
    the transcript, never from a summary;
  * "done" and "blocked" must quote the words that say so, and the quote must
    be in the transcript. Without that the item stays OPEN -- an item wrongly
    marked done disappears from someone's list, which is worse than one left
    open;
  * "blocked" carries what it is blocked on.

A statement of intent is not a completion: "they're going to be done by
Thursday" is open. And the status is where the meeting ENDED: blocked at the
start and unblocked during the meeting is open.

Fails soft: any error leaves every item open and says why in the report.
"""

from __future__ import annotations

import json
from typing import Any, Callable

STATUSES = ("open", "done", "blocked")
EVIDENCE_PASSAGES = 2
# Groq's per-minute window is shared by the prompt and the reply; items are
# asked about in batches small enough to stay well inside it.
BATCH = 8

PROMPT = """You decide where each action item from a meeting stands at the END of the
meeting. For each item you get its own quote and excerpts of what was said
about it.

Return JSON only:
{"items": [{"id": "A1", "status": "open" | "done" | "blocked",
            "evidence": "verbatim words from the excerpts that show it, or null",
            "blocked_on": "what it is waiting for, or null"}]}

RULES:
- "done": someone says it has ALREADY happened ("shipped yesterday", "it's
  finished", "I already sent it"). A plan or a promise ("will be done by
  Thursday", "I'll do it today") is NOT done -- it is open.
- "blocked": someone says it cannot proceed until something else happens,
  and nothing later in the excerpts unblocks it. If it was unblocked during the
  meeting, it is open.
- "open": everything else. When unsure, open.
- evidence is REQUIRED for done and blocked, copied word for word.
"""


def _evidence_for(item: Any, passages: list[Any]) -> list[str]:
    """The raw passages that best match this item, by the same hybrid search."""
    from brahmastra import hybrid

    if not passages:
        return []
    rows = [{"text": p.text, "embedding": None} for p in passages]
    try:
        from brahmastra.embeddings import embed

        vectors = embed([p.text for p in passages])
        if vectors:
            for row, v in zip(rows, vectors):
                row["embedding"] = hybrid.pack(v)
    except Exception:                                          # noqa: BLE001
        pass
    ranked = hybrid.rank(item.statement + " " + (item.quote or ""), rows, text=lambda r: r["text"])
    return [rows[i]["text"] for i, _ in ranked[:EVIDENCE_PASSAGES]]


def assign(artifacts: list[Any], turns: list[Any],
           chat: Callable[..., str] | None = None) -> tuple[list[Any], dict[str, Any]]:
    """
    (artifacts with `status` set on every action item, report). Pure except for
    the one model call per batch, which `chat` replaces in tests.
    """
    from dataclasses import replace

    from brahmastra.ingest.comprehend import quote_is_grounded
    from brahmastra.ingest.passages import passages as make_passages

    report: dict[str, Any] = {"done": [], "blocked": [], "refused": [], "error": None, "calls": 0}
    actions = [(i, a) for i, a in enumerate(artifacts) if a.kind == "action_item"]
    out = [replace(a, status="open") if a.kind == "action_item" else a for a in artifacts]
    if not actions:
        return out, report
    if chat is None:
        from brahmastra.ingest.comprehend import _cached_chat as chat

    source = "\n".join(t.text for t in turns)
    passages = make_passages(turns)
    for start in range(0, len(actions), BATCH):
        batch = actions[start:start + BATCH]
        blocks = []
        for n, (_, a) in enumerate(batch, start=1):
            said = "\n---\n".join(_evidence_for(a, passages))
            who = f" (owner: {a.owner})" if a.owner else ""
            blocks.append(f"A{n}{who}: {a.statement}\n  its quote: \"{a.quote or ''}\"\n"
                          f"  what was said around it:\n{said}")
        report["calls"] += 1
        try:
            raw = chat(PROMPT, "\n\n".join(blocks), json_mode=True, temperature=0.0,
                       max_tokens=min(4000, 1200 + 250 * len(batch)))
            payload = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as exc:                               # noqa: BLE001
            report["error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        for entry in payload.get("items") or []:
            try:
                n = int(str(entry.get("id", "")).lstrip("Aa")) - 1
                index, item = batch[n]
            except (ValueError, IndexError):
                continue
            status = str(entry.get("status") or "open").strip().lower()
            if status not in STATUSES or status == "open":
                continue
            evidence = str(entry.get("evidence") or "").strip()
            if not quote_is_grounded(evidence, source):
                report["refused"].append(f"{status} without words that say so: {item.statement[:60]!r}")
                continue
            blocked_on = str(entry.get("blocked_on") or "").strip() or None
            out[index] = replace(out[index], status=status, status_evidence=evidence,
                                 blocked_on=blocked_on if status == "blocked" else None)
            report[status].append(item.statement[:80])
    return out, report
