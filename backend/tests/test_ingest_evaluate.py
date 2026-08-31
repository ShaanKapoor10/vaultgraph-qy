"""
The scorer has to be trustworthy before its numbers can settle an argument.

This exists so "should comprehension be multi-agent?" stops being a matter of
taste. That only works if the measurement is sound -- a scorer that inflates
recall would justify whatever architecture happened to be tried next.
"""
from __future__ import annotations

from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
from brahmastra.ingest.evaluate import load_cases, run_case, score_against


def art(kind, statement, **kw):
    return Artifact(kind=kind, statement=statement, **kw)


def test_a_perfect_run_scores_perfectly():
    expected = [{"kind": "decision", "statement": "Move the release to April 15th"}]
    scores = score_against(expected, [art("decision", "Move the release to April 15th")])
    assert scores["decision"].recall == 1.0
    assert scores["decision"].precision == 1.0


def test_a_paraphrase_still_counts_as_found():
    """Otherwise this measures phrasing rather than comprehension."""
    scores = score_against(
        [{"kind": "decision", "statement": "Move the release to April 15th"}],
        [art("decision", "The release date moves to April 15")],
    )
    assert scores["decision"].matched == 1


def test_a_missed_artifact_lowers_recall_and_is_named():
    scores = score_against(
        [{"kind": "risk", "statement": "The Acme contract assumes March delivery"}], []
    )
    assert scores["risk"].recall == 0.0
    assert scores["risk"].missed


def test_a_fabricated_artifact_lowers_precision_and_is_named():
    """The number to watch: a missed decision is recoverable from the
    transcript, a fabricated one is not."""
    scores = score_against([], [art("decision", "Approve a 40% budget increase")])
    assert scores["decision"].precision == 0.0
    assert scores["decision"].spurious


def test_repeating_one_finding_cannot_inflate_recall():
    """
    Greedy one-to-one matching. Without it, a model that emits the same
    decision five times would score 500% recall and look like an improvement.
    """
    expected = [{"kind": "decision", "statement": "Move the release to April 15th"}]
    produced = [art("decision", "Move the release to April 15th") for _ in range(5)]
    scores = score_against(expected, produced)

    assert scores["decision"].matched == 1
    assert scores["decision"].recall == 1.0
    assert scores["decision"].precision == 0.2


def test_the_wrong_kind_is_not_a_match():
    """A risk about a decision's subject is a different finding."""
    scores = score_against(
        [{"kind": "decision", "statement": "Move the release to April"}],
        [art("risk", "Move the release to April")],
    )
    assert scores["decision"].matched == 0
    assert scores["risk"].spurious


def test_the_seeded_case_loads_and_is_labelled():
    cases = load_cases()
    assert cases, "the seeded evaluation case is missing"
    case = cases[0]
    assert case["transcript"].strip()
    assert len(case["expected"]) >= 8
    assert {"decision", "action_item", "risk", "open_question"} <= {
        item["kind"] for item in case["expected"]
    }


def test_a_case_can_be_run_without_a_provider():
    """The harness must be exercisable offline, or nobody will run it."""
    case = load_cases()[0]

    def perfect(chunk, max_tokens=None):
        return ChunkUnderstanding(
            chunk_index=chunk.index,
            artifacts=[art(e["kind"], e["statement"], chunk_index=chunk.index)
                       for e in case["expected"]] if chunk.index == 0 else [],
        )

    result = run_case(case, comprehend=perfect)
    assert result["scores"]["decision"].recall == 1.0
    assert result["calls"] == result["chunks"]


def test_a_failing_chunk_is_reported_not_swallowed():
    case = load_cases()[0]

    def broken(chunk, max_tokens=None):
        return ChunkUnderstanding(chunk_index=chunk.index, error="429 rate limit")

    result = run_case(case, comprehend=broken)
    assert result["errors"]
    assert result["after_consolidation"] == 0
