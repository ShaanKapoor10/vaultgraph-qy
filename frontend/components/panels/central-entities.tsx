"use client"

import type { CentralEntity } from "@/lib/types"

interface Props {
  central: CentralEntity[]
  onSelect: (id: string) => void
  selected: string | null
}

export function CentralEntities({ central, onSelect, selected }: Props) {
  const max = central[0]?.score ?? 1
  return (
    <div className="flex flex-col gap-1.5">
      <p className="mb-1 text-sm text-muted-foreground">
        PageRank over inferred semantic relations — what your notes actually revolve around, not what you remembered to
        hyperlink.
      </p>
      {central.map((c, i) => (
        <button
          key={c.entity}
          onClick={() => onSelect(c.entity)}
          className={`group flex items-center gap-3 rounded-md border px-3 py-2 text-left transition-colors ${
            selected === c.entity ? "border-primary/60 bg-primary/5" : "border-border hover:bg-accent"
          }`}
        >
          <span className="w-5 font-mono text-xs text-muted-foreground">{i + 1}</span>
          <span className="w-44 shrink-0 truncate font-mono text-sm text-foreground">{c.entity}</span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary" style={{ width: `${(c.score / max) * 100}%` }} />
          </div>
          <span className="w-14 text-right font-mono text-xs text-muted-foreground">{c.score.toFixed(4)}</span>
        </button>
      ))}
    </div>
  )
}
