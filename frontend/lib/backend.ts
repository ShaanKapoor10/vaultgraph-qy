/**
 * Server-side access to the Brahmastra API.
 *
 * BRAHMASTRA_API_KEY must never reach the browser, so every call goes through
 * the server: React Server Components and server actions use backendFetch()
 * directly, and browser code goes through the /api/[...path] proxy route,
 * which calls this and injects the key on the way past.
 *
 * This is why the old next.config rewrite was removed. A rewrite forwards the
 * request untouched — it cannot attach an Authorization header — so once the
 * API requires a key, every browser call through it would 401.
 */

export const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8001"

export function backendHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra)
  const key = process.env.BRAHMASTRA_API_KEY
  if (key) headers.set("Authorization", `Bearer ${key}`)
  return headers
}

/** fetch() against the backend with authentication attached. Server only. */
export function backendFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${BACKEND_URL}${path.startsWith("/") ? path : `/${path}`}`
  return fetch(url, { ...init, headers: backendHeaders(init.headers) })
}
