"use client"

import type { ConceptGraph, ResolutionResult } from "@/lib/types"
import { RELATION_LABEL } from "@/lib/viz"
import { X } from "lucide-react"

interface Props {
  entity: string
  graph: ConceptGraph
  resolution: ResolutionResult
  noteTitles: Record<string, string>
  onSelect: (id: string) => void
  onClose: () => void
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" })
}

export function EntityDetail({ entity, graph, resolution, noteTitles, onSelect, onClose }: Props) {
  const outgoing = graph.edges.filter((e) => e.source === entity)
  const incoming = graph.edges.filter((e) => e.target === entity)
  const cluster = resolution.clusters.find((c) => c.canonical === entity)

  return (
    <aside className="flex h-full w-full flex-col border-l border-border bg-sidebar">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="min-w-0">
          <p className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">entity</p>
          <h2 className="truncate font-mono text-sm text-foreground">{entity}</h2>
        </div>
        <button onClick={onClose} className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {cluster && cluster.mentions.length > 1 && (
          <section className="mb-4">
            <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
              resolved from
            </p>
            <div className="flex flex-wrap gap-1.5">
              {cluster.mentions.map((m) => (
                <span key={m} className="rounded border border-border bg-card px-2 py-0.5 font-mono text-[11px]">
                  {m}
                </span>
              ))}
            </div>
          </section>
        )}

        <section className="mb-4">
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            outgoing ({outgoing.length})
          </p>
          <div className="flex flex-col gap-1.5">
            {outgoing.map((e, i) => (
              <Fact
                key={i}
                rel={RELATION_LABEL[e.relation]}
                other={e.target}
                quote={e.sourceQuote}
                meta={`${fmtDate(e.extractedAt)} · ${noteTitles[e.sourceNoteId] ?? e.sourceNoteId}`}
                onSelect={() => onSelect(e.target)}
              />
            ))}
            {outgoing.length === 0 && <p className="text-xs text-muted-foreground">none</p>}
          </div>
        </section>

        <section>
          <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            incoming ({incoming.length})
          </p>
          <div className="flex flex-col gap-1.5">
            {incoming.map((e, i) => (
              <Fact
                key={i}
                rel={RELATION_LABEL[e.relation]}
                other={e.source}
                incoming
                quote={e.sourceQuote}
                meta={`${fmtDate(e.extractedAt)} · ${noteTitles[e.sourceNoteId] ?? e.sourceNoteId}`}
                onSelect={() => onSelect(e.source)}
              />
            ))}
            {incoming.length === 0 && <p className="text-xs text-muted-foreground">none</p>}
          </div>
        </section>
      </div>
    </aside>
  )
}

function Fact({
  rel,
  other,
  quote,
  meta,
  incoming,
  onSelect,
}: {
  rel: string
  other: string
  quote: string
  meta: string
  incoming?: boolean
  onSelect: () => void
}) {
  return (
    <div className="rounded-md border border-border bg-card p-2">
      <div className="flex items-center gap-1.5 font-mono text-[11px]">
        {incoming && <span className="text-muted-foreground">↳</span>}
        <span className="text-primary">{rel}</span>
        <button onClick={onSelect} className="truncate text-foreground hover:underline">
          {other}
        </button>
      </div>
      <p className="mt-1 text-[11px] italic leading-snug text-muted-foreground">&ldquo;{quote}&rdquo;</p>
      <p className="mt-0.5 text-[10px] text-muted-foreground/70">{meta}</p>
    </div>
  )
}
