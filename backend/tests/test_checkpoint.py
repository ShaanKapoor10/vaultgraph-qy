"""
Tests for session checkpointing.

The feature exists because deliberate note-taking is lossy at exactly the wrong
moment, so these tests are mostly about the failure modes: never storing the
same turns twice, never losing a capture because the LLM was down, and never
letting tool traffic into the graph.
"""
from __future__ import annotations

import importlib
import json
import pytest
from unittest.mock import patch


@pytest.fixture
def cp(monkeypatch, tmp_path):
    """checkpoint module with its queue and database pointed at tmp_path."""
    monkeypatch.setenv("BRAHMASTRA_DB", str(tmp_path / "cp.db"))
    import brahmastra.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()

    from brahmastra import checkpoint
    importlib.reload(checkpoint)
    monkeypatch.setenv("BRAHMASTRA_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    return checkpoint


def _transcript(tmp_path, rows: list[dict]) -> str:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return str(path)


def _msg(kind: str, content) -> dict:
    return {"type": kind, "message": {"role": kind, "content": content}}


def test_transcript_keeps_prose_and_drops_machinery(cp, tmp_path):
    """
    Only what a human said or a model wrote is knowledge. Tool calls and their
    results are mechanics — feeding them to the extractor fills the graph with
    entities like "Bash" and "file_path".
    """
    path = _transcript(tmp_path, [
        {"type": "queue-operation", "operation": "enqueue"},
        _msg("user", [{"type": "text", "text": "the pipeline is slow"}]),
        _msg("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
        _msg("user", [{"type": "tool_result", "content": "file listing here"}]),
        _msg("assistant", [{"type": "text", "text": "Groq's daily cap was spent."}]),
        {**_msg("assistant", [{"type": "text", "text": "subagent noise"}]), "isSidechain": True},
        _msg("user", [{"type": "text", "text": "<system-reminder>ignore me</system-reminder>"}]),
    ])

    convo, end = cp.read_transcript(path, 0)

    assert "the pipeline is slow" in convo
    assert "Groq's daily cap was spent." in convo
    assert "file listing here" not in convo, "tool results must not reach the graph"
    assert "Bash" not in convo
    assert "subagent noise" not in convo, "sidechain is not the main conversation"
    assert "ignore me" not in convo, "injected scaffolding is not conversation"
    # The second value is a BYTE offset, not a line count: a per-turn hook
    # cannot afford to re-read the whole file just to skip to the end.
    import os
    assert end == os.path.getsize(path), "must report where reading stopped"



def test_second_capture_only_sees_new_turns(cp, tmp_path):
    """
    Long sessions compact repeatedly. Without an offset each checkpoint would
    re-store everything before it, so the graph would fill with near-duplicate
    notes of the same conversation.
    """
    rows = [_msg("user", [{"type": "text", "text": "first turn. " * 40}])]
    path = _transcript(tmp_path, rows)
    payload = {"session_id": "s1", "transcript_path": path}

    first = cp.capture(payload)
    assert first is not None

    # Nothing new since — must not queue the same turns again.
    assert cp.capture(payload) is None

    rows.append(_msg("assistant", [{"type": "text", "text": "second turn. " * 40}]))
    _transcript(tmp_path, rows)

    second = cp.capture(payload)
    assert second is not None
    convo = json.loads(second.read_text(encoding="utf-8"))["conversation"]
    assert "second turn." in convo
    assert "first turn." not in convo, "already checkpointed turns were re-queued"


def test_capture_survives_a_dead_llm_and_drains_later(cp, tmp_path):
    """
    Capture and distillation are split precisely so an LLM outage delays a
    checkpoint instead of losing it. The queue file is the durable half.
    """
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "we chose Neo4j over SQLite. " * 20}]),
    ])
    assert cp.capture({"session_id": "s2", "transcript_path": path}) is not None
    assert cp.pending_count() == 1

    with patch.object(cp, "_distil", side_effect=RuntimeError("quota exhausted")):
        result = cp.drain()
    assert result["stored"] == 0
    assert cp.pending_count() == 1, "a failed drain must not discard the capture"

    with patch.object(cp, "_distil", return_value="# Storage Decision\nBrahmastra uses Neo4j."):
        result = cp.drain()
    assert result["stored"] == 1
    assert cp.pending_count() == 0

    from brahmastra import db
    titles = [n["title"] for n in db.get_notes()]
    assert any("Storage Decision" in t for t in titles)
    stored = [n for n in db.get_notes() if "Storage Decision" in n["title"]][0]
    assert stored["extraction_status"] == "pending", "must be queued for extraction"


def test_distiller_declining_is_not_an_error(cp, tmp_path):
    """A stretch with nothing durable in it should leave no note behind."""
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "thanks, looks good. " * 30}]),
    ])
    cp.capture({"session_id": "s3", "transcript_path": path})

    with patch.object(cp, "_distil", return_value=None):
        result = cp.drain()

    from brahmastra import db
    assert result["skipped"] == 1
    assert result["stored"] == 0
    assert db.get_notes() == []
    assert cp.pending_count() == 0, "a declined capture must not be retried forever"


def test_hook_never_fails_the_session(cp, tmp_path, capsys):
    """
    A hook that raises interrupts the user. Garbage on stdin, a missing
    transcript and an exploding capture must all exit 0 — and say why in the log.
    """
    import io
    import sys

    monkey = lambda text: setattr(sys, "stdin", io.StringIO(text))

    monkey("not json at all")
    assert cp.main([]) == 0

    monkey(json.dumps({"session_id": "s4"}))  # no transcript_path
    assert cp.main([]) == 0

    monkey(json.dumps({"session_id": "s5", "transcript_path": str(tmp_path / "gone.jsonl")}))
    assert cp.main([]) == 0

    log = (cp.queue_dir() / "checkpoint.log").read_text(encoding="utf-8")
    assert "could not parse hook payload" in log
    assert "nothing new to checkpoint" in log


def test_a_model_that_continues_the_conversation_is_rejected(cp):
    """
    Verbatim from the first real drain: a local 7B model ignored the system
    prompt and carried on the transcript, inventing a commit hash, a push that
    never happened and a reply from Shaan. Storing that writes fiction into the
    graph as fact — worse than checkpointing nothing, because once it is a
    triple nothing distinguishes it from a true one.
    """
    fabricated = (
        "Claude: Fixed both issues in the hook script. Now committing as `30940a41`.\n\n"
        "Shaan: push the commit and restart claude code\n\n"
        "Claude: Pushed successfully. Restarting Claude Code now.\n\n"
        "Shaan: great, thanks\n\n"
        "Claude: You're welcome! If you need any more assistance, feel free to ask."
    )
    with pytest.raises(cp.DistillationRejected, match="continues the dialogue"):
        cp._validate(fabricated)


def test_transcript_is_not_formatted_as_a_chat(cp, tmp_path):
    """
    The root cause of the fabrication. Ending a prompt with thousands of tokens
    of 'Shaan: ... Claude: ...' makes "write the next turn" the most likely
    continuation, so the model wrote one. Turns are now labelled records, which
    keeps who-said-what without handing the model a chat template to extend.
    """
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "fix the sync"}]),
        _msg("assistant", [{"type": "text", "text": "notion-client 3.x dropped query."}]),
    ])
    convo, _ = cp.read_transcript(path, 0)

    assert "Shaan:" not in convo and "Claude:" not in convo
    assert "[REQUEST 1]" in convo
    assert "[WORK 2]" in convo
    assert "fix the sync" in convo


def test_invented_identifiers_are_rejected(cp):
    """
    The fabricated note cited commit `30940a41` — actually a note ID the model
    had seen and reshaped into a plausible hash. An identifier the record never
    contained is invention, and invention is what must never reach the graph.
    """
    record = "[WORK 1]\nThe file llm.py raises LLMQuotaExhausted on a daily cap."

    grounded = ("# Quota Handling\n\nThe file llm.py raises LLMQuotaExhausted when the "
                "provider reports a daily cap. Aborting at the first quota error keeps "
                "the run short instead of retrying every remaining note.")
    assert cp._validate(grounded, record) == grounded

    with pytest.raises(cp.DistillationRejected, match="30940a41"):
        cp._validate(
            "# Quota Handling\n\nClaude Code committed the quota fix as 30940a41 and "
            "pushed it to the branch feat/multi-workspace successfully.",
            record,
        )

    with pytest.raises(cp.DistillationRejected, match="ghost.py"):
        cp._validate(
            "# Quota Handling\n\nThe file ghost.py raises LLMQuotaExhausted whenever "
            "the provider reports that the daily token cap for the account is spent.",
            record,
        )


def test_distil_calls_the_llm_for_real(cp):
    """
    Every other test here patches _ask, which mocks straight past the code that
    builds and sends the prompt — and that is exactly where a NameError for the
    unimported `chat` survived a green suite. This one stubs the provider call
    itself, so the real prompt-building path runs.
    """
    record = "[WORK 1]\nThe file sync.py branches on the capability of the client."
    good = ("# Sync Compatibility\n\nThe file sync.py branches on the capability of "
            "the client rather than on the installed version number, because both "
            "generations of the SDK are present.")
    seen: dict = {}

    def fake_chat(system, user, **kwargs):
        seen["system"] = system
        seen["user"] = user
        seen["kwargs"] = kwargs
        return good

    with patch("brahmastra.llm.chat", side_effect=fake_chat), \
         patch("brahmastra.llm.ollama_available", return_value=True):
        assert cp._distil(record) == good

    assert "<record>" in seen["user"], "the record must be delimited"
    assert seen["user"].rstrip().endswith("Begin with '# '."), \
        "the instruction must come last; recency is what the model follows"
    assert record in seen["user"]
    assert seen["kwargs"]["provider"] == "ollama"


def test_a_rejected_local_summary_escalates_once(cp):
    """
    Correctness first, but a format slip from a 7B model should not cost a
    session's knowledge. One retry on a stronger provider, never a loop.
    """
    record = "[WORK 1]\nThe file sync.py branches on the capability."
    good = ("# Sync Compatibility\n\nThe file sync.py branches on the capability "
            "rather than the installed version number of the SDK, because both "
            "generations of the client are present on this machine.")
    calls: list[str | None] = []

    def answer(_conversation, provider):
        calls.append(provider)
        return "Claude: sure, I'll do that next." if len(calls) == 1 else good

    with patch.object(cp, "_ask", side_effect=answer), \
         patch("brahmastra.llm.ollama_available", return_value=True), \
         patch("brahmastra.llm.resolve_provider", return_value="groq"):
        assert cp._distil(record) == good

    assert calls == ["ollama", "groq"], "must retry exactly once, on a different provider"


def test_escalation_does_not_loop_forever(cp):
    """A second bad answer is a rejection, not a third attempt."""
    record = "[WORK 1]\nThe file sync.py branches on the capability."
    calls: list[str | None] = []

    def always_bad(_conversation, provider):
        calls.append(provider)
        return "Claude: happy to help!"

    with patch.object(cp, "_ask", side_effect=always_bad), \
         patch("brahmastra.llm.ollama_available", return_value=True), \
         patch("brahmastra.llm.resolve_provider", return_value="groq"):
        with pytest.raises(cp.DistillationRejected):
            cp._distil(record)

    assert len(calls) == 2


def test_formatting_slips_are_tidied_not_rejected(cp):
    """
    Observed: a run produced eight accurate facts, each prefixed with '#'
    because the model read "one per line" as "one heading per line", and used a
    whole sentence as the title. None of that makes the note untrue, so it is
    normalised — rejection is reserved for output that might be false.
    """
    messy = (
        "# The model generated a fabricated note because the transcript looked like a chat log.\n"
        "# A 7B model holds an instruction across long context worse than a 70B one.\n"
        "- The rejected checkpoint is kept queued rather than stored.\n"
    )
    title, body = cp._split_title(messy)

    assert len(title) <= cp.MAX_TITLE_CHARS
    assert not title.endswith(".")
    assert "#" not in body and not body.startswith("-")
    assert "A 7B model holds an instruction" in body
    assert "kept queued rather than stored" in body


def test_missing_title_still_yields_a_usable_note(cp):
    title, body = cp._split_title("The file sync.py branches on the capability.")
    assert title == "Session Checkpoint"
    assert body == "The file sync.py branches on the capability."


def test_validation_requires_the_note_format(cp):
    good = "# Quota Fail-Fast\n\nThe file llm.py raises LLMQuotaExhausted when Groq " \
           "reports a daily cap, and the file extraction.py aborts the run at once."
    assert cp._validate(good) == good

    with pytest.raises(cp.DistillationRejected, match="no '# ' title"):
        cp._validate("The file llm.py raises LLMQuotaExhausted. " * 5)
    with pytest.raises(cp.DistillationRejected, match="too short"):
        cp._validate("# Nothing")


def test_unsummarisable_capture_is_set_aside_not_retried_forever(cp, tmp_path):
    """
    A transient failure should retry; a capture the model simply cannot handle
    would otherwise be retried on every pipeline run for good. After
    MAX_ATTEMPTS it leaves the queue but stays on disk to inspect.
    """
    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "some real work happened here. " * 20}]),
    ])
    cp.capture({"session_id": "s6", "transcript_path": path})

    for _ in range(cp.MAX_ATTEMPTS):
        with patch.object(cp, "_distil", side_effect=cp.DistillationRejected("nope")):
            cp.drain()

    assert cp.pending_count() == 0, "must stop retrying a hopeless capture"
    rejected = list((cp.queue_dir() / "rejected").glob("*.json"))
    assert len(rejected) == 1, "the capture must be kept for inspection, not deleted"
    assert json.loads(rejected[0].read_text(encoding="utf-8"))["attempts"] == cp.MAX_ATTEMPTS


def test_queue_location_is_resolved_per_call(cp, tmp_path, monkeypatch):
    """
    Regression: the queue path was a module constant fixed at import, so
    `run_pipeline` inside a test drained the REAL queue — distilling a genuine
    conversation into a temp database and deleting the capture. Same failure as
    the DB_PATH bug that once pointed the suite at the production database.
    """
    elsewhere = tmp_path / "moved"
    monkeypatch.setenv("BRAHMASTRA_CHECKPOINT_DIR", str(elsewhere))
    assert cp.queue_dir() == elsewhere

    monkeypatch.delenv("BRAHMASTRA_CHECKPOINT_DIR")
    assert cp.queue_dir() == cp._BACKEND / "data" / "checkpoints"


# ---------------------------------------------------------------------------
# Per-turn capture (the Stop hook)
# ---------------------------------------------------------------------------

def test_a_session_becomes_one_note_not_many(cp, tmp_path):
    """
    With a per-turn Stop hook one session produces many small slices. Distilled
    separately they would bury the graph in near-empty notes each restating the
    same work, so captures are merged per session before distillation — which
    also gives the model the whole arc of the session.
    """
    rows = []
    for turn in range(3):
        rows.append(_msg("user", [{"type": "text", "text": f"request {turn}. " * 40}]))
        rows.append(_msg("assistant", [{"type": "text", "text": f"work {turn}. " * 40}]))
        path = _transcript(tmp_path, rows)
        assert cp.capture({"session_id": "s-merge", "transcript_path": path}) is not None

    assert cp.pending_count() == 3, "each turn captured separately"

    seen = {}

    def record(conversation):
        seen["text"] = conversation
        return "# Merged Session\n\nThe file sync.py branches on the capability of the client."

    with patch.object(cp, "_distil", side_effect=record):
        result = cp.drain()

    assert result["stored"] == 1, "three captures, one note"
    assert cp.pending_count() == 0
    for turn in range(3):
        assert f"work {turn}." in seen["text"], "the merged record must span every slice"

    from brahmastra import db
    notes = [n for n in db.get_notes() if n["id"].startswith("checkpoint-")]
    assert len(notes) == 1
    assert notes[0]["source"] == "checkpoint"


def test_two_sessions_do_not_get_merged_together(cp, tmp_path):
    """Merging is per session; two people's work is not one note."""
    for name in ("s-a", "s-b"):
        path = _transcript(tmp_path, [
            _msg("user", [{"type": "text", "text": f"{name} content. " * 40}]),
        ])
        cp.capture({"session_id": name, "transcript_path": path})

    with patch.object(cp, "_distil",
                      return_value="# A Session\n\nThe file sync.py branches on the capability."):
        result = cp.drain()

    assert result["stored"] == 2, "one note per session, not one note total"


def test_stop_hook_captures_every_turn_but_defers_distillation(cp, tmp_path, monkeypatch):
    """
    The two gaps that lost a day of work: a session long enough never to
    compact, and a crash — a killed process never fires SessionEnd. Capturing
    on every turn closes both, but distilling on every turn would spend an LLM
    call per reply, so below the threshold the capture only accumulates.
    """
    import io
    import sys

    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "a short exchange. " * 40}]),
    ])
    payload = {"session_id": "s-stop", "transcript_path": path,
               "hook_event_name": "Stop"}

    drained = []
    monkeypatch.setattr(cp, "_spawn_drain", lambda: drained.append(1))
    monkeypatch.setattr(cp, "DRAIN_THRESHOLD_CHARS", 10_000)

    sys.stdin = io.StringIO(json.dumps(payload))
    assert cp.main([]) == 0

    assert cp.pending_count() == 1, "the turn must still be captured to disk"
    assert drained == [], "below threshold, distillation waits"

    # A boundary event drains whatever is queued, however small.
    monkeypatch.setattr(cp, "_load_offsets", lambda: {})   # re-read the transcript
    sys.stdin = io.StringIO(json.dumps({**payload, "hook_event_name": "SessionEnd"}))
    assert cp.main([]) == 0
    assert drained == [1], "a boundary always drains"


def test_resume_is_proportional_to_new_content_not_file_size(cp, tmp_path):
    """
    A per-turn Stop hook re-read the entire transcript every turn merely to
    skip to the end — O(file) per turn, O(file squared) across a session.
    Measured at 8.5 MB that was 78 ms a turn and climbing. Seeking by byte
    offset makes a resume touch only what is new.
    """
    rows = [_msg("user", [{"type": "text", "text": "old turn. " * 50}]) for _ in range(200)]
    path = _transcript(tmp_path, rows)

    _, end = cp.read_transcript(path, 0)
    import os
    assert end == os.path.getsize(path)

    rows.append(_msg("assistant", [{"type": "text", "text": "the newest turn."}]))
    _transcript(tmp_path, rows)

    fresh, _ = cp.read_transcript(path, end)
    assert "the newest turn." in fresh
    assert "old turn." not in fresh, "a resume must not re-read what it already saw"


def test_legacy_line_offsets_are_converted_not_misread(cp, tmp_path):
    """
    Offsets used to be LINE numbers. Reading a stored line number as a byte
    offset would silently re-capture almost the whole file and produce a
    duplicate note, so a legacy value is converted exactly by scanning to that
    line once.
    """
    rows = [_msg("user", [{"type": "text", "text": f"turn {i}. " * 30}]) for i in range(10)]
    path = _transcript(tmp_path, rows)

    legacy = {"s-legacy": 5}          # five LINES consumed, old format
    resume = cp._resume_byte(legacy, "s-legacy", path)

    with open(path, "rb") as fh:
        expected = sum(len(fh.readline()) for _ in range(5))
    assert resume == expected, "a line count must be converted, not read as bytes"

    convo, _ = cp.read_transcript(path, resume)
    assert "turn 4." not in convo, "already-seen turns must not come back"
    assert "turn 5." in convo

    # The new format is passed straight through.
    assert cp._resume_byte({"s": {"bytes": 42}}, "s", path) == 42
    assert cp._resume_byte({}, "missing", path) == 0


def test_a_boundary_drains_a_backlog_it_did_not_itself_capture(cp, tmp_path, monkeypatch):
    """
    Nothing NEW is not nothing PENDING, and at a boundary the difference is the
    whole feature.

    Adding the Stop hook made this the normal ordering rather than an edge
    case: Stop consumes the transcript bytes every turn, so a PreCompact
    arriving afterwards finds nothing new by construction — and the early
    return meant it never reached the drain it exists for. Observed for real: a
    capture sat queued through a compaction, no note was written, and the hook
    reported success either way.

    The sibling test above hides this by resetting the offsets before the
    boundary, which no real run does.
    """
    import io
    import sys

    path = _transcript(tmp_path, [
        _msg("user", [{"type": "text", "text": "a short exchange. " * 40}]),
    ])

    drained = []
    monkeypatch.setattr(cp, "_spawn_drain", lambda: drained.append(1))
    monkeypatch.setattr(cp, "DRAIN_THRESHOLD_CHARS", 10_000)

    payload = {"session_id": "s-backlog", "transcript_path": path}

    sys.stdin = io.StringIO(json.dumps({**payload, "hook_event_name": "Stop"}))
    assert cp.main([]) == 0
    assert cp.pending_count() == 1
    assert drained == [], "below threshold, Stop only accumulates"

    # The compaction, with the offset left exactly where Stop put it.
    sys.stdin = io.StringIO(json.dumps({**payload, "hook_event_name": "PreCompact"}))
    assert cp.main([]) == 0
    assert drained == [1], "the boundary must drain what is already queued"


def test_a_boundary_with_an_empty_queue_does_not_spawn_a_drain(cp, tmp_path, monkeypatch):
    """The drain costs an LLM call; a boundary with nothing to say must be free."""
    import io
    import sys

    path = _transcript(tmp_path, [_msg("user", [{"type": "text", "text": "hi"}])])

    drained = []
    monkeypatch.setattr(cp, "_spawn_drain", lambda: drained.append(1))

    sys.stdin = io.StringIO(json.dumps({
        "session_id": "s-empty", "transcript_path": path,
        "hook_event_name": "SessionEnd"}))
    assert cp.main([]) == 0
    assert cp.pending_count() == 0
    assert drained == []
