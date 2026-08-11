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
from brahmastra.llm import ollama_available, ollama_chat

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

def _subgraph_facts(
    entity_ids: set[str],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Collect 1-hop facts touching any matched entity. Each fact carries the
    note_id so the answer can be cited. Higher-confidence facts first.
    """
    facts: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for e in edges:
        if e["source"] in entity_ids or e["target"] in entity_ids:
            key = (e["source"], e["relation"], e["target"])
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "text": f'{e["source"]} {e["relation"]} {e["target"]}',
                "quote": e.get("source_quote", ""),
                "note_id": e.get("note_id", ""),
                "confidence": float(e.get("confidence", 1.0)),
            })
    facts.sort(key=lambda f: f["confidence"], reverse=True)
    return facts[:MAX_FACTS]


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


def local_search(question: str, cached: dict[str, Any]) -> dict[str, Any]:
    nodes = cached["graph"]["nodes"]
    edges = cached["graph"]["edges"]

    matched = _match_entities(question, nodes)
    if not matched:
        return {
            "mode": "local",
            "answer": "I couldn't find any entity in your knowledge graph matching that question.",
            "entities": [],
            "citations": [],
        }

    entity_ids = {n["id"] for n in matched}
    facts = _subgraph_facts(entity_ids, edges)
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
        fact_lines.append(f"{i}. {f['text']} {tag}{quote}")

    user = (
        f"Question: {question}\n\n"
        f"Facts from the knowledge graph:\n" + "\n".join(fact_lines)
    )
    answer = ollama_chat(_LOCAL_SYSTEM, user, temperature=0.2).strip()

    # Prefer the notes the answer actually cited; fall back to all subgraph
    # notes only if the model emitted no [n:...] tags.
    fact_note_ids = {f["note_id"] for f in facts if f["note_id"]}
    cited = _cited_in(answer, fact_note_ids)
    return {
        "mode": "local",
        "answer": answer,
        "entities": sorted(entity_ids),
        "citations": _citations(cited or fact_note_ids),
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
    answer = ollama_chat(_GLOBAL_SYSTEM, user, temperature=0.3).strip()

    return {
        "mode": "global",
        "answer": answer,
        "entities": [],
        "citations": [],
    }


def answer_question(question: str, mode: str = "auto") -> dict[str, Any]:
    """
    Answer a natural-language question against the graph.

    mode: "auto" (route by heuristic), "local", or "global".
    Returns {mode, answer, entities, citations}.
    """
    question = (question or "").strip()
    if not question:
        return {"mode": "none", "answer": "Please ask a question.", "entities": [], "citations": []}

    if not ollama_available():
        return {
            "mode": "none",
            "answer": "The local LLM (Ollama) is not reachable, so I can't answer right now.",
            "entities": [],
            "citations": [],
        }

    cached = _load()
    if not cached:
        return {
            "mode": "none",
            "answer": "The knowledge graph is empty. Add notes and run the pipeline first.",
            "entities": [],
            "citations": [],
        }

    # The searches call the LLM, which can fail transiently when Ollama is busy
    # or cold (e.g. the pipeline/watcher is mid-run saturating it). Catch that so
    # the caller gets a friendly message instead of an opaque HTTP 500.
    try:
        if mode == "global":
            return global_search(question, cached)
        if mode == "local":
            return local_search(question, cached)

        # auto
        matched = _match_entities(question, cached["graph"]["nodes"])
        if _is_global(question, matched):
            return global_search(question, cached)
        return local_search(question, cached)
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
