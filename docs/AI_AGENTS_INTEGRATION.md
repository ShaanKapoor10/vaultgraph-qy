# 🤖 Using Brahmastra with AI Agents (Replace Obsidian)

**The short answer:** Yes, Brahmastra can replace Obsidian for AI agent workflows, but with a **key philosophical difference**: instead of manually linking notes, Brahmastra **automatically extracts facts and builds a knowledge graph** that AI agents can query and reason over.

---

## 🔄 Brahmastra vs. Obsidian for AI Agents

### Obsidian (Traditional Approach)
```
Human writes note with [[manual links]]
         ↓
Human creates graph structure
         ↓
AI agent reads notes + links
         ↓
AI agent reasons over knowledge
```

**Problems:**
- Manual linking is tedious
- Requires human structure
- AI has to parse markdown + infer relationships
- Doesn't scale well (100+ notes get messy)

### Brahmastra (Automated Approach) ✨
```
Human writes plain notes (no linking)
         ↓
Claude extracts facts automatically → (S, relation, O) triples
         ↓
Brahmastra deduplicates entities (Union-Find)
         ↓
Brahmastra builds graph automatically
         ↓
AI agent queries structured graph via API/MCP
         ↓
AI agent reasons with FULL context + high confidence
```

**Advantages:**
- No manual linking needed
- Graph built automatically
- AI agents get **structured, deduplicated data**
- Scales to 1000+ notes easily
- Confidence scores on facts
- Provenance (where each fact came from)

---

## ✅ Use Brahmastra with AI Agents in 3 Ways

### **Option 1: MCP Server (Easiest for Claude)**

**What it is:** Brahmastra runs as an MCP server that Claude Code can call directly.

**Setup (5 minutes):**

```bash
# Terminal 1: Start Brahmastra
cd backend
source .venv/bin/activate
brahmastra mcp  # Starts MCP server on stdio

# Terminal 2: Add to Claude's config
# ~/.config/claude/claude.json (or equivalent for your IDE)
{
  "mcpServers": {
    "brahmastra": {
      "command": "python",
      "args": ["-m", "brahmastra.mcp_server"],
      "env": {
        "ANTHROPIC_API_KEY": "your-key"
      }
    }
  }
}
```

**In Claude Code/Web:**
```
User: "What's the latest status of the auth migration?"

Claude can now call:
  → run_pipeline() [update knowledge]
  → search_entities("auth migration")
  → get_entity_details("auth migration")
  → get_contradictions() [check for conflicts]
  → predict_links() [find new connections]
```

**MCP Tools Available:**
1. `run_pipeline()` — Extract + resolve + build graph
2. `get_graph_stats()` — Total nodes, edges, clusters
3. `search_entities(query)` — Find entities by name
4. `get_entity_details(entity_id)` — All relations + provenance
5. `get_contradictions()` — Conflicting facts
6. `add_note(title, content)` — Ingest new knowledge
7. `get_predicted_links()` — Suggested connections

**Best for:** Claude integration, one-off queries, minimal setup

---

### **Option 2: REST API (Most Flexible)**

**What it is:** Query Brahmastra via HTTP from any AI agent framework.

**Setup (automatic):**
```bash
cd backend
uvicorn main:app --reload --port 8001
# Server runs on http://localhost:8001
# Docs at http://localhost:8001/docs (FastAPI Swagger UI)
```

**Endpoints:**

```python
# Example: LangChain agent querying Brahmastra

from langchain.agents import initialize_agent, Tool
from langchain.llms import OpenAI
import requests

BASE_URL = "http://localhost:8001"

# Define tools
def query_graph(entity_name: str):
    """Search for entity and return details"""
    resp = requests.get(f"{BASE_URL}/api/entities/search", params={"q": entity_name})
    return resp.json()

def get_entity_relations(entity_id: str):
    """Get all relations for an entity"""
    resp = requests.get(f"{BASE_URL}/api/entities/{entity_id}/relations")
    return resp.json()

def add_note_to_brahmastra(title: str, content: str):
    """Add a note and auto-extract facts"""
    resp = requests.post(f"{BASE_URL}/api/notes", json={
        "title": title,
        "content": content
    })
    return resp.json()

# Register with LangChain
tools = [
    Tool(name="search_entity", func=query_graph, description="Search for entity in graph"),
    Tool(name="get_relations", func=get_entity_relations, description="Get entity relations"),
    Tool(name="add_note", func=add_note_to_brahmastra, description="Add note to knowledge base"),
]

agent = initialize_agent(tools, OpenAI(), agent="zero-shot-react-description", verbose=True)

# Now your agent can use Brahmastra!
result = agent.run("What is Sarah's role and who does she report to?")
```

**Available REST Endpoints:**

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/notes` | Add note |
| GET | `/api/notes` | List all notes |
| GET | `/api/notes/{id}` | Get note details |
| POST | `/api/graph/triples` | Add triples |
| GET | `/api/graph/triples` | List triples |
| GET | `/api/graph` | Get full graph |
| GET | `/api/entities/search` | Search entities |
| GET | `/api/entities/{id}` | Get entity details |
| POST | `/api/pipeline/run` | Run full pipeline |
| GET | `/api/pipeline/stats` | Get pipeline stats |

**Best for:** LangChain, AutoGPT, custom agents, multi-language support

---

### **Option 3: CLI Integration (Batch Processing)**

**What it is:** Call Brahmastra CLI from your agent's scripts/system commands.

**Setup:**

```bash
# Add notes programmatically
brahmastra add-note "Meeting with Sarah" "Sarah leads auth migration. Reports to Mei Lin."

# Run extraction pipeline
brahmastra run --full

# Query results (returns JSON)
brahmastra show graph --json  # Full graph
brahmastra show clusters --json  # Communities
brahmastra search "Sarah"  # Find entity
```

**Example: Agent using CLI**

```python
import subprocess
import json
from typing import Any

class BrahmastraAgent:
    def add_knowledge(self, title: str, content: str):
        """Add note to Brahmastra"""
        subprocess.run([
            "brahmastra", "add-note", title, content
        ])
        subprocess.run(["brahmastra", "run"])
    
    def query(self, entity_name: str) -> dict:
        """Query knowledge base"""
        result = subprocess.run(
            ["brahmastra", "show", "graph", "--json"],
            capture_output=True,
            text=True
        )
        graph = json.loads(result.stdout)
        # Search for entity in graph
        return [n for n in graph["nodes"] if entity_name.lower() in n["name"].lower()]
    
    def get_contradictions(self) -> list:
        """Find conflicting facts"""
        result = subprocess.run(
            ["brahmastra", "show", "contradictions", "--json"],
            capture_output=True,
            text=True
        )
        return json.loads(result.stdout)

agent = BrahmastraAgent()

# Use it
agent.add_knowledge("Sprint Planning", "Sarah owns auth, Raj owns API")
conflicts = agent.get_contradictions()
```

**Best for:** Simple integrations, batch processing, shell scripts

---

## 🎯 Real-World Workflow: AI Agent Using Brahmastra

### Scenario: Knowledge Management Agent

```
┌─────────────────────────────────────────────────────────────────┐
│ User: "What are we doing on auth and who's responsible?"       │
└─────────────────────────────────────────────────────────────────┘
         ↓
┌─ Agent Decision ───────────────────────────────────────────────┐
│  "I need to search Brahmastra for 'auth'"                      │
└────────────────────────────────────────────────────────────────┘
         ↓
┌─ Agent calls MCP tool ─────────────────────────────────────────┐
│  search_entities("auth migration")                             │
│  get_entity_details("auth migration")                          │
│  get_predicted_links()                                         │
└────────────────────────────────────────────────────────────────┘
         ↓
┌─ Brahmastra returns ──────────────────────────────────────────┐
│  {                                                             │
│    "entity": "auth migration",                                │
│    "relations": [                                             │
│      {"type": "owns", "target": "Sarah", "confidence": 0.95} │
│      {"type": "scheduled_for", "date": "March 15"},          │
│      {"type": "depends_on", "target": "API"},                │
│    ],                                                         │
│    "source_quotes": [                                         │
│      "Sarah leads the auth migration" (from note #42),       │
│    ],                                                         │
│    "predicted": [                                             │
│      {"type": "blocks", "target": "frontend", "score": 0.8}  │
│    ]                                                          │
│  }                                                            │
└────────────────────────────────────────────────────────────────┘
         ↓
┌─ Agent formulates response ────────────────────────────────────┐
│ "Auth migration is owned by Sarah (95% confidence), scheduled │
│  for March 15, and depends on the API work. It might block    │
│  frontend work (80% predicted). Sarah reports to Mei Lin."    │
└────────────────────────────────────────────────────────────────┘
```

### Example Code: Multi-Turn Agent

```python
from langchain.agents import initialize_agent, AgentType
from langchain.llms import OpenAI
from langchain.callbacks import StreamingStdOutCallbackHandler
import requests

# Define Brahmastra tools for LangChain
class BrahmastraTools:
    BASE_URL = "http://localhost:8001"
    
    @staticmethod
    def search_entity(query: str) -> str:
        """Search for entities in the knowledge graph"""
        try:
            resp = requests.get(
                f"{BrahmastraTools.BASE_URL}/api/entities/search",
                params={"q": query},
                timeout=5
            )
            data = resp.json()
            return f"Found {len(data.get('results', []))} entities:\n" + \
                   "\n".join([f"- {e['name']} (id: {e['id']})" 
                             for e in data.get('results', [])])
        except Exception as e:
            return f"Error: {str(e)}"
    
    @staticmethod
    def get_entity_details(entity_id: str) -> str:
        """Get full details about an entity"""
        try:
            resp = requests.get(
                f"{BrahmastraTools.BASE_URL}/api/entities/{entity_id}",
                timeout=5
            )
            data = resp.json()
            entity = data.get("entity", {})
            relations = data.get("relations", [])
            
            details = f"Entity: {entity.get('name')}\n"
            details += f"Mentions: {entity.get('mention_count')}\n"
            details += f"Relations:\n"
            for rel in relations:
                details += f"  - {rel['relation']} → {rel['target']} " \
                          f"(confidence: {rel['confidence']:.1%})\n"
                if rel.get('source_quote'):
                    details += f"    Quote: \"{rel['source_quote']}\"\n"
            
            return details
        except Exception as e:
            return f"Error: {str(e)}"
    
    @staticmethod
    def add_note(title: str, content: str) -> str:
        """Add a new note to Brahmastra"""
        try:
            resp = requests.post(
                f"{BrahmastraTools.BASE_URL}/api/notes",
                json={"title": title, "content": content},
                timeout=10
            )
            result = resp.json()
            return f"Note added. Extracted {result.get('triples_count', 0)} facts."
        except Exception as e:
            return f"Error: {str(e)}"
    
    @staticmethod
    def get_contradictions() -> str:
        """Get conflicting facts in the knowledge graph"""
        try:
            resp = requests.get(
                f"{BrahmastraTools.BASE_URL}/api/graph/contradictions",
                timeout=5
            )
            contradictions = resp.json().get("contradictions", [])
            if not contradictions:
                return "No contradictions found."
            
            details = f"Found {len(contradictions)} contradictions:\n"
            for c in contradictions:
                details += f"\n- {c['entity']} has multiple values for " \
                          f"{c['relation']}:\n"
                for value in c['values']:
                    details += f"  • {value['value']} " \
                              f"(from {value['source']})\n"
            return details
        except Exception as e:
            return f"Error: {str(e)}"

# Set up LangChain agent
from langchain.agents import tool
from langchain.schema import AgentAction, AgentFinish

@tool
def search_entity(query: str) -> str:
    """Search for entities in Brahmastra knowledge graph. Use this to find people, projects, or concepts."""
    return BrahmastraTools.search_entity(query)

@tool
def get_entity_details(entity_id: str) -> str:
    """Get detailed information about a specific entity including all relations and source quotes."""
    return BrahmastraTools.get_entity_details(entity_id)

@tool
def add_note(title: str, content: str) -> str:
    """Add a new note to Brahmastra. Will automatically extract facts and update the knowledge graph."""
    return BrahmastraTools.add_note(title, content)

@tool
def check_contradictions() -> str:
    """Check for conflicting facts in the knowledge base (e.g., same person in two places)."""
    return BrahmastraTools.get_contradictions()

# Initialize agent
tools = [search_entity, get_entity_details, add_note, check_contradictions]

llm = OpenAI(temperature=0)
agent = initialize_agent(
    tools,
    llm,
    agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
    verbose=True,
    callbacks=[StreamingStdOutCallbackHandler()]
)

# Multi-turn conversation
print("=" * 60)
print("BRAHMASTRA AI AGENT - KNOWLEDGE MANAGEMENT")
print("=" * 60)

queries = [
    "Who is Sarah and what is she responsible for?",
    "Are there any conflicting facts about people's locations?",
    "Add this note: 'Raj and Sarah are working on API integration together'",
    "What is the relationship between auth migration and API work?",
]

for query in queries:
    print(f"\nUser: {query}")
    print("-" * 60)
    result = agent.run(query)
    print(f"Agent: {result}\n")
```

---

## 🚀 Comparison: Obsidian vs. Brahmastra for AI Agents

| Feature | Obsidian | Brahmastra |
|---------|----------|-----------|
| **Manual linking** | Required | Not needed (automatic) |
| **Scales to 100+ notes** | Gets messy | Stays clean |
| **AI agent integration** | Parse markdown | Query structured API |
| **Fact extraction** | Manual | Automatic (Claude) |
| **Entity deduplication** | Manual | Automatic (Union-Find) |
| **Confidence scores** | Not available | Yes (0.0-1.0) |
| **Contradiction detection** | Manual review | Automatic |
| **Link prediction** | Manual | Automatic (common-neighbor) |
| **Graph visualization** | Yes | Yes (d3-force) |
| **Programmatic access** | No API | REST API + MCP + CLI |
| **For AI agents** | Requires parsing | Native integration |

---

## 🎓 Step-by-Step: Set Up with LangChain

### 1. Start Brahmastra Backend

```bash
cd backend
source .venv/bin/activate
uvicorn main:app --reload --port 8001
```

### 2. Create Python Agent File

```python
# my_agent.py
from langchain.agents import initialize_agent, AgentType, tool
from langchain.llms import OpenAI
import requests

BASE_URL = "http://localhost:8001"

@tool
def search_knowledge(query: str) -> str:
    """Search Brahmastra knowledge graph for entities"""
    resp = requests.get(f"{BASE_URL}/entities/search", params={"q": query})
    results = resp.json().get("results", [])
    return "\n".join([f"- {r['name']}" for r in results[:5]])

@tool
def learn_fact(title: str, content: str) -> str:
    """Add new fact to knowledge base"""
    resp = requests.post(
        f"{BASE_URL}/notes",
        json={"title": title, "content": content}
    )
    return "Learned and processed."

tools = [search_knowledge, learn_fact]
agent = initialize_agent(tools, OpenAI(), agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION)

# Use it
print(agent.run("Who leads the auth migration?"))
```

### 3. Run Agent

```bash
python my_agent.py
```

---

## 📊 Why Brahmastra > Obsidian for AI Agents

1. **Structured Data** — AI agents get facts as (subject, relation, object), not markdown text
2. **Confidence** — Each fact has a confidence score (0.0-1.0)
3. **Provenance** — Every fact links back to source note + quote
4. **Automation** — No manual linking; Claude extracts everything
5. **Scalability** — Works smoothly with 100+ notes; Obsidian gets slow
6. **Query Power** — Search entities, get relations, find patterns
7. **Contradiction Detection** — Automatically finds conflicting facts
8. **Link Prediction** — Suggests new connections AI might have missed
9. **API-First** — REST, MCP, CLI; works with any language/framework
10. **Graph Algorithms** — PageRank (importance), Louvain (communities)

---

## 🔌 Integration Examples by Framework

### With LangChain (Python)
```python
from langchain.agents import initialize_agent, tool
@tool
def query_brahmastra(q: str) -> str:
    # Query your API
    pass
agent = initialize_agent([query_brahmastra], llm)
```

### With LlamaIndex (Python)
```python
from llama_index.core import VectorStoreIndex
# Index Brahmastra graph as documents
docs = fetch_from_brahmastra_api()
index = VectorStoreIndex.from_documents(docs)
```

### With Vercel AI SDK (TypeScript/JavaScript)
```typescript
const tools = {
  search_entity: {
    description: "Search Brahmastra entities",
    parameters: { /* ... */ },
    execute: async (params) => fetch("http://localhost:8001/entities/search", ...)
  }
};
```

### With AutoGPT (Python)
```python
# Add Brahmastra as a memory source in AutoGPT config
MEMORY_PROVIDERS = ["brahmastra"]
# Configure to query via REST API
```

---

## 💡 Tips for Best Results

1. **Keep notes concise** — 100-300 words per note works best
2. **Use consistent entity names** — "Sarah" not "S. Khan" (but Brahmastra handles this!)
3. **Include dates** — Helps with timeline analysis
4. **Run pipeline regularly** — `brahmastra run` after adding notes
5. **Query with context** — "Who reports to Sarah?" gets better results than just "Sarah"
6. **Monitor contradictions** — Check `brahmastra show contradictions` regularly
7. **Use predicted links** — Review suggestions and add links if correct
8. **Set ontology** — Customize relations in `ontology.yaml` for your domain

---

## ✨ Summary: Brahmastra as Obsidian Replacement

**Brahmastra is NOT a 1:1 replacement for Obsidian.**

Instead, it's the **AI-agent-native knowledge base** you should use alongside or instead of Obsidian:

✅ **Use Brahmastra if:**
- You work with AI agents regularly
- You want automatic fact extraction
- You need structured, queryable data
- You have 50+ notes to manage
- You want confidence scores + provenance
- You need API access for programmatic queries

❌ **Use Obsidian if:**
- You prefer manual knowledge organization
- You want bidirectional links you control
- You work primarily with humans reading notes
- You prefer offline-first tools
- You have a small personal vault (<20 notes)

**Best practice:** Use **both**:
- Obsidian for **writing** (comfortable, minimal friction)
- Brahmastra for **AI reasoning** (structured, queryable, patterns)
- Sync notes from Obsidian to Brahmastra (via Notion or direct file import)

---

**Build your AI agent with Brahmastra's knowledge graph as the brain! 🧠**
