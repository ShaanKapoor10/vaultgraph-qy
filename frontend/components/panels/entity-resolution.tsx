"use client"

import type { ResolutionResult } from "@/lib/types"
import { Sparkles } from "lucide-react"

interface Props {
  resolution: ResolutionResult
  onSelect: (id: string) => void
}

const METHOD_LABEL: Record<string, string> = {
  "jaro-winkler": "Jaro-Winkler",
  "token-subset": "token subset",
  acronym: "acronym",
  exact: "exact",
}

export function EntityResolution({ resolution, onSelect }: Props) {
  const resolved = resolution.clusters.filter((c) => c.mentions.length > 1)
  const singles = resolution.clusters.filter((c) => c.mentions.length === 1)

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Pairwise similarity (string + token heuristics) builds a candidate graph; <strong>Union-Find</strong> collapses
        transitively-similar mentions into canonical entities in near-linear time.
      </p>

      <div className="flex flex-wrap gap-3 font-mono text-xs">
        <span className="rounded border border-border bg-card px-2.5 py-1 text-muted-foreground">
          {Object.keys(resolution.canonicalMap).length} mentions
        </span>
        <span className="rounded border border-border bg-card px-2.5 py-1 text-muted-foreground">
          {resolution.clusters.length} canonical entities
        </span>
        <span className="rounded border border-primary/40 bg-primary/10 px-2.5 py-1 text-primary">
          {resolved.length} merged clusters
        </span>
      </div>

      <div className="flex flex-col gap-2.5">
        {resolved.map((c) => (
          <div key={c.canonical} className="rounded-lg border border-border bg-card p-3">
            <div className="mb-2 flex items-center gap-2">
              <Sparkles className="h-3.5 w-3.5 text-primary" />
              <button onClick={() => onSelect(c.canonical)} className="font-mono text-sm text-foreground hover:underline">
                {c.canonical}
              </button>
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">canonical</span>
            </div>
            <div className="mb-2 flex flex-wrap gap-1.5">
              {c.mentions.map((m) => (
                <span
                  key={m}
                  className={`rounded px-2 py-0.5 font-mono text-xs ${
                    m === c.canonical
                      ? "bg-primary/15 text-primary"
                      : "border border-border bg-secondary text-secondary-foreground"
                  }`}
                >
                  {m}
                </span>
              ))}
            </div>
            {c.merges.length > 0 && (
              <div className="flex flex-col gap-1 border-t border-border pt-2">
                {c.merges.map((mg, i) => (
                  <div key={i} className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
                    <span className="text-foreground/70">{mg.a}</span>
                    <span>≈</span>
                    <span className="text-foreground/70">{mg.b}</span>
                    <span className="ml-auto rounded bg-muted px-1.5 py-0.5">
                      {METHOD_LABEL[mg.method] ?? mg.method} · {mg.score.toFixed(2)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {singles.length > 0 && (
        <div>
          <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            Unambiguous entities ({singles.length})
          </p>
          <div className="flex flex-wrap gap-1.5">
            {singles.map((c) => (
              <button
                key={c.canonical}
                onClick={() => onSelect(c.canonical)}
                className="rounded border border-border bg-card px-2 py-0.5 font-mono text-xs text-muted-foreground hover:border-primary/50 hover:text-foreground"
              >
                {c.canonical}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
