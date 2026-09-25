"use client"

import { useCallback, useEffect, useState } from "react"
import {
  Activity,
  CircleAlert,
  CircleCheck,
  Database,
  Hourglass,
  Inbox,
  Info,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  RotateCw,
  TriangleAlert,
} from "lucide-react"

// The shape of GET /diagnostics -- see backend/brahmastra/routers/diagnostics.py.
interface NoteError {
  id: string
  title: string
  source: string | null
  error: string
  kind: string
  will_fix_itself: boolean
  will_retry: boolean
  advice: string
}
interface Diagnostics {
  generated_at: string
  workspace: string
  code: { fingerprint?: string; on_disk?: string; stale?: boolean; commit?: string; error?: string }
  pipeline: {
    running?: boolean
    stale?: boolean
    behind?: unknown
    last?: {
      finished_at?: string
      status?: string
      failed_stages?: string[]
      mode?: string
      extracted?: number
      triples_added?: number
      nodes?: number
      edges?: number
      contradictions?: number
    } | null
    error?: string
  }
  notes: {
    total?: number
    by_status?: Record<string, number>
    by_source?: Record<string, number>
    undated?: number
    retry_errors?: boolean
    errors?: NoteError[]
    pending?: { id: string; title: string; source: string | null }[]
    error?: string
  }
  llm: {
    provider?: string
    providers?: Record<string, boolean>
    extraction_model?: string
    resolution_model?: string
    judge_enabled?: boolean
    groq_keys?: { key: string; state: string; reason?: string | null; organization?: string | null; calls: number; failures: number }[]
    error?: string
  }
  transcripts: {
    id: string
    title: string
    status: string
    complete: boolean
    incomplete_chunks: number[]
    artifacts: number
    rejected: number
    error?: string | null
  }[]
  checkpoints: { queued?: number; queued_chars?: number; log_tail?: string[]; error?: string }
  graph: {
    built?: boolean
    built_at?: string
    nodes?: number
    edges?: number
    clusters?: number
    clusters_summarised?: number
    contradictions?: number
    unresolved?: { subject: string; relation: string; values: string[]; why: string }[]
    error?: string
  }
  indexes: { sessions?: Record<string, number>; code?: { chunks?: number; files?: number } }
  coercions: { total?: number; notes_affected?: number; by_kind?: Record<string, number>; top?: { kind: string; relation: string; notes: number }[] }
  attention: { level: "error" | "warn" | "info"; text: string; hint: string }[]
}

const LEVEL = {
  error: { icon: CircleAlert, box: "border-destructive/40 bg-destructive/5", text: "text-destructive" },
  warn: { icon: TriangleAlert, box: "border-amber-400/30 bg-amber-400/5", text: "text-amber-400" },
  info: { icon: Info, box: "border-border bg-card", text: "text-primary" },
} as const

function ago(iso?: string | null) {
  if (!iso) return "never"
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (Number.isNaN(s)) return iso
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function scoped(path: string, workspace: string) {
  if (workspace === "default") return path
  return `${path}${path.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(workspace)}`
}

export function DiagnosticsPanel({ workspace, backendAvailable }: { workspace: string; backendAvailable: boolean }) {
  const [data, setData] = useState<Diagnostics | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api${scoped("/diagnostics", workspace)}`, { cache: "no-store" })
      const body = await res.json()
      if (!res.ok) throw new Error(body?.detail ?? res.statusText)
      setData(body)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [workspace])

  useEffect(() => {
    setData(null)
    if (!backendAvailable) return
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [backendAvailable, load])

  /** Run one safe action, say what happened, refresh. */
  const act = async (key: string, path: string, done: (body: any) => string) => {
    setBusy(key)
    try {
      const res = await fetch(`/api${scoped(path, workspace)}`, { method: "POST" })
      const body = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(body?.detail ?? res.statusText)
      setFlash(done(body))
      await load()
    } catch (e) {
      setFlash(`Failed: ${(e as Error).message}`)
    } finally {
      setBusy(null)
    }
  }

  if (!backendAvailable) {
    return <p className="text-sm text-muted-foreground">Diagnostics need the backend. Start it and reload.</p>
  }
  if (!data) {
    return error ? <p className="text-sm text-destructive">{error}</p> : <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
  }

  const errors = data.notes.errors ?? []
  const pending = data.notes.pending ?? []

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          What is running, what is waiting, what failed and whether it will fix itself. Refreshes every 15 seconds.
        </p>
        <button
          onClick={load}
          className="flex items-center gap-1.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className="h-3 w-3" /> checked {ago(data.generated_at)}
        </button>
      </div>

      {flash && (
        <div className="rounded-md border border-border bg-secondary px-3 py-2 font-mono text-[11px] text-muted-foreground">{flash}</div>
      )}

      {/* Needs attention */}
      <section className="flex flex-col gap-2">
        <h3 className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">Needs attention</h3>
        {data.attention.length === 0 ? (
          <div className="flex items-center gap-2 rounded-lg border border-green-500/30 bg-green-500/5 p-3 text-sm text-green-400">
            <CircleCheck className="h-4 w-4" /> Nothing is stuck or waiting.
          </div>
        ) : (
          data.attention.map((a, i) => {
            const L = LEVEL[a.level]
            const Icon = L.icon
            return (
              <div key={i} className={`flex items-start gap-2.5 rounded-lg border p-3 ${L.box}`}>
                <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${L.text}`} />
                <div>
                  <p className="text-sm text-foreground">{a.text}</p>
                  <p className="text-xs text-muted-foreground">{a.hint}</p>
                </div>
              </div>
            )
          })
        )}
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        {/* Pipeline */}
        <Card title="Pipeline" icon={Activity}>
          <Row label="state">
            {data.pipeline.running ? (
              <span className="flex items-center gap-1 text-primary"><Loader2 className="h-3 w-3 animate-spin" /> running</span>
            ) : (
              "idle"
            )}
          </Row>
          <Row label="graph">
            <span className={data.pipeline.stale ? "text-amber-400" : "text-green-400"}>
              {data.pipeline.stale ? "behind the notes" : "up to date"}
            </span>
          </Row>
          <Row label="last run">
            {data.pipeline.last ? (
              <span>
                {ago(data.pipeline.last.finished_at)} &middot;{" "}
                <span className={data.pipeline.last.status === "ok" ? "text-green-400" : "text-amber-400"}>
                  {data.pipeline.last.status}
                </span>
                {(data.pipeline.last.failed_stages ?? []).length > 0 && ` (${data.pipeline.last.failed_stages!.join(", ")})`}
              </span>
            ) : (
              "none recorded"
            )}
          </Row>
          {data.pipeline.last && (
            <Row label="produced">
              {data.pipeline.last.extracted ?? 0} extracted &middot; {data.pipeline.last.nodes ?? 0} nodes &middot;{" "}
              {data.pipeline.last.edges ?? 0} edges
            </Row>
          )}
          <ActionButton
            icon={Play}
            busy={busy === "run"}
            disabled={!!data.pipeline.running}
            onClick={() => act("run", "/pipeline/run", () => "Pipeline started. The card updates when it finishes.")}
          >
            run pipeline
          </ActionButton>
        </Card>

        {/* Notes */}
        <Card title="Notes" icon={Database}>
          <Row label="total">{data.notes.total ?? 0}</Row>
          <Row label="status">
            <Counts counts={data.notes.by_status ?? {}} tone={{ error: "text-destructive", pending: "text-amber-400", done: "text-green-400" }} />
          </Row>
          <Row label="written by">
            <Counts counts={data.notes.by_source ?? {}} />
          </Row>
          <Row label="undated">
            <span title="Notes with no creation time. Contradictions involving them cannot be settled by date.">
              {data.notes.undated ?? 0}
            </span>
          </Row>
          {pending.length > 0 && (
            <div className="mt-2">
              <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                waiting to be extracted
              </p>
              <ul className="flex flex-col gap-0.5">
                {pending.slice(0, 6).map((n) => (
                  <li key={n.id} className="flex items-center gap-1.5 truncate text-xs text-foreground">
                    <Hourglass className="h-3 w-3 shrink-0 text-amber-400" /> {n.title}
                  </li>
                ))}
                {pending.length > 6 && <li className="text-xs text-muted-foreground">+{pending.length - 6} more</li>}
              </ul>
            </div>
          )}
        </Card>

        {/* Failed notes: full width, because each needs a verdict */}
        {errors.length > 0 && (
          <div className="md:col-span-2">
            <Card title={`Failed extraction · ${errors.length}`} icon={CircleAlert}>
              <p className="mb-2 text-xs text-muted-foreground">
                {data.notes.retry_errors
                  ? "Every failed note is tried again on the next pipeline run. The verdict says whether that will help."
                  : "EXTRACT_RETRY_ERRORS=0: failed notes are NOT retried automatically."}
              </p>
              <ul className="flex flex-col gap-2">
                {errors.map((e) => (
                  <li key={e.id} className="rounded-md border border-border bg-background p-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm text-foreground">{e.title}</p>
                        <p className={`text-xs ${e.will_fix_itself ? "text-amber-400" : "text-destructive"}`}>
                          {e.will_fix_itself ? "will retry" : "will fail again"} &middot; {e.advice}
                        </p>
                      </div>
                      <ActionButton
                        small
                        icon={RotateCw}
                        busy={busy === `retry-${e.id}`}
                        onClick={() => act(`retry-${e.id}`, `/diagnostics/notes/${encodeURIComponent(e.id)}/retry`, () => `“${e.title}” will be extracted on the next run.`)}
                      >
                        retry
                      </ActionButton>
                    </div>
                    <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={e.error}>
                      {e.error}
                    </p>
                  </li>
                ))}
              </ul>
              <ActionButton
                icon={RotateCw}
                busy={busy === "retry-all"}
                onClick={() => act("retry-all", "/diagnostics/notes/retry-errors", (b) => `${b.marked ?? 0} note(s) marked for the next run.`)}
              >
                retry all failed
              </ActionButton>
            </Card>
          </div>
        )}

        {/* Models and keys */}
        <Card title="Models and keys" icon={KeyRound}>
          <Row label="provider">{data.llm.provider ?? "none"}</Row>
          <Row label="extraction">{data.llm.extraction_model || "—"}</Row>
          <Row label="merge judge">
            {data.llm.judge_enabled ? (
              <span className="text-green-400">on &middot; {data.llm.resolution_model}</span>
            ) : (
              <span className="text-muted-foreground">off</span>
            )}
          </Row>
          {(data.llm.groq_keys ?? []).length > 0 && (
            <ul className="mt-2 flex flex-col gap-1">
              {data.llm.groq_keys!.map((k) => (
                <li key={k.key} className="flex items-center justify-between gap-2 font-mono text-[11px]">
                  <span className="text-muted-foreground">{k.key}</span>
                  <span className={k.state === "ready" ? "text-green-400" : k.state === "dead" ? "text-destructive" : "text-amber-400"} title={k.reason ?? ""}>
                    {k.state} &middot; {k.calls} calls
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-muted-foreground">
            Key state is this server&apos;s view. Each process keeps its own, so the scheduler may know of a limit this one has not hit yet.
          </p>
        </Card>

        {/* Graph */}
        <Card title="Graph" icon={Activity}>
          {data.graph.built ? (
            <>
              <Row label="built">{ago(data.graph.built_at)}</Row>
              <Row label="size">
                {data.graph.nodes} nodes &middot; {data.graph.edges} edges
              </Row>
              <Row label="topics">
                {data.graph.clusters_summarised}/{data.graph.clusters} summarised
              </Row>
              <Row label="contradictions">
                {data.graph.contradictions} &middot;{" "}
                <span className={(data.graph.unresolved ?? []).length ? "text-amber-400" : ""}>
                  {(data.graph.unresolved ?? []).length} unresolved
                </span>
              </Row>
              {(data.graph.unresolved ?? []).map((u, i) => (
                <p key={i} className="mt-1 text-xs text-muted-foreground">
                  <span className="font-mono text-foreground">{u.subject}</span> {u.relation}: {u.values.join(" / ")}{" "}
                  <span className="text-amber-400">({u.why.replace(/^unresolved:\s*/, "")})</span>
                </p>
              ))}
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Not built yet. Run the pipeline.</p>
          )}
        </Card>

        {/* Meetings */}
        <Card title="Meetings" icon={Inbox}>
          {data.transcripts.length === 0 ? (
            <p className="text-sm text-muted-foreground">No meetings in this workspace.</p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {data.transcripts.map((t) => (
                <li key={t.id} className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-foreground">{t.title}</p>
                    <p className="font-mono text-[11px] text-muted-foreground">
                      {t.status} &middot; {t.artifacts} items
                      {t.rejected > 0 && ` · ${t.rejected} rejected`}
                      {!t.complete && <span className="text-amber-400"> &middot; incomplete</span>}
                    </p>
                  </div>
                  {!t.complete && (
                    <ActionButton
                      small
                      icon={RotateCw}
                      busy={busy === `re-${t.id}`}
                      onClick={() => act(`re-${t.id}`, `/ingest/transcripts/${t.id}/reprocess`, () => `Reprocessing “${t.title}”.`)}
                    >
                      reprocess
                    </ActionButton>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* Session capture */}
        <Card title="Session capture" icon={Hourglass}>
          <Row label="queued">
            {data.checkpoints.queued ?? 0} capture(s) &middot; {(data.checkpoints.queued_chars ?? 0).toLocaleString()} chars
          </Row>
          {(data.checkpoints.log_tail ?? []).length > 0 && (
            <pre className="mt-2 max-h-28 overflow-auto rounded-md bg-background p-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
              {data.checkpoints.log_tail!.join("\n")}
            </pre>
          )}
          <ActionButton
            icon={Play}
            busy={busy === "drain"}
            disabled={!data.checkpoints.queued}
            onClick={() => act("drain", "/diagnostics/checkpoints/drain", (b) => (b.started ? `Distilling ${b.queued} capture(s).` : "Nothing queued."))}
          >
            distil now
          </ActionButton>
        </Card>

        {/* Indexes and code */}
        <Card title="Search indexes and code" icon={Database}>
          <Row label="sessions">
            {Object.values(data.indexes.sessions ?? {}).reduce((a, b) => a + b, 0)} pieces &middot;{" "}
            {Object.keys(data.indexes.sessions ?? {}).length} session(s)
          </Row>
          <Row label="code">
            {data.indexes.code?.chunks ?? 0} chunks &middot; {data.indexes.code?.files ?? 0} files
          </Row>
          <Row label="running code">
            <span className={data.code.stale ? "text-destructive" : "text-green-400"}>
              {data.code.stale ? "older than disk" : "current"}
            </span>{" "}
            <span className="font-mono text-muted-foreground">{data.code.commit ?? data.code.fingerprint}</span>
          </Row>
          <Row label="ontology bends">
            {data.coercions.total ?? 0} across {data.coercions.notes_affected ?? 0} notes
          </Row>
        </Card>
      </div>
    </div>
  )
}

function Card({ title, icon: Icon, children }: { title: string; icon: React.ElementType; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-4">
      <h3 className="mb-1 flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
        <Icon className="h-3.5 w-3.5" /> {title}
      </h3>
      {children}
    </section>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-sm">
      <span className="font-mono text-[11px] text-muted-foreground">{label}</span>
      <span className="text-right text-foreground">{children}</span>
    </div>
  )
}

function Counts({ counts, tone = {} }: { counts: Record<string, number>; tone?: Record<string, string> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return <>—</>
  return (
    <span className="font-mono text-[11px]">
      {entries.map(([k, v], i) => (
        <span key={k}>
          {i > 0 && " · "}
          <span className={tone[k] ?? ""}>
            {v} {k}
          </span>
        </span>
      ))}
    </span>
  )
}

function ActionButton({
  icon: Icon,
  busy,
  disabled,
  small,
  onClick,
  children,
}: {
  icon: React.ElementType
  busy?: boolean
  disabled?: boolean
  small?: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      disabled={busy || disabled}
      className={`flex items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 font-mono text-[11px] text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-50 ${
        small ? "shrink-0 px-2 py-0.5" : "mt-3 self-start px-2.5 py-1"
      }`}
    >
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Icon className="h-3 w-3" />}
      {children}
    </button>
  )
}
