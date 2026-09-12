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

- `LLM_PROVIDER` = `groq` (default) | `ollama` | `anthropic`. Auto order is cloud-first
  so the same code deploys unchanged; set `ollama` to stay local and off the network.
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
- **Backoff honours the delay Groq states** ("Please try again in 7.5s"). Guessing made
  retries useless: a blind 2s+4s covers six seconds of a limit needing thirty, so all
  three attempts land in the same closed window. Capped at `EXTRACT_MAX_BACKOFF` (45s) —
  past that the note fails fast and the next run retries it for free.
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
