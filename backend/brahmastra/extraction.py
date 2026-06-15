"""
Stage 3 — Extraction agent.

Reads notes with extraction_status='pending' from SQLite,
calls Anthropic claude-3-5-haiku via structured JSON output,
validates each triple against the ontology, then writes results back.

Usage (programmatic):   from brahmastra.extraction import run_extraction
Usage (CLI):            brahmastra extract
"""

from __future__ import annotations

import json
import os
from typing import Any

from brahmastra import db
from brahmastra.ontology import ENTITY_TYPES, RELATION_NAMES, is_valid_triple

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are an expert knowledge graph extractor.
Given a note, extract factual (subject, relation, object) triples.

Rules:
- Subject and object must be named entities (people, projects, tools, concepts, organisations, events, dates).
- Relation must be EXACTLY one of: {", ".join(RELATION_NAMES)}
- Subject and object type must be EXACTLY one of: {", ".join(ENTITY_TYPES)}
- Only extract triples that are clearly stated in the text — no inference.
- confidence: 0.0–1.0 (how certain you are this is stated in the text).
- source_quote: the shortest phrase from the note that supports this triple (verbatim).

Return ONLY a JSON object with this shape (no markdown, no extra text):
{{
  "triples": [
    {{
      "subject_text": "...",
      "subject_type": "...",
      "relation": "...",
      "object_text": "...",
      "object_type": "...",
      "confidence": 0.95,
      "source_quote": "..."
    }}
  ]
}}
"""


def _build_user_message(title: str, content: str) -> str:
    return f"Note title: {title}\n\n{content}"


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------

def _extract_with_llm(title: str, content: str) -> list[dict[str, Any]]:
    """Call Anthropic and return a list of raw triple dicts."""
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic package not installed — run: uv pip install anthropic") from e

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY env var not set")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(title, content)}],
    )

    raw_text = message.content[0].text.strip()

    # Strip any accidental markdown fences
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        data = json.loads(raw_text)
        return data.get("triples", [])
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}\nRaw: {raw_text[:300]}") from e


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_triple(t: dict[str, Any]) -> bool:
    """Return True if the triple passes ontology constraints."""
    required = {"subject_text", "subject_type", "relation", "object_text", "object_type"}
    if not required.issubset(t.keys()):
        return False
    if t["subject_type"] not in ENTITY_TYPES:
        return False
    if t["object_type"] not in ENTITY_TYPES:
        return False
    if t["relation"] not in RELATION_NAMES:
        return False
    if not is_valid_triple(t["subject_type"], t["relation"], t["object_type"]):
        return False
    confidence = float(t.get("confidence", 1.0))
    if confidence < 0.4:   # drop very low confidence triples
        return False
    return True


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_note(note: dict[str, Any]) -> dict[str, Any]:
    """
    Extract triples for a single note dict.
    Returns {"triples_added": N, "triples_skipped": M, "error": None|str}
    """
    note_id = note["id"]
    try:
        raw = _extract_with_llm(note["title"], note["content"])
    except Exception as e:
        db.mark_note_error(note_id)
        return {"triples_added": 0, "triples_skipped": 0, "error": str(e)}

    # Delete previous triples for this note before re-inserting
    db.delete_triples_for_note(note_id)

    valid = [t for t in raw if _validate_triple(t)]
    skipped = len(raw) - len(valid)

    # Tag each triple with the source note id
    for t in valid:
        t["source_note_id"] = note_id

    if valid:
        db.insert_triples(valid)

    db.mark_note_done(note_id)
    return {"triples_added": len(valid), "triples_skipped": skipped, "error": None}


def run_extraction(full: bool = False) -> dict[str, Any]:
    """
    Extract all pending notes (or all notes if full=True).
    Called by the pipeline router and the full pipeline orchestrator.
    """
    if full:
        # Re-mark all notes as pending so they get re-extracted
        notes = db.get_notes()
        for n in notes:
            db.upsert_note(
                n["id"], n["title"], n["content"],
                last_edited=n.get("last_edited"),
                mark_pending=True,
            )

    pending = db.get_notes(status="pending")
    if not pending:
        return {"extracted": 0, "total_pending": 0, "errors": []}

    results = []
    errors = []
    for note in pending:
        r = extract_note(note)
        results.append(r)
        if r["error"]:
            errors.append({"note_id": note["id"], "error": r["error"]})

    total_added = sum(r["triples_added"] for r in results)
    return {
        "extracted": len([r for r in results if not r["error"]]),
        "total_pending": len(pending),
        "triples_added": total_added,
        "errors": errors,
    }
