"use client"

import type { ConceptCluster } from "@/lib/types"
import { clusterColor } from "@/lib/viz"

interface Props {
  clusters: ConceptCluster[]
  onSelect: (id: string) => void
}

export function ConceptClusters({ clusters, onSelect }: Props) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Louvain community detection finds topic domains that emerge from the relation structure — no manual tagging.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {clusters
          .filter((c) => c.members.length > 0)
          .map((c) => (
            <div key={c.id} className="rounded-lg border border-border bg-card p-3">
              <div className="mb-2 flex items-center gap-2">
                <span className="h-3 w-3 rounded-full" style={{ backgroundColor: clusterColor(c.id) }} />
                <span className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
                  Cluster {c.id + 1} · {c.members.length}
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {c.members.map((m) => (
                  <button
                    key={m}
                    onClick={() => onSelect(m)}
                    className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground transition-colors hover:border-primary/50"
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>
          ))}
      </div>
    </div>
  )
}
