import { resolveEntities } from "./entity-resolution"
import {
  buildConceptGraph,
  centralEntities,
  conceptClusters,
  detectContradictions,
  predictLinks,
} from "./concept-graph"
import type { Note, RawTriple, PipelineResult } from "./types"

/**
 * Run the full concept-graph pipeline over a set of notes + their extracted
 * triples. Mirrors `run_full_pipeline()` from the plan:
 *   extract (done upstream) -> resolve entities -> build graph -> analyze.
 *
 * Entity resolution and graph construction are always rebuilt from raw triples,
 * which is the plan's "always a fresh, complete reflection of current content".
 */
export function runPipeline(notes: Note[], rawTriples: RawTriple[]): PipelineResult {
  const resolution = resolveEntities(rawTriples)
  const graph = buildConceptGraph(rawTriples, resolution)

  return {
    notes,
    rawTriples,
    resolution,
    graph,
    central: centralEntities(graph, 10),
    clusters: conceptClusters(graph),
    contradictions: detectContradictions(graph),
    predictedLinks: predictLinks(graph, 10),
  }
}
