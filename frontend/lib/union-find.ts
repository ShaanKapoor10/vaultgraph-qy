/**
 * Disjoint Set Union (Union-Find) with path compression + union by rank.
 *
 * This is the DSA core of entity resolution: pairwise similarity builds a
 * candidate graph, then Union-Find collapses *transitively*-similar mentions
 * into canonical clusters in near-O(1) amortized time per operation.
 *
 * e.g. if "Sarah" ~ "Sarah K." and "Sarah K." ~ "Sarah Khan", all three end up
 * in one cluster even if "Sarah" and "Sarah Khan" were never directly compared.
 */
export class UnionFind<T> {
  private parent = new Map<T, T>()
  private rank = new Map<T, number>()

  constructor(items: Iterable<T>) {
    for (const item of items) {
      this.parent.set(item, item)
      this.rank.set(item, 0)
    }
  }

  /** Find with path compression. */
  find(x: T): T {
    const p = this.parent.get(x)
    if (p === undefined) {
      // unknown item — treat as its own set
      this.parent.set(x, x)
      this.rank.set(x, 0)
      return x
    }
    if (p !== x) {
      const root = this.find(p)
      this.parent.set(x, root)
      return root
    }
    return x
  }

  /** Union by rank. */
  union(x: T, y: T): void {
    let rx = this.find(x)
    let ry = this.find(y)
    if (rx === ry) return

    if ((this.rank.get(rx) ?? 0) < (this.rank.get(ry) ?? 0)) {
      ;[rx, ry] = [ry, rx]
    }
    this.parent.set(ry, rx)
    if ((this.rank.get(rx) ?? 0) === (this.rank.get(ry) ?? 0)) {
      this.rank.set(rx, (this.rank.get(rx) ?? 0) + 1)
    }
  }

  /** Group every item by its root representative. */
  groups(): Map<T, T[]> {
    const out = new Map<T, T[]>()
    for (const item of this.parent.keys()) {
      const root = this.find(item)
      const arr = out.get(root)
      if (arr) arr.push(item)
      else out.set(root, [item])
    }
    return out
  }
}
