"use client"

import { useRouter } from "next/navigation"
import { useTransition } from "react"

/**
 * Pick which knowledge graph the dashboard is showing.
 *
 * The workspace lives in the URL rather than in component state, for two
 * reasons. The data is loaded by a server component, so a change has to reach
 * the server to have any effect — and a URL is shareable, so "look at the
 * office graph" is a link rather than a sequence of clicks. It is also the
 * honest representation: the page really is showing a different graph, not a
 * filtered view of one.
 */
export type WorkspaceOption = {
  id: string
  name?: string
}

export function WorkspaceSwitcher({
  workspaces,
  current,
}: {
  workspaces: WorkspaceOption[]
  current: string
}) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()

  // One workspace is the normal case and a picker offering no alternative is
  // just noise — but say which graph is on screen, because a dashboard that
  // never names its workspace is how you end up unsure which one you edited.
  if (workspaces.length <= 1) {
    return (
      <span className="text-xs text-muted-foreground" title="The only workspace">
        workspace: <span className="font-medium text-foreground">{current}</span>
      </span>
    )
  }

  return (
    <label className="flex items-center gap-2 text-xs text-muted-foreground">
      <span>workspace</span>
      <select
        value={current}
        disabled={pending}
        aria-label="Select workspace"
        onChange={(event) => {
          const next = event.target.value
          startTransition(() => {
            // `default` is the implicit workspace, so leave it out of the URL
            // rather than carrying a parameter that changes nothing.
            //
            // No router.refresh() here. It refreshes the route currently on
            // screen, and queued in the same transition as the push it ran
            // against the workspace being LEFT -- refetching the old graph
            // while the new one was already being requested. The page reads
            // searchParams, so it renders dynamically and the push refetches
            // it on its own; the client router does not reuse a cached payload
            // for a dynamic segment.
            router.push(next === "default" ? "/" : `/?workspace=${encodeURIComponent(next)}`)
          })
        }}
        className="rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground
                   focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60"
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name && w.name !== w.id ? `${w.name} (${w.id})` : w.id}
          </option>
        ))}
      </select>
      {pending && <span className="animate-pulse">loading…</span>}
    </label>
  )
}
