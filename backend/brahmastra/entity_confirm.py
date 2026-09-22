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

MEASURED, AND NOT ADOPTED -- IT IS OFF BY DEFAULT.

Four runs against 21 labelled pairs from the live graph, judging only what the
deterministic guards in entity_resolution.py leave undecided:

    true merges kept       14-15 of 16
    false merges refused    3-4 of 5

Read carefully, that is not a win. Two of the five "false" pairs it lets
through every single time -- "GraphRAG"/"Microsoft GraphRAG" and
"vaultgraph-qy repository"/"...repository root" -- are pairs where my own
label is arguable, so the honest count is that it reliably prevents TWO wrong
merges ("Neo4j Aura"/"Neo4j Aura Free", "PageRank"/"PageRank results") and
reliably costs ONE OR TWO right ones.

And the ones it costs CHANGE BETWEEN RUNS at temperature 0 -- extract.ts in
one run, Neo4j Aura backend in the next, backend-adapter.ts in the third. That
is the part that decides it. A resolver whose clusters differ run to run makes
the graph churn for no reason, and the deterministic guards next door get the
same class of merge right for free and get it right every time.

WHAT WOULD CHANGE THE ANSWER, in order of promise: a larger model (this was
measured on gpt-oss-120b, which is the small thing the free tier offers);
asking only about pairs in the ambiguous similarity band rather than all of
them; and giving the judge the SENTENCES the two names appeared in, which is
the one piece of evidence a human uses here and this prompt withholds.

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
    OFF unless asked for. ENTITY_CONFIRM=1 turns the judge on.

    Off by default because the measurement did not earn it -- see MEASURED,
    AND NOT ADOPTED in the module docstring. Opt-in rather than deleted,
    because the same code on a larger model is the obvious next thing to try
    and the harness for judging it already exists.
    """
    return os.environ.get("ENTITY_CONFIRM", "").strip() == "1"


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


class Unanswered(Exception):
    """The model never gave a verdict. NOT the same as a verdict of `no`."""


def _ask(pairs: list[tuple[str, str]]) -> dict[int, bool]:
    """
    One call. Returns {pair_number: same}. Raises Unanswered if the model
    could not be reached or its reply could not be read.

    THE DISTINCTION IS THE WHOLE POINT, and getting it wrong wasted a
    measurement. An earlier version swallowed every exception and returned an
    empty dict, which the caller read as "not confirmed" -- so when Groq's
    DAILY quota ran out mid-run, a whole batch came back refused and the
    numbers looked like a model being cautious. The first eight pairs of a
    sixteen-pair probe were "rejected"; they had simply never been asked.

    A cache that cannot be reached degrades to paying again. A JUDGE that
    cannot be reached must not silently return a verdict.
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
                       temperature=0.0, max_tokens=1200)
        except Exception as exc:
            raise Unanswered(f"{type(exc).__name__}: {exc}"[:200]) from exc
        memo.save(key, raw, VARIANT)

    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise Unanswered(f"unreadable reply: {exc}"[:200]) from exc

    out: dict[int, bool] = {}
    for verdict in payload.get("verdicts") or []:
        try:
            number = int(verdict["pair"])
            # Only verdicts about pairs we actually asked about. A model that
            # invents a pair 9 in a batch of 8 must not decide anything.
            if 1 <= number <= len(pairs):
                out[number] = bool(verdict["same"])
        except Exception:
            continue
    if not out:
        raise Unanswered("reply contained no usable verdicts")
    return out


def confirm(pairs: Iterable[tuple[str, str]]) -> tuple[
        dict[tuple[str, str], bool], list[tuple[str, str]]]:
    """
    Verdicts, and the pairs nobody managed to answer for.

    Returns (verdicts, unanswered). A pair in `unanswered` has NO verdict: the
    caller decides what to do about it, and the only safe default is to leave
    the pipeline behaving as it did before this judge existed. Silently
    refusing them would turn an outage into a change in the graph.
    """
    todo = list(pairs)
    verdicts: dict[tuple[str, str], bool] = {}
    unanswered: list[tuple[str, str]] = []
    if not todo:
        return verdicts, unanswered
    if not available():
        return verdicts, todo

    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        try:
            answers = _ask(batch)
        except Unanswered:
            unanswered.extend(batch)
            continue
        for offset, pair in enumerate(batch, start=1):
            if offset in answers:
                verdicts[pair] = answers[offset]
            else:
                # Asked, and the model skipped it. Not a verdict either.
                unanswered.append(pair)
    return verdicts, unanswered
