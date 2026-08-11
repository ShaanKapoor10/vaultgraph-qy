# 🧠 Agent Memory & Persistence: How Brahmastra Maintains Context

Your question is excellent: **"Does AI agents only search, or do they also MAINTAIN and STORE everything like Obsidian?"**

The short answer: **YES - Brahmastra maintains EVERYTHING. It's a persistent knowledge store, not just a search tool.**

---

## 🎯 Quick Comparison

| Aspect | Search Only | Obsidian | Brahmastra |
|--------|------------|----------|-----------|
| **Search/Query** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Maintain data** | ❌ No | ✅ Yes (files) | ✅ Yes (database) |
| **Persistent storage** | ❌ No | ✅ Yes (markdown) | ✅ Yes (PostgreSQL) |
| **Store context** | ❌ Loses it | ⚠️ Manual | ✅ Automatic |
| **Across sessions** | ❌ Lost | ✅ Files persist | ✅ Always available |
| **Version history** | ❌ No | ❌ No | ✅ Yes (all versions) |
| **Automatic dedup** | ❌ No | ❌ No | ✅ Yes |
| **Agent memory** | ❌ Stateless | ⚠️ Manual mgmt | ✅ Automatic |

---

## 🔄 What Agents Can Do with Brahmastra

### Operation 1: ADD & MAINTAIN Knowledge (Persistent)

```typescript
// Agent: "Remember that Sarah leads the auth migration"

// What happens:
const note = await brahmastra.add_note({
  title: "Sarah leads auth",
  content: "Sarah is the leader of the auth migration project"
});

// Returns:
{
  id: "note_42",
  created_at: "2025-06-15T10:30:00Z",
  triples_extracted: [
    {
      subject: "Sarah",
      relation: "leads",
      object: "auth migration",
      confidence: 0.95,
      source_quote: "Sarah is the leader..."
    }
  ]
}

// This is NOW STORED in the database
// It persists forever (or until deleted)
// Every agent can access it forever
```

**Key point:** Unlike a search-only tool, this information is **permanently maintained** in the database.

---

### Operation 2: QUERY & Retrieve Stored Knowledge

```typescript
// Next day, another agent or same agent asks:
// "What does Sarah do?"

const sarah = await brahmastra.search_entities("Sarah");

// Returns stored knowledge:
{
  id: "entity_123",
  name: "Sarah",
  mention_count: 42,  // How many times mentioned
  created_at: "2025-06-10",
  last_updated: "2025-06-15",
  all_relations: [
    {
      type: "leads",
      target: "auth migration",
      confidence: 0.95,
      source_note: "note_42",
      source_quote: "Sarah is the leader...",
      created_at: "2025-06-15"
    },
    {
      type: "reports_to",
      target: "Mei Lin",
      confidence: 0.98,
      source_note: "note_55",
      created_at: "2025-06-12"
    }
  ]
}

// Agent gets COMPLETE context
// Nothing is lost between sessions
```

**Key point:** Knowledge is retrieved exactly as stored, with full history and provenance.

---

### Operation 3: UPDATE & Evolve Knowledge

```typescript
// Later, agent learns: "Sarah now also works on payments"

const update = await brahmastra.add_note({
  title: "Sarah on payments team",
  content: "Sarah has been assigned to the payments team project"
});

// Brahmastra:
// 1. Recognizes "Sarah" is already in the graph
// 2. Uses Union-Find to deduplicate
// 3. Adds new relation: (Sarah, works_on, payments)
// 4. Updates entity "Sarah" with new info
// 5. Stores version history

// Query Sarah again:
const updated_sarah = await brahmastra.search_entities("Sarah");

// Now returns:
{
  mention_count: 45,  // Updated from 42
  all_relations: [
    // ... previous relations still there
    {
      type: "works_on",
      target: "payments",
      confidence: 0.92,
      source_note: "note_63",
      created_at: "2025-06-16"  // New entry
    }
  ]
}

// Agent knows Sarah's knowledge has grown
// Can track evolution over time
```

**Key point:** Knowledge grows and evolves. Nothing is forgotten or overwritten.

---

### Operation 4: MAINTAIN Contradiction Detection

```typescript
// If someone says: "Sarah works in London"
// And later: "Sarah works in San Francisco"

await brahmastra.add_note({
  title: "Sarah location - London",
  content: "Sarah relocated to London office"
});

await brahmastra.add_note({
  title: "Sarah location - SF",
  content: "Sarah moved to San Francisco HQ"
});

// Agent can query:
const conflicts = await brahmastra.get_contradictions();

// Returns:
{
  contradictions: [
    {
      entity: "Sarah",
      relation: "location",
      values: [
        {
          value: "London",
          source_note: "note_70",
          source_quote: "Sarah relocated to London",
          date: "2025-06-15"
        },
        {
          value: "San Francisco",
          source_note: "note_75",
          source_quote: "Sarah moved to San Francisco",
          date: "2025-06-16"
        }
      ]
    }
  ]
}

// Agent MAINTAINS awareness of conflicts
// Can track what's outdated vs current
```

**Key point:** Context is maintained with conflict detection automatically.

---

## 💾 Persistence Model: How Data Flows

### The Complete Flow

```
┌─────────────────────────────────────────────────────────┐
│ Agent Session 1 (Day 1)                                 │
├─────────────────────────────────────────────────────────┤
│ User: "Add Sarah leads auth"                            │
│ ↓                                                        │
│ Agent calls: add_note(...)                              │
│ ↓                                                        │
│ Brahmastra:                                             │
│   1. Extract: (Sarah, leads, auth migration)            │
│   2. Store in PostgreSQL                                │
│   3. Return: {id: note_42, success: true}              │
│ ↓                                                        │
│ Data now PERSISTED in database ✅                        │
└─────────────────────────────────────────────────────────┘
                        ↓
        (Agent 1 session ends, process stops)
                        ↓
┌─────────────────────────────────────────────────────────┐
│ Agent Session 2 (Day 10, Different Agent or Restart)   │
├─────────────────────────────────────────────────────────┤
│ User: "What does Sarah do?"                             │
│ ↓                                                        │
│ Agent calls: search_entities("Sarah")                   │
│ ↓                                                        │
│ Brahmastra queries PostgreSQL                           │
│ ↓                                                        │
│ Returns EVERYTHING stored:                              │
│   - All relations for Sarah                             │
│   - All source quotes                                   │
│   - All dates and versions                              │
│   - All confidence scores                               │
│ ↓                                                        │
│ Agent has COMPLETE context from Day 1 ✅                │
│ Nothing lost, everything maintained                     │
└─────────────────────────────────────────────────────────┘
```

---

## 🗄️ What's Actually Stored in Database

### PostgreSQL Persistence (5 tables)

**1. Notes Table**
```sql
id | title | content | created_at | updated_at | version
```
Every note ever added is stored here permanently.

**2. Entities Table**
```sql
id | name | entity_type | first_seen | last_updated | mention_count
```
Every entity (person, project, etc.) is tracked with full history.

**3. Relations Table**
```sql
id | subject_id | relation | object_id | confidence | source_note_id | created_at
```
Every relationship (Sarah → leads → auth) is maintained with provenance.

**4. Entity Resolution Table** (Union-Find)
```sql
id | canonical_id | mentions | merge_history
```
Tracks all deduplication decisions so context isn't lost.

**5. Graph Cache** (for performance)
```sql
id | graph_snapshot | computed_at | algorithms_run
```
Stores precomputed graph metrics (PageRank, Louvain clusters).

**Everything is versioned:** Every change creates a new record. Nothing is deleted, only marked outdated.

---

## 🔁 Agent Memory Patterns

### Pattern 1: Stateless Agent (Stateless Memoryless)

```typescript
// Each time agent runs, it starts fresh
class StatelessAgent {
  async run(user_query: string) {
    // Agent doesn't remember previous conversations
    // But queries Brahmastra for all context
    
    const context = await brahmastra.search_entities(extract_keywords(user_query));
    const response = await llm.generate({
      query: user_query,
      context: context  // Gets everything from Brahmastra
    });
    return response;
  }
}

// Advantage: Simple, scalable, no session management
// Brahmastra maintains all memory
```

---

### Pattern 2: Stateful Agent (Session Memory)

```typescript
// Agent maintains conversation history in memory
class StatefulAgent {
  private conversation_history = [];
  
  async run(user_query: string) {
    // Add to session memory
    this.conversation_history.push({
      role: "user",
      content: user_query,
      timestamp: Date.now()
    });
    
    // Get context from Brahmastra (persistent)
    const persistent_context = await brahmastra.search_entities(...);
    
    // Get session context (in-memory)
    const session_context = this.get_last_n_messages(5);
    
    // Combine both
    const full_context = {
      persistent: persistent_context,  // From database
      session: session_context          // From memory
    };
    
    const response = await llm.generate({
      query: user_query,
      context: full_context
    });
    
    this.conversation_history.push({
      role: "assistant",
      content: response,
      timestamp: Date.now()
    });
    
    return response;
  }
}

// Advantage: Combines persistent + session memory
// Persistent (Brahmastra) + Session (agent memory)
```

---

### Pattern 3: Multi-Agent Coordination

```typescript
// Multiple agents sharing Brahmastra as shared memory

class CoordinatingAgents {
  // Agent 1
  async agent_1() {
    const task = "Add team structure";
    await brahmastra.add_note({
      title: "Team structure",
      content: "Sarah leads auth, Raj leads API, Alex leads payments"
    });
    // Data is NOW in Brahmastra for all agents
  }
  
  // Agent 2 (can be different instance, different machine)
  async agent_2() {
    const task = "Generate org chart";
    const all_people = await brahmastra.search_entities("type:person");
    const all_relations = await brahmastra.get_graph_stats();
    
    // Agent 2 sees EVERYTHING Agent 1 added
    // Even if Agent 1 is no longer running
    return generate_org_chart(all_people, all_relations);
  }
}

// Advantage: Shared, persistent memory
// Agents coordinate through Brahmastra
// No session loss, no memory loss
```

---

## 📊 Real Workflow: Multi-Day Agent Interaction

### Day 1: Agent Learning

```
Agent: "Remember Sarah leads auth migration"
  → add_note() → Stored in DB ✅

Agent: "Remember Raj leads API redesign"
  → add_note() → Stored in DB ✅

Agent: "Remember Alex leads payments"
  → add_note() → Stored in DB ✅

Agent Session ends
Database now contains: 3 people, 3 roles, 3 projects
```

### Day 2: Agent Querying

```
Agent restarts (same or different agent)

Agent: "Who are our leaders?"
  → search_entities("type:person")
  → Returns: Sarah, Raj, Alex (from database)
  → Agent knows everything from Day 1 ✅

Agent: "Create org chart"
  → get_graph_stats()
  → get_predicted_links()
  → Returns complete structure
  → Agent can reason about organization
```

### Day 3: Agent Learning More

```
Agent: "Add: Sarah now also works on payments"
  → add_note()
  → Brahmastra recognizes "Sarah" (already in DB)
  → Adds new relation
  → Stored in DB ✅

Agent: "Tell me Sarah's responsibilities"
  → search_entities("Sarah")
  → Returns:
    - leads: auth migration (from Day 1)
    - works_on: payments (from Day 3)
  → Agent has complete, growing context ✅
```

### Day 4: Agent Analysis

```
Agent: "Are there any conflicts?"
  → get_contradictions()
  → Returns all conflicts found across all 4 days
  → Agent can track evolution of knowledge

Agent: "What patterns do you see?"
  → get_graph_stats() [uses PageRank]
  → Returns importance ranking
  → Agent discovers: "Sarah is most central (mentioned 15 times)"
```

---

## 🔐 Persistence Guarantees

### What's Guaranteed

```
✅ Everything added is permanently stored
✅ Nothing is lost between sessions
✅ Full version history maintained
✅ All relationships tracked with provenance
✅ Deduplication doesn't lose context
✅ Contradictions detected across all time
✅ Algorithms run on complete history
✅ Agents always see complete picture
```

### How It Differs from Search-Only

```
Search-Only Tool:
  → Finds data that exists somewhere
  → Doesn't store anything
  → Doesn't maintain state
  → Agents are stateless, forgetful

Brahmastra:
  → Finds + stores + maintains everything
  → Database persistence
  → Full state tracking
  → Agents can access complete history
```

---

## 💻 Code Example: Agent with Full Memory

```typescript
import { BrahmastraClient } from "@brahmastra/sdk";

class KnowledgeMainteiningAgent {
  private brahmastra: BrahmastraClient;
  
  constructor() {
    this.brahmastra = new BrahmastraClient({
      url: "http://localhost:8001",
      apiKey: process.env.BRAHMASTRA_API_KEY
    });
  }
  
  // Agent learns new information
  async learn(user_input: string) {
    // Add to persistent knowledge store
    const result = await this.brahmastra.add_note({
      title: `Learning: ${Date.now()}`,
      content: user_input
    });
    
    console.log(`Added ${result.triples_extracted.length} facts`);
    return result;
  }
  
  // Agent recalls what it learned
  async recall(query: string) {
    // Query persistent knowledge store
    const entities = await this.brahmastra.search_entities(query);
    const details = await Promise.all(
      entities.map(e => this.brahmastra.get_entity_details(e.id))
    );
    
    return {
      query,
      found_entities: entities.length,
      details
    };
  }
  
  // Agent analyzes knowledge evolution
  async analyze() {
    const stats = await this.brahmastra.get_graph_stats();
    const contradictions = await this.brahmastra.get_contradictions();
    const predictions = await this.brahmastra.get_predicted_links();
    
    return {
      graph_size: `${stats.nodes} entities, ${stats.edges} relations`,
      conflicts: contradictions.length,
      new_connections: predictions.length,
      communities: stats.clusters.length
    };
  }
  
  // Agent maintains awareness of changes
  async get_changes_since(date: string) {
    // Get all updates since a date
    const stats = await this.brahmastra.get_graph_stats();
    
    // Brahmastra can return timeline
    return {
      date,
      new_facts: stats.total_triples,
      new_entities: stats.total_entities,
      evolution: "All tracked in database"
    };
  }
}

// Usage
const agent = new KnowledgeMainteiningAgent();

// Session 1: Learn
await agent.learn("Sarah leads the auth migration");
await agent.learn("Raj leads the API redesign");

// ... session ends, agent stopped ...

// Session 2: Recall (days later)
const response = await agent.recall("Sarah");
// Returns EVERYTHING about Sarah from Session 1 ✅

// Session 3: Analyze
const analysis = await agent.analyze();
// Returns complete analysis of all knowledge ✅
```

---

## 🎓 Key Differences: Search Tool vs Maintenance System

### Search-Only Tool (like Google)

```
User: "Who is Sarah?"
  ↓
Search service finds data
  ↓
Returns what exists
  ↓
Doesn't store your query
Doesn't maintain state
Next search starts fresh
```

### Brahmastra (Maintenance System)

```
User: "Remember Sarah leads auth"
  ↓
Brahmastra stores: (Sarah, leads, auth)
  ↓
User: "Who does Sarah report to?"
  ↓
Brahmastra returns: All Sarah's relations + context
  ↓
User: "Add Sarah works on payments"
  ↓
Brahmastra deduplicates Sarah + adds relation
  ↓
Now Sarah has: leads auth, reports_to Mei, works_on payments
  ↓
ALL persisted in database
NOTHING lost
Complete history maintained
```

---

## 📝 What Gets Maintained

### Persistent State (Database)

```
✅ Every note added
✅ Every entity recognized
✅ Every relationship extracted
✅ Every confidence score
✅ Every source quote
✅ Every timestamp
✅ Every version
✅ Every deduplication decision
✅ Every contradiction
✅ Every algorithm result
```

### Transient State (Session Memory)

```
⚠️ Conversation history (agent choice)
⚠️ Working memory (agent choice)
⚠️ Temporary calculations (recomputed)
```

Both can be used together.

---

## 🔄 Session Lifecycle

### Session 1 (Agent starts)

```
1. Agent initialized
2. Agent queries Brahmastra for context
3. Brahmastra returns everything stored
4. Agent operates with full context
5. Agent calls add_note() multiple times
6. All added notes stored in DB
7. Session ends
8. Agent process stops
```

### Session 2 (Agent restarts hours/days later)

```
1. Agent initialized (fresh start)
2. Agent queries Brahmastra for context
3. Brahmastra returns EVERYTHING:
   - All notes from Session 1
   - All relationships from Session 1
   - All entities from Session 1
   - All contradictions detected
4. Agent has complete context ✅
5. Agent can add more knowledge
6. All new knowledge stored in DB
```

**Nothing is lost between sessions.**

---

## ✅ Summary

| Question | Answer |
|----------|--------|
| **Does it only search?** | No, it maintains persistent state |
| **Does it store everything?** | Yes, in PostgreSQL database |
| **Can agents access stored data?** | Yes, via MCP/REST API |
| **Does it remember between sessions?** | Yes, always |
| **Can context be lost?** | No, nothing is lost |
| **Is it like Obsidian?** | Yes, but better (auto-extraction, algorithms) |
| **Can agents evolve knowledge?** | Yes, continuously |
| **Can multiple agents share?** | Yes, through same database |
| **Are contradictions tracked?** | Yes, automatically |
| **Is there version history?** | Yes, complete history |

---

## 🎯 Bottom Line

Brahmastra is NOT just a search tool. It's a **persistent knowledge maintenance system for AI agents**.

**Like Obsidian:**
- Stores everything permanently
- Maintains across sessions
- Full history tracking
- Grows over time

**Better than Obsidian:**
- Automatic extraction (no manual linking)
- Structured data (not markdown)
- Deduplication automatic
- Contradictions detected
- Graph algorithms
- Purpose-built for agents

**For agents, this means:**
- Learn once, remember forever
- Access complete context anytime
- Discover patterns in knowledge
- Coordinate through shared memory
- Grow knowledge without losing anything

