# Workspaces — multiple knowledge graphs in one system

Today Brahmastra has exactly one graph. This adds several: a personal one, one
for work, one per project — creatable from the UI, the API, or an AI agent over
MCP.

---

## 1. The constraint that shaped this

Aura Free **cannot create additional databases**:

```
CREATE DATABASE probe_ws
  → Neo.ClientError.Statement.UnsupportedAdministrationCommand
```

So "one Neo4j database per workspace" — the textbook answer — is unavailable.
Isolation has to happen inside the single database.

## 2. Isolation model: `workspaceId` on every node

Every `:Note`, `:Entity`, `:Mention` and `:Cluster` carries a `workspaceId`,
and every query filters on it.

Chosen over the alternatives because:

- **Extra label per workspace** (`:Note:WsOffice`) enforces isolation by label
  scan rather than a `WHERE` clause, which is harder to leak. But it needs APOC
  for dynamic labels, workspace names must be sanitised into valid label
  syntax, and the label set grows without bound.
- **Separate Aura instance per workspace** gives true isolation but costs a
  paid instance and separate credentials each, which rules out creating a
  workspace on demand from the UI or an agent — a core requirement.

### The risk, and how it is contained

Property-based isolation has one failure mode: **a forgotten filter leaks one
workspace's data into another.** That is a correctness bug, not a style issue.

This is not hypothetical — it happened. The store factory built the Neo4j
store without forwarding the requested workspace, so `for_workspace("office")`
silently bound to `default` and a write meant for one graph overwrote a note in
another. Nothing errored.

Containment is therefore in three layers, none of which relies on remembering:

1. **Callers never pass a workspace filter.** The store is bound at
   construction and every query adds the predicate itself, so a caller cannot
   express — and therefore cannot forget — the filter. Cross-workspace reads go
   through separately named methods.
2. **The factory resolves the workspace and verifies the binding.** It never
   lets a backend decide what "no workspace" means, and it raises
   `WorkspaceBindingError` if the constructed store bound to anything other
   than what was asked for. A backend that drops the argument now fails at
   construction, before it can write.
3. **Unscoped queries are refused at runtime.** `Neo4jStore._run` rejects any
   Cypher that touches a partitioned label without naming `workspaceId`,
   raising `WorkspaceIsolationError`. Genuinely cross-workspace queries pass
   `unscoped=True`, which makes the exception visible at the call site. This is
   what turns a forgotten filter from a silent leak into an error.

Layer 1 is convention; layers 2 and 3 are enforcement. The bug above defeated
layer 1 alone, which is why the other two exist.

### Why a single store, not one SQLite file per workspace

Separate files would give SQLite free isolation. It would also make
cross-workspace search impossible without opening every file and merging in
Python. Since cross-workspace search is wanted, both backends use one store
with a `workspace_id` column/property. The two backends then behave
identically, which is the property the whole `GraphStore` contract exists to
preserve.

---

## 3. Data model

### Workspace record

| Field | Meaning |
|---|---|
| `id` | slug, e.g. `personal`, `office`, `apollo` — used as the partition key |
| `name` | display name |
| `description` | free text |
| `notion_database_id` | this workspace's Notion source (nullable) |
| `ontology` | ontology name; `default` for now (see §5) |
| `created_at` | ISO timestamp |

Neo4j stores these as `(:Workspace {...})`; SQLite as a `workspaces` table.
A workspace is a real row/node so it can exist before it has any content, and
so the UI can list workspaces without scanning the graph.

### Partitioning

| Store | Change |
|---|---|
| `notes`, `raw_triples`, `canonical_map`, `entity_clusters` | new `workspace_id` column, `NOT NULL DEFAULT 'default'` |
| `graph_cache` | was a singleton (`CHECK(id = 1)`); becomes one row per workspace, keyed on `workspace_id` |
| `:Note` / `:Entity` / `:Mention` / `:Cluster` / `:GraphMeta` | new `workspaceId` property |
| uniqueness | `Note.id` and `Entity.name` are unique **per workspace**, not globally — two workspaces may both have a "Sarah" and they are different people |

That last point matters: entity resolution must never merge across workspaces.

---

## 4. Selecting a workspace

Resolution order, first match wins:

1. explicit argument (`db.for_workspace("office")`, `?workspace=office`)
2. `BRAHMASTRA_WORKSPACE` environment variable
3. `default`

Existing data migrates into `default`, so nothing breaks and single-workspace
use stays exactly as it is today.

Creation is possible from all three surfaces asked for:

- **API** — `POST /workspaces`
- **MCP** — `brahmastra_create_workspace`, so an agent can make one mid-conversation
- **UI** — calls the same API

---

## 5. Per-workspace ontology — deliberately deferred

This was requested, and it is the one scope item being held back on purpose.

The ontology is currently a module-level constant, synced across three files,
that feeds the extraction prompt, triple validation, and Neo4j relationship
types. Making it per-workspace means loading it at runtime, rebuilding the
prompt per workspace, and validating per workspace — a substantial change that
touches every extraction path.

Against that: the current 18 relations are generic. `employed_by`, `reports_to`,
`depends_on`, `has_status` read the same at work and at home. There is no
evidence yet of a workspace needing a relation another must not have.

`docs/ONTOLOGY_DESIGN.md` sets the rule — grow the vocabulary when the
`coercions` output shows real demand, not in anticipation. The same rule
applies here. So the workspace record carries an `ontology` field from day one
and every workspace sets it to `default`; when one genuinely needs its own, the
hook is already in place and nothing has to be migrated.

---

## 6. Cross-workspace reads

Default is single-workspace. Cross-workspace is opt-in and explicit:

- `search_notes_across(workspaces=None, ...)` — `None` means every workspace
- results carry their `workspace_id`, so the caller can always tell where a
  hit came from

Deliberately **not** cross-workspace: entity resolution, graph building,
PageRank and clustering. Merging a work "Sarah" with a personal "Sarah" would
silently corrupt both graphs, and a PageRank computed over the union describes
a graph that does not exist.
