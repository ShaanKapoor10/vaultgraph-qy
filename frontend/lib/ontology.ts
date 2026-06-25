// The relation ontology. Kept in sync with backend/brahmastra/ontology.py.
// Used for contradiction detection (`functional` flag) and relation display labels.

export type RelationType =
  | "owns"
  | "created_by"
  | "reports_to"
  | "works_on"
  | "part_of"
  | "has_component"
  | "depends_on"
  | "implements"
  | "uses"
  | "provides"
  | "integrates_with"
  | "has_status"
  | "scheduled_for"
  | "located_in"
  | "blocks"
  | "related_to"

export interface RelationDef {
  functional: boolean
  description: string
}

export const ONTOLOGY: Record<RelationType, RelationDef> = {
  owns:            { functional: false, description: "person or org owns/is responsible for project/tool/concept" },
  created_by:      { functional: false, description: "X was created or authored by a person/org" },
  reports_to:      { functional: true,  description: "person's manager or reporting line" },
  works_on:        { functional: false, description: "person or org is actively contributing to X" },
  part_of:         { functional: false, description: "X is a sub-component or member of Y" },
  has_component:   { functional: false, description: "X contains or is composed of Y" },
  depends_on:      { functional: false, description: "X requires Y to work; Y is a prerequisite" },
  implements:      { functional: false, description: "X implements a concept, standard, algorithm, or pattern" },
  uses:            { functional: false, description: "X uses/utilises Y — only when no more specific relation fits" },
  provides:        { functional: false, description: "X exposes or offers Y as a capability or service" },
  integrates_with: { functional: false, description: "X connects to or interfaces with Y" },
  has_status:      { functional: true,  description: "X's current state (e.g. complete, in progress, blocked)" },
  scheduled_for:   { functional: true,  description: "X is planned for date Y" },
  located_in:      { functional: true,  description: "X is physically or logically located/hosted in Y" },
  blocks:          { functional: false, description: "X prevents Y from progressing" },
  related_to:      { functional: false, description: "general topical link — only when no specific relation fits" },
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
