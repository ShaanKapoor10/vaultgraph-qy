# CLAUDE.md — Working Instructions

## 🧠 Brahmastra is your brain (NON-NEGOTIABLE)

The **Brahmastra MCP server** is your persistent memory for this workspace. You MUST use it
every session — not as the app under development, but as your actual brain. The MCP connection
only makes the tools available; using them is on you, every time.

### The loop you MUST follow

1. **RECALL at the start of any task.**
   Before planning or coding, call `brahmastra_get_graph_stats` and
   `brahmastra_search_entities` to load what's already known about the project.
   IMPORTANT: `brahmastra_search_entities` only matches entity NAMES in the graph.
   To recall what was *written* about a topic (decisions, bug fixes, changes), use
   `brahmastra_search_notes` — it searches the full text of every note and surfaces
   content even when extraction produced few triples. When entity search is thin,
   ALWAYS follow up with `brahmastra_search_notes` before concluding nothing is stored.

2. **STORE as you work — in the SAME turn you make a change.**
   After every meaningful decision, bug fix, migration, or new feature, call
   `brahmastra_add_note` immediately. Do NOT batch this for the end of the session.
   Write notes as entity-rich subject–relation–object prose (e.g. "Brahmastra uses Ollama.
   The file extraction.py implements retry logic."). That extracts into a good graph.

3. **REBUILD after adding notes — EXPLICITLY, in the same turn.**
   Call `brahmastra_run_pipeline` (use `incremental`; use `full` only to re-extract
   everything). Do NOT rely on the live watcher to do this — it can stop silently. An
   `add_note` without a rebuild leaves the note `pending` with 0 triples, so it is
   INVISIBLE to `search_entities` and other sessions cannot recall it. Verify the note
   reached `extraction_status='done'` before considering it stored.

### Available MCP tools (10)
Core: `brahmastra_add_note`, `brahmastra_search_entities`, `brahmastra_search_notes`,
`brahmastra_get_entity_details`, `brahmastra_get_graph_stats`,
`brahmastra_get_contradictions`, `brahmastra_run_pipeline`.

Workspaces: `brahmastra_list_workspaces`, `brahmastra_create_workspace`,
`brahmastra_search_all_workspaces`.

New tools require an MCP server restart to appear.

### Self-check before ending a turn
If you fixed/decided/changed something this turn and did NOT call `brahmastra_add_note`,
you broke the protocol. Store it before replying.

### The checkpoint hook is a NET, not a substitute
`backend/brahmastra/checkpoint.py` runs on Claude Code's `PreCompact` and `SessionEnd`
hooks (registered in `.claude/settings.json`) and distils the conversation into a note
automatically. It exists because this protocol has already failed in practice — the
workspace migration got stored, the quota fix immediately after it did not.

It does **not** relieve you of storing things yourself. A note you write deliberately,
while you still know which detail mattered and why, is better than anything distilled
from a transcript afterwards; the distiller is even shown existing note titles and told
to skip what is already covered, so it fills gaps rather than duplicating you. Treat a
checkpoint note appearing where you should have written one as evidence you missed a turn.

Two phases, deliberately split: **capture** (reads the transcript, writes a queue file —
pure file I/O, so a dead LLM can never delay compaction) and **drain** (distils and
stores — needs an LLM, so it may fail; the queue file survives until it succeeds).
The pipeline drains any backlog before extract, so nothing stays stuck.

```
python -m brahmastra.checkpoint --status   # what is queued
python -m brahmastra.checkpoint --drain    # store it now
```
Failures go to `backend/data/checkpoints/checkpoint.log` — a hook must never raise, so
that log is the only place a broken checkpoint is distinguishable from a quiet one.

**The distiller fails closed.** A 7B model once fabricated an entire note — an invented
commit, a push that never happened, a reply from Shaan — because the transcript was
formatted as `Shaan:`/`Claude:` dialogue and "write the next turn" was the likeliest
continuation. Turns are now `[REQUEST n]`/`[WORK n]` records, identifiers in the output
must occur in the source, and anything doubtful is rejected rather than stored: a missing
note is recoverable, a false triple is not. Before changing the prompt, the validation or
the transcript format, read **`docs/CHECKPOINTING_DESIGN.md`** — it records which real
failure each defence came from, and where to take this next.

---

## Project: Brahmastra (repo: vaultgraph-qy)

Knowledge graph engine that replaces Obsidian — turns notes into a queryable graph.

- **Backend:** Python + FastAPI, **port 8001**. Entry: `vaultgraph-qy/backend/main.py`.
  Run from `backend/`: `uvicorn main:app --reload --port 8001` (NOT `brahmastra.main`,
  which does not exist).
- **Frontend:** Next.js + React + D3, **port 3000**. Run `pnpm dev` from `frontend/`.
- **Tests:** `python -m pytest tests/ -q` from `backend/` (201 passing; 14 of them
  need a reachable Postgres and skip cleanly without one).
- **Full stack:** `docker compose up --build` **from the repo root** — postgres,
  neo4j, backend, frontend. Dashboard :3000, API :8001.

### ⚠️ Two Python interpreters — and they hold DIFFERENT package versions
`backend/.venv` runs the API server **and the MCP server**; fastapi and the neo4j driver
live only there. The global WindowsApps Python 3.12 runs the tests. Both import the code
fine, so a missing package usually means you are in the wrong one:

```
.venv/Scripts/python.exe -m uvicorn main:app --port 8001   # server
python -m pytest tests/ -q                                  # tests
```

The versions **diverge**, so the same code can behave differently per interpreter — this
is not theoretical, it is how the Notion sync broke:

| package | `.venv` | global |
|---|---|---|
| notion-client | 3.1.0 (no `databases.query`) | 2.2.1 (has it) |

Check the interpreter before concluding a bug is in the code: `Get-Process python`
shows the path each running process was launched from.

---

## Storage — the system of record is separate from the engine

**Two kinds of data, and they are not owed the same care.** This is the single most
important thing to understand before changing anything here.

- **SOURCE data — `notes`, `workspaces`.** Cannot be recomputed. If the only copy is
  lost, it is gone.
- **DERIVED data — `raw_triples`, `canonical_map`, `entity_clusters`, `graph_cache`.**
  A cache. Every row is a function of the notes; `run_pipeline(full=True)` rebuilds all
  of it. Losing it costs LLM calls and time, never information.

Selecting a backend is a decision about the ENGINE, and it must never put source data at
risk. Two real incidents came from ignoring that: a backend switch left 61 notes in one
store and 54 in another, and a store built without its workspace overwrote a note in
`default` belonging to `office`.

The split is declared in `stores/base.py` (`SOURCE_DATA`, `DERIVED_DATA`,
`SOURCE_METHODS`) and enforced by `tests/test_store_authority.py`, which **fails when a
contract method is added without being classified** — so it cannot rot into a comment.

### Two variables, not one

```
NOTE_BACKEND    where notes and workspaces live   (postgres)
GRAPH_BACKEND   where the derived graph lives      (neo4j)
```

`NOTE_BACKEND` unset means "wherever `GRAPH_BACKEND` puts them" — the original
single-store arrangement, still fully supported and fine locally. When they differ,
`CompositeStore` routes each call by `SOURCE_METHODS`. `db.py` and its ~104 call sites
are unchanged either way and never touch a backend directly.

**The deployed arrangement is `NOTE_BACKEND=postgres` + `GRAPH_BACKEND=neo4j`**, and
that is now the compose default. `/health/ready` reports `system_of_record` and warns if
it is SQLite.

- **Postgres** — the system of record. Networked (several machines, one truth) and the
  only other backend that can do hybrid search: `tsvector` for the lexical half,
  `pgvector` for the semantic half, fused with the SAME RRF (K=60) as Neo4j so moving
  notes changes storage, not ranking. Holds ONLY notes and workspaces; the derived
  methods raise, because that data is meant to have exactly one home.
- **Neo4j Aura** — the engine: `(:Note) (:Entity) (:Mention) (:Cluster) (:GraphMeta)
  (:Workspace)`, all carrying `workspaceId`. See `docs/NEO4J_DATA_MODEL.md`.
- **SQLite** — `backend/data/concept_graph.db`. Zero dependencies, nothing to run, still
  the right choice locally and what the tests use. Not for deployment: one file on one
  container's disk, and lexical search only.

### ⚠️ A note store must support hybrid search
Routing note search to a lexical-only store is an **invisible** downgrade — every query
still succeeds and quietly returns worse results. So backends declare `capabilities()`,
and `CompositeStore` raises `CapabilityDowngrade` at construction rather than degrading.
`ALLOW_SEARCH_DOWNGRADE=1` accepts it explicitly.

**pgvector is not in a stock PostgreSQL** — the Windows installer offers only `pg_trgm`.
Use the `pgvector/pgvector` image; compose does. Without it the store still holds notes
but reports itself lexical-only, and the composite refuses it.

### ⚠️ Derived data needs an OWNER, or it becomes an orphan
A derived row that no source item claims is a row nothing can delete. Measured on the
real path: a 40-turn transcript segments into 19 chunks and writes 19 notes; edited down
to 4 turns and re-ingested it writes 1 — and **18 notes stayed in the graph**, still
holding triples, still answering searches, sourced from sentences that no longer exist.
Chunks and artifacts shrank correctly because `clear_derived` deletes from the two tables
beside it; the notes live in another store reached through another module, and nothing
recorded that the transcript owned them.

`backend/brahmastra/ownership.py` is the fix, and it is general — a transcript today, a
code file or a dropped PDF next. A ledger maps `(owner_kind, owner_id, target_kind,
target_key) -> fingerprint`, and a sync applies the same three columns to everything:

```
first declared   |  declared differently  |  no longer declared
insert           |  update                |  DELETE
```

- `plan()` is **pure** — declared vs remembered, no I/O — so every edge case is testable
  without a database. That split is deliberate (cocoindex requires the same of
  `reconcile()`).
- **Intent is recorded before the write**, so a key an interrupted run left in an unknown
  state has TWO possible fingerprints and is redone rather than trusted. Nothing is ever
  rolled back; runs converge forwards. Write and delete callbacks **must be idempotent**.
- **Absence is a possible state.** A row with a `pending` fingerprint and no `confirmed`
  one is a first write that never landed. Counting only the fingerprints present made it
  look unchanged — losing exactly the case the two-phase protocol exists for.
- Re-ingesting an **unchanged** transcript now writes nothing, so it does not re-mark
  notes pending and does not buy a fresh round of extraction. `force=True` rebuilds
  anyway: the ledger knows what *this system* last wrote, not what the store holds.
- Deleting a transcript has two shapes, both from cocoindex. **Abandon** (default) keeps
  the notes and releases the claim — deleting a transcript deletes the SOURCE, and
  nothing can recompute them afterwards. **Destroy** (`?purge_notes=true`) takes them.
- **`clear_derived` is no longer on the re-ingestion path.** Deleting every chunk and
  artifact and rewriting them is not a reconciliation: it leaves a hole for as long as
  the run takes, and an interrupted run ended with *nothing* rather than with the older
  version. All three derived kinds — notes, chunks, artifacts — now reconcile, so
  `save_chunk` and `save_artifacts` are upserts (ownership requires idempotent actions).
  The method is kept for a deliberate wipe.

Losing the ledger is not losing data, it is **losing cleanup**: an empty ledger means
"nothing known to have been written", so it is DERIVED, never migrated, and lives beside
the notes so the two are lost together.

### Migrations
```
python -m brahmastra.migrate_to_postgres --apply   # notes: sqlite -> postgres
python -m brahmastra.migrate_to_neo4j --apply      # notes verified, then cache copied
python -m brahmastra.migrate_to_neo4j --apply --rebuild   # recompute instead of copy
```
Both verify the system of record landed intact and raise `MigrationIncomplete` on a
short count **before** writing anything derived — a graph rebuilt on a partial corpus is
confidently wrong. **The pipeline writes only to the selected backend**, so an unused
copy drifts until you re-migrate.

### Neo4j gotchas (all verified the hard way)
- **TLS**: this machine has a TLS-intercepting root CA, so the driver's default
  verification fails with *"self-signed certificate in certificate chain"*. The store
  passes an explicit **certifi** SSL context. This is also why Groq works — its SDK uses
  certifi, not the system store. Any library defaulting to system trust will fail here.
  **Only when TLS is actually in play**: `_encrypted()` infers it from the host, because
  forcing the context at a plaintext local server fails the handshake and the driver
  reports it as *"Unable to retrieve routing information"* — which reads like an absent
  server, not a TLS mismatch. `NEO4J_ENCRYPTED=0/1` overrides.
- **URI**: must be bare `neo4j://` (not `neo4j+s://`) because the `+s` schemes lock TLS
  config and forbid supplying our own context.
- **Database name**: Aura did NOT name it `neo4j` — it is named after the instance, so
  the store defaults to the server's **home** database. Asking for `neo4j` fails with
  `DatabaseNotFound`.
- **Username** is the instance id (`208ed26a`), not `neo4j`.
- **GDS is NOT usable** on this tier. Its 443 procedures are listed, but
  `gds.graph.project` demands a `sessionId` — that needs paid Aura Graph Analytics.
  **PageRank and Louvain stay in NetworkX.**
- **Aura Free cannot `CREATE DATABASE`** — which is why workspaces are a property, not a
  database per workspace.
- **Aura Free SUSPENDS after ~3 days idle**, and a suspended instance stops *resolving* —
  the failure reads as a DNS error, i.e. like a typo in `NEO4J_URI`, not like an instance
  needing a resume. `brahmastra/keepalive.py` prevents it; the `keepalive` compose service
  runs it always-on (no profile, since it reads counts and writes nothing).
  ⚠️ **A keepalive built on `db.*` would NOT work.** The scheduler's idle tick calls
  `db.get_notes(status="pending")` — a SOURCE method, so `CompositeStore` sends it to
  Postgres. That loop can tick every 15 minutes for a week without querying Neo4j once.
  The keepalive reaches `CompositeStore.graph_store` deliberately. `run_pipeline` records
  a contact too, so real work counts and the ping is skipped.
  ```
  python -m brahmastra.keepalive --status    # when was the engine last touched
  python -m brahmastra.keepalive --force     # touch it now
  ```
  `GRAPH_KEEPALIVE_HOURS` (12), `GRAPH_KEEPALIVE=0`, `BRAHMASTRA_DATA_DIR` (where the
  `.graph-touch-<hash>` stamp lives — on the shared volume so containers agree).

---

## Workspaces — several independent graphs

One personal graph, one for work, one per project. See `docs/WORKSPACES_DESIGN.md`.

- Selection: explicit argument → `BRAHMASTRA_WORKSPACE` → `default`.
- Pre-existing data migrates into `default`; single-graph use is unchanged.
- Uniqueness is **per workspace** — two workspaces may each have a different "Sarah".
- Create from: `POST /workspaces`, MCP `brahmastra_create_workspace`, or the UI.
- Cross-workspace search is **explicit** (`db.search_notes_across`) and results carry
  their `workspace_id`.
- Deliberately NOT cross-workspace: entity resolution, graph building, PageRank,
  clustering. Merging a work "Sarah" with a personal one corrupts both graphs.

### ⚠️ Isolation fails OPEN — respect the three layers
Property-based partitioning leaks *silently* when a filter is forgotten. This already
happened once: the store factory built `Neo4jStore` without forwarding the workspace, so
a write meant for `office` overwrote a note in `default`, with no error. Three layers now
prevent it — do not weaken them:

1. Callers never pass a workspace filter; the store is bound and adds it itself.
2. The factory resolves the workspace and **verifies the binding**
   (`WorkspaceBindingError`), so a backend that drops the argument fails at construction.
3. `Neo4jStore._run` **refuses Cypher** touching a partitioned label without
   `workspaceId` (`WorkspaceIsolationError`). Genuinely cross-workspace queries must pass
   `unscoped=True` explicitly.

If you add a query and it raises `WorkspaceIsolationError`, the guard is right and the
query is wrong. Add the filter.

---

## LLM — pluggable, Groq by default

`backend/brahmastra/llm.py` owns provider selection for **everything** (extraction,
GraphRAG, cluster summaries), so they can never disagree about which provider is live.

- `LLM_PROVIDER` = `groq` (default) | `openai` | `gemini` | `anthropic` | `ollama`.
  Auto order is cloud-first so the same code deploys unchanged; set `ollama` to stay
  local and off the network.
- **Groq is what is affordable today, not what this is for.** A provider is one entry in
  `_REGISTRY` in `llm.py` — name, key env, SDK module, model env, default model — and
  `PROVIDERS`, `provider_status()`, `model_for()` and `active_model()` all derive from
  it. Extraction reaches any registered provider through `llm.chat` with no code of its
  own; Groq and Ollama keep bespoke paths only because Groq's tier words a 413 and a 429
  almost identically and Ollama is plain HTTP with no SDK.
  ⚠️ **A key in `.env` does not move production onto that provider** — it only makes it
  available as a fallback. Moving is `LLM_PROVIDER=gemini`, deliberately, because a
  silent switch of the model behind every extraction shows up later as "it got worse"
  with nothing to explain it.
  ⚠️ `_gemini_chat` has **never been run against a live endpoint** — no key on this
  machine. Treat its first real call as a test.
- An explicit `LLM_PROVIDER` only wins **if that provider is actually usable** — a stale
  `LLM_PROVIDER=ollama` with a dead server falls through instead of failing.
- A cloud provider counts as available only with **both** its key and its SDK installed.
- Groq model: **`openai/gpt-oss-120b`** (`GROQ_DEFAULT_MODEL` in `llm.py`). Ollama:
  `qwen2.5:7b-instruct`.
- ⚠️ **Groq retires hosted models, so this default WILL go stale.** It was
  `llama-3.3-70b-versatile` until Groq decommissioned it mid-session — the same model
  served traffic one hour and returned `404 does not exist` the next, breaking extraction,
  cluster summaries, GraphRAG and checkpointing at once. `LLMModelUnavailable` now raises
  immediately (retrying a retired model is as pointless as retrying a daily cap) and names
  the fix. List current models with `Groq(...).models.list()`; the replacement must honour
  `response_format={"type":"json_object"}`, which extraction depends on — `qwen/qwen3.6-27b`
  does **not** (it emits reasoning tokens and fails JSON validation).
- Groq's free tier is rate limited: a `full=True` re-extraction of ~44 notes typically
  errors on a third of them. Those notes are **retried automatically on the next run**
  (`EXTRACT_RETRY_ERRORS=0` disables), so just run the pipeline again.
- **Replies are memoised** (`brahmastra/memo.py`), so most of that re-extraction is now
  free. The key is the note, the model and `SYSTEM_PROMPT` — **which carries the
  ontology**, so an ontology edit correctly re-extracts everything while an unrelated
  note edit costs nothing elsewhere. The RAW reply is cached, never the parsed triples:
  validation and relation coercion re-run on every hit, or a fixed bug would stay fixed
  only for notes nobody had extracted yet.
  ```
  LLM_MEMO=0      no caching anywhere
  INGEST_MEMO=0   re-read every transcript passage, but keep extraction cached
                  (what comparing comprehension variants needs)
  ```
  Table `llm_memo`, in whichever database holds the notes. DERIVED — drop it freely.
- **Backoff honours the delay Groq states** ("Please try again in 7.5s"). Guessing made
  retries useless: a blind 2s+4s covers six seconds of a limit needing thirty, so all
  three attempts land in the same closed window. Capped at `EXTRACT_MAX_BACKOFF` (45s) —
  past that the note fails fast and the next run retries it for free.
  ⚠️ This rule lived **only in `extraction.py`** for months, while `llm.chat` — the path
  comprehension, cluster summaries, GraphRAG and checkpointing all take — still slept a
  blind 2s/4s/6s into a window the server had already said was shut. It is now
  `llm.retry_delay`, and `extraction.py` imports it. One rule, one implementation.
- **A 413 is not a rate limit.** An oversized note fails permanently, so it is not
  retried — but unlike a spent quota it must not stop the run, because one big note says
  nothing about the next. Read `notes.extraction_error` before assuming which you have:
  a 413 was misdiagnosed as congestion for two days because nothing recorded the message.

---

## Pipeline (7 stages)

Sync (Notion) → Extract → Resolve (Union-Find + Jaro-Winkler + sentence-transformers)
→ Build Graph (NetworkX + PageRank + Louvain) → Cluster summaries (LLM) → Cache →
Notion write-back.

Runs return **`status`: `ok` | `partial` | `error`** plus `failed_stages`. Check it —
a run where every extraction failed used to look successful. Write-back is skipped when
extraction failed for every note, rather than pushing a stale graph into Notion.

The lock is **per store** (`data/.pipeline-<hash>.lock`), so runs against different
workspaces or backends do not block each other. It is **stolen after 900s**, so a crashed
run cannot block forever — check a lock's AGE, never its existence. A script that waited
for the file to disappear waited an hour for a lock that was already stealable.

Cluster summaries are **reused when a cluster's membership is unchanged**, keyed on
membership and never on cluster id (Louvain renumbers communities every run, so matching
on id would hand a summary to the wrong cluster). The carry-forward lives in
`concept_graph.py` because caching the graph is what destroyed the old summaries. The
stage reports `reused` vs `generated`: on an unchanged graph that is 25 and 0, where it
used to be 25 LLM calls every single run.

---

## Ontology — 18 relations, 12 entity types, 5 functional

Synced across 3 files, keep them in step: `backend/brahmastra/ontology.py` (source of
truth, with domain/range), `frontend/lib/ontology.ts`, `ontology.yaml`.

Functional relations (at most one value ⇒ a second is a contradiction):
`reports_to`, `has_status`, `scheduled_for`, `located_in`, `employed_by`.

**Extraction degrades, it never discards.** An unmappable relation, or a real one with
argument types it does not admit, becomes `related_to` rather than being dropped — that
silent dropping is why "Sapan works at Veraxion" once left no Veraxion entity at all.
`RELATION_ALIASES` normalises model phrasings ("works at" → `employed_by`) and
`INVERSE_ALIASES` swaps direction ("Mei manages Sarah" → `Sarah reports_to Mei`).

Every coercion is reported in `extract_note()["coercions"]`. **Grow the vocabulary from
that evidence**, not in anticipation — see `docs/ONTOLOGY_DESIGN.md`.

---

### ⚠️ Entity identity must not depend on the hash seed
Measured on the live graph — 970 triples, 901 clusters — by running the resolver twice in
two **processes** over byte-identical input:

| | before | after |
|---|---|---|
| cluster ids naming the same members | 2 / 901 | **901 / 901** |
| mentions under a different canonical name | 14 / 980 | **0** |

Mentions are collected into a **set**, set iteration order for strings depends on
`PYTHONHASHSEED`, and that differs per process. So `f"c{i:04d}"` numbered clusters by
luck, and `max(pool, key=len)` broke ties by the same luck — `function run_pipeline` vs
`run_pipeline function`, `Apollo Project` vs `Apollo project`. Every pipeline run rewrote
the whole canonical map for no reason.

- `cluster_id_for(members)` derives the id from **who is in the cluster**. Membership
  changing changes the id, which is correct — the same rule cluster summaries follow.
- `_pick_canonical` breaks ties on `(len, name)`. Arbitrary but **stable**, which is the
  property that was missing.

Growing the corpus tells the same story: 70% of the notes then all of them left **1 of
667** positional ids intact against **645 of 667** derived ones. cocoindex states the
consequence plainly — ids not derived from the data make every reprocessing run churn the
target, deleting rows and re-inserting identical ones under new keys.

### A name that already won keeps winning (PINNED)
The tie-break fixed *identical* runs. Corpus **growth** was a separate bug: the heuristic
re-runs a popularity contest every time a cluster gains a member, and "longest
title-cased" is a poor judge of which name a person means. Simulating growth on the live
graph — cluster 70% of the notes, take that map as `existing`, then cluster all of them:

| | renames |
|---|---|
| heuristic alone | 9 / 727 mentions |
| **pinned** | **0** |

Pinning changed the answer in 8 of 676 clusters and kept the better name in every one:

```
Shaan Kapoor      →  ShaanKapoor10      a person, renamed to a handle
CocoIndex         →  Cocoindex          correct casing, lost
2026-08-12        →  2026-08-18         a DIFFERENT DATE
embedding model   →  embeddings.get_model
decision          →  decisions
```

One escape hatch, deliberately narrow: a **strict word-superset** still wins, so a cluster
first seen as `Sarah` that later gains `Sarah Chen` takes the fuller name. Plurals, casing
and reorderings are not fuller forms — they are what pinning exists to stop flapping
between. The eight are measured; the `Sarah Chen` case is reasoned, because growth did not
produce one. `ENTITY_PINNED=0` turns it off, which is also how a name frozen by mistake
gets re-picked.

**What is NOT copied from cocoindex:** their PINNED also says *two existing canonicals
never merge*. That rule does not survive the trip — here a lone mention is its own cluster
and therefore trivially its own canonical, so 676 of 676 names were "existing" and the
rule would refuse nearly every merge. The equivalent event (a cluster holding several
former canonicals) occurred **0 times** in that growth, so it is reported as
`absorbed_canonicals` rather than decided by an untested rule.

### How this compares to cocoindex's resolver
Different halves of the same problem, and we are ahead on one of them.

| | cocoindex | Brahmastra |
|---|---|---|
| blocking | FAISS, `top_n=5`, distance ≤ 0.3 | all-pairs Jaro-Winkler **and** embeddings |
| deciding | LLM pair-resolver, **required** | deterministic guards; LLM judge opt-in |
| naming | the resolver picks (`CanonicalSide.NEW`) | heuristic + PINNED |
| existing canonicals | PINNED / PREFERRED | PINNED |
| structure | entity → candidates, sequential | all-pairs → Union-Find |

**Where we are better.** Our guards removed 12 wrong merges on the live graph with 100%
precision, every run; their shape *requires* a resolver, and our measured LLM judge was
break-even and unstable at temperature 0. We also carry two signals — four of those 12
came from Jaro-Winkler scoring `brahmastra_search_entities`/`brahmastra_search_notes` at
0.951, which embeddings alone rank differently — and a negation rule, which nothing in
their default prompt covers.

**What they had that we now do too** — all four, each measured:
- **Transitivity.** A~B and B~C confirmed merged A~C *unasked*. `_split_incoherent`
  re-clusters greedily over the accepted edges, strongest first, skipping any union that
  would put a refused pair in one set. Live graph: **0** pairs remain inside a cluster
  that the guards would refuse.
- **Blocking.** `_candidate_pairs` covers all four match methods — exact, token_subset,
  acronym, and a provable Jaro bound (`J ≥ x ⟹ m ≥ (3x−1)/(1/|a|+1/|b|)`, with `m`
  capped by the character multiset intersection). 506,521 pairs → 7,228, **111 merges
  either way**, 6× faster. Off below `BLOCKING_MIN_MENTIONS` (2000), because 5s is not
  worth trading for a filter that only *almost* covers the methods.
- **`entity_type` hints + `extra_guidance`.** Inferred per pair (theirs is one type per
  resolver); batches are grouped so a file pair and a person pair get opposite advice.
  `ENTITY_CONFIRM_GUIDANCE` appends domain rules. **Unmeasured** — the judge is off and
  the numbers that turned it off used one generic question.
- **Validate-and-re-prompt.** Two retries, told *why* the last reply was rejected. A
  verdict that parsed is never re-asked, so this cannot become "ask until it agrees".

⚠️ **Rounding in a blocker must err toward offering a candidate.** `PROVIDERS` vs
`provider_status` needs exactly 9 matching characters and has exactly 9 — the bound
computed `9.000000000000002` and dropped a merge scoring exactly `JARO_THRESHOLD`.

⚠️ **`entity_confirm` resolves the model once per `confirm`, not per batch.** Provider
resolution *probes*; against an unreachable host that probe waits out its timeout —
measured at **2.0s per batch**, silent because the call is wrapped in try/except. The same
leak made 18 tests take 29 seconds; stubbing it took them to 0.10s.

## Search & retrieval

- **Hybrid search** on **Neo4j and Postgres**: lexical relevance fused with vector
  similarity by Reciprocal Rank Fusion (K=60, both engines over-fetched to `limit*5`).
  Finds notes sharing no keywords with the query. The two backends use the SAME
  arithmetic on purpose, so moving notes between them changes storage, not ranking —
  Neo4j via BM25 + its vector index, Postgres via `tsvector` + `pgvector`. SQLite stays
  lexical, which is exactly why it cannot hold the notes in a deployment.
  Substring fallback semantics also match across all three: match ANY term, rank by how
  many matched, prefer all-term hits but still return partial ones.
- **Embeddings**: `backend/brahmastra/embeddings.py`, `all-MiniLM-L6-v2`, 384-dim, cached
  to `backend/.cache` (absolute path — a relative one previously landed the 87MB model in
  `backend/backend/.cache`). Changing the model means rebuilding the vector indexes.
- **GraphRAG** (`/ask`): local (entity subgraph) or global (cluster summaries) mode, with
  citations. **Multi-hop** — depth 2 is chosen automatically for chained questions
  ("Sarah's manager's other reports"); `POST /ask {"depth": 1..3}` overrides.
- **Connection finder** (`GET /paths?source=X&target=Y`): shortest path between entities.
  Hops state the fact **as stored**, with `walk_from`/`walk_to` for traversal order.

---

## API routes (22)

```
/health  /health/ready
/notes  /notes/{note_id}                    GET /notes?q= is HYBRID search
/entities  /entities/{name}
/graph  /graph/stats  /graph/clusters  /graph/triples
/pipeline/run  /pipeline/status  /pipeline/sync  /pipeline/extract
/pipeline/resolve  /pipeline/build-graph  /pipeline/cluster-summaries
/ask
/paths
/workspaces  /workspaces/{workspace_id}  /workspaces/search
```

Verify against reality rather than this list: `main.app.openapi()["paths"]`. The routers
are `_IncludedRouter` objects, so `app.routes` does NOT flatten them — reading that
instead reports only the two health routes and looks like everything is missing.

There is **no `/graph/contradictions`** and no `/pipeline/stats`; contradictions travel
inside `GET /graph`, and the counts are `GET /graph/stats`. Docs cited both for months.

There is **no `/api` prefix** on the backend. `/api/*` exists only on the Next dev
server, which rewrites `/api/:path*` → `localhost:8001/:path*`.

---

## Notion = bidirectional brain (the core product bet)

Notion is the **content layer** (where Shaan writes); Brahmastra is the **intelligence
layer**. The loop is bidirectional, which is what beats Obsidian (no manual `[[links]]`):

- **Pull** — `sync.py` auto-detects whether the target is a database or a **page**;
  page-mode pulls every page the integration can access. Empty-body pages are skipped.
  The 2025-09-03 Notion API split a database into **data sources** and notion-client 3.x
  dropped `databases.query` entirely, so `_iter_database_rows` branches on the capability
  (`hasattr(client.databases, "query")`) and falls back to `data_sources.query`. Branch on
  the capability, never the version — both SDK generations are installed on this machine.
- **Per-workspace source** — each workspace can name its own `notion_database_id`, so an
  office graph syncs from the work Notion. The global `NOTION_DATABASE_ID` is the
  fallback.
- **Write-back** — `notion_writeback.py` writes one idempotent "🧠 Brahmastra Insights"
  toggle per page: relationships, ⚠️ contradictions, 🔗 suggested links.
- **Live** — `python -m brahmastra.live_sync` (`POLL_INTERVAL`, default 120s).
- `notion-client` must be installed in the active Python.

---

## Config (`backend/.env`)

```
GRAPH_BACKEND  NOTE_BACKEND
POSTGRES_HOST  POSTGRES_PORT  POSTGRES_USER  POSTGRES_PASSWORD  POSTGRES_DB
NEO4J_URI  NEO4J_USER  NEO4J_PASSWORD  AURA_INSTANCEID
LLM_PROVIDER  OLLAMA_MODEL  OLLAMA_HOST  GROQ_API_KEY
NOTION_TOKEN  NOTION_DATABASE_ID
```
Set per run when needed: `BRAHMASTRA_WORKSPACE`, `BRAHMASTRA_DB`,
`ALLOW_SEARCH_DOWNGRADE`, `NEO4J_ENCRYPTED`.

### ⚠️ Two traps around this file
1. **`docker compose` reads a `.env` from the CURRENT WORKING DIRECTORY.** Running it
   from `backend/` picks up THIS file and deploys whatever it says — same compose file,
   same command, different directory, different deployment. It resolved sqlite plus the
   Aura URI from `backend/`, and neo4j plus the local engine from the repo root. Keeping
   `backend/.env` aligned with the deployed arrangement is what stops that mattering.
   **Always run compose from the repo root.**
2. **Postgres publishes on host port 5433, not 5432.** A native PostgreSQL install holds
   5432, and Windows lets both it and Docker's proxy bind `0.0.0.0:5432` without an
   error — the first binder wins, so the mapping becomes a silent no-op and anything
   host-side reaching `localhost:5432` gets the OTHER database, with no pgvector and
   none of your notes.

Tests pin their own storage (`tests/conftest.py`) rather than inheriting it from `.env`.
They previously did inherit it, and the moment `.env` named the deployed arrangement the
suite started building a `CompositeStore` against real infrastructure.

---

## Important runtime note

The MCP server and the uvicorn backend each load their modules + `.env` at startup.
After editing extraction/ontology code or `.env`, those processes hold STALE code until
restarted. To run the pipeline with fresh code immediately, use a fresh process from
`backend/`:

```
python -c "from brahmastra.pipeline import run_pipeline; run_pipeline(full=False)"
```

### ⚠️ A throwaway `python -c` hits PRODUCTION
That command above is deliberate. Most are not. A bare `python -c` reads `backend/.env`
like every other process and resolves the deployed Postgres + Aura — which is how two
debug probes wrote notes into the production graph and had to be deleted afterwards.

Do **not** rely on remembering `BRAHMASTRA_NO_DOTENV=1` and three other variables; that
is the same "careful enough" that failed the test suite three times, and it cannot work
anyway because the trap is **import order** — `brahmastra.stores` reads `.env` at import
and answers "which store?" for the whole process. Use the runner, which sets the
environment *before* your code is imported:

```
python -m brahmastra.scratch -c "from brahmastra import db; print(db.describe())"
python -m brahmastra.scratch -m brahmastra.keepalive --status
```

It prints where it pointed, redirects storage to a temp SQLite file, and blanks
`NOTION_TOKEN` so a probe that happens to run the pipeline cannot edit real pages.

Tests use their own temp DB and clear `NOTION_*` and the LLM keys, so a run can neither
touch the real database nor bill an API. Keep it that way — they previously ran against
production and pushed to the real Notion workspace.

Developer: Shaan Kapoor (shaan2003kapoor@gmail.com).
