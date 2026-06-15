import { UnionFind } from "./union-find"
import { areLikelySameEntity } from "./string-similarity"
import type { RawTriple, ResolutionResult, EntityCluster } from "./types"

/** All unique subject/object strings across the raw triples. */
export function collectDistinctMentions(triples: RawTriple[]): string[] {
  const set = new Set<string>()
  for (const t of triples) {
    set.add(t.subjectText)
    set.add(t.objectText)
  }
  return [...set]
}

function countOccurrences(mention: string, triples: RawTriple[]): number {
  let n = 0
  for (const t of triples) {
    if (t.subjectText === mention) n++
    if (t.objectText === mention) n++
  }
  return n
}

/**
 * Resolve raw entity mentions into canonical entities.
 *
 * 1. Blocking — only compare mentions sharing the first 2 (case-insensitive)
 *    characters, to avoid an O(n^2) all-pairs comparison on large vaults.
 * 2. Pairwise similarity within each block builds union edges.
 * 3. Union-Find collapses transitively-similar mentions into clusters.
 * 4. Canonical name per cluster = most frequent mention (longer wins ties).
 */
export function resolveEntities(triples: RawTriple[]): ResolutionResult {
  const mentions = collectDistinctMentions(triples)
  const uf = new UnionFind(mentions)

  // Blocking by first 2 chars.
  const blocks = new Map<string, string[]>()
  for (const m of mentions) {
    const key = m.toLowerCase().slice(0, 2)
    const arr = blocks.get(key)
    if (arr) arr.push(m)
    else blocks.set(key, [m])
  }

  // Track the specific merges so the UI can explain each one.
  const mergesByRoot = new Map<string, { a: string; b: string; method: string; score: number }[]>()
  const recordMerge = (a: string, b: string, method: string, score: number) => {
    uf.union(a, b)
    const root = uf.find(a)
    const arr = mergesByRoot.get(root) ?? []
    arr.push({ a, b, method, score })
    mergesByRoot.set(root, arr)
  }

  for (const blockMentions of blocks.values()) {
    for (let i = 0; i < blockMentions.length; i++) {
      for (let j = i + 1; j < blockMentions.length; j++) {
        const verdict = areLikelySameEntity(blockMentions[i], blockMentions[j])
        if (verdict.same) {
          recordMerge(blockMentions[i], blockMentions[j], verdict.method, verdict.score)
        }
      }
    }
  }

  // Group by root and choose canonical names.
  const groups = uf.groups()
  const clusters: EntityCluster[] = []
  const canonicalMap: Record<string, string> = {}

  for (const [root, group] of groups) {
    const canonical = group.reduce((best, m) => {
      const bf = countOccurrences(best, triples)
      const mf = countOccurrences(m, triples)
      if (mf > bf) return m
      if (mf === bf && m.length > best.length) return m
      return best
    }, group[0])

    for (const m of group) canonicalMap[m] = canonical

    // Re-key recorded merges onto the current root, then attach.
    const merges: { a: string; b: string; method: string; score: number }[] = []
    for (const m of group) {
      const recorded = mergesByRoot.get(m)
      if (recorded) merges.push(...recorded)
    }
    const rootMerges = mergesByRoot.get(root)
    if (rootMerges) merges.push(...rootMerges)

    clusters.push({
      canonical,
      mentions: group.sort((a, b) => countOccurrences(b, triples) - countOccurrences(a, triples)),
      merges: dedupeMerges(merges),
    })
  }

  // Sort clusters: multi-mention (resolved) clusters first, then by size.
  clusters.sort((a, b) => b.mentions.length - a.mentions.length || a.canonical.localeCompare(b.canonical))

  return { clusters, canonicalMap }
}

function dedupeMerges(merges: { a: string; b: string; method: string; score: number }[]) {
  const seen = new Set<string>()
  const out: typeof merges = []
  for (const m of merges) {
    const key = [m.a, m.b].sort().join("|")
    if (seen.has(key)) continue
    seen.add(key)
    out.push(m)
  }
  return out
}
