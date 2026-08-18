/**
 * Proxies /api/* to the Brahmastra backend, adding authentication.
 *
 * Replaces the next.config.mjs rewrite. A rewrite forwards the request as-is
 * and cannot attach an Authorization header, so as soon as the API required a
 * key every browser call through it returned 401. Doing it as a route handler
 * keeps BRAHMASTRA_API_KEY on the server while browser code carries on calling
 * /api/... unchanged.
 */
import { backendFetch } from "@/lib/backend"

export const dynamic = "force-dynamic"

async function proxy(request: Request, path: string[]): Promise<Response> {
  const target = `/${path.join("/")}${new URL(request.url).search}`

  const init: RequestInit = { method: request.method }
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text()
    init.headers = { "Content-Type": request.headers.get("content-type") ?? "application/json" }
  }

  try {
    const upstream = await backendFetch(target, init)
    // Stream the body through untouched; only the auth header was added.
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
      },
    })
  } catch (error) {
    // The backend being unreachable is an expected state — Aura Free suspends
    // after a few days idle — so say so rather than surfacing a stack trace.
    return Response.json(
      { detail: `Backend unreachable: ${(error as Error).message}` },
      { status: 502 },
    )
  }
}

export async function GET(request: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await ctx.params).path)
}
export async function POST(request: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await ctx.params).path)
}
export async function DELETE(request: Request, ctx: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await ctx.params).path)
}
