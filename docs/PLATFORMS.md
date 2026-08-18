# Choosing where to run it

Free-tier terms change constantly, so this page leads with **measured
requirements** — those are stable, and they decide which platforms can host
this at all. Check current limits before committing to any provider.

---

## What the backend actually needs

| Requirement | Measured / required | Why it decides the platform |
|---|---|---|
| **RAM** | **461 MB** just to load torch + `all-MiniLM-L6-v2`, before serving a request | A 512 MB instance **OOMs at startup**. Realistically wants **1 GB**. |
| RAM without embeddings | ~120 MB | `EMBEDDINGS_ENABLED=0` fits a 512 MB box — see the trade below |
| Image size | ~1–1.5 GB (CPU-only torch, model baked in) | Some free tiers cap image size |
| Persistent disk | **Only if `GRAPH_BACKEND=sqlite`** | This is the single biggest constraint on free tiers |
| Always-on | Preferred, not required | Cold starts are slow: torch import plus model load |
| Outbound network | Groq / Notion / Neo4j Aura | Fine everywhere |
| Inbound auth | Provided by the app (`BRAHMASTRA_API_KEY`) | No platform feature needed |

### The 461 MB number is the one that matters

That is real measured RSS, not an estimate:

```
baseline python      :  30 MB
torch + model loaded : 461 MB
```

Most "free tier" web services offer 512 MB. The process would be killed while
starting, repeatedly, which reads as a crash loop rather than as "needs a
bigger box".

Two honest ways out:

1. **Give it 1 GB.** Simplest, and what the default configuration assumes.
2. **Set `EMBEDDINGS_ENABLED=0`.** Entity resolution falls back to exact
   matching plus Jaro-Winkler. Measured on the live graph: **417 entity
   clusters with embeddings, 475 without** — so roughly 58 merges are lost.
   "Sarah" and "sarah" still merge; "payments integration" and "the payments
   work" no longer do. A documented downgrade, not a crash.

---

## Recommended shape

**Neo4j for the graph, because you want multi-workspace and multi-device.**
That choice removes the persistent-disk requirement from the backend entirely,
which is what makes free and low-cost hosting realistic — the backend becomes
stateless and replaceable.

```
Frontend  ──▶  Backend (stateless)  ──▶  Neo4j Aura
Next.js        FastAPI + pipeline         the system of record
```

| Piece | Host on | Why |
|---|---|---|
| Frontend | **Vercel** | Next.js is its native target; the standalone build is not even needed there |
| Backend | a container host with **≥1 GB RAM** | see the table above |
| Graph | **Neo4j Aura** | already in use; removes the disk requirement |
| Scheduler | same host, second process | `python -m brahmastra.live_sync` |

### Backend host, by what you care about

| If you want | Look at | Watch out for |
|---|---|---|
| Least ops, generous free allowance | **Hugging Face Spaces** (Docker) | Public by default — set `BRAHMASTRA_API_KEY`; persistent storage is a paid add-on, so pair with Neo4j |
| Real always-free VM | **Oracle Cloud Free Tier** | Genuinely free ARM instances with real RAM and disk, but you operate the box yourself |
| Simple Docker + volumes | **Fly.io**, **Railway**, **Render** | Free allowances have narrowed; Render's free tier has **no persistent disk** and sleeps after idle |
| Already on a cloud | Cloud Run, App Runner, Container Apps | Scale-to-zero means slow cold starts here (torch import); set min instances to 1 |

**Do not put the backend on Vercel, Netlify or Lambda.** They are
request-scoped and ephemeral: no persistent disk for SQLite, and the torch
import cost is paid on cold starts. The frontend belongs there; the backend
does not.

---

## Keeping SQLite pluggable

Nothing above prevents SQLite. `GRAPH_BACKEND` is one variable, read by every
process from one file, so switching is a redeploy and not a rewrite:

```bash
GRAPH_BACKEND=sqlite   BRAHMASTRA_DB=/data/concept_graph.db   WEB_CONCURRENCY=1
GRAPH_BACKEND=neo4j    NEO4J_URI=...                          WEB_CONCURRENCY=4
```

Use SQLite when you want one box and no second service; use Neo4j when more
than one device writes. Moving between them:

```bash
python -m brahmastra.migrate_to_neo4j --apply   # SQLite -> Neo4j, mirrors
```

There is no automated Neo4j → SQLite path. Going back means re-extracting, or
writing the reverse migration.

---

## The free-tier reality check

- **Neo4j Aura Free suspends after ~3 days idle** and its hostname stops
  resolving while suspended — which looks exactly like deletion. Anything
  long-lived either needs traffic, a keep-alive ping, or a paid instance.
- **Groq's free tier has a hard daily token cap.** Extraction aborts the run at
  the first daily-cap error rather than grinding through it, and the notes stay
  `pending` for the next run. Budget roughly one LLM call per note per
  extraction, plus 25 for cluster summaries.
- **Sleeping web services** make the first request after idle very slow here,
  because it pays the torch import.

---

## Cost floor, honestly

With Aura Free, Groq free tier, Vercel free frontend, and a ~1 GB backend
instance, the only thing likely to cost money is the backend host. Whether a
free option exists there depends on current provider terms — check before
committing. The application itself imposes no licensing or per-seat cost.
