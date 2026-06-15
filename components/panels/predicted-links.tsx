"use client"

import type { PredictedLink } from "@/lib/types"
import { GitMerge } from "lucide-react"

interface Props {
  links: PredictedLink[]
  onSelect: (id: string) => void
}

export function PredictedLinks({ links, onSelect }: Props) {
  if (links.length === 0) {
    return <p className="text-sm text-muted-foreground">No high-confidence link predictions yet.</p>
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Common-neighbors heuristic: entity pairs that share connections but aren&apos;t directly linked are likely
        related — a relationship the structure implies but no note has stated yet.
      </p>
      {links.map((l, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-3">
          <div className="flex items-center gap-2">
            <GitMerge className="h-4 w-4 text-primary" />
            <button onClick={() => onSelect(l.a)} className="font-mono text-sm text-foreground hover:underline">
              {l.a}
            </button>
            <span className="text-muted-foreground">⟷</span>
            <button onClick={() => onSelect(l.b)} className="font-mono text-sm text-foreground hover:underline">
              {l.b}
            </button>
            <span className="ml-auto rounded bg-primary/15 px-2 py-0.5 font-mono text-xs text-primary">
              score {l.score}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">via</span>
            {l.commonNeighbors.map((n) => (
              <button
                key={n}
                onClick={() => onSelect(n)}
                className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground hover:border-primary/50"
              >
                {n}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
