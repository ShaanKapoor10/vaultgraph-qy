"""
Embeddings propose a merge. A model decides it.

WHY THIS EXISTS, measured on the live graph rather than imagined. Running the
resolver read-only over 906 triples and 907 unique mentions, cosine similarity
alone merged 143 pairs. Most were right -- "pipeline.py" with "file
pipeline.py", "test_checkpoint.py" with "tests/test_checkpoint.py". Several
were not:

    0.910   backend/brahmastra/ingest/memo.py == backend/brahmastra/memo.py
    0.914   function run_pipeline            == run_full_pipeline function
    0.910   Neo4j Aura                       == Neo4j Aura Free
    0.962   GraphRAG                         == Microsoft GraphRAG

Two different files fused into one node. Two different functions. A product
and one of its tiers. A vendor's research system and a feature of this one.
None of these is a threshold problem: "pipeline.py"/"file pipeline.py" scores
0.888 and is right, while the two memo.py files score 0.910 and are wrong, so
no cut separates them. The distinction needs to be KNOWN, not measured.

THE SHAPE, borrowed from cocoindex's ops.entity_resolution. Its embedding step
is a BLOCKER -- it proposes candidates -- and an LLM pair-resolver confirms
each one before anything is merged. Two of its choices are copied deliberately:

  * Err towards NOT matching. An unmerged duplicate is a visible extra node
    somebody can merge; a wrong merge is invisible and unmergeable.
  * Validate the reply against what was asked, and fail closed. A verdict the
    model did not actually give must never become a merge.

WHAT IS DIFFERENT HERE. cocoindex asks one entity against a list of canonical
candidates; this asks about PAIRS, because the resolver upstream already works
in pairs and feeds them to a Union-Find. Pairs are also batched -- eight
verdicts per call -- because the corpus produces ~143 candidate pairs and the
tier that runs this counts requests.

THE REMAINING WEAKNESS, stated rather than hidden: Union-Find is transitive.
If A~B and B~C are both confirmed, A and C merge without ever being asked
about. Confirming every edge makes that far less likely than it was; it does
not make it impossible.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

# How many pairs go in one call. Small enough that the model is still reading
# each pair rather than pattern-matching down a list, large enough that a
# 900-mention corpus costs tens of calls rather than hundreds.
BATCH = 8

VARIANT = "entity-pair"

SYSTEM_PROMPT = """You are cleaning up a software project's knowledge graph. Something has
already decided that each numbered pair of names LOOKS similar. Your job is
the question it could not answer: do the two names refer to one thing, or to
two things that both exist?

Answer `same` when the two names are one thing described at different lengths
or from different angles -- a file and its fuller path, a function and a
sentence about that function, a tool and its formal name, a thing with and
without filler words like "the", "feature", "note", "model", "database".
People write the same thing loosely, and most of these pairs are that.

Answer `different` when both names would still exist as separate things in the
project:
  - two distinct files, functions, modules, branches or endpoints, however
    alike their names;
  - a product and one particular tier, edition or version of it;
  - something asserted and the same thing denied.

Use what you know about software to tell these apart. When a pair genuinely
leaves you undecided, answer `different` -- an unmerged duplicate is a visible
extra node somebody can merge later, while a wrong merge is invisible. That is
the tiebreaker for a real coin-flip, not a reason to refuse a pair you can
actually call.

Return ONLY JSON:

{"verdicts": [{"pair": 1, "same": true}, {"pair": 2, "same": false}]}

Give exactly one verdict per pair, using the numbers as given.
"""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "same": {"type": "boolean"},
                },
                "required": ["pair", "same"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def enabled() -> bool:
    """
    On unless switched off. ENTITY_CONFIRM=0 restores embedding-only merging.

    Kept switchable because this is the one part of resolution that costs
    model calls, and somebody re-running a pipeline purely to rebuild a graph
    should be able to say no.
    """
    return os.environ.get("ENTITY_CONFIRM", "").strip() != "0"


def available() -> bool:
    """Whether a model can be reached at all. Never raises."""
    if not enabled():
        return False
    try:
        from brahmastra.llm import llm_available

        return llm_available()
    except Exception:
        return False


def _render(pairs: list[tuple[str, str]]) -> str:
    lines = []
    for i, (a, b) in enumerate(pairs, start=1):
        lines.append(f"{i}. {a!r}  ||  {b!r}")
    return "\n".join(lines)


def _ask(pairs: list[tuple[str, str]]) -> dict[int, bool]:
    """
    One call. Returns {pair_number: same}, possibly partial. Never raises.

    A pair the model did not answer about is simply absent, and the caller
    treats absence as "not confirmed" -- so a truncated or malformed reply
    costs recall and can never invent a merge.
    """
    from brahmastra import memo
    from brahmastra.llm import active_model, chat

    user = _render(pairs)
    model = ""
    try:
        model = active_model()
    except Exception:
        pass

    key = memo.key_for(user, VARIANT, model, SYSTEM_PROMPT)
    raw = memo.load(key)
    if raw is None:
        try:
            raw = chat(SYSTEM_PROMPT, user, json_schema=_SCHEMA,
                       temperature=0.0, max_tokens=800)
        except Exception:
            return {}
        memo.save(key, raw, VARIANT)

    try:
        payload = json.loads(raw)
    except Exception:
        return {}

    out: dict[int, bool] = {}
    for verdict in payload.get("verdicts") or []:
        try:
            number = int(verdict["pair"])
            # Only verdicts about pairs we actually asked about. A model that
            # invents a pair 9 in a batch of 8 must not merge anything.
            if 1 <= number <= len(pairs):
                out[number] = bool(verdict["same"])
        except Exception:
            continue
    return out


def confirm(pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], bool]:
    """
    Which of these candidate merges a model will vouch for.

    Returns a verdict for every pair given. Absent, unparseable or unreachable
    answers all come back False, because this function's job is to be the
    thing that must SAY YES before two entities become one.
    """
    todo = list(pairs)
    verdicts: dict[tuple[str, str], bool] = {pair: False for pair in todo}
    if not todo or not available():
        return verdicts

    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        answers = _ask(batch)
        for offset, pair in enumerate(batch, start=1):
            if answers.get(offset):
                verdicts[pair] = True
    return verdicts
