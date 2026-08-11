"use client"

import { useMemo, useState } from "react"
import type { Note, RawTriple, PipelineResult } from "@/lib/types"
import { runPipeline } from "@/lib/pipeline"
import { noteTitleMap } from "@/lib/viz"
import { GraphView } from "@/components/graph-view"
import { EntityDetail } from "@/components/entity-detail"
import { CentralEntities } from "@/components/panels/central-entities"
import { ConceptClusters } from "@/components/panels/concept-clusters"
import { Contradictions } from "@/components/panels/contradictions"
import { PredictedLinks } from "@/components/panels/predicted-links"
import { EntityResolution } from "@/components/panels/entity-resolution"
import { NotesPanel } from "@/components/panels/notes-panel"
import { AskPanel } from "@/components/panels/ask-panel"
import { Workflow, Network, TrendingUp, Boxes, TriangleAlert, GitMerge, Sparkles, FileText, Play, Loader2, MessageCircleQuestion } from "lucide-react"

type View = "graph" | "ask" | "central" | "clusters" | "contradictions" | "links" | "resolution" | "notes"

const TABS: { id: View; label: string; icon: React.ElementType }[] = [
  { id: "graph", label: "Graph", icon: Network },
  { id: "ask", label: "Ask", icon: MessageCircleQuestion },
  { id: "central", label: "Central", icon: TrendingUp },
  { id: "clusters", label: "Clusters", icon: Boxes },
  { id: "contradictions", label: "Contradictions", icon: TriangleAlert },
  { id: "links", label: "Predicted Links", icon: GitMerge },
  { id: "resolution", label: "Entity Resolution", icon: Sparkles },
  { id: "notes", label: "Notes", icon: FileText },
]

interface Props {
  initialNotes: Note[]
  initialTriples: RawTriple[]
  backendAvailable?: boolean
  /** Precomputed PipelineResult from the Python backend. Used on first render;
   *  falls back to running the TS pipeline once the user adds notes locally. */
  initialResult?: PipelineResult | null
}

export function Dashboard({ initialNotes, initialTriples, backendAvailable = false, initialResult = null }: Props) {
  const [notes, setNotes] = useState<Note[]>(initialNotes)
  const [triples, setTriples] = useState<RawTriple[]>(initialTriples)
  const [pipelineRunning, setPipelineRunning] = useState(false)
  const [pipelineStatus, setPipelineStatus] = useState<string | null>(null)
  const [view, setView] = useState<View>("graph")
  const [selected, setSelected] = useState<string | null>(null)
  // Track whether the user has added local notes (which invalidates the backend result)
  const [localNotesAdded, setLocalNotesAdded] = useState(false)

  // Use the precomputed backend result unless the user has added notes locally.
  const result = useMemo(() => {
    if (initialResult && !localNotesAdded) return initialResult
    return runPipeline(notes, triples)
  }, [notes, triples, initialResult, localNotesAdded])
  const noteTitles = useMemo(() => noteTitleMap(notes), [notes])

  const select = (id: string) => setSelected(id)

  const runPipelineNow = async () => {
    if (!backendAvailable) return
    setPipelineRunning(true)
    setPipelineStatus("Starting…")
    try {
      // Kick off the run — the backend runs it in the background and returns
      // immediately, since a full run (Ollama extraction + embeddings + graph
      // build) can take minutes, longer than the dev proxy holds a request open.
      const startRes = await fetch("/api/pipeline/run", { method: "POST" })
      const startRaw = await startRes.text()
      let startData: any = null
      try {
        startData = startRaw ? JSON.parse(startRaw) : null
      } catch {
        startData = null
      }
      if (!startRes.ok) {
        const msg = startData?.detail ?? startRaw?.slice(0, 140) ?? startRes.statusText
        setPipelineStatus(`Error (${startRes.status}): ${msg || "pipeline failed to start"}`)
        return
      }

      setPipelineStatus("Pipeline running…")

      // Poll for completion.
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000))
        const res = await fetch("/api/pipeline/status")
        if (!res.ok) continue
        const status = await res.json()

        if (status.state === "running" || status.state === "started") continue

        if (status.state === "error") {
          setPipelineStatus(`Error: ${status.error ?? "pipeline failed"}`)
          break
        }
        if (status.state === "skipped") {
          setPipelineStatus(`Busy: ${status.result?.skipped ?? "another run in progress"}. Try again in a moment.`)
          break
        }
        if (status.state === "done") {
          const data = status.result
          const g = data?.stages?.graph ?? {}
          const e = data?.stages?.extract ?? {}
          const r = data?.stages?.resolve ?? {}
          setPipelineStatus(
            `Pipeline complete — extracted ${e.triples_added ?? 0} triples, ` +
            `${r.clusters ?? 0} entity clusters, ` +
            `${g.nodes ?? 0} nodes / ${g.edges ?? 0} edges, ` +
            `${g.contradictions ?? 0} contradictions. Reloading…`
          )
          setLocalNotesAdded(false)
          setTimeout(() => window.location.reload(), 1200)
          break
        }
        // Unknown/idle state — stop polling rather than looping forever.
        break
      }
    } catch (e) {
      setPipelineStatus(`Error: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setPipelineRunning(false)
    }
  }

  const addNote = (note: Note, newTriples: RawTriple[]) => {
    setNotes((n) => [note, ...n])
    setTriples((t) => [...t, ...newTriples])
    setLocalNotesAdded(true)
  }

  const stats = [
    { label: "notes", value: notes.length },
    { label: "triples", value: triples.length },
    { label: "entities", value: result.resolution.clusters.length },
    { label: "edges", value: result.graph.edges.length },
    { label: "contradictions", value: result.contradictions.length, alert: result.contradictions.length > 0 },
    { label: "predicted", value: result.predictedLinks.length },
  ]

  return (
    <div className="flex h-dvh flex-col bg-background text-foreground">
      {/* Header */}
      <header className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="flex items-center gap-2.5">
            <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <Workflow className="h-4 w-4" />
            </div>
            <div>
              <h1 className="font-mono text-sm font-semibold tracking-tight text-foreground">Brahmastra</h1>
              <p className="text-[11px] text-muted-foreground">Concept Graph Engine</p>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <div className="hidden items-center gap-1.5 font-mono text-[11px] text-muted-foreground md:flex">
              {["sync", "extract", "resolve", "graph", "analyze"].map((s, i) => (
                <span key={s} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-border">→</span>}
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-secondary-foreground">{s}</span>
                </span>
              ))}
            </div>

            {/* Backend status + run button */}
            <div
              className={`hidden items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px] md:flex ${
                backendAvailable
                  ? "border-green-500/30 bg-green-500/10 text-green-400"
                  : "border-border bg-secondary text-muted-foreground"
              }`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${backendAvailable ? "bg-green-400" : "bg-muted-foreground"}`} />
              {backendAvailable ? "backend live" : "seed data"}
            </div>

            {backendAvailable && (
              <button
                onClick={runPipelineNow}
                disabled={pipelineRunning}
                className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1 font-mono text-[11px] text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {pipelineRunning ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Play className="h-3 w-3" />
                )}
                {pipelineRunning ? "running…" : "run pipeline"}
              </button>
            )}
          </div>
        </div>

        {/* Stats */}
        <div className="flex flex-wrap gap-2">
          {stats.map((s) => (
            <div
              key={s.label}
              className={`flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 ${
                s.alert ? "border-destructive/40 bg-destructive/5" : "border-border bg-card"
              }`}
            >
              <span className={`font-mono text-sm font-semibold ${s.alert ? "text-destructive" : "text-foreground"}`}>
                {s.value}
              </span>
              <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{s.label}</span>
            </div>
          ))}
        </div>

        {/* Tabs */}
        <nav className="-mb-3 flex flex-wrap gap-1 overflow-x-auto">
          {TABS.map((t) => {
            const Icon = t.icon
            const active = view === t.id
            return (
              <button
                key={t.id}
                onClick={() => setView(t.id)}
                className={`flex items-center gap-1.5 rounded-t-md border-b-2 px-3 py-2 text-sm transition-colors ${
                  active
                    ? "border-primary text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {t.label}
              </button>
            )
          })}
        </nav>
      </header>

      {/* Pipeline status bar */}
      {pipelineStatus && (
        <div className="border-b border-border bg-secondary px-4 py-2 font-mono text-[11px] text-muted-foreground sm:px-6">
          {pipelineStatus}
        </div>
      )}

      {/* Body */}
      <div className="flex min-h-0 flex-1">
        <main className="min-w-0 flex-1 overflow-hidden">
          {view === "graph" ? (
            <GraphView
              graph={result.graph}
              clusters={result.clusters}
              central={result.central}
              selected={selected}
              onSelect={setSelected}
            />
          ) : (
            <div className="h-full overflow-y-auto px-4 py-5 sm:px-6">
              <div className="mx-auto max-w-3xl">
                {view === "ask" && <AskPanel backendAvailable={backendAvailable} />}
                {view === "central" && (
                  <CentralEntities central={result.central} onSelect={select} selected={selected} />
                )}
                {view === "clusters" && <ConceptClusters clusters={result.clusters} onSelect={select} />}
                {view === "contradictions" && (
                  <Contradictions contradictions={result.contradictions} noteTitles={noteTitles} onSelect={select} />
                )}
                {view === "links" && <PredictedLinks links={result.predictedLinks} onSelect={select} />}
                {view === "resolution" && <EntityResolution resolution={result.resolution} onSelect={select} />}
                {view === "notes" && <NotesPanel notes={notes} triples={triples} onAddNote={addNote} />}
              </div>
            </div>
          )}
        </main>

        {selected && (
          <div className="hidden w-80 shrink-0 lg:block">
            <EntityDetail
              entity={selected}
              graph={result.graph}
              resolution={result.resolution}
              noteTitles={noteTitles}
              onSelect={select}
              onClose={() => setSelected(null)}
            />
          </div>
        )}
      </div>
    </div>
  )
}
