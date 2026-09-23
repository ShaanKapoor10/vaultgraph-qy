# Roadmap — what is left, and why each thing is on the list

Written 2026-09-23, on branch `brahmastra-v3`, at 658 passing tests.

Every item below carries the evidence that put it there. Items without evidence
are in the last section, marked as such, and are not scheduled — this project
has already thrown away one architecture decision founded on a single run, and
the rule since then is that a change earns its place by measurement.

---

## Where things stand

The last stretch closed the gap between Brahmastra and cocoindex's target-state
and entity-resolution machinery. Shipped, each measured on the live graph:

| | before | after |
|---|---|---|
| orphan notes after a shorter re-ingestion | 18 of 19 survived | 0 |
| cluster ids stable across two identical runs | 2 / 901 | 901 / 901 |
| canonical names stable across two identical runs | 14 flipped | 0 |
| canonical names stable across corpus growth | 9 renamed | 0 |
| merges that were two different things | 35 of 105 | 0 |
| clusters holding a pair the guards refuse | 2 | 0 |
| one file held as two nodes | 18 pairs | 0 |
| heuristic pass, 1007 mentions | 5.1s all-pairs | 0.85s blocked, same result |

Current resolve stage: **1011 mentions → 852 clusters**, 186 merges, 58 refusals,
17 same-file joins, 4 clusters split.

---

## Tier 1 — evidence in hand, do these first

### 1. Persist `coercions`

**What.** `extract_note()` returns `coercions` — every relation the model produced
that the ontology had to degrade to `related_to`. `run_extraction` collects the
results and never reads that key. It is computed for every note, on every run,
and thrown away.

**Why it matters.** `docs/ONTOLOGY_DESIGN.md` states the growth rule plainly:
*"Do not add relations in anticipation. Add a relation when it keeps appearing as
`unmapped_relation:` — that is data telling you."* There is no such data, because
nothing stores it. The rule has been unusable since it was written.

**Size.** Small. One table, one write in `run_extraction`, one read surface.
It is derived data, so it belongs beside the notes and never migrates.

**Blocks.** Item 3.

### 2. Typed extraction for core `extraction.py`

**What.** Core extraction asks for `response_format={"type": "json_object"}` —
valid JSON of *any shape*. Ingestion asks for a real `json_schema`, so the
provider enforces the field names and types.

**Why it matters.** `comprehend.py` records the measurement: *"Schema enforcement
bought 15 points at one call (43% → 58%)."* That is the single largest quality
gain measured anywhere in this system, and the half of the codebase that produces
the actual knowledge graph does not have it.

**Why it is cheap.** `llm.py` already carries `json_schema` through every
provider — Groq, OpenAI, Gemini, Anthropic, Ollama. The plumbing exists; only
`extraction.py` does not use it.

**Risk to watch.** Not every model honours schema mode. CLAUDE.md already records
`qwen/qwen3.6-27b` failing JSON validation. The change must degrade to
`json_object` rather than fail, exactly as comprehension does.

**Size.** Medium. Define the schema from `ontology.py` so the two cannot drift.

---

## Tier 2 — gated on Tier 1

### 3. Promote `decided` and `assigned_to` into the ontology

**What.** Add the relations that meetings keep producing and the vocabulary
cannot hold.

**Why it is gated.** The whole argument for adding them is that the coercion log
asks for them. Until item 1 ships there is no log, and adding them now would be
precisely the "in anticipation" move ONTOLOGY_DESIGN.md forbids. Ship item 1,
run the corpus, read what it says, then decide — including deciding not to.

**Remember.** Three files stay in step: `backend/brahmastra/ontology.py`,
`frontend/lib/ontology.ts`, `ontology.yaml`.

---

## Tier 3 — the reason ownership was built

### 4. A second owner kind: code files

**What.** `brahmastra/ownership.py` is general by construction — `owner_kind`,
`owner_id`, `target_kind`, `target_key`. It currently has exactly one user:
transcripts. Give it a second one: source files, chunked by AST rather than by
paragraph, declaring the notes and artifacts they own.

**Why it matters more than it looks.** "One strong brain, many use cases" is
currently an assertion. Ownership is what makes it true rather than three
pipelines sharing a database: delete a source, its derived rows go, and the rule
is the same whatever the source was. A second kind is what proves the
abstraction holds — and the failure it prevents is the one already measured,
where each ingestion path knew about a different subset of what it wrote.

**Note the shape is already right.** cocoindex's code example is the same
pattern with a Tree-sitter chunker in front. The chunker is the new part; the
ownership, the memoisation and the reconciliation are done.

**Size.** Large. Worth breaking into: chunker → declare → ownership wiring.

---

## Tier 4 — known limits, not yet costing anything

### 5. An ANN index for the embedding stage

**What.** `_embedding_sim` now computes in row blocks, which caps *memory* at any
corpus size. It does not make the work sub-quadratic — every pair still gets a
score.

**When it starts mattering.** Measured and projected in the code:

```
 1,000 mentions       1M entries     4 MB
10,000              100M           400 MB
50,000              2.5B            10 GB
```

The heuristic path is already blocked (506,521 pairs → 7,228, identical result).
This is the remaining wall. cocoindex reaches for FAISS here, and that is the
right answer — but at 1011 mentions there is nothing to buy.

**Trigger.** Revisit at roughly 10,000 mentions, or when the resolve stage stops
finishing in seconds.

### 6. Re-measure the LLM merge judge on a larger model

**What.** `ENTITY_CONFIRM=1` turns it on. It is off because four runs over 21
labelled pairs said it prevents two wrong merges and costs one or two right ones,
and **the ones it costs change between runs at temperature 0**.

**Why the measurement deserves redoing.** It was taken on `gpt-oss-120b`, which is
the small thing the free tier offers, with one generic question. Since then the
prompt gained per-pair type guidance and validate-and-re-prompt — both
**unmeasured**. And cocoindex's own arrangement is the opposite of what was
tested: a *strong* model for extraction and a *light* one for pair confirmation.

**What would make it conclusive.** A tier that does not run out mid-run. The
outage during the first attempt produced four invalid measurements, and that is
the reason `Unanswered` exists.

**Note.** The deterministic guards now catch 58 refusals per run with no model at
all. The judge has to beat that, not beat nothing.

### 7. Settle the comprehension quality question

**What.** Whether four per-kind calls per chunk beat two focused ones.

**Why it is open.** Recorded in `comprehend.py`: the per-kind runs contain **0%
scores**, and those are Groq's daily cap rather than the architecture — four
calls per chunk burns the free tier four times faster. The operational answer is
already clear (four specialists exhaust the budget); the quality answer needs a
tier that will not run out mid-measurement.

---

## Tier 5 — smaller, opportunistic

### 8. `_path_of` abstains on extensionless paths

`backend/brahmastra/ingest/cases` has no extension, so the file rule abstains on
every pair involving it — and six abstentions bridged seven distinct files into
one cluster. `_split_incoherent` cleans that up now, but the root cause stands: a
directory is a path too, and the rule could say so.

### 9. Union-Find transitivity is mitigated, not eliminated

`_split_incoherent` guarantees no cluster contains a pair the guards **refuse**.
It says nothing about pairs that are neither confirmed nor refused, which can
still be fused through a chain. cocoindex's entity→candidates shape has no
equivalent hole because each entity joins exactly one canonical. Changing shape
here is a large rewrite for a failure that currently has no measured instance —
listed so it is not forgotten, not scheduled.

### 10. Make a stale MCP server visible

Cost real time today: the MCP server and uvicorn load their modules at startup,
so `brahmastra_run_pipeline` ran the *old* resolver and reported 837 clusters
while a fresh process produced 852. CLAUDE.md documents the trap; nothing
*detects* it. Having the pipeline report a code fingerprint would turn a silent
wrong answer into an obvious one.

---

## Deliberately not doing

Recorded so they are not re-proposed.

- **Token-bucket rate limiting.** Considered, never measured, no evidence the
  current backoff is the bottleneck. The backoff honours the delay Groq states,
  which was the actual bug.
- **Turning the LLM judge on by default.** Measured. Break-even, and unstable at
  temperature 0. Revisit only with item 6.
- **The `SELF_CONTAINED` prompt rule.** Tried, measured, no effect, removed —
  with a test pinning it out so it is not re-added by accident.
- **Coupling anything further to Groq.** It is what is available now, not a
  constraint on the design. `llm.py` is a provider registry for exactly this
  reason; OpenAI, Gemini and Anthropic are already wired.

---

## Cross-cutting, always true

- **Two Python interpreters.** `backend/.venv` runs the servers and has torch,
  psycopg and groq; the global one does not. A missing package usually means the
  wrong interpreter.
- **A throwaway `python -c` hits production.** Use `python -m brahmastra.scratch`
  unless running the pipeline with fresh code is the point.
- **Isolation fails open.** Three layers stop it; do not weaken any of them.
- **Derived data needs an owner.** Anything new that writes rows from a source
  declares them to `ownership.py`, or it invents its own cleanup and they
  quietly disagree.
