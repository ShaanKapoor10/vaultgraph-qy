"use client"

import { useEffect, useState } from "react"
import { Loader2, CheckCircle2, TriangleAlert, CircleDashed } from "lucide-react"

/**
 * Says whether the pipeline is running and how the last run went.
 *
 * Always on screen, and polling on its own rather than only while this tab is
 * driving a run. The pipeline is started from four places -- this button, the
 * scheduler, the CLI and MCP -- and previously the dashboard could only see
 * its own: a run kicked off anywhere else was invisible, so a spinner that
 * ended in silence left "I hope it ran" as the only conclusion available.
 *
 * The backend answers from the lock and a run record on disk, so this is true
 * regardless of who started the run or whether the API was even up at the time.
 */

type RunRecord = {
  finished_at?: string | null
  status?: string | null
  failed_stages?: string[]
  extracted?: number
  triples_added?: number
  nodes?: number | null
  edges?: number | null
  contradictions?: number | null
}

type Status = {
  running?: boolean
  active?: { age_seconds?: number; stale?: boolean } | null
  last?: RunRecord | null
}

/** "4m ago" — relative, because the absolute time answers a question nobody asked. */
function ago(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function PipelineIndicator({
  scope,
  pollMs = 10000,
}: {
  /** Query string binding this to one workspace, e.g. "?workspace=office". */
  scope: string
  pollMs?: number
}) {
  const [status, setStatus] = useState<Status | null>(null)
  const [reachable, setReachable] = useState(true)

  useEffect(() => {
    let cancelled = false

    const poll = async () => {
      try {
        const res = await fetch(`/api/pipeline/status${scope}`, { cache: "no-store" })
        if (!res.ok) throw new Error(String(res.status))
        const body: Status = await res.json()
        if (!cancelled) {
          setStatus(body)
          setReachable(true)
        }
      } catch {
        if (!cancelled) setReachable(false)
      }
    }

    poll()
    // Faster while a run is in flight, so finishing feels immediate, and slow
    // the rest of the time -- this polls forever, on every open tab.
    const id = setInterval(poll, status?.running ? 3000 : pollMs)
    return () => {
      cancelled = true
      clearInterval(id)
    }
    // Re-subscribing on `running` is what switches the interval.
  }, [scope, pollMs, status?.running])

  if (!reachable || !status) return null

  const base =
    "flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px]"

  if (status.running) {
    const secs = status.active?.age_seconds
    return (
      <div
        className={`${base} border-primary/40 bg-primary/10 text-primary`}
        title="A pipeline run is in progress — it may have been started here, by the scheduler, or from the CLI"
      >
        <Loader2 className="h-3 w-3 animate-spin" />
        pipeline running{typeof secs === "number" ? ` · ${Math.round(secs)}s` : ""}
      </div>
    )
  }

  const last = status.last
  if (!last?.finished_at) {
    return (
      <div className={`${base} border-border bg-secondary text-muted-foreground`}
           title="No pipeline run has been recorded against this workspace yet">
        <CircleDashed className="h-3 w-3" />
        never run
      </div>
    )
  }

  // `partial` is the verdict worth surfacing: the run completed, so nothing
  // looks broken, but a stage failed -- which on the free LLM tier usually
  // means some notes were never extracted and the graph is incomplete.
  const bad = last.status && last.status !== "ok"
  const detail =
    `${last.status ?? "ok"} · extracted ${last.extracted ?? 0}` +
    (last.nodes != null ? ` · ${last.nodes} nodes` : "") +
    (bad && last.failed_stages?.length ? ` · failed: ${last.failed_stages.join(", ")}` : "")

  return (
    <div
      className={`${base} ${
        bad
          ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
          : "border-green-500/30 bg-green-500/10 text-green-400"
      }`}
      title={`Last pipeline run: ${detail}`}
    >
      {bad ? <TriangleAlert className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
      ran {ago(last.finished_at)}
      {bad ? ` · ${last.status}` : ""}
    </div>
  )
}
