"use server"

import type { RawTriple } from "@/lib/types"
import { backendFetch } from "@/lib/backend"

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

  try {
    // 1. Persist the note — backend marks it as pending for extraction.
    const noteRes = await backendFetch("/notes", {
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
    //    /pipeline/run kicks the run off in the background and returns
    //    immediately (a run can take minutes, longer than a request should
    //    stay open), so poll /pipeline/status until it finishes.
    const pipelineRes = await backendFetch("/pipeline/run", {
      method: "POST",
    })
    if (!pipelineRes.ok) {
      const text = await pipelineRes.text().catch(() => "")
      throw new Error(`Pipeline /pipeline/run returned ${pipelineRes.status}: ${text}`)
    }

    const PIPELINE_TIMEOUT_MS = 5 * 60 * 1000
    const deadline = Date.now() + PIPELINE_TIMEOUT_MS
    for (;;) {
      const statusRes = await backendFetch("/pipeline/status", { cache: "no-store" })
      if (statusRes.ok) {
        const status = await statusRes.json()
        if (status.state === "done" || status.state === "skipped") break
        if (status.state === "error") throw new Error(`Pipeline failed: ${status.error}`)
      }
      if (Date.now() > deadline) throw new Error("Pipeline timed out waiting for /pipeline/status")
      await new Promise((r) => setTimeout(r, 1000))
    }

    // 3. Fetch all triples and return those belonging to this note.
    const triplesRes = await backendFetch("/graph/triples")
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
