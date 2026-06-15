import { ONTOLOGY, FUNCTIONAL_RELATIONS } from "./ontology"
import type {
  RawTriple,
  ResolutionResult,
  ConceptGraph,
  GraphEdge,
  GraphNode,
  CentralEntity,
  ConceptCluster,
  Contradiction,
  PredictedLink,
} from "./types"

/**
 * Build the concept graph from raw triples, mapping each raw mention through
 * the canonical map. The result is a directed multigraph (multiple edges
 * between the same pair are kept to preserve fact history / source attribution).
 */
export function buildConceptGraph(triples: RawTriple[], resolution: ResolutionResult): ConceptGraph {
  const { canonicalMap } = resolution
  const edges: GraphEdge[] = []
  const nodeIds = new Set<string>()
  const mentionCounts = new Map<string, number>()
  const degree = new Map<string, number>()

  const canon = (m: string) => canonicalMap[m] ?? m

  for (const t of triples) {
    const source = canon(t.subjectText)
    const target = canon(t.objectText)
    nodeIds.add(source)
    nodeIds.add(target)
    edges.push({
      source,
      target,
      relation: t.relation,
      confidence: t.confidence,
      sourceNoteId: t.sourceNoteId,
      sourceQuote: t.sourceQuote,
      extractedAt: t.extractedAt,
    })
    degree.set(source, (degree.get(source) ?? 0) + 1)
    degree.set(target, (degree.get(target) ?? 0) + 1)
  }

  // mentionCount = how many raw mention strings collapsed into each canonical node
  for (const [, canonical] of Object.entries(canonicalMap)) {
    mentionCounts.set(canonical, (mentionCounts.get(canonical) ?? 0) + 1)
  }

  const nodes: GraphNode[] = [...nodeIds].map((id) => ({
    id,
    mentionCount: mentionCounts.get(id) ?? 1,
    degree: degree.get(id) ?? 0,
  }))

  return { nodes, edges }
}

/* ------------------------------------------------------------------ */
/* PageRank — centrality over inferred semantic relations             */
/* ------------------------------------------------------------------ */

export function centralEntities(graph: ConceptGraph, topN = 10, damping = 0.85, iterations = 100): CentralEntity[] {
  const nodes = graph.nodes.map((n) => n.id)
  const N = nodes.length
  if (N === 0) return []

  // Collapse multi-edges into a simple directed graph for PageRank.
  const outLinks = new Map<string, Set<string>>()
  for (const n of nodes) outLinks.set(n, new Set())
  for (const e of graph.edges) {
    if (e.source !== e.target) outLinks.get(e.source)?.add(e.target)
  }

  let rank = new Map<string, number>(nodes.map((n) => [n, 1 / N]))

  for (let it = 0; it < iterations; it++) {
    const next = new Map<string, number>(nodes.map((n) => [n, (1 - damping) / N]))
    let danglingSum = 0
    for (const n of nodes) {
      const outs = outLinks.get(n)!
      if (outs.size === 0) danglingSum += rank.get(n)! // dangling node
    }
    for (const n of nodes) {
      const outs = outLinks.get(n)!
      const share = rank.get(n)! / (outs.size || 1)
      // distribute dangling mass uniformly
      next.set(n, next.get(n)! + (damping * danglingSum) / N)
      if (outs.size > 0) {
        for (const t of outs) next.set(t, next.get(t)! + damping * share)
      }
    }
    rank = next
  }

  return [...rank.entries()]
    .map(([entity, score]) => ({ entity, score }))
    .sort((a, b) => b.score - a.score)
    .slice(0, topN)
}

/* ------------------------------------------------------------------ */
/* Louvain — concept clusters from inferred relations                 */
/* ------------------------------------------------------------------ */

/**
 * Single-level Louvain modularity optimization (local moving phase) on the
 * undirected, weighted projection of the concept graph. Produces emergent
 * topic domains rather than manually-tagged categories.
 */
export function conceptClusters(graph: ConceptGraph): ConceptCluster[] {
  const nodes = graph.nodes.map((n) => n.id)
  if (nodes.length === 0) return []

  // Build undirected weighted adjacency (parallel edges add weight).
  const adj = new Map<string, Map<string, number>>()
  const k = new Map<string, number>() // weighted degree
  for (const n of nodes) {
    adj.set(n, new Map())
    k.set(n, 0)
  }
  let m2 = 0 // 2 * total weight
  for (const e of graph.edges) {
    if (e.source === e.target) continue
    const w = 1
    adj.get(e.source)!.set(e.target, (adj.get(e.source)!.get(e.target) ?? 0) + w)
    adj.get(e.target)!.set(e.source, (adj.get(e.target)!.get(e.source) ?? 0) + w)
    k.set(e.source, k.get(e.source)! + w)
    k.set(e.target, k.get(e.target)! + w)
    m2 += 2 * w
  }
  if (m2 === 0) {
    // no edges — every node is its own community
    return nodes.map((id, i) => ({ id: i, members: [id] }))
  }

  const community = new Map<string, string>(nodes.map((n) => [n, n]))
  const sigmaTot = new Map<string, number>(nodes.map((n) => [n, k.get(n)!]))

  let improved = true
  let passes = 0
  while (improved && passes < 20) {
    improved = false
    passes++
    for (const n of nodes) {
      const cur = community.get(n)!
      const kn = k.get(n)!
      // remove n from its community
      sigmaTot.set(cur, sigmaTot.get(cur)! - kn)

      // weights from n to each neighboring community
      const weightToComm = new Map<string, number>()
      for (const [nb, w] of adj.get(n)!) {
        const c = community.get(nb)!
        weightToComm.set(c, (weightToComm.get(c) ?? 0) + w)
      }

      let bestComm = cur
      let bestGain = 0
      for (const [c, wIn] of weightToComm) {
        const gain = wIn - (sigmaTot.get(c)! * kn) / m2
        if (gain > bestGain) {
          bestGain = gain
          bestComm = c
        }
      }

      community.set(n, bestComm)
      sigmaTot.set(bestComm, sigmaTot.get(bestComm)! + kn)
      if (bestComm !== cur) improved = true
    }
  }

  // Group nodes by community label.
  const byComm = new Map<string, string[]>()
  for (const n of nodes) {
    const c = community.get(n)!
    const arr = byComm.get(c)
    if (arr) arr.push(n)
    else byComm.set(c, [n])
  }

  return [...byComm.values()]
    .sort((a, b) => b.length - a.length)
    .map((members, i) => ({ id: i, members: members.sort() }))
}

/* ------------------------------------------------------------------ */
/* Contradiction detection — functional relations with >1 value       */
/* ------------------------------------------------------------------ */

export function detectContradictions(graph: ConceptGraph): Contradiction[] {
  const contradictions: Contradiction[] = []

  // entity -> relation -> (value -> latest edge)
  const byEntity = new Map<string, Map<string, Map<string, GraphEdge>>>()
  for (const e of graph.edges) {
    if (!FUNCTIONAL_RELATIONS.includes(e.relation)) continue
    if (!byEntity.has(e.source)) byEntity.set(e.source, new Map())
    const byRel = byEntity.get(e.source)!
    if (!byRel.has(e.relation)) byRel.set(e.relation, new Map())
    const byVal = byRel.get(e.relation)!
    const existing = byVal.get(e.target)
    if (!existing || e.extractedAt > existing.extractedAt) byVal.set(e.target, e)
  }

  for (const [entity, byRel] of byEntity) {
    for (const [relation, byVal] of byRel) {
      if (byVal.size > 1) {
        const values = [...byVal.entries()]
          .map(([value, edge]) => ({
            value,
            extractedAt: edge.extractedAt,
            sourceNoteId: edge.sourceNoteId,
            sourceQuote: edge.sourceQuote,
          }))
          .sort((a, b) => a.extractedAt.localeCompare(b.extractedAt))
        contradictions.push({ entity, relation: relation as Contradiction["relation"], values })
      }
    }
  }

  return contradictions
}

/* ------------------------------------------------------------------ */
/* Link prediction — common-neighbors heuristic                       */
/* ------------------------------------------------------------------ */

export function predictLinks(graph: ConceptGraph, topN = 10, minCommon = 2): PredictedLink[] {
  // Build undirected neighbor sets.
  const neighbors = new Map<string, Set<string>>()
  const add = (a: string, b: string) => {
    if (!neighbors.has(a)) neighbors.set(a, new Set())
    neighbors.get(a)!.add(b)
  }
  for (const e of graph.edges) {
    if (e.source === e.target) continue
    add(e.source, e.target)
    add(e.target, e.source)
  }

  const nodes = [...neighbors.keys()]
  const candidates: PredictedLink[] = []
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const u = nodes[i]
      const v = nodes[j]
      if (neighbors.get(u)!.has(v)) continue // already connected
      const common = [...neighbors.get(u)!].filter((x) => neighbors.get(v)!.has(x))
      if (common.length >= minCommon) {
        candidates.push({ a: u, b: v, score: common.length, commonNeighbors: common.sort() })
      }
    }
  }

  return candidates.sort((a, b) => b.score - a.score).slice(0, topN)
}

/** Relation color tokens for visualization (keeps the palette constrained). */
export const RELATION_DESCRIPTIONS = ONTOLOGY
