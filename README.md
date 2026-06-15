# Brahmastra — Concept Graph Engine

Brahmastra turns a pile of unstructured personal notes into a **living, queryable knowledge graph**. It reads plain text (no manual `[[links]]`, no tags), extracts typed facts with an LLM, figures out which mentions refer to the same real-world thing, builds a directed concept graph, and then runs classic graph algorithms over it to surface **what matters, what clusters together, what contradicts, and what's probably connected but never written down**.

This repository is a TypeScript / Next.js implementation of the original "Concept Graph Engine" plan (which described a local Python CLI + MCP server). It is delivered here as a **deployable web app with an interactive dashboard** so the entire pipeline can be seen and demoed end to end in the browser.

---

## Table of contents

1. [The core idea](#the-core-idea)
2. [Pipeline at a glance](#pipeline-at-a-glance)
3. [Tech stack](#tech-stack)
4. [Project structure](#project-structure)
5. [The engine, stage by stage](#the-engine-stage-by-stage)
   - [Stage 0 — Ontology](#stage-0--ontology)
   - [Stage 1 — Extraction (LLM)](#stage-1--extraction-llm)
   - [Stage 2 — Entity resolution (Union-Find)](#stage-2--entity-resolution-union-find)
   - [Stage 3 — Concept graph construction](#stage-3--concept-graph-construction)
   - [Stage 4 — Graph algorithms](#stage-4--graph-algorithms)
6. [The dashboard (UI)](#the-dashboard-ui)
7. [Data model](#data-model)
8. [The sample vault](#the-sample-vault)
9. [Running locally](#running-locally)
10. [Enabling live LLM extraction](#enabling-live-llm-extraction)
11. [What's implemented vs. what's left](#whats-implemented-vs-whats-left)
12. [Design notes & deliberate trade-offs](#design-notes--deliberate-trade-offs)

---

## The core idea

A note like:

> "Sarah is leading the auth migration. She owns the whole effort. The auth migration is scheduled for March 15. Sarah reports to Mei Lin."

is just text. Brahmastra turns it into **facts** (triples):

```
(Sarah, owns, auth migration)
(auth migration, scheduled_for, March 15)
(Sarah, reports_to, Mei Lin)
```

Do this across an entire vault, merge the duplicate ways people refer to the same entity ("Sarah" / "Sarah K." / "Sarah Khan"), connect every fact into one graph, and you can suddenly ask questions the notes never explicitly answered — *who is the most central person in my work?*, *which topics form a cluster?*, *where do my notes contradict each other?*, *who should probably be connected but isn't?*

---

## Pipeline at a glance

```
 Notes (plain text)
        │
        ▼
 ┌──────────────────┐   Stage 1: ontology-constrained LLM extraction
 │   Raw Triples    │   (subject, relation, object, confidence, source quote)
 └──────────────────┘
        │
        ▼
 ┌──────────────────┐   Stage 2: string similarity + Union-Find
 │ Resolved Entities│   ("Sarah", "Sarah K.", "Sarah Khan") → one canonical node
 └──────────────────┘
        │
        ▼
 ┌──────────────────┐   Stage 3: map mentions → canonical, build directed multigraph
 │  Concept Graph   │
 └──────────────────┘
        │
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ Stage 4: analysis                                             │
 │  • PageRank        → most central entities                    │
 │  • Louvain         → emergent concept clusters                │
 │  • Contradictions  → functional relations with >1 value       │
 │  • Link prediction → common-neighbors heuristic               │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
   Interactive dashboard
```

The whole pipeline lives in `lib/` and is orchestrated by a single function, `runPipeline()` (`lib/pipeline.ts`). It is **pure and deterministic** — given the same triples it always produces the same graph and insights. Entity resolution and the graph are *always* rebuilt from the raw triples, so the graph is always a fresh, complete reflection of the current content (mirroring the plan's "rebuild from scratch" philosophy).

---

## Tech stack

| Concern | Choice |
| --- | --- |
| Framework | Next.js (App Router) + React + TypeScript |
| Styling | Tailwind CSS v4 + shadcn/ui components |
| LLM extraction | Vercel AI SDK (`ai`) via the AI Gateway, `generateText` + `Output.object()` |
| Schema validation | `zod` |
| Graph layout / viz | `d3-force` (force simulation) rendered to hand-built SVG |
| Algorithms | Implemented from scratch in TypeScript (Union-Find, PageRank, Louvain, etc.) |

No database is used — the engine runs in-memory over a seeded vault and any notes you add in the session. This keeps the project instantly demoable; persistence is a documented next step.

---

## Project structure

```
app/
  layout.tsx                  Root layout, fonts, dark theme, metadata
  page.tsx                    Entry point — runs the pipeline, renders the dashboard
  actions/
    extract.ts                'use server' action: ontology-constrained LLM extraction

lib/                          ← THE ENGINE (framework-agnostic, pure TypeScript)
  ontology.ts                 Typed relation set + functional flags + prompt formatting
  types.ts                    All shared types (Note, RawTriple, ConceptGraph, …)
  string-similarity.ts        Jaro-Winkler + token-subset + acronym heuristics
  union-find.ts               Disjoint Set Union (path compression + union by rank)
  entity-resolution.ts        Blocking → pairwise similarity → Union-Find → canonical names
  concept-graph.ts            Graph build + PageRank + Louvain + contradictions + link prediction
  pipeline.ts                 runPipeline(): wires every stage together
  sample-notes.ts             Synthetic vault + pre-extracted triples (instant demo)
  viz.ts                      Cluster color palette + node-radius scaling helpers

components/
  dashboard.tsx               Top-level layout, stat header, tab orchestration, shared state
  graph-view.tsx              Interactive force-directed SVG graph (zoom/pan/drag/select)
  entity-detail.tsx           Slide-in inspector for a selected entity (all relations + provenance)
  panels/
    central-entities.tsx      PageRank leaderboard
    concept-clusters.tsx      Louvain clusters
    contradictions.tsx        Conflicting functional facts
    predicted-links.tsx       Suggested-but-missing connections
    entity-resolution.tsx     Raw mentions → canonical clusters + why they merged
    notes-panel.tsx           Vault browser + live "extract triples" input
```

---

## The engine, stage by stage

### Stage 0 — Ontology

**File:** `lib/ontology.ts`

Everything is anchored to a fixed, typed **relation ontology**. There are 10 relation types (`owns`, `works_on`, `depends_on`, `blocks`, `part_of`, `scheduled_for`, `located_in`, `reports_to`, `uses`, `related_to`).

Each relation carries:
- a **description** — injected into the extraction prompt so the LLM is constrained to this vocabulary (no free-form relations), and
- a **`functional` flag** — marks relations that should have **at most one current value** per subject (`scheduled_for`, `located_in`, `reports_to`). This single flag is what makes contradiction detection possible later: a functional relation with two live values is, by definition, a contradiction.

Helpers: `isValidRelation()` (used to drop any out-of-ontology output from the LLM) and `formatOntologyForPrompt()` (renders the ontology into the system prompt).

### Stage 1 — Extraction (LLM)

**File:** `app/actions/extract.ts`

A Next.js **server action** (`extractTriples`) sends a note's text to an LLM through the **Vercel AI Gateway** and asks for structured output. Implementation details:

- Uses `generateText` + `Output.object()` with a **`zod` schema** (the current AI SDK pattern; `generateObject` is deprecated).
- The schema forces each triple to `{ subject, relation (enum), object, confidence, source_quote }`. Using an **enum** for the relation means the model literally cannot emit a relation outside the ontology.
- The system prompt instructs the model to extract **only explicitly stated facts**, keep entity names **exactly as written** (normalization is a later stage's job), and attach a **source quote** for provenance.
- Output is validated, out-of-ontology relations are filtered, and each fact is stamped with `extractedAt` (used by contradiction detection) → returned as `RawTriple[]`.
- Errors are caught and returned as a structured `{ ok: false, error }` so the UI can show them gracefully.

### Stage 2 — Entity resolution (Union-Find)

**Files:** `lib/string-similarity.ts`, `lib/union-find.ts`, `lib/entity-resolution.ts`

This is the algorithmic centerpiece. The LLM emits raw mention strings; the same entity is referred to many ways ("Sarah" / "Sarah K." / "Sarah Khan"). Resolution collapses these into one canonical node.

**`union-find.ts` — Disjoint Set Union.** A generic `UnionFind<T>` with **path compression** and **union by rank** (near-O(1) amortized). Its job: given pairwise "these two are the same" edges, collapse *transitively*-similar mentions into clusters — so "Sarah"~"Sarah K." and "Sarah K."~"Sarah Khan" puts all three together even though "Sarah" and "Sarah Khan" were never directly compared.

**`string-similarity.ts` — does the pair check, *with an explanation*.** `areLikelySameEntity(a, b)` returns `{ same, method, score }`:
- **Jaro-Winkler** for typo/spelling closeness (prefix-boosted), implemented from scratch.
- **Token-subset** — one mention's significant tokens (stopwords removed) are a subset of the other's, catching "Sarah" ⊂ "Sarah Khan".
- **Acronym** — "PromptlyBI" vs "Promptly BI".

> Note: the original plan paired Jaro-Winkler with a sentence-transformer embedding fallback. Running real embeddings in the browser is out of scope, so the semantic fallback is approximated with deterministic, **explainable** token/acronym heuristics that capture the same intent. The `method` + `score` are surfaced in the UI so every merge is auditable.

**`entity-resolution.ts` — orchestration:**
1. **Blocking** — only compare mentions sharing the first 2 characters, avoiding an O(n²) all-pairs blowup on large vaults.
2. **Pairwise similarity** within each block produces union edges (and records *why* each merge happened).
3. **Union-Find** collapses everything into clusters.
4. **Canonical name** per cluster = most frequent mention (longer string wins ties).

Output: `ResolutionResult` = the clusters (with per-merge explanations) + a `canonicalMap` (`every raw mention → canonical name`).

### Stage 3 — Concept graph construction

**File:** `lib/concept-graph.ts` → `buildConceptGraph()`

Each raw triple's subject/object are mapped through the `canonicalMap`, producing a **directed multigraph**: nodes are canonical entities; edges keep `relation`, `confidence`, and full provenance (`sourceNoteId`, `sourceQuote`, `extractedAt`). Parallel edges between the same pair are intentionally **kept** so fact history and per-fact source attribution survive into the analysis stage. Each node also tracks `mentionCount` (how many raw strings collapsed into it) and `degree`.

### Stage 4 — Graph algorithms

**File:** `lib/concept-graph.ts`. All four are implemented from scratch:

- **PageRank — `centralEntities()`.** Classic power-iteration PageRank (damping 0.85, dangling-mass redistribution) over the simple directed projection. Answers *"which entities are most structurally important across my notes?"*
- **Louvain — `conceptClusters()`.** Single-level modularity optimization (local-moving phase) on the undirected, weighted projection. Produces **emergent topic domains** rather than manually-tagged categories.
- **Contradiction detection — `detectContradictions()`.** For each **functional** relation, group facts by `(entity, relation)`; if more than one distinct value exists, it's a contradiction. Values are sorted by `extractedAt` so the UI can mark the most recent one as the likely-correct "LATEST". Each conflicting value keeps its source quote + note.
- **Link prediction — `predictLinks()`.** A **common-neighbors** heuristic: for every unconnected pair, count shared neighbors; pairs with ≥2 commons are surfaced as "probably related but never written down," scored by overlap.

---

## The dashboard (UI)

**Files:** `components/dashboard.tsx` and `components/**`.

A dark, technical, single-accent (amber) dashboard. `app/page.tsx` runs `runPipeline()` and hands the result to `<Dashboard>`, which renders:

- **Stat header** — live counts for the run: notes → triples → resolved entities → contradictions, with the pipeline stages shown as a flow.
- **Interactive graph (`graph-view.tsx`)** — a `d3-force` simulation rendered to SVG. Nodes are **sized by PageRank centrality** and **colored by Louvain cluster**; you can **drag** nodes, **zoom/pan**, and **click** a node to select it. Selecting a node dims the rest of the graph to highlight its neighborhood.
- **Entity inspector (`entity-detail.tsx`)** — a slide-in panel showing every incoming/outgoing relation for the selected entity, each with its **source quote, note, and date** — full traceability from insight back to the original sentence.
- **Insight tabs (`panels/`):**
  - **Central Entities** — PageRank leaderboard.
  - **Clusters** — Louvain communities.
  - **Contradictions** — conflicting functional facts with a "LATEST" badge and both source quotes.
  - **Predicted Links** — suggested connections + the shared neighbors that triggered them.
  - **Entity Resolution** — the raw-mentions→canonical showcase, listing each cluster and the pairwise merges (method + score) that produced it.
  - **Notes** — browse the vault and **run live extraction** on new text you paste in.

`viz.ts` holds the constrained categorical **cluster palette** (literal `oklch` values, kept self-contained for SVG reliability) and the node-radius scaling helper.

---

## Data model

**File:** `lib/types.ts`. Key types that flow through the pipeline:

- `Note` — `{ id, title, content, lastEdited, extractionStatus }`
- `RawTriple` — a fact with **raw** (unresolved) subject/object strings + confidence + provenance.
- `EntityCluster` / `ResolutionResult` — resolved entities + the `canonicalMap` + per-merge explanations.
- `ConceptGraph` (`GraphNode[]`, `GraphEdge[]`) — the directed multigraph.
- `CentralEntity`, `ConceptCluster`, `Contradiction`, `PredictedLink` — analysis outputs.
- `PipelineResult` — the single object bundling all of the above, returned by `runPipeline()`.

---

## The sample vault

**File:** `lib/sample-notes.ts`

A small synthetic vault of plain-text notes with **pre-extracted triples**, so the entire dashboard works **instantly with no API key**. The data is deliberately engineered to exercise every stage:

- **Entity resolution:** "Sarah" / "Sarah K." / "Sarah Khan", "PromptlyBI" / "Promptly BI" / "the BI project", "Raj" / "Raj P."
- **Contradictions:** the auth migration has two deadlines; Sarah reports to two different managers; Raj is in two locations.
- **Link prediction:** people & projects that share neighbors but aren't directly linked.

There are **no `[[links]]`** between notes — every edge in the graph is *inferred*, which is the entire point.

---

## Running locally

```bash
pnpm install
pnpm dev
```

Open the printed URL. The dashboard loads immediately on the seeded vault — no configuration required.

---

## Enabling live LLM extraction

The **Notes** tab has an "extract triples" box that calls the real LLM via the Vercel AI Gateway. In v0 the gateway is zero-config, but it requires a valid payment method on the Vercel account before it will serve requests (you get free monthly credits). If you see an `AI Gateway requires a valid credit card on file` message, add a card in your Vercel account — that's an **account setup** step, not a code issue. Everything else in the app runs without it.

The model is set in `app/actions/extract.ts` (`openai/gpt-5-mini`) and can be swapped for any gateway-supported model string.

---

## What's implemented vs. what's left

### ✅ Implemented

- **Ontology** — 10 typed relations with functional flags, used by both extraction and contradiction detection.
- **LLM extraction** — ontology-constrained, schema-validated, with confidence + source quotes, via a server action over the AI Gateway.
- **Entity resolution** — blocking + Jaro-Winkler + token-subset + acronym similarity + **Union-Find** clustering + canonical naming, with **explainable merges**.
- **Concept graph** — directed multigraph with full provenance on every edge.
- **PageRank**, **Louvain clustering**, **contradiction detection**, **common-neighbors link prediction** — all from scratch.
- **Full pipeline orchestration** (`runPipeline`), pure and deterministic.
- **Interactive dashboard** — force-directed graph (sized by centrality, colored by cluster, drag/zoom/select), entity inspector with provenance, and one panel per insight type.
- **Seeded vault** so the whole thing is demoable with zero setup.
- **Live extraction** of new notes pasted into the UI.

### 🔜 Not yet implemented (natural next steps)

- **Persistence / database** — currently in-memory. Notes and triples reset on reload. A DB (e.g. Neon/Postgres) would let the vault accumulate over time and make re-extraction incremental.
- **Notion (or other source) sync** — the plan reads from a Notion workspace. The data layer is source-agnostic (`Note[]`), so a Notion importer would slot in cleanly, but it isn't wired up yet.
- **Real semantic embeddings** for entity resolution — the embedding fallback is approximated with deterministic heuristics. Swapping in a real embedding model (with a vector similarity threshold) would catch harder coreference cases (e.g. "the BI project" ↔ "PromptlyBI" purely by meaning).
- **Graph querying** — shortest-path / "how is X connected to Y?", relation-filtered views, and natural-language questions over the graph.
- **MCP server** — the plan envisions exposing the graph as MCP tools to an assistant. This build exposes the engine through a web UI instead; an MCP wrapper around `lib/` would be additive.
- **Incremental / streaming extraction** — currently extraction runs per note on demand; batching the whole vault with progress + caching would scale better.
- **Confidence-weighted algorithms** — PageRank/Louvain treat edges uniformly; edge `confidence` is stored but not yet used as edge weight.
- **Tests** — the original plan called for `pytest`; the pure functions in `lib/` are highly testable and would benefit from a unit-test suite.

---

## Design notes & deliberate trade-offs

- **TypeScript instead of Python.** The plan specified a Python CLI + MCP server. Re-implementing the engine in TypeScript makes it a **deployable, visual** product where the algorithms can actually be *seen* working. The algorithm logic is faithful to the plan.
- **In-memory, not a database.** Chosen so the project is instantly runnable and demoable. Persistence is the first item on the "what's left" list.
- **Heuristic semantic fallback.** Real sentence-transformer embeddings don't run well in a browser/edge context, so the semantic step is approximated with explainable, deterministic heuristics — which has the bonus that every merge can be justified to the user.
- **Multigraph kept intact.** Parallel edges are preserved (not collapsed at build time) specifically so contradiction detection and the entity inspector can show the full, dated history of each fact with its source quote.
