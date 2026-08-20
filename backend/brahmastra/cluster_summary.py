"""
Stage 5b — Cluster Summaries.

Each Louvain concept cluster (from concept_graph.py) is a topic domain that
emerged from the relation structure. This stage asks the local LLM to name the
theme of each cluster in one short sentence, so the UI can label clusters and
GraphRAG "global search" can answer broad questions from these summaries.

Runs AFTER run_build_graph(): it reads the cached graph, computes summaries,
merges them into each cluster in stats.concept_clusters, and re-caches.

Designed to fail soft: if Ollama is unreachable, clusters keep their members
and simply get an empty summary — the pipeline never breaks because of this.

Usage: from brahmastra.cluster_summary import run_cluster_summaries
"""

from __future__ import annotations

from typing import Any

from brahmastra import db
from brahmastra.llm import LLMQuotaExhausted, chat, llm_available

# Skip clusters smaller than this — a 1-entity "cluster" has no theme worth a
# round-trip to the LLM.
MIN_CLUSTER_SIZE = 2
# Cap how many clusters we summarise per run, largest first, to bound latency.
MAX_CLUSTERS = 25
# Cap members / edges shown in a prompt so a giant cluster can't blow num_ctx.
MAX_MEMBERS_IN_PROMPT = 40
MAX_EDGES_IN_PROMPT = 40

SYSTEM_PROMPT = (
    "You label clusters in a personal knowledge graph. Given the entities in one "
    "cluster and the relationships among them, reply with a SINGLE sentence (max 25 "
    "words) naming the theme that ties them together. No preamble, no quotes, no "
    "markdown — just the sentence."
)


def _build_user_message(members: list[str], internal_edges: list[dict[str, Any]]) -> str:
    member_line = ", ".join(members[:MAX_MEMBERS_IN_PROMPT])
    rel_lines = [
        f"  {e['source']} --{e['relation']}--> {e['target']}"
        for e in internal_edges[:MAX_EDGES_IN_PROMPT]
    ]
    rels = "\n".join(rel_lines) if rel_lines else "  (no internal relationships)"
    return (
        f"Entities in this cluster:\n  {member_line}\n\n"
        f"Relationships within the cluster:\n{rels}\n\n"
        "Theme sentence:"
    )


def _summarise_one(members: list[str], internal_edges: list[dict[str, Any]]) -> str:
    raw = chat(
        SYSTEM_PROMPT,
        _build_user_message(members, internal_edges),
        temperature=0.2,
        timeout=120,
    )
    # Collapse to a single trimmed line — the model occasionally adds a newline.
    return " ".join(raw.strip().split())


def summarise_clusters(
    concept_clusters: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[int, str]:
    """
    Return {cluster_id: summary} for clusters at/above MIN_CLUSTER_SIZE.

    Clusters that error out individually are skipped (left without a summary)
    rather than failing the whole batch.
    """
    if not llm_available():
        return {}

    # Index edges by endpoint membership once.
    summaries: dict[int, str] = {}
    ranked = sorted(concept_clusters, key=lambda c: -c.get("size", len(c["members"])))

    for cluster in ranked[:MAX_CLUSTERS]:
        members = cluster["members"]
        if len(members) < MIN_CLUSTER_SIZE:
            continue
        # Already summarised, and its membership has not changed since --
        # concept_graph carries such summaries across the rebuild, keyed on who
        # is in the cluster. Regenerating would spend an LLM call to produce a
        # description of the same entities. This is what turned a routine
        # incremental run from ~25 calls into, typically, none: one run of the
        # pipeline used to exceed a 30-minute timeout on this stage alone.
        carried = cluster.get("summary")
        if carried:
            summaries[cluster["id"]] = carried
            continue
        member_set = set(members)
        internal_edges = [
            e for e in edges
            if e["source"] in member_set and e["target"] in member_set
        ]
        try:
            summaries[cluster["id"]] = _summarise_one(members, internal_edges)
        except LLMQuotaExhausted:
            # The provider is out of quota until it resets, so every remaining
            # cluster would fail the same way. Keep what we have and stop
            # rather than spending minutes to add nothing.
            break
        except Exception:
            # Fail soft per-cluster: one bad/empty response shouldn't drop the rest.
            continue

    return summaries


def run_cluster_summaries() -> dict[str, Any]:
    """
    Read the cached graph, summarise each cluster, merge summaries into
    stats.concept_clusters, and re-cache. Returns a small status dict.
    """
    cached = db.get_cached_graph()
    if not cached:
        return {"summarised": 0, "skipped": "no cached graph"}

    stats = cached["stats"]
    clusters = stats.get("concept_clusters", [])
    edges = cached["graph"].get("edges", [])
    if not clusters:
        return {"summarised": 0, "skipped": "no clusters"}

    # Counted BEFORE summarising, because summarise_clusters fills the gaps in.
    # Reporting only a total would hide the thing worth knowing: whether this
    # run spent 25 LLM calls or none.
    carried_in = sum(1 for c in clusters if c.get("summary"))

    summaries = summarise_clusters(clusters, edges)

    # Merge: every cluster gets a `summary` key (empty string if not generated)
    # so the frontend can rely on the field always existing.
    for c in clusters:
        c["summary"] = summaries.get(c["id"], "")

    db.cache_graph(cached["graph"], stats)
    generated = max(len(summaries) - carried_in, 0)
    return {
        "summarised": len(summaries),
        "clusters_total": len(clusters),
        # Split out because the total says nothing about cost. `generated` is
        # the LLM calls this run actually paid for; on a run where nothing
        # changed it should be 0, and a number that stays high run after run
        # means the carry-forward has stopped matching.
        "reused": carried_in,
        "generated": generated,
        "llm_used": generated > 0,
    }
