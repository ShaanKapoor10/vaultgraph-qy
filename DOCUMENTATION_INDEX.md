# Brahmastra Documentation Index & Navigation Guide

## Overview

This is your complete guide to understanding and using **Brahmastra: Concept Graph Engine**. We've provided multiple documentation levels so you can find what you need based on your goal.

---

## 📚 Documentation Files

### 1. **QUICK_START.md** (15 min read)
**For:** Anyone wanting to get up and running fast

**Contains:**
- 30-second summary
- Installation steps (5 minutes)
- Running locally (2 minutes)
- Dashboard navigation
- CLI commands reference
- Basic troubleshooting
- Deployment to Vercel

**Read this if:** You want to install and start using Brahmastra immediately.

**Example questions answered:**
- "How do I run this locally?"
- "What are the CLI commands?"
- "How do I deploy to Vercel?"

---

### 2. **README.md** (30 min read)
**For:** Developers wanting overview of all features

**Contains:**
- Complete feature list
- 12-step implementation summary
- Full project structure
- Database schema
- Configuration guide
- Deployment checklist
- Performance benchmarks

**Read this if:** You're evaluating whether Brahmastra fits your needs, or need a comprehensive feature overview.

**Example questions answered:**
- "What can this system do?"
- "What does the database schema look like?"
- "How is this deployed?"

---

### 3. **DETAILED_ARCHITECTURE.md** (1-2 hour deep dive)
**For:** Developers wanting to understand the implementation

**Contains:**
- The big picture (vision, problem, solution)
- 5-stage pipeline with detailed explanations
- Complete code examples for each stage
- Algorithm deep dives (Union-Find, Jaro-Winkler, PageRank, Louvain)
- Data flow through the system
- Component breakdowns (frontend, backend)
- How to use every feature in detail

**Read this if:** You want to understand HOW the system works, modify it, extend it, or learn the algorithms.

**Example questions answered:**
- "How does entity deduplication work?"
- "What does Union-Find do and why?"
- "How are contradictions detected?"
- "Show me complete code for PageRank"
- "What's the data flow from notes to graph?"

---

## 🗺️ Navigation by Goal

### "I want to START USING it RIGHT NOW"
→ **QUICK_START.md**
1. Follow installation (5 min)
2. Run locally
3. See it work
4. Add your own notes

**Time investment:** 15 minutes to working system

---

### "I want to UNDERSTAND what this does"
→ **README.md** → **QUICK_START.md**
1. Read README.md overview
2. Check out QUICK_START for examples
3. Try it locally to see it in action

**Time investment:** 30 minutes

---

### "I want to UNDERSTAND how it WORKS"
→ **DETAILED_ARCHITECTURE.md** (in order):
1. Read "The Big Picture" (5 min)
2. Read "Core Problem & Solution" (5 min)
3. Read each stage of the 5-stage pipeline (20 min)
4. Read the algorithm deep dives (30 min)
5. Read the component breakdown (20 min)

**Time investment:** 1-2 hours

---

### "I want to EXTEND or MODIFY it"
→ **DETAILED_ARCHITECTURE.md** → **Code files**
1. Understand the 5-stage pipeline
2. Find the relevant component (extraction, resolution, graph, etc.)
3. Read the algorithm explanation
4. Look at the code file
5. Modify

**Time investment:** Depends on scope, but you now have full understanding

---

### "I want to ADD A NEW RELATION TYPE"
→ **QUICK_START.md** (see Ontology section) → `ontology.yaml`
1. Add to `ontology.yaml` (10 seconds)
2. The LLM will automatically respect the new relation
3. Graph algorithms will use it

**Time investment:** 5 minutes

---

### "I want to DEPLOY this to production"
→ **QUICK_START.md** (Deployment section) → **README.md** (Deployment Checklist)
1. Connect GitHub to Vercel
2. Set env variables
3. Push to main
4. Vercel auto-deploys both services

**Time investment:** 10 minutes

---

### "I want to INTEGRATE with Claude"
→ **QUICK_START.md** (MCP Server section)
1. Start `brahmastra mcp`
2. Register in Claude Code
3. Use tools to query your graph

**Time investment:** 5 minutes

---

### "I'm getting an ERROR"
→ **QUICK_START.md** (Troubleshooting section)
- Has solutions for the 7 most common issues
- If not there, check GitHub issues

**Time investment:** 5 minutes

---

## 🔍 Documentation by Role

### Data Scientist
1. Read **DETAILED_ARCHITECTURE.md** — Algorithm Deep Dives section
2. Understand Union-Find, Jaro-Winkler, PageRank, Louvain
3. Read component breakdown for entity resolution and concept graph
4. Review the code files

**Resources:**
- `backend/brahmastra/entity_resolution.py` — implementation
- `backend/brahmastra/concept_graph.py` — implementation
- `backend/tests/test_*.py` — unit tests

---

### Full-Stack Developer
1. Read **README.md** for overview
2. Read **DETAILED_ARCHITECTURE.md** — full sections
3. Understand the FastAPI + React architecture
4. Look at frontend and backend code
5. Try modifying something and deploy

**Resources:**
- `frontend/` — React app
- `backend/` — Python FastAPI
- `backend/main.py` — API routes
- `vercel.json` — deployment config

---

### DevOps / Infrastructure
1. Read **QUICK_START.md** — Deployment section
2. Read **README.md** — Deployment Checklist
3. Understand Vercel experimentalServices
4. Set up environment variables
5. Configure monitoring

**Resources:**
- `vercel.json` — service configuration
- `backend/pyproject.toml` — Python dependencies
- `frontend/package.json` — Node dependencies

---

### Product Manager
1. Read **QUICK_START.md** — 30-second summary
2. Read **README.md** — Features section
3. Try it locally using QUICK_START
4. Understand the 12-step implementation

**Resources:**
- See the working dashboard at localhost:3000
- Review the insight tabs (Central Entities, Clusters, etc.)

---

### Student / Learner
1. Start with **README.md** for overview
2. Read **DETAILED_ARCHITECTURE.md** for deep understanding
3. Look at actual code
4. Try modifying and running locally
5. Read the algorithm sections line by line

**Learning path:**
- Union-Find (data structure fundamentals)
- String similarity (Jaro-Winkler, Levenshtein)
- Graph algorithms (PageRank, community detection)
- LLM integration (prompt engineering, JSON validation)
- Full-stack web (FastAPI + React)

---

## 📖 Reading Recommendations by Time Available

### 15 minutes
→ **QUICK_START.md**

### 30 minutes
→ **README.md** + **QUICK_START.md** (skim)

### 1 hour
→ **DETAILED_ARCHITECTURE.md** (skip code examples)

### 2 hours
→ **DETAILED_ARCHITECTURE.md** (full read)
→ **Code files** for components you're interested in

### 4+ hours
→ All documentation
→ All code files
→ Run locally and experiment

---

## 🎯 Key Sections by Topic

### Understanding the Concept
- README.md — The Big Picture
- DETAILED_ARCHITECTURE.md — The Big Picture

### The 5-Stage Pipeline
- DETAILED_ARCHITECTURE.md — "The 5-Stage Pipeline Explained"
- Each stage has detailed explanation + code examples

### Algorithms
- DETAILED_ARCHITECTURE.md — "Algorithm Deep Dives"
- Includes formulas, intuitions, and code

### Frontend
- README.md — Project Structure (frontend section)
- DETAILED_ARCHITECTURE.md — "Detailed Component Breakdown"

### Backend
- README.md — Project Structure (backend section)
- DETAILED_ARCHITECTURE.md — "Detailed Component Breakdown"

### Database
- README.md — Database Schema
- DETAILED_ARCHITECTURE.md — Database Consistency section

### Deployment
- QUICK_START.md — Deployment section
- README.md — Deployment Architecture
- DETAILED_ARCHITECTURE.md — Deployment Architecture

### Configuration
- QUICK_START.md — Configuration section
- README.md — Configuration & Environment

### Troubleshooting
- QUICK_START.md — Troubleshooting table
- README.md — Troubleshooting guide

---

## 🔗 Cross-References

### If you're reading about:

**Entity Resolution**
- See: DETAILED_ARCHITECTURE.md Stage 3
- Code: `backend/brahmastra/entity_resolution.py`
- Tests: `backend/tests/test_entity_resolution.py`

**Extraction**
- See: DETAILED_ARCHITECTURE.md Stage 2
- Code: `backend/brahmastra/extraction.py`
- Tests: `backend/tests/test_extraction.py`

**Graph Algorithms**
- See: DETAILED_ARCHITECTURE.md Stage 5 + Algorithm Deep Dives
- Code: `backend/brahmastra/concept_graph.py`
- Tests: `backend/tests/test_concept_graph.py`

**Dashboard**
- See: README.md Dashboard section
- Code: `frontend/components/dashboard.tsx`
- Logic: `frontend/lib/pipeline.ts`

**CLI**
- See: README.md CLI section + QUICK_START.md CLI Reference
- Code: `backend/brahmastra/cli.py`

**Deployment**
- See: QUICK_START.md Deployment section
- Config: `vercel.json`

---

## 📊 Documentation Statistics

| File | Length | Read Time | For Whom |
|------|--------|-----------|----------|
| QUICK_START.md | 407 lines | 15 min | Everyone (start here) |
| README.md | 350+ lines | 30 min | Feature overview |
| DETAILED_ARCHITECTURE.md | 1974 lines | 1-2 hours | Deep understanding |
| ontology.yaml | 106 lines | 5 min | Understanding relations |
| Code files | 2872 lines | Varies | Implementation |

**Total documentation: ~2,700 lines + 2,900 lines of code with docstrings**

---

## ✅ You Should Read:

1. **Minimum** — QUICK_START.md (get it running)
2. **Basic** — README.md (understand features)
3. **Intermediate** — DETAILED_ARCHITECTURE.md Stages 1-2 (how extraction works)
4. **Advanced** — DETAILED_ARCHITECTURE.md Stages 3-5 + Algorithms (full system)

---

## 🚀 Getting Started Checklist

- [ ] Read QUICK_START.md (15 min)
- [ ] Install using QUICK_START.md (5 min)
- [ ] Run locally and see dashboard (2 min)
- [ ] Add a note using web UI (2 min)
- [ ] Run CLI: `brahmastra run` (1 min)
- [ ] View results: `brahmastra show graph` (1 min)
- [ ] Try CLI: `brahmastra show clusters` (1 min)
- [ ] Read README.md for features (30 min, optional)
- [ ] Deploy to Vercel (10 min, optional)

**Total: ~30 min to working system**

---

## 📞 Need Help?

1. Check QUICK_START.md Troubleshooting section
2. Check README.md Troubleshooting guide
3. Search GitHub issues
4. Read DETAILED_ARCHITECTURE.md for your specific area
5. Check code docstrings
6. Create a GitHub issue with details

---

## 📝 Notes for Contributors

If you're contributing code:
1. Understand DETAILED_ARCHITECTURE.md for your component
2. Follow the patterns in existing code
3. Add docstrings (follow Google format)
4. Add/update tests
5. Update relevant docs
6. Add implementation notes to this index if needed

---

## Version Info

- **Last Updated:** June 2025
- **Brahmastra Version:** 1.0 (12 steps complete)
- **Python:** 3.11+
- **Node.js:** 18+

---

## Quick Links

- **GitHub:** [ShaanKapoor10/vaultgraph-qy](https://github.com/ShaanKapoor10/vaultgraph-qy)
- **Live Demo:** Coming soon to Vercel
- **Issues:** GitHub Issues
- **Contributing:** See CONTRIBUTING.md (coming soon)

---

**Start with QUICK_START.md and go from there! 🚀**
