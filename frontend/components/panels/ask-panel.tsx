"use client"

import { useState } from "react"
import { Sparkles, Loader2, CornerDownLeft, Network, MessageSquareQuote, CircleSlash } from "lucide-react"

interface Citation {
  note_id: string
  title: string
}

/** A raw transcript passage an answer was read from (only when the graph had nothing). */
interface Passage {
  transcript_id: string
  title: string | null
  start_time: string | null
  end_time: string | null
  speakers: string | null
}

interface AskResult {
  mode: string
  answer: string
  entities: string[]
  citations: Citation[]
  /**
   * Where the answer came from. "graph": the knowledge graph answered.
   * "transcript": the graph had nothing, so it was read from what was said --
   * which also means extraction missed it. "none": nothing answered it.
   * Absent on global (cluster summary) answers.
   */
  source?: "graph" | "transcript" | "none"
  passages?: Passage[]
}

const SOURCE: Record<string, { label: string; title: string; cls: string; Icon: typeof Network }> = {
  graph: {
    label: "from the graph",
    title: "Answered from facts and statements in the knowledge graph.",
    cls: "border-green-500/30 bg-green-500/10 text-green-400",
    Icon: Network,
  },
  transcript: {
    label: "from the transcript, not in the graph yet",
    title:
      "The graph had no answer, so this was read from what was actually said in a meeting. " +
      "That also means extraction missed it.",
    cls: "border-amber-500/30 bg-amber-500/10 text-amber-400",
    Icon: MessageSquareQuote,
  },
  none: {
    label: "no answer found",
    title: "Neither the graph nor any recorded transcript answers this.",
    cls: "border-border bg-secondary text-muted-foreground",
    Icon: CircleSlash,
  },
}

/** HH:MM:SS, without the milliseconds a VTT file carries. */
const clock = (ts: string | null) => (ts ? ts.replace(/[.,]\d+$/, "") : null)
/** An anchor can be a whole statement; the header shows the start of it. */
const short = (name: string, max = 60) => (name.length > max ? `${name.slice(0, max - 1).trimEnd()}…` : name)

const EXAMPLES = [
  "What do I know about the Apollo project?",
  "What are the main themes in my knowledge graph?",
  "Who does Sarah report to and what does she own?",
]

export function AskPanel({ backendAvailable = false }: { backendAvailable?: boolean }) {
  const [question, setQuestion] = useState("")
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<AskResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const ask = async (q: string) => {
    const query = q.trim()
    if (!query || loading) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: query, mode: "auto" }),
      })
      const raw = await res.text()
      let data: any = null
      try {
        data = raw ? JSON.parse(raw) : null
      } catch {
        data = null
      }
      if (!res.ok) {
        setError(data?.detail ?? raw?.slice(0, 160) ?? res.statusText)
      } else {
        setResult(data as AskResult)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-2">
        <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <p className="text-sm text-muted-foreground">
          Ask a natural-language question. Brahmastra retrieves the relevant subgraph (or cluster
          summaries for broad questions) and answers from your own notes — with citations.
        </p>
      </div>

      {/* Input */}
      <div className="flex flex-col gap-2">
        <div className="flex items-end gap-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                ask(question)
              }
            }}
            placeholder={backendAvailable ? "Ask your knowledge graph…" : "Backend offline — start it to ask"}
            disabled={!backendAvailable || loading}
            rows={2}
            className="min-h-[44px] flex-1 resize-y rounded-md border border-border bg-card px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none disabled:opacity-50"
          />
          <button
            onClick={() => ask(question)}
            disabled={!backendAvailable || loading || !question.trim()}
            className="flex h-[44px] items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 font-mono text-sm text-primary transition-colors hover:bg-primary/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <CornerDownLeft className="h-4 w-4" />}
            {loading ? "thinking…" : "ask"}
          </button>
        </div>

        {/* Example questions */}
        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              onClick={() => {
                setQuestion(ex)
                ask(ex)
              }}
              disabled={!backendAvailable || loading}
              className="rounded border border-border bg-secondary px-2 py-0.5 text-xs text-secondary-foreground transition-colors hover:border-primary/50 disabled:opacity-50"
            >
              {ex}
            </button>
          ))}
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 font-mono text-xs text-destructive">
          {error}
        </div>
      )}

      {/* Answer */}
      {result && (
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
          <div className="flex items-center gap-2">
            <span
              className={`rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                result.mode === "global"
                  ? "bg-primary/15 text-primary"
                  : result.mode === "local"
                    ? "bg-green-500/15 text-green-400"
                    : "bg-secondary text-muted-foreground"
              }`}
            >
              {result.mode} search
            </span>
            {result.source && SOURCE[result.source] && (() => {
              const s = SOURCE[result.source!]
              return (
                <span
                  title={s.title}
                  className={`flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px] ${s.cls}`}
                >
                  <s.Icon className="h-3 w-3" />
                  {s.label}
                </span>
              )
            })()}
          </div>
          {result.entities.length > 0 && (
            <p className="font-mono text-[11px] text-muted-foreground" title={result.entities.join("\n")}>
              anchored to: {result.entities.map((e) => short(e)).join(" · ")}
            </p>
          )}

          <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">{result.answer}</p>

          {result.passages && result.passages.length > 0 && (
            <div className="flex flex-col gap-1.5 border-t border-border pt-3">
              <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                Read from what was said
              </span>
              <ul className="flex flex-col gap-1">
                {result.passages.map((p, i) => (
                  <li key={i} className="font-mono text-xs text-secondary-foreground">
                    [t:{i + 1}] {p.title ?? "a session"}
                    {p.start_time && (
                      <span className="text-muted-foreground">
                        {" "}
                        · {clock(p.start_time)}
                        {p.end_time && p.end_time !== p.start_time ? `–${clock(p.end_time)}` : ""}
                      </span>
                    )}
                    {p.speakers && <span className="text-muted-foreground"> · {p.speakers}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.citations.length > 0 && (
            <div className="flex flex-col gap-1.5 border-t border-border pt-3">
              <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                Sources
              </span>
              <div className="flex flex-wrap gap-1.5">
                {result.citations.map((c) => (
                  <span
                    key={c.note_id}
                    className="rounded border border-border bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground"
                  >
                    {c.title}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
