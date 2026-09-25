import type { RelationType } from "./ontology"

export type ExtractionStatus = "pending" | "done" | "failed"

export interface Note {
  id: string
  title: string
  content: string
  lastEdited: string // ISO timestamp
  extractionStatus: ExtractionStatus
}

/** A fact extracted from a note. Subject/object are raw mention strings here. */
export interface RawTriple {
  id: string
  subjectText: string
  relation: RelationType
  objectText: string
  confidence: number
  sourceQuote: string
  sourceNoteId: string
  extractedAt: string // ISO timestamp
}

/** A cluster of raw mentions that resolve to a single canonical entity. */
export interface EntityCluster {
  canonical: string
  mentions: string[]
  /** Human-readable explanations of why each non-canonical mention merged. */
  merges: { a: string; b: string; method: string; score: number }[]
}

export interface ResolutionResult {
  clusters: EntityCluster[]
  /** maps every raw mention -> canonical name */
  canonicalMap: Record<string, string>
}

/** A directed, possibly multi-edge fact in the concept graph. */
export interface GraphEdge {
  source: string
  target: string
  relation: RelationType
  confidence: number
  sourceNoteId: string
  sourceQuote: string
  extractedAt: string
}

export interface GraphNode {
  id: string
  /** number of raw mentions that collapsed into this node */
  mentionCount: number
  degree: number
}

export interface ConceptGraph {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface CentralEntity {
  entity: string
  score: number
}

export interface ConceptCluster {
  id: number
  members: string[]
  /** One-line LLM-generated theme for the cluster. Empty if not yet summarised. */
  summary?: string
}

export interface Contradiction {
  entity: string
  relation: RelationType
  /** The backend's verdict: the value asserted most recently, or "" when the
   *  dates cannot settle it. Absent for contradictions computed locally. */
  resolvedValue?: string
  /** "newest", or "unresolved: <why>". */
  resolution?: string
  values: {
    value: string
    extractedAt: string
    /** When the NOTE asserted it -- the clock a contradiction is settled by. */
    assertedAt?: string | null
    sourceNoteId: string
    sourceQuote: string
  }[]
}

export interface PredictedLink {
  a: string
  b: string
  score: number
  commonNeighbors: string[]
}

export interface PipelineResult {
  notes: Note[]
  rawTriples: RawTriple[]
  resolution: ResolutionResult
  graph: ConceptGraph
  central: CentralEntity[]
  clusters: ConceptCluster[]
  contradictions: Contradiction[]
  predictedLinks: PredictedLink[]
}
