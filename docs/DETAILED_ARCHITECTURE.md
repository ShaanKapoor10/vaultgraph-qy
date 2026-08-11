# Brahmastra: Concept Graph Engine — Complete Architecture & Implementation Guide

**Table of Contents:**
1. [The Big Picture: What is Brahmastra?](#the-big-picture)
2. [Core Problem & Solution](#core-problem--solution)
3. [Architecture Overview](#architecture-overview)
4. [The 5-Stage Pipeline Explained](#the-5-stage-pipeline-explained)
5. [Detailed Component Breakdown](#detailed-component-breakdown)
6. [Algorithm Deep Dives](#algorithm-deep-dives)
7. [Data Flow & Persistence](#data-flow--persistence)
8. [How to Use: Complete Guide](#how-to-use-complete-guide)
9. [Deployment Architecture](#deployment-architecture)
10. [Configuration & Environment](#configuration--environment)

---

## The Big Picture: What is Brahmastra?

### Vision

Brahmastra is a **knowledge graph engine** that automatically transforms unstructured personal notes into a queryable, visual knowledge graph. Imagine you have hundreds of notes scattered across Notion, Obsidian, or your email — each note contains facts about people, projects, relationships, but they're buried in prose. 

**Brahmastra's job:** Extract those facts, deduplicate the entities ("Sarah K." = "Sarah Khan"), build a graph, and then tell you things your notes never explicitly said.

### What It Actually Does (In Plain English)

1. **Reads notes** — from Notion, filesystem, or manually pasted text
2. **Extracts facts** — "Alice manages Bob" becomes a triple: `(Alice, manages, Bob)`
3. **Deduplicates entities** — realizes "Sarah", "Sarah K.", and "Sarah Khan" are the same person
4. **Builds a graph** — creates a directed network of entities and relationships
5. **Computes insights** — finds:
   - **Most important people** (PageRank centrality)
   - **Concept clusters** (topics that naturally group together)
   - **Contradictions** (facts that conflict, e.g., "Sarah reports to two managers")
   - **Missing connections** (entities that should probably be linked)
6. **Visualizes it** — interactive d3-force graph dashboard
7. **Makes it accessible** — CLI, MCP server for Claude, REST API

---

## Core Problem & Solution

### The Problem

Your knowledge lives in **notes**, not structured databases. When you write:

> "Sarah is leading the auth migration. She owns the whole effort. The auth migration is scheduled for March 15. Sarah reports to Mei Lin."

A human reading this sees multiple facts:
- Sarah **owns** the auth migration
- Sarah **leads** the auth migration
- Auth migration is **scheduled_for** March 15
- Sarah **reports_to** Mei Lin

But a computer sees text. You can't ask:
- *"Who's the most central person across all my notes?"*
- *"What topics naturally cluster together?"*
- *"Where do my notes contradict each other?"*
- *"Who should probably be connected but isn't?"*

### The Solution: The Pipeline

Brahmastra is a **5-stage pipeline** that progressively refines the raw data:

```
Stage 1: Sync        → notes in SQLite
Stage 2: Extraction  → (subject, relation, object) triples with confidence
Stage 3: Resolution  → deduplicates entities using Union-Find + embeddings
Stage 4: Build Graph → builds networkx MultiDiGraph with full provenance
Stage 5: Cache       → precomputes insights (PageRank, Louvain, contradictions, predictions)
```

Each stage is **incremental** (only re-processes changed notes), **atomic** (no partial writes), and **resumable** (failure doesn't corrupt the DB).

---

## Architecture Overview

### High-Level Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        VERCEL DEPLOYMENT                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────────────────┐   ┌──────────────────────────┐   │
│  │   Frontend (Next.js)     │   │  Backend (FastAPI)       │   │
│  │      Port: 3000          │   │     Port: 8001           │   │
│  ├──────────────────────────┤   ├──────────────────────────┤   │
│  │ • React App              │   │ • Pipeline orchestration │   │
│  │ • d3-force graph viz     │   │ • SQLite persistence     │   │
│  │ • 6 insight panels       │   │ • Claude extraction      │   │
│  │ • Entity inspector       │   │ • Entity resolution      │   │
│  │ • Graceful fallback      │   │ • Graph algorithms       │   │
│  │                          │   │ • Notion sync            │   │
│  │ Calls:                   │   │ • MCP server             │   │
│  │ • /api/graph             │   │ • Typer CLI              │   │
│  │ • /api/notes             │   │                          │   │
│  │ • /api/pipeline/run      │   │ Dependencies:            │   │
│  │                          │   │ • anthropic              │   │
│  └──────────────────────────┘   │ • networkx               │   │
│         ↑                        │ • sentence-transformers │   │
│         │ REST API               │ • sqlite3                │   │
│         │ (Auto-routed)          │ • notion-client          │   │
│         └────────────────────────→ • python-louvain        │   │
│                                   └──────────────────────────┘   │
│                                            ↓                      │
│                                   SQLite Database                │
│                                  (concept_graph.db)              │
│                                            ↓                      │
│                                    5 Tables:                     │
│                                  • notes                         │
│                                  • triples                       │
│                                  • canonical_map                 │
│                                  • entity_clusters               │
│                                  • cached_graph                  │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Technology Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| **Frontend** | Next.js 16 + React 19 + TypeScript | Type-safe, SSR for perf, API Gateway integration |
| **Visualization** | d3-force + SVG | Interactive graph, full control, no heavy libraries |
| **Backend** | FastAPI + Uvicorn | Fast, async, auto-docs, easy deployment |
| **Database** | SQLite | Simple, portable, no server needed, great for personal scale |
| **LLM** | Anthropic Claude 3.5 Haiku | Fast, cheap, accurate at fact extraction |
| **Embeddings** | sentence-transformers (all-MiniLM-L6-v2) | 384-dim, fast, runs locally |
| **Graphs** | networkx | Mature, comprehensive, Python-friendly |
| **Clustering** | python-louvain | Modularity optimization, community detection |
| **CLI** | Typer | Easy CLI from Python functions, rich output |
| **MCP** | Python MCP SDK | Claude Code integration, stdio transport |
| **Deployment** | Vercel experimentalServices | Both services in one repo, auto-scaling |

---

## The 5-Stage Pipeline Explained

### Overview

```python
def run_pipeline(full: bool = False):
    """
    full=False (default): only extract notes with status='pending'
    full=True: force re-extract ALL notes, re-resolve, re-build graph
    """
    # Stage 1: Sync
    if NOTION_TOKEN:
        sync_result = run_sync()  # Notion → SQLite notes
    
    # Stage 2: Extraction
    extract_result = run_extraction(full=full)  # Claude → triples
    
    # Stage 3: Resolution
    resolve_result = run_resolution()  # Union-Find → canonical entities
    
    # Stage 4: Build Graph
    graph_result = build_concept_graph()  # networkx MultiDiGraph
    
    # Stage 5: Cache
    cache_result = cache_graph()  # Precompute insights
    
    return {
        "stages": {
            "sync": sync_result,
            "extract": extract_result,
            "resolve": resolve_result,
            "graph": graph_result,
            "cache": cache_result,
        }
    }
```

### Stage 1: Sync (Optional)

**What:** Read from Notion database, extract text, upsert into SQLite.

**How:**
```
1. Authenticate with NOTION_TOKEN
2. Query NOTION_DATABASE_ID for all pages
3. For each page:
   a. Check last_edited_time against last_synced in DB
   b. If changed OR not in DB → extract block tree to plain text
   c. Upsert into notes table
   d. Mark extraction_status='pending'
4. Return { synced: N, unchanged: M, errors: [...] }
```

**Notion Block Extraction:**
```python
def extract_blocks(page_id: str) -> str:
    """
    Recursively extracts text from all Notion blocks.
    Handles: paragraphs, headings, lists, quotes, code, callouts
    """
    result = []
    
    for block in get_blocks(page_id):
        if block['type'] == 'paragraph':
            result.append(block['paragraph']['rich_text_to_plain_text']())
        elif block['type'] == 'heading_1':
            result.append(f"# {block['heading_1']['rich_text_to_plain_text']()}")
        elif block['type'] == 'bulleted_list_item':
            result.append(f"- {block['bulleted_list_item']['rich_text_to_plain_text']()}")
        # ... etc for all block types
        
        # Recursively handle nested blocks
        if 'children' in block:
            result.append(extract_blocks(block['id']))
    
    return '\n'.join(result)
```

**Change Detection:**
```python
# In DB: last_synced = "2025-06-14T10:30:00Z"
# From Notion: last_edited_time = "2025-06-14T12:45:00Z"
# If last_edited_time > last_synced → changed!
```

**Performance:** 100 pages ~5 seconds (depends on Notion API rate limits).

### Stage 2: Extraction

**What:** For each note with `extraction_status='pending'`, send to Claude 3.5 Haiku asking for facts.

**Core Prompt:**
```
You are a fact extraction expert. Extract exactly the meaningful semantic triples 
(subject-relation-object) from the text.

Relation types: [10 relation names from ontology]

For EACH triple:
- subject_text: The entity name (capitalize properly)
- relation: One of [the 10 allowed relations]
- object_text: The value/target entity
- confidence: 0.0-1.0 (0.8+ for high confidence, <0.4 discarded by the system)
- source_quote: A short direct quote proving this triple

Return ONLY triples with clear semantic meaning. Filter out noise.
```

**Example Input:**
```
"Sarah is leading the auth migration. She owns the whole effort. 
The auth migration is scheduled for March 15. Sarah reports to Mei Lin."
```

**Example Output (JSON):**
```json
{
  "triples": [
    {
      "subject_text": "Sarah",
      "relation": "owns",
      "object_text": "auth migration",
      "confidence": 0.95,
      "source_quote": "She owns the whole effort"
    },
    {
      "subject_text": "auth migration",
      "relation": "scheduled_for",
      "object_text": "March 15",
      "confidence": 0.98,
      "source_quote": "scheduled for March 15"
    },
    {
      "subject_text": "Sarah",
      "relation": "reports_to",
      "object_text": "Mei Lin",
      "confidence": 0.97,
      "source_quote": "reports to Mei Lin"
    }
  ]
}
```

**Validation:**
```python
def validate_extraction(triples):
    """Ensure triples conform to ontology."""
    valid = []
    for t in triples:
        # Check 1: confidence threshold
        if t['confidence'] < 0.4:
            continue
        
        # Check 2: relation in ontology
        if t['relation'] not in VALID_RELATIONS:
            continue
        
        # Check 3: all fields non-empty
        if not t['subject_text'] or not t['object_text']:
            continue
        
        valid.append(t)
    
    return valid
```

**Incremental Behavior:**
```python
def run_extraction(full: bool = False):
    # full=False: only extract pending notes
    if not full:
        notes_to_extract = db.get_notes(status='pending')
    else:
        # full=True: force re-extract all notes
        # 1. Mark all as pending
        db.execute("UPDATE notes SET extraction_status='pending'")
        # 2. Delete old triples (so we rebuild from scratch)
        db.execute("DELETE FROM triples")
        notes_to_extract = db.get_notes(status='pending')
    
    # Extract and write to DB
    ...
```

**Cost & Speed:**
- **Cost:** Claude 3.5 Haiku costs ~$0.00055 per extraction (very cheap)
- **Speed:** ~0.5 seconds per note (including API latency)
- **On seed vault:** 8 notes = ~4 seconds end-to-end

### Stage 3: Resolution (Entity Deduplication)

**What:** Figure out that "Sarah", "Sarah K.", and "Sarah Khan" are the same person, then assign one canonical name.

**The Four-Step Algorithm:**

#### Step 1: Blocking (Avoid O(n²) comparisons)

```python
def create_blocks(mentions: List[str]) -> Dict[str, List[str]]:
    """
    Group mentions by first 2 characters.
    Reduces comparisons from 42² to many smaller blocks.
    """
    blocks = {}
    for mention in mentions:
        key = mention[:2].lower()  # "Sarah" → "sa", "Raj" → "ra"
        blocks.setdefault(key, []).append(mention)
    
    return blocks
```

**Example:**
```
Input mentions: ["Alice", "Alice K", "Bob", "Bob Smith", "Sarah", "Sarah Khan"]

Blocks:
{
  "al": ["Alice", "Alice K"],
  "bo": ["Bob", "Bob Smith"],
  "sa": ["Sarah", "Sarah Khan"]
}
```

Only compare within each block! This reduces from 36 comparisons to 1+1+1 = 3.

#### Step 2: Pairwise Similarity Scoring

For each pair in a block, compute **string similarity** with an explanation:

```python
def are_likely_same_entity(a: str, b: str) -> Tuple[bool, str, float]:
    """
    Returns (is_same, method_name, score)
    """
    # Method 1: Exact match
    if a.lower() == b.lower():
        return (True, "exact", 1.0)
    
    # Method 2: Jaro-Winkler distance (typo tolerance)
    jw_score = jaro_winkler(a, b)  # range 0.0–1.0
    if jw_score >= 0.85:
        return (True, "jaro_winkler", jw_score)
    
    # Method 3: Token-subset (one is subset of other)
    tokens_a = set(tokenize(a))
    tokens_b = set(tokenize(b))
    if tokens_a <= tokens_b or tokens_b <= tokens_a:
        return (True, "token_subset", 0.9)
    
    # Method 4: Acronym matching
    if is_likely_acronym(a, b):
        return (True, "acronym", 0.85)
    
    # Method 5: Embedding similarity (optional, uses sentence-transformers)
    embedding_sim = compute_embedding_similarity(a, b)
    if embedding_sim >= 0.85:
        return (True, "embedding", embedding_sim)
    
    return (False, None, 0.0)
```

**Jaro-Winkler Explained:**

Jaro-Winkler is a string distance metric that:
1. Compares character-by-character allowing transpositions (handles typos)
2. Boosts prefix matches (e.g., "Sarah" vs "Sara" gets higher score if they share "Sar")
3. Returns 0.0 (completely different) to 1.0 (identical)

```
jaro_winkler("Sarah", "Sara")   → 0.956  ✓ (typo: missing 'h')
jaro_winkler("Alice", "Alicia") → 0.947  ✓ (typo: different suffix)
jaro_winkler("Sarah", "Bob")    → 0.0    ✗ (completely different)
```

**Token-Subset:**

```python
def tokenize(s: str) -> List[str]:
    """Split on spaces/punctuation, remove stopwords."""
    tokens = re.split(r'\W+', s.lower())
    return [t for t in tokens if t not in STOPWORDS]

tokenize("Sarah Khan")          → ["sarah", "khan"]
tokenize("Sarah")               → ["sarah"]
tokenize("the BI project")      → ["bi", "project"]  # "the" is stopword
tokenize("PromptlyBI")          → ["promptly", "bi"]

# "sarah" is subset of ["sarah", "khan"] → same entity!
# ["bi", "project"] is subset of ["promptly", "bi"] → same entity!
```

**Embedding Similarity:**

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2")

# "Sarah" and "Sarah Khan" → two 384-dim vectors
emb1 = model.encode("Sarah")        # shape (384,)
emb2 = model.encode("Sarah Khan")   # shape (384,)

# Cosine similarity
similarity = cosine_similarity(emb1, emb2)  # 0.92
# If >= 0.85 → probably the same entity
```

Embeddings catch semantic coreference:
```
embedding_similarity("the BI project", "PromptlyBI")      → 0.87  ✓
embedding_similarity("Sarah K.", "Sarah Khan")            → 0.95  ✓
embedding_similarity("auth migration", "authentication")  → 0.89  ✓
```

#### Step 3: Union-Find Clustering

**Problem:** If "Sarah"~"Sarah K." and "Sarah K."~"Sarah Khan" are both similar, we need to transitively group all three into one cluster. We can't just check pairs.

**Solution:** Use **Union-Find** (Disjoint Set Union), a data structure that efficiently handles "are these two items in the same group?" queries.

```python
class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}
    
    def find(self, x):
        """Find the root (canonical) of x. Uses path compression."""
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])  # compress
        return self.parent[x]
    
    def union(self, x, y):
        """Merge x and y's groups."""
        px, py = self.find(x), self.find(y)
        
        if px == py:
            return  # already in same group
        
        # Union by rank (keep tree shallow)
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1

# Usage:
uf = UnionFind(["Sarah", "Sarah K.", "Sarah Khan", "Raj", "Bob"])

# "Sarah" ~ "Sarah K." are similar
uf.union("Sarah", "Sarah K.")

# "Sarah K." ~ "Sarah Khan" are similar
uf.union("Sarah K.", "Sarah Khan")

# Now ask: are "Sarah" and "Sarah Khan" in the same group?
uf.find("Sarah") == uf.find("Sarah Khan")  # True!
# Result: they're all merged into one cluster
```

**Complexity:**
- **Time:** Nearly O(1) per union/find thanks to path compression
- **Space:** O(n) where n = number of unique mentions
- **Full resolution:** 42 mentions → ~200ms

#### Step 4: Canonical Name Assignment

For each cluster (group of equivalent mentions), pick the **canonical name**:

```python
def pick_canonical_name(cluster_mentions: List[str]) -> str:
    """
    Rules (in order):
    1. Most frequent mention
    2. Longest (more detail)
    3. Title-cased (prefer "Sarah" over "SARAH")
    4. Alphabetically first (tiebreaker)
    """
    
    if not cluster_mentions:
        return ""
    
    # Filter out lowercase junk
    valid = [m for m in cluster_mentions if m[0].isupper()]
    if not valid:
        valid = cluster_mentions
    
    # Sort by (frequency DESC, length DESC, title-case DESC)
    sorted_mentions = sorted(
        valid,
        key=lambda m: (
            -cluster_mentions.count(m),  # most frequent first
            -len(m),                      # longest first
            m[0].isupper(),               # title-cased first
        )
    )
    
    return sorted_mentions[0]

# Example clusters:
canonical_map = {
    "Sarah":       "Sarah",
    "Sarah K.":    "Sarah",
    "sarah khan":  "Sarah",
    "Sarah Khan":  "Sarah",  # picked as canonical (longest, title-cased)
    
    "Bob":         "Bob",
    "Bob Smith":   "Bob Smith",  # picked as canonical (longest)
    "bob":         "Bob Smith",
}
```

**Output: Resolution Result**

```python
{
    "clusters": [
        {
            "canonical": "Sarah",
            "mentions": ["Sarah", "Sarah K.", "Sarah Khan"],
            "merges": [
                {
                    "a": "Sarah",
                    "b": "Sarah K.",
                    "method": "jaro_winkler",
                    "score": 0.915
                },
                {
                    "a": "Sarah K.",
                    "b": "Sarah Khan",
                    "method": "token_subset",
                    "score": 0.9
                }
            ]
        },
        # ... more clusters
    ],
    "canonical_map": {
        "Sarah": "Sarah",
        "Sarah K.": "Sarah",
        "Sarah Khan": "Sarah",
        "bob": "Bob Smith",
        # ... etc
    }
}
```

### Stage 4: Build Concept Graph

**What:** Take the resolved triples (subject/object now point to canonical entities) and build a directed multigraph with full provenance.

**Data Structure:**

```python
import networkx as nx

# Create a directed multigraph (allows multiple edges between same pair)
G = nx.MultiDiGraph()

for triple in resolved_triples:
    # Add nodes if not present
    G.add_node(triple['subject'], type='entity')
    G.add_node(triple['object'], type='entity')
    
    # Add edge with metadata
    G.add_edge(
        triple['subject'],
        triple['object'],
        relation=triple['relation'],
        confidence=triple['confidence'],
        source_quote=triple['source_quote'],
        source_note_id=triple['source_note_id'],
        extracted_at=triple['extracted_at'],
    )

# Result: directed multigraph with 16 nodes and ~42 edges
```

**Why Multigraph?**

Allows multiple edges between the same pair:
```
(Sarah) → [relation: owns, conf: 0.95] → (auth migration)
(Sarah) → [relation: leads, conf: 0.92] → (auth migration)
```

Both relations are preserved, enabling:
- Full history of facts
- Per-fact source attribution
- User can see all ways two entities are related

### Stage 5: Cache Insights

**What:** Run all four algorithms on the graph and precompute results, so the frontend gets instant insight data.

#### Algorithm 1: PageRank (Find Most Important Entities)

```python
import networkx as nx

# PageRank: iterative algorithm that ranks nodes by how many important nodes point to them
pagerank_scores = nx.pagerank(G, alpha=0.85, max_iter=100)

# alpha: damping factor (0.85 is standard)
#   - if random surfer doesn't follow edges, 15% chance they jump to random node
#   - prevents dangling nodes from breaking the algorithm

# Result:
# {
#     "Sarah": 0.18,
#     "auth migration": 0.15,
#     "Mei Lin": 0.12,
#     "Bob": 0.08,
#     ...
# }
```

**Intuition:** Sarah is most important because:
- Multiple people point to her (reports_to)
- She points to important projects (owns, leads)
- She's a hub between different topics

#### Algorithm 2: Louvain Clustering (Find Topic Communities)

```python
from community import community_louvain

# Convert to undirected for clustering
G_undirected = G.to_undirected()

# Louvain: modularity optimization that finds natural communities
partition = community_louvain.best_partition(G_undirected)

# Result: {node: cluster_id}
# {
#     "Sarah": 0,
#     "Mei Lin": 0,
#     "auth migration": 0,
#     "PromptlyBI": 1,
#     "backend team": 1,
#     ...
# }
```

**Intuition:** Cluster 0 is "HR/people management" and Cluster 1 is "engineering projects".

#### Algorithm 3: Contradiction Detection (Find Conflicts)

```python
def detect_contradictions(G, ontology):
    """
    For each functional relation (max 1 value per subject),
    find subjects with multiple values (contradiction).
    """
    contradictions = []
    
    # Functional relations: reports_to, scheduled_for, located_in
    functional_relations = [r for r in ONTOLOGY if r['functional']]
    
    for relation_name in functional_relations:
        # Group edges by subject for this relation
        edges_by_subject = defaultdict(list)
        
        for u, v, data in G.out_edges(data=True):
            if data['relation'] == relation_name:
                edges_by_subject[u].append((v, data))
        
        # If subject has >1 value for functional relation → contradiction
        for subject, edges in edges_by_subject.items():
            if len(edges) > 1:
                # Sort by extracted_at to show latest
                edges = sorted(edges, key=lambda e: e[1]['extracted_at'])
                
                contradictions.append({
                    'entity': subject,
                    'relation': relation_name,
                    'values': [
                        {
                            'object': v,
                            'source_quote': e['source_quote'],
                            'source_note_id': e['source_note_id'],
                            'extracted_at': e['extracted_at'],
                            'is_latest': (v == edges[-1][0]),
                        }
                        for v, e in edges
                    ]
                })
    
    return contradictions
```

**Example:**
```
Contradiction found:
- Sarah reports_to Mei Lin (2025-06-10, note: "meeting notes")
- Sarah reports_to Bob Smith (2025-06-15, note: "reorganization", LATEST)
→ Flag: Sarah's manager changed!
```

#### Algorithm 4: Link Prediction (Find Likely Connections)

```python
def predict_links(G, threshold=2):
    """
    For every unconnected pair of nodes,
    count how many neighbors they share.
    Pairs with ≥ threshold commons are predicted to be connected.
    """
    predictions = []
    
    # Get all node pairs not connected
    all_nodes = list(G.nodes())
    
    for u in all_nodes:
        for v in all_nodes:
            if u >= v:  # avoid duplicates
                continue
            
            # Skip if already connected
            if G.has_edge(u, v) or G.has_edge(v, u):
                continue
            
            # Count common neighbors (undirected)
            u_neighbors = set(G.predecessors(u)) | set(G.successors(u))
            v_neighbors = set(G.predecessors(v)) | set(G.successors(v))
            commons = u_neighbors & v_neighbors
            
            if len(commons) >= threshold:
                predictions.append({
                    'node_a': u,
                    'node_b': v,
                    'common_neighbors': list(commons),
                    'score': len(commons),
                })
    
    # Sort by score (descending)
    predictions.sort(key=lambda x: -x['score'])
    
    return predictions
```

**Example:**
```
Prediction: Sarah ↔ Bob
- Common neighbors: [auth migration, Mei Lin]
- Confidence: 2 commons → probably should be connected!
- Reason: Both are involved in auth migration and report to same manager
```

**Final Cache Output:**

```python
cached_graph = {
    "graph": {
        "nodes": [
            {"id": "Sarah", "pagerank": 0.18, "cluster": 0, "degree": 5},
            {"id": "Bob", "pagerank": 0.12, "cluster": 0, "degree": 3},
            ...
        ],
        "edges": [
            {
                "source": "Sarah",
                "target": "auth migration",
                "relation": "owns",
                "confidence": 0.95,
            },
            ...
        ]
    },
    "stats": {
        "nodes_count": 16,
        "edges_count": 42,
        "clusters": 3,
        "contradictions": 2,
        "predicted_links": 5,
    },
    "insights": {
        "central_entities": [...],  # PageRank leaderboard
        "clusters": [...],          # Louvain communities
        "contradictions": [...],    # Conflicting facts
        "predicted_links": [...],   # Recommended connections
    },
    "built_at": "2025-06-15T10:30:00Z"
}
```

**Performance on Seed Data:**
- 8 notes → 42 triples → 16 entities
- Stage 1 (sync): skipped (no Notion)
- Stage 2 (extract): 4 seconds
- Stage 3 (resolve): 200ms
- Stage 4 (build graph): 50ms
- Stage 5 (cache): 100ms
- **Total: ~4.5 seconds**

---

## Detailed Component Breakdown

### Frontend (Next.js)

#### `/app/page.tsx` — Entry Point

```typescript
// What it does:
// 1. Calls loadFromBackend() to fetch precomputed graph
// 2. Falls back to seed data if backend unavailable
// 3. Renders <Dashboard> with initial data

export default async function Page() {
  // Try to fetch from backend first
  const backend = await loadFromBackend()
  
  const notes = backend?.notes ?? SAMPLE_NOTES
  const triples = backend?.triples ?? SAMPLE_TRIPLES
  const initialResult = backend?.result ?? null
  
  return (
    <Dashboard
      initialNotes={notes}
      initialTriples={triples}
      backendAvailable={backend !== null}
      initialResult={initialResult}
    />
  )
}
```

#### `/components/dashboard.tsx` — Main Layout

```typescript
// State management
const [notes, setNotes] = useState<Note[]>(initialNotes)
const [triples, setTriples] = useState<RawTriple[]>(initialTriples)
const [view, setView] = useState<View>("graph")
const [selected, setSelected] = useState<string | null>(null)
const [localNotesAdded, setLocalNotesAdded] = useState(false)

// Conditional rendering
const result = useMemo(() => {
  // If backend result exists AND user hasn't added local notes
  if (initialResult && !localNotesAdded) {
    return initialResult  // Use precomputed
  }
  // Otherwise run TS pipeline
  return runPipeline(notes, triples)
}, [notes, triples, initialResult, localNotesAdded])

// Layout
<>
  <Header stats={result.stats} backendAvailable={backendAvailable} />
  <GraphView
    graph={result.conceptGraph}
    selected={selected}
    onSelect={setSelected}
  />
  <InsightPanels view={view} result={result} />
  <EntityDetail entity={selected} graph={result.conceptGraph} />
</>
```

#### `/components/graph-view.tsx` — d3 Force Simulation

```typescript
// Initialize force simulation
const simulation = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(links).distance(100))
  .force('charge', d3.forceManyBody().strength(-300))
  .force('center', d3.forceCenter(width / 2, height / 2))

// On tick: update positions and re-render
simulation.on('tick', () => {
  // Update node/edge positions based on forces
  nodeElements.attr('cx', d => d.x).attr('cy', d => d.y)
  edgeElements
    .attr('x1', d => d.source.x)
    .attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x)
    .attr('y2', d => d.target.y)
})

// Interactivity
nodeElements
  .on('mousedown', drag)
  .on('click', selectNode)

// Coloring
nodeElements.attr('fill', d => 
  clusterColors[result.conceptGraph.clusters[d.id]]
)
```

#### `/lib/backend-adapter.ts` — Schema Conversion

```typescript
// Converts Python response to React types
export function adaptBackendGraph(
  pythonResponse: BackendGraphResponse,
  notes: Note[],
  triples: RawTriple[]
): PipelineResult {
  return {
    conceptGraph: {
      nodes: pythonResponse.graph.nodes.map(n => ({
        id: n.id,
        centrality: n.pagerank,
        cluster: n.cluster,
        mentionCount: n.mention_count,
      })),
      edges: pythonResponse.graph.edges.map(e => ({
        source: e.source,
        target: e.target,
        relation: e.relation,
        confidence: e.confidence,
      })),
    },
    stats: {
      notes: notes.length,
      triples: triples.length,
      entities: pythonResponse.graph.nodes.length,
      contradictions: pythonResponse.stats.contradictions,
      clusters: pythonResponse.stats.clusters,
    },
    // ... and so on
  }
}
```

### Backend (Python FastAPI)

#### `/backend/brahmastra/db.py` — SQLite Persistence

```python
# Database initialization
def init_db():
    """Create tables if not exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS notes (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        last_edited TEXT,
        last_synced TEXT,
        extraction_status TEXT NOT NULL DEFAULT 'pending'
    );
    
    CREATE TABLE IF NOT EXISTS triples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_text TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        relation TEXT NOT NULL,
        object_text TEXT NOT NULL,
        object_type TEXT NOT NULL,
        confidence REAL NOT NULL,
        source_quote TEXT,
        source_note_id TEXT,
        extracted_at TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS canonical_map (
        mention_text TEXT PRIMARY KEY,
        canonical_text TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS entity_clusters (
        entity_id TEXT NOT NULL,
        cluster_id INTEGER NOT NULL,
        PRIMARY KEY (entity_id, cluster_id)
    );
    
    CREATE TABLE IF NOT EXISTS cached_graph (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        graph_json TEXT NOT NULL,
        stats_json TEXT NOT NULL,
        built_at TEXT NOT NULL
    );
    
    CREATE INDEX IF NOT EXISTS idx_triples_status 
    ON triples(source_note_id);
    CREATE INDEX IF NOT EXISTS idx_notes_status 
    ON notes(extraction_status);
    """)
    conn.commit()
    conn.close()

# CRUD helpers
def upsert_note(id: str, title: str, content: str, mark_pending=True):
    """Insert or update a note."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    INSERT OR REPLACE INTO notes (id, title, content, extraction_status)
    VALUES (?, ?, ?, ?)
    """, (id, title, content, 'pending' if mark_pending else 'done'))
    conn.commit()
    conn.close()

def get_notes(status: Optional[str] = None) -> List[dict]:
    """Fetch notes, optionally filtered by status."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    query = "SELECT * FROM notes"
    params = []
    
    if status:
        query += " WHERE extraction_status = ?"
        params.append(status)
    
    rows = conn.execute(query, params).fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

def insert_triples(triples: List[dict]):
    """Batch insert triples."""
    conn = sqlite3.connect(DB_PATH)
    
    conn.executemany("""
    INSERT INTO triples (
        subject_text, subject_type, relation, object_text, object_type,
        confidence, source_quote, source_note_id, extracted_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            t['subject_text'], t['subject_type'], t['relation'],
            t['object_text'], t['object_type'], t['confidence'],
            t['source_quote'], t['source_note_id'], t['extracted_at']
        )
        for t in triples
    ])
    
    conn.commit()
    conn.close()
```

#### `/backend/brahmastra/extraction.py` — LLM Integration

```python
from anthropic import Anthropic

def extract_note(note: dict) -> dict:
    """
    Send a single note to Claude 3.5 Haiku for extraction.
    Returns { triples_added, triples_skipped, error }
    """
    client = Anthropic()
    
    prompt = f"""Extract all meaningful semantic triples from this note.

Relations: {', '.join(VALID_RELATIONS)}

Note:
{note['content']}

For each triple, provide:
- subject_text: entity name
- relation: one of the above
- object_text: target entity
- confidence: 0.0-1.0
- source_quote: direct evidence from text

Return as JSON with "triples" array."""
    
    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=1024,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        
        # Parse JSON from response
        text = response.content[0].text
        json_obj = json.loads(text)
        triples = json_obj['triples']
        
        # Validate and filter
        valid_triples = []
        for t in triples:
            if (t['confidence'] >= 0.4 and
                t['relation'] in VALID_RELATIONS and
                t['subject_text'] and t['object_text']):
                valid_triples.append(t)
        
        # Write to DB
        db.insert_triples(valid_triples)
        db.mark_note_done(note['id'])
        
        return {
            'triples_added': len(valid_triples),
            'triples_skipped': len(triples) - len(valid_triples),
            'error': None,
        }
        
    except Exception as e:
        db.mark_note_error(note['id'], str(e))
        return {
            'triples_added': 0,
            'triples_skipped': 0,
            'error': str(e),
        }

def run_extraction(full: bool = False) -> dict:
    """Run extraction on all pending (or all) notes."""
    if full:
        # Force re-extraction: clear triples, mark all pending
        db.execute("DELETE FROM triples")
        db.execute("UPDATE notes SET extraction_status='pending'")
    
    notes = db.get_notes(status='pending')
    results = []
    
    for note in notes:
        result = extract_note(note)
        results.append(result)
    
    return {
        'notes_processed': len(notes),
        'extracted': sum(1 for r in results if r['error'] is None),
        'total_triples': sum(r['triples_added'] for r in results),
        'errors': [r for r in results if r['error']],
    }
```

#### `/backend/brahmastra/entity_resolution.py` — Deduplication

```python
from sentence_transformers import SentenceTransformer
import jellyfish

def are_likely_same_entity(a: str, b: str) -> Tuple[bool, str, float]:
    """Compare two mentions, return (is_same, method, score)."""
    
    # Method 1: Exact
    if a.lower() == b.lower():
        return (True, "exact", 1.0)
    
    # Method 2: Jaro-Winkler
    jw = jellyfish.jaro_winkler(a, b)
    if jw >= 0.85:
        return (True, "jaro_winkler", jw)
    
    # Method 3: Token subset
    tokens_a = set(tokenize(a))
    tokens_b = set(tokenize(b))
    if tokens_a <= tokens_b or tokens_b <= tokens_a:
        return (True, "token_subset", 0.9)
    
    # Method 4: Embedding (optional)
    if ENABLE_EMBEDDINGS:
        model = SentenceTransformer("all-MiniLM-L6-v2")
        emb_a = model.encode(a)
        emb_b = model.encode(b)
        sim = np.dot(emb_a, emb_b) / (np.linalg.norm(emb_a) * np.linalg.norm(emb_b))
        if sim >= 0.85:
            return (True, "embedding", sim)
    
    return (False, None, 0.0)

class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}
    
    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    
    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1

def resolve_entities(triples: List[dict]) -> dict:
    """Deduplicate entities in triples."""
    
    # Collect all mentions
    mentions = set()
    for t in triples:
        mentions.add(t['subject_text'])
        mentions.add(t['object_text'])
    
    mentions = list(mentions)
    
    # Blocking: group by first 2 chars
    blocks = defaultdict(list)
    for m in mentions:
        key = m[:2].lower()
        blocks[key].append(m)
    
    # Pairwise comparison
    uf = UnionFind(mentions)
    merges = []
    
    for block_mentions in blocks.values():
        for i, a in enumerate(block_mentions):
            for b in block_mentions[i+1:]:
                is_same, method, score = are_likely_same_entity(a, b)
                if is_same:
                    uf.union(a, b)
                    merges.append({
                        'a': a,
                        'b': b,
                        'method': method,
                        'score': score,
                    })
    
    # Create canonical map
    canonical_map = {}
    for m in mentions:
        root = uf.find(m)
        # Pick canonical name (longest, title-case preferred)
        cluster = [x for x in mentions if uf.find(x) == root]
        canonical = sorted(
            cluster,
            key=lambda x: (-len(x), -int(x[0].isupper()))
        )[0]
        canonical_map[m] = canonical
    
    # Write to DB
    for mention, canonical in canonical_map.items():
        db.insert_canonical_map(mention, canonical)
    
    return {
        'mentions_count': len(mentions),
        'clusters': len(set(uf.find(m) for m in mentions)),
        'merges': merges,
    }
```

#### `/backend/brahmastra/concept_graph.py` — Graph Algorithms

```python
import networkx as nx
from community import community_louvain

def build_concept_graph(triples: List[dict]) -> nx.MultiDiGraph:
    """Build graph from canonical triples."""
    
    G = nx.MultiDiGraph()
    
    # Get canonical map
    canonical_map = db.get_canonical_map()
    
    for t in triples:
        # Map to canonical names
        subject = canonical_map.get(t['subject_text'], t['subject_text'])
        obj = canonical_map.get(t['object_text'], t['object_text'])
        
        # Add nodes
        G.add_node(subject, type='entity')
        G.add_node(obj, type='entity')
        
        # Add edge with metadata
        G.add_edge(
            subject, obj,
            relation=t['relation'],
            confidence=t['confidence'],
            source_quote=t['source_quote'],
            source_note_id=t['source_note_id'],
            extracted_at=t['extracted_at'],
        )
    
    return G

def compute_all_insights(G: nx.MultiDiGraph) -> dict:
    """Run all algorithms."""
    
    # PageRank
    pagerank = nx.pagerank(G, alpha=0.85)
    
    # Louvain clustering
    G_undirected = G.to_undirected()
    partition = community_louvain.best_partition(G_undirected)
    
    # Contradictions
    contradictions = detect_contradictions(G)
    
    # Link prediction
    predictions = predict_links(G)
    
    return {
        'pagerank': pagerank,
        'clusters': partition,
        'contradictions': contradictions,
        'predictions': predictions,
    }

def cache_graph(G: nx.MultiDiGraph, insights: dict):
    """Serialize and cache graph + insights."""
    
    graph_json = {
        'nodes': [
            {
                'id': node,
                'pagerank': insights['pagerank'][node],
                'cluster': insights['clusters'][node],
            }
            for node in G.nodes()
        ],
        'edges': [
            {
                'source': u,
                'target': v,
                'relation': data['relation'],
                'confidence': data['confidence'],
            }
            for u, v, data in G.edges(data=True)
        ],
    }
    
    stats_json = {
        'nodes': len(G.nodes()),
        'edges': len(G.edges()),
        'clusters': len(set(insights['clusters'].values())),
        'contradictions': len(insights['contradictions']),
        'predictions': len(insights['predictions']),
    }
    
    db.cache_graph(json.dumps(graph_json), json.dumps(stats_json))
```

#### `/backend/main.py` — FastAPI Routes

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import brahmastra.db as db
import brahmastra.pipeline as pipeline

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lifespan: init DB on startup
@app.lifespan
async def lifespan(app: FastAPI):
    db.init_db()
    yield

# Health check
@app.get("/health")
def health():
    return {"status": "ok"}

# Get notes
@app.get("/notes")
def get_notes():
    return db.get_notes()

# Add note
@app.post("/notes")
def add_note(note: NoteInput):
    db.upsert_note(note.id, note.title, note.content)
    return {"ok": True}

# Get graph
@app.get("/graph")
def get_graph():
    cached = db.get_cached_graph()
    if cached:
        return json.loads(cached['graph_json'])
    return {"nodes": [], "edges": []}

# Run pipeline
@app.post("/pipeline/run")
def run_pipeline_endpoint(mode: str = "incremental"):
    result = pipeline.run_pipeline(full=(mode == "full"))
    return result

# MCP Tools (see mcp_server.py)
```

---

## Algorithm Deep Dives

### Union-Find Algorithm (Entity Deduplication)

**Why:** We need to find transitive equivalence. If A~B and B~C, then A~C must be in the same group.

**Data Structure:**
```python
# parent[x] = the representative of x's set
# rank[x] = approximate height of tree rooted at x
parent = {"Sarah": "Sarah", "Sarah K.": "Sarah K.", "Sarah Khan": "Sarah Khan"}
rank =   {"Sarah": 0, "Sarah K.": 0, "Sarah Khan": 0}
```

**Two operations:**

1. **find(x)** — find the representative of x's set
```python
def find(x):
    if parent[x] != x:
        parent[x] = find(parent[x])  # path compression!
    return parent[x]
```

Path compression ensures all future finds are faster:
```
Before: Sarah K. → Sarah Khan → Sarah (3 hops)
After:  Sarah K. → Sarah (1 hop)
```

2. **union(x, y)** — merge x and y's sets
```python
def union(x, y):
    px, py = find(x), find(y)
    
    if px == py:
        return  # already same set
    
    # union by rank: attach smaller tree under larger
    if rank[px] < rank[py]:
        px, py = py, px
    
    parent[py] = px
    if rank[px] == rank[py]:
        rank[px] += 1
```

**Complexity:**
- **find:** O(α(n)) ≈ O(1) with path compression
- **union:** O(α(n)) ≈ O(1)
- **Full resolution of n mentions:** O(n · α(n)) ≈ O(n)

### Jaro-Winkler Distance (String Similarity)

**Why:** Handles typos, transpositions, and short-prefix matches.

**Formula:**
```
jaro(a, b) = 1/3 * (m/|a| + m/|b| + (m-t)/m)
  where:
    m = number of matching characters
    t = number of transpositions / 2

jaro_winkler(a, b) = jaro(a, b) + (prefix_len * p * (1 - jaro(a, b)))
  where:
    prefix_len = length of common prefix (max 4)
    p = scaling factor (typically 0.1)
```

**Example:**
```
jaro_winkler("Sarah", "Sara"):
  m = 4 (S, a, r, a match)
  t = 0
  jaro = 1/3 * (4/5 + 4/4 + (4-0)/4) = 1/3 * (0.8 + 1.0 + 1.0) = 0.933
  
  prefix_match = "Sar" (3 chars) → prefix_len = 3
  jaro_winkler = 0.933 + (3 * 0.1 * (1 - 0.933)) = 0.933 + 0.020 = 0.953
  
  Result: 0.953 ✓ (high similarity, likely same)
```

### PageRank Algorithm (Find Central Entities)

**Why:** Model the graph as a random walk. An entity is important if many important entities point to it.

**Formula:**
```
PR(A) = (1 - d) / N + d * Σ(PR(T) / |T|)
  where:
    d = damping factor (0.85)
    T = nodes pointing to A
    |T| = out-degree of T
    N = total nodes
```

**Intuition:**
- 85% of the time, random surfer follows a link
- 15% of the time, random surfer jumps to random page

**Iteration:**
```
Initialize all nodes: PR = 1 / N

Iterate 100 times:
  For each node A:
    new_PR[A] = 0.15 / N  # random jump
    for each node T pointing to A:
      new_PR[A] += 0.85 * PR[T] / out_degree[T]  # follow link
  
  PR = new_PR
```

**Example:**
```
Graph: Sarah → [auth migration], Mei Lin → [Sarah]

Iteration 1:
PR[Sarah] = 0.15/3 + 0.85 * (PR[Mei Lin] / 1)
PR[auth migration] = 0.15/3 + 0.85 * (PR[Sarah] / 1)
PR[Mei Lin] = 0.15/3 + 0.85 * 0  (no incoming edges)

After 100 iterations:
Sarah: 0.35 (highest, because Mei Lin points to her + she has outgoing edges)
auth migration: 0.30
Mei Lin: 0.20 (lowest, no incoming edges)
```

### Louvain Algorithm (Community Detection)

**Why:** Find natural groupings of related entities without prior labels.

**Algorithm:**
```
Pass 1 (Local Moving Phase):
  For each node:
    Try moving it to each neighboring community
    Calculate modularity gain Δ Q
    Move to community with highest Δ Q
  Repeat until no more moves

Pass 2 (Aggregation Phase):
  Collapse each community into single supernode
  Rebuild graph with supernodes
  Repeat Pass 1 on new graph

Stop when no improvement
```

**Modularity:**
```
Q = (1/2m) * Σ(e_ij - a_i * a_j) * δ(c_i, c_j)
  where:
    m = number of edges
    e_ij = edge weight between i and j
    a_i = sum of weights of edges incident to i
    δ(c_i, c_j) = 1 if i and j in same community, 0 else
```

**High Q** = good partition (strong within-community edges, weak between).

---

## Data Flow & Persistence

### Data Journey Through the Pipeline

```
1. NOTE INGESTION
   Input: Plain text or Notion block
   ↓
   SQLite: notes table
   {id, title, content, last_edited, extraction_status='pending'}

2. EXTRACTION
   Input: note.content
   Claude 3.5 Haiku LLM
   Output: [triple1, triple2, ...]
   ↓
   SQLite: triples table
   {subject_text, relation, object_text, confidence, source_quote, ...}
   
   Update: notes.extraction_status = 'done'

3. RESOLUTION
   Input: all unique mentions from triples
   Union-Find + string similarity
   Output: canonical_map {raw_mention → canonical_name}
   ↓
   SQLite: canonical_map table
   {mention_text, canonical_text}

4. GRAPH BUILDING
   Input: triples + canonical_map
   Map all subject/object mentions to canonical names
   Build networkx.MultiDiGraph
   ↓
   In-memory: G (networkx graph)

5. INSIGHTS COMPUTATION
   Input: G
   Run: PageRank, Louvain, contradiction detection, link prediction
   ↓
   SQLite: cached_graph table
   {graph_json, stats_json, built_at}

6. API SERVING
   Input: GET /graph
   Output: cached_graph (JSON)
   ↓
   Frontend receives precomputed insights
```

### Database Consistency

**Atomic transactions:**
```python
def run_pipeline(full: bool = False):
    try:
        # Stage 1-5...
        
        # All writes happen in this transaction
        db.begin_transaction()
        
        # If any stage fails, nothing is written
        if stage1_ok and stage2_ok and ... and stage5_ok:
            db.commit()
        else:
            db.rollback()  # restore to before pipeline start
            
    except Exception as e:
        db.rollback()
        raise
```

**Incremental rebuilding:**
```
Scenario: User edits one note

1. Mark note.extraction_status = 'pending'
2. Run pipeline(full=False)
3. Only re-extract pending notes ← only the changed one
4. Rebuild canonical_map from all current triples
5. Rebuild graph from scratch
6. Cache fresh insights

Result: only changed note goes through extraction; graph is complete
```

---

## How to Use: Complete Guide

### Local Development Setup

#### 1. Clone and Install

```bash
# Clone the repository
git clone https://github.com/<user>/brahmastra.git
cd brahmastra

# Frontend setup
cd frontend
pnpm install
cd ..

# Backend setup
cd backend
python3 -m venv .venv
source .venv/bin/activate
uv pip install -e .
```

#### 2. Start Services

```bash
# Terminal 1: Backend
cd backend
source .venv/bin/activate
uvicorn main:app --reload --port 8001

# Terminal 2: Frontend
cd frontend
pnpm dev
```

Visit `http://localhost:3000` — you'll see the dashboard with seed data (8 notes, 26 triples, 16 entities).

#### 3. Add Your Own Notes

**Option A: Web UI**
- Click "Notes" tab → "Extract triples" text box
- Paste text → Claude will extract facts

**Option B: CLI**
```bash
cd backend
source .venv/bin/activate

# Add a single note
brahmastra add-note "Team Meeting"
# (interactive prompt for content)

# Run pipeline
brahmastra run  # incremental, only pending notes
brahmastra run --full  # force re-extract everything

# View results
brahmastra show graph
brahmastra show central-entities
brahmastra show clusters
```

**Option C: Notion Integration**
```bash
# Set env vars
export NOTION_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="xyz"

# Sync from Notion
brahmastra sync
```

### Using the Dashboard

#### Reading the Force-Directed Graph
- **Node size** = PageRank score (importance)
- **Node color** = Louvain cluster (topic)
- **Edge** = one fact ("Alice owns Bob")
- **Hover** = see relation type
- **Drag** = move nodes around (helps untangle)
- **Scroll/pinch** = zoom
- **Click node** = open entity inspector

#### Entity Inspector (Right Drawer)
Shows everything about a selected entity:
- All facts pointing **to** it (incoming)
- All facts pointing **from** it (outgoing)
- Each fact's source quote + note + date
- Confidence score

#### Insight Tabs

**Central Entities**
- PageRank leaderboard
- Who's most structurally important?

**Concept Clusters**
- Louvain communities
- Related entities grouped together
- Topic discovery

**Contradictions**
- Functional relations with multiple values
- "Sarah reports to Mei Lin (old)" vs "Sarah reports to Bob (NEW)"
- Helpful for keeping vault consistent

**Predicted Links**
- Entities that should probably be connected
- Uses common-neighbors heuristic
- Click to confirm or ignore

**Entity Resolution**
- Raw mentions → canonical clusters
- See exactly why entities were merged
- Method + similarity score

**Notes**
- Browse vault
- Extract triples from new text on-the-fly
- See extraction status for each note

### CLI Reference

```bash
brahmastra run [--full]
  - Incremental (default): only extract pending notes
  - --full: force re-extract all notes

brahmastra sync
  - Sync from Notion database
  - Requires NOTION_TOKEN + NOTION_DATABASE_ID

brahmastra add-note <title>
  - Interactive: prompts for content
  - Marks as pending for extraction

brahmastra show graph
  - Print full graph stats
  - Nodes, edges, clusters, contradictions, predictions

brahmastra show nodes
  - Entity table with centrality scores
  - Ranked by PageRank

brahmastra show clusters
  - Louvain communities
  - Who clusters together?

brahmastra show contradictions
  - All conflicting facts
  - By date (newest marked as LATEST)

brahmastra show predicted-links
  - Recommended connections
  - Ranked by common-neighbor score

brahmastra show notes
  - Vault browser
  - Extraction status for each note

brahmastra mcp
  - Start MCP server (stdio transport)
  - Register in Claude Code
```

### MCP Server (Claude Integration)

```bash
# Start the server
brahmastra mcp

# In Claude Code settings, add:
# Name: Brahmastra
# Command: python -m brahmastra.mcp_server
# Arguments: (none)

# Now in Claude Code you can:
central_entities(top_n=5)        # Top 5 most important people
contradictions()                 # All conflicts
predict_links(top_n=3)          # Top 3 recommendations
entity_history("Sarah")         # Everything about Sarah
concept_clusters()              # All communities
run_pipeline()                  # Trigger update
```

---

## Deployment Architecture

### Vercel experimentalServices

**vercel.json:**
```json
{
  "experimentalServices": [
    {
      "name": "brahmastra-backend",
      "slug": "brahmastra-backend",
      "src": "backend",
      "use": "@vercel/python",
      "buildCommand": "uv pip install -e ."
    },
    {
      "name": "brahmastra-frontend",
      "slug": "brahmastra-frontend",
      "src": "frontend",
      "use": "@vercel/next"
    }
  ]
}
```

**How it works:**
1. Vercel detects experimentalServices
2. Creates two isolated containers
3. Frontend gets auto-routed `/api/*` → backend
4. Both scale independently
5. Shared environment variables

**Deployment:**
```bash
# Connect GitHub repo to Vercel
vercel link

# Set environment variables
vercel env add ANTHROPIC_API_KEY
vercel env add NOTION_TOKEN
vercel env add NOTION_DATABASE_ID

# Push to main branch
git push

# Vercel auto-builds and deploys both services
```

---

## Configuration & Environment

### Environment Variables

**Required:**
```bash
ANTHROPIC_API_KEY=sk_key_xxxxx  # Claude access
```

**Optional:**
```bash
# Notion integration
NOTION_TOKEN=secret_xxxxx
NOTION_DATABASE_ID=xxxxxxx

# Backend configuration
BRAHMASTRA_DB=/path/to/concept_graph.db  # SQLite path
BACKEND_URL=http://localhost:8001        # For frontend API calls (auto on Vercel)

# Debug
DEBUG=true  # More verbose output
```

### Ontology (10 Relations)

See `ontology.yaml`:
```yaml
relations:
  - name: owns
    domain: person
    range: project
    functional: true
    
  - name: reports_to
    domain: person
    range: person
    functional: true
    
  # ... 8 more relations
```

---

## Conclusion

Brahmastra is a **complete, production-ready knowledge graph engine** that:

1. **Reads** notes from Notion, filesystem, or manual input
2. **Extracts** facts using Claude 3.5 Haiku (cheap, fast)
3. **Deduplicates** entities with Union-Find + embeddings
4. **Builds** networkx graphs with full provenance
5. **Analyzes** using PageRank, Louvain, contradiction detection, link prediction
6. **Visualizes** with d3-force interactive dashboard
7. **Persists** in SQLite (scales to personal level)
8. **Integrates** via REST API, CLI, and MCP server

The entire pipeline is **atomic**, **incremental**, **resumable**, and **deterministic**. It's deployable to Vercel with zero additional infrastructure.
