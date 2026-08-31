"""
Run a transcript through the whole thing, and connect it to what already works.

    transcript -> segment -> comprehend -> { artifacts, notes }
                                              |         |
                                              |         `-> EXISTING pipeline
                                              |             extract -> resolve
                                              |             -> graph -> /ask
                                              `-> queried directly

THE NOTE IS THE BRIDGE
----------------------
Artifacts are stored as typed rows because the ontology has no vocabulary for
them -- no `decided`, no `action_item`, no `attended` -- so pushing them
through extraction alone would degrade every one to `related_to`.

But they must still reach the graph, or a decision is queryable only if you
already know to look in the artifacts table. So each chunk becomes a NOTE whose
body is deliberately entity-rich prose: the summary, then the decisions and
commitments written as plain subject-verb-object sentences. Extraction then
does what it is good at and yields "Sarah owns the comms", "the release is
scheduled for April 15th" -- real triples, from real sentences, using relations
that already exist.

That is also the disciplined way to grow the vocabulary. ONTOLOGY_DESIGN.md is
explicit that relations are added from observed coercions rather than in
anticipation; feeding meetings through this path produces exactly that
evidence, and `decided`/`attended` can then be added because the data asked
for them rather than because it seemed likely.

RE-INGESTION REPLACES
---------------------
Derived rows are cleared first and note ids are deterministic
(`<transcript>-c<n>`), so running a transcript twice corrects it instead of
doubling it -- the same contract extraction has when it deletes a note's
triples before re-inserting them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from brahmastra.ingest.comprehend import ChunkUnderstanding, comprehend_chunk
from brahmastra.ingest.segment import Chunk, segment
from brahmastra.ingest.store import IngestStore, get_ingest_store

# Headings used in the generated note. Plain words on purpose: the note is read
# by an extraction prompt, and prose beats a data structure there.
_SECTIONS = [
    ("decision", "Decisions"),
    ("action_item", "Action items"),
    ("risk", "Risks and blockers"),
    ("open_question", "Open questions"),
]


def note_id_for(transcript_id: str, chunk_index: int) -> str:
    """Deterministic, so a second ingestion replaces rather than duplicates."""
    return f"{transcript_id}-c{chunk_index}"


def build_note_body(title: str, understanding: ChunkUnderstanding,
                    chunk: Chunk) -> str:
    """
    Compose the prose that carries this chunk into the graph.

    Written as full sentences with named subjects because that is what
    extraction can read. "Owner: Mei" yields nothing; "Mei will update the
    roadmap by Friday" yields a person, an action and a date.
    """
    lines: list[str] = []
    if understanding.summary:
        lines.append(understanding.summary)

    when = ""
    if chunk.start_time:
        when = f" (from {chunk.start_time}"
        when += f" to {chunk.end_time})" if chunk.end_time else ")"
    if understanding.participants:
        speakers = ", ".join(understanding.participants)
        lines.append(f"This part of {title}{when} involved {speakers}.")

    for kind, heading in _SECTIONS:
        items = [a for a in understanding.artifacts if a.kind == kind]
        if not items:
            continue
        lines.append(f"\n{heading}:")
        for a in items:
            lines.append(f"- {_sentence_for(a, kind)}")

    return "\n".join(lines).strip()


def _sentence_for(artifact: Any, kind: str) -> str:
    """
    One artifact as a sentence a person would write.

    The owner's ROLE differs by kind and saying it wrongly puts a falsehood in
    the knowledge base: whoever raised a risk is not accountable for it, and
    whoever asked a question has not been assigned it. `comprehend` already
    collects the owner as "who raised it" and "who asked" for those two kinds,
    so the phrasing here has to match what was actually captured.
    """
    body = artifact.statement.strip()
    # Strip any terminator before composing; re-added at the end. Otherwise a
    # question keeps its "?" and picks up a second full stop -- "...the slip?."
    body = body.rstrip(".!?").strip()

    if kind == "action_item":
        if artifact.owner:
            body = f"{artifact.owner} will {body[0].lower()}{body[1:]}"
        if artifact.due:
            body += f", due {artifact.due}"
    elif kind == "decision":
        if artifact.owner:
            body = f"{body}. {artifact.owner} is accountable for it"
        if artifact.due:
            body += f", by {artifact.due}"
    elif kind == "risk":
        if artifact.owner:
            body = f"{body}. {artifact.owner} raised it"
    elif kind == "open_question":
        # Kept as a question, because that is what it is -- and the question
        # mark is the only thing marking it unresolved once it is prose.
        return f"{body}?" + (f" ({artifact.owner} asked it.)" if artifact.owner else "")

    if artifact.rationale:
        rationale = artifact.rationale.strip().rstrip(".")
        body += f", because {rationale[0].lower()}{rationale[1:]}"

    return f"{body}."


def process_transcript(
    transcript_id: str,
    store: IngestStore | None = None,
    workspace: str | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """
    Segment, comprehend and store one transcript.

    Reports rather than raises for anything a single chunk can cause. A
    transcript is many LLM calls on a rate-limited tier, and one failed chunk
    must not discard the other forty -- the same stance extraction takes toward
    a note that fails, so the outcome is a `partial` run rather than a lost
    document.
    """
    store = store or get_ingest_store(workspace)
    started = datetime.now(timezone.utc).isoformat()

    record = store.get_transcript(transcript_id)
    if record is None:
        return {"status": "error", "error": f"no transcript {transcript_id!r}",
                "transcript_id": transcript_id}

    report: dict[str, Any] = {
        "transcript_id": transcript_id,
        "title": record["title"],
        "started_at": started,
        "chunks": 0,
        "comprehended": 0,
        "artifacts": 0,
        "notes": 0,
        "rejected": [],
        "errors": [],
    }

    store.set_transcript_status(transcript_id, "processing")
    # A second run corrects the first rather than doubling it.
    store.clear_derived(transcript_id)

    chunks = segment(record["content"])
    report["chunks"] = len(chunks)
    store.set_transcript_status(transcript_id, "processing", chunk_count=len(chunks))

    if not chunks:
        store.set_transcript_status(transcript_id, "done", chunk_count=0)
        report["status"] = "ok"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        return report

    from brahmastra import db

    # This stage WRITES notes, so it owns making sure it can.
    #
    # Every test passed without this and the first real run died on "no such
    # table: notes". The API path happened to work because the app's lifespan
    # calls init_db() at startup, so the CLI -- the entry point that has no
    # lifespan -- was the only caller relying on somebody else having done it.
    # Idempotent, and cheap next to the LLM calls that follow.
    db.init_db()

    for chunk in chunks:
        store.save_chunk(
            transcript_id, chunk.index, chunk.text, chunk.speakers,
            chunk.start_time, chunk.end_time, chunk.start_char, chunk.end_char,
        )

        understanding = comprehend_chunk(chunk)

        if understanding.error:
            store.set_chunk_result(transcript_id, chunk.index, "error",
                                   error=understanding.error)
            report["errors"].append({"chunk": chunk.index, "error": understanding.error})
            if on_progress:
                on_progress({"chunk": chunk.index, "of": len(chunks), "ok": False})
            continue

        report["comprehended"] += 1
        report["artifacts"] += store.save_artifacts(transcript_id, understanding.artifacts)
        # Surfaced, not swallowed: what was rejected is the evidence that the
        # grounding check is doing something, and the first place to look when
        # a transcript yields less than expected.
        report["rejected"].extend(understanding.rejected)

        body = build_note_body(record["title"], understanding, chunk)
        note_id = None
        if body:
            note_id = note_id_for(transcript_id, chunk.index)
            part = f" — part {chunk.index + 1}" if len(chunks) > 1 else ""
            db.upsert_note(
                note_id,
                f"{record['title']}{part}",
                body,
                mark_pending=True,
                # Recorded so retrieval can weight it later: a paragraph a
                # model distilled from speech is not prose a person wrote.
                source="transcript",
            )
            report["notes"] += 1

        store.set_chunk_result(transcript_id, chunk.index, "done",
                               summary=understanding.summary, note_id=note_id)
        if on_progress:
            on_progress({"chunk": chunk.index, "of": len(chunks), "ok": True})

    failed = len(report["errors"])
    if failed == len(chunks):
        report["status"] = "error"
        store.set_transcript_status(
            transcript_id, "error",
            error=f"every chunk failed; first: {report['errors'][0]['error']}"[:400],
        )
    elif failed:
        report["status"] = "partial"
        store.set_transcript_status(transcript_id, "done",
                                    error=f"{failed} of {len(chunks)} chunks failed")
    else:
        report["status"] = "ok"
        store.set_transcript_status(transcript_id, "done")

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report
