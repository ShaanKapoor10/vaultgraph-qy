"""
Ontology definition — mirrors frontend/lib/ontology.ts and ontology.yaml.

Entity types and allowed relation types.
Only triples whose (subject_type, relation, object_type) satisfy
`is_valid_triple()` are written to the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------

ENTITY_TYPES = [
    "person",
    "project",
    "concept",
    "tool",
    "organisation",
    "event",
    "date",
    "location",
    "feature",
    "file",
    "status",
    "unknown",
]

EntityType = Literal[
    "person", "project", "concept", "tool", "organisation",
    "event", "date", "location", "feature", "file", "status", "unknown"
]

# ---------------------------------------------------------------------------
# Relation types with domain / range constraints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelationDef:
    name: str
    domain: list[str]   # allowed subject entity types ("*" = any)
    range_: list[str]   # allowed object entity types  ("*" = any)
    functional: bool = False  # True ⇒ subject can have only one value at a time
    description: str = ""

    def allows(self, subject_type: str, object_type: str) -> bool:
        dom_ok = "*" in self.domain or subject_type in self.domain
        rng_ok = "*" in self.range_ or object_type in self.range_
        return dom_ok and rng_ok


RELATIONS: list[RelationDef] = [
    # Ownership / authorship
    RelationDef(
        "owns",
        domain=["person", "organisation"],
        range_=["project", "concept", "tool", "unknown"],
        description="person or org owns/is responsible for project/tool/concept",
    ),
    RelationDef(
        "created_by",
        domain=["*"],
        range_=["person", "organisation"],
        description="X was created or authored by a person/org",
    ),

    # People relations
    RelationDef(
        "reports_to",
        domain=["person"],
        range_=["person", "organisation"],
        functional=True,
        description="person's manager or reporting line",
    ),
    RelationDef(
        "works_on",
        domain=["person", "organisation"],
        range_=["project", "concept", "tool", "feature"],
        description="person or org is actively contributing to X",
    ),

    # Structural / compositional
    RelationDef(
        "part_of",
        domain=["*"],
        range_=["*"],
        description="X is a sub-component or member of Y",
    ),
    # `file` and `feature` in three domains below were added 2026-09-23, from
    # the FIRST evidence brahmastra.coercions ever collected -- and it did not
    # say what ONTOLOGY_DESIGN.md expected it to.
    #
    # The design doc waits for `unmapped_relation`: new verbs the vocabulary
    # lacks. Across 39 notes of the real corpus there were ZERO. The prompt
    # lists all eighteen relations and the model never leaves that list. The
    # signal was `domain_range` -- a KNOWN relation between types the ontology
    # refuses -- and it came from one place:
    #
    #     has_component   + file     10 notes   "live_sync.py has_component live sync watcher"
    #     provides        + file      7 notes   "evaluate.py provides fabrication detection"
    #     provides        + feature   6 notes   "NetworkX MultiDiGraph provides PageRank computation"
    #     has_component   + feature   4 notes   "Brahmastra extraction has_component extraction.py"
    #     implements      + file      4 notes   "extraction.py implements entity extraction"
    #
    # These relations were defined on 2026-06-15; `file` and `feature` joined
    # ENTITY_TYPES on 2026-06-25 and the domains were never revisited. The
    # prompt describes each relation but does not state its domain, so the
    # model followed the description correctly and a check it could not see
    # degraded the result to `related_to` -- 34% of the live graph is that
    # catch-all. CLAUDE.md's own recommended note, "The file extraction.py
    # implements retry logic", was one of them, and so was ontology.yaml's own
    # example for `implements`.
    #
    # NOT widened to `concept`, though three relations cleared the same bar for
    # it. Reading the sentences rather than the counts: "len(chunks) provides
    # call count", "groq=True provides live Groq key". `concept` is where the
    # model puts what it cannot type, and admitting it would admit that.
    RelationDef(
        "has_component",
        domain=["project", "concept", "tool", "organisation", "file", "feature"],
        range_=["*"],
        description="X contains or is composed of Y (use instead of part_of when X is the whole)",
    ),

    # Technical relations
    RelationDef(
        "depends_on",
        domain=["*"],
        range_=["*"],
        description="X requires Y to work; Y is a prerequisite",
    ),
    RelationDef(
        "implements",
        domain=["project", "person", "tool", "file"],
        range_=["concept", "tool", "feature", "unknown"],
        description="X implements a concept, standard, algorithm, or pattern",
    ),
    RelationDef(
        "uses",
        domain=["*"],
        range_=["*"],
        description="X uses/utilises Y — use only when no more specific relation fits",
    ),
    RelationDef(
        "provides",
        domain=["project", "tool", "person", "organisation", "file", "feature"],
        range_=["feature", "concept", "tool", "unknown"],
        description="X exposes or offers Y as a capability or service",
    ),
    RelationDef(
        "integrates_with",
        domain=["project", "tool"],
        range_=["project", "tool", "unknown"],
        description="X connects to or interfaces with Y",
    ),

    # State / scheduling
    RelationDef(
        "has_status",
        domain=["*"],
        range_=["status", "concept", "unknown"],
        functional=True,
        description="X's current state (e.g. 'complete', 'in progress', 'blocked')",
    ),
    RelationDef(
        "scheduled_for",
        domain=["project", "event", "unknown"],
        range_=["date"],
        functional=True,
        description="X is planned for date Y",
    ),

    # Location
    RelationDef(
        "located_in",
        domain=["*"],
        range_=["location", "organisation", "unknown"],
        functional=True,
        description="X is physically or logically located/hosted in Y",
    ),

    # Flow / blocking
    RelationDef(
        "blocks",
        domain=["project", "event", "concept", "unknown"],
        range_=["project", "event", "concept", "unknown"],
        description="X prevents Y from progressing",
    ),

    # Membership / affiliation.
    # Added because employment had no representation at all: works_on excludes
    # `organisation` from its range, so "Sapan works at Veraxion" failed
    # validation and was DISCARDED — the Veraxion entity never existed in the
    # graph. Any note about who works where was silently losing that fact.
    RelationDef(
        "employed_by",
        domain=["person"],
        range_=["organisation", "project", "unknown"],
        functional=True,   # one current employer; a change is a contradiction
        description="person works at / is employed by an organisation",
    ),
    RelationDef(
        "member_of",
        domain=["person", "organisation", "project", "tool", "concept"],
        range_=["organisation", "project", "concept", "event", "unknown"],
        description="X belongs to / is part of a group, team or body (non-employment)",
    ),

    # Catch-all
    RelationDef(
        "related_to",
        domain=["*"],
        range_=["*"],
        description="general topical link — use ONLY when no specific relation fits",
    ),
]

# ---------------------------------------------------------------------------
# Surface forms → canonical relations
# ---------------------------------------------------------------------------
#
# An LLM writes the same relation many ways ("works at", "employed by",
# "works for"). Without normalisation those either fail validation and are
# dropped, or — in an open-predicate design — become distinct edge types that
# fragment the graph so "who works at Veraxion" misses most of the answer.
#
# This keeps the strict core (domain/range checks and the `functional` flag
# that contradiction detection depends on) while accepting the phrasings a
# model actually produces.
RELATION_ALIASES: dict[str, str] = {
    "works_at": "employed_by",
    "works_for": "employed_by",
    "employed_at": "employed_by",
    "employee_of": "employed_by",
    "employer_of": "employed_by",
    "belongs_to": "member_of",
    "member": "member_of",
    "part_of_team": "member_of",
    "manages": "reports_to",        # inverse; direction is fixed on ingest
    "managed_by": "reports_to",
    "reports": "reports_to",
    "authored_by": "created_by",
    "written_by": "created_by",
    "built_by": "created_by",
    "owned_by": "owns",             # inverse
    "owner_of": "owns",
    "requires": "depends_on",
    "needs": "depends_on",
    "uses_tool": "uses",
    "utilises": "uses",
    "utilizes": "uses",
    "contains": "has_component",
    "includes": "has_component",
    "located_at": "located_in",
    "based_in": "located_in",
    "lives_in": "located_in",
    "status_is": "has_status",
    "planned_for": "scheduled_for",
    "due": "scheduled_for",
    "blocked_by": "blocks",         # inverse
    "integrates": "integrates_with",
    "connects_to": "integrates_with",
    "offers": "provides",
    "exposes": "provides",
}

# Aliases that mean the INVERSE of their canonical relation: the subject and
# object must be swapped, or the graph asserts the opposite of the note.
# "Mei manages Sarah" is "Sarah reports_to Mei", not "Mei reports_to Sarah".
INVERSE_ALIASES: frozenset[str] = frozenset({
    "manages", "owned_by", "blocked_by", "employer_of", "owner_of",
})

RELATION_NAMES: list[str] = [r.name for r in RELATIONS]


# ---------------------------------------------------------------------------
# System vocabulary -- written by code, never asked of a model
# ---------------------------------------------------------------------------
#
# Meetings reach the graph two ways. Their chunks become prose notes that
# extraction reads, which carries the CONTENT well: on the one ingested meeting,
# 15 of 16 decisions, risks, actions and questions shared their key words with
# a triple. It carries the STRUCTURE badly:
#
#     owner tied to their item          5 of 13
#     decision / risk / question kept   0 of 16
#     evidence                          a quote of generated prose
#
# "Mei will update the roadmap" arrived as `Mei --related_to--> roadmap update`.
# And a decision to "revisit in planning for Q4" arrived as a FUNCTIONAL
# `scheduled_for Q4`, a date nobody set, which would read as a contradiction
# the day a real one is.
#
# The artifacts already hold all of it -- kind, owner, verbatim quote -- as
# typed rows. So `ingest/graph_record.py` declares them straight into the graph
# with this vocabulary: deterministic, no model, every owner tied. cocoindex's
# meeting example uses the same shape (Meeting, Task and Person nodes;
# ATTENDED, DECIDED, ASSIGNED_TO), adopted here because it measured better,
# not because it was theirs.
#
# NOT in the extraction prompt, deliberately. The prompt lists ENTITY_TYPES and
# RELATIONS, and is part of every memoised extraction's cache key; putting these
# there would invalidate every cached reply and invite the model to invent
# decisions. Code writes them. Validation accepts them.

SYSTEM_ENTITY_TYPES: list[str] = [
    "meeting", "decision", "action_item", "risk", "question",
]

_ITEMS = ["decision", "action_item", "risk", "question"]

SYSTEM_RELATIONS: list[RelationDef] = [
    RelationDef("decided_by", domain=["decision"], range_=["person"],
                description="the decision was made by this person"),
    RelationDef("assigned_to", domain=["action_item"], range_=["person"],
                description="this person is accountable for the action"),
    RelationDef("raised_by", domain=["risk"], range_=["person"],
                description="this person named the risk -- not its owner"),
    RelationDef("asked_by", domain=["question"], range_=["person"],
                description="this person asked the question"),
    RelationDef("discussed_in", domain=_ITEMS, range_=["meeting"],
                description="the item came up in this meeting"),
    RelationDef("attended", domain=["person"], range_=["meeting"],
                description="the person took part in the meeting"),
]

SYSTEM_RELATION_NAMES: list[str] = [r.name for r in SYSTEM_RELATIONS]

# Every name a stored triple may carry, extractable or not. What storage
# validates against -- Neo4j refuses to build Cypher for anything else.
ALL_ENTITY_TYPES: list[str] = ENTITY_TYPES + SYSTEM_ENTITY_TYPES
ALL_RELATION_NAMES: list[str] = RELATION_NAMES + SYSTEM_RELATION_NAMES

_RELATION_MAP: dict[str, RelationDef] = {
    r.name: r for r in RELATIONS + SYSTEM_RELATIONS}


def normalise_relation(raw: str) -> tuple[str | None, bool]:
    """
    Map a model-produced relation onto the ontology.

    Returns (canonical_name, inverted). `inverted` means the alias expressed
    the relation the other way round, so the caller must swap subject and
    object — "Mei manages Sarah" is stored as "Sarah reports_to Mei".

    Returns (None, False) when nothing matches, leaving the caller to decide
    between dropping the fact and keeping it as a weaker link.
    """
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not key:
        return None, False
    if key in _RELATION_MAP:
        return key, False
    canonical = RELATION_ALIASES.get(key)
    if canonical:
        return canonical, key in INVERSE_ALIASES
    return None, False


def is_valid_triple(subject_type: str, relation: str, object_type: str) -> bool:
    """Return True if the triple satisfies ontology constraints."""
    rel = _RELATION_MAP.get(relation)
    if rel is None:
        return False
    return rel.allows(subject_type, object_type)


def is_functional(relation: str) -> bool:
    """Return True if a relation is functional (only one value per subject)."""
    rel = _RELATION_MAP.get(relation)
    return rel.functional if rel else False
