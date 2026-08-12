# Ontology Design — how specific should relations be?

Why Brahmastra keeps a fixed relation vocabulary rather than letting the model
emit any predicate, and why that vocabulary must never delete a fact.

---

## The failure that prompted this

A note said *"Sapan works at Veraxion."* The graph ended up with
`Sapan related_to Saan Kapoor` and **no Veraxion entity at all**.

Two separate causes:

1. There was no relation for employment. `works_on` excludes `organisation`
   from its range, and nothing else fit.
2. `_validate_triple` returned False for anything off-ontology, and the caller
   **discarded** those triples — counting them in `triples_skipped` while
   throwing the content away.

So every note about who works where was silently losing that fact. The count
was visible; the loss was not.

---

## Three possible designs

### A. Closed vocabulary, drop what does not fit (what we had)

Validate `(subject_type, relation, object_type)` and reject the rest.

- **Buys:** domain/range catches model nonsense (`PostgreSQL reports_to Sarah`);
  the `functional` flag is what contradiction detection is built on; a fixed
  set maps onto indexed Neo4j relationship types.
- **Costs:** silent data loss. Personal knowledge is open-ended and will always
  exceed any fixed list.

### B. Open predicates (OpenIE style)

Let the model emit any verb phrase and store it verbatim.

- **Buys:** nothing is ever lost.
- **Costs:** `works at`, `works for`, `employed by` and `is employed at` become
  four different edge types, so "who works at Veraxion" matches a quarter of
  the answer. No domain/range means no nonsense filter. `functional` cannot be
  defined over an open set, so contradiction detection stops working — and
  that is one of the things this product is *for*.

### C. Strict core that degrades (what we do now)

Keep the closed vocabulary for the semantics that depend on it, and make
failure lossy in precision rather than in content.

1. **Normalise surface forms.** `RELATION_ALIASES` maps the phrasings a model
   actually produces onto canonical relations, so the graph does not fragment.
2. **Handle inverses explicitly.** Some aliases mean the opposite direction.
   `INVERSE_ALIASES` swaps subject and object, so *"Mei manages Sarah"* is
   stored as `Sarah reports_to Mei` rather than asserting the reverse.
3. **Degrade, never delete.** An unmappable relation, or a real relation with
   argument types it does not admit, becomes `related_to` — defined over `*`/`*`,
   so it always validates. The connection survives; only its precision is lost.
4. **Report every coercion.** `extract_note` returns a `coercions` list
   (`unmapped_relation:frobnicates`, `domain_range:works_on(person->organisation)`).

Only genuinely unusable input is still dropped: a missing or empty endpoint,
or confidence below the 0.4 floor.

---

## So: how specific should relations be?

**Specific enough to carry meaning the product acts on; no more.**

A relation earns its place when something depends on distinguishing it:

- **`functional` semantics.** `employed_by` is functional — one current
  employer — so a second value is a contradiction worth surfacing. That is
  only expressible with a named relation.
- **A traversal someone actually runs.** `reports_to` supports "who does
  Sarah's manager also manage". `related_to` supports nothing.
- **Domain/range that rejects nonsense.**

If none of those apply, the distinction is decoration — the extra relation adds
prompt length and a chance for the model to pick wrongly, for no query benefit.

**Growth should be driven by evidence, not anticipation.** Do not add relations
speculatively. Run extraction, read the `coercions` output, and promote a
relation when it keeps appearing as `unmapped_relation:` — that is data telling
you the vocabulary is short. `employed_by` and `member_of` were added on
exactly that basis: a real note lost a real fact.

---

## Current vocabulary

18 relations, 5 of them functional (`reports_to`, `has_status`,
`scheduled_for`, `located_in`, `employed_by`).

Defined in three files that must stay in sync:

- `backend/brahmastra/ontology.py` — source of truth, with domain/range
- `frontend/lib/ontology.ts` — display names and functional flags
- `ontology.yaml` — spec with examples

---

## Changing the ontology

1. Add the relation to all three files, with the narrowest domain/range that
   is still true.
2. Set `functional` only if a subject may hold **one** value at a time —
   this directly drives contradiction detection.
3. Add surface forms to `RELATION_ALIASES`; if any states the inverse, add it
   to `INVERSE_ALIASES` too.
4. Give the extraction prompt a concrete good/bad example. The model follows
   examples more reliably than descriptions.
5. Re-extract with `run_pipeline(full=True)`. Existing triples are not
   retro-fitted — they were extracted under the old vocabulary.
