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
- **Session checkpoint hooks.** Same reason — they fire in a local Claude Code
  session.
- **A scheduler.** Nothing runs the pipeline on a timer today; it is triggered
  by `POST /pipeline/run`, the CLI, or MCP. For a deployment that syncs from
  Notion regularly you want either a cron container calling `/pipeline/run`, or
  `python -m brahmastra.live_sync` as a second process.

---

## Before going public

- [ ] `CORS_ORIGINS` set to a real allowlist, not `*`
- [ ] The API has **no authentication** — put it behind a proxy, or on a
      private network, before exposing it
- [ ] `backend/.env` present on the host and excluded from the image
      (`.dockerignore` already does this)
- [ ] Volume mounted and backed up if using SQLite
- [ ] `WEB_CONCURRENCY=1` if using SQLite
- [ ] `GROQ_MODEL` pinned to a model that currently exists and supports
      `response_format=json_object` — extraction depends on it
