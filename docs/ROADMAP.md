# Roadmap — what is left, and why each thing is on the list

Revised 2026-09-24 (evening), on branch `brahmastra-v3`, at 854 passing tests, deployed to the
local Docker stack (`python -m brahmastra.version --against http://localhost:8001`
confirms the running code matches the checkout).
The morning version was written before coercions were collected; the evidence
they produced rewrote three of its items.

Every item carries the evidence that put it there. Items without evidence are
in the last section and are not scheduled. The rule since a single-run
measurement once founded an architecture decision that did not survive being
repeated: a change earns its place by measurement.

---

## Tier 0 — resolved 2026-09-24

The stack was rebuilt from this branch and every note re-extracted. Resolving it
turned up four more things, all fixed and deployed:

| | found by | fix |
|---|---|---|
| **The scheduler only ever ran `default`** — notes in every other workspace, a transcript's included, sat at `pending` forever | counting notes per workspace | `live_sync` ticks every workspace |
| **That change leaked the personal Notion into three workspaces**, and they wrote their insights onto the real pages | its first tick | the global Notion source is for the home workspace only, enforced in the resolver; copies removed, pages restored |
| **Deleting a note never reached Neo4j** — every deleted note's triples survived in the deployed arrangement | cleaning up the leak | `CompositeStore.delete_note` / `delete_workspace` reach both halves, derived first |
| **Nothing could say which code was running** | the stale Docker image and MCP server | `version.py`: fingerprint in `/health` and every pipeline result |

Also shipped: **Groq key rotation** (`GROQ_API_KEYS`, waits as long as Groq says),
and **meeting records** (item 3 below).

⚠️ **The MCP server still runs code from before all of this.** It is a host process
started by Claude Code, not a container; restart it (reconnect the Brahmastra MCP
server, or restart Claude Code) to pick up the fixes.

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
| **Code symbols have a type** (from the typed-extraction post-mortem) | 38 of 129 degradations had a function/constant/class endpoint | replayed over all 95 notes: degraded 129 → 109, related_to 18.3% → 16.2%, 20 rescued, 0 regressed |
| **Domain evidence finished** (was item 2) | all 95 notes now extracted and memoised | the analysis above ran corpus-wide; nothing else clears the bar |
| **Stale code visible** (was item 10) | the Docker stack three days behind | `version.py` fingerprint in `/health` and every pipeline result |

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

### 1. Typed extraction — NOT adopted, and a post-mortem of why

The first verdict was right and its reasons were not. Re-read from the cached
replies, no new calls:

| claim in the first verdict | what the replies say |
|---|---|
| "the enum forces awkward choices" | the cited triple (`_different_numbers blocks merge`) is in the **JSON-mode** reply too, degraded there as well |
| "degraded 24 vs 19" | five of the schema's are one list in one note fanned out; without it, 19 each |
| "nothing better" | quotes are verbatim more often under the schema (92% vs 84%), but per note it is 7 wins, 4 losses, 6 ties — noise at n=17 |
| (unstated) | **no noise floor**: JSON mode was never run against itself, and only 63 of ~180 endpoint pairs recur between runs |

**What it actually found:** a hole in the vocabulary that both modes fall into.
The model steps outside the type list in JSON mode to write `function`,
`module`, `hook`, `environment variable`; the schema forbids that and forces
`feature`/`tool`/`concept`; the domain check then degrades the triple either
way. Fixed without a model — `code_symbol`, assigned by spelling (see Done).
The schema constrained the decoding; the constraint that mattered was the
ontology's.

**Is ours better than cocoindex's here?** On this corpus, yes, on cost. Schema
enforcement (their typed LLM output) buys a shape guarantee we already get from
`_coerce_triple`, which keeps a note when one array element is malformed, and it
costs ~15% more output tokens against a daily token cap that is our binding
constraint.

**Still owed:** the noise floor — JSON mode against itself on the same 17 notes,
~70k tokens. Attempted today; the cap was spent. Only if the grounding gain
survives it is a second A/B worth running.

**Budget note for any corpus-wide probe:** the free tier cannot read the whole
corpus in one day (200,000 tokens/day per account). Throttle, or stop well short
of the cap — a probe that exhausts it takes extraction, cluster summaries and
`/ask` down with it. Groq's day is a ROLLING window: a 1-token probe can pass
while a full-size request is still refused.

---

## Tier 2 — the brain

### 3. Meeting artifacts as graph nodes — DONE

Measured first: through the prose bridge 5 of 13 owners were tied to their item and
0 of 16 items kept their kind. `ingest/graph_record.py` now declares them directly —
`decided_by`, `assigned_to`, `raised_by`, `asked_by`, `discussed_in`, `attended` —
13 of 13 and 16 of 16, the speaker's own words as evidence, no model. System
vocabulary, kept out of the extraction prompt so nothing cached was invalidated.
A meeting node never merges with its topic (`Q3 release` ≠ `Q3 release planning`);
action items still merge with the extracted tasks they came from, which is right.

### 4. Code files in the brain — DONE

`brahmastra/code_index.py` plus MCP `brahmastra_search_code`. Only git-tracked files,
redacted. Chunks are cut by language (Python `ast` at def/class, TS at top-level
declarations, Markdown at headings), neighbours merged up to 40 lines, and each keeps
`start_line`/`end_line`. These two details are from cocoindex's `code_embedding`. The
repo is 189 files and 1,616 chunks; a full embed takes 54 s, and a re-index after edits
about 20 s.

- **Identity is content, not position.** An edit re-embeds the chunk it touched and
  only re-lines the ones below it. Measured on the first re-index: 8 embedded,
  28 re-lined, 7 removed.
- **Measured on 13 questions with a known answer file:** top-1 7/13 and top-3 11/13
  searching everything; **10/13 and 13/13 searching source only**, which is now the
  default. Tests and docs outrank the code they describe because they use the
  same words.
- **Bridge to the graph:** `get_entity_details` on a code symbol adds `defined_at`
  (`_groq_chat` → `backend/brahmastra/llm.py:633-693`), joined on the exact identifier.

**Not via `ownership.py`**, though this item was first written as its second
"owner kind". Each file owns its chunks, but chunks and owner live in one table
and are written in one place, so the table is its own ledger: write new, re-line
moved, delete gone, in that order. The ledger exists for derived rows that
cross stores, where a crash between the two writes leaves an orphan; nothing
here crosses one. Measured better by the only criterion that applies: less code
for the same guarantee.

### 5. Search raw session transcripts alongside the distilled notes — DONE

`brahmastra/sessions.py` plus the MCP tool `brahmastra_search_sessions`. It turned out to
matter more than planned: the distiller **keeps only the last 20,000 characters** of
a stretch and deletes the queue file once stored, so most of a long session never
reached any note. This project's transcript holds ~1.2M characters.

- **Unit:** an exchange (a request plus the work that answered it), cut to the
  embedding window, with each later piece prefixed by its request.
- **Idempotent:** it re-reads the JSONL (0.33 s for 46 MB) and re-embeds only
  changed pieces. It found Claude Code **re-logging 29 exchanges verbatim on
  resume**, and counts each once.
- **Secrets redacted before storage:** checked on the live index, 0 of 706 rows
  hold a key fragment.
- **Measured:** on 7 questions whose answers are known to be in the session, the
  right exchange was in the top 3 for 6 of them. The miss surfaced a neighbouring
  Neo4j bug. It complements note search rather than replacing it: notes win on
  summaries, the raw index on exact words and on whatever the distiller dropped.

Not built, not yet needed: offsets instead of re-reading (only if one transcript
grows past ~500 MB), and indexing other projects' transcripts (they belong to
other workspaces).

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

### 7b. An activity merged with the thing it is about (new, unmeasured)

Seen once on the meeting graph: `legal review of Acme contract` ≡ `Acme contract`.
An activity and its object are two things. One example is not evidence — survey the
default workspace for the pattern before writing a rule.

### 8. An ANN index for the embedding stage

Row blocks cap memory at any size; the work is still quadratic. Revisit near
10,000 mentions. (1,039 today; embedding them all takes 1.25s.)

### 9. Settle the comprehension quality question

Per-kind (4 calls) vs focused (2 calls) is unresolved because the free tier's
daily cap produced 0% runs. Needs a tier that will not run out mid-measurement.

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

- **Typed (schema-enforced) extraction as it stands.** Behind
  `EXTRACTION_SCHEMA=1`. No better on 17 notes once the post-mortem (item 1)
  corrected the first reading, and ~15% dearer in output tokens. Revisit after
  the noise floor, then with field descriptions or a larger model -- each its
  own A/B.
- **Widening `located_in` or `works_on` to files.** Left over from the
  code-symbol evidence. `located_in` is functional, so `NOTION_TOKEN located_in
  backend/.env` would contradict the same token in the root `.env`; `sync.py
  works_on brahmastra_add_note` is the wrong verb, not the wrong type.

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
