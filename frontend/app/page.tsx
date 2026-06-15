import { Dashboard } from "@/components/dashboard"
import { SAMPLE_NOTES, SAMPLE_TRIPLES } from "@/lib/sample-notes"
import type { Note, RawTriple } from "@/lib/types"

/**
 * Try to load notes + triples from the Python backend.
 * Falls back to the seeded sample data if the backend is unavailable
 * (e.g. running without `vercel dev`, or first launch before any pipeline run).
 */
async function loadFromBackend(): Promise<{ notes: Note[]; triples: RawTriple[] } | null> {
  try {
    // In the Vercel Services setup the backend is reachable at /api/*.
    // In a standalone Next.js dev server the next.config rewrites handle it.
    const base = process.env.BACKEND_URL ?? "http://localhost:8000"
    const [notesRes, triplesRes] = await Promise.all([
      fetch(`${base}/notes`, { cache: "no-store" }),
      fetch(`${base}/graph/triples`, { cache: "no-store" }),
    ])
    if (!notesRes.ok || !triplesRes.ok) return null

    const backendNotes: Array<{
      id: string
      title: string
      content: string
      last_edited: string | null
      extraction_status: string
    }> = await notesRes.json()

    const backendTriples: Array<{
      id: number
      subject_text: string
      subject_type: string
      relation: string
      object_text: string
      object_type: string
      confidence: number
      source_quote: string | null
      source_note_id: string | null
      extracted_at: string
    }> = await triplesRes.json()

    // No data yet — let the seed data show
    if (backendNotes.length === 0) return null

    const notes: Note[] = backendNotes.map((n) => ({
      id: n.id,
      title: n.title,
      content: n.content,
      lastEdited: n.last_edited ?? n.id,
      extractionStatus: n.extraction_status as Note["extractionStatus"],
    }))

    const triples: RawTriple[] = backendTriples.map((t) => ({
      id: String(t.id),
      subjectText: t.subject_text,
      relation: t.relation as RawTriple["relation"],
      objectText: t.object_text,
      confidence: t.confidence,
      sourceQuote: t.source_quote ?? undefined,
      sourceNoteId: t.source_note_id ?? undefined,
      extractedAt: t.extracted_at,
    }))

    return { notes, triples }
  } catch {
    // Backend not reachable — silently fall back
    return null
  }
}

export default async function Page() {
  const backend = await loadFromBackend()
  const notes = backend?.notes ?? SAMPLE_NOTES
  const triples = backend?.triples ?? SAMPLE_TRIPLES

  return (
    <Dashboard
      initialNotes={notes}
      initialTriples={triples}
      backendAvailable={backend !== null}
    />
  )
}
