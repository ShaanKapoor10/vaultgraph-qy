"use client"

import { useState, useTransition } from "react"
import type { Note, RawTriple } from "@/lib/types"
import { extractTriples } from "@/app/actions/extract"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { RELATION_LABEL } from "@/lib/viz"
import { FileText, Loader2, Plus } from "lucide-react"

interface Props {
  notes: Note[]
  triples: RawTriple[]
  onAddNote: (note: Note, triples: RawTriple[]) => void
}

export function NotesPanel({ notes, triples, onAddNote }: Props) {
  const [title, setTitle] = useState("")
  const [content, setContent] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, startTransition] = useTransition()

  const triplesByNote = (id: string) => triples.filter((t) => t.sourceNoteId === id)

  const handleExtract = () => {
    if (!content.trim()) return
    setError(null)
    const id = `user-${Date.now()}`
    startTransition(async () => {
      const res = await extractTriples(id, content.trim())
      if (!res.ok) {
        setError(res.error ?? "Extraction failed")
        return
      }
      const note: Note = {
        id,
        title: title.trim() || "Untitled note",
        content: content.trim(),
        lastEdited: new Date().toISOString(),
        extractionStatus: "done",
      }
      onAddNote(note, res.triples)
      setTitle("")
      setContent("")
    })
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-3 flex items-center gap-2">
          <Plus className="h-4 w-4 text-primary" />
          <h3 className="text-sm font-medium text-foreground">Add a note & extract live</h3>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">
          The note is sent to an ontology-constrained LLM (via the AI Gateway). The returned triples flow through entity
          resolution and the graph rebuilds instantly.
        </p>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Note title"
          className="mb-2 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:border-primary/60"
        />
        <Textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="e.g. Priya owns the search revamp. The search revamp depends on the auth migration and is scheduled for May 10."
          rows={4}
          className="mb-3 resize-none bg-background font-mono text-sm"
        />
        {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
        <Button onClick={handleExtract} disabled={pending || !content.trim()} className="gap-2">
          {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          {pending ? "Extracting…" : "Extract triples"}
        </Button>
      </div>

      <div className="flex flex-col gap-2.5">
        {notes.map((n) => {
          const ts = triplesByNote(n.id)
          return (
            <div key={n.id} className="rounded-lg border border-border bg-card p-3.5">
              <div className="mb-1.5 flex items-center gap-2">
                <FileText className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm font-medium text-foreground">{n.title}</span>
                <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-primary">
                  {ts.length} triple{ts.length === 1 ? "" : "s"}
                </span>
              </div>
              <p className="mb-2.5 text-sm leading-relaxed text-muted-foreground">{n.content}</p>
              <div className="flex flex-col gap-1">
                {ts.map((t) => (
                  <div key={t.id} className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
                    <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.subjectText}</span>
                    <span className="text-primary">{RELATION_LABEL[t.relation]}</span>
                    <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.objectText}</span>
                    <span className="text-muted-foreground/60">·{t.confidence.toFixed(2)}</span>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
