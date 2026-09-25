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
}

/** One finding, as GET /ingest/transcripts/{id}/meeting returns it. */
interface Item {
  id: string
  kind: "decision" | "action_item" | "risk" | "open_question"
  statement: string
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
  items: Item[]
}

const KINDS: { id: Item["kind"]; label: string; person: string }[] = [
  { id: "decision", label: "Decisions", person: "decided by" },
  { id: "action_item", label: "Action items", person: "owner" },
  { id: "risk", label: "Risks", person: "raised by" },
  { id: "open_question", label: "Open questions", person: "asked by" },
]

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

export function MeetingsPanel({ workspace, backendAvailable }: { workspace: string; backendAvailable: boolean }) {
  const [rows, setRows] = useState<TranscriptRow[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busyItem, setBusyItem] = useState<string | null>(null)
  const [showRejected, setShowRejected] = useState(false)

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
  const inFlight = rows.some((r) => r.status === "pending" || r.status === "processing")
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

  const reprocess = async (id: string) => {
    try {
      await api(scoped(`/ingest/transcripts/${id}/reprocess`, workspace), { method: "POST" })
      await loadList()
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
        Every decision, action item, risk and open question found in a meeting, with the words that were actually said.
        Each item goes into the graph as it appears here. <span className="text-foreground">Reject</span> anything
        that is wrong: it leaves the graph at once and stays out, even when the meeting is processed again.
      </p>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[280px_minmax(0,1fr)]">
        {/* Meeting list + intake */}
        <aside className="flex flex-col gap-3">
          <AddMeeting workspace={workspace} onAdded={(id) => { loadList(); setSelected(id) }} />
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
                  </span>
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
              busyItem={busyItem}
              showRejected={showRejected}
              onToggleShowRejected={() => setShowRejected((s) => !s)}
              onToggleReject={toggleReject}
              onReprocess={() => reprocess(meeting.id)}
            />
          )}
        </section>
      </div>
    </div>
  )
}

function MeetingView({
  meeting,
  busyItem,
  showRejected,
  onToggleShowRejected,
  onToggleReject,
  onReprocess,
}: {
  meeting: Meeting
  busyItem: string | null
  showRejected: boolean
  onToggleShowRejected: () => void
  onToggleReject: (item: Item) => void
  onReprocess: () => void
}) {
  const rejectedCount = meeting.items.filter((i) => i.rejected).length
  const current = meeting.items.filter((i) => !i.superseded_by)
  const grouped = useMemo(
    () =>
      KINDS.map((k) => ({
        ...k,
        items: current.filter((i) => i.kind === k.id && (showRejected || !i.rejected)),
      })),
    [current, showRejected],
  )

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
          <button
            onClick={onReprocess}
            className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 font-mono text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
          >
            <RotateCw className="h-3 w-3" /> process again
          </button>
        </div>
        {meeting.error && <p className="mt-2 text-sm text-destructive">{meeting.error}</p>}
        <div className="mt-3 flex flex-wrap gap-2">
          {KINDS.map((k) => (
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
      {current.length === 0 && (
        <p className="text-sm text-muted-foreground">Nothing was found in this meeting yet.</p>
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
      {item.quote && (
        <blockquote className="mt-2 border-l-2 border-primary/40 pl-2.5 text-xs italic text-muted-foreground">
          &ldquo;{item.quote}&rdquo;
        </blockquote>
      )}
    </div>
  )
}

function AddMeeting({ workspace, onAdded }: { workspace: string; onAdded: (id: string) => void }) {
  const [open, setOpen] = useState(false)
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
      const res = await api<{ transcript_id?: string; id?: string }>(scoped("/ingest/transcripts", workspace), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), content: text, occurred_at: date || null }),
      })
      setTitle("")
      setDate("")
      setText("")
      setOpen(false)
      onAdded(res.transcript_id ?? res.id ?? "")
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
