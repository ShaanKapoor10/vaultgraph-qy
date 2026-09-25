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

RE-MEASURED 2026-09-25, AND ADOPTED ON GROQ -- see the end of this docstring.

FIRST MEASUREMENT (kept, because what changed it is the point):

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

THE RE-MEASUREMENT. resolution_cases.json now fixes 71 labelled pairs in
advance -- the embedding path's real candidates, answered by hand before any
model saw them -- and brahmastra.resolution_eval scores any model on them:

                          stopped wrong    broke right    across 3 runs
    gpt-oss-120b             21-22 / 29       6-9 / 42     unstable
    qwen/qwen3.8-27b         21    / 29       3   / 42     identical x3

On qwen3.8-27b the three it "breaks" are all labels a reader could argue
(`Neo4j Aura` / `Neo4j Aura Free` is a product and its tier; `qwen2.5:7b` /
`-instruct` really are two models). Run live, read-only, it refused 43 of 119
candidates: ~25 plainly right (`Obsidian` / `Obsidian replacement`,
`checkpoint` / `checkpoint queue`), 2 plainly wrong (`Groq key` / `live Groq
key`), the rest arguable. Stable, cheap and clearly net positive: the two
objections that kept it off -- break-even and churn -- are both gone.

So it runs by DEFAULT where it was measured: when the resolution model
(RESOLUTION_LLM_MODEL, default groq:qwen/qwen3.8-27b) belongs to the provider
actually in use. On Ollama that is a 7B model nobody measured, so it stays off
there unless ENTITY_CONFIRM=1 asks for it. ENTITY_CONFIRM=0 turns it off
everywhere. An unanswered pair still merges as it did before the judge, so a
retired model or a spent quota degrades to the old behaviour, not to a
different graph.

THEN THE SENTENCES (2026-09-25). The lever the first measurement named:
each name is shown with up to two sentences from the notes it was used in
(`entity_resolution.usage_context`). Same 71 pairs, same model:

                          stopped wrong    broke right    runs
    names only               21 / 29          3 / 42       x3 identical
    reworded guidance only   22 / 29          4 / 42       noise
    names + sentences        25 / 29          7 / 42       x2 identical

The four it newly stops were the hard ones: GraphRAG / Microsoft GraphRAG,
backend/.env / loading backend/.env, /health/ready / Health endpoint, a
decision note / the concept it decided. Five of the seven it breaks are one
family -- Neo4j Aura and its tier, backend and instance -- which becomes a few
VISIBLE duplicate nodes, against four INVISIBLE wrong merges prevented. By the
asymmetry this module is built on, adopted.

THE REMAINING WEAKNESS, stated rather than hidden: Union-Find is transitive.
If A~B and B~C are both confirmed, A and C merge without ever being asked
about. Confirming every edge makes that far less likely than it was; it does
not make it impossible.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

# How many pairs go in one call. Small enough that the model is still reading
# each pair rather than pattern-matching down a list, large enough that a
# 900-mention corpus costs tens of calls rather than hundreds.
BATCH = 8

# How many times a REJECTED reply is asked again, with the reason. cocoindex's
# resolver defaults to the same number. A retry never re-asks a verdict that
# parsed, so this cannot become "keep asking until it agrees".
RETRIES = 2

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
  - something asserted and the same thing denied;
  - a thing and something ABOUT it, FOR it, PART of it or DONE to it: a
    service and its API key, a database and its client package, a system and
    its replacement, a pipeline and one of its stages, a component and its
    queue or cache, a file and the act of loading it, a feature and the tests
    or coverage for it. Extra words that only say what KIND of thing it is
    ("database", "function", "model", "library", "class", "column") do not
    make it different; extra words that name a DIFFERENT object do;
  - an old or previous version of something and the current one;
  - a general idea and one specific, named implementation of it (someone
    else's product versus this project's feature of the same name).

Use what you know about software to tell these apart. When a pair genuinely
leaves you undecided, answer `different` -- an unmerged duplicate is a visible
extra node somebody can merge later, while a wrong merge is invisible. That is
the tiebreaker for a real coin-flip, not a reason to refuse a pair you can
actually call.

When sentences from the notes follow a pair, they show how each name was
actually used. Judge by what the sentences say each name IS, not only by how
the names are spelled.

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
    ENTITY_CONFIRM=1 / 0 decides when set. Unset: on only where it was
    measured -- when the resolution model belongs to the provider in use.
    See RE-MEASURED in the module docstring.
    """
    explicit = os.environ.get("ENTITY_CONFIRM", "").strip()
    if explicit in ("0", "1"):
        return explicit == "1"
    try:
        from brahmastra.llm import (parse_model_setting, resolution_model_setting,
                                    resolve_provider)

        parsed = parse_model_setting(resolution_model_setting())
        return bool(parsed and parsed[0] and parsed[0] == resolve_provider())
    except Exception:
        return False


def available() -> bool:
    """Whether a model can be reached at all. Never raises."""
    if not enabled():
        return False
    try:
        from brahmastra.llm import llm_available

        return llm_available()
    except Exception:
        return False


# What KIND of thing a pair is about, and the one sentence that matters for it.
#
# cocoindex's LlmPairResolver takes an `entity_type` hint -- "person",
# "technology", "organization" -- and weaves it into the prompt, on the grounds
# that a model judges names better when it knows what it is looking at. Its
# docs give the example directly: be more conservative with personal names.
#
# It takes ONE type per resolver, because there a caller resolves one column of
# one table. Here the candidates arrive mixed -- a file, a person and a
# threshold in the same batch -- so the type is INFERRED per pair and the batch
# is grouped by it. Same idea, adapted to a heterogeneous corpus.
#
# UNMEASURED, and stated as such. The judge is off by default and the numbers
# that turned it off were taken with one generic question; whether typed
# guidance moves them is exactly the experiment to run when there is quota for
# it. `brahmastra/ingest/evaluate.py` is the shape that harness should take.
_GUIDANCE = {
    "person": (
        "These are PEOPLE. Be conservative: a handle, a username or an email "
        "local-part is not the person's name, and two people can share a "
        "first name. Merge only a short and long form of one person's name."
    ),
    "path": (
        "These are FILE PATHS. Two paths are the same file only when one is a "
        "tail of the other, as 'page.tsx' is of 'app/page.tsx'. A shared "
        "directory means nothing -- files that sit together have near-"
        "identical names and are still different files."
    ),
    "identifier": (
        "These are CODE IDENTIFIERS -- functions, tools, modules, variables. "
        "Two that differ by a word are two different things: a caller and a "
        "helper, a tool and its test. Merge only a bare name and the same "
        "name with a word like 'function' or 'the' around it."
    ),
    "versioned": (
        "These carry NUMBERS -- versions, dates, thresholds, ports. The number "
        "is the fact. Different numbers mean different things, however alike "
        "the rest of the string reads."
    ),
    "general": (
        "These are general names: products, concepts, teams, systems."
    ),
}

_PATHY = re.compile(r"[\w./-]+\.[A-Za-z]{1,5}(?:\s|$)")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_DIGIT = re.compile(r"\d")
_PERSONISH = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z'-]+)+$")


def entity_type(a: str, b: str) -> str:
    """
    What kind of thing this pair is about. A hint for the prompt, never a rule.

    Checked most-specific first, and it only has to be right often enough to
    make the guidance relevant -- nothing here decides a merge, so a wrong
    guess costs a sentence of irrelevant advice rather than a wrong node.
    """
    both = (a, b)
    if all(_PATHY.search(x) for x in both):
        return "path"
    if all(_PERSONISH.match(x.strip()) for x in both):
        return "person"
    if all(_DIGIT.search(x) for x in both):
        return "versioned"
    if all(_SNAKE.search(x.lower()) for x in both):
        return "identifier"
    return "general"


def _prompt_for(kind: str) -> str:
    """The base prompt plus the one sentence this kind of name needs."""
    guidance = _GUIDANCE.get(kind) or _GUIDANCE["general"]
    parts = [SYSTEM_PROMPT, f"\nAbout this batch in particular: {guidance}"]
    extra = (os.environ.get("ENTITY_CONFIRM_GUIDANCE") or "").strip()
    if extra:
        # Domain rules only, as cocoindex puts it -- the output format is not
        # the caller's to change, and the schema is what parses the reply.
        parts.append(f"\nAlso: {extra}")
    return "\n".join(parts)


CONTEXT_PER_NAME = 2
CONTEXT_CHARS = 200


def _render(pairs: list[tuple[str, str]],
            context: dict[str, list[str]] | None = None) -> str:
    """
    The pairs, each name followed by the sentences it was used in.

    THE EVIDENCE A PERSON USES. The first measurement named this as the most
    promising lever: "giving the judge the SENTENCES the two names appeared
    in, which is the one piece of evidence a human uses here and this prompt
    withholds." Two names can read alike and be used as plainly different
    things; the sentences are where that shows.
    """
    lines = []
    for i, (a, b) in enumerate(pairs, start=1):
        lines.append(f"{i}. {a!r}  ||  {b!r}")
        for name in (a, b):
            for quote in (context or {}).get(name, [])[:CONTEXT_PER_NAME]:
                lines.append(f"     {name!r} used in: \"{quote[:CONTEXT_CHARS]}\"")
    return "\n".join(lines)


class Unanswered(Exception):
    """The model never gave a verdict. NOT the same as a verdict of `no`."""


def _read_verdicts(raw: str, count: int) -> dict[int, bool]:
    """
    Whatever the reply actually answered. Raises Unanswered if that is nothing.

    Only verdicts about pairs we asked about: a model that invents a pair 9 in
    a batch of 8 must not decide anything.
    """
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise Unanswered(f"unreadable reply: {exc}"[:200]) from exc

    out: dict[int, bool] = {}
    for verdict in payload.get("verdicts") or []:
        try:
            number = int(verdict["pair"])
            if 1 <= number <= count:
                out[number] = bool(verdict["same"])
        except Exception:
            continue
    if not out:
        raise Unanswered("reply contained no usable verdicts")
    return out


def _active_model() -> str:
    """
    Which model is answering, for the cache key. Never raises.

    RESOLVED ONCE PER `confirm`, not once per batch, and that is not a
    micro-optimisation. Provider resolution PROBES -- it asks whether Ollama is
    up before deciding -- and against an unreachable host that probe waits out
    its timeout. Measured in the test suite, where the host is deliberately
    pointed at a closed port: 2.0 seconds per batch, on a path that otherwise
    does no I/O at all, silent because the call is wrapped in a try/except.

    A 900-mention corpus produces tens of batches. That was tens of seconds of
    waiting to re-learn an answer that cannot change mid-run.
    """
    try:
        from brahmastra.llm import active_model

        return active_model()
    except Exception:
        return ""


def _ask(pairs: list[tuple[str, str]], model: str = "",
         context: dict[str, list[str]] | None = None) -> dict[int, bool]:
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
    from brahmastra.llm import chat

    user = _render(pairs, context)
    # Every pair in a batch shares a type, because `confirm` groups them.
    system = _prompt_for(entity_type(*pairs[0]))
    key = memo.key_for(user, VARIANT, model, system)
    raw = memo.load(key)
    if raw is not None:
        return _read_verdicts(raw, len(pairs))

    # ASK, THEN RE-ASK WITH THE REASON IT FAILED.
    #
    # cocoindex's resolver validates the reply and, when it does not hold up,
    # re-prompts with explicit feedback rather than giving up -- two retries by
    # default. This version validated and then raised, which throws away a
    # model that would have got it right on being told what was wrong, and
    # turns a reply missing one verdict into eight unanswered pairs.
    #
    # A RETRY IS NOT A SECOND OPINION. It only ever happens when the reply was
    # unreadable or incomplete: a verdict that parsed is never asked again, or
    # this would quietly become "keep asking until it agrees". And it is still
    # bounded -- exhausting the budget raises Unanswered, because a judge that
    # cannot be reached must not return a verdict.
    complaint = ""
    last = Unanswered("never asked")
    for attempt in range(RETRIES + 1):
        question = user if not complaint else (
            f"{user}\n\nYour previous reply was rejected: {complaint}\n"
            f"Answer again, with exactly one verdict for each of the "
            f"{len(pairs)} pairs above, numbered 1 to {len(pairs)}."
        )
        try:
            raw = chat(system, question, json_schema=_SCHEMA,
                       temperature=0.0, max_tokens=1200)
        except Exception as exc:
            # An outage is not a bad reply; asking again will not fix it.
            raise Unanswered(f"{type(exc).__name__}: {exc}"[:200]) from exc

        try:
            verdicts = _read_verdicts(raw, len(pairs))
        except Unanswered as exc:
            last, complaint = exc, str(exc)
            continue

        missing = [n for n in range(1, len(pairs) + 1) if n not in verdicts]
        if missing and attempt < RETRIES:
            complaint = f"it gave no verdict for pair(s) {missing}"
            last = Unanswered(complaint)
            continue

        # Only a reply that is going to be USED gets cached. Caching a partial
        # one would make this run's shortfall permanent for that batch.
        if not missing:
            memo.save(key, raw, VARIANT)
        return verdicts

    raise last


def confirm(pairs: Iterable[tuple[str, str]],
            context: dict[str, list[str]] | None = None) -> tuple[
        dict[tuple[str, str], bool], list[tuple[str, str]]]:
    """Run the judge on RESOLUTION_LLM_MODEL when it is set. See `_confirm`.
    `context` maps a name to sentences it was used in (see `_render`)."""
    from brahmastra.llm import resolution_model_setting, using_model

    with using_model(resolution_model_setting()):
        return _confirm(pairs, context)


def _confirm(pairs: Iterable[tuple[str, str]],
             context: dict[str, list[str]] | None = None) -> tuple[
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

    # GROUPED BY KIND, so every batch can carry guidance that fits it. A file
    # pair and a person pair need opposite advice -- "a shared directory means
    # nothing" against "a handle is not a name" -- and a batch holding both can
    # be given neither.
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for pair in todo:
        by_kind.setdefault(entity_type(*pair), []).append(pair)

    # Once, for the whole call. See `_active_model`.
    model = _active_model()

    for kind in sorted(by_kind):
        group = by_kind[kind]
        for start in range(0, len(group), BATCH):
            batch = group[start:start + BATCH]
            try:
                answers = _ask(batch, model, context)
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
