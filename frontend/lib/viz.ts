import type { RelationType } from "./ontology"

/** Categorical cluster palette (literal values, self-contained for SVG). */
export const CLUSTER_COLORS = [
  "oklch(0.8 0.15 78)", // amber
  "oklch(0.72 0.12 200)", // cyan
  "oklch(0.7 0.13 145)", // green
  "oklch(0.68 0.16 15)", // rose
  "oklch(0.7 0.1 255)", // blue
  "oklch(0.74 0.14 330)", // magenta
  "oklch(0.78 0.13 110)", // lime
  "oklch(0.72 0.11 40)", // orange
]

export function clusterColor(clusterId: number): string {
  return CLUSTER_COLORS[clusterId % CLUSTER_COLORS.length]
}

/** Short human label per relation. Keep in sync with lib/ontology.ts. */
export const RELATION_LABEL: Record<RelationType, string> = {
  owns: "owns",
  created_by: "created by",
  reports_to: "reports to",
  works_on: "works on",
  part_of: "part of",
  has_component: "has component",
  depends_on: "depends on",
  implements: "implements",
  uses: "uses",
  provides: "provides",
  integrates_with: "integrates with",
  has_status: "has status",
  scheduled_for: "scheduled for",
  located_in: "located in",
  // Added when the ontology grew to 18 relations and this map stayed at 16.
  // A missing label is not cosmetic: these are the relations that carry
  // employment and membership, so those edges rendered unlabelled.
  employed_by: "employed by",
  member_of: "member of",
  blocks: "blocks",
  related_to: "related to",
}

export function noteTitleMap(notes: { id: string; title: string }[]): Record<string, string> {
  return Object.fromEntries(notes.map((n) => [n.id, n.title]))
}
