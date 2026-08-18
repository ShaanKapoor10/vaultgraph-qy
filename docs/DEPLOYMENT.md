# Deployment

Two supported shapes, both first-class:

| | SQLite | Neo4j |
|---|---|---|
| Services to operate | 1 | 2 (or Aura) |
| Scale-out | **No — one worker** | Yes |
| Hybrid vector search | No, lexical only | Yes |
| Survives instance replacement | Only with a volume | Yes |
| Good for | single user, one box, low ops | shared, multi-machine, deployed |

---

## Quick start

```bash
cp .env.example backend/.env          # fill in GROQ_API_KEY at minimum
docker compose up --build

# dashboard   http://localhost:3000
# readiness   http://localhost:8001/health/ready
```

Against a local Neo4j instead of SQLite:

```bash
docker compose --profile neo4j up --build
```

For Aura, leave the profile off and set `GRAPH_BACKEND=neo4j` plus the
`NEO4J_*` variables.

---

## Deploying with SQLite — and why that is allowed

SQLite is a legitimate production choice here, not a development shortcut. The
workload is one writer, the data is small (a 900 KB database at 55 notes), and
the alternative is operating a second stateful service. What it demands is two
things that are easy to lose by accident.

### 1. A persistent volume — this is the whole risk

`BRAHMASTRA_DB` must point **inside a mounted volume**. Written anywhere else,
the database lives in the container's writable layer and is **silently
discarded on the next deploy**. Nothing errors; the graph is simply empty, and
the notes — the one part of this system that is not rebuildable — are gone.

```yaml
environment:
  BRAHMASTRA_DB: /data/concept_graph.db
  BRAHMASTRA_CHECKPOINT_DIR: /data/checkpoints
volumes:
  - brahmastra-data:/data
```

Platforms that give you a real disk: **Fly.io** volumes, **Render** disks,
**Railway** volumes, any VM or Kubernetes `PersistentVolumeClaim`.

Platforms that do **not**, where SQLite will lose data: Vercel, Netlify, AWS
Lambda, Cloud Run without a mounted filesystem. On those, use Neo4j.

### 2. Exactly one worker

```yaml
WEB_CONCURRENCY: 1
```

SQLite is a single file. Concurrent writers produce `database is locked`, and
the pipeline lock is per *store*, not per process, so a second worker is not
serialised by it. WAL mode is already enabled and raises the ceiling for
concurrent **readers**, but it does not make multiple writer processes safe.

This also means **you cannot horizontally scale a SQLite deployment.** One
instance, vertically sized. If you need more than that, the answer is Neo4j —
not more workers.

### Backups

The volume is the only stateful thing in the deployment. `sqlite3 file
".backup"` is the safe way to copy a live database; copying the file directly
while a write is in flight can capture a torn state.

```bash
docker compose exec backend \
  python -c "import sqlite3,os; s=sqlite3.connect(os.environ['BRAHMASTRA_DB']); \
d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close(); print('ok')"
```

---

## Deploying with Neo4j

Set `GRAPH_BACKEND=neo4j` and the three `NEO4J_*` variables. Then:

```bash
python -m brahmastra.migrate_to_neo4j            # dry run, prints counts
python -m brahmastra.migrate_to_neo4j --apply    # mirrors SQLite into Neo4j
```

The migration is a **mirror**, not a union: each note's triples are deleted
before re-insert, so re-running it neither duplicates facts nor leaves behind
ones the source has since dropped.

Expect Neo4j to report slightly **fewer** triples than SQLite. That is correct:
`raw_triples` has no uniqueness constraint, so a fact the extractor emitted
twice from one note is stored twice, while Neo4j's `MERGE` collapses it.

Known constraints, all verified the hard way:

- **URI must be bare `neo4j://`**, not `neo4j+s://`. The `+s` schemes lock TLS
  configuration and forbid supplying the explicit certifi context the driver
  needs behind a TLS-intercepting root CA.
- **Aura's username is the instance id**, not `neo4j`.
- **The database is not named `neo4j`** — the store defaults to the server's
  home database. Asking for `neo4j` fails with `DatabaseNotFound`.
- **Aura Free suspends after ~3 days idle**, and while suspended its hostname
  stops resolving entirely — which looks exactly like the instance having been
  deleted. For anything long-lived this is an operational constraint, not a
  footnote.
- **GDS is unusable on the free tier**, so PageRank and Louvain stay in
  NetworkX.

---

## Health probes

| Endpoint | Answers | Wire it to |
|---|---|---|
| `GET /health` | Is the process alive? | liveness |
| `GET /health/ready` | Can it serve traffic? | readiness, load balancer |

`/health/ready` queries the graph store and returns **503** when it cannot
reach it. Use it for readiness only — never for liveness. A liveness probe that
fails when a dependency is down gets the container killed, and restarting
cannot fix a sleeping database; it converts a degraded system into an
unavailable one.

---

## The image

Two decisions dominate its size and cold-start time:

- **CPU-only torch.** `sentence-transformers` pulls torch, and the default
  wheel ships CUDA kernels — roughly 2.5 GB a server without a GPU will never
  execute. The CPU index cuts that with no behavioural change.
- **The embedding model is baked in.** `all-MiniLM-L6-v2` is ~88 MB and is
  otherwise downloaded on the first request after every cold start, and fails
  outright with no network. `BRAHMASTRA_CACHE` points at the baked copy.

---

## What is *not* deployed

- **The MCP server.** It speaks stdio to a local client, so it runs on your
  machine, not on the server. It reads the same `backend/.env`, which is how it
  agrees with everything else about `GRAPH_BACKEND`. Pointing it at a deployed
  Neo4j is what lets a local agent write into the shared graph.
- **Session checkpoint hooks** fire inside a local Claude Code session, so
  they run on your machine and write through whichever store `.env` selects.

Both of those are covered below: the scheduler ships as an opt-in compose
profile, and MCP can additionally be served over HTTP when a client cannot
reach the graph directly.

---

## Authentication

The API exposes the whole graph, can spend LLM tokens and can write into a
Notion workspace. It **fails closed**:

| Configuration | Result |
|---|---|
| `BRAHMASTRA_API_KEY` set | `Authorization: Bearer <key>` required (or `X-API-Key`) |
| nothing set | **503 on every route except health probes** |
| `BRAHMASTRA_ALLOW_ANONYMOUS=1` | open — local development only |

Forgetting to configure a deployment therefore breaks it loudly instead of
publishing the graph quietly. That default is deliberate: this codebase has
already shipped workspace isolation that leaked silently and a hardcoded CORS
`*`, and both failed open.

`/health` and `/health/ready` stay open, because an orchestrator's health
checker cannot carry a token, and a probe that 503s on unset auth would mask
the real cause. `/health/ready` reports which state it is in, so you can see a
deployment is protected without reading its environment.

The dashboard reaches a protected API through `app/api/[...path]/route.ts`,
which proxies server-side and injects the key. The old `next.config.mjs`
rewrite was removed for exactly this reason — a rewrite forwards the request
untouched and cannot attach a header, so every browser call would 401.
`BRAHMASTRA_API_KEY` never reaches the browser.

---

## Running the pipeline on a schedule

`live_sync` already polls and runs the pipeline, so this is a process, not new
code:

```bash
docker compose --profile scheduler up --build     # POLL_INTERVAL, default 900s
```

Off by default, and the reason is the storage choice. With
`GRAPH_BACKEND=sqlite` the scheduler is a **second writer** against one file:
the pipeline lock prevents overlapping runs, but not a long pipeline
transaction making an API write wait on `busy_timeout`. Survivable at this data
size, but on-demand runs are the safer default. With `GRAPH_BACKEND=neo4j`
there is no such constraint — turn it on.

---

## Using MCP against a deployed instance

Two ways, and the first needs no new infrastructure at all.

### 1. Local MCP server, shared graph (recommended)

The MCP server speaks stdio to a local client, so it keeps running on your
machine — but it reads the same `backend/.env` as everything else. Point that
at the deployed Neo4j and a local agent writes straight into the shared graph:

```bash
GRAPH_BACKEND=neo4j
NEO4J_URI=neo4j://<instance>.databases.neo4j.io:7687
NEO4J_USER=<instance-id>
NEO4J_PASSWORD=...
```

Every device with those credentials sees the same graph. This is the
multi-device story, and it is why `GRAPH_BACKEND` had to move into `.env`:
the MCP server, uvicorn, the CLI and the session hooks all start
independently, and they must agree on where the database is.

### 2. Remote MCP over HTTP

When a client cannot reach Neo4j directly, or you want a single authenticated
door, set `MCP_HTTP=1` and the same ten tools are served at `/mcp`, mounted
inside the FastAPI app so they inherit the API's authentication — one boundary,
not a second unprotected way into the same graph. Verified: `/mcp` returns 401
without a key.

```json
{
  "mcpServers": {
    "brahmastra": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

Session checkpoint hooks stay local either way — they fire inside a local
Claude Code session and write through whichever store `.env` selects.

---

## Memory, and the 512 MB trap

Loading torch plus the embedding model costs **461 MB RSS, measured**, before
serving anything. A 512 MB instance is killed while starting, which reads as a
crash loop rather than as "needs a bigger box". Give it **1 GB**, or set
`EMBEDDINGS_ENABLED=0` to fall back to exact matching plus Jaro-Winkler —
measured cost of that trade on the live graph: **417 entity clusters with
embeddings, 475 without**, so about 58 merges are lost.

See `docs/PLATFORMS.md` for which hosts can meet this.

---

## Before going public

- [ ] `CORS_ORIGINS` set to a real allowlist, not `*`
- [ ] At least 1 GB RAM, or `EMBEDDINGS_ENABLED=0`
- [ ] TLS in front of it — a bearer token over plain HTTP is not a secret
- [x] Authentication — set `BRAHMASTRA_API_KEY`, and make sure
      `BRAHMASTRA_ALLOW_ANONYMOUS` is **not** set on the host
- [ ] `backend/.env` present on the host and excluded from the image
      (`.dockerignore` already does this)
- [ ] Volume mounted and backed up if using SQLite
- [ ] `WEB_CONCURRENCY=1` if using SQLite
- [ ] `GROQ_MODEL` pinned to a model that currently exists and supports
      `response_format=json_object` — extraction depends on it
