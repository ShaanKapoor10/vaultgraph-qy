"use client"

import { useState } from "react"
import type { ConceptCluster } from "@/lib/types"
import { clusterColor } from "@/lib/viz"

interface Props {
  clusters: ConceptCluster[]
  onSelect: (id: string) => void
}

/** A chip label stays a label: a long entity name is cut, with the whole name on hover. */
function short(name: string, max = 40) {
  return name.length > max ? `${name.slice(0, max - 1).trimEnd()}…` : name
}

export function ConceptClusters({ clusters, onSelect }: Props) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Louvain community detection finds topic domains that emerge from the relation structure — no manual tagging.
        Statements and meeting items are placed in the topic they mention, rather than forming clusters of their own.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {clusters
          .filter((c) => c.members.length > 0)
          .map((c) => (
            <ClusterCard key={c.id} cluster={c} onSelect={onSelect} />
          ))}
      </div>
    </div>
  )
}

function ClusterCard({ cluster: c, onSelect }: { cluster: ConceptCluster; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const statements = c.statements ?? []
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: clusterColor(c.id) }} />
        <span className="truncate text-sm font-medium text-foreground" title={c.label || undefined}>
          {c.label || `Cluster ${c.id + 1}`}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
          {c.members.length}
          {statements.length > 0 && ` · ${statements.length} said`}
        </span>
      </div>
      {c.summary && <p className="mb-2 text-xs leading-snug text-muted-foreground">{c.summary}</p>}
      <div className="flex flex-wrap gap-1.5">
        {c.members.map((m) => (
          <button
            key={m}
            onClick={() => onSelect(m)}
            title={m.length > 40 ? m : undefined}
            className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground transition-colors hover:border-primary/50"
          >
            {short(m)}
          </button>
        ))}
      </div>
      {statements.length > 0 && (
        <div className="mt-2">
          <button
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="font-mono text-[11px] text-muted-foreground hover:text-foreground"
          >
            {open ? "▾" : "▸"} {statements.length} statement{statements.length > 1 ? "s" : ""} and meeting item
            {statements.length > 1 ? "s" : ""}
          </button>
          {open && (
            <ul className="mt-1 flex list-disc flex-col gap-1 pl-5 text-xs leading-snug text-muted-foreground">
              {statements.map((s) => (
                <li key={s}>
                  <button onClick={() => onSelect(s)} className="text-left hover:text-foreground">
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
