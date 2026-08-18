# Brahmastra — Status

**Branch:** `feat/multi-workspace` · **18 commits ahead of the last pushed branch, none pushed yet**
**Tests:** 92 passing across 8 files (`python -m pytest tests/ -q` from `backend/`)
**Last verified:** 12 August 2026

Live graph (workspace `default`): **53 notes**, 602 triples, **396 entities**, 61 concept
clusters, 5 contradictions, 10 predicted links. Top entities by PageRank: Claude Code,
Brahmastra Insights toggle, MCP server, Shaan Kapoor, Brahmastra backend.

---

## What this version added

Sixteen commits, grouped by what they actually deliver.

### 1. Workspaces — several independent graphs
`2d0b45e` `efc9000` `427b098` `723a953`

One personal graph, one for work, one per project. Selection is explicit argument →
`BRAHMASTRA_WORKSPACE` → `default`; existing data migrated into `default`, so single-graph
use is unchanged. Created from `POST /workspaces`, the MCP tool, or the UI. Works on
**both** backends. Each workspace can name its own Notion source, so an office graph syncs
from the work Notion.

Uniqueness is per workspace — two workspaces may each hold a different "Sarah". Entity
resolution, graph building, PageRank and clustering are deliberately **not**
cross-workspace; merging a work Sarah with a personal one corrupts both graphs.

**Isolation fails open, so it has three layers.** Property-based partitioning leaks
silently when a filter is forgotten, and it already did once: the store factory built a
`Neo4jStore` without forwarding the workspace, so a write meant for `office` overwrote a
note in `default` with no error. Now: callers cannot express a filter (the store adds it),
the factory verifies the binding at construction (`WorkspaceBindingError`), and
`Neo4jStore._run` refuses Cypher touching a partitioned label without `workspaceId`
(`WorkspaceIsolationError`). Genuine cross-workspace queries pass `unscoped=True`.

### 2. Retrieval that finds things keyword search cannot
`e19bea7`

Hybrid search on Neo4j: BM25 fulltext + vector similarity fused with Reciprocal Rank
Fusion, so a note sharing no keywords with the query still surfaces. Semantic entity
matching. Connection finder (`GET /paths?source=X&target=Y`) returning the shortest path
between two entities, each hop stated as stored, with `walk_from`/`walk_to` for traversal
order. SQLite stays lexical.

### 3. Extraction stops losing facts
`392d512` `03adaf8`

Extraction **degrades, never discards**. An unmappable relation, or a real one with
argument types it does not admit, becomes `related_to` instead of vanishing — silent
dropping is why "Sapan works at Veraxion" once left no Veraxion entity at all.
`RELATION_ALIASES` normalises model phrasings, `INVERSE_ALIASES` swaps direction, and every
coercion is reported in `extract_note()["coercions"]` so the ontology grows from evidence.
Placeholder entities are rejected.

### 4. Provider quota fails fast
`80f9193`

Groq reports per-minute and per-day limits both as HTTP 429, and only the wording separates
them — but the right response is opposite. A per-minute limit clears in seconds and is
worth retrying; a per-day limit does not, so retrying guarantees every remaining call fails
too. A real run spent **10m11s to extract nothing**. `LLMQuotaExhausted` now aborts the run
at the first daily-cap error and reports `aborted_after` / `remaining`. Same run, same
exhausted quota: **1m18s**.

### 5. Session checkpointing — the graph remembers what the AI forgets
`da89d17` `c734fba` `4544df0` `f47b1ce` `9803f5e`

A Claude Code conversation becomes a note before the context window discards it, via the
`PreCompact` and `SessionEnd` hooks. **Capture** writes the transcript to a queue file
(pure file I/O, so a dead LLM can never delay compaction); **drain** distils and stores it
(needs an LLM, so it may fail — the queue file survives until it succeeds). The pipeline
drains any backlog before extract.

It fails closed. A local 7B model once fabricated an entire note — an invented commit, a
push that never happened, a reply from Shaan — because the transcript was formatted as
`Shaan:`/`Claude:` dialogue and "write the next turn" was the likeliest continuation. Turns
are now `[REQUEST n]`/`[WORK n]` records; identifiers in the output must occur in the
source; anything doubtful is rejected rather than stored.

Full rationale, and what each defence came from, is in **`docs/CHECKPOINTING_DESIGN.md`**.

### 6. Notion pull works again
`da89d17` `e914b03`

The 2025-09-03 Notion API split a database into **data sources** and notion-client 3.x
dropped `databases.query`, which had silently reduced the bidirectional loop to write-only.
`_iter_database_rows` branches on the capability rather than a version number — both SDK
generations are installed on this machine (3.1.0 in `.venv`, 2.2.1 globally). Verified live
against the real database on both interpreters.

`e914b03` fixes a related config bug: `pipeline.py` did not load `.env`, and its stages are
imported lazily, so stage 0 checked `NOTION_TOKEN` before any module had loaded it. One run
reported `sync: skipped — NOTION_TOKEN not set` and `notion_writeback: pushed 3` in the same
result. The pull was being skipped while the push went ahead.

---

## What is left

### Blocking a push
- **16 commits are unpushed.** Nothing on this branch exists on any remote.

### Open and unexplained
- **Three workspace MCP tools are invisible to the client.** The server registers all ten
  (`brahmastra_list_workspaces`, `_create_workspace`, `_search_all_workspaces` included —
  verified by listing them from the running module), but the client exposes seven. Survived
  a full restart. Cause unknown; it is client-side, not the server.
- **One note fails extraction** — `879c07dd`, the checkpointing design record. It times out
  on the local 7B because it is long. It extracts fine on Groq.

### Known waste, worth fixing
- **Timeouts are retried.** A timeout means the model cannot handle that input, so retrying
  it identically twice more is guaranteed waste: 240s × 3 = **12m25s of a 45m run** spent on
  one note that still failed. Retry connection drops, not timeouts.
- **Cluster summaries recompute every run.** All 25 largest clusters are re-summarised on
  every pipeline run with no check for whether membership changed. Cheap on Groq (~5s each,
  2m16s total), but it is pure repetition. Cache keyed by cluster membership.

### Deferred by decision
- **Deployment** — to happen on its own branch, not this one.
- **Per-workspace ontology** — the hook exists (`ontology` field, all set to `default`), the
  behaviour does not.
- **Docs reference endpoints that do not exist**: `/entities/search`, `/entities/{id}`,
  `/graph/contradictions`, `/pipeline/stats`.

### Next level for checkpointing
Ordered by value; detail in `docs/CHECKPOINTING_DESIGN.md` §6.
1. **Provenance on distilled triples** — nothing currently distinguishes a checkpoint triple
   from a human-authored one, which is exactly why fabrication was dangerous.
2. **Entailment pass** — catches wrong claims about *real* files, which identifier grounding
   structurally cannot.
3. **Chunked distillation** — the 20k cap takes the tail and silently drops the start of a
   long session.
4. **Mechanical facts from tool traffic** — `tool_use` blocks hold ground truth a model
   cannot hallucinate (files edited, commands run, commits actually made).

---

## Performance notes

Measured 12 August 2026 on this machine.

| Provider | Per extraction call |
|---|---|
| Groq `openai/gpt-oss-120b` (default) | ~0.8s |
| Ollama `qwen2.5:7b-instruct` | 1m37s – 3m57s |

**The Groq default changed mid-session.** `llama-3.3-70b-versatile` served traffic one hour
and returned `404 does not exist` the next — Groq had decommissioned it, which broke
extraction, cluster summaries, GraphRAG and checkpointing simultaneously. The replacement
is `openai/gpt-oss-120b`: largest current option, 131k context, and it honours
`response_format={"type":"json_object"}`, which extraction requires. `qwen/qwen3.6-27b`
does **not** — it emits reasoning tokens and fails JSON validation, so it is unsuitable
despite being available. `LLMModelUnavailable` now fails fast instead of retrying a model
that no longer exists. **Expect this default to go stale again.**

Ollama is ~100× slower **here specifically** because the model does not fit the GPU: the
RTX 3060 Laptop has 4 GB VRAM and `qwen2.5:7b` at 8k context needs ~5.6 GB, so `ollama ps`
reports a 17%/83% CPU/GPU split. A model that fits (`llama3.2`, `gemma2:2b`) would be far
faster at lower extraction quality — worth benchmarking rather than assuming.

Stage costs from a full local run (45m42s total): extraction ~43 min, cluster summaries
**2m16s**. Extraction is the cost; summaries are cheap because their prompts are tiny.

`LLM_PROVIDER=groq` in `backend/.env` is the normal configuration.
