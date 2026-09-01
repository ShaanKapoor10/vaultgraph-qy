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

    python -m brahmastra.ingest.evaluate                  # score the current pass
    python -m brahmastra.ingest.evaluate --compare        # ... against a variant

Score the single pass, change one thing, score it again. Adopt what wins. An
architecture adopted without that is a guess wearing an org chart.

WHAT IS MEASURED, AND WHY THESE TWO NUMBERS
-------------------------------------------
  RECALL     of the artifacts a human labelled, how many were found. Low recall
             is what a specialised agent per kind would plausibly fix -- one
             pass asked to do five jobs at once has divided attention.

  PRECISION  of what was produced, how much was actually in the meeting. This
             is the one that matters more here: a missed decision is
             recoverable because the transcript is still on disk, while a
             fabricated one is a false record an organisation may act on.

Matched by similarity rather than string equality, because "move the release to
April 15th" and "the release date moves to April 15" are the same finding and
scoring them as a miss would measure phrasing instead of comprehension.
"""

from __future__ import annotations

import argparse
import json
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
from brahmastra.ingest.consolidate import consolidate, similarity
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
# in it -- see `cases/` for the format and the seeded example.
CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass
class Score:
    kind: str
    expected: int = 0
    found: int = 0
    matched: int = 0
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)

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
    """
    try:
        from brahmastra.embeddings import embed

        vectors = embed(statements)
        if vectors:
            table = dict(zip(statements, vectors))

            def cosine(a: str, b: str) -> float:
                va, vb = table.get(a), table.get(b)
                if va is None or vb is None:
                    return similarity(a, b)
                return sum(x * y for x, y in zip(va, vb))   # already L2-normalised

            return cosine, MATCH_THRESHOLD
    except Exception:
        pass
    return similarity, LEXICAL_FALLBACK_THRESHOLD


def score_against(expected: list[dict[str, Any]],
                  produced: list[Any]) -> dict[str, Score]:
    """
    Compare what was found with what a human said is there.

    Greedy one-to-one matching: an expected artifact can be claimed once, so
    producing the same finding five times cannot inflate recall.

    Matched on MEANING. Lexical similarity was measurably unable to do this --
    see MATCH_THRESHOLD -- and reported the same paraphrased risk as both a
    miss and a fabrication, which would have made every comparison between two
    architectures meaningless.
    """
    scores = {kind: Score(kind) for kind in ARTIFACT_KINDS}

    for item in expected:
        scores.setdefault(item["kind"], Score(item["kind"])).expected += 1
    for artifact in produced:
        scores.setdefault(artifact.kind, Score(artifact.kind)).found += 1

    compare, threshold = _matcher(
        [e["statement"] for e in expected] + [a.statement for a in produced]
    )

    unclaimed = list(expected)
    for artifact in produced:
        best, best_score = None, 0.0
        for candidate in unclaimed:
            if candidate["kind"] != artifact.kind:
                continue
            value = compare(candidate["statement"], artifact.statement)
            if value > best_score:
                best, best_score = candidate, value
        if best is not None and best_score >= threshold:
            unclaimed.remove(best)
            scores[artifact.kind].matched += 1
        else:
            scores[artifact.kind].spurious.append(artifact.statement[:80])

    for leftover in unclaimed:
        scores[leftover["kind"]].missed.append(leftover["statement"][:80])

    return scores


def run_case(case: dict[str, Any],
             comprehend: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Segment, comprehend, consolidate, score. One labelled transcript."""
    comprehend = comprehend or comprehend_chunk
    chunks = segment(case["transcript"])

    produced: list[Any] = []
    errors: list[str] = []
    for chunk in chunks:
        understanding = comprehend(chunk)
        if understanding.error:
            errors.append(understanding.error)
            continue
        produced.extend(understanding.artifacts)

    reduced = consolidate(produced)
    scores = score_against(case["expected"], reduced["artifacts"])

    return {
        "name": case.get("name", "unnamed"),
        "chunks": len(chunks),
        "calls": len(chunks),
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


def _report(result: dict[str, Any]) -> None:
    print(f"\n{result['name']}  —  {result['chunks']} chunks, "
          f"{result['calls']} LLM calls")
    if result["errors"]:
        print(f"  {len(result['errors'])} chunk(s) failed: {result['errors'][0][:90]}")
    print(f"  {result['raw_artifacts']} artifacts -> "
          f"{result['after_consolidation']} after consolidation "
          f"({result['merged']} merged)")
    print(f"  {'kind':<15}{'exp':>4}{'found':>7}{'recall':>9}{'prec':>8}{'f1':>8}")

    total = Score("total")
    for kind in ARTIFACT_KINDS:
        s = result["scores"].get(kind)
        if s is None or (s.expected == 0 and s.found == 0):
            continue
        total.expected += s.expected
        total.found += s.found
        total.matched += s.matched
        print(f"  {kind:<15}{s.expected:>4}{s.found:>7}"
              f"{s.recall:>9.0%}{s.precision:>8.0%}{s.f1:>8.2f}")
    print(f"  {'TOTAL':<15}{total.expected:>4}{total.found:>7}"
          f"{total.recall:>9.0%}{total.precision:>8.0%}{total.f1:>8.2f}")

    for kind in ARTIFACT_KINDS:
        s = result["scores"].get(kind)
        if s and s.missed:
            print(f"  missed {kind}: {s.missed[0]}")
        if s and s.spurious:
            # The number to watch. A fabricated decision is a false record, and
            # unlike a missed one it is not recoverable from the transcript.
            print(f"  SPURIOUS {kind}: {s.spurious[0]}")


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
    args = parser.parse_args(argv)

    directory = Path(args.cases) if args.cases else CASES_DIR
    cases = load_cases(directory)
    if not cases:
        print(f"no labelled cases in {directory}. Add one -- see the module "
              f"docstring for why this comes before the next architecture.",
              file=sys.stderr)
        return 1

    variants = {"single": comprehend_chunk, "focused": comprehend_chunk_focused}

    if args.compare:
        rule = "=" * 62
        for name, fn in variants.items():
            print(f"\n{rule}\n{name.upper()} PASS\n{rule}")
            for case in cases:
                _report(run_case(case, comprehend=fn))
        print("\nTwice the calls has to be paid for in recall. If it is not, "
              "the single pass wins on cost alone.")
        return 0

    results = [run_case(case, comprehend=variants[args.variant]) for case in cases]

    if args.json:
        print(json.dumps([{
            "name": r["name"], "calls": r["calls"],
            "scores": {k: {"expected": s.expected, "found": s.found,
                           "matched": s.matched, "recall": round(s.recall, 3),
                           "precision": round(s.precision, 3)}
                       for k, s in r["scores"].items() if s.expected or s.found},
        } for r in results], indent=2))
        return 0

    for result in results:
        _report(result)
    print("\nChange one thing, run this again, adopt what wins. Watch SPURIOUS "
          "hardest:\na missed decision is recoverable from the transcript, a "
          "fabricated one is not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
