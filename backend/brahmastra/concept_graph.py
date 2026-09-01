"""
Stage 5 — Concept Graph Builder.

Builds a real networkx MultiDiGraph from resolved triples, then computes:
  - PageRank centrality
  - Louvain community detection (modularity-based clustering)
  - Contradiction detection (functional relations that changed over time)
  - Link prediction (Common Neighbors + Jaccard coefficient)
  - Serialises everything to a JSON payload that both the API and CLI can consume.

Usage: from brahmastra.concept_graph import run_build_graph
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import networkx as nx

from brahmastra import db
from brahmastra.ontology import is_functional


def _membership_key(members: list[str]) -> str:
    """
    A stable identity for a cluster: exactly who is in it.

    Not the cluster id. Louvain numbers communities per run, so the same set of
    entities routinely reappears under a different id -- keying on id would
    hand a cached summary to the wrong cluster, which is worse than paying to
    regenerate it. Membership changing at all invalidates the summary, which is
    the correct conservative rule: a summary describes the members it saw.
    """
    # Joined on a unit separator, which cannot occur in an entity name. An
    # empty join would make ["ab", "c"] and ["a", "bc"] the same cluster.
    return chr(0x1f).join(sorted(members))


def _previous_summaries_by_membership() -> dict[str, str]:
    """
    Summaries from the last cached graph, keyed by membership.

    Returns empty on any failure -- a missing cache just means everything gets
    resummarised, which is the old behaviour and merely slow.
    """
    try:
        cached = db.get_cached_graph()
    except Exception:
        return {}
    if not cached:
        return {}
    out: dict[str, str] = {}
    for cluster in (cached.get("stats") or {}).get("concept_clusters") or []:
        summary = cluster.get("summary")
        members = cluster.get("members")
        if summary and members:
            out[_membership_key(members)] = summary
    return out


# ---------------------------------------------------------------------------
# Louvain (python-louvain / community package)
# ---------------------------------------------------------------------------

def _louvain_partition(G: nx.Graph) -> dict[str, int]:
    """
    Run Louvain community detection on the undirected projection of G.
    Falls back to connected-components if python-louvain is not installed.
    """
    undirected = G.to_undirected()
    try:
        import community as community_louvain  # python-louvain package
        return community_louvain.best_partition(undirected)
    except ImportError:
        pass
    # Fallback: label propagation (built-in to networkx)
    try:
        communities = nx.algorithms.community.label_propagation_communities(undirected)
        partition: dict[str, int] = {}
        for i, comm in enumerate(communities):
            for node in comm:
                partition[node] = i
        return partition
    except Exception:
        # Last resort: all in one cluster
        return {n: 0 for n in G.nodes()}


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

def _detect_contradictions(
    triples: list[dict[str, Any]],
    canonical_map: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Find contradictions: two triples with the same canonical subject and a
    functional relation but different canonical objects.

    Returns a list of contradiction dicts sorted by recency.
    """
    # Group triples by (canonical_subject, relation) for functional relations only
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for t in triples:
        if not is_functional(t["relation"]):
            continue
        canon_subj = canonical_map.get(t["subject_text"], t["subject_text"])
        groups[(canon_subj, t["relation"])].append(t)

    contradictions = []
    for (subj, rel), entries in groups.items():
        # Multiple distinct canonical objects = contradiction
        canon_objects = {canonical_map.get(e["object_text"], e["object_text"]) for e in entries}
        if len(canon_objects) < 2:
            continue

        # Sort by extraction time (newest first)
        sorted_entries = sorted(
            entries,
            key=lambda e: e.get("extracted_at", ""),
            reverse=True,
        )

        contradictions.append({
            "subject": subj,
            "relation": rel,
            "conflicting_values": sorted(canon_objects),
            "resolved_value": canonical_map.get(
                sorted_entries[0]["object_text"], sorted_entries[0]["object_text"]
            ),
            "evidence": [
                {
                    "object": canonical_map.get(e["object_text"], e["object_text"]),
                    "source_quote": e.get("source_quote", ""),
                    "note_id": e.get("source_note_id", ""),
                    "extracted_at": e.get("extracted_at", ""),
                }
                for e in sorted_entries[:4]  # show up to 4 pieces of evidence
            ],
        })

    return contradictions


# ---------------------------------------------------------------------------
# Link prediction
# ---------------------------------------------------------------------------

def _predict_links(
    G: nx.DiGraph,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """
    Predict missing edges using Common Neighbors + Jaccard coefficient
    on the undirected projection of the graph.

    Returns top-k predicted pairs not already connected.
    """
    undirected = G.to_undirected()
    if undirected.number_of_nodes() < 3:
        return []

    # Only consider pairs that share at least one neighbour
    predictions = []
    try:
        for u, v, score in nx.jaccard_coefficient(undirected):
            if score > 0 and not undirected.has_edge(u, v):
                cn = len(sorted(nx.common_neighbors(undirected, u, v)))
                predictions.append({
                    "source": u,
                    "target": v,
                    "jaccard": round(score, 4),
                    "common_neighbors": cn,
                    "score": round(score, 4),
                })
    except Exception:
        pass

    predictions.sort(key=lambda x: x["score"], reverse=True)
    return predictions[:top_k]


# ---------------------------------------------------------------------------
# Graph serialisation helpers
# ---------------------------------------------------------------------------

def _serialise_graph(
    G: nx.MultiDiGraph,
    pagerank: dict[str, float],
    partition: dict[str, int],
) -> dict[str, Any]:
    """Convert networkx graph to a frontend-compatible JSON dict."""
    nodes = []
    for node, data in G.nodes(data=True):
        nodes.append({
            "id": node,
            "label": node,
            "type": data.get("type", "unknown"),
            "pagerank": round(pagerank.get(node, 0.0), 6),
            "cluster": partition.get(node, 0),
        })

    edges = []
    for u, v, data in G.edges(data=True):
        edges.append({
            "source": u,
            "target": v,
            "relation": data.get("relation", "related_to"),
            "source_quote": data.get("source_quote", ""),
            "note_id": data.get("note_id", ""),
            "confidence": round(float(data.get("confidence", 1.0)), 3),
        })

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_build_graph() -> dict[str, Any]:
    """
    Build the concept graph from all canonical triples and cache it.
    """
    triples = db.get_all_triples()
    canonical_map = db.get_canonical_map()
    entity_clusters = db.get_entity_clusters()

    if not triples:
        empty: dict[str, Any] = {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "graph": {"nodes": [], "edges": []},
            "stats": {
                "nodes": 0,
                "edges": 0,
                "central_entities": [],
                "concept_clusters": [],
                "contradictions": [],
                "predicted_links": [],
                "entity_clusters": [],
            },
        }
        db.cache_graph(empty["graph"], empty["stats"])
        return empty

    # ------------------------------------------------------------------
    # 1. Build MultiDiGraph from canonical triples
    # ------------------------------------------------------------------
    G: nx.MultiDiGraph = nx.MultiDiGraph()

    for t in triples:
        subj = canonical_map.get(t["subject_text"], t["subject_text"])
        obj  = canonical_map.get(t["object_text"],  t["object_text"])

        if not subj or not obj:
            continue

        # Add / update nodes with entity types
        if subj not in G:
            G.add_node(subj, type=t.get("subject_type", "unknown"))
        if obj not in G:
            G.add_node(obj, type=t.get("object_type", "unknown"))

        G.add_edge(
            subj, obj,
            relation=t["relation"],
            source_quote=t.get("source_quote", ""),
            note_id=t.get("source_note_id", ""),
            confidence=float(t.get("confidence", 1.0)),
        )

    # ------------------------------------------------------------------
    # 2. PageRank
    # ------------------------------------------------------------------
    # nx.pagerank needs a DiGraph view (no multi-edges)
    simple_G = nx.DiGraph()
    for u, v, data in G.edges(data=True):
        if simple_G.has_edge(u, v):
            # accumulate weight
            simple_G[u][v]["weight"] = simple_G[u][v].get("weight", 1) + 1
        else:
            simple_G.add_edge(u, v, weight=1)
    for node, data in G.nodes(data=True):
        if node not in simple_G:
            simple_G.add_node(node, **data)

    if simple_G.number_of_nodes() > 0:
        try:
            pagerank = nx.pagerank(simple_G, alpha=0.85, weight="weight")
        except nx.PowerIterationFailedConvergence:
            pagerank = {n: 1.0 / simple_G.number_of_nodes() for n in simple_G.nodes()}
    else:
        pagerank = {}

    # ------------------------------------------------------------------
    # 3. Louvain clustering
    # ------------------------------------------------------------------
    partition = _louvain_partition(simple_G)

    # ------------------------------------------------------------------
    # 4. Contradiction detection
    # ------------------------------------------------------------------
    contradictions = _detect_contradictions(triples, canonical_map)

    # ------------------------------------------------------------------
    # 5. Link prediction
    # ------------------------------------------------------------------
    predicted_links = _predict_links(simple_G)

    # ------------------------------------------------------------------
    # 6. Build summary stats
    # ------------------------------------------------------------------
    # Top central entities (top 10 by PageRank)
    central_entities = sorted(
        [{"entity": n, "pagerank": round(v, 6)} for n, v in pagerank.items()],
        key=lambda x: x["pagerank"],
        reverse=True,
    )[:10]

    # Cluster summaries
    cluster_members: dict[int, list[str]] = defaultdict(list)
    for node, cid in partition.items():
        cluster_members[cid].append(node)

    # Carry forward the summary of any cluster whose membership is unchanged.
    #
    # Summarising costs one LLM call per cluster and used to run for all of
    # them on every pipeline run, however little had changed -- 25 calls a run,
    # which is what made a single incremental run exceed a 30-minute timeout.
    #
    # This is done HERE, not in cluster_summary, because caching the graph is
    # what destroys the previous summaries: by the time that stage reads the
    # cache, it is already looking at the clusters this function just wrote.
    #
    # Keyed on membership, never on cluster id. Louvain numbers communities per
    # run, so the same set of entities routinely comes back under a different
    # id -- matching on id would reuse summaries for the wrong clusters, which
    # is far worse than recomputing them.
    previous = _previous_summaries_by_membership()
    concept_clusters = []
    for cid, members in sorted(cluster_members.items(), key=lambda x: -len(x[1])):
        ordered = sorted(members)
        cluster = {"id": cid, "members": ordered, "size": len(ordered)}
        carried = previous.get(_membership_key(ordered))
        if carried:
            cluster["summary"] = carried
        concept_clusters.append(cluster)

    # Entity resolution summary for the stats payload
    er_summary = [
        {
            "cluster_id": c["cluster_id"],
            "canonical_name": c["canonical_name"],
            "mentions": c["mentions"],
            "size": len(c["mentions"]),
        }
        for c in entity_clusters
        if len(c["mentions"]) > 1   # only show merged clusters
    ]

    graph_payload = _serialise_graph(G, pagerank, partition)
    stats_payload: dict[str, Any] = {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "central_entities": central_entities,
        "concept_clusters": concept_clusters,
        "contradictions": contradictions,
        "predicted_links": predicted_links,
        "entity_clusters": er_summary,
        # How many triples this graph was built FROM, so anyone can later ask
        # whether it still reflects them. Recorded in the cache rather than
        # beside it because the cache lives in the store, which host processes
        # and containers share -- a file on disk does not: the MCP server
        # writes to backend/data while the containers write to /data, so a
        # marker there is invisible to exactly the half that needs it.
        "triples_total": db.get_db_stats().get("triples_total"),
    }

    built_at = datetime.now(timezone.utc).isoformat()
    db.cache_graph(graph_payload, stats_payload)

    return {
        "built_at": built_at,
        "graph": graph_payload,
        "stats": stats_payload,
    }
