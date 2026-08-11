# Brahmastra: Quick Start Guide

## 30-Second Summary

Brahmastra turns **unstructured notes into a queryable knowledge graph** using LLM extraction, entity deduplication, and graph algorithms.

**What it does:**
- Reads notes from Notion, files, or text input
- Extracts facts (triples) using Claude 3.5 Haiku
- Deduplicates entities (Sarah = Sarah K. = Sarah Khan)
- Builds a graph and computes: centrality (PageRank), communities (Louvain), conflicts (contradictions), missing links (predictions)
- Shows results in interactive d3-force dashboard + CLI + MCP server

**Tech:** Python backend (FastAPI, SQLite, networkx) + Next.js frontend (React, d3-force), deployed on Vercel.

---

## Installation (5 minutes)

### Prerequisites
- Python 3.11+
- Node.js 18+ with pnpm
- Git

### Setup

```bash
git clone https://github.com/<user>/brahmastra.git
cd brahmastra

# Frontend
cd frontend && pnpm install && cd ..

# Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
uv pip install -e .
```

---

## Running Locally (2 minutes)

### Start Backend
```bash
cd backend
source .venv/bin/activate
uvicorn main:app --reload --port 8001
```

### Start Frontend (new terminal)
```bash
cd frontend
pnpm dev
```

**Open:** `http://localhost:3000`

You'll see a dashboard with **8 example notes** already processed into a graph (16 entities, 26 facts).

---

## Using the Dashboard

### The Graph View
- **Nodes** = entities (people, projects)
- **Edges** = relationships ("Sarah owns auth-migration")
- **Node size** = how important (PageRank)
- **Node color** = topic cluster (Louvain)
- **Drag/zoom** = interact
- **Click node** = see details

### The Insight Tabs

| Tab | What It Shows |
|-----|---------------|
| **Central Entities** | Most important people/things (PageRank leaderboard) |
| **Clusters** | Natural groupings (Louvain communities) |
| **Contradictions** | Conflicting facts ("Sarah reports to Mei Lin" vs "Bob") |
| **Predicted Links** | Recommendations ("Alice and Bob should probably be connected") |
| **Entity Resolution** | How duplicates were merged ("Sarah", "Sarah K.", "Sarah Khan" → "Sarah") |
| **Notes** | Browse vault, extract triples from new text |

---

## Add Your Own Notes

### Option 1: Web UI (Easiest)
1. Click **Notes** tab
2. Paste text in "Extract triples" box
3. Claude will extract facts automatically

### Option 2: CLI
```bash
cd backend && source .venv/bin/activate

# Add a note
brahmastra add-note "Team Meeting"  # follow prompts

# Process it
brahmastra run  # incremental (only new notes)
brahmastra run --full  # re-process everything

# View results
brahmastra show graph
brahmastra show central-entities
brahmastra show clusters
```

### Option 3: Notion Integration
```bash
export NOTION_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="xxxxx"

brahmastra sync
brahmastra run
```

---

## CLI Commands Reference

```bash
# Pipeline operations
brahmastra run              # Incremental extraction
brahmastra run --full       # Force re-extract all notes
brahmastra sync             # Sync from Notion

# View results
brahmastra show graph       # Full stats
brahmastra show nodes       # Entity leaderboard
brahmastra show clusters    # Communities
brahmastra show contradictions  # Conflicts
brahmastra show predicted-links  # Recommendations
brahmastra show notes       # Vault browser

# Add data
brahmastra add-note "Title" # Interactive prompt

# Server
brahmastra mcp              # Start MCP server (for Claude)
```

---

## MCP Server (Claude Integration)

```bash
# Terminal 1: Start server
brahmastra mcp

# In Claude Code:
# Settings → Add Connection
# Name: Brahmastra
# Command: python -m brahmastra.mcp_server
```

Now you can ask Claude:
```
"What are the 5 most central entities in my knowledge graph?"
"Show me all contradictions in my notes"
"Who should I probably introduce to each other?"
```

---

## Architecture at a Glance

```
Your Notes → [Sync] → SQLite
    ↓
[Extract] → Claude 3.5 Haiku (facts)
    ↓
[Resolve] → Union-Find (dedup entities)
    ↓
[Build Graph] → networkx (full provenance)
    ↓
[Analyze] → PageRank, Louvain, contradictions, predictions
    ↓
Frontend Dashboard ← REST API ← /api/graph
```

**5 stages, all incremental + atomic.**

---

## Configuration

### Environment Variables

**Required (to use live LLM extraction):**
```bash
ANTHROPIC_API_KEY=sk_key_xxx
```

**Optional (for Notion sync):**
```bash
NOTION_TOKEN=secret_xxx
NOTION_DATABASE_ID=xxx_yyy
```

**Set them:**
```bash
export ANTHROPIC_API_KEY="sk_..."
export NOTION_TOKEN="secret_..."
```

Or create `.env` in `backend/`:
```
ANTHROPIC_API_KEY=sk_...
NOTION_TOKEN=secret_...
NOTION_DATABASE_ID=xxx
```

### Ontology (10 Relation Types)

All extraction is constrained to 10 relation types:

| Relation | Example |
|----------|---------|
| `owns` | Sarah **owns** auth-migration |
| `reports_to` | Sarah **reports_to** Mei-Lin |
| `depends_on` | auth-migration **depends_on** database-migration |
| `related_to` | authentication **related_to** security |
| `scheduled_for` | auth-migration **scheduled_for** March-15 |
| `uses` | team **uses** PostgreSQL |
| `located_in` | team **located_in** San-Francisco |
| `has_status` | project **has_status** in-progress |
| `created_by` | RFC **created_by** Alice |
| `mentions` | note **mentions** compliance |

See `ontology.yaml` for full definitions.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Backend won't start | Check Python 3.11+, `python3 --version`. Check port 8001 is free. |
| "No module named X" | `uv pip install -e .` in backend directory |
| Frontend can't reach backend | Is backend running on 8001? Check browser console Network tab. |
| LLM extraction not working | Check `ANTHROPIC_API_KEY` is set, not empty, starts with `sk_` |
| Dashboard shows "seed data only" | Backend is not running or `/api/health` fails. Frontend gracefully falls back. |
| Notion sync fails | Check `NOTION_TOKEN` is valid, DB is shared with email that has access |

---

## Deployment to Vercel

### 1. Connect GitHub

```bash
vercel link
```

### 2. Set Environment Variables

```bash
vercel env add ANTHROPIC_API_KEY
vercel env add NOTION_TOKEN
vercel env add NOTION_DATABASE_ID
```

### 3. Deploy

```bash
git push  # Vercel auto-deploys on push to main
```

**Both services deploy together** via `vercel.json` experimentalServices.

---

## Key Algorithms Explained (2-Minute Versions)

### PageRank (Find Central Entities)
**Idea:** Model as random walk. An entity is important if important entities point to it.
```
Sarah ← Mei-Lin ← CTO → (Sarah is central because CTO is important)
```

### Louvain (Find Communities)
**Idea:** Group entities that have many connections to each other, few to outside.
```
Cluster 1: [Sarah, Mei-Lin, HR-policies]
Cluster 2: [auth-migration, backend-team, database]
```

### Union-Find (Dedup Entities)
**Idea:** If "Sarah" ~ "Sarah K." and "Sarah K." ~ "Sarah Khan", then all three are same.
```
Sarah → Sarah K. → Sarah Khan  (all in same group via Union-Find)
```

### Contradiction Detection
**Idea:** For relations that should have ≤1 value (reports_to, located_in), flag multiple values.
```
ERROR: Sarah reports_to [Mei-Lin (old), Bob (new)]
```

---

## Performance

On seed data (8 notes, 26 triples):
- Extraction: 4 seconds (0.5s per note via Claude)
- Entity resolution: 200ms (blocking + Union-Find)
- Graph building: 50ms (networkx)
- Algorithms: 100ms (PageRank + Louvain)
- **Total: ~4.5 seconds**

Scales linearly with note count. 100 notes ≈ 50 seconds. 1000 notes ≈ 8 minutes.

---

## Next Steps

1. **Read full docs:** See `README.md` for complete feature list
2. **Understand architecture:** See `DETAILED_ARCHITECTURE.md` for deep dives
3. **Add your data:** Use CLI or web UI
4. **Integrate with Claude:** Set up MCP server
5. **Deploy:** Push to Vercel for live instance

---

## File Structure

```
brahmastra/
├── frontend/               ← Next.js React app (port 3000)
│   ├── components/        ← Graph, panels, inspector
│   ├── app/page.tsx       ← Entry point
│   └── lib/               ← Types, pipeline, viz helpers
├── backend/               ← Python FastAPI (port 8001)
│   ├── brahmastra/
│   │   ├── db.py         ← SQLite CRUD
│   │   ├── extraction.py ← Claude integration
│   │   ├── entity_resolution.py ← Union-Find
│   │   ├── concept_graph.py ← Algorithms
│   │   ├── pipeline.py   ← Orchestration
│   │   ├── cli.py        ← Typer CLI
│   │   ├── sync.py       ← Notion sync
│   │   └── mcp_server.py ← MCP server
│   ├── main.py           ← FastAPI routes
│   └── data/
│       └── concept_graph.db ← SQLite (gitignored)
├── README.md             ← Full documentation
├── DETAILED_ARCHITECTURE.md ← Deep technical guide
└── QUICK_START.md        ← This file
```

---

## Core Concepts

### Triple
A fact: `(subject, relation, object, confidence, source_quote)`
```
("Sarah", "owns", "auth-migration", 0.95, "She owns the whole effort")
```

### Graph
Directed multigraph where:
- Nodes = canonical entity names
- Edges = triples (multiple edges between same pair allowed)
- Metadata on each edge = confidence, source, date

### Insight
Computed property of the graph:
- **Centrality** = PageRank score (0.0–1.0)
- **Community** = Louvain cluster ID
- **Contradiction** = multiple values for functional relation
- **Prediction** = likely-but-missing connection

### Canonical Entity
The deduplicated name for a cluster of mentions:
```
Mentions: ["Sarah", "Sarah K.", "Sarah Khan"]
Canonical: "Sarah"
```

---

## Support

- **Docs:** `README.md`, `DETAILED_ARCHITECTURE.md`
- **Issues:** GitHub issues
- **Questions:** Add docstring comments in code, commit, push

---

## License

See LICENSE file (MIT or similar).

---

## TL;DR

1. **Install:** `pnpm install` (frontend), `uv pip install -e .` (backend)
2. **Run:** `vercel dev` or `pnpm dev` + `uvicorn main:app --reload --port 8001`
3. **Use:** Open `http://localhost:3000`, add notes, see graph
4. **Deploy:** `git push`, Vercel auto-deploys both services
5. **Extend:** Add custom relations to `ontology.yaml`, add stages to `pipeline.py`
