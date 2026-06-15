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
