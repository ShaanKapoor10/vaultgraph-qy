# Roadmap — what is left, and why each thing is on the list

Revised 2026-09-23 (afternoon), on branch `brahmastra-v3`, at 701 passing tests.
The morning version was written before coercions were collected; the evidence
they produced rewrote three of its items.

Every item carries the evidence that put it there. Items without evidence are
in the last section and are not scheduled. The rule since a single-run
measurement once founded an architecture decision that did not survive being
repeated: a change earns its place by measurement.

---

## ⚠️ Tier 0 — needs a decision, and blocks seeing any of the rest

### The running stack is three days stale, and it reverts every fix

`docker ps` shows the whole stack — `backend`, `scheduler`, `keepalive` — on an
image **built 2026-09-20**, before any of this branch's work. The `scheduler`
container re-runs the pipeline with that image's resolver, so every
fresh-process run that wrote the corrected graph was overwritten within minutes:

```
fresh process, today's code      886 clusters, 0 incoherent pairs
what /graph serves right now     850 clusters, a 20-file mega-cluster, and
                                 'BRAHMASTRA_API_KEY located_in Vercel'
```

Nothing below is visible in the running system until this is resolved. It is a
decision rather than a task because the stack presumably tracks `main`, and this
branch is unmerged. The options:

- **Merge `brahmastra-v3`, then rebuild** (`docker compose up --build -d` from
  the repo root). Volumes hold the data and survive a rebuild.
- **Rebuild from the branch** to try it live before merging.
- **Stop the `scheduler` container** while testing, so fresh-process runs stick.

Then, once the running code is current: a re-extraction of every note (item 1
below applied to the live graph). **41 of 92 notes cost nothing** — their
replies are memoised and the ontology change left every key intact — and the
rest cost one call each.

---

## Done since the morning version

| | evidence | result |
|---|---|---|
| **Coercions persisted** (was item 1) | `run_extraction` discarded them | `python -m brahmastra.coercions` |
| **Relation domains widened** (was gated item 3) | 59 triples degraded on 41 notes | related_to 31.3% → 16.7% there |
| **A malformed reply no longer deletes a note's triples** | reproduced: 2 → 0, stayed `done` | replacement built before delete |
| **A file is not the thing it implements** | 12 of 12 path-vs-name merges wrong | the `ingest/cases` bridge gone (was item 8) |
| **Handles and constants never name a cluster** | `ShaanKapoor10`, `SYSTEM_PROMPT` | natural names win, pinning included |
| **One sidecar-table base** | three copies of 40 lines | memo, ownership, coercions share it |

### What the coercions actually said — read this before touching the ontology

`docs/ONTOLOGY_DESIGN.md` waits for `unmapped_relation`: new verbs the
vocabulary lacks. Across 41 notes of the real corpus there were **zero**. The
prompt lists all eighteen relations and the model never leaves the list.

The signal was `domain_range` — a *known* relation between types the ontology
refused. `file` and `feature` joined `ENTITY_TYPES` on 2026-06-25, ten days after
the relation domains were written, and the domains were never revisited. The
prompt describes each relation but does not state its domain, so the model
followed the description correctly and a check it could not see degraded the
result. CLAUDE.md's own recommended note — *"The file extraction.py implements
retry logic"* — was one of them.

**The consequence for the design doc:** with a closed vocabulary in the prompt,
the evidence for growing it arrives as domain/range pressure, not as new verbs.
ONTOLOGY_DESIGN.md should say so.

---

## Tier 1 — evidence in hand

### 1. Typed extraction for core `extraction.py`

Core asks for `response_format={"type":"json_object"}` — valid JSON of *any
shape*. The evidence for fixing that has grown three ways since this morning:

- **Shape failures are common.** The probe found an empty string sitting in the
  triples array in **3 of 37 notes**, and one note failed outright on Groq's own
  `400 Failed to validate JSON`. The first of these used to delete the note's
  triples.
- **The measured gain.** `comprehend.py` records schema enforcement buying
  **15 points (43% → 58%)** in ingestion.
- **It is the root cause of the domain problem.** The model cannot obey a
  domain it is never told. A schema can carry it.

**How to build it — from cocoindex's meeting-notes example.** Their schema
carries the rules in field descriptions (*"participants: people who attended
other than the organizer. Do not include the organizer here."*), so the schema
is half the prompt. Two further ideas from their conversation example: give
each entity type **example names** ("Python (programming language)", "Lex
Fridman"), and ask for **the fullest unambiguous name** at extraction time.
Fewer variants born is less resolution needed afterwards.

**Budget it.** Unlike the domain widening, this changes `SYSTEM_PROMPT`, so it
invalidates every memoised extraction: 92 calls for a full re-extraction. A test
(`test_the_widening_did_not_touch_the_prompt`) guards the distinction.

`llm.py` already carries `json_schema` through every provider. Must degrade to
`json_object` for a model that rejects schema mode.

### 2. Finish the domain evidence

Widened on **41 of 92 notes**. The read-only probe that gathered the evidence
spent Groq's **entire daily token budget** (200,000 tokens/day on this tier) at
note 41; the other 51 returned 429. Those 41 are now memoised and free forever.
The remaining 51 need one call each — about a day's quota, so run them on a day
when nothing else needs the model, or on a paid tier.

When they are in: repeat the analysis (per relation and rejected type, ranked by
notes, with example sentences read by hand). Anything else clearing the bar is a
candidate; `concept` stays out unless the sentences say otherwise. Then update
ONTOLOGY_DESIGN.md with what the evidence turned out to look like.

**Budget note for any future corpus-wide probe:** the free tier cannot read the
whole corpus in one day. Throttle, or stop well short of the cap — a probe that
exhausts it takes extraction, cluster summaries and `/ask` down with it until
the reset.

---

## Tier 2 — the brain

### 3. Meeting artifacts as graph nodes (replaces "promote `decided`/`assigned_to`")

**Why the old item was wrong.** It waited for coercion evidence that `decided`
and `assigned_to` were needed. That evidence can never arrive: meetings do not
go through the extraction vocabulary at all. Decisions, action items, risks and
questions go into the `meeting_artifacts` table — typed, owned, quoted — where
GraphRAG cannot traverse them.

**What cocoindex does instead.** Their meeting-notes example uses exactly
`ATTENDED`, `DECIDED` and `ASSIGNED_TO` — between **Meeting**, **Task** and
**Person nodes**. Their conversation example generalises it: a **Statement** is
a node, with `Person → made → Statement`, `Session → contains → Statement` and
`Statement → mentions → Entity`. Provenance lives in the graph structure rather
than in a side table.

**Why it matters.** "What did Sarah decide about the Acme contract?" becomes a
traversal. Today it is a SQL query nobody using `/ask` knows to make.

**What is already done.** Artifacts have stable derived ids, owners, quotes and
chunk provenance, and ownership reconciles them. The missing part is declaring
them as nodes and edges.

### 4. A second owner kind: code files

Unchanged in purpose — ownership is general and has one user. Two details from
cocoindex's `code_embedding` example: chunk with a **language-aware** splitter
(`detect_code_language` + recursive splitting), and keep each chunk's
**`start_line`/`end_line`** so a hit points at lines, not just a file.

### 5. Search raw session transcripts alongside the distilled notes (new)

From cocoindex's `entire_session_search`, which does for AI coding sessions what
`checkpoint.py` does here — with one difference that matters. They **index the
raw per-turn transcript**, embedded, with no model in the loop; *"how did I fix
the auth bug"* finds the session by meaning. We **distil** it into a note through
an LLM, and CLAUDE.md records that distiller fabricating an entire note once,
which is why it now fails closed.

Keep the distilled note for the graph; index the raw turns for retrieval. The
raw index cannot hallucinate, and it catches what the distiller chose to drop.

---

## Tier 3 — known limits

### 6. Union-Find transitivity — **now has a live instance**

The morning version said this had "no measured instance". It does now, on
today's code: `brahmastra-v3 ~ Brahmastra ~ brahmastra_ask` — a branch, the
product and an MCP tool in one node. No guard refuses any link, so
`_split_incoherent` has nothing to split. cocoindex's entity→candidates shape
has no equivalent hole; their resolver joins each entity to exactly one
canonical.

A syntactic fix was measured and rejected (see below). What remains is a
different resolution shape, or the judge (item 7).

### 7. Re-measure the LLM merge judge — with two model knobs

Off because four runs showed it break-even and unstable at temperature 0 — on
`gpt-oss-120b`, the free tier's small model. cocoindex's meeting example runs
**two** model settings, `LLM_MODEL` for extraction and `RESOLUTION_LLM_MODEL`
for resolution. Split ours the same way first, so the judge can be measured on a
stronger model without changing extraction.

### 8. An ANN index for the embedding stage

Row blocks cap memory at any size; the work is still quadratic. Revisit near
10,000 mentions. (1,039 today; embedding them all takes 1.25s.)

### 9. Settle the comprehension quality question

Per-kind (4 calls) vs focused (2 calls) is unresolved because the free tier's
daily cap produced 0% runs. Needs a tier that will not run out mid-measurement.

### 10. Make stale code visible — **proven urgent**

Tier 0 is this, at full size. The morning version recorded one symptom (the MCP
server running the old resolver); the real exposure was the whole Docker stack
three days behind, silently reverting fixes. The pipeline should report a code
fingerprint, and `/health` should expose it, so a stale process is obvious
rather than discovered.

### 11. Identify speakers before extracting, for diarized audio (new)

For the transcription path. cocoindex's conversation example runs extraction in
**two steps**: first map `Speaker A` / `Speaker B` to real names using the
metadata and the conversation, then extract statements from a transcript with
the names substituted. Unrecognised speakers stay `(Speaker A)` and their
statements are kept but **not attributed**. Our text transcripts already carry
names; audio will not.

---

## Deliberately not doing

Recorded so they are not re-proposed. Each was measured.

- **Typed (schema-enforced) extraction as it stands.** Built, behind
  `EXTRACTION_SCHEMA=1`, and A/B'd on 17 real notes: more triples (195 vs 169)
  but no drop in domain_range degradation (24 vs 19) or the related_to share
  (15.4% vs 14.8%), and the enum pushes the model into wrong relations. Its one
  win, no malformed elements, no longer matters. Revisit only with field
  descriptions in the schema or a larger model -- each its own A/B.

- **Per-type entity resolution, as cocoindex does it.** 29 of 73 heuristic
  merges cross a type boundary, and about half of those are *right* — `Groq` is
  an `organisation` in one triple and a `tool` in the next. Their types are
  structural (a name sits in a `mentioned_person` field); ours are assigned by
  the model per triple across twelve fuzzy categories. Adopted instead in the
  one place syntax can prove the type: a file path is not a name.
- **Refusing a merge when exactly one side is a code identifier.** Same shape as
  the file rule that went 12 for 12 — and it splits about 50/50. Right for
  `Brahmastra` vs `brahmastra_ask`, wrong for `live sync watcher` vs
  `live_sync watcher`. In prose, snake_case and plain words are often one thing.
- **Memoising entity embeddings.** cocoindex does, because it calls an embedding
  API per name. Ours is local: 1.25s for all 1,039 mentions.
- **Waiting for `decided`/`assigned_to` to appear as coercions.** They cannot:
  meetings do not use the extraction vocabulary. See item 3.
- **Widening domains to `concept`.** Cleared the evidence bar three times; the
  sentences behind it were junk ("len(chunks) provides call count").
- **Token-bucket rate limiting.** Never measured; the backoff already honours the
  delay Groq states.
- **The LLM judge on by default.** Measured; revisit only with item 7.
- **The `SELF_CONTAINED` prompt rule.** Measured, no effect, removed.
- **Coupling anything further to Groq.** `llm.py` is a provider registry.

---

## Cross-cutting, always true

- **Two Python interpreters.** `backend/.venv` has torch, psycopg and groq; the
  global one does not.
- **A throwaway `python -c` hits production.** Use `python -m brahmastra.scratch`
  unless running the pipeline with fresh code is the point.
- **Isolation fails open.** Three layers stop it; do not weaken any of them.
- **Derived data needs an owner.** Anything that writes rows from a source
  declares them to `ownership.py`.
- **Never delete the old rows before the new ones exist.** Ownership was built on
  it; extraction was losing notes without it.
- **Rules on syntax beat rules on model output.** Every guard that held up here
  keyed on structure the model cannot change — a path, a number, an identifier.
  The two that keyed on model-assigned types did not.
