import { Dashboard } from "@/components/dashboard"
import { SAMPLE_NOTES, SAMPLE_TRIPLES } from "@/lib/sample-notes"
import type { Note, RawTriple, PipelineResult } from "@/lib/types"
import { adaptBackendGraph } from "@/lib/backend-adapter"
import { backendFetch } from "@/lib/backend"
import type { WorkspaceOption } from "@/components/workspace-switcher"
import type { BackendGraphResponse } from "@/lib/backend-adapter"

/**
 * Try to load notes, triples, and the precomputed graph from the Python backend.
 * Falls back to the seeded sample data if the backend is unavailable
 * (e.g. running without `vercel dev`, or first launch before any pipeline run).
 */
async function loadFromBackend(workspace: string): Promise<{
  notes: Note[]
  triples: RawTriple[]
  result: PipelineResult | null
} | null> {
  // Every request names the workspace it wants. The API resolves it per
  // request, so leaving it off silently returns whichever graph the server
  // process defaults to -- which is how the dashboard could only ever show one.
  const scope = workspace === "default" ? "" : `?workspace=${encodeURIComponent(workspace)}`

  try {
    // backendFetch attaches BRAHMASTRA_API_KEY; this runs on the server, so
    // the key never reaches the browser.
    const [notesRes, triplesRes, graphRes] = await Promise.all([
      backendFetch(`/notes${scope}`, { cache: "no-store" }),
      backendFetch(`/graph/triples${scope}`, { cache: "no-store" }),
      backendFetch(`/graph${scope}`, { cache: "no-store" }),
    ])
    if (!notesRes.ok || !triplesRes.ok || !graphRes.ok) return null

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

    const graphPayload: BackendGraphResponse = await graphRes.json()

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
      sourceQuote: t.source_quote ?? "",
      sourceNoteId: t.source_note_id ?? "",
      extractedAt: t.extracted_at,
    }))

    // Use the precomputed graph if the backend has built one (nodes > 0)
    const result =
      graphPayload.graph.nodes.length > 0
        ? adaptBackendGraph(graphPayload, notes, triples)
        : null

    return { notes, triples, result }
  } catch {
    // Backend not reachable — silently fall back
    return null
  }
}

/** Workspaces the backend knows about. Empty when it is unreachable. */
async function loadWorkspaces(): Promise<WorkspaceOption[]> {
  try {
    const res = await backendFetch("/workspaces", { cache: "no-store" })
    if (!res.ok) return []
    const body: { workspaces?: WorkspaceOption[] } = await res.json()
    return body.workspaces ?? []
  } catch {
    return []
  }
}

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workspace?: string }>
}) {
  // The workspace lives in the URL so a selection is shareable and survives a
  // reload, and so the server component below actually re-renders with the
  // other graph rather than filtering one it already fetched.
  const workspace = (await searchParams).workspace?.trim() || "default"

  const [backend, workspaces] = await Promise.all([
    loadFromBackend(workspace),
    loadWorkspaces(),
  ])
  const notes = backend?.notes ?? SAMPLE_NOTES
  const triples = backend?.triples ?? SAMPLE_TRIPLES
  const initialResult = backend?.result ?? null

  return (
    <Dashboard
      initialNotes={notes}
      initialTriples={triples}
      backendAvailable={backend !== null}
      initialResult={initialResult}
      workspace={workspace}
      workspaces={workspaces.length > 0 ? workspaces : [{ id: workspace }]}
    />
  )
}
