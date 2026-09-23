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

RE-INGESTION REPLACES -- AND, NOW, REMOVES
------------------------------------------
Note ids are deterministic (`<transcript>-c<n>`), so running a transcript twice
corrects each note instead of doubling it. That was only ever half the
contract, and the missing half was measured:

    a 40-turn transcript    -> 19 chunks, 19 notes
    edited down to 4 turns  -> 1 chunk, 1 note, and 19 notes still in the graph

Chunks and artifacts shrank correctly, because `clear_derived` deletes from the
two tables beside it. The notes live in another store, reached through another
module, and nothing recorded that this transcript OWNED them -- so eighteen
notes stayed, holding triples, answering searches, sourced from sentences that
no longer exist anywhere.

All three now go through `brahmastra.ownership`: every note, chunk and artifact
a run produces is DECLARED, what changed is written, and what the last run
declared and this one did not is deleted. `clear_derived` is no longer on this
path at all -- deleting everything and rewriting it is not a reconciliation. It
leaves a hole for as long as the run takes, destroys rows that did not change,
and an interrupted run ends with nothing rather than with the older version.

The same rule covers a transcript deleted outright, in cocoindex's two shapes:
DESTROY takes the notes too, ABANDON leaves them and releases the claim. See
`ownership.py` for why the ledger records an intent before acting.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from brahmastra.ingest.comprehend import (
    ChunkUnderstanding,
    comprehend_chunk,
    comprehend_chunk_focused,
)
from brahmastra.ingest.consolidate import consolidate
from brahmastra.ingest.segment import Chunk, segment
from brahmastra.ingest.store import IngestStore, get_ingest_store
from brahmastra import ownership

# Headings used in the generated note. Plain words on purpose: the note is read
# by an extraction prompt, and prose beats a data structure there.
_SECTIONS = [
    ("decision", "Decisions"),
    ("action_item", "Action items"),
    ("risk", "Risks and blockers"),
    ("open_question", "Open questions"),
]


# Below this many billion parameters, the focused split stopped paying for
# itself: same recall as one pass, for twice the calls.
#
# Drawn between the two models actually measured -- 7B and 120B -- so it is a
# line through two points and should move the moment a third is measured.
# Defaulting an UNKNOWN model to `focused` therefore risks calls rather than
# correctness, which is the right way round for a guess to be wrong.
SMALL_MODEL_PARAMS_B = 30.0

# What a transcript's notes are marked as, and part of their fingerprint --
# retrieval weights a paragraph a model distilled from speech differently from
# prose a person wrote, so changing it changes the note and must rewrite it.
NOTE_SOURCE = "transcript"

# Who owns the notes, in the ledger. A second kind of source -- a code file, a
# dropped PDF -- gets its own owner_kind and the same three columns.
OWNER_KIND = "transcript"

_MODEL_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.IGNORECASE)


def model_params_b(name: str) -> float | None:
    """
    Billions of parameters, read off the model name, or None if it says nothing.

    Names are a convention rather than an interface -- "qwen2.5:7b-instruct",
    "openai/gpt-oss-120b" -- so this is a hint and is treated as one. The
    version number is skipped deliberately: "qwen2.5" must not read as 2.5B.
    """
    tail = name.rsplit("/", 1)[-1]
    tail = tail.split(":", 1)[-1] if ":" in tail else tail
    match = _MODEL_SIZE.search(tail)
    return float(match.group(1)) if match else None


def comprehension_strategy():
    """
    Which comprehension to run, decided from the model that will run it.

    Measured over two labelled cases, three runs each, RANGES not single runs
    (`--compare --runs 3`), because the first version of this table was six
    single runs and one of its claims did not survive being repeated:

                       single                    focused
                   recall        calls      recall        calls
      gpt-oss-120b  43% [14-64]    6        69% [64-79]    12
      qwen2.5:7b    19% [ 9-36]    6        24% [14-36]    12

    On the large model the ranges barely touch, so the split is a real gain:
    one pass asked to find four different things at once reliably drops risks
    and open questions, and asking twice with a narrower brief recovers them.
    It costs exactly double, which on a rate-limited tier is the trade.

    On the 7B the ranges sit on top of each other. Nothing here distinguishes
    the two, so the single pass wins on cost alone -- half the calls for a
    difference the measurement cannot see.

    WHAT THE EARLIER TABLE GOT WRONG, since it is the reason this one reports
    ranges: single runs had focused SCORING WORSE on the 7B, 36% down to 27%,
    and that was written up as "specialisation is not free competence, and a
    small model cannot use the narrower instruction". A tidy story, and false.

    Repeating it reversed the sign twice. Two runs each put single ahead,
    27 to 26; three runs each put focused ahead, 19 to 24. Every one of those
    numbers is inside the band the configuration produces run to run, so the
    ORDER of the two variants on this model is noise, and a single pair of runs
    will keep producing whichever answer is asked for. That is the argument for
    `--runs`: not that one run is imprecise, but that one run of each is enough
    to found an architecture on, and reads exactly like evidence while doing it.

    The choice keys on SIZE rather than on provider. "Local" is not the same
    claim as "small", and a 70B on someone's own hardware would be sent down
    the small-model path by a provider check for no reason. An unreadable model
    name gets `focused`, the better-measured of the two.

    Neither variant produced a single trap hit on either model across all of
    those runs -- nothing matching a statement the cases declare is NOT in the
    meeting -- which is the number that would have mattered most.

    INGEST_COMPREHEND_PASSES = focused | single overrides all of it.
    """
    choice = (os.environ.get("INGEST_COMPREHEND_PASSES", "") or "").strip().lower()
    if choice == "single":
        return comprehend_chunk
    if choice == "focused":
        return comprehend_chunk_focused

    try:
        from brahmastra.llm import active_model

        size = model_params_b(active_model())
    except Exception:
        size = None

    if size is not None and size < SMALL_MODEL_PARAMS_B:
        return comprehend_chunk
    return comprehend_chunk_focused


def _write_graph_record(notes: ownership.Streaming, transcript_id: str,
                        record: dict[str, Any], chunks: list[Chunk],
                        artifacts: list[Any], report: dict[str, Any]) -> int:
    """
    Write the meeting record note and its triples. Returns how many triples.

    Owned through the same `notes` stream as the chunk notes, so a transcript
    that stops producing a record -- emptied, or left with no artifacts --
    loses it at settle time, and an unchanged record costs nothing.

    Reported, never raised: the chunks are comprehended by now, and one failed
    write must not discard them. The intent is already in the ledger, so the
    next run redoes it.
    """
    import json

    from brahmastra import db
    from brahmastra.ingest import graph_record as gr

    try:
        meeting = gr.meeting_name(record["title"], record.get("occurred_at"))
        # Speakers come from segmentation, not from a model, so the attendance
        # edges are as deterministic as everything else in the record.
        participants = sorted({s for c in chunks for s in (c.speakers or [])})
        triples = gr.record_triples(meeting, participants, artifacts)
        if not triples:
            return 0
        body = gr.record_body(meeting, participants, artifacts)
        note_id = gr.record_note_id(transcript_id)
        fp = ownership.fingerprint(meeting, body,
                                   json.dumps(triples, sort_keys=True))
        if notes.begin(note_id, fp):
            db.upsert_note(note_id, meeting, body, mark_pending=False,
                           source=gr.SOURCE)
            db.delete_triples_for_note(note_id)
            for t in triples:
                t["source_note_id"] = note_id
            db.insert_triples(triples)
            db.mark_note_done(note_id)
            try:
                from brahmastra.pipeline import mark_dirty
                mark_dirty(f"meeting record {note_id}")
            except Exception:                          # noqa: BLE001
                pass
            notes.commit(note_id)
        return len(triples)
    except Exception as exc:                           # noqa: BLE001
        report["errors"].append(
            {"stage": "graph_record", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return 0


def _settle_chunks(owned: ownership.Streaming, store: IngestStore,
                   transcript_id: str, report: dict[str, Any]) -> int:
    """Delete the chunk rows a re-segmentation stopped producing."""
    try:
        return len(owned.finish(
            lambda keys: store.delete_chunks(transcript_id, [int(k) for k in keys])
        ).deletes)
    except Exception as exc:
        report["errors"].append(
            {"stage": "ownership", "error": f"chunks: {type(exc).__name__}: {exc}"[:300]})
        return 0


def _settle_artifacts(ledger: ownership.Ledger, store: IngestStore,
                      transcript_id: str, artifacts: list[Any],
                      report: dict[str, Any], force: bool) -> int:
    """
    Reconcile the artifact table against what this run found.

    A bulk `sync` rather than the streaming shape the notes and chunks use,
    because artifacts are only known at the END: consolidation needs the whole
    document, since chunks overlap and a decision in an overlap region is
    comprehended twice.

    Unchanged artifacts are genuinely skipped here. Their id is derived from
    the statement, so "the ledger says this row already holds exactly this" is
    a real answer rather than a guess -- and it means a re-run over an
    unedited meeting stops rewriting every decision it ever recorded.
    """
    from brahmastra.ingest.store import identify_artifacts

    identified = identify_artifacts(transcript_id, artifacts)
    declared = [
        ownership.Declared(
            aid,
            ownership.fingerprint(a.kind, a.statement, a.owner, a.due,
                                  a.rationale, a.quote, a.chunk_index,
                                  getattr(a, "mentions", 1),
                                  getattr(a, "superseded_by", None)),
            a,
        )
        for aid, a in identified
    ]
    try:
        decided = ownership.sync(
            ledger, OWNER_KIND, transcript_id, "artifact", declared,
            write=lambda items: store.save_artifacts(
                transcript_id, [d.payload for d in items]),
            delete=store.delete_artifacts,
            force=force,
        )
        report["artifacts_written"] = len(decided.upserts)
        report["artifacts_removed"] = len(decided.deletes)
    except Exception as exc:
        report["errors"].append(
            {"stage": "ownership",
             "error": f"artifacts: {type(exc).__name__}: {exc}"[:300]})
    return len(declared)


def drop_transcript(transcript_id: str, store: IngestStore | None = None,
                    purge_notes: bool = False) -> dict[str, Any]:
    """
    The transcript is gone. Settle what it owned, one way or the other.

    cocoindex names exactly these two shapes for a container that is no longer
    declared, and the distinction is not a preference -- it is about who can
    recreate the contents:

      DESTROY (`purge_notes=True`)  the notes go too. Coherent with every other
        path here: shortening a transcript already deletes the notes it stopped
        declaring, so deleting the whole thing should not be the one case that
        leaves nineteen behind.

      ABANDON (the default)  the notes stay, and this system RELEASES its claim
        on them. They become ordinary notes that nothing will ever rewrite or
        remove. The default, because deleting a transcript deletes the SOURCE:
        unlike a re-ingestion, nothing can recompute those notes afterwards,
        and CLAUDE.md's first rule is that source data is not owed the same
        treatment as derived data.

    Releasing the claim is not optional in either case. A ledger full of
    records naming owners that no longer exist is a slow leak, and worse, it is
    a lie about what the system is tracking.
    """
    from brahmastra import db

    store = store or get_ingest_store()
    ledger = ownership.Ledger(workspace=store.workspace)

    removed: dict[str, int] = {}
    if purge_notes:
        def remove(keys):
            for note_id in keys:
                db.delete_note(note_id)

        removed = ownership.drop_owner(ledger, OWNER_KIND, transcript_id,
                                       {"note": remove})
    ledger.forget_owner(OWNER_KIND, transcript_id)
    store.delete_transcript(transcript_id)
    return {"deleted": transcript_id, "notes_removed": removed.get("note", 0),
            "notes_kept": not purge_notes}


def _settle(notes: ownership.Streaming, report: dict[str, Any]) -> int:
    """
    Delete the notes this transcript owns and no longer declares.

    `db.delete_note` takes the note's triples with it, which is the whole
    point: an orphan note is not merely an extra row, it is a set of entities
    and relations asserting things about sentences that were deleted.

    Reported, never raised, and never SILENT. One note that will not delete
    must not fail a run that has already comprehended forty chunks -- and
    leaving it costs nothing, because the ledger still says this transcript
    owns it and the next run tries again. But a failure that nobody can see is
    how the quota outage turned into four invalid measurements, so it lands in
    `report["errors"]` rather than being swallowed into a zero.
    """
    from brahmastra import db

    def remove(keys):
        for note_id in keys:
            db.delete_note(note_id)

    try:
        return len(notes.finish(remove).deletes)
    except Exception as exc:
        report["errors"].append(
            {"stage": "ownership", "error": f"{type(exc).__name__}: {exc}"[:300]})
        return 0


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
    force: bool = False,
) -> dict[str, Any]:
    """
    Segment, comprehend and store one transcript.

    `force` rewrites every note even where the ledger can prove it is already
    current. The ledger only knows what THIS system last wrote, so a note
    deleted by hand looks unchanged to it; this is the way to say so.

    Reports rather than raises for anything a single chunk can cause. A
    transcript is many LLM calls on a rate-limited tier, and one failed chunk
    must not discard the other forty -- the same stance extraction takes toward
    a note that fails, so the outcome is a `partial` run rather than a lost
    document.
    """
    from brahmastra.workspace import reset_request_workspace, set_request_workspace

    # Bind the workspace for the WHOLE operation, not just for the store.
    #
    # This produced a real leak: `workspace=` reached get_ingest_store, so
    # transcripts and artifacts landed correctly, while db.upsert_note kept
    # using the ambient workspace -- so a transcript processed into `office`
    # wrote its NOTES into `default`. No error, no warning; exactly the shape
    # of the leak this system has had before, where a store built without its
    # workspace overwrote a note belonging to another graph.
    #
    # Binding here rather than asking callers to do it, because the callers who
    # got it right (the route, the CLI) did so incidentally and a fourth caller
    # would have had to know. A guarantee that depends on remembering is not one.
    target = workspace or (store.workspace if store is not None else None)
    token = set_request_workspace(target) if target else None
    try:
        return _process(transcript_id, store, target, on_progress, force)
    finally:
        if token is not None:
            reset_request_workspace(token)


def _process(
    transcript_id: str,
    store: IngestStore | None,
    workspace: str | None,
    on_progress: Callable[[dict[str, Any]], None] | None,
    force: bool = False,
) -> dict[str, Any]:
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
        # Split out because "19 notes" hid the whole bug. `notes` is what this
        # run declares, `notes_written` what it actually had to store, and
        # `notes_removed` what the last run left behind -- which was
        # unreportable before anything recorded ownership.
        "notes_written": 0,
        "notes_removed": 0,
        "graph_record": 0,
        "chunks_removed": 0,
        "artifacts_written": 0,
        "artifacts_removed": 0,
        "rejected": [],
        "degraded": [],
        "errors": [],
    }

    store.set_transcript_status(transcript_id, "processing")

    # EVERYTHING this transcript owns, opened before the first row is written
    # and settled after the last, so the run can answer "and what did the
    # PREVIOUS one leave here?"
    #
    # There used to be a `clear_derived` on this line: delete every chunk and
    # every artifact, then write them all again. That is not a reconciliation.
    # It leaves the transcript with nothing for as long as the run takes, it
    # destroys rows that did not change, and an interrupted run leaves a hole
    # rather than an older version. And it only ever covered the two tables
    # next door, which is how eighteen notes came to survive a re-ingestion.
    ledger = ownership.Ledger(workspace=store.workspace)
    notes = ownership.Streaming(ledger, OWNER_KIND, transcript_id, "note",
                                force=force)
    owned_chunks = ownership.Streaming(ledger, OWNER_KIND, transcript_id,
                                       "chunk", force=force)

    chunks = segment(record["content"])
    report["chunks"] = len(chunks)
    store.set_transcript_status(transcript_id, "processing", chunk_count=len(chunks))

    if not chunks:
        # A transcript emptied to nothing still OWNS whatever the last run
        # left in the graph, and this is the most extreme version of the bug
        # that made this module necessary: declaring no notes must delete every
        # note, not quietly leave the whole document behind.
        report["notes_removed"] = _settle(notes, report)
        report["chunks_removed"] = _settle_chunks(owned_chunks, store,
                                                  transcript_id, report)
        _settle_artifacts(ledger, store, transcript_id, [], report, force)
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

    pending_artifacts: list[Any] = []
    # Resolved once per document, so a run cannot change strategy halfway.
    comprehend = comprehension_strategy()

    for chunk in chunks:
        # Declared, then written UNCONDITIONALLY -- unlike a note, where the
        # ledger's "unchanged" answer is allowed to skip the write. A chunk row
        # carries status, summary and note_id, all of which this run is about
        # to overwrite anyway, so there is nothing to save by skipping it and a
        # stale status to gain. The declaration is what matters here: it is
        # what lets a re-segmentation that produces fewer chunks delete the
        # ones it stopped producing.
        owned_chunks.begin(str(chunk.index),
                           ownership.fingerprint(chunk.text, chunk.start_char,
                                                 chunk.end_char))
        store.save_chunk(
            transcript_id, chunk.index, chunk.text, chunk.speakers,
            chunk.start_time, chunk.end_time, chunk.start_char, chunk.end_char,
        )
        owned_chunks.commit(str(chunk.index))

        understanding = comprehend(chunk)

        if understanding.error:
            store.set_chunk_result(transcript_id, chunk.index, "error",
                                   error=understanding.error)
            report["errors"].append({"chunk": chunk.index, "error": understanding.error})
            if on_progress:
                on_progress({"chunk": chunk.index, "of": len(chunks), "ok": False})
            continue

        report["comprehended"] += 1
        # Accumulated, not saved yet. Consolidation needs the WHOLE document:
        # chunks overlap, so a decision in an overlap region is comprehended
        # twice, and saving per chunk would store it twice with no later
        # opportunity to notice. Artifacts are derived data, so holding them
        # until the end risks a re-run, never information.
        pending_artifacts.extend(understanding.artifacts)
        # Surfaced, not swallowed: what was rejected is the evidence that the
        # grounding check is doing something, and the first place to look when
        # a transcript yields less than expected.
        report["rejected"].extend(understanding.rejected)

        body = build_note_body(record["title"], understanding, chunk)
        note_id = None
        if body:
            note_id = note_id_for(transcript_id, chunk.index)
            part = f" — part {chunk.index + 1}" if len(chunks) > 1 else ""
            title = f"{record['title']}{part}"
            # Declared first, written second. `begin` records the intent and
            # answers whether the write is needed at all: a chunk whose note
            # is byte-for-byte what the ledger says is already stored costs
            # nothing, and -- more to the point -- is NOT re-marked pending, so
            # an unchanged chunk does not buy another round of extraction.
            if notes.begin(note_id, ownership.fingerprint(title, body, NOTE_SOURCE)):
                db.upsert_note(
                    note_id,
                    title,
                    body,
                    mark_pending=True,
                    # Recorded so retrieval can weight it later: a paragraph a
                    # model distilled from speech is not prose a person wrote.
                    source=NOTE_SOURCE,
                )
                notes.commit(note_id)
                report["notes_written"] += 1
            report["notes"] += 1

        # A chunk where one comprehension pass failed is NOT "done", and
        # calling it that is how a half-record passes for a whole one.
        #
        # `comprehend_chunk_focused` degrades on purpose: if the concerns pass
        # fails it still returns the commitments, because half a record beats
        # none. That is right, and it was invisible. A real ingestion here hit
        # the Groq daily cap on the second call and stored four decisions and
        # four action items with ZERO risks and ZERO open questions, reporting
        # `status: done, error: null` -- indistinguishable from a meeting that
        # genuinely raised no concerns. The only evidence lived in the report
        # returned by this function, which the HTTP path throws away because
        # processing runs as a background task.
        #
        # Only pass failures count. The rest of `rejected` is the grounding
        # check refusing ungrounded quotes, which is the system working.
        failures = [r for r in understanding.rejected if r.startswith("pass failed")]
        # Kept in the existing status vocabulary -- the table has a CHECK
        # constraint and the deployed Postgres already carries it, so widening
        # it is a migration on live data for a reporting field. The `error`
        # column carries the truth instead, and the route derives `complete`
        # from it so a caller does not have to know that.
        store.set_chunk_result(
            transcript_id, chunk.index, "done",
            summary=understanding.summary, note_id=note_id,
            error="; ".join(failures)[:300] if failures else None,
        )
        if failures:
            # DEGRADED, not failed, and the distinction is load-bearing: this
            # chunk produced artifacts, it is only missing the kinds the failed
            # pass was looking for. Counting it as a failure made a one-chunk
            # transcript report `status: error` while holding three decisions
            # and four action items -- understating the record as badly as the
            # silent `done` overstated it.
            report["degraded"].append({"chunk": chunk.index,
                                       "error": "; ".join(failures)[:300]})
        if on_progress:
            on_progress({"chunk": chunk.index, "of": len(chunks), "ok": True})

    # Reduce, then store. Without this the overlap that protects meaning at the
    # chunk boundary becomes duplication in the knowledge base -- three
    # decisions reported where one was made, and the error grows with the
    # document.
    #
    # Only the ARTIFACTS are consolidated. The per-chunk notes keep their
    # overlap deliberately: two notes asserting the same fact is ordinary
    # provenance in a knowledge graph, whereas three rows in a decisions table
    # is a false claim about how many decisions were taken.
    # Everything this transcript used to own and did not declare this time.
    # After the loop, because until the last chunk is comprehended the run does
    # not yet know what it declares -- and before the status is decided, so a
    # `partial` run still reports what it removed.
    reduced = consolidate(pending_artifacts)

    # The meeting record, declared straight into the graph from the verified
    # artifacts -- see ingest/graph_record.py for what the prose bridge lost.
    # BEFORE `_settle`, which is not optional: the record note is owned like
    # every other note this transcript writes, and settling first would find
    # last run's record undeclared and delete it as an orphan.
    report["graph_record"] = _write_graph_record(
        notes, transcript_id, record, chunks, reduced["artifacts"], report)

    report["notes_removed"] = _settle(notes, report)
    report["chunks_removed"] = _settle_chunks(owned_chunks, store,
                                              transcript_id, report)

    report["artifacts"] = _settle_artifacts(
        ledger, store, transcript_id, reduced["artifacts"], report, force)
    report["merged"] = reduced["merged"]
    report["superseded"] = reduced["superseded"]
    report["revisions"] = reduced["notes"]

    # Counted from the CHUNK failures only. `errors` also carries an ownership
    # failure now, and a one-chunk transcript whose cleanup failed would
    # otherwise satisfy `failed == len(chunks)` and be declared a total loss.
    chunk_errors = [e for e in report["errors"] if "chunk" in e]
    failed = len(chunk_errors)
    degraded = len(report["degraded"])
    if failed == len(chunks):
        report["status"] = "error"
        store.set_transcript_status(
            transcript_id, "error",
            error=f"every chunk failed; first: {chunk_errors[0]['error']}"[:400],
        )
    elif failed or degraded:
        report["status"] = "partial"
        # `done` with an error set, which the route reports as complete=false.
        # The report said partial and the stored row said done, so the only
        # caller that learned the truth was the one holding the return value --
        # and the HTTP path does not hold it, because processing runs in a
        # background task. Anyone polling saw a clean `done` over a record
        # missing half its findings.
        note = []
        if failed:
            note.append(f"{failed} of {len(chunks)} chunks failed")
        if degraded:
            note.append(f"{degraded} comprehended only in part")
        store.set_transcript_status(transcript_id, "done", error="; ".join(note))
    else:
        report["status"] = "ok"
        store.set_transcript_status(transcript_id, "done")

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report
