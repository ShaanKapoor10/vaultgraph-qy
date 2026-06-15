/**
 * String-similarity primitives used by entity resolution.
 *
 * The plan pairs a cheap string check (Jaro-Winkler) with an embedding-based
 * fallback (sentence-transformers) to catch cases like "Sarah" vs "Sarah Khan".
 * Running real embeddings in the browser is out of scope, so we approximate the
 * semantic fallback with token-overlap and acronym heuristics that capture the
 * same intent in an explainable, fully deterministic way.
 */

/** Jaro similarity. */
function jaro(s1: string, s2: string): number {
  if (s1 === s2) return 1
  const len1 = s1.length
  const len2 = s2.length
  if (len1 === 0 || len2 === 0) return 0

  const matchDistance = Math.max(0, Math.floor(Math.max(len1, len2) / 2) - 1)
  const s1Matches = new Array(len1).fill(false)
  const s2Matches = new Array(len2).fill(false)

  let matches = 0
  for (let i = 0; i < len1; i++) {
    const start = Math.max(0, i - matchDistance)
    const end = Math.min(i + matchDistance + 1, len2)
    for (let j = start; j < end; j++) {
      if (s2Matches[j]) continue
      if (s1[i] !== s2[j]) continue
      s1Matches[i] = true
      s2Matches[j] = true
      matches++
      break
    }
  }
  if (matches === 0) return 0

  let transpositions = 0
  let k = 0
  for (let i = 0; i < len1; i++) {
    if (!s1Matches[i]) continue
    while (!s2Matches[k]) k++
    if (s1[i] !== s2[k]) transpositions++
    k++
  }
  transpositions /= 2

  return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3
}

/** Jaro-Winkler similarity (boosts common prefixes, like jellyfish's). */
export function jaroWinkler(a: string, b: string): number {
  const s1 = a.toLowerCase()
  const s2 = b.toLowerCase()
  const j = jaro(s1, s2)
  let prefix = 0
  const maxPrefix = 4
  for (let i = 0; i < Math.min(maxPrefix, s1.length, s2.length); i++) {
    if (s1[i] === s2[i]) prefix++
    else break
  }
  return j + prefix * 0.1 * (1 - j)
}

const STOPWORDS = new Set(["the", "a", "an", "of", "project", "team", "new", "our", "system"])

function significantTokens(s: string): string[] {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 0 && !STOPWORDS.has(t))
}

/** Acronym formed from the capital letters / token initials, e.g. "PromptlyBI" -> "pb". */
function acronym(s: string): string {
  const caps = s.replace(/[^A-Z]/g, "").toLowerCase()
  if (caps.length >= 2) return caps
  return significantTokens(s)
    .map((t) => t[0])
    .join("")
}

export interface SimilarityVerdict {
  same: boolean
  method: string
  score: number
}

/**
 * Decide whether two raw mentions likely refer to the same entity, returning an
 * explanation of *why*. Cheap string check first, then token/acronym fallbacks.
 */
export function areLikelySameEntity(a: string, b: string): SimilarityVerdict {
  if (a === b) return { same: true, method: "exact", score: 1 }

  const jw = jaroWinkler(a, b)
  if (jw > 0.9) return { same: true, method: "jaro-winkler", score: jw }

  const ta = significantTokens(a)
  const tb = significantTokens(b)
  const setA = new Set(ta)
  const setB = new Set(tb)

  // Token containment: one mention's significant tokens are a subset of the
  // other's, e.g. {sarah} ⊂ {sarah, khan}. Catches "Sarah" vs "Sarah Khan".
  if (setA.size > 0 && setB.size > 0) {
    const aSubsetB = ta.every((t) => setB.has(t))
    const bSubsetA = tb.every((t) => setA.has(t))
    if (aSubsetB || bSubsetA) {
      const overlap = ta.filter((t) => setB.has(t)).length
      const score = 0.85 + 0.1 * (overlap / Math.max(setA.size, setB.size))
      return { same: true, method: "token-subset", score: Math.min(score, 0.99) }
    }
  }

  // Acronym match, e.g. "PromptlyBI" vs "Promptly BI".
  const acA = acronym(a)
  const acB = acronym(b)
  if (acA.length >= 2 && acA === acB) {
    return { same: true, method: "acronym", score: 0.86 }
  }

  return { same: false, method: "none", score: jw }
}
