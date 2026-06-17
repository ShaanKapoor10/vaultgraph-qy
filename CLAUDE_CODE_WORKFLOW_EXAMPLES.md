# 🎬 Claude Code Workflow Examples

Real-world examples of how Claude Code should work with Brahmastra on this project.

---

## Example 1: Complete Feature Build (With Brahmastra Integration)

### Scenario
You want Claude Code to add a new feature: **"Filter entities by type in graph queries"**

### Session Start

```
You: "I want to add entity type filtering to the graph API"

Claude Code Workflow:

STEP 1: RECALL CONTEXT
  Claude: "Let me check what I know about this project"
  → Calls: search_entities("graph API")
  → Retrieves: Past work on graph.py, API structure, type system
  → Stores in Brahmastra automatically ✅

STEP 2: UNDERSTAND CURRENT STATE
  Claude: "Current graph query endpoints don't filter by type"
  → Opens: backend/brahmastra/routers/graph.py
  → Checks: ontology.yaml for entity types
  → Reads: concept_graph.py for type definitions
  → Stores: "Understanding current graph filtering capabilities" ✅

STEP 3: PLAN FEATURE
  Claude: "Here's my plan:
    1. Add type_filter parameter to /graph/entities endpoint
    2. Filter in query_graph() function
    3. Add tests for filtering
    4. Update dashboard to show type filter UI"
  → Stores: "Planned feature implementation" ✅

STEP 4: IMPLEMENT BACKEND
  You: "Implement the backend changes"
  
  Claude Code:
    a) Opens: routers/graph.py
    b) Adds: @app.get("/graph/entities/{entity_type}")
    c) Opens: concept_graph.py
    d) Updates: query_graph(type_filter: Optional[str] = None)
    e) Runs: pytest tests/test_graph.py
    f) Tests pass ✅
    → Stores: "Implemented type_filter parameter in graph API" ✅

STEP 5: UPDATE FRONTEND
  You: "Update dashboard to use new filter"
  
  Claude Code:
    a) Opens: components/graph-view.tsx
    b) Adds: type selector dropdown
    c) Adds: useEffect to call filtered endpoint
    d) Tests in browser at http://localhost:3000
    → Stores: "Added type filter UI to dashboard" ✅

STEP 6: ADD TESTS
  You: "Add comprehensive tests"
  
  Claude Code:
    a) Opens: tests/test_graph.py
    b) Adds: test_filter_by_entity_type()
    c) Adds: test_filter_with_invalid_type()
    d) Runs: pytest tests/
    e) All 46 tests pass ✅
    → Stores: "Added 2 new tests for type filtering" ✅

STEP 7: COMMIT & DOCUMENT
  You: "Finalize this feature"
  
  Claude Code:
    a) Runs: git add .
    b) Commits: "feat: add entity type filtering to graph queries"
    c) Updates: API documentation
    d) Stores: "Feature complete: entity type filtering" ✅

STEP 8: SESSION SUMMARY
  You: "What did we accomplish?"
  
  Claude: "This session:
    ✅ Added type_filter parameter to /graph/entities
    ✅ Implemented filtering in query_graph()
    ✅ Added type selector to dashboard UI
    ✅ Added 2 new comprehensive tests
    ✅ All tests passing
    ✅ Feature deployed
    
    Next: Could add type-based aggregation stats"
```

### What Brahmastra Captured

In Brahmastra's persistent memory:
- Feature added: type filtering in graph API
- Files modified: routers/graph.py, concept_graph.py, graph-view.tsx
- Tests added: 2 new test cases
- Architecture: Updated endpoint structure
- Decision: Filter implementation uses query_graph() function
- Time: When completed

**Result:** Claude Code has complete, persisted memory of this work. Any session later, Claude can recall exactly what was done and why.

---

## Example 2: Bug Fix with Reproduction

### Scenario
Entity deduplication is not merging similar names correctly.

### Session Flow

```
STEP 1: RETRIEVE CONTEXT
  You: "There's a bug in entity deduplication"
  
  Claude: "Let me recall what we know about this"
  → Searches: "entity deduplication bugs"
  → Finds: Any previous issues with merging
  → Understands: Union-Find implementation
  → Stores: "Investigating deduplication bug" ✅

STEP 2: REPRODUCE
  Claude: "Let me create a test case that reproduces the bug"
  → Opens: tests/test_entity_resolution.py
  → Creates: test case with similar names:
       ["Sarah", "sara", "S. Khan", "Sarah Khan"]
  → Runs: test and confirms it fails
  → Stores: "Bug reproduced: similar names not merging" ✅

STEP 3: DIAGNOSE
  Claude: "Let me trace through the resolution logic"
  → Opens: entity_resolution.py
  → Reviews: Union-Find merge logic
  → Finds: Merge condition is too strict (line 42)
  → Stores: "Root cause: similarity threshold too high" ✅

STEP 4: FIX
  Claude: "Fix the merge condition"
  → Changes: threshold from 0.95 to 0.85 (Jaro-Winkler)
  → Explains: "0.85 better catches similar variations"
  → Runs: Previous failing test
  → Test now passes ✅
  → Stores: "Fixed merge condition threshold" ✅

STEP 5: COMPREHENSIVE TESTING
  Claude: "Verify fix doesn't break anything"
  → Runs: pytest tests/test_entity_resolution.py -v
  → All 12 entity resolution tests pass ✅
  → Runs: Full test suite (44 tests)
  → All tests pass ✅
  → Stores: "Verified: fix works, no regressions" ✅

STEP 6: ADD EDGE CASES
  Claude: "Add more test cases for edge cases"
  → Adds: test_merge_with_spaces()
  → Adds: test_merge_with_special_characters()
  → Adds: test_no_false_positives()
  → All pass ✅
  → Stores: "Added 3 edge case tests" ✅

STEP 7: DOCUMENT & COMMIT
  You: "Finalize the fix"
  
  Claude:
    a) Updates: Code comments with explanation
    b) Runs: git add . && git commit -m "fix: entity deduplication threshold"
    c) Stores: "Bug fixed: threshold adjusted to 0.85" ✅

STEP 8: VERIFY IN ACTION
  Claude: "Test with real data"
  → Adds: test note with similar names
  → Checks: All variations merged correctly
  → Stores: "Verified with real-world test case" ✅
```

### What Brahmastra Knows

After this session, Brahmastra stores:
- Bug: Entity deduplication wasn't merging similar names
- Root cause: Jaro-Winkler threshold was 0.95 (too strict)
- Solution: Changed to 0.85
- Verification: 15 tests pass, including 3 new edge cases
- Files modified: entity_resolution.py, test_entity_resolution.py
- Decision rationale: 0.85 catches "Sarah"/"sara" variations without false positives

**Later Session:** Claude asks "What bugs did we fix?" → Immediately recalls this entire session.

---

## Example 3: Performance Optimization

### Scenario
Graph queries are slow on large datasets.

### Session Flow

```
STEP 1: RECALL PAST WORK
  You: "Our graph queries are slow, let's optimize"
  
  Claude: "Let me check what I know about performance"
  → Searches: "graph query performance"
  → Finds: Any previous optimization attempts
  → Recalls: Graph size and query patterns
  → Stores: "Starting performance investigation" ✅

STEP 2: PROFILE
  Claude: "Let me measure what's slow"
  → Writes: Performance test with 1000 entities
  → Profiles: concept_graph.py functions
  → Finds: compute_centrality() takes 5 seconds
  → Stores: "Performance issue identified" ✅

STEP 3: ANALYZE
  Claude: "Why is compute_centrality slow?"
  → Reviews: Implementation in concept_graph.py
  → Realizes: PageRank recomputes from scratch every time
  → Stores: "Root cause: no caching on PageRank results" ✅

STEP 4: IMPLEMENT CACHING
  Claude: "Add caching to centrality computation"
  → Opens: db.py
  → Adds: cache table "graph_cache"
  → Modifies: compute_centrality() to check/update cache
  → Stores: "Implemented PageRank result caching" ✅

STEP 5: BENCHMARK
  Claude: "Measure improvement"
  → Runs: Same 1000-entity test
  → Before: 5 seconds
  → After: 0.5 seconds ✅
  → Improvement: 10x faster
  → Stores: "Performance improved 10x with caching" ✅

STEP 6: TEST INVALIDATION
  Claude: "Ensure cache invalidates correctly"
  → Adds: test_cache_invalidation_on_new_entity()
  → Adds: test_cache_invalidation_on_merge()
  → Tests: Cache expires after changes
  → All tests pass ✅
  → Stores: "Cache invalidation working correctly" ✅

STEP 7: INTEGRATE
  Claude: "Use cached results in queries"
  → Updates: routers/graph.py to use cache
  → Tests: End-to-end with dashboard
  → Browser shows instant queries ✅
  → Stores: "Cache integrated into API queries" ✅

STEP 8: FINAL VERIFICATION
  Claude: "One more full test suite run"
  → Runs: pytest tests/
  → All 44 tests pass ✅
  → Stores: "Final verification complete" ✅
  
  Summary stored: "Session: performance optimization
    Issue: Graph queries slow (5 sec)
    Cause: No caching on PageRank
    Solution: Added graph_cache table
    Result: 10x faster (0.5 sec)
    Tests: All passing with cache invalidation"
```

### What Brahmastra Captures

Complete record of:
- Original performance metrics
- Root cause analysis
- Implementation approach
- Performance improvement (10x)
- Cache invalidation strategy
- Test coverage added
- Date and session duration
- Code changes made

**Later:** Claude can ask "What performance optimizations have we done?" and get this entire history.

---

## Example 4: Multi-Session Continuity

### Session 1 (Monday)

```
You: "Let's start refactoring extraction.py"

Claude: Stores in Brahmastra:
  - "Starting refactoring of extraction.py"
  - "Goal: Separate fact extraction from prompt engineering"
  - "Completed: Split extract_triples() into 2 functions"
  - "Remaining: Update all callers of new functions"

[Session ends]
```

### Session 2 (Wednesday, 2 days later)

```
You: "Continue the extraction.py refactoring"

Claude: Queries Brahmastra:
  → Finds: "We started refactoring extraction.py"
  → Recalls: "Split extract_triples() into 2 functions"
  → Knows: "Need to update all callers"
  → Sees: Which files call these functions
  
Claude: "Let's pick up where we left off.
  We split extract_triples() on Monday.
  Now we need to update 3 callers in pipeline.py and routers/notes.py"

Continues refactoring with full context! ✅
```

### What This Means

Without Brahmastra:
- "What were we working on?" (you have to remember)
- Loss of context between sessions
- Might duplicate work

With Brahmastra:
- Claude automatically recalls "You were refactoring extraction.py"
- Claude knows exactly what was done and what remains
- Claude picks up seamlessly

---

## Example 5: Discovering Patterns

### Scenario
You want Claude to analyze what it has learned.

### Session

```
You: "What patterns do you see in our work?"

Claude: Queries Brahmastra for all work:
  → get_graph_stats()
  → get_predicted_links()
  → Returns patterns and insights

Claude: "Over our sessions, I've discovered:
  1. Most changes are in entity_resolution.py and concept_graph.py
  2. We've added 12 new tests this month
  3. Performance optimization was a theme (3 sessions)
  4. entity_resolution has highest change frequency
  5. Suggested next focus: improve graph visualization
  
  Files changed most: [list]
  Test coverage trend: [increasing]
  Architecture decisions: [pattern]"

This analysis is automatic! Brahmastra provides data, Claude analyzes.
```

---

## Example 6: Emergency Bug (Different Day, Different Claude Session)

### Scenario
Unexpected bug in production, you need Claude Code to help.

### Session

```
You: "Emergency: graph queries returning wrong results"

Claude: 
  1. Queries Brahmastra: "What recent changes affect graph queries?"
  2. Finds: Last 3 commits affecting concept_graph.py
  3. Reviews: Changes made in past sessions
  4. Recalls: Caching implementation from optimization session
  5. Hypothesis: "Cache not invalidating after merge"
  
  Checks: Recent merges and cache state
  Finds: Bug! Cache not cleared when entities merge
  Fixes: Add invalidation in entity_resolution.py
  Tests: Bug reproduction now passes
  Commits: "hotfix: cache invalidation on entity merge"
  
Store: "Emergency fix completed. Cache invalidation improved."
```

### Power of Persistence

Claude knew:
- ✅ All recent changes affecting queries
- ✅ Caching strategy (from past session)
- ✅ How entities are merged
- ✅ How to reproduce and fix

All because Brahmastra maintained context across sessions.

---

## Key Takeaways

All these workflows share a pattern:

```
Session Start:
  1. Claude queries Brahmastra for context
  2. Claude understands current state
  
During Work:
  3. Claude stores decisions/progress
  4. Context accumulates
  
Session End:
  5. Claude summarizes what was done
  
Next Session:
  6. Claude remembers everything
  7. Picks up seamlessly with full context
```

**That's the power of integrating Claude Code with Brahmastra.**

No context loss. Self-documenting code. Persistent AI assistant.

---

## How to Use These Examples

1. **Read one that matches your task**
2. **Adapt the workflow to your needs**
3. **Use the prompts as templates**
4. **Claude Code handles the Brahmastra storage automatically**

**You just focus on the work. Brahmastra manages the memory.**
