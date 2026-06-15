"""
Ontology definition — mirrors frontend/lib/ontology.ts.

Entity types and allowed relation types per the Brahmastra plan.
Only triples whose (subject_type, relation, object_type) satisfy
`is_valid_triple()` are written to the DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    "unknown",
]

EntityType = Literal[
    "person", "project", "concept", "tool", "organisation", "event", "date", "unknown"
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

    def allows(self, subject_type: str, object_type: str) -> bool:
        dom_ok = "*" in self.domain or subject_type in self.domain
        rng_ok = "*" in self.range_ or object_type in self.range_
        return dom_ok and rng_ok


RELATIONS: list[RelationDef] = [
    RelationDef("owns",           ["person"],                      ["project", "concept", "tool"]),
    RelationDef("reports_to",     ["person"],                      ["person"],                    functional=True),
    RelationDef("depends_on",     ["project", "concept"],          ["project", "concept", "tool"]),
    RelationDef("implements",     ["project", "person"],           ["concept", "tool"]),
    RelationDef("scheduled_for",  ["project", "event"],            ["date"],                      functional=True),
    RelationDef("uses",           ["person", "project"],           ["tool", "concept"]),
    RelationDef("part_of",        ["*"],                           ["project", "organisation"]),
    RelationDef("related_to",     ["*"],                           ["*"]),
    RelationDef("blocks",         ["project", "event", "concept"], ["project", "event"]),
    RelationDef("created_by",     ["project", "tool", "concept"],  ["person", "organisation"]),
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
