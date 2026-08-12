# Session Checkpointing — Design

**Status:** shipped on `feat/multi-workspace` (`da89d17`, `c734fba`, `4544df0`, `f47b1ce`, `d29cace`)
**Code:** `backend/brahmastra/checkpoint.py` · **Tests:** `backend/tests/test_checkpoint.py` (14) + `backend/tests/conftest.py`
**Hooks:** `.claude/settings.json` → `PreCompact`, `SessionEnd`

Turns a Claude Code conversation into a note before the context window discards it.

---

## 1. Why this exists

`CLAUDE.md` already instructs the assistant to call `brahmastra_add_note` every turn.
That instruction **failed in practice**: in one session the workspace migration got
stored and the quota fix immediately after it did not.

The structural reason matters more than the lapse. **Compaction happens between turns.**
Auto-compact fires without giving the assistant a turn, so nothing ever asks "anything to
save first?". Judgement-based storing therefore requires the assistant to predict when it
is about to run out of context — which it cannot observe. The one moment memory is
guaranteed to be lost is the one moment it has no agency.

So the trigger moves off memory and onto a hook.

### This is a net, not a replacement

Deliberate notes remain primary. They are written while the author still knows which
detail mattered and why; a distillation is a reconstruction by something that was not
there. The distiller is given the titles already in the graph and told to skip what they
cover, so it fills gaps rather than writing a second, worse copy.

**A checkpoint note appearing where a deliberate one should have been is evidence a turn
was missed.**

---

## 2. Architecture — two phases, deliberately split

```
PreCompact / SessionEnd hook
        │
        ▼
   capture()  ── reads transcript, writes queue file      pure file I/O, ~ms
        │                                                  CANNOT fail the session
        ├── spawns detached ──► drain()
        │                          │
   (queue file on disk) ◄──────────┤ distils, validates, stores note (pending)
                                   │ on failure: file stays queued
   run_pipeline() ──► drain() ─────┘ (backlog is drained before extract)
```

**Why split.** The hook runs inside the user's compaction path. If distillation happened
there, a dead Ollama or a rate-limited Groq would stall compaction. Capture touches no
network and cannot block. Distillation is the part allowed to fail, and the queue file is
the durable half that survives until it succeeds.

**Why `run_pipeline` also drains.** The detached drain is best-effort. Draining before
the extract stage means a checkpoint taken while every provider was down still lands, and
lands early enough to be graphed in the same run.

### Files on disk

| Path | Purpose |
|---|---|
| `backend/data/checkpoints/*.json` | queued captures awaiting distillation |
| `backend/data/checkpoints/.offsets.json` | per-session line offset, so repeated compactions never re-store the same turns |
| `backend/data/checkpoints/checkpoint.log` | why a checkpoint did not happen |
| `backend/data/checkpoints/rejected/` | captures abandoned after `MAX_ATTEMPTS`, kept for inspection |

The whole directory is gitignored — it holds raw conversation transcripts, the most
sensitive thing on disk after `.env`.

---

## 3. The trust model: fail closed

**A missing note is recoverable. A false note is not.**

Once a note is extracted, its triples are indistinguishable from triples that came out of
a real Notion page. There is no provenance marker saying "a 7B model guessed this".
Worse, `/ask` cites notes, so a fabricated fact is returned *with a citation*, and
contradiction detection has no basis for preferring the true one over the false one.

Therefore every check in `_validate()` fails closed: rejection stores nothing and leaves
the capture queued. Storing less is always the correct trade.

**Cosmetic problems are normalised, not rejected.** A formatting slip does not make a note
untrue, and discarding eight accurate facts because the model prefixed each with `#`
throws away real knowledge. Rejection is reserved for output that might be false.

---

## 4. Defences, and the real failure each one came from

Each of these was added in response to something that actually happened, not in
anticipation.

### 4.1 Transcript is not formatted as a chat — **the root cause**

`read_transcript` originally labelled turns `Shaan:` and `Claude:`. That is a chat
template. The prompt ended with ~10k tokens of perfectly regular alternating dialogue, so
the single most likely continuation was *more dialogue* — and the model produced it:

```
Claude: Pushing 30940a41 to feat/multi-workspace.
Pushed successfully.
Shaan: great, thanks
```

Entirely invented. `30940a41` was a **note ID** seen earlier in the conversation, reshaped
into a plausible commit hash. Turns are now labelled `[REQUEST n]` / `[WORK n]`, which
keeps attribution without leaving a pattern to extend.

Everything below is defence in depth behind this fix.

### 4.2 Instruction repeated *after* the record

The failure is recency-driven: a 150-token system prompt cannot compete with 10k tokens of
content immediately before the generation point. The record is wrapped in `<record>` and
followed by `DISTIL_REMINDER`, so the last thing the model reads is the instruction.

### 4.3 Ask for less

The prompt demands 3–8 standalone factual sentences and states that **accuracy beats
completeness** — if a detail cannot be pointed at in the record, drop the sentence.
`temperature=0`; nothing here benefits from sampling variety.

### 4.4 Identifier grounding — `_ungrounded()`

Every commit-hash-shaped token (`[0-9a-f]{7,40}`) and every filename
(`*.py|ts|tsx|js|jsx|md|json|ya?ml|toml|sql|env`) in the output must occur in the source
record. This is the check that catches invention outright.

**Limitation:** it catches *invented* identifiers, not *wrong claims about real ones*.
"The file `llm.py` was deleted" passes grounding. See §6.1.

### 4.5 Shape checks

Rejected: any line opening with a speaker name; missing `# ` title; under 120 characters;
any `?` (a record of facts contains no questions).

### 4.6 Escalate once

A rejection from the local model retries **exactly once** on the strongest available
provider, then gives up. A format slip should not cost a session's knowledge, but this
must never become a loop — and it is skipped entirely when `CHECKPOINT_PROVIDER` pins a
model explicitly.

### 4.7 Attempt cap

`_record_attempt()` counts failures inside the queue file. Transient failures (LLM down,
quota spent) retry as normal; after `MAX_ATTEMPTS` the capture moves to `rejected/` —
out of the queue, still on disk. Retrying forever is its own failure mode.

### 4.8 Never fail the session

A hook that raises interrupts the user. `main()` swallows everything and exits 0 — but
**silence is the exact bug this feature exists to fix**, so every swallowed failure is
written to `checkpoint.log`. That log is the only place a broken checkpoint is
distinguishable from a quiet one.

---

## 5. Two bugs worth remembering

**Tests drained the real queue.** `run_pipeline` drains, and `test_pipeline.py` calls
`run_pipeline`, so the suite reached into `backend/data/checkpoints/`. It only failed
loudly because the suite clears the API keys — with a key present it would have distilled
a genuine conversation into a throwaway database and then **deleted the capture**, since a
queue file is removed once its note is stored.

Root cause: a path bound at import time. Identical in shape to the `DB_PATH` bug that once
pointed the suite at the production database. `queue_dir()` now resolves per call and
honours `BRAHMASTRA_CHECKPOINT_DIR`; `conftest.py` redirects it suite-wide.

> **Rule:** any module-level path constant is a production-data incident waiting to happen.
> Resolve at call time.

**A green suite hid a `NameError`.** `_ask()` called `chat()` while the import stayed
behind in `_distil()`. Every test mocked `_ask`, so nothing ever executed the
prompt-building path; only a live drain caught it. There is now a test that stubs the
*provider call* instead, and asserts the instruction lands last in the prompt.

> **Rule:** if every test mocks the function under discussion, nothing tests it. Mock the
> boundary, not the unit you are trying to verify.

---

## 6. Where to take this next

Ordered by value.

### 6.1 Provenance on notes and triples — *the biggest structural gap*

A checkpoint-derived triple is currently indistinguishable from one authored in Notion.
Marking origin (`source=checkpoint`) would let:

- `/ask` weight deliberate notes above distilled ones, and say so in its answer;
- contradiction detection prefer the human-authored side of a conflict;
- a future cleanup pass find and re-verify everything a weak model produced.

`raw_triples` already carries `confidence`, so there is a natural place to put it. This is
the change that would make the accuracy ceiling in §4.4 tolerable rather than load-bearing.

### 6.2 Entailment pass

A second cheap LLM call per sentence — "is this supported by the record? yes/no" — drops
sentences that fail. Catches wrong claims about real files, which grounding cannot. Costs
one extra call per checkpoint; a small local model is enough for a binary judgement.

### 6.3 Chunked distillation instead of truncation

`MAX_TRANSCRIPT_CHARS = 20_000` takes the **tail** and silently drops everything earlier.
A long session loses its beginning. Distil per chunk, then merge, so length costs tokens
rather than knowledge.

### 6.4 Mechanical facts from tool traffic

Tool calls are currently dropped wholesale to keep entities like `Bash` out of the graph.
But `tool_use` blocks contain *ground truth* an LLM cannot hallucinate: which files were
edited, which commands ran, which commits were actually made. Extracting those
deterministically and handing them to the distiller as verified context would both improve
accuracy and give grounding something stronger to check against.

### 6.5 Better distiller

`qwen2.5:7b-instruct` is the weak link. A 14B local model, or Groq when quota allows,
would raise the ceiling. `CHECKPOINT_PROVIDER` already exists for this. Worth
benchmarking before assuming.

### 6.6 Smaller items

- Dedup by embedding similarity rather than a list of titles (titles miss paraphrase).
- Prune `rejected/` on a schedule; nothing cleans it today.
- `.offsets.json` is the only record of what has been captured — losing it re-checkpoints
  whole sessions. Consider storing the offset alongside the note instead.

---

## 7. Operating it

```bash
python -m brahmastra.checkpoint --status   # what is queued
python -m brahmastra.checkpoint --drain    # distil and store now
```

| Setting | Default | Purpose |
|---|---|---|
| `BRAHMASTRA_CHECKPOINT_DIR` | `backend/data/checkpoints` | queue location; tests redirect it |
| `CHECKPOINT_PROVIDER` | unset | pin a distiller; disables the escalation retry |
| `MAX_TRANSCRIPT_CHARS` | `20_000` | prompt bound — what a 7B holds an instruction across |
| `MIN_TRANSCRIPT_CHARS` | `400` | below this there is nothing worth a note |
| `MAX_ATTEMPTS` | `3` | failures before a capture is set aside |

Provider preference is **local first** (`ollama`), deliberately: checkpointing runs
unattended and must not compete with extraction for the Groq daily cap.

Failures are in `backend/data/checkpoints/checkpoint.log`. If checkpoints are not
appearing, read that file first — it distinguishes "nothing to say" from "broken".
