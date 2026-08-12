"""
GraphRAG — natural-language question answering over the concept graph.

Two retrieval modes, inspired by Microsoft GraphRAG:

  • LOCAL search  — for questions about specific entities. Match the question to
    graph nodes, pull the 1-hop subgraph (facts + source quotes) around them, and
    have the local LLM answer strictly from those facts.

  • GLOBAL search — for broad / thematic questions ("what are the main themes?").
    Feed the per-cluster summaries (from cluster_summary.py) as context.

A cheap heuristic routes each question; callers can also force a mode.
Every answer carries citations back to the source notes (edges already store
note_id), so the UI can link a claim to where it came from.

Usage: from brahmastra.rag import answer_question
"""

from __future__ import annotations

import re
from typing import Any

from brahmastra import db
from brahmastra.llm import chat, llm_available

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_MATCHED_ENTITIES = 6     # how many graph nodes a question can anchor to
MAX_FACTS = 60               # cap subgraph facts sent to the LLM
MIN_ENTITY_LEN = 3           # ignore 1-2 char tokens when matching ("a", "is")

# Words that signal a broad/thematic question → prefer GLOBAL search.
_GLOBAL_HINTS = {
    "overview", "summary", "summarise", "summarize", "themes", "theme",
    "topics", "main", "overall", "everything", "big", "picture", "structure",
    "areas", "domains", "clusters",
}

# Common question words to ignore when matching entities.
_STOPWORDS = {
    "what", "who", "where", "when", "why", "how", "is", "are", "was", "were",
    "do", "does", "did", "the", "a", "an", "of", "to", "in", "on", "for",
    "and", "or", "about", "tell", "me", "know", "i", "my", "with", "that",
    "this", "it", "its", "their", "there", "have", "has", "can", "you",
}


# ---------------------------------------------------------------------------
# Normalisation (mirrors entity_resolution._normalise)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_tokens(text: str) -> set[str]:
    return {
        t for t in _normalise(text).split()
        if len(t) >= MIN_ENTITY_LEN and t not in _STOPWORDS
    }


# ---------------------------------------------------------------------------
# Graph loading
# ---------------------------------------------------------------------------

def _load() -> dict[str, Any] | None:
    cached = db.get_cached_graph()
    if not cached or not cached["graph"].get("nodes"):
        return None
    return cached


# ---------------------------------------------------------------------------
# Entity matching
# ---------------------------------------------------------------------------

def _match_entities(question: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return graph nodes the question is about, best first.

    Scoring: a node whose full (normalised) label appears verbatim in the
    question scores highest; otherwise score by token overlap between the
    node label and the question. Ties broken by PageRank (more central wins).
    """
    q_norm = _normalise(question)
    q_tokens = _content_tokens(question)

    scored: list[tuple[float, dict[str, Any]]] = []
    for n in nodes:
        label_norm = _normalise(n["id"])
        if not label_norm:
            continue
        label_tokens = {t for t in label_norm.split() if len(t) >= MIN_ENTITY_LEN}
        if not label_tokens:
            continue

        score = 0.0
        # Strong: whole entity name appears in the question.
        if label_norm in q_norm and len(label_norm) >= MIN_ENTITY_LEN:
            score = 1.0 + len(label_tokens)  # longer exact matches rank above short ones
        else:
            overlap = label_tokens & q_tokens
            if overlap:
                # Fraction of the entity's own tokens present in the question.
                score = len(overlap) / len(label_tokens)
                # Require a meaningful match for multi-word entities.
                if score < 0.5:
                    score = 0.0

        if score > 0:
            scored.append((score + n.get("pagerank", 0.0), n))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored[:MAX_MATCHED_ENTITIES]]


# ---------------------------------------------------------------------------
# Subgraph → facts
# ---------------------------------------------------------------------------

def _subgraph_facts(entity_ids: set[str], depth: int = 1) -> list[dict[str, Any]]:
    """
    Collect facts within `depth` hops of any matched entity, nearest first.
    Each fact carries the note_id so the answer can be cited.

    Delegated to the store: on Neo4j this is an indexed traversal over just the
    reachable edges, rather than a Python scan of every edge in the graph.
    """
    return db.neighbourhood(entity_ids, limit=MAX_FACTS, depth=depth)


# Questions whose answer lives further than one relationship away — "Sarah's
# manager's other reports" is two hops, and at depth 1 the graph simply does
# not contain it. Detecting the shape is cheap and avoids paying for a wider
# traversal on questions that do not need one.
_MULTIHOP_HINTS = (
    "also", "else", "other", "others", "indirectly", "connected", "connection",
    "related to", "through", "via", "chain", "path", "between", "colleague",
    "peer", "peers", "teammate", "sibling", "downstream", "upstream",
    "depends on", "affected", "impact", "reach",
)


def _wants_multihop(question: str) -> bool:
    """True when the question implies a chain rather than a direct fact."""
    q = _normalise(question)
    if "'s " in question.lower() or "s' " in question.lower():
        # Possessive chaining: "Sarah's manager", "the project's owner".
        return True
    return any(h in q for h in _MULTIHOP_HINTS)


_CITE_RE = re.compile(r"\[n:([^\]]+)\]")


def _citations(note_ids: set[str]) -> list[dict[str, str]]:
    """Map note_ids to {id, title} for the UI to link against."""
    out = []
    for nid in note_ids:
        if not nid:
            continue
        note = db.get_note(nid)
        out.append({"note_id": nid, "title": note["title"] if note else nid})
    return out


def _cited_in(answer: str, available: set[str]) -> set[str]:
    """Note ids the LLM actually cited inline (intersected with real subgraph ids)."""
    return {nid for nid in _CITE_RE.findall(answer)} & available


# ---------------------------------------------------------------------------
# Mode routing
# ---------------------------------------------------------------------------

def _is_global(question: str, matched: list[dict[str, Any]]) -> bool:
    q_tokens = set(_normalise(question).split())
    if q_tokens & _GLOBAL_HINTS:
        return True
    # No specific entity anchored → fall back to a global/thematic answer.
    return len(matched) == 0


# ---------------------------------------------------------------------------
# Answer generation
# ---------------------------------------------------------------------------

_LOCAL_SYSTEM = (
    "You answer questions about a personal knowledge graph using ONLY the facts "
    "provided. Each fact is numbered and tagged with a source note id like [n:abc123]. "
    "Write a concise, direct answer. After any claim, cite the supporting note id(s) in "
    "square brackets. If the facts do not contain the answer, say plainly that the graph "
    "has no information on it. Do not invent facts."
)

_GLOBAL_SYSTEM = (
    "You answer broad questions about a personal knowledge graph using the cluster "
    "summaries provided. Each summary describes one topic cluster. Synthesise a concise "
    "high-level answer about the themes and how they relate. Do not invent specifics that "
    "are not in the summaries."
)


def local_search(
    question: str, nodes: list[dict[str, Any]], depth: int | None = None
) -> dict[str, Any]:
    """
    Answer from the subgraph around entities named in the question.

    Depth defaults to 2 for chained questions and 1 otherwise; an explicit
    depth from the caller always wins.
    """
    if depth is None:
        depth = 2 if _wants_multihop(question) else 1
    matched = _match_entities(question, nodes)
    if not matched:
        return {
            "mode": "local",
            "answer": "I couldn't find any entity in your knowledge graph matching that question.",
            "entities": [],
            "citations": [],
        }

    entity_ids = {n["id"] for n in matched}
    facts = _subgraph_facts(entity_ids, depth=depth)
    if not facts:
        return {
            "mode": "local",
            "answer": f"I found {', '.join(sorted(entity_ids))} in the graph but no related facts.",
            "entities": sorted(entity_ids),
            "citations": [],
        }

    fact_lines = []
    for i, f in enumerate(facts, 1):
        tag = f"[n:{f['note_id']}]" if f["note_id"] else ""
        quote = f'  ("{f["quote"]}")' if f["quote"] else ""
        # Flag indirect facts so the model can tell a stated fact from one
        # reached by following a chain, and hedge accordingly.
        hops = f.get("hops", 1)
        via = f" (indirect, {hops} hops)" if hops > 1 else ""
        fact_lines.append(f"{i}. {f['text']}{via} {tag}{quote}")

    user = (
        f"Question: {question}\n\n"
        f"Facts from the knowledge graph:\n" + "\n".join(fact_lines)
    )
    answer = chat(_LOCAL_SYSTEM, user, temperature=0.2).strip()

    # Prefer the notes the answer actually cited; fall back to all subgraph
    # notes only if the model emitted no [n:...] tags.
    fact_note_ids = {f["note_id"] for f in facts if f["note_id"]}
    cited = _cited_in(answer, fact_note_ids)
    return {
        "mode": "local",
        "answer": answer,
        "entities": sorted(entity_ids),
        "citations": _citations(cited or fact_note_ids),
        # Surfaced so a caller can see whether the answer used chained facts.
        "depth": depth,
        "facts_used": len(facts),
    }


def global_search(question: str, cached: dict[str, Any]) -> dict[str, Any]:
    clusters = cached["stats"].get("concept_clusters", [])
    summarised = [c for c in clusters if c.get("summary")]
    if not summarised:
        return {
            "mode": "global",
            "answer": "No cluster summaries are available yet. Run the pipeline to generate them.",
            "entities": [],
            "citations": [],
        }

    summary_lines = [
        f"- {c['summary']} (members: {', '.join(c['members'][:6])})"
        for c in summarised
    ]
    user = (
        f"Question: {question}\n\n"
        f"Cluster summaries of the knowledge graph:\n" + "\n".join(summary_lines)
    )
    answer = chat(_GLOBAL_SYSTEM, user, temperature=0.3).strip()

    return {
        "mode": "global",
        "answer": answer,
        "entities": [],
        "citations": [],
    }


def answer_question(
    question: str, mode: str = "auto", depth: int | None = None
) -> dict[str, Any]:
    """
    Answer a natural-language question against the graph.

    mode:  "auto" (route by heuristic), "local", or "global".
    depth: hops to traverse for local search. None picks 2 for chained
           questions ("Sarah's manager's other reports") and 1 otherwise.
    Returns {mode, answer, entities, citations} plus depth/facts_used on local.
    """
    question = (question or "").strip()
    if not question:
        return {"mode": "none", "answer": "Please ask a question.", "entities": [], "citations": []}

    if not llm_available():
        return {
            "mode": "none",
            "answer": (
                "No LLM provider is reachable, so I can't answer right now. "
                "Set GROQ_API_KEY in backend/.env or start Ollama locally."
            ),
            "entities": [],
            "citations": [],
        }

    # Nodes only. Local search never needs the edge list — it asks the store
    # for the 1-hop neighbourhood instead, which is an indexed traversal on a
    # graph backend. Only global search loads the full projection, for the
    # cluster summaries in stats.
    nodes = db.get_entities()
    if not nodes:
        return {
            "mode": "none",
            "answer": "The knowledge graph is empty. Add notes and run the pipeline first.",
            "entities": [],
            "citations": [],
        }

    def _global() -> dict[str, Any]:
        cached = _load()
        if not cached:
            return {
                "mode": "none",
                "answer": "The knowledge graph is empty. Add notes and run the pipeline first.",
                "entities": [],
                "citations": [],
            }
        return global_search(question, cached)

    # The searches call the LLM, which can fail transiently when the provider is
    # busy or cold. Catch that so the caller gets a friendly message instead of
    # an opaque HTTP 500.
    try:
        if mode == "global":
            return _global()
        if mode == "local":
            return local_search(question, nodes, depth)

        # auto
        matched = _match_entities(question, nodes)
        if _is_global(question, matched):
            return _global()
        return local_search(question, nodes, depth)
    except Exception as e:
        return {
            "mode": "error",
            "answer": (
                "The local model didn't respond in time — this usually means Ollama is "
                "busy (the pipeline or live-sync watcher may be mid-run) or is reloading "
                "the model into memory. Please try again in a moment."
            ),
            "entities": [],
            "citations": [],
            "error": str(e),
        }
