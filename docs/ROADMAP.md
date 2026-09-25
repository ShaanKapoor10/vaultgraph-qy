# Roadmap — what is left, and why each thing is on the list

Revised 2026-09-24 (evening), on branch `brahmastra-v3`, at 890 passing tests, deployed to the
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

### 1. Typed extraction — CLOSED: the noise floor says it is noise

The noise floor was measured on 2026-09-25: JSON mode run a second time on the same 17 notes (A′).

| | JSON A | JSON A′ | schema B |
|---|---|---|---|
| quotes verbatim | 84.0% | **92.6%** | 91.8% |
| degraded | 14 | 22 | 18 |
| related_to share | 11.8% | 14.8% | 12.3% |
| endpoint overlap with A | — | 21% | 21% |
| per-note verbatim vs A (win/loss/tie) | — | 7/4/6 | 7/4/6 |

JSON against itself differs exactly as much as JSON against the schema. The
schema's one apparent gain, grounding, was A being an unlucky draw. The only
difference beyond the noise is volume (195 against 162–169 triples), and more is
not better. Not adopted, for good; the post-mortem's other finding, the
`code_symbol` vocabulary gap, was the real one.

**The bigger finding:** two extraction runs at temperature 0 share only **21%** of
their triples. The memo cache is what makes the graph stable between rebuilds;
without it every full re-extraction would draw a different graph. Treat
`LLM_MEMO=0` as a change to the graph, not a performance setting.

**Budget note for any corpus-wide probe:** the free tier cannot read the whole
corpus in one day (200,000 tokens/day per account; `GROQ_API_KEYS` holds two
accounts). Groq's day is a ROLLING window: a 1-token probe can pass while a
full-size request is still refused. Limits are **per model**, so the judge on
qwen3.8-27b does not spend extraction's gpt-oss-120b budget.

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

### 6. Union-Find transitivity — live instance FIXED; the audit behind it

An audit of all 154 merge edges on the live graph (read-only; canonical-map write
patched out) found the live instance and more besides. Both fixes are syntactic
and measured edge by edge:

| fix | edges removed | wrong | arguable | added |
|---|---|---|---|---|
| **Jaro-Winkler judges words, not a shared prefix** | 12 of its 30 | 11 | 1 | 0 |
| **A release is not the product** (`Brahmastra` vs `Brahmastra v3`) | 4 | 4 | 0 | 0 |

- **Jaro-Winkler:** about half its merges were two things sharing a first word
  (`Brahmastra engine` ≈ `Brahmastra pipeline`, `NOTION_DATABASE_ID` ≈ `Notion
  database`, `ANLI` ≈ `MNLI`). It now needs the same number of words, each a spelling
  variant, a plural, or the same words run together (`ShaanKapoor10`, `GraphStore`).
- **`global`/`local`** joined the contrast pairs. GraphRAG's two modes merged twice.
- **The release rule:** exactly one side adds a version token (`v3`, `3.1.0`). This was
  the one-sided case `_different_numbers` could not see, and it takes `Brahmastra`
  out of the `brahmastra-v3` node.
- **Found by measuring:** the first version of the spelling rule was silently shadowed
  by another `_words` further down the module and measured that way. A test now pins
  the helper.

The general hole remains: any chain the guards do not refuse still fuses. It is now
narrower, since the two biggest bridge makers were JW prefixes and version
suffixes.

### 7. The LLM merge judge — ADOPTED on Groq (qwen3.8-27b)

`RESOLUTION_LLM_MODEL` now separates the judge's model from extraction's
(`llm.using_model`, a context bound to one provider, so a fallback to Ollama is
never handed a Groq model id). `brahmastra/resolution_cases.json` holds 71
labelled pairs from the audit (one annotator, arguable pairs left out), and
`python -m brahmastra.resolution_eval` scores any model on them:

| model | wrong merges stopped | right merges broken | 3 runs |
|---|---|---|---|
| gpt-oss-120b | 21–22 / 29 | 6–9 / 42 | varies |
| **qwen/qwen3.8-27b** | **21 / 29** | **3 / 42** | **identical** |

qwen's three "broken" are arguable labels (`Neo4j Aura` / `Neo4j Aura Free`,
`qwen2.5:7b` / `-instruct`). Live and read-only, it refused 43 of 119
candidates: about 25 plainly right, 2 plainly wrong (`Groq key` / `live Groq key`).
Both objections that kept it off, break-even and churn, are gone. **On by default
when the provider is Groq**, off on Ollama (a 7B judge was never measured);
`ENTITY_CONFIRM=0/1` overrides. An unanswered pair still merges as before.

**Then the sentences** (the lever the first measurement named). Each name is shown
with up to two sentences from the notes it was used in:

| | stopped | broke | runs |
|---|---|---|---|
| names only | 21 / 29 | 3 / 42 | ×3 identical |
| reworded guidance only | 22 / 29 | 4 / 42 | noise |
| **names + sentences** | **25 / 29** | 7 / 42 | ×2 identical |

It newly stops `GraphRAG` / `Microsoft GraphRAG`, `backend/.env` / `loading
backend/.env`, `/health/ready` / `Health endpoint`, and a decision note vs its
concept. Five of the seven it breaks are one family (Neo4j Aura and its tier,
backend and instance): visible duplicates, against four invisible wrong merges.
Adopted.

**Still missed, both runs:** `knowledge graph` / `knowledge graph engine`,
`Brahmastra pipeline` / `brahmastra_run_pipeline`, `quota` / `quota consumption`,
`uvicorn` / `uvicorn backend`. These are "X" against "X + a head noun", the
pattern no spelling rule separates. What's left to try: a stronger model when one
exists on the tier, or a second opinion on exactly the "X vs X + noun" shape.

### 7b. An activity merged with the thing it is about — handled by 7

The audit found more of the pattern: `coverage for session checkpointing` ≈
`session checkpointing`, `solution to invisible MCP tools` ≈ `MCP tools`,
`loading backend/.env` ≈ `backend/.env`. Same shape as item 7, same answer.

### 8. An ANN index for the embedding stage

Row blocks cap memory at any size; the work is still quadratic. Revisit near
10,000 mentions. (1,039 today; embedding them all takes 1.25s.)

### 9. Comprehension quality — SETTLED: focused stays

Measured on 2026-09-25 on gpt-oss-120b: both labelled meetings, 3 runs each, with the ingest memo off
(`INGEST_MEMO=0`) so no run was served from cache:

| | recall | precision | traps | calls |
|---|---|---|---|---|
| **focused** (default) | **67%** [50–79] | **68%** | 0 | 12 |
| per-kind | 61% [29–86] | 52% | 0 | 24 |

Twice the calls buy no recall, a wider spread and lower precision. No traps in
either, across all 12 runs. `comprehension_strategy()` already picks focused for
a large model, so nothing changes; the question is closed.

### 11. Speaker identification for diarized transcripts — DONE

`brahmastra/ingest/speakers.py`, run in `assemble` between parsing and chunking,
and only when a transcript carries diarizer labels (`Speaker A`, `SPEAKER_01`,
`spk_2`). cocoindex's conversation example is the shape; the order is ours:

1. **No model:** a self-introduction in the speaker's own turn ("it's Sarah").
2. **The model**, for voices named only by being addressed. Held to the transcript:
   the name must occur in it, the quoted evidence must be verbatim, and two voices
   are never one person. Memoised, so a re-ingest cannot rename the owners.
3. **Fail closed:** an unnamed voice stays `(Speaker A)` and is never an owner, a
   `decided_by`/`assigned_to` target or an attendee.

**Measured.** On a diarized standup with the usual clues, 3 of 4 voices named
correctly, identically on 2 runs; the latecomer stayed unnamed, and "I'm not
sure" was not read as a name. On the two labelled meetings with their names
removed, the only voice the text identifies (Raj, addressed and answering) was
named, and nobody was named wrongly. Priya, mentioned but absent, was never
assigned. 0 wrong names in all runs.

Next only with real audio: a diarizer (whisper + pyannote) in front of this. The
text side is ready for it.

---

### 12. Windows Smart App Control blocks the venv intermittently (new, environment)

2026-09-25: `ImportError: DLL load failed ... An Application Control policy has
blocked this file` on scipy's `_rgi_cython.pyd`. The same import then passed
twice. Smart App Control is On and checks reputation online, and a failed check
blocks. It hits only HOST processes (MCP server, hooks, scripts); Docker is Linux
and unaffected.

The damage it could do was real. The embedding model fails to load, and
resolution used to rewrite the canonical map without all 85 meaning-based merges.
**Fixed in code:** a resolve whose embeddings were meant to run and did not keeps
the previous map and marks the pipeline `partial`. **The environment is the
user's decision**: allow the files in Windows Security › Protection history, or
turn Smart App Control off. It cannot be turned back on without a reset.

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
