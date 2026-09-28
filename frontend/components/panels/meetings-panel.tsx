"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Ban,
  CalendarDays,
  CircleAlert,
  Clock,
  Loader2,
  Mic,
  RefreshCw,
  RotateCw,
  Search,
  Undo2,
  Upload,
  Users,
} from "lucide-react"

/** One meeting in the list, as GET /ingest/transcripts returns it. */
interface TranscriptRow {
  id: string
  title: string
  status: "pending" | "processing" | "done" | "error"
  error?: string | null
  occurred_at?: string | null
  created_at?: string | null
  chunk_count?: number | null
  mode?: string | null
  progress?: Progress
}

/** How a session is read (GET /ingest/modes): a meeting, a lecture, ... */
interface ModeInfo {
  id: string
  label: string
  description: string
  kinds: { id: string; label: string; person: string }[]
  points_kind: string | null
  points_label: string
  examples: string[]
}

/** The whole session in a few lines, written from the part notes. */
interface Overview {
  headline: string | null
  summary: string | null
  themes: { title: string; points: string[] }[]
  error: string | null
}

// Shown until GET /ingest/modes answers, and if it never does.
const FALLBACK_MODES: ModeInfo[] = [
  {
    id: "meeting",
    label: "Meeting",
    description: "People deciding and dividing up work: decisions, action items, risks, open questions.",
    kinds: [
      { id: "decision", label: "Decisions", person: "decided by" },
      { id: "action_item", label: "Action items", person: "owner" },
      { id: "risk", label: "Risks", person: "raised by" },
      { id: "open_question", label: "Open questions", person: "asked by" },
    ],
    points_kind: null,
    points_label: "Discussion points",
    examples: [],
  },
]

/** Where a run is. Findings only land once every part is read. */
interface Progress {
  status: string
  stage: string
  parts_total: number
  parts_read: number
  parts_failed: number
}

/** One part of the transcript as it was read: its summary, or why it failed. */
interface Part {
  idx: number
  status: string
  error: string | null
  topic: string | null
  summary: string | null
  start_time: string | null
  end_time: string | null
  speakers: string[] | null
  points: { id: string; statement: string; said_by: string | null }[]
}

/** One finding, as GET /ingest/transcripts/{id}/meeting returns it. */
interface Item {
  id: string
  kind: string
  statement: string
  rationale: string | null
  owner: string | null
  said_by: string | null
  quote: string | null
  due: string | null
  start_time: string | null
  mentions: number
  superseded_by: string | null
  rejected: boolean
}

interface Meeting {
  id: string
  title: string
  occurred_at: string | null
  status: string
  error: string | null
  participants: string[]
  unnamed_voices: string[]
  mode?: ModeInfo
  overview?: Overview | null
  progress?: Progress
  parts?: Part[]
  items: Item[]
}

const running = (status: string | undefined) => status === "pending" || status === "processing"

/** HH:MM:SS, without the milliseconds a VTT file carries. */
function clock(ts: string | null | undefined) {
  return ts ? ts.replace(/[.,]\d+$/, "") : null
}

function ProgressBar({ progress, compact = false }: { progress: Progress; compact?: boolean }) {
  const pct = progress.parts_total ? Math.round((progress.parts_read / progress.parts_total) * 100) : 0
  return (
    <div className={`flex flex-col ${compact ? "gap-1" : "gap-1.5"}`}>
      <div className="flex items-center gap-1.5 font-mono text-[11px] text-primary">
        <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
        <span className="truncate">{progress.stage}</span>
      </div>
      <div
        className="h-1 overflow-hidden rounded-full bg-secondary"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={progress.stage}
      >
        <div className="h-full bg-primary transition-[width] duration-500" style={{ width: `${pct}%` }} />
      </div>
      {!compact && progress.parts_failed > 0 && (
        <span className="font-mono text-[11px] text-destructive">
          {progress.parts_failed} part{progress.parts_failed > 1 ? "s" : ""} failed; they are retried on the next run
        </span>
      )}
    </div>
  )
}

/** The person an item is attributed to -- the same rule the graph uses. */
function personOf(item: Item): string | null {
  return item.kind === "action_item" ? item.owner : item.said_by
}

function fmtDate(iso: string | null | undefined) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
}

const STATUS_STYLE: Record<string, string> = {
  done: "border-green-500/30 bg-green-500/10 text-green-400",
  processing: "border-primary/30 bg-primary/10 text-primary",
  pending: "border-border bg-secondary text-muted-foreground",
  error: "border-destructive/40 bg-destructive/10 text-destructive",
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { cache: "no-store", ...init })
  const text = await res.text()
  const body = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error(body?.detail ?? `${res.status} ${res.statusText}`)
  return body as T
}

/** Append the workspace to a path that may already carry a query string. */
function scoped(path: string, workspace: string) {
  if (workspace === "default") return path
  return `${path}${path.includes("?") ? "&" : "?"}workspace=${encodeURIComponent(workspace)}`
}

export function MeetingsPanel({
  workspace,
  workspaces = [],
  backendAvailable,
}: {
  workspace: string
  workspaces?: { id: string; name?: string }[]
  backendAvailable: boolean
}) {
  const [rows, setRows] = useState<TranscriptRow[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyItem, setBusyItem] = useState<string | null>(null)
  const [showRejected, setShowRejected] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [modes, setModes] = useState<ModeInfo[]>(FALLBACK_MODES)

  useEffect(() => {
    if (!backendAvailable) return
    api<ModeInfo[]>("/ingest/modes")
      .then((m) => m.length && setModes(m))
      .catch(() => {})
  }, [backendAvailable])

  const loadList = useCallback(async () => {
    try {
      const list = await api<TranscriptRow[]>(scoped("/ingest/transcripts", workspace))
      setRows(list)
      setSelected((cur) => cur ?? list[0]?.id ?? null)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [workspace])

  const loadMeeting = useCallback(
    async (id: string) => {
      setLoading(true)
      try {
        setMeeting(await api<Meeting>(scoped(`/ingest/transcripts/${id}/meeting`, workspace)))
        setError(null)
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setLoading(false)
      }
    },
    [workspace],
  )

  useEffect(() => {
    setSelected(null)
    setMeeting(null)
    if (backendAvailable) loadList()
  }, [workspace, backendAvailable, loadList])

  useEffect(() => {
    if (selected) loadMeeting(selected)
  }, [selected, loadMeeting])

  // While anything is being processed, keep the list (and the open meeting) live.
  const inFlight = rows.some((r) => running(r.status))
  useEffect(() => {
    if (!inFlight) return
    const t = setInterval(() => {
      loadList()
      if (selected) loadMeeting(selected)
    }, 4000)
    return () => clearInterval(t)
  }, [inFlight, selected, loadList, loadMeeting])

  const toggleReject = async (item: Item) => {
    setBusyItem(item.id)
    try {
      await api(scoped(`/ingest/artifacts/${item.id}/reject`, workspace), {
        method: item.rejected ? "DELETE" : "POST",
        headers: { "Content-Type": "application/json" },
        body: item.rejected ? undefined : JSON.stringify({}),
      })
      if (selected) await loadMeeting(selected)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyItem(null)
    }
  }

  const reprocess = async (id: string, mode?: string) => {
    try {
      const q = mode ? `?mode=${encodeURIComponent(mode)}` : ""
      await api(scoped(`/ingest/transcripts/${id}/reprocess${q}`, workspace), { method: "POST" })
      await loadList()
      await loadMeeting(id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  if (!backendAvailable) {
    return <p className="text-sm text-muted-foreground">Meetings need the backend. Start it and reload.</p>
  }

  return (
    <div className="flex flex-col gap-5">
      <p className="text-sm text-muted-foreground">
        What each session yielded, read the way its kind asks: a meeting for its decisions, action items, risks and
        open questions, a lecture for what was taught and what the audience asked. Every item carries the words that
        were actually said. <span className="text-foreground">Reject</span> anything that is wrong: it leaves the
        graph at once and stays out, even when the session is processed again.
      </p>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="flex items-start justify-between gap-2 rounded-lg border border-primary/30 bg-primary/5 p-3 text-sm text-foreground">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} className="font-mono text-[11px] text-muted-foreground hover:text-foreground">
            dismiss
          </button>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[280px_minmax(0,1fr)]">
        {/* Meeting list + intake */}
        <aside className="flex flex-col gap-3">
          <AddMeeting
            workspace={workspace}
            workspaces={workspaces}
            modes={modes}
            onAdded={(id, target) => {
              if (target === workspace) {
                loadList()
                setSelected(id)
                setNotice(null)
              } else {
                setNotice(`Added to the "${target}" workspace. Switch to it in the header to watch it process.`)
              }
            }}
          />
          <div className="flex items-center justify-between">
            <h3 className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
              Meetings &middot; {rows.length}
            </h3>
            <button onClick={loadList} className="text-muted-foreground hover:text-foreground" aria-label="Refresh meetings">
              <RefreshCw className="h-3.5 w-3.5" />
            </button>
          </div>
          {rows.length === 0 && (
            <p className="text-sm text-muted-foreground">No meetings in this workspace yet. Paste or upload one above.</p>
          )}
          <ul className="flex flex-col gap-1.5">
            {rows.map((r) => (
              <li key={r.id}>
                <button
                  onClick={() => setSelected(r.id)}
                  className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                    selected === r.id ? "border-primary/50 bg-primary/5" : "border-border bg-card hover:border-primary/30"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm text-foreground">{r.title}</span>
                    <span className={`shrink-0 rounded-full border px-1.5 py-0.5 font-mono text-[10px] ${STATUS_STYLE[r.status] ?? STATUS_STYLE.pending}`}>
                      {r.status}
                    </span>
                  </div>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {fmtDate(r.occurred_at) ?? fmtDate(r.created_at) ?? "undated"}
                    {r.mode && r.mode !== "meeting" && (
                      <> &middot; {modes.find((m) => m.id === r.mode)?.label.toLowerCase() ?? r.mode}</>
                    )}
                  </span>
                  {running(r.status) && r.progress && (
                    <div className="mt-1.5">
                      <ProgressBar progress={r.progress} compact />
                    </div>
                  )}
                  {r.status === "error" && r.error && (
                    <p className="mt-1 line-clamp-2 text-[11px] text-destructive">{r.error}</p>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {/* The meeting */}
        <section className="min-w-0">
          {loading && !meeting && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
          {meeting && (
            <MeetingView
              meeting={meeting}
              workspace={workspace}
              modes={modes}
              busyItem={busyItem}
              showRejected={showRejected}
              onToggleShowRejected={() => setShowRejected((s) => !s)}
              onToggleReject={toggleReject}
              onReprocess={(mode) => reprocess(meeting.id, mode)}
            />
          )}
        </section>
      </div>
    </div>
  )
}

function MeetingView({
  meeting,
  workspace,
  modes,
  busyItem,
  showRejected,
  onToggleShowRejected,
  onToggleReject,
  onReprocess,
}: {
  meeting: Meeting
  workspace: string
  modes: ModeInfo[]
  busyItem: string | null
  showRejected: boolean
  onToggleShowRejected: () => void
  onToggleReject: (item: Item) => void
  onReprocess: (mode?: string) => void
}) {
  const mode = meeting.mode ?? FALLBACK_MODES[0]
  const kinds = mode.kinds
  const shownKinds = new Set(kinds.map((k) => k.id))
  const rejectedCount = meeting.items.filter((i) => i.rejected && shownKinds.has(i.kind)).length
  const current = meeting.items.filter((i) => !i.superseded_by)
  const grouped = useMemo(
    () =>
      kinds.map((k) => ({
        ...k,
        items: current
          .filter((i) => i.kind === k.id && (showRejected || !i.rejected))
          .sort((a, b) => (a.start_time ?? "").localeCompare(b.start_time ?? "")),
      })),
    [kinds, current, showRejected],
  )
  const [readAs, setReadAs] = useState(mode.id)
  useEffect(() => setReadAs(mode.id), [mode.id])

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-medium text-foreground">{meeting.title}</h2>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1">
                <CalendarDays className="h-3 w-3" />
                {fmtDate(meeting.occurred_at) ?? "undated"}
              </span>
              <span className="flex items-center gap-1">
                <Users className="h-3 w-3" />
                {meeting.participants.join(", ") || "no named speakers"}
              </span>
              {meeting.unnamed_voices.length > 0 && (
                <span className="flex items-center gap-1 text-amber-400" title="Voices the transcript does not name. Their words are kept but attributed to nobody.">
                  <Mic className="h-3 w-3" />
                  {meeting.unnamed_voices.length} unnamed voice{meeting.unnamed_voices.length > 1 ? "s" : ""}
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <label className="sr-only" htmlFor="read-as">Read this session as</label>
            <select
              id="read-as"
              value={readAs}
              onChange={(e) => setReadAs(e.target.value)}
              title="What kind of session this is decides what is taken from it"
              className="rounded-md border border-border bg-background px-2 py-1 font-mono text-[11px] text-foreground focus:border-primary/50 focus:outline-none"
            >
              {modes.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
            <button
              onClick={() => onReprocess(readAs !== mode.id ? readAs : undefined)}
              disabled={running(meeting.status)}
              className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 font-mono text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-50"
            >
              <RotateCw className="h-3 w-3" />
              {readAs !== mode.id ? `process again as ${modes.find((m) => m.id === readAs)?.label.toLowerCase() ?? readAs}` : "process again"}
            </button>
          </div>
        </div>
        {meeting.error && <p className="mt-2 text-sm text-destructive">{meeting.error}</p>}
        {running(meeting.status) && meeting.progress && (
          <div className="mt-3 flex flex-col gap-1.5">
            <ProgressBar progress={meeting.progress} />
            <p className="text-xs text-muted-foreground">
              Findings appear once every part is read, because duplicates and reversed decisions can only be
              settled across the whole meeting. Each part&apos;s summary shows below as soon as it is read.
            </p>
          </div>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {kinds.map((k) => (
            <span key={k.id} className="flex items-baseline gap-1.5 rounded-md border border-border bg-background px-2 py-0.5">
              <span className="font-mono text-sm font-semibold text-foreground">
                {current.filter((i) => i.kind === k.id && !i.rejected).length}
              </span>
              <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{k.label}</span>
            </span>
          ))}
          {rejectedCount > 0 && (
            <button
              onClick={onToggleShowRejected}
              className="rounded-md border border-border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground hover:text-foreground"
            >
              {showRejected ? "hide" : "show"} {rejectedCount} rejected
            </button>
          )}
        </div>
      </div>

      {meeting.overview && (meeting.overview.headline || meeting.overview.summary) && !running(meeting.status) && (
        <OverviewCard overview={meeting.overview} />
      )}

      <SaidSearch transcriptId={meeting.id} workspace={workspace} />

      {grouped.map((group) =>
        group.items.length === 0 ? null : (
          <div key={group.id} className="flex flex-col gap-2">
            <h3 className="font-mono text-[11px] uppercase tracking-wider text-muted-foreground">{group.label}</h3>
            {group.items.map((item) => (
              <ItemCard
                key={item.id}
                item={item}
                personLabel={group.person}
                busy={busyItem === item.id}
                onToggleReject={() => onToggleReject(item)}
              />
            ))}
          </div>
        ),
      )}
      {current.length === 0 && !running(meeting.status) && (
        <p className="text-sm text-muted-foreground">Nothing was found in this meeting.</p>
      )}

      {meeting.parts && meeting.parts.length > 0 && (
        <Parts
          parts={meeting.parts}
          pointsLabel={mode.points_kind ? null : mode.points_label}
          defaultOpen={running(meeting.status)}
        />
      )}
    </div>
  )
}

/** One raw transcript passage, as GET /ingest/passages returns it. */
interface Passage {
  transcript_id: string
  start_time: string | null
  end_time: string | null
  speakers: string | null
  text: string
}

/**
 * Search the words themselves. Everything else on this page passed through a
 * model; this is what was actually said, with who said it and when.
 */
function SaidSearch({ transcriptId, workspace }: { transcriptId: string; workspace: string }) {
  const [q, setQ] = useState("")
  const [hits, setHits] = useState<Passage[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setQ("")
    setHits(null)
  }, [transcriptId])

  const run = async () => {
    if (!q.trim()) return
    setBusy(true)
    setErr(null)
    try {
      setHits(
        await api<Passage[]>(
          scoped(`/ingest/passages?q=${encodeURIComponent(q.trim())}&transcript_id=${encodeURIComponent(transcriptId)}&limit=5`, workspace),
        ),
      )
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-1.5 focus-within:border-primary/50">
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground" />
        ) : (
          <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="Search what was said in this session"
          aria-label="Search what was said in this session"
          className="flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
        />
      </div>
      {err && <p className="text-xs text-destructive">{err}</p>}
      {hits && hits.length === 0 && <p className="text-xs text-muted-foreground">Nothing said matches that.</p>}
      {hits && hits.length > 0 && (
        <ol className="flex flex-col gap-1.5">
          {hits.map((h, i) => (
            <li key={i} className="rounded-lg border border-border bg-card p-2.5">
              <p className="font-mono text-[10px] text-muted-foreground">
                {clock(h.start_time)}
                {h.end_time && h.end_time !== h.start_time ? `–${clock(h.end_time)}` : ""}
                {h.speakers ? ` · ${h.speakers}` : ""}
              </p>
              <p className="mt-1 whitespace-pre-line text-xs leading-relaxed text-foreground">
                {h.text.replace(/^\[[^\]]+\] /gm, "")}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** The session in a few lines. Written from the part notes, so it says so. */
function OverviewCard({ overview }: { overview: Overview }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      {overview.headline && <p className="text-sm font-medium text-foreground">{overview.headline}</p>}
      {overview.summary && <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{overview.summary}</p>}
      {overview.themes.length > 0 && (
        <>
          <button
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="mt-2 font-mono text-[11px] text-muted-foreground hover:text-foreground"
          >
            {open ? "▾" : "▸"} {overview.themes.length} theme{overview.themes.length > 1 ? "s" : ""}
          </button>
          {open && (
            <div className="mt-2 flex flex-col gap-3">
              {overview.themes.map((t) => (
                <div key={t.title}>
                  <p className="text-sm text-foreground">{t.title}</p>
                  <ul className="mt-1 flex list-disc flex-col gap-0.5 pl-5 text-sm text-muted-foreground">
                    {t.points.map((pt, i) => (
                      <li key={i}>{pt}</li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      <p className="mt-2 font-mono text-[10px] text-muted-foreground/70">
        Written from the part notes below, not from the transcript, and never added to the graph.
      </p>
    </div>
  )
}

/** What each part of the transcript was about, in order: the session as it was read. */
function Parts({
  parts,
  pointsLabel,
  defaultOpen,
}: {
  parts: Part[]
  pointsLabel: string | null
  defaultOpen: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const read = parts.filter((p) => p.status === "done" || p.status === "error").length
  return (
    <div className="flex flex-col gap-2">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-2 text-left font-mono text-[11px] uppercase tracking-wider text-muted-foreground hover:text-foreground"
      >
        <span>{open ? "▾" : "▸"}</span>
        Part by part &middot; {read} of {parts.length} read
      </button>
      {open && (
        <ol className="flex flex-col gap-2">
          {parts.map((p) => (
            <li key={p.idx} className="rounded-lg border border-border bg-card p-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
                <span className="text-foreground">part {p.idx + 1}</span>
                {(p.start_time || p.end_time) && (
                  <span className="flex items-center gap-1">
                    <Clock className="h-3 w-3" />
                    {clock(p.start_time)}–{clock(p.end_time)}
                  </span>
                )}
                {p.speakers && p.speakers.length > 0 && (
                  <span className="flex items-center gap-1">
                    <Users className="h-3 w-3" />
                    {p.speakers.join(", ")}
                  </span>
                )}
                <span
                  className={`ml-auto rounded-full border px-1.5 py-0.5 text-[10px] ${
                    p.error
                      ? STATUS_STYLE.error
                      : STATUS_STYLE[p.status === "pending" ? "pending" : "done"]
                  }`}
                >
                  {p.error ? "failed" : p.status === "pending" ? "waiting" : "read"}
                </span>
              </div>
              {p.topic && <p className="mt-1.5 text-sm font-medium text-foreground">{p.topic}</p>}
              {p.summary ? (
                <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{p.summary}</p>
              ) : p.error ? (
                <p className="mt-1.5 text-xs text-destructive">{p.error}</p>
              ) : (
                <p className="mt-1.5 text-xs text-muted-foreground">Not read yet.</p>
              )}
              {pointsLabel && p.points.length > 0 && (
                <div className="mt-2">
                  <p className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{pointsLabel}</p>
                  <ul className="mt-1 flex list-disc flex-col gap-0.5 pl-5 text-sm text-foreground">
                    {p.points.map((pt) => (
                      <li key={pt.id}>{pt.statement}</li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function ItemCard({
  item,
  personLabel,
  busy,
  onToggleReject,
}: {
  item: Item
  personLabel: string
  busy: boolean
  onToggleReject: () => void
}) {
  const person = personOf(item)
  return (
    <div
      className={`rounded-lg border p-3 transition-opacity ${
        item.rejected ? "border-dashed border-border bg-transparent opacity-60" : "border-border bg-card"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <p className={`text-sm ${item.rejected ? "text-muted-foreground line-through" : "text-foreground"}`}>
          {item.statement}
        </p>
        <button
          onClick={onToggleReject}
          disabled={busy}
          className={`flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider transition-colors disabled:opacity-50 ${
            item.rejected
              ? "border-border text-muted-foreground hover:text-foreground"
              : "border-destructive/30 text-destructive/80 hover:bg-destructive/10 hover:text-destructive"
          }`}
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : item.rejected ? <Undo2 className="h-3 w-3" /> : <Ban className="h-3 w-3" />}
          {item.rejected ? "undo" : "reject"}
        </button>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-muted-foreground">
        <span>
          {personLabel}:{" "}
          <span className={person ? "text-foreground" : "text-amber-400"}>{person ?? "not attributed"}</span>
        </span>
        {item.kind === "action_item" && item.said_by && item.said_by !== item.owner && (
          <span>assigned by {item.said_by}</span>
        )}
        {item.due && <span>due {item.due}</span>}
        {item.start_time && (
          <span className="flex items-center gap-1">
            <Clock className="h-3 w-3" />
            {item.start_time}
          </span>
        )}
        {item.mentions > 1 && <span>said {item.mentions}&times;</span>}
      </div>
      {item.kind === "question" && (
        <p className="mt-1.5 text-sm text-muted-foreground">
          {item.rationale ? (
            <>
              <span className="font-mono text-[10px] uppercase tracking-wider">answer</span> {item.rationale}
            </>
          ) : (
            <span className="text-amber-400">not answered in the session</span>
          )}
        </p>
      )}
      {item.quote && (
        <blockquote className="mt-2 border-l-2 border-primary/40 pl-2.5 text-xs italic text-muted-foreground">
          &ldquo;{item.quote}&rdquo;
        </blockquote>
      )}
    </div>
  )
}

function AddMeeting({
  workspace,
  workspaces,
  modes,
  onAdded,
}: {
  workspace: string
  workspaces: { id: string; name?: string }[]
  modes: ModeInfo[]
  onAdded: (id: string, target: string) => void
}) {
  const [mode, setMode] = useState("meeting")
  const [open, setOpen] = useState(false)
  // Asked every time, defaulting to the graph on screen: a meeting filed in
  // the wrong workspace is invisible from the right one.
  const [target, setTarget] = useState(workspace)
  useEffect(() => setTarget(workspace), [workspace])
  const choices = workspaces.some((w) => w.id === workspace) ? workspaces : [{ id: workspace }, ...workspaces]
  const [title, setTitle] = useState("")
  const [date, setDate] = useState("")
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const readFile = async (file: File | undefined) => {
    if (!file) return
    setText(await file.text())
    if (!title) setTitle(file.name.replace(/\.[^.]+$/, ""))
  }

  const submit = async () => {
    if (!title.trim() || !text.trim()) {
      setErr("A title and the transcript text are both needed.")
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const res = await api<{ transcript_id?: string; id?: string }>(scoped("/ingest/transcripts", target), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), content: text, occurred_at: date || null, mode }),
      })
      setTitle("")
      setDate("")
      setText("")
      setOpen(false)
      onAdded(res.transcript_id ?? res.id ?? "", target)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 font-mono text-[11px] text-primary transition-colors hover:bg-primary/20"
      >
        <Upload className="h-3 w-3" /> add a meeting
      </button>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Meeting title"
        className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none"
      />
      <label className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
        kind
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground focus:border-primary/50 focus:outline-none"
        >
          {modes.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      <p className="-mt-1 text-[11px] leading-snug text-muted-foreground">
        {modes.find((m) => m.id === mode)?.description}
      </p>
      <label className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
        workspace
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground focus:border-primary/50 focus:outline-none"
        >
          {choices.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name && w.name !== w.id ? `${w.name} (${w.id})` : w.id}
              {w.id === workspace ? " · on screen" : ""}
            </option>
          ))}
        </select>
      </label>
      <input
        type="date"
        value={date}
        onChange={(e) => setDate(e.target.value)}
        aria-label="When the meeting happened"
        className="rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-xs text-foreground focus:border-primary/50 focus:outline-none"
      />
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={"Paste the transcript.\nSarah: Let's move the release to April 15th.\nMei: I'll update the roadmap."}
        rows={6}
        className="rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-xs text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none"
      />
      <label className="cursor-pointer font-mono text-[11px] text-muted-foreground hover:text-foreground">
        or choose a file (.txt, .vtt, .srt, .md)
        <input type="file" accept=".txt,.vtt,.srt,.md,.text,.log" className="hidden" onChange={(e) => readFile(e.target.files?.[0])} />
      </label>
      {err && <p className="text-xs text-destructive">{err}</p>}
      <div className="flex gap-2">
        <button
          onClick={submit}
          disabled={busy}
          className="flex flex-1 items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1 font-mono text-[11px] text-primary transition-colors hover:bg-primary/20 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Upload className="h-3 w-3" />}
          process meeting
        </button>
        <button
          onClick={() => setOpen(false)}
          className="rounded-md border border-border px-2.5 py-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          cancel
        </button>
      </div>
    </div>
  )
}
