"use client"

import { useEffect, useMemo, useState, useTransition } from "react"
import type { Note, RawTriple } from "@/lib/types"
import { extractTriples } from "@/app/actions/extract"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { RELATION_LABEL } from "@/lib/viz"
import { FileText, Loader2, Plus, Search, X } from "lucide-react"

interface Props {
  notes: Note[]
  triples: RawTriple[]
  onAddNote: (note: Note, triples: RawTriple[]) => void
  workspace?: string
  backendAvailable?: boolean
}

/** A search hit as GET /notes?q= returns it: relevance order, never re-sorted. */
interface Hit {
  id: string
  title: string
  content: string
  last_edited?: string | null
  extraction_status?: string | null
}

export function NotesPanel({ notes, triples, onAddNote, workspace = "default", backendAvailable = false }: Props) {
  const [title, setTitle] = useState("")
  const [content, setContent] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [pending, startTransition] = useTransition()

  const triplesByNote = (id: string) => triples.filter((t) => t.sourceNoteId === id)

  const [query, setQuery] = useState("")
  const [hits, setHits] = useState<Note[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)

  // The backend's HYBRID search (BM25 + vectors), so a note sharing no words
  // with the query still turns up. Offline, a plain substring filter.
  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setHits(null)
      setSearchError(null)
      return
    }
    if (!backendAvailable) {
      const needle = q.toLowerCase()
      setHits(notes.filter((n) => `${n.title} ${n.content}`.toLowerCase().includes(needle)))
      return
    }
    const ctrl = new AbortController()
    const t = setTimeout(async () => {
      setSearching(true)
      try {
        const scope = workspace === "default" ? "" : `&workspace=${encodeURIComponent(workspace)}`
        const res = await fetch(`/api/notes?q=${encodeURIComponent(q)}&limit=30${scope}`, {
          cache: "no-store",
          signal: ctrl.signal,
        })
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
        const body: Hit[] = await res.json()
        setHits(
          body.map((h) => ({
            id: h.id,
            title: h.title,
            content: h.content,
            lastEdited: h.last_edited ?? "",
            extractionStatus: (h.extraction_status ?? "done") as Note["extractionStatus"],
          })),
        )
        setSearchError(null)
      } catch (e) {
        if ((e as Error).name !== "AbortError") setSearchError((e as Error).message)
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => {
      clearTimeout(t)
      ctrl.abort()
    }
  }, [query, backendAvailable, workspace, notes])

  const newestFirst = useMemo(
    () => [...notes].sort((a, b) => (b.lastEdited || "").localeCompare(a.lastEdited || "")),
    [notes],
  )
  const shown = hits ?? newestFirst

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

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 rounded-md border border-input bg-background px-3 py-2 focus-within:border-primary/60">
          {searching ? (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
          ) : (
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          )}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search notes by meaning, not just words"
            aria-label="Search notes"
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          {query && (
            <button onClick={() => setQuery("")} aria-label="Clear search" className="text-muted-foreground hover:text-foreground">
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <p className="font-mono text-[11px] text-muted-foreground">
          {hits
            ? `${hits.length} match${hits.length === 1 ? "" : "es"}, most relevant first${backendAvailable ? "" : " (offline: exact words only)"}`
            : `${notes.length} notes, newest first`}
        </p>
        {searchError && <p className="text-sm text-destructive">Search failed: {searchError}</p>}
      </div>

      <div className="flex flex-col gap-2.5">
        {shown.map((n) => (
          <NoteCard key={n.id} note={n} triples={triplesByNote(n.id)} />
        ))}
        {hits && hits.length === 0 && !searching && <p className="text-sm text-muted-foreground">No note matches that.</p>}
      </div>
    </div>
  )
}

function NoteCard({ note, triples }: { note: Note; triples: RawTriple[] }) {
  const [open, setOpen] = useState(false)
  const long = note.content.length > 360
  const status = note.extractionStatus as string
  return (
    <div className="rounded-lg border border-border bg-card p-3.5">
      <div className="mb-1.5 flex items-center gap-2">
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">{note.title}</span>
        {status && status !== "done" && (
          <span
            className={`rounded-full border px-1.5 py-0.5 font-mono text-[10px] ${
              status === "pending" ? "border-border text-muted-foreground" : "border-destructive/40 text-destructive"
            }`}
          >
            {status}
          </span>
        )}
        <span className="ml-auto shrink-0 font-mono text-[10px] uppercase tracking-wider text-primary">
          {triples.length} triple{triples.length === 1 ? "" : "s"}
        </span>
      </div>
      <p className={`whitespace-pre-line text-sm leading-relaxed text-muted-foreground ${open ? "" : "line-clamp-4"}`}>
        {note.content}
      </p>
      {(long || triples.length > 0) && (
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="mt-1.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          {open ? "less" : triples.length > 0 ? `more, with ${triples.length} extracted fact${triples.length === 1 ? "" : "s"}` : "more"}
        </button>
      )}
      {open && triples.length > 0 && (
        <div className="mt-2.5 flex flex-col gap-1">
          {triples.map((t) => (
            <div key={t.id} className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
              <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.subjectText}</span>
              <span className="text-primary">{RELATION_LABEL[t.relation] ?? t.relation}</span>
              <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{t.objectText}</span>
              <span className="text-muted-foreground/60">·{t.confidence.toFixed(2)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
