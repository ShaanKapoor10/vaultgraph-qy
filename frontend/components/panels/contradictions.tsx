"use client"

import type { Contradiction } from "@/lib/types"
import { RELATION_LABEL } from "@/lib/viz"
import { AlertTriangle, CircleHelp } from "lucide-react"

interface Props {
  contradictions: Contradiction[]
  noteTitles: Record<string, string>
  onSelect: (id: string) => void
}

function fmtDate(iso: string | null | undefined) {
  if (!iso) return "undated"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return "undated"
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
}

/**
 * Which value stands. From the backend it is the value the notes asserted most
 * recently -- or none, when the dates cannot settle it, which is shown as such
 * rather than guessed. A contradiction computed locally has no verdict, so it
 * keeps the old rule: its values are in time order and the last is newest.
 */
function verdict(c: Contradiction): { value: string | null; unresolved: string | null } {
  if (c.resolution !== undefined) {
    if (c.resolvedValue) return { value: c.resolvedValue, unresolved: null }
    return { value: null, unresolved: c.resolution.replace(/^unresolved:\s*/, "") }
  }
  return { value: c.values[c.values.length - 1]?.value ?? null, unresolved: null }
}

export function Contradictions({ contradictions, noteTitles, onSelect }: Props) {
  if (contradictions.length === 0) {
    return <p className="text-sm text-muted-foreground">No contradictions detected across functional relations.</p>
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        Each functional relation (e.g. <span className="font-mono text-foreground">scheduled_for</span>,{" "}
        <span className="font-mono text-foreground">reports_to</span>) should have one value. When notes disagree, the
        value asserted most recently stands. When the dates cannot tell &mdash; both values in one note, or notes with
        no date &mdash; it is left <span className="text-amber-400">unresolved</span> for you to decide.
      </p>
      {contradictions.map((c, i) => {
        const v = verdict(c)
        return (
          <div
            key={i}
            className={`rounded-lg border p-3.5 ${
              v.unresolved ? "border-amber-400/30 bg-amber-400/5" : "border-destructive/30 bg-destructive/5"
            }`}
          >
            <div className="mb-2.5 flex flex-wrap items-center gap-2">
              {v.unresolved ? (
                <CircleHelp className="h-4 w-4 text-amber-400" />
              ) : (
                <AlertTriangle className="h-4 w-4 text-destructive" />
              )}
              <button onClick={() => onSelect(c.entity)} className="font-mono text-sm text-foreground hover:underline">
                {c.entity}
              </button>
              <span className="font-mono text-xs text-muted-foreground">{RELATION_LABEL[c.relation]}</span>
              {v.unresolved && (
                <span className="rounded bg-amber-400/15 px-1.5 py-0.5 font-mono text-[10px] uppercase text-amber-400">
                  unresolved &middot; {v.unresolved}
                </span>
              )}
            </div>
            <ol className="flex flex-col gap-2">
              {c.values.map((val, j) => (
                <li key={j} className="flex gap-3 border-l-2 border-border pl-3">
                  <div className="flex flex-col">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm font-medium text-foreground">{val.value}</span>
                      {v.value === val.value && j === c.values.findIndex((x) => x.value === v.value) && (
                        <span className="rounded bg-primary/15 px-1.5 py-0.5 font-mono text-[10px] uppercase text-primary">
                          newest
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {fmtDate(val.assertedAt ?? (c.resolution === undefined ? val.extractedAt : null))} &middot;{" "}
                      {noteTitles[val.sourceNoteId] ?? val.sourceNoteId}
                    </span>
                    <span className="mt-0.5 text-xs italic text-muted-foreground">&ldquo;{val.sourceQuote}&rdquo;</span>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        )
      })}
    </div>
  )
}
