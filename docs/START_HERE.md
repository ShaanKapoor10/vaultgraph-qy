# 🚀 START HERE — Brahmastra Documentation Roadmap

Welcome to **Brahmastra: Concept Graph Engine**! This guide will help you find exactly what you need.

---

## 🎯 What Do You Want To Do?

### ⚡ **"I want to RUN IT RIGHT NOW"** (15 minutes)
👉 **Read:** [`QUICK_START.md`](QUICK_START.md)

Includes:
- Installation (5 min)
- Local setup (2 min)  
- Add your first note (2 min)
- CLI commands (instant results)

### 📚 **"I want to UNDERSTAND what it does"** (30 minutes)
👉 **Read:** [`README.md`](../README.md) then [`QUICK_START.md`](QUICK_START.md)

Includes:
- Feature overview
- Database schema
- 12-step implementation summary
- Architecture diagram

### 🔬 **"I want to UNDERSTAND how it WORKS"** (1-2 hours)
👉 **Read:** [`DETAILED_ARCHITECTURE.md`](DETAILED_ARCHITECTURE.md)

Includes:
- Big picture & problem/solution
- 5-stage pipeline explained (with code)
- Algorithm deep dives:
  - Union-Find (entity deduplication)
  - Jaro-Winkler (string similarity)
  - PageRank (centrality ranking)
  - Louvain (community detection)
- Data flow & persistence
- Component breakdown (frontend + backend)

### 🎓 **"I'm confused — HELP ME NAVIGATE"** (5 minutes)
👉 **Read:** [`DOCUMENTATION_INDEX.md`](DOCUMENTATION_INDEX.md)

Includes:
- 15 different reading paths by goal
- Navigation by role (data scientist, dev, devops, PM, student)
- Time-based reading plans
- Topic-based section finder
- Cross-reference guide

---

## 📖 Documentation Map

```
START_HERE.md (you are here)
    ↓
 ┌──────────────────────────────────────────┐
 │                                          │
 ├─→ QUICK_START.md ────→ Run locally      │
 │   (15 min)              Get working      │
 │                                          │
 ├─→ README.md ─────────→ See all features │
 │   (30 min)              Know what exists │
 │                                          │
 ├─→ DETAILED_ARCHITECTURE.md ─→ Learn  │
 │   (1-2 hours)                   implementation
 │                                          │
 └─→ DOCUMENTATION_INDEX.md ─→ Pick your  │
    (navigation guide)       perfect path  │
```

---

## 🚦 Quick Decision Tree

```
Do you have 15 minutes?
├─ YES → QUICK_START.md
└─ NO  → Skip to specific doc

Do you want to RUN it?
├─ YES → QUICK_START.md (installation + local dev)
└─ NO  → Continue below

Do you want to UNDERSTAND it?
├─ YES → README.md + DETAILED_ARCHITECTURE.md
└─ NO  → Continue below

Do you want to EXTEND it?
├─ YES → DETAILED_ARCHITECTURE.md (full code examples)
└─ NO  → Continue below

Do you want to DEPLOY it?
├─ YES → QUICK_START.md (Vercel section)
└─ NO  → Try: vercel dev (it just works!)

Do you want CLAUDE integration?
└─ YES → QUICK_START.md (MCP server section)
```

---

## 📊 What Is This Project?

**Brahmastra** is a **knowledge graph engine** that:

```
YOUR NOTES → Extract facts → Dedup entities → Build graph → Visualize
   (text)    (with Claude)   (Union-Find)    (NetworkX)   (d3-force)
```

**Example:**
```
Input note: "Sarah leads the auth migration. She reports to Mei Lin."

Facts extracted: 
  • Sarah → leads → auth migration
  • Sarah → reports_to → Mei Lin

Output graph shows:
  • Sarah is important (PageRank centrality)
  • Sarah + Mei Lin form a team (Louvain community)
  • Recommended link: Mei Lin → auth migration
```

---

## ✨ Key Features at a Glance

| Feature | Details |
|---------|---------|
| **Input** | Notion, files, manual text |
| **Extraction** | Claude 3.5 Haiku (ontology-constrained) |
| **Deduplication** | Union-Find + semantic embeddings |
| **Graph** | NetworkX with full provenance |
| **Analysis** | PageRank, Louvain, contradictions, predictions |
| **Output** | Interactive dashboard, CLI, REST API, MCP server |
| **Database** | SQLite with incremental updates |
| **Testing** | 44 pytest tests, all passing |

---

## 🎯 Suggested Reading Paths

### Path 1: I Just Want to Try It (⏱️ 15 min)
1. Read **QUICK_START.md** (10 min)
2. Run locally and play (5 min)
3. Done! You have a working system.

### Path 2: I Want to Understand (⏱️ 45 min)
1. Read **README.md** (15 min)
2. Read **QUICK_START.md** for examples (15 min)
3. Try it locally (15 min)
4. Check out the dashboard

### Path 3: I Want to Learn Deep (⏱️ 2 hours)
1. Read **README.md** (15 min)
2. Read **DETAILED_ARCHITECTURE.md** (90 min)
   - The Big Picture (5 min)
   - 5-Stage Pipeline (30 min)
   - Algorithm Deep Dives (30 min)
   - Component Breakdown (15 min)
3. Look at code files
4. Try running locally and modifying

### Path 4: I'm a [Role] (⏱️ varies)
- **Data Scientist** → DETAILED_ARCHITECTURE.md (Algorithm section)
- **Full-Stack Developer** → README.md + DETAILED_ARCHITECTURE.md (all)
- **DevOps** → QUICK_START.md (Deployment section)
- **Product Manager** → README.md (Features section)
- **Student** → Everything (great learning project)

See **DOCUMENTATION_INDEX.md** for more paths.

---

## 🔍 File Reference

```
START_HERE.md ........................... This file (entry point)
│
├─ QUICK_START.md ....................... Fastest way to run (15 min)
├─ README.md ........................... Complete overview (30 min)
├─ DETAILED_ARCHITECTURE.md ........... Deep technical dive (1-2 hours)
├─ DOCUMENTATION_INDEX.md ............ Navigation guide (find your path)
│
├─ backend/ ........................... Python code (2,872 lines)
│   ├─ brahmastra/
│   │  ├─ extraction.py .............. Claude LLM integration
│   │  ├─ entity_resolution.py ....... Union-Find algorithm
│   │  ├─ concept_graph.py ........... PageRank, Louvain, etc.
│   │  ├─ pipeline.py ............... 5-stage orchestrator
│   │  ├─ cli.py ..................... Typer CLI
│   │  ├─ sync.py .................... Notion sync
│   │  └─ mcp_server.py .............. Claude integration
│   ├─ main.py ....................... FastAPI routes
│   └─ tests/ ......................... 44 pytest tests
│
├─ frontend/ .......................... React code (21+ components)
│   ├─ components/
│   │  ├─ dashboard.tsx .............. Main layout
│   │  ├─ graph-view.tsx ............ d3-force visualization
│   │  └─ panels/ ................... 6 insight tabs
│   ├─ lib/
│   │  ├─ backend-adapter.ts ........ Schema conversion
│   │  └─ types.ts .................. TypeScript definitions
│   └─ app/
│      ├─ page.tsx .................. Entry point
│      └─ actions/extract.ts ........ Claude integration
│
├─ ontology.yaml ...................... 10 relations, 12 entity types
└─ vercel.json ........................ Deployment config
```

---

## 🚀 Quick Start Commands

```bash
# Clone and install
git clone <repo>
cd brahmastra
cd frontend && pnpm install && cd ..
cd backend && python3 -m venv .venv && source .venv/bin/activate && uv pip install -e .

# Run locally
vercel dev  # or: pnpm dev + uvicorn main:app --reload --port 8001

# View at http://localhost:3000

# Use CLI
brahmastra run              # Extract
brahmastra show graph       # Results
brahmastra show clusters    # Communities

# Deploy
git push                    # Auto-deploys to Vercel
```

---

## ✅ What You'll Learn

- **Algorithm**: Union-Find, Jaro-Winkler, PageRank, Louvain
- **Backend**: FastAPI, SQLite, LLM integration, async
- **Frontend**: React, d3-force, TypeScript
- **Full-stack**: How to build and deploy a complete system
- **Testing**: pytest, mocking, integration tests
- **DevOps**: Vercel deployments, multi-service orchestration

---

## 🎓 Recommended Learning Order

1. **Start**: Read **QUICK_START.md** (get it running)
2. **Understand**: Read **README.md** (see what's possible)
3. **Deep dive**: Read **DETAILED_ARCHITECTURE.md** (learn how)
4. **Experiment**: Modify code locally
5. **Deploy**: Push to Vercel
6. **Integrate**: Set up MCP server for Claude

---

## 💡 Common Questions

**Q: How long does it take to get it running?**
A: 15 minutes with **QUICK_START.md**

**Q: Do I need to know about machine learning?**
A: No! The LLM (Claude) handles the hard part. You just orchestrate.

**Q: Can I use this with my own data?**
A: Yes! See **QUICK_START.md** for multiple ways to add notes.

**Q: Is this production-ready?**
A: Yes! 44 tests passing, SQLite persistence, Vercel deployment.

**Q: How do I contribute?**
A: Read the relevant documentation, modify code, test, commit, push PR.

---

## 🎯 Your Next Step

**Choose your path based on your goal:**

| Goal | Next File | Time |
|------|-----------|------|
| 🏃 Run it now | QUICK_START.md | 15 min |
| 📖 Understand features | README.md | 30 min |
| 🧠 Learn implementation | DETAILED_ARCHITECTURE.md | 1-2 hrs |
| 🗺️ Find your path | DOCUMENTATION_INDEX.md | 5 min |

---

## 📞 Need Help?

1. **Can't find what you need?** → Read **DOCUMENTATION_INDEX.md** (navigation guide)
2. **Getting an error?** → Check **QUICK_START.md** Troubleshooting section
3. **Want to understand deeper?** → Read **DETAILED_ARCHITECTURE.md**
4. **Have a question?** → GitHub issues

---

## 🎉 You're Ready!

Pick one of the files above and start reading. You'll have a working knowledge graph system in minutes.

**Recommended first step:** [`QUICK_START.md`](QUICK_START.md) ← Click here!

---

**Brahmastra © 2025 • Built with v0**
