# 📋 Claude Code Quick Reference Card

**Print this or keep open while using Claude Code with Brahmastra**

---

## ⚡ Quick Start (Copy-Paste)

### Terminal 1: Start Brahmastra MCP
```bash
cd /vercel/share/v0-project/backend
brahmastra mcp
```

### Terminal 2: Start Frontend (optional)
```bash
cd /vercel/share/v0-project/frontend
pnpm dev
```

### File: ~/.config/claude/claude.json
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

**Then:** Restart Claude Code and it works! ✅

---

## 🗂️ Project Files Cheat Sheet

| File | What it does | When to edit |
|------|-------------|-------------|
| `extraction.py` | Claude LLM fact extraction | New extraction logic |
| `entity_resolution.py` | Deduplicates entities | Merge logic issues |
| `concept_graph.py` | Graph algorithms | Add/fix PageRank, Louvain |
| `pipeline.py` | Main 5-stage pipeline | Change processing flow |
| `db.py` | Database queries | Add new queries |
| `mcp_server.py` | Claude integration | MCP protocol issues |
| `routers/notes.py` | REST API for notes | Change API behavior |
| `routers/graph.py` | REST API for queries | Change query API |
| `dashboard.tsx` | Main UI | UI changes |
| `graph-view.tsx` | Graph visualization | Visualization changes |

---

## 🎯 Common Prompts

**Start session:** "Recall what I've been working on"
**During work:** "Remember: [change you made]"
**End session:** "Summarize what we accomplished"
**Status:** "What's the current state of [component]?"
**Debug:** "Debug this issue: [description]"
**Add feature:** "Add [feature] to [file]"
**Fix bug:** "Fix [bug] in [file]"

---

## 🔄 Typical Session Flow

1. **Start:** `"Show me recent work"`
2. **Plan:** `"Here's what I want to do: [description]"`
3. **Execute:** Claude Code implements
4. **Test:** `"Run tests for [component]"`
5. **Verify:** Check http://localhost:3000 (if frontend)
6. **Commit:** `"Commit: [message]"`
7. **End:** `"Summarize this session"`

---

## 🧪 Testing

```bash
# Run all tests
pytest tests/

# Run specific module
pytest tests/test_extraction.py -v

# With coverage
pytest --cov=brahmastra tests/
```

All 44 tests should pass.

---

## 🔌 Brahmastra MCP Tools (Auto-Available)

Claude Code can call these automatically:

```
add_note(title, content)           → Store project context
search_entities(query)              → Find stored info
get_entity_details(entity_id)       → Get all details
get_contradictions()                → Find conflicts
get_graph_stats()                   → Get metrics
run_pipeline()                      → Process pending notes
get_predicted_links()               → Suggest connections
```

---

## 📊 Project Structure

```
backend/brahmastra/           ← Edit these files
├── extraction.py            (Claude LLM calls)
├── entity_resolution.py     (Deduplication)
├── concept_graph.py         (Graph algos)
├── pipeline.py              (Processing)
├── db.py                    (Database)
├── mcp_server.py            (Claude integration)
└── routers/
    ├── notes.py             (API for notes)
    └── graph.py             (API for queries)

frontend/                     ← UI changes
├── components/
│   ├── dashboard.tsx        (Main UI)
│   └── graph-view.tsx       (Graph viz)
└── app/
    └── page.tsx             (Home)
```

---

## 🚀 Workflow Example

### Adding a Feature

```
You: "Add entity type filtering to graph queries"

Claude Code:
1. Stores in Brahmastra: "Working on: entity type filtering"
2. Opens routers/graph.py
3. Checks ontology.yaml for types
4. Implements filtering
5. Stores: "Added filter_by_type parameter"
6. Runs: pytest tests/test_graph.py
7. Updates: dashboard to show filter UI
8. Commits: "feat: entity type filtering in graph queries"
9. Stores final: "Feature complete and tested"
```

---

## 🐛 Debugging Example

```
You: "Graph queries are slow"

Claude Code:
1. Stores: "Investigating: slow graph queries"
2. Profiles queries in concept_graph.py
3. Stores: "Found: O(n²) in centrality calculation"
4. Optimizes with caching in db.py
5. Benchmarks: "10x faster after caching"
6. Stores: "Performance improved, benchmark added"
7. Runs all tests to verify no regressions
```

---

## 📈 Important URLs

- **Backend API:** http://localhost:8000
- **Frontend UI:** http://localhost:3000
- **API Docs:** http://localhost:8000/docs

---

## 🔑 Key Files at a Glance

**Read these first:**
- `README.md` — Project overview
- `DETAILED_ARCHITECTURE.md` — Deep technical guide
- `CLAUDE_CODE_HANDOFF.md` — Full integration guide (this handoff)

**Reference during work:**
- `ontology.yaml` — Entity types and relations
- `backend/brahmastra/db.py` — Database schema
- `backend/tests/` — Test examples

---

## ⚙️ Database Tables

```sql
notes              → All notes/inputs
entities           → All entities (Sarah, auth, etc.)
relations          → All relationships (Sarah→leads→auth)
entity_resolution  → Deduplication history
graph_cache        → Computed metrics
```

**Don't edit directly.** Use APIs in `db.py`.

---

## 🎓 5-Stage Pipeline (Important!)

```
1. Input: User adds note
   ↓
2. Extraction: Claude LLM extracts facts
   ↓
3. Resolution: Deduplicate entities (Union-Find)
   ↓
4. Graph: Update knowledge graph
   ↓
5. Algorithms: Compute PageRank, communities
```

Remember this when planning changes!

---

## ✅ Pre-Work Checklist

- [ ] `brahmastra mcp` running in terminal
- [ ] Claude Code configured (claude.json)
- [ ] `pytest tests/` all green
- [ ] You've read at least the README

---

## 🚨 If Something Breaks

1. **MCP not connecting?** Restart Claude Code
2. **Tests failing?** Run `pytest -v` and check error
3. **Database locked?** Delete `backend/data/brahmastra.db`
4. **Import errors?** Run `cd backend && pip install -e .`
5. **Module not found?** Ensure you're in correct directory

---

## 💡 Pro Tips

- **Ask for status:** "What work is stored in Brahmastra?"
- **Review past:** "What changed in extraction.py?"
- **Find patterns:** "What's most important in the graph?"
- **Check health:** "Are there any contradictions?"
- **Plan ahead:** "What should we work on next?"

---

## 🎯 Remember

- **Context persists:** Brahmastra stores everything
- **No context loss:** Between sessions, Claude remembers
- **Self-documenting:** Decisions stored automatically
- **One command:** `brahmastra mcp` and you're set
- **No manual setup:** MCP discovers tools automatically

---

**Print this card. Keep it next to you while working.**

**Questions? Read: `CLAUDE_CODE_HANDOFF.md` for full guide.**
