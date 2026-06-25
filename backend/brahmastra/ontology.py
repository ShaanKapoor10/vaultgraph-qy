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
    RelationDef(
        "has_component",
        domain=["project", "concept", "tool", "organisation"],
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
        domain=["project", "person", "tool"],
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
        domain=["project", "tool", "person", "organisation"],
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

    # Catch-all
    RelationDef(
        "related_to",
        domain=["*"],
        range_=["*"],
        description="general topical link — use ONLY when no specific relation fits",
    ),
]

RELATION_NAMES: list[str] = [r.name for r in RELATIONS]

_RELATION_MAP: dict[str, RelationDef] = {r.name: r for r in RELATIONS}


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
