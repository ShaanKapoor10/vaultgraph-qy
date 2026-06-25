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
from pathlib import Path
from typing import Any

# Load backend/.env on import so LLM_PROVIDER / keys are set no matter which
# entrypoint runs extraction (pipeline, CLI, fresh `python -c`, tests).
# Without this, a bare `python -c "run_pipeline()"` would miss the config and
# fall through to the flaky auto-detect path.
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV)
    except ImportError:
        pass

from brahmastra import db
from brahmastra.ontology import ENTITY_TYPES, RELATION_NAMES, RELATIONS, is_valid_triple

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _build_relation_guide() -> str:
    lines = []
    for r in RELATIONS:
        lines.append(f"  - {r.name}: {r.description}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are an expert knowledge graph extractor for a personal knowledge base.
Given a note, extract rich (subject, relation, object) triples that capture every meaningful
relationship, fact, dependency, and connection in the text.

━━ ENTITY TYPES ━━
{", ".join(ENTITY_TYPES)}

━━ RELATIONS (pick the MOST SPECIFIC one that fits) ━━
{_build_relation_guide()}

━━ RULES ━━
1. Subject and object must be NAMED entities — specific names, not generic words.
2. Relation must be EXACTLY one word from the list above.
3. Entity types must be EXACTLY one from the entity types list.
4. DO NOT default to "uses" or "related_to" — only use them when no specific relation fits.
   Prefer: has_component, depends_on, implements, provides, integrates_with, created_by, works_on.
5. Extract what is clearly stated OR strongly implied by the text.
6. Aim for 8–20 triples per note. Cover all major entities and relationships mentioned.
7. confidence: 0.0–1.0 based on how directly the text supports this triple.
8. source_quote: shortest verbatim phrase from the note that supports this triple.

━━ ANTI-PATTERNS TO AVOID ━━
✗ BAD:  ("Brahmastra", "uses", "Python")           ← too vague, use has_component or depends_on
✓ GOOD: ("Brahmastra", "has_component", "Python backend")
✓ GOOD: ("Brahmastra backend", "depends_on", "FastAPI")
✓ GOOD: ("Brahmastra", "implements", "knowledge graph")
✓ GOOD: ("pipeline.py", "part_of", "Brahmastra backend")
✓ GOOD: ("Claude 3.5 Haiku", "provides", "entity extraction")
✓ GOOD: ("Brahmastra", "created_by", "Shaan Kapoor")

Return ONLY a JSON object (no markdown, no extra text):
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
# LLM provider selection
# ---------------------------------------------------------------------------
#
# Priority (configurable via LLM_PROVIDER env var: "ollama" | "groq" | "anthropic"):
#   1. Ollama  — local, free, no rate limits (default if reachable)
#   2. Groq    — fast cloud, but free tier is rate limited (~12k TPM)
#   3. Anthropic
#
# Ollama config:
#   OLLAMA_MODEL  (default "qwen2.5:7b-instruct")
#   OLLAMA_HOST   (default "http://localhost:11434")

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _ollama_available() -> bool:
    """Return True if a local Ollama server is reachable."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _extract_with_llm(title: str, content: str) -> list[dict[str, Any]]:
    """Dispatch to the configured/available LLM provider and return raw triple dicts."""
    provider = os.environ.get("LLM_PROVIDER", "").lower().strip()
    groq_key = os.environ.get("GROQ_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    # Explicit provider override
    if provider == "ollama":
        return _extract_with_ollama(title, content)
    if provider == "groq" and groq_key:
        return _extract_with_groq(title, content, groq_key)
    if provider == "anthropic" and anthropic_key:
        return _extract_with_anthropic(title, content, anthropic_key)

    # Auto: prefer local Ollama, then cloud providers
    if _ollama_available():
        return _extract_with_ollama(title, content)
    if groq_key:
        return _extract_with_groq(title, content, groq_key)
    if anthropic_key:
        return _extract_with_anthropic(title, content, anthropic_key)

    raise RuntimeError(
        "No LLM provider available — start Ollama (ollama serve) or set "
        "GROQ_API_KEY / ANTHROPIC_API_KEY in backend/.env"
    )


def _extract_with_ollama(title: str, content: str) -> list[dict[str, Any]]:
    """Call a local Ollama model with native JSON mode for reliable structured output."""
    import json as _json
    import urllib.request

    payload = {
        "model": OLLAMA_MODEL,
        "format": "json",  # force valid JSON output — no markdown fences, no prose
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(title, content)},
        ],
    }
    # Retry to survive transient drops (Ollama can close a connection while
    # loading the model into VRAM or under concurrent load).
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            return _parse_llm_response(data["message"]["content"])
        except Exception as e:
            last_err = e
            import time
            time.sleep(2 * (attempt + 1))  # 2s, 4s backoff

    raise RuntimeError(
        f"Ollama request failed after 3 attempts ({OLLAMA_MODEL} @ {OLLAMA_HOST}): {last_err}"
    )


def _parse_llm_response(raw_text: str) -> list[dict[str, Any]]:
    raw_text = raw_text.strip()
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


def _extract_with_groq(title: str, content: str, api_key: str) -> list[dict[str, Any]]:
    try:
        from groq import Groq
    except ImportError as e:
        raise RuntimeError("groq package not installed — run: uv pip install groq") from e

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(title, content)},
        ],
    )
    return _parse_llm_response(response.choices[0].message.content)


def _extract_with_anthropic(title: str, content: str, api_key: str) -> list[dict[str, Any]]:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic package not installed — run: uv pip install anthropic") from e

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(title, content)}],
    )
    return _parse_llm_response(message.content[0].text)


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
