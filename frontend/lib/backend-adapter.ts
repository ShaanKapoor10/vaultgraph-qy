/**
 * backend-adapter.ts
 *
 * Converts the Python backend's /api/graph response into the
 * TypeScript PipelineResult shape used throughout the dashboard.
 *
 * Backend shape:
 *   { built_at, graph: { nodes[], edges[] }, stats: { ... } }
 *
 * The adapter normalises fields so the dashboard components work
 * identically whether data comes from the backend or the local TS pipeline.
 */

import type {
  PipelineResult,
  ConceptGraph,
  CentralEntity,
  ConceptCluster,
  Contradiction,
  PredictedLink,
  ResolutionResult,
  Note,
  RawTriple,
} from "@/lib/types"
import type { RelationType } from "@/lib/ontology"

// ---------------------------------------------------------------------------
// Backend JSON shapes (raw fetch types)
// ---------------------------------------------------------------------------

export interface BackendNode {
  id: string
  label: string
  type: string
  pagerank: number
  cluster: number
}

export interface BackendEdge {
  source: string
  target: string
  relation: string
  source_quote: string
  note_id: string
  confidence: number
}

export interface BackendContradictionEvidence {
  object: string
  source_quote: string
  note_id: string
  extracted_at: string
}

export interface BackendContradiction {
  subject: string
  relation: string
  conflicting_values: string[]
  resolved_value: string
  evidence: BackendContradictionEvidence[]
}

export interface BackendPredictedLink {
  source: string
  target: string
  jaccard: number
  common_neighbors: number
  score: number
}

export interface BackendCentralEntity {
  entity: string
  pagerank: number
}

export interface BackendConceptCluster {
  id: number
  members: string[]
  size: number
  summary?: string
}

export interface BackendEntityCluster {
  cluster_id: string
  canonical_name: string
  mentions: string[]
  size: number
}

export interface BackendStats {
  nodes: number
  edges: number
  central_entities: BackendCentralEntity[]
  concept_clusters: BackendConceptCluster[]
  contradictions: BackendContradiction[]
  predicted_links: BackendPredictedLink[]
  entity_clusters: BackendEntityCluster[]
}

export interface BackendGraphResponse {
  built_at: string | null
  graph: {
    nodes: BackendNode[]
    edges: BackendEdge[]
  }
  stats: BackendStats
}

// ---------------------------------------------------------------------------
// Adapter
// ---------------------------------------------------------------------------

/**
 * Convert the backend /api/graph response into a full PipelineResult.
 * Notes and triples are provided separately (from /api/notes and /api/graph/triples).
 */
export function adaptBackendGraph(
  response: BackendGraphResponse,
  notes: Note[],
  triples: RawTriple[],
): PipelineResult {
  const { graph, stats } = response

  // -- ConceptGraph --------------------------------------------------------
  const degreeMap: Record<string, number> = {}
  const mentionCountMap: Record<string, number> = {}

  for (const e of graph.edges) {
    degreeMap[e.source] = (degreeMap[e.source] ?? 0) + 1
    degreeMap[e.target] = (degreeMap[e.target] ?? 0) + 1
  }

  // Mention count: count how many raw triples mention each canonical node
  for (const t of triples) {
    mentionCountMap[t.subjectText] = (mentionCountMap[t.subjectText] ?? 0) + 1
    mentionCountMap[t.objectText] = (mentionCountMap[t.objectText] ?? 0) + 1
  }

  const conceptGraph: ConceptGraph = {
    nodes: graph.nodes.map((n) => ({
      id: n.id,
      mentionCount: mentionCountMap[n.id] ?? 1,
      degree: degreeMap[n.id] ?? 0,
    })),
    edges: graph.edges.map((e) => ({
      source: e.source,
      target: e.target,
      relation: e.relation as RelationType,
      confidence: e.confidence,
      sourceNoteId: e.note_id ?? "",
      sourceQuote: e.source_quote ?? "",
      extractedAt: "",
    })),
  }

  // -- Central entities ----------------------------------------------------
  const central: CentralEntity[] = stats.central_entities.map((c) => ({
    entity: c.entity,
    score: c.pagerank,
  }))

  // -- Concept clusters (Louvain) ------------------------------------------
  const clusters: ConceptCluster[] = stats.concept_clusters.map((c) => ({
    id: c.id,
    members: c.members,
    summary: c.summary ?? "",
  }))

  // -- Contradictions -------------------------------------------------------
  const contradictions: Contradiction[] = stats.contradictions.map((c) => ({
    entity: c.subject,
    relation: c.relation as RelationType,
    values: c.evidence.map((ev) => ({
      value: ev.object,
      extractedAt: ev.extracted_at ?? "",
      sourceNoteId: ev.note_id ?? "",
      sourceQuote: ev.source_quote ?? "",
    })),
  }))

  // -- Predicted links ------------------------------------------------------
  const predictedLinks: PredictedLink[] = stats.predicted_links.map((p) => ({
    a: p.source,
    b: p.target,
    score: p.score,
    commonNeighbors: [],   // backend returns count, not the list; UI shows the score
  }))

  // -- Entity resolution result --------------------------------------------
  const canonicalMap: Record<string, string> = {}
  for (const cluster of stats.entity_clusters) {
    for (const mention of cluster.mentions) {
      canonicalMap[mention] = cluster.canonical_name
    }
  }

  const resolution: ResolutionResult = {
    clusters: stats.entity_clusters.map((c) => ({
      canonical: c.canonical_name,
      mentions: c.mentions,
      merges: [],   // merge-edge details not included in the /graph summary endpoint
    })),
    canonicalMap,
  }

  return {
    notes,
    rawTriples: triples,
    resolution,
    graph: conceptGraph,
    central,
    clusters,
    contradictions,
    predictedLinks,
  }
}
