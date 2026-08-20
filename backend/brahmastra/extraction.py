"""
Stage 3 — Extraction agent.

Reads notes with extraction_status='pending' from SQLite, calls the configured
LLM via structured JSON output, validates each triple against the ontology,
then writes results back.

Which provider AND which model both come from `brahmastra.llm` — never name a
model here. Naming one is how extraction kept calling a retired Groq model
after llm.py had already been pointed at its replacement.

Usage (programmatic):   from brahmastra.extraction import run_extraction
Usage (CLI):            brahmastra extract
"""

from __future__ import annotations

import json
import os
import re
import time
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
from brahmastra.ontology import (
    ENTITY_TYPES, RELATION_NAMES, RELATIONS, is_valid_triple, normalise_relation,
)

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

━━ PEOPLE AND ORGANISATIONS ━━
✗ BAD:  ("Sapan", "related_to", "Shaan Kapoor")     ← loses where he works
✓ GOOD: ("Sapan", "employed_by", "Veraxion")        ← name the organisation
Use employed_by for "works at / works for / is employed by" an organisation.
Use member_of for belonging to a team, group or body without employment.
Always emit the organisation as its own entity of type organisation.

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

# A truncated reply is not partial JSON, it is unparseable, so the entire note
# fails and loses every triple in it. Long design notes overran the old 2048
# limit; current cloud models have 131k context, so headroom is cheap.
EXTRACTION_MAX_TOKENS = int(os.environ.get("EXTRACTION_MAX_TOKENS", "8192"))


def _ollama_available() -> bool:
    """Return True if a local Ollama server is reachable."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _extract_with_llm(title: str, content: str) -> list[dict[str, Any]]:
    """
    Dispatch to the configured/available LLM provider and return raw triple dicts.

    Provider *selection* lives in llm.py so extraction, GraphRAG and cluster
    summaries can never disagree about which provider is live. The per-provider
    calls stay here because this module's JSON-mode prompt is tuned.
    """
    from brahmastra.llm import resolve_provider  # local import: avoids a cycle

    provider = resolve_provider()  # raises LLMUnavailable with guidance

    if provider == "ollama":
        return _extract_with_ollama(title, content)
    if provider == "groq":
        return _extract_with_groq(title, content, os.environ["GROQ_API_KEY"])
    if provider == "anthropic":
        return _extract_with_anthropic(title, content, os.environ["ANTHROPIC_API_KEY"])

    raise RuntimeError(f"Unknown LLM provider: {provider!r}")


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
    raw_text = (raw_text or "").strip()
    if not raw_text:
        # Distinguish an empty reply from malformed JSON. A reasoning-style
        # model that spends its budget before emitting content returns "", and
        # "Expecting value: line 1 column 1" gives no clue what went wrong.
        raise ValueError(
            "LLM returned an empty response — no content to parse. The model may "
            "have exhausted max_tokens before answering, or ignored JSON mode."
        )
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


# Groq states how long to wait in the 429 itself: "Please try again in 7.5s",
# or "in 1m14.2s". Honouring that beats guessing, and guessing is what made
# retries useless -- 2s + 4s covers about six seconds of a limit the server
# says needs thirty, so all three attempts land inside the same closed window
# and the note fails as though the outage were permanent.
_RETRY_AFTER = re.compile(
    r"try again in\s+(?:(\d+)m)?\s*([\d.]+)s", re.IGNORECASE
)

# Upper bound on a single in-run wait. Past this, sleeping blocks the whole
# pipeline for a note that the NEXT run will retry for free -- errored notes are
# re-queued automatically. Better to fail this note fast and keep going.
EXTRACT_MAX_BACKOFF = float(os.environ.get("EXTRACT_MAX_BACKOFF", "45"))


def _too_large_hint(note_id: str = "") -> str:
    return (
        "note is too large for one extraction request. Split it, or lower "
        "EXTRACTION_MAX_TOKENS / raise the model's context." + (f" ({note_id})" if note_id else "")
    )


def _is_too_large(error: Exception) -> bool:
    """
    HTTP 413: the request exceeds what the model accepts.

    Settled, not transient -- waiting cannot make the request smaller. Kept
    separate from _is_quota_error because the correct response differs: a spent
    quota means every remaining note will fail and the run should stop, while
    one oversized note says nothing about the next one.
    """
    text = str(error).lower()
    return "413" in text and ("too large" in text or "request_too_large" in text)


def _retry_delay(error: Exception, attempt: int) -> float:
    """
    How long to wait before retrying, preferring the server's own instruction.

    Falls back to exponential backoff when the error carries no hint, and
    always waits at least as long as that fallback: a suspiciously short hint
    should not make us retry sooner than we otherwise would.
    """
    fallback = 2.0 * (attempt + 1)          # 2s, 4s
    match = _RETRY_AFTER.search(str(error))
    if not match:
        return fallback
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    # A tenth of a second of slack: waking exactly on the boundary tends to
    # land just inside the window that is still closed.
    advised = minutes * 60 + seconds + 0.1
    return min(max(advised, fallback), EXTRACT_MAX_BACKOFF)


def _extract_with_groq(title: str, content: str, api_key: str) -> list[dict[str, Any]]:
    try:
        from groq import Groq
    except ImportError as e:
        raise RuntimeError("groq package not installed — run: uv pip install groq") from e

    from brahmastra.llm import groq_model

    from brahmastra.llm import _is_model_missing, _is_quota_exhausted

    client = Groq(api_key=api_key)
    kwargs: dict[str, Any] = dict(
        # NOT a literal: llm.py owns which model runs, or this call site drifts
        # and keeps hitting a retired one after llm.py has been fixed.
        model=groq_model(),
        # A long note yields many triples, and a cut-off reply is not partial
        # JSON — it is unparseable, so the whole note fails. At 2048 a 3KB note
        # truncated mid-string at char 3151 and lost everything.
        max_tokens=EXTRACTION_MAX_TOKENS,
        # Ollama has always asked for JSON; this path never did, and relied on
        # the model volunteering it. Reasoning-style models answer with prose
        # first and leave `content` empty, which parses as "no triples".
        response_format={"type": "json_object"},
        temperature=0.0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(title, content)},
        ],
    )

    # Retry a PER-MINUTE limit; never retry a per-day one or a retired model.
    #
    # This path had no retry at all, while llm.chat has always had one — so a
    # TPM 429 failed the note outright. Observed: a pipeline run failed all
    # three pending notes, and running extraction again immediately afterwards
    # succeeded with 55 triples. Nothing was lost (errored notes are retried on
    # the next run) but the run reported `status: error` and skipped write-back
    # for a condition that clears in seconds.
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(**kwargs)
            return _parse_llm_response(response.choices[0].message.content)
        except Exception as e:
            last = e
            # Both of these are settled facts, not congestion: backing off
            # cannot make a spent daily quota refill or a deleted model exist.
            # run_extraction stops the whole run on them.
            if _is_quota_exhausted(e) or _is_model_missing(e):
                raise
            # A 413 is settled too -- waiting cannot make the request smaller,
            # so three attempts just spend the backoff to fail identically.
            # Unlike the two above it must NOT stop the run: one oversized note
            # says nothing about the next one, so this fails just this note and
            # extraction carries on. Found via extraction_error, which is what
            # that column was added for -- the failure had been read as a rate
            # limit until the message was actually recorded.
            if _is_too_large(e):
                raise
            if attempt == 2:
                break                      # no point sleeping before giving up
            time.sleep(_retry_delay(e, attempt))

    raise RuntimeError(f"Groq extraction failed after 3 attempts: {last}")


def _extract_with_anthropic(title: str, content: str, api_key: str) -> list[dict[str, Any]]:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("anthropic package not installed — run: uv pip install anthropic") from e

    from brahmastra.llm import anthropic_model

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=anthropic_model(),  # see _extract_with_groq
        max_tokens=EXTRACTION_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(title, content)}],
    )
    return _parse_llm_response(message.content[0].text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Words a model uses to mean "I don't know", which must never become entities.
_PLACEHOLDERS = frozenset({
    "unknown", "none", "null", "n/a", "na", "nil", "unspecified", "unnamed",
    "not specified", "not mentioned", "not stated", "tbd", "todo", "-", "?",
    "someone", "something", "unknown entity", "unknown person",
})


def _is_placeholder(text: str) -> bool:
    return str(text or "").strip().lower() in _PLACEHOLDERS


def _is_quota_error(message: str) -> bool:
    """
    True if this failure will not clear during this run, so the run must stop.

    Covers two causes with the same remedy — stop now, do not grind through the
    remaining notes. A spent DAILY quota does not refill in seconds, and a
    retired model does not come back at all: Groq decommissioned
    llama-3.3-70b-versatile mid-session, and every pending note failed against
    it one after another.
    """
    from brahmastra.llm import _is_model_missing, _is_quota_exhausted
    exc = Exception(str(message))
    return _is_quota_exhausted(exc) or _is_model_missing(exc)


def _coerce_triple(t: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    Normalise a model-produced triple onto the ontology.

    Returns (triple, coercion_reason). A None triple means genuinely
    unusable — malformed, or below the confidence floor.

    This replaces a hard validate-and-drop. Dropping meant a relation outside
    the ontology, or a right relation with the wrong argument types, deleted
    the fact entirely: "Sapan works at Veraxion" failed and left no Veraxion
    entity at all. A strict core is worth keeping for domain/range checks and
    the `functional` flag contradiction detection needs, but it should degrade
    a fact rather than destroy it. Unmappable relations become `related_to`,
    which is defined over any types, so the connection survives even when its
    precise meaning does not.
    """
    required = {"subject_text", "subject_type", "relation", "object_text", "object_type"}
    if not required.issubset(t.keys()):
        return None, "missing_fields"
    if not str(t.get("subject_text", "")).strip() or not str(t.get("object_text", "")).strip():
        return None, "empty_endpoint"
    # Models emit placeholders when they cannot find a value — "Shaan Kapoor
    # employed_by Unknown". These are not entities: they create a junk node,
    # and on a functional relation they read as a second value and so fire a
    # FALSE contradiction against the real one.
    if _is_placeholder(t["subject_text"]) or _is_placeholder(t["object_text"]):
        return None, "placeholder_entity"

    try:
        confidence = float(t.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    if confidence < 0.4:
        return None, "low_confidence"

    out = dict(t)
    # An unrecognised entity type becomes 'unknown' rather than voiding the
    # fact; most relations admit 'unknown' on at least one side.
    if out["subject_type"] not in ENTITY_TYPES:
        out["subject_type"] = "unknown"
    if out["object_type"] not in ENTITY_TYPES:
        out["object_type"] = "unknown"

    reason: str | None = None
    canonical, inverted = normalise_relation(out["relation"])

    if canonical is None:
        reason = f"unmapped_relation:{str(out['relation']).strip().lower()}"
        out["relation"] = "related_to"
    else:
        if canonical != out["relation"]:
            reason = f"alias:{str(out['relation']).strip().lower()}->{canonical}"
        out["relation"] = canonical
        if inverted:
            # The alias stated the relation backwards; swap so the stored fact
            # matches the note. "Mei manages Sarah" -> "Sarah reports_to Mei".
            out["subject_text"], out["object_text"] = out["object_text"], out["subject_text"]
            out["subject_type"], out["object_type"] = out["object_type"], out["subject_type"]

    if not is_valid_triple(out["subject_type"], out["relation"], out["object_type"]):
        # Right idea, wrong argument types. Keep the association as a weak
        # link instead of deleting it.
        if out["relation"] != "related_to":
            reason = (
                f"domain_range:{out['relation']}"
                f"({out['subject_type']}->{out['object_type']})"
            )
            out["relation"] = "related_to"
    return out, reason


def _validate_triple(t: dict[str, Any]) -> bool:
    """Kept for callers/tests that only need a yes/no verdict."""
    triple, _ = _coerce_triple(t)
    return triple is not None


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
        db.mark_note_error(note_id, str(e))
        return {"triples_added": 0, "triples_skipped": 0, "error": str(e)}

    # Delete previous triples for this note before re-inserting
    db.delete_triples_for_note(note_id)

    valid: list[dict[str, Any]] = []
    coercions: list[str] = []
    for t in raw:
        triple, reason = _coerce_triple(t)
        if triple is None:
            # Only genuinely unusable facts are dropped now.
            coercions.append(reason or "dropped")
            continue
        if reason:
            coercions.append(reason)
        valid.append(triple)
    skipped = len(raw) - len(valid)

    # Tag each triple with the source note id
    for t in valid:
        t["source_note_id"] = note_id

    if valid:
        db.insert_triples(valid)

    db.mark_note_done(note_id)
    return {
        "triples_added": len(valid),
        "triples_skipped": skipped,
        # Surfaced so ontology gaps are visible instead of silent: a relation
        # that keeps showing up as unmapped is evidence it should be added.
        "coercions": coercions,
        "error": None,
    }


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

    # Notes that previously errored are retried. Selecting only 'pending'
    # stranded them forever: a transient provider outage (Ollama down, Groq
    # rate limit) flipped a note to 'error' and no later run ever looked at it
    # again, so it stayed invisible to search_entities with no way back except
    # editing the row by hand. Retrying is safe — a note that genuinely cannot
    # be parsed simply reappears in `errors` every run, which is visible rather
    # than silent. Set EXTRACT_RETRY_ERRORS=0 to opt out.
    retried: list[dict[str, Any]] = []
    if os.environ.get("EXTRACT_RETRY_ERRORS", "1") != "0":
        retried = db.get_notes(status="error")

    queue = pending + retried
    if not queue:
        return {
            "extracted": 0, "total_pending": 0, "retried": 0,
            "triples_added": 0, "errors": [],
        }

    results = []
    errors = []
    quota_exhausted: str | None = None
    for note in queue:
        r = extract_note(note)
        results.append(r)
        if r["error"]:
            errors.append({"note_id": note["id"], "error": r["error"]})
            # A spent daily quota fails every remaining note identically, so
            # continuing burns minutes to accomplish nothing. Stop and say so.
            # Notes already marked 'error' are picked up by the next run.
            if _is_quota_error(r["error"]):
                quota_exhausted = r["error"]
                break

    total_added = sum(r["triples_added"] for r in results)
    out = {
        "extracted": len([r for r in results if not r["error"]]),
        "total_pending": len(queue),
        "retried": len(retried),
        "triples_added": total_added,
        "errors": errors,
    }
    if quota_exhausted:
        # Surfaced so the caller can tell "the provider is out of quota until
        # tomorrow" from "extraction is broken" — very different responses.
        out["quota_exhausted"] = quota_exhausted
        out["aborted_after"] = len(results)
        out["remaining"] = len(queue) - len(results)
    return out
