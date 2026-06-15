"use client"

import type { Contradiction } from "@/lib/types"
import { RELATION_LABEL } from "@/lib/viz"
import { AlertTriangle } from "lucide-react"

interface Props {
  contradictions: Contradiction[]
  noteTitles: Record<string, string>
  onSelect: (id: string) => void
}

function fmtDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
}

export function Contradictions({ contradictions, noteTitles, onSelect }: Props) {
  if (contradictions.length === 0) {
    return <p className="text-sm text-muted-foreground">No contradictions detected across functional relations.</p>
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Each functional relation (e.g. <span className="font-mono text-foreground">scheduled_for</span>,{" "}
        <span className="font-mono text-foreground">reports_to</span>) should have one value. Multiple distinct values
        across notes surface as conflicts, ordered oldest to newest.
      </p>
      {contradictions.map((c, i) => (
        <div key={i} className="rounded-lg border border-destructive/30 bg-destructive/5 p-3.5">
          <div className="mb-2.5 flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-destructive" />
            <button onClick={() => onSelect(c.entity)} className="font-mono text-sm text-foreground hover:underline">
              {c.entity}
            </button>
            <span className="font-mono text-xs text-muted-foreground">{RELATION_LABEL[c.relation]}</span>
          </div>
          <ol className="flex flex-col gap-2">
            {c.values.map((v, j) => (
              <li key={j} className="flex gap-3 border-l-2 border-border pl-3">
                <div className="flex flex-col">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-medium text-foreground">{v.value}</span>
                    {j === c.values.length - 1 && (
                      <span className="rounded bg-primary/15 px-1.5 py-0.5 font-mono text-[10px] uppercase text-primary">
                        latest
                      </span>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {fmtDate(v.extractedAt)} · {noteTitles[v.sourceNoteId] ?? v.sourceNoteId}
                  </span>
                  <span className="mt-0.5 text-xs italic text-muted-foreground">&ldquo;{v.sourceQuote}&rdquo;</span>
                </div>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  )
}
