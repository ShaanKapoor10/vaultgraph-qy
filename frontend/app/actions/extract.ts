"use server"

import { generateText, Output } from "ai"
import { z } from "zod"
import { RELATION_TYPES, isValidRelation, formatOntologyForPrompt } from "@/lib/ontology"
import type { RawTriple } from "@/lib/types"

const tripleSchema = z.object({
  triples: z
    .array(
      z.object({
        subject: z.string().describe("entity name as written in the text, do NOT normalize"),
        relation: z.enum(RELATION_TYPES as [string, ...string[]]),
        object: z.string().describe("entity name as written in the text, do NOT normalize"),
        confidence: z.number().min(0).max(1),
        source_quote: z.string().describe("the exact sentence this fact came from"),
      }),
    )
    .describe("all factual relationships found in the note; empty array if none"),
})

const SYSTEM_PROMPT = `You extract factual relationships from personal notes as typed triples.

Use ONLY these relation types:
${formatOntologyForPrompt()}

Rules:
- subject and object should be entity names (people, projects, systems, concepts) exactly as written in the text — do NOT normalize them, that happens in a later stage.
- Only extract facts that are explicitly stated, not inferred.
- If unsure, use a lower confidence rather than omitting the fact.
- Every relation MUST be one of the allowed types above. Use "related_to" as a fallback.`

export interface ExtractResult {
  ok: boolean
  triples: RawTriple[]
  error?: string
}

/**
 * Ontology-constrained extraction. Sends the note to an LLM via the Vercel AI
 * Gateway and returns typed, validated triples with confidence + source quotes.
 * Automatically persists the note and triples to the backend if available.
 */
export async function extractTriples(noteId: string, content: string): Promise<ExtractResult> {
  try {
    const { experimental_output } = await generateText({
      model: "openai/gpt-5-mini",
      system: SYSTEM_PROMPT,
      prompt: `Extract every factual relationship from this note.\n\nNote:\n"""${content}"""`,
      experimental_output: Output.object({ schema: tripleSchema }),
    })

    const now = new Date().toISOString()
    const triples: RawTriple[] = (experimental_output?.triples ?? [])
      .filter((t) => isValidRelation(t.relation)) // drop any out-of-ontology relations
      .map((t, i) => ({
        id: `${noteId}-x${i}`,
        subjectText: t.subject.trim(),
        relation: t.relation as RawTriple["relation"],
        objectText: t.object.trim(),
        confidence: t.confidence,
        sourceQuote: t.source_quote,
        sourceNoteId: noteId,
        extractedAt: now,
      }))
      .filter((t) => t.subjectText && t.objectText)

    // Persist to backend if available
    try {
      const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000"
      await fetch(`${backendUrl}/api/notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: noteId,
          title: "Extracted from web",
          content: content.slice(0, 500),
          extraction_status: "done",
        }),
      }).catch(() => {}) // silently ignore backend errors

      if (triples.length > 0) {
        await fetch(`${backendUrl}/api/graph/triples`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            triples: triples.map((t) => ({
              subject_text: t.subjectText,
              subject_type: "entity",
              relation: t.relation,
              object_text: t.objectText,
              object_type: "entity",
              confidence: t.confidence,
              source_quote: t.sourceQuote,
              source_note_id: t.sourceNoteId,
            })),
          }),
        }).catch(() => {}) // silently ignore backend errors
      }
    } catch {
      // Backend not reachable — continue anyway
    }

    return { ok: true, triples }
  } catch (err) {
    console.log("[v0] extractTriples error:", err instanceof Error ? err.message : String(err))
    return {
      ok: false,
      triples: [],
      error: err instanceof Error ? err.message : "Extraction failed",
    }
  }
}
