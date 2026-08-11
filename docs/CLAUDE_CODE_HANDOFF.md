# 🎯 Claude Code Handoff: Brahmastra Project Integration

**Purpose:** This document enables Claude Code to work effectively on the Brahmastra project while using Brahmastra itself to maintain context and coordinate work.

**Status:** Self-documenting & self-maintaining project with persistent memory.

---

## 📋 Quick Reference

- **Project:** Brahmastra Concept Graph Engine
- **Type:** Full-stack app (Python backend + TypeScript frontend)
- **Integration:** Claude Code + Brahmastra MCP
- **Your Role:** Use Brahmastra to store/recall project context automatically

---

## 🏗️ Project Architecture

```
brahmastra-project/
├── backend/                          # Python FastAPI + AI/Graph
│   ├── brahmastra/
│   │   ├── cli.py                   # CLI interface
│   │   ├── extraction.py            # Claude LLM extraction (facts)
│   │   ├── entity_resolution.py     # Union-Find deduplication
│   │   ├── concept_graph.py         # NetworkX graph algorithms
│   │   ├── pipeline.py              # Main 5-stage pipeline
│   │   ├── db.py                    # PostgreSQL/SQLite DB
│   │   ├── mcp_server.py            # MCP protocol handler
│   │   ├── ontology.py              # Relation/type definitions
│   │   ├── sync.py                  # Notion/external sync
│   │   └── routers/
│   │       ├── notes.py             # POST /notes (add notes)
│   │       ├── graph.py             # GET /graph (query)
│   │       └── pipeline.py          # POST /pipeline (process)
│   ├── main.py                      # FastAPI app entry
│   ├── pyproject.toml               # Dependencies (FastAPI, Claude API, etc.)
│   └── tests/                       # pytest tests (44 passing)
│
├── frontend/                         # Next.js 16 + TypeScript
│   ├── app/
│   │   ├── page.tsx                 # Main dashboard
│   │   └── layout.tsx               # Root layout
│   ├── components/
│   │   ├── dashboard.tsx            # Dashboard UI
│   │   ├── graph-view.tsx           # NetworkX graph visualization
│   │   └── panels/                  # UI panels
│   ├── package.json                 # Dependencies
│   └── next.config.mjs              # Next.js config
│
├── ontology.yaml                    # Relations & entity types
├── vercel.json                      # Vercel deployment config
└── *.md                             # Documentation
```

---

## 🚀 How to Integrate Claude Code

### Step 1: Set Up Brahmastra MCP (One-Time)

```bash
# Terminal 1: Start Brahmastra MCP server
cd /vercel/share/v0-project/backend
brahmastra mcp
```

### Step 2: Configure Claude Code

Edit `~/.config/claude/claude.json`:

```json
{
  "mcpServers": {
    "brahmastra": {
      "command": "python",
      "args": ["-m", "brahmastra.mcp_server"]
    }
  }
}
```

### Step 3: Start Using

```
You: "I'm working on Brahmastra feature X"
Claude Code: (stores in Brahmastra automatically)

Later:
You: "What was I working on?"
Claude Code: (recalls from Brahmastra automatically)
```

---

## 💾 How Claude Code Should Maintain Project Context

### On Every Work Session

1. **Start:** Ask Claude Code to recall project status
   ```
   "Show me what I've been working on in Brahmastra"
   ```

2. **During work:** Claude Code automatically stores decisions
   ```
   "Remember: Fixed bug in entity_resolution.py (line 42)"
   → Automatically stored in Brahmastra ✅
   ```

3. **End session:** Ask Claude Code to summarize
   ```
   "What did we accomplish? Any blockers?"
   → Claude Code queries Brahmastra, provides summary ✅
   ```

### What Claude Code Should Store

Claude Code should use Brahmastra to maintain:

```
✅ Features you're building
✅ Bugs you've found
✅ Code decisions made
✅ Files modified
✅ Tests added/fixed
✅ Architecture changes
✅ Performance improvements
✅ Known issues/TODOs
✅ Dependencies added
✅ Configuration changes
```

---

## 📁 Key Files to Understand

### Backend Files

| File | Purpose | Key Functions |
|------|---------|---------------|
| `extraction.py` | Claude LLM calls to extract facts | `extract_triples(text)` |
| `entity_resolution.py` | Deduplicates entities (Union-Find) | `deduplicate()` |
| `concept_graph.py` | Graph algorithms (PageRank, Louvain) | `compute_centrality()` |
| `pipeline.py` | 5-stage processing pipeline | `run_pipeline()` |
| `db.py` | Database operations (PostgreSQL) | `add_triple()`, `query_entities()` |
| `mcp_server.py` | MCP protocol for Claude integration | `handle_request()` |
| `routers/notes.py` | REST API for adding notes | `POST /notes` |

### Frontend Files

| File | Purpose |
|------|---------|
| `components/dashboard.tsx` | Main UI dashboard |
| `components/graph-view.tsx` | Visualize knowledge graph |
| `app/page.tsx` | Home page |

---

## 🔧 Common Tasks & How to Use Them

### Task 1: Add a New Feature

```
You: "Add feature: entity type filtering in graph queries"

Claude Code workflow:
1. Recalls project structure (from Brahmastra)
2. Looks at routers/graph.py for query patterns
3. Checks ontology.yaml for entity types
4. Implements filtering logic
5. Stores: "Added entity type filtering to graph.py"
   → Automatic Brahmastra storage ✅
6. Runs tests to verify
7. Stores: "Feature complete, 2 new tests added"
   → Automatic Brahmastra storage ✅
```

### Task 2: Fix a Bug

```
You: "Bug in entity_resolution.py - duplicates not merging correctly"

Claude Code workflow:
1. Recalls: "What bugs are known?" (from Brahmastra)
2. Opens entity_resolution.py, understands Union-Find logic
3. Traces issue to line 42 (merge condition)
4. Stores: "Found bug: merge condition incorrect (line 42)"
   → Automatic Brahmastra storage ✅
5. Fixes and tests
6. Stores: "Bug fixed and verified with test case"
   → Automatic Brahmastra storage ✅
```

### Task 3: Refactor Code

```
You: "Refactor extraction.py for clarity"

Claude Code workflow:
1. Recalls: What was changed last in extraction.py? (from Brahmastra)
2. Understands current structure
3. Plans refactoring (storing in Brahmastra)
4. Implements changes
5. Stores: "Refactored extraction.py: separated concerns"
   → Automatic Brahmastra storage ✅
6. Verifies tests still pass
```

### Task 4: Debug an Issue

```
You: "Graph query is slow, debug performance"

Claude Code workflow:
1. Recalls: Any recent performance changes? (from Brahmastra)
2. Profiles the query
3. Stores: "Performance issue: O(n²) in concept_graph.py"
   → Automatic Brahmastra storage ✅
4. Optimizes using caching
5. Stores: "Optimized with caching, 10x faster"
   → Automatic Brahmastra storage ✅
6. Benchmarks improvement
```

---

## 🔌 MCP Tools Available to Claude Code

Claude Code can call these automatically:

```python
# 1. Add/store notes (project context)
add_note(title="Feature: entity filtering", content="...")

# 2. Query stored knowledge
search_entities("bug fixes")

# 3. Get all changes
get_entity_details("extraction.py")

# 4. Check for contradictions
get_contradictions()

# 5. Find patterns in work
get_graph_stats()
```

---

## 📊 Project Statistics

- **Lines of Python:** ~2,000 (backend)
- **Lines of TypeScript:** ~1,500 (frontend)
- **Test Coverage:** 44 passing tests
- **Database Tables:** 5 (notes, entities, relations, entity_resolution, graph_cache)
- **API Endpoints:** 12 REST + MCP protocol
- **Graph Algorithms:** 3 (Union-Find, PageRank, Louvain)
- **LLM Integration:** Anthropic Claude API

---

## 🧪 Running Tests

```bash
# Run all tests
cd backend
pytest tests/

# Run specific test
pytest tests/test_extraction.py

# Run with coverage
pytest --cov=brahmastra tests/
```

All 44 tests should pass.

---

## ⚙️ Development Workflow

### Start Development

```bash
# Terminal 1: Start Brahmastra backend (MCP)
cd backend
brahmastra mcp

# Terminal 2: Start Brahmastra REST server (for testing)
cd backend
python -m brahmastra.main
# Server on http://localhost:8000

# Terminal 3: Start frontend dev server
cd frontend
pnpm dev
# Frontend on http://localhost:3000
```

### Make Changes

1. Edit files in `backend/brahmastra/` or `frontend/components/`
2. Claude Code automatically stores changes in Brahmastra
3. Tests auto-run or manually run `pytest`
4. Verify in browser at `http://localhost:3000`

### Commit Changes

```bash
git add .
git commit -m "feat: description of changes"
git push origin v0/shaankapoorwork-5560-4b5b397a
```

---

## 🗂️ File-by-File Guide

### backend/brahmastra/extraction.py

**Purpose:** Call Claude LLM to extract facts from text

```python
# Example: Extract facts from note
response = extract_triples("Sarah leads auth migration")
# Returns: [
#   {"subject": "Sarah", "relation": "leads", "object": "auth migration", "confidence": 0.95}
# ]
```

**Claude Code should know:** 
- Uses Anthropic Claude API
- Returns structured triples
- Confidence scores on each triple

### backend/brahmastra/entity_resolution.py

**Purpose:** Deduplicate entities using Union-Find

```python
# Example: Merge "Sarah" and "S. Khan" as same entity
resolve_entities(["Sarah", "S. Khan", "sarah"])
# Returns: canonical entity "Sarah" with all mentions
```

**Claude Code should know:**
- Union-Find data structure
- Jaro-Winkler string similarity
- Deduplication affects entire graph

### backend/brahmastra/concept_graph.py

**Purpose:** Graph algorithms on knowledge graph

```python
# Example: Find most important entities
centrality = compute_centrality(graph)
# Returns: PageRank scores for all entities
```

**Claude Code should know:**
- NetworkX library
- PageRank algorithm
- Louvain community detection

### backend/brahmastra/pipeline.py

**Purpose:** 5-stage processing pipeline

**Stages:**
1. Note input
2. Claude LLM extraction
3. Entity resolution
4. Graph update
5. Algorithm computation

**Claude Code should know:**
- Pipeline is fully automated
- Each stage stores results
- Runs on demand or scheduled

### backend/brahmastra/db.py

**Purpose:** Database operations

```python
# Add a triple
add_triple(subject="Sarah", relation="leads", object="auth", confidence=0.95)

# Query entities
entities = query_entities(name="Sarah")

# Get contradictions
conflicts = get_contradictions()
```

**Claude Code should know:**
- Uses SQLite (dev) or PostgreSQL (prod)
- ACID transactions
- Full schema versioning

### backend/brahmastra/mcp_server.py

**Purpose:** MCP protocol implementation

**What Claude Code needs to know:**
- Automatically discovered by Claude
- Handles stdin/stdout
- Transforms REST API to MCP protocol

---

## 🎯 Recommended Claude Code Prompts

### For Feature Development
```
"Add [feature name] to Brahmastra following these specs: [details]
Store your progress in Brahmastra so we maintain context."
```

### For Bug Fixes
```
"Fix bug: [description]
Store the bug details and fix in Brahmastra.
Include any test cases added."
```

### For Code Review
```
"Review [file] for:
- Performance issues
- Edge cases
- Best practices
Store findings in Brahmastra."
```

### For Documentation
```
"Document [feature/module] and store in Brahmastra.
Include: purpose, usage, examples."
```

### For Status Check
```
"What have I been working on? Get status from Brahmastra."
```

### For Architecture Questions
```
"How does [component] work? Check Brahmastra for context.
Update Brahmastra if you modify the architecture."
```

---

## 📚 Documentation Links

- **Architecture Deep Dive:** `DETAILED_ARCHITECTURE.md`
- **Algorithm Explanations:** `DETAILED_ARCHITECTURE.md` (Algorithms section)
- **Integration Guide:** `AI_AGENTS_INTEGRATION.md`
- **API Endpoints:** See `backend/brahmastra/routers/`
- **Database Schema:** `backend/brahmastra/db.py`

---

## 🚨 Common Issues & Solutions

### Issue: MCP Server Not Starting
**Solution:** Ensure backend is installed:
```bash
cd backend
pip install -e .
```

### Issue: Claude Code Can't Find Brahmastra
**Solution:** Restart Claude Code after updating config.json

### Issue: Tests Failing
**Solution:** 
```bash
cd backend
pytest -v tests/
```

### Issue: Database Locked
**Solution:** Clear database and restart:
```bash
rm backend/data/brahmastra.db
```

---

## ✅ Checklist: Before You Start

- [ ] Backend installed: `cd backend && pip install -e .`
- [ ] Brahmastra MCP started: `brahmastra mcp`
- [ ] Claude Code configured: `~/.config/claude/claude.json` updated
- [ ] Tests passing: `pytest tests/` all green
- [ ] Frontend builds: `cd frontend && pnpm dev` runs
- [ ] You've read: `README.md` and `DETAILED_ARCHITECTURE.md`

---

## 🎓 Key Concepts

### The 5-Stage Pipeline
1. **Input:** User adds note
2. **Extraction:** Claude LLM extracts facts
3. **Resolution:** Deduplicate entities
4. **Graph:** Update knowledge graph
5. **Algorithms:** Compute centrality & clusters

### Union-Find (Deduplication)
- Merges "Sarah", "S. Khan", "sarah" → single entity
- Tracks merge history
- Never loses information

### Graph Algorithms
- **PageRank:** Find most important entities
- **Louvain:** Discover entity clusters/communities

### MCP Protocol
- Claude Code ↔ stdin/stdout ↔ Brahmastra MCP
- Self-describing (Claude discovers tools)
- Automatic tool calling

---

## 🚀 Getting Started Right Now

1. **Read this:** You're reading it ✅
2. **Start MCP:** `brahmastra mcp`
3. **Configure:** Update `~/.config/claude/claude.json`
4. **Try it:** Ask Claude Code something
5. **It works:** Claude Code now has Brahmastra

---

## 📝 Session Template

Use this template for each session:

```
Session Start: [date/time]

Task: [what you're working on]

Claude Code workflow:
1. Recall context: "What have I been doing?"
2. Check status: "Any open issues or blockers?"
3. Start work: [implement/fix/refactor]
4. Store progress: Automatic (Brahmastra)
5. End session: "Summarize what we did"

Notes:
- [key decisions]
- [blockers encountered]
- [next steps]

Session End: [date/time]
```

---

## 🎯 The Point

With this setup, Claude Code:
- ✅ Understands your full project structure
- ✅ Maintains context across sessions (via Brahmastra)
- ✅ Never forgets what you built
- ✅ Can coordinate multi-session work
- ✅ Learns from past changes
- ✅ Stores all decisions

**Result:** Self-maintaining project with persistent AI assistant.

**Ready?** Start your Claude Code session now! 🚀
