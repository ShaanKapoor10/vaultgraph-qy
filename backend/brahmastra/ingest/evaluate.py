"""
Measure how well comprehension actually reads a meeting.

WHY THIS EXISTS BEFORE THE NEXT ARCHITECTURE
--------------------------------------------
The obvious next move is a multi-agent map step: one agent hunting decisions,
one commitments, one risks, a fourth reconciling them. It might be a real
improvement. It might also be four times the LLM calls for the same output,
which on a rate-limited tier is strictly worse -- and nobody would be able to
tell the difference, because there is no number to compare.

So this comes first. It turns "should we go multi-agent?" from a matter of
taste into a measurement:

    python -m brahmastra.ingest.evaluate                       # score current pass
    python -m brahmastra.ingest.evaluate --compare             # ... against a variant
    python -m brahmastra.ingest.evaluate --compare --runs 3    # ... with the noise shown
    python -m brahmastra.ingest.evaluate --audit               # check LABELS, no model

Score the single pass, change one thing, score it again. Adopt what wins. An
architecture adopted without that is a guess wearing an org chart.

THREE NUMBERS, AND WHY NOT TWO
------------------------------
  RECALL       of the artifacts a human labelled, how many were found. Low
               recall is what a specialised agent per kind would plausibly fix:
               one pass asked to do four jobs at once has divided attention.

  UNLABELLED   produced, grounded in a real quote, and not in the label set.
               Usually the LABELS being incomplete rather than the model being
               wrong -- a meeting contains more true statements than anyone
               bothers to write down.

  TRAPPED      produced, and matching something the case says is NOT in the
               meeting. This is the one that must be zero.

Precision used to carry both of the last two at once, which made it unusable
for the decision it exists to inform: the focused variant "lost" 16 points of
precision on a run whose extra artifacts were, on inspection, all true. A
missed decision is recoverable because the transcript is still on disk. An
unlabelled true finding costs nothing at all. A trap is a false record an
organisation may act on. Collapsing the last two into one number hid the only
serious failure inside a figure that moves for harmless reasons -- so they are
counted apart now, and `must_not_find` is finally read. It sat in the case
file, described in its own note as deliberate, and no code had ever looked at
it: the traps that were the whole point of the case were scored as nothing.

Matched by similarity rather than string equality, because "move the release to
April 15th" and "the release date moves to April 15" are the same finding and
scoring them as a miss would measure phrasing instead of comprehension.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from brahmastra.env import load_env

load_env()

from brahmastra.ingest.comprehend import (
    ARTIFACT_KINDS,
    comprehend_chunk,
    comprehend_chunk_focused,
)
from brahmastra.ingest.consolidate import (
    POLARITY_SENSITIVE,
    consolidate,
    polarity_differs,
    similarity,
)
from brahmastra.ingest.segment import segment

# A found artifact counts as the labelled one when it MEANS the same thing.
#
# Semantic, not lexical, and that is not a refinement -- lexical similarity
# cannot do this job at all. Measured on real pairs from a live run:
#
#                                        lexical   cosine
#   same risk, paraphrased                  0.42     0.78
#   same question, paraphrased              0.68     0.89
#   same decision, reworded                 0.80     0.80
#   two DIFFERENT decisions                 0.39     0.03
#   two DIFFERENT action items              0.40     0.38
#
# Lexical matches span 0.42-0.80 and non-matches span 0.39-0.40: the ranges
# OVERLAP, so no threshold separates them and the scorer was reporting the same
# finding as both a miss and a fabrication. Cosine leaves a clean gap between
# 0.38 and 0.78.
#
# The model is all-MiniLM-L6-v2, already a dependency, local, and free of any
# quota -- so the measurement never competes with the thing being measured.
MATCH_THRESHOLD = 0.60

# Used only when embeddings are unavailable. Deliberately high, because on this
# evidence lexical matching is unreliable and should fail towards "not a match"
# rather than quietly inflating recall.
LEXICAL_FALLBACK_THRESHOLD = 0.75

# Where labelled cases live. A case is a transcript plus what a human says is
# in it -- see `cases/` for the format and the seeded examples.
CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class Score:
    kind: str
    expected: int = 0
    found: int = 0
    matched: int = 0
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    trapped: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.found if self.found else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _matcher(statements: list[str]) -> tuple[Callable[[str, str], float], float]:
    """
    Return a similarity function and the threshold that goes with it.

    Embeds everything in ONE batch and caches, so scoring is a handful of
    vector dot products rather than a model call per comparison.

    Callers pass the result through `for_kind` before using it, which applies
    the polarity guard where negation is load-bearing. Both similarity measures
    here are blind to negation, and on this data one is blind in a way that
    inverts the result: the seeded case has a trap, "migrate the reporting
    service this quarter", whose nearest LABEL is "leave the reporting service
    migration out of Q3" -- the decision the meeting actually made, and the
    trap's exact opposite. Cosine put them at 0.72, comfortably over the
    threshold, so a model that read the meeting correctly was scored as having
    fabricated the one thing the case was built to catch.
    """
    try:
        from brahmastra.embeddings import embed

        unique = list(dict.fromkeys(s for s in statements if s))
        vectors = embed(unique) if unique else None
        if vectors:
            table = dict(zip(unique, vectors))

            def cosine(a: str, b: str) -> float:
                va, vb = table.get(a), table.get(b)
                if va is None or vb is None:
                    return similarity(a, b)
                return sum(x * y for x, y in zip(va, vb))   # already L2-normalised

            return cosine, MATCH_THRESHOLD
    except Exception:
        pass
    return similarity, LEXICAL_FALLBACK_THRESHOLD


def for_kind(compare: Callable[[str, str], float],
             kind: str) -> Callable[[str, str], float]:
    """
    The similarity function to use when scoring artifacts of `kind`.

    Adds the polarity guard for decisions and commitments, where "we are not
    doing X" is the opposite record rather than a rephrasing of it, and leaves
    risks and open questions alone, where negation is normally descriptive.
    See POLARITY_SENSITIVE for why that line is drawn there.
    """
    if kind not in POLARITY_SENSITIVE:
        return compare

    def guarded(a: str, b: str) -> float:
        return 0.0 if polarity_differs(a, b) else compare(a, b)

    return guarded


def trap_entries(traps: list[Any] | None) -> list[tuple[str | None, str]]:
    """
    Normalise `must_not_find` into (kind or None, statement) pairs.

    A trap may be a bare string, meaning "this must not appear as ANYTHING",
    or `{"kind": ..., "statement": ...}`, meaning it must not appear as that
    kind in particular. The typed form exists because the most valuable traps
    are MODAL: the meeting genuinely discussed moving off the vendor, and
    recording it as a DECISION is the failure, while recording it as an open
    question is the correct reading. Untyped, that trap collides with the
    label it is meant to be told apart from, and the case cannot express the
    distinction it was written to test.
    """
    entries: list[tuple[str | None, str]] = []
    for trap in traps or []:
        if isinstance(trap, dict):
            entries.append((trap.get("kind"), trap["statement"]))
        else:
            entries.append((None, trap))
    return entries


def score_against(expected: list[dict[str, Any]],
                  produced: list[Any],
                  traps: list[Any] | None = None) -> dict[str, Score]:
    """
    Compare what was found with what a human said is there.

    Greedy one-to-one matching: an expected artifact can be claimed once, so
    producing the same finding five times cannot inflate recall.

    Anything unmatched is then checked against `traps` -- the case's
    `must_not_find` list, statements a human confirmed are NOT in the meeting.
    A trap hit is a real fabrication and is counted apart from a merely
    unlabelled finding, because those two failures are worth wildly different
    amounts and one number could not carry both.

    An untyped trap is matched WITHOUT regard to kind -- a model that reports
    the uncommitted migration as a risk rather than as a decision has still
    recorded something that did not happen. A typed trap is checked only
    against its own kind; see `trap_entries`.

    Matched on MEANING. Lexical similarity was measurably unable to do this --
    see MATCH_THRESHOLD -- and reported the same paraphrased risk as both a
    miss and a fabrication, which would have made every comparison between two
    architectures meaningless.
    """
    entries = trap_entries(traps)
    scores = {kind: Score(kind) for kind in ARTIFACT_KINDS}

    for item in expected:
        scores.setdefault(item["kind"], Score(item["kind"])).expected += 1
    for artifact in produced:
        scores.setdefault(artifact.kind, Score(artifact.kind)).found += 1

    compare, threshold = _matcher(
        [e["statement"] for e in expected]
        + [a.statement for a in produced]
        + [statement for _, statement in entries]
    )

    unclaimed = list(expected)
    for artifact in produced:
        against = for_kind(compare, artifact.kind)
        best, best_score = None, 0.0
        for candidate in unclaimed:
            if candidate["kind"] != artifact.kind:
                continue
            value = against(candidate["statement"], artifact.statement)
            if value > best_score:
                best, best_score = candidate, value
        if best is not None and best_score >= threshold:
            unclaimed.remove(best)
            scores[artifact.kind].matched += 1
            continue

        worst_trap, trap_score = None, 0.0
        for kind, statement in entries:
            if kind is not None and kind != artifact.kind:
                continue
            value = against(statement, artifact.statement)
            if value > trap_score:
                worst_trap, trap_score = statement, value
        if worst_trap is not None and trap_score >= threshold:
            scores[artifact.kind].trapped.append(artifact.statement[:80])
        else:
            scores[artifact.kind].spurious.append(artifact.statement[:80])

    for leftover in unclaimed:
        scores[leftover["kind"]].missed.append(leftover["statement"][:80])

    return scores


def totals(scores: dict[str, Score]) -> Score:
    """Roll the per-kind scores into one, for reporting and aggregation."""
    total = Score("total")
    for s in scores.values():
        total.expected += s.expected
        total.found += s.found
        total.matched += s.matched
        total.trapped.extend(s.trapped)
        total.spurious.extend(s.spurious)
        total.missed.extend(s.missed)
    return total


def run_case(case: dict[str, Any],
             comprehend: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Segment, comprehend, consolidate, score. One labelled transcript."""
    comprehend = comprehend or comprehend_chunk
    chunks = segment(case["transcript"])

    produced: list[Any] = []
    errors: list[str] = []
    calls = 0
    for chunk in chunks:
        understanding = comprehend(chunk)
        # What it COST, as reported by the variant that paid it. Counting
        # chunks made `focused` and `single` report the same cost, which is
        # the one number the comparison cannot do without: the question is
        # never "is it better" but "is it better enough to be worth twice the
        # calls", and on a rate-limited tier that is the whole decision.
        calls += getattr(understanding, "calls", 1)
        if understanding.error:
            errors.append(understanding.error)
            continue
        produced.extend(understanding.artifacts)

    reduced = consolidate(produced)
    scores = score_against(case["expected"], reduced["artifacts"],
                           case.get("must_not_find"))

    return {
        "name": case.get("name", "unnamed"),
        "chunks": len(chunks),
        "calls": calls,
        "errors": errors,
        "raw_artifacts": len(produced),
        "after_consolidation": len(reduced["artifacts"]),
        "merged": reduced["merged"],
        "scores": scores,
    }


def load_cases(directory: Path | None = None) -> list[dict[str, Any]]:
    directory = directory or CASES_DIR
    cases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("name", path.stem)
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Auditing the labels, which every number above depends on
# ---------------------------------------------------------------------------

def audit_case(case: dict[str, Any], threshold: float | None = None) -> list[str]:
    """
    Check a label set can actually be scored against. No model involved.

    Two ways a case lies about a model, both invisible without this:

      * TWO LABELS OF ONE KIND THAT MEAN THE SAME THING. Matching is greedy and
        one-to-one, so an artifact answering either can be credited to whichever
        scores marginally higher: which of the two is then reported as missed is
        arbitrary, and a model that found one of them can be recorded as having
        found the other. Either the two labels are one fact written twice, or
        they need phrasing that tells them apart.
      * A TRAP THAT RESEMBLES A LABEL. Then one output is both the right answer
        and a fabrication, and which it scores as comes down to a rounding
        error in cosine.

    Returns the problems, empty when the case is sound. Worth running whenever
    labels are added -- growing a case is precisely when these appear.
    """
    expected = case.get("expected", [])
    entries = trap_entries(case.get("must_not_find"))
    statements = ([e["statement"] for e in expected]
                  + [statement for _, statement in entries])
    compare, default_threshold = _matcher(statements)
    limit = threshold if threshold is not None else default_threshold

    problems: list[str] = []
    for i, a in enumerate(expected):
        for b in expected[i + 1:]:
            if a["kind"] != b["kind"]:
                continue
            score = for_kind(compare, a["kind"])(a["statement"], b["statement"])
            if score >= limit:
                problems.append(
                    f"two {a['kind']} labels are confusable ({score:.2f}), so an "
                    f"artifact answering one can be credited to the other:\n"
                    f"      - {a['statement']}\n      - {b['statement']}"
                )
    for kind, trap in entries:
        for item in expected:
            if kind is not None and kind != item["kind"]:
                continue
            score = for_kind(compare, item["kind"])(trap, item["statement"])
            if score >= limit:
                label = f"{kind} trap" if kind else "trap"
                problems.append(
                    f"a {label} resembles a real {item['kind']} label ({score:.2f}), "
                    f"so one output scores as both correct and fabricated:\n"
                    f"      trap  - {trap}\n      label - {item['statement']}"
                )
    return problems


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _report(result: dict[str, Any]) -> None:
    print(f"\n{result['name']}  —  {result['chunks']} chunks, "
          f"{result['calls']} LLM calls")
    if result["errors"]:
        print(f"  {len(result['errors'])} chunk(s) failed: {result['errors'][0][:90]}")
    print(f"  {result['raw_artifacts']} artifacts -> "
          f"{result['after_consolidation']} after consolidation "
          f"({result['merged']} merged)")
    print(f"  {'kind':<15}{'exp':>4}{'found':>7}{'recall':>9}{'prec':>8}"
          f"{'unlab':>7}{'TRAP':>6}")

    for kind in ARTIFACT_KINDS:
        s = result["scores"].get(kind)
        if s is None or (s.expected == 0 and s.found == 0):
            continue
        print(f"  {kind:<15}{s.expected:>4}{s.found:>7}"
              f"{s.recall:>9.0%}{s.precision:>8.0%}"
              f"{len(s.spurious):>7}{len(s.trapped):>6}")

    total = totals(result["scores"])
    print(f"  {'TOTAL':<15}{total.expected:>4}{total.found:>7}"
          f"{total.recall:>9.0%}{total.precision:>8.0%}"
          f"{len(total.spurious):>7}{len(total.trapped):>6}")

    for kind in ARTIFACT_KINDS:
        s = result["scores"].get(kind)
        if s and s.missed:
            print(f"  missed {kind}: {s.missed[0]}")
        if s and s.spurious:
            # Usually the label set being incomplete rather than a model error.
            # Worth reading; not worth panicking about.
            print(f"  unlabelled {kind}: {s.spurious[0]}")
        if s and s.trapped:
            # The number that matters. This one IS a false record.
            print(f"  !! TRAP HIT {kind}: {s.trapped[0]}")


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Mean and range across repeated runs of one configuration.

    The range is the point. The same Groq single-pass configuration scored 64%
    recall on one run and 36% on the next, which means a single number from a
    single run cannot support a claim about an architecture -- and two of those
    numbers compared side by side is worse, because it looks like evidence.
    """
    per_run = [totals(r["scores"]) for r in runs]
    recalls = [t.recall for t in per_run]
    precisions = [t.precision for t in per_run]
    traps = [len(t.trapped) for t in per_run]
    return {
        "runs": len(runs),
        "recall": (statistics.mean(recalls), min(recalls), max(recalls)),
        "precision": (statistics.mean(precisions), min(precisions), max(precisions)),
        "traps": (statistics.mean(traps), min(traps), max(traps)),
        "calls": sum(r["calls"] for r in runs),
    }


def _report_spread(label: str, agg: dict[str, Any]) -> None:
    r_mean, r_lo, r_hi = agg["recall"]
    p_mean, p_lo, p_hi = agg["precision"]
    t_mean, _, t_hi = agg["traps"]
    print(f"  {label:<9}{agg['runs']:>3} runs  "
          f"recall {r_mean:>4.0%} [{r_lo:.0%}-{r_hi:.0%}]  "
          f"prec {p_mean:>4.0%} [{p_lo:.0%}-{p_hi:.0%}]  "
          f"traps {t_mean:>4.1f} (worst {t_hi})  "
          f"{agg['calls']} calls")


def _run_audit(cases: list[dict[str, Any]]) -> int:
    bad = 0
    for case in cases:
        problems = audit_case(case)
        status = "ok" if not problems else f"{len(problems)} problem(s)"
        print(f"\n{case['name']}: {len(case['expected'])} labels, "
              f"{len(case.get('must_not_find', []))} traps — {status}")
        for problem in problems:
            bad += 1
            print(f"    {problem}")
    if bad:
        print(f"\n{bad} problem(s). These cap recall or make a correct answer "
              f"score as a fabrication;\nfix the labels, not the model.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brahmastra.ingest.evaluate",
        description="Score comprehension against labelled transcripts.",
    )
    parser.add_argument("--cases", default=None, help="directory of labelled cases")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--variant", choices=("single", "focused"), default="single",
                        help="single pass, or two specialised passes")
    parser.add_argument("--compare", action="store_true",
                        help="run both variants and print them side by side")
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat each configuration N times and show the "
                             "spread. One run cannot tell a real difference "
                             "from sampling noise.")
    parser.add_argument("--audit", action="store_true",
                        help="check the label sets are scoreable; runs no model")
    args = parser.parse_args(argv)

    directory = Path(args.cases) if args.cases else CASES_DIR
    cases = load_cases(directory)
    if not cases:
        print(f"no labelled cases in {directory}. Add one -- see the module "
              f"docstring for why this comes before the next architecture.",
              file=sys.stderr)
        return 1

    if args.audit:
        return _run_audit(cases)

    variants = {"single": comprehend_chunk, "focused": comprehend_chunk_focused}
    chosen = variants if args.compare else {args.variant: variants[args.variant]}

    all_runs: dict[str, list[dict[str, Any]]] = {}
    for name, fn in chosen.items():
        if args.compare and not args.json:
            rule = "=" * 66
            print(f"\n{rule}\n{name.upper()} PASS\n{rule}")
        collected: list[dict[str, Any]] = []
        for attempt in range(args.runs):
            for case in cases:
                result = run_case(case, comprehend=fn)
                if args.runs > 1:
                    result["name"] = f"{result['name']} (run {attempt + 1})"
                collected.append(result)
                if not args.json:
                    _report(result)
        all_runs[name] = collected

    if args.json:
        print(json.dumps({
            name: [{
                "name": r["name"], "calls": r["calls"],
                "scores": {k: {"expected": s.expected, "found": s.found,
                               "matched": s.matched, "recall": round(s.recall, 3),
                               "precision": round(s.precision, 3),
                               "unlabelled": len(s.spurious),
                               "trapped": len(s.trapped)}
                           for k, s in r["scores"].items() if s.expected or s.found},
            } for r in runs] for name, runs in all_runs.items()
        }, indent=2))
        return 0

    if args.runs > 1 or args.compare:
        print(f"\n{'=' * 66}\nSUMMARY\n{'=' * 66}")
        for name, runs in all_runs.items():
            _report_spread(name, _aggregate(runs))

    if args.compare:
        print("\nTwice the calls has to be paid for in RECALL. Judge the cost "
              "by the TRAP column,\nnot the unlabelled one: an unlabelled "
              "finding is usually an incomplete label set,\na trap is a false "
              "record. If the ranges overlap, --runs more before believing it.")
    else:
        print("\nChange one thing, run this again, adopt what wins. Watch TRAP "
              "hardest:\na missed decision is recoverable from the transcript, "
              "a fabricated one is not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
