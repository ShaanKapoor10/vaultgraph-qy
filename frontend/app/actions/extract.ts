"use server"

import type { RawTriple } from "@/lib/types"

export interface ExtractResult {
  ok: boolean
  triples: RawTriple[]
  error?: string
}

/**
 * Saves the note to the Python backend, runs the incremental pipeline
 * (which uses GROQ_API_KEY / ANTHROPIC_API_KEY server-side), then returns
 * the extracted triples for this note so the dashboard can show them immediately.
 *
 * This replaces the previous approach of calling an LLM directly from the
 * frontend (which required a Vercel AI Gateway and a non-existent model name).
 */
export async function extractTriples(noteId: string, content: string): Promise<ExtractResult> {
  const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8001"

  try {
    // 1. Persist the note — backend marks it as pending for extraction.
    const noteRes = await fetch(`${backendUrl}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: noteId,
        title: "Quick note",
        content,
      }),
    })
    if (!noteRes.ok) {
      const text = await noteRes.text().catch(() => "")
      throw new Error(`Backend /notes returned ${noteRes.status}: ${text}`)
    }

    // 2. Run the incremental pipeline: extract → resolve → build-graph.
    //    The backend uses GROQ_API_KEY or ANTHROPIC_API_KEY from its .env.
    const pipelineRes = await fetch(`${backendUrl}/pipeline/run`, {
      method: "POST",
    })
    if (!pipelineRes.ok) {
      const text = await pipelineRes.text().catch(() => "")
      throw new Error(`Pipeline /pipeline/run returned ${pipelineRes.status}: ${text}`)
    }

    // 3. Fetch all triples and return those belonging to this note.
    const triplesRes = await fetch(`${backendUrl}/graph/triples`)
    if (!triplesRes.ok) throw new Error(`/graph/triples returned ${triplesRes.status}`)

    const allTriples: Array<{
      id: number
      subject_text: string
      relation: string
      object_text: string
      confidence: number
      source_quote: string | null
      source_note_id: string | null
      extracted_at: string
    }> = await triplesRes.json()

    const triples: RawTriple[] = allTriples
      .filter((t) => t.source_note_id === noteId)
      .map((t) => ({
        id: String(t.id),
        subjectText: t.subject_text,
        relation: t.relation as RawTriple["relation"],
        objectText: t.object_text,
        confidence: t.confidence,
        sourceQuote: t.source_quote ?? "",
        sourceNoteId: t.source_note_id ?? "",
        extractedAt: t.extracted_at,
      }))

    return { ok: true, triples }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    console.error("[brahmastra] extractTriples error:", message)
    return { ok: false, triples: [], error: message }
  }
}
