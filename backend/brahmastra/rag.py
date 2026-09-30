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

import os
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
MAX_PASSAGES = 4             # raw transcript passages, used only as a fallback
MAX_STATEMENTS = 6           # statement nodes a question can anchor to by meaning
STATEMENT_MIN_COSINE = 0.35  # below this a "closest statement" is just the least unrelated
NOT_IN_GRAPH = "NOT_IN_GRAPH"

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

    Asks the store first, which on Neo4j fuses fulltext with embedding
    similarity and so can match an entity the question never literally names.
    Falls back to the token-overlap scoring below when the store returns
    nothing — an empty result is normal before the first graph build, and on
    SQLite the store's own matching is this same lexical scoring anyway.
    """
    try:
        hits = db.search_entities(question, limit=MAX_MATCHED_ENTITIES)
        if hits:
            return hits
    except Exception:
        # Never let a search-index problem take down question answering;
        # the lexical path below always works.
        pass
    return _match_entities_lexical(question, nodes)


def _match_entities_lexical(
    question: str, nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Token-overlap scoring over the node list.

    A node whose full (normalised) label appears verbatim in the question
    scores highest; otherwise score by token overlap between the node label
    and the question. Ties broken by PageRank (more central wins).
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


_statement_vectors: dict[str, Any] = {}


def _match_statements(question: str, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Statement nodes the question is about, by MEANING -- whatever the backend.

    A statement is named by its own sentence ("Payments is sixty percent done:
    card flow finished..."), so a question almost never names it the way entity
    matching needs. Neo4j's entity search fuses in vectors and could find it;
    SQLite's is lexical, and there a question that named no entity went to the
    cluster summaries and answered "none of the summaries mention a film". The
    graph held the answer and was never searched for it.

    Vectors are cached per process by statement text, so a question costs one
    embedding plus whatever statements are new since the last question.
    """
    statements = [n for n in nodes if (n.get("type") or "") == "statement"]
    if not statements:
        return []
    try:
        from brahmastra.embeddings import embed

        missing = [n["id"] for n in statements if n["id"] not in _statement_vectors]
        if missing:
            vectors = embed(missing)
            if vectors is None:
                return []
            _statement_vectors.update(zip(missing, vectors))
        q = embed([question])
        if not q:
            return []
        qv = q[0]
    except Exception:                                          # noqa: BLE001
        return []
    scored = []
    for n in statements:
        v = _statement_vectors.get(n["id"])
        if v is not None:
            cos = sum(a * b for a, b in zip(qv, v))
            if cos >= STATEMENT_MIN_COSINE:
                scored.append((cos, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:MAX_STATEMENTS]]


def _with_related_statements(matched: list[dict[str, Any]], anchors: set[str],
                             facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    A matched statement brings the other statements about what IT mentions.

    Measured on Q3 planning (ingest/qa_eval.py): "who owns payments and when is
    she back?" found "payments is sixty percent done and Priya will be out for
    two weeks" and answered "two weeks" -- while "Priya is out until the 20th"
    sat one hop further, on Priya. Half-answers like that were most of the
    misses. So the entities a matched statement mentions become anchors too,
    and their other statements come in: a two-hop walk through the graph,
    which is what the graph is for.
    """
    if os.environ.get("RAG_RELATED_STATEMENTS", "1").strip() == "0":
        return facts
    statements = [n["id"] for n in matched if (n.get("type") or "") == "statement"]
    if not statements or len(facts) >= MAX_FACTS:
        return facts
    mentioned: set[str] = set()
    for sid in statements:
        prefix = f"{sid} mentions "
        mentioned.update(f["text"][len(prefix):] for f in facts if f["text"].startswith(prefix))
    extra = mentioned - anchors
    if not extra:
        return facts
    seen = {f["text"] for f in facts}
    more = [f for f in _subgraph_facts(extra, depth=1) if f["text"] not in seen]
    return facts + more[: MAX_FACTS - len(facts)]


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
    """
    Map note_ids to {id, title} for the UI to link against.

    Fetched in one call rather than one per citation. That was invisible while
    notes sat in the same local file as the graph; once the notes live on a
    separate store, a well-cited answer turns into a round trip per citation on
    the critical path of every /ask.

    An id with no note still yields a citation, titled with the id: a fact was
    genuinely extracted from it, and dropping the row would silently shorten
    the evidence list rather than showing the gap.
    """
    wanted = [nid for nid in note_ids if nid]
    if not wanted:
        return []
    found = db.get_notes_by_ids(wanted)
    return [
        {"note_id": nid, "title": (found.get(nid) or {}).get("title") or nid}
        for nid in wanted
    ]


def _cited_in(answer: str, available: set[str]) -> set[str]:
    """Note ids the LLM actually cited inline (intersected with real subgraph ids)."""
    return {nid for nid in _CITE_RE.findall(answer)} & available


# ---------------------------------------------------------------------------
# Mode routing
# ---------------------------------------------------------------------------

def _is_global(question: str, matched: list[dict[str, Any]]) -> bool:
    q_tokens = set(_normalise(question).split())
    # Only a question that ASKS for the big picture goes to the cluster
    # summaries. "No entity matched" used to send it there too, which answered
    # "what film did the trainer recommend?" from topic summaries that cannot
    # hold a film -- local search now looks for statements by meaning first.
    return bool(q_tokens & _GLOBAL_HINTS)


# ---------------------------------------------------------------------------
# Answer generation
# ---------------------------------------------------------------------------

_LOCAL_SYSTEM = (
    "You answer questions about a personal knowledge graph using ONLY the facts "
    "provided. Each fact is numbered and tagged with a source note id like [n:abc123]; "
    "some facts are whole statements someone made, with who said them. "
    "Answer directly, and include the specifics the facts carry -- the reason, the "
    "number or date, who said it or owns it -- rather than the gist; a partial "
    "answer drops exactly what the question is usually after. After any claim, cite "
    "the supporting note id(s) in "
    "square brackets. If the facts do not contain the answer, reply with exactly "
    f"{NOT_IN_GRAPH} and nothing else. Do not invent facts."
)

_TRANSCRIPT_SYSTEM = (
    "The knowledge graph had no answer to this question. Answer it using ONLY the "
    "excerpts of what was actually said, tagged like [t:2], citing them after each "
    "claim. If they do not contain the answer either, say plainly that nothing "
    "recorded answers it. Do not invent facts."
)


def _passages(question: str) -> list[dict[str, Any]]:
    """
    What was actually SAID that bears on the question (ingest/passages.py).

    Measured before this existed (ingest/qa_eval.py, Q3 planning): questions
    about detail no item carries were answered 2 of 5 times from the graph and
    5 of 5 from the transcript. The graph keeps what was extracted; this keeps
    what was said. RAG_PASSAGES=0 turns it off, which is how the two are
    compared.
    """
    if os.environ.get("RAG_PASSAGES", "1").strip() == "0":
        return []
    try:
        from brahmastra.ingest.passages import search

        return search(question, limit=MAX_PASSAGES)
    except Exception:                                          # noqa: BLE001
        return []


def _passage_lines(passages: list[dict[str, Any]]) -> list[str]:
    lines = []
    for i, p in enumerate(passages, 1):
        where = f"{p.get('title') or 'a meeting'}, {p.get('start_time') or ''}".rstrip(", ")
        lines.append(f"[t:{i}] ({where})\n{p['text']}")
    return lines


def _passage_citations(passages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    cited = {int(n) for n in re.findall(r"\[t:(\d+)\]", answer or "")}
    return [{"transcript_id": p["transcript_id"], "title": p.get("title"),
             "start_time": p.get("start_time"), "end_time": p.get("end_time"),
             "speakers": p.get("speakers")}
            for i, p in enumerate(passages, 1) if not cited or i in cited]


def _answer_from_passages(question: str, entities: list[str]) -> dict[str, Any]:
    """
    The FALLBACK: the graph had nothing, and the words may still have been said.

    Marked `source: "transcript"` so the UI can say the answer is not in the
    graph yet -- which is also a report that extraction missed something.
    """
    passages = _passages(question)
    if not passages:
        return {"mode": "local", "source": "none", "entities": entities, "citations": [],
                "answer": "Nothing in the knowledge graph or the recorded transcripts answers that."}
    user = (f"Question: {question}\n\nExcerpts of what was said:\n\n"
            + "\n\n".join(_passage_lines(passages)))
    answer = chat(_TRANSCRIPT_SYSTEM, user, temperature=0.2).strip()
    return {"mode": "local", "source": "transcript", "answer": answer, "entities": entities,
            "citations": [], "passages": _passage_citations(passages, answer), "facts_used": 0}

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
    # Statements by meaning, beside entities by name: the graph's own search.
    for n in _match_statements(question, nodes):
        if all(m["id"] != n["id"] for m in matched):
            matched.append(n)
    if not matched:
        return _answer_from_passages(question, [])

    entity_ids = {n["id"] for n in matched}
    facts = _subgraph_facts(entity_ids, depth=depth)
    facts = _with_related_statements(matched, entity_ids, facts)
    if not facts:
        return _answer_from_passages(question, sorted(entity_ids))

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
    if NOT_IN_GRAPH in answer:
        # The graph was searched and did not hold it. Only now the words.
        return _answer_from_passages(question, sorted(entity_ids))

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
        "source": "graph",
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
