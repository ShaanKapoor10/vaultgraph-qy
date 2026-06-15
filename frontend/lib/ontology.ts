// The relation ontology. Used by BOTH the extraction prompt (to constrain the
// LLM to a fixed, typed set of relations) and contradiction detection (via the
// `functional` flag: a functional relation should have at most one current
// value per subject).

export type RelationType =
  | "owns"
  | "works_on"
  | "depends_on"
  | "blocks"
  | "part_of"
  | "scheduled_for"
  | "located_in"
  | "reports_to"
  | "uses"
  | "related_to"

export interface RelationDef {
  functional: boolean
  description: string
}

export const ONTOLOGY: Record<RelationType, RelationDef> = {
  owns: { functional: false, description: "subject is responsible for object" },
  works_on: { functional: false, description: "subject is actively contributing to object" },
  depends_on: { functional: false, description: "object must be done before subject can proceed" },
  blocks: { functional: false, description: "subject prevents progress on object" },
  part_of: { functional: false, description: "subject is a component of object" },
  scheduled_for: { functional: true, description: "subject's deadline/date is object" },
  located_in: { functional: true, description: "subject's location is object" },
  reports_to: { functional: true, description: "subject's manager is object" },
  uses: { functional: false, description: "subject utilizes object (tool, system, etc.)" },
  related_to: { functional: false, description: "general topical relation, fallback" },
}

export const RELATION_TYPES = Object.keys(ONTOLOGY) as RelationType[]

export const FUNCTIONAL_RELATIONS = RELATION_TYPES.filter((r) => ONTOLOGY[r].functional)

export function isValidRelation(relation: string): relation is RelationType {
  return relation in ONTOLOGY
}

/** Formats the ontology into the description block injected into the extraction prompt. */
export function formatOntologyForPrompt(): string {
  return RELATION_TYPES.map((r) => `- ${r}: ${ONTOLOGY[r].description}`).join("\n")
}
