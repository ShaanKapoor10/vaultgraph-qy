import type { RelationType } from "./ontology"

/** Categorical cluster palette (maps to --chart-* tokens). */
export const CLUSTER_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
]

export function clusterColor(clusterId: number): string {
  return CLUSTER_COLORS[clusterId % CLUSTER_COLORS.length]
}

/** Short human label per relation. */
export const RELATION_LABEL: Record<RelationType, string> = {
  owns: "owns",
  works_on: "works on",
  depends_on: "depends on",
  blocks: "blocks",
  part_of: "part of",
  scheduled_for: "scheduled for",
  located_in: "located in",
  reports_to: "reports to",
  uses: "uses",
  related_to: "related to",
}

export function noteTitleMap(notes: { id: string; title: string }[]): Record<string, string> {
  return Object.fromEntries(notes.map((n) => [n.id, n.title]))
}
