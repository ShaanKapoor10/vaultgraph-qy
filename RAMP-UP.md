# RAMP-UP — read this first if you are Claude and this session is new

Written 1 September 2026, immediately before Shaan reinstalled Windows. Its job is
to get you from cold start to useful in about ten minutes without re-deriving
anything, and without guessing at decisions that were already made and measured.

Read in this order: **this file → `CLAUDE.md` → recall from Brahmastra.**
`CLAUDE.md` is the operating manual and is not optional; it records traps that have
each cost hours at least once.

---

## 0. Where things are now

The project lives on **`F:\brahmastra\vaultgraph-qy`**, deliberately, because `C:`
was wiped. A full backup of everything else sits at
**`F:\brahmastra-backup-2026-09-01\`** — read its `RESTORE.md` for infrastructure,
and `SESSION-CONTEXT.md` for the state of play and the open decisions.

The previous conversation, in full, is at
`F:\brahmastra-backup-2026-09-01\claude\SESSION-TRANSCRIPT.jsonl`. You cannot resume
it as a session. Read it only if this file leaves you short — it is long, and
everything load-bearing has been distilled into here, `CLAUDE.md`, and the notes in
Brahmastra.

---

## 1. Bring the machine back up

```powershell
# 1. Docker Desktop must be running. It stopped twice unprompted on the old machine;
#    if containers are missing, check the daemon before debugging the app.
cd F:\brahmastra\vaultgraph-qy          # ALWAYS from the repo root, never backend/
docker compose up -d

# 2. Python. Rebuild the venv rather than trusting the copied one -- it holds
#    absolute paths from the old install.
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .     # editable; the MCP server needs it

# 3. Frontend
cd ..\frontend && pnpm install
```

Exact package versions as they were:
`F:\brahmastra-backup-2026-09-01\notes\pip-freeze-venv.txt` (server + MCP) and
`pip-freeze-global.txt` (tests). **They diverge on purpose** — see CLAUDE.md on the
two interpreters. If behaviour differs between them, check which one you are in
before suspecting the code.

### Secrets

`backend\.env` and the root `.env` are gitignored and exist only at
`F:\brahmastra-backup-2026-09-01\secrets\`. Copy both back. Nothing works without
them, and the root one is what `docker compose` reads.

---

## 2. Two things that will be broken when you arrive

**Neo4j Aura was SUSPENDED at backup time.** `208ed26a.databases.neo4j.io` returns
non-existent domain. That reads like a typo in `NEO4J_URI` and is not one — a
suspended Aura Free instance stops resolving. Resume it at https://console.neo4j.io.
If it was deleted, nothing is lost that a rebuild cannot restore: the graph is
DERIVED, and all 77 notes are in the Postgres dump.

```powershell
python -c "from brahmastra.pipeline import run_pipeline; run_pipeline(full=True)"
```

**Groq's daily cap may be spent** (200,000 TPD). `chat()` now falls back to Ollama
automatically, so this degrades speed and quality rather than failing. Ollama models
are at `F:\brahmastra-backup-2026-09-01\ollama` or re-pull `qwen2.5:7b-instruct`.

---

## 3. Confirm you are actually back

```powershell
cd backend
.venv\Scripts\python.exe -m pytest tests\ -q        # expect 450 passed
curl http://localhost:8001/health/ready             # expect notes: 77
curl http://localhost:8001/pipeline/status          # stale / behind tell you if the graph lags
```

If `notes` is not 77, stop and restore Postgres before writing anything — see
`RESTORE.md`. 77 notes, 5 workspaces, 1 transcript, 16 meeting artifacts.

---

## 4. Recall before you plan

This is the protocol in `CLAUDE.md` and it is not ceremony. Start with
`brahmastra_search_notes` — it searches the full text of every note, including ones
written by sessions you had no part in. `brahmastra_search_entities` matches entity
NAMES only and comes up thin whenever your wording differs.

Useful queries for this work: `transcript ingestion comprehension evaluation`,
`negation polarity decision reversal`, `MCP server hang stderr`,
`Groq quota rate limit ollama fallback`.

If the MCP server hangs on the first call, it is not hung — see §6.

---

## 5. What was being worked on, and what was decided

Branch `feat/transcript-ingestion`, head `1901cd8` at time of writing, all pushed.
The module ingests large meeting transcripts into typed artifacts plus notes that
flow into the existing extract→resolve→graph pipeline.

**Settled, with measurements — do not reopen these without new numbers:**

- Comprehension runs as **two specialised passes** (commitments, concerns), chosen
  by model size. Over two labelled cases, three runs each:
  `gpt-oss-120b` 43% [14-64] single vs **69% [64-79] focused**;
  `qwen2.5:7b` 19% [9-36] vs 24% [14-36], i.e. indistinguishable, so single wins on
  cost. Zero trap hits anywhere.
- **Report ranges, never single runs.** A single-run table once claimed focused made
  the 7B worse and that claim did not survive repetition — the sign reversed twice.
  `python -m brahmastra.ingest.evaluate --compare --runs 3`.
- **Embeddings cannot judge whether a quote supports a claim** (the wrong pairing and
  the right one both scored 0.40) and **cannot see negation**. Both are handled
  deterministically; see `consolidate.polarity_differs` and
  `comprehend.quote_supports_statement`.

**Open, awaiting Shaan's decision** — he was given three proposals at
https://claude.ai/code/artifact/e14dd29b-d629-41f6-8e56-4dad4d5a6039 and has not
chosen. Do not start building these without asking:

1. **A — source adapters** (~2 days): split parser from prompt so any source works,
   not just transcripts. Recommended first.
2. **B — review queue + frontend** (~4-5 days): the UI, plus human-in-the-loop.
   Forces a real decision: a human edit is SOURCE data and would be destroyed by a
   reprocess, so it needs a third authority tier.
3. **C — a verifier that asks** (~1 week): hold until measured.

**The one cheap task with a standing recommendation:** extend
`quote_supports_statement` from dates/numbers to **named entities** — if a statement
names Acme or Raj and the quote names none of them, refuse. Free at runtime, and the
eval harness will say within minutes whether it costs recall.

---

## 6. Traps specific to this work

Everything in `CLAUDE.md` still applies. These were learned after it was last
updated:

- **The MCP server hangs on the first tool call** if heavy imports happen lazily.
  FastMCP runs sync tools on a worker thread, and importing numpy/neo4j/torch there
  deadlocks on Windows. `_warm_native_imports()` fixes it by importing on the main
  thread before `mcp.run()`. Warming only numpy is NOT enough — torch deadlocks on
  its own account. If a tool call ever hangs for 30 minutes again, dump thread stacks
  with `faulthandler` rather than guessing; two wrong diagnoses came first.
- **Running it beats testing it.** The ingestion module had 146 passing tests and the
  first real end-to-end run found three defects in an hour, all of them invisible to
  tests because tests and uvicorn both load `.env` before anything else.
- **`_backend()` reads `NOTE_BACKEND` but nothing loaded `.env`** on the explicit
  workspace path — fixed, but if transcripts ever vanish, check that first.
- **The status endpoint hand-picks keys** out of `run_state()`, so a field added
  there is silently dropped. It happened once already.

---

## 7. Protocol reminders

Store what you learn, in the same turn you learn it (`brahmastra_add_note`), and
write entity-rich prose so extraction yields real triples. The checkpoint hook is a
net, not a substitute. And prefer `python -m brahmastra.scratch -c "..."` over a bare
`python -c`, which reads `backend/.env` and hits production.
