"""
The scorer has to be trustworthy before its numbers can settle an argument.

This exists so "should comprehension be multi-agent?" stops being a matter of
taste. That only works if the measurement is sound -- a scorer that inflates
recall would justify whatever architecture happened to be tried next, and a
scorer that reports a correct reading as a fabrication would reject a good one.
Both have now happened here, so the checks below are regressions rather than
hypotheticals.
"""
from __future__ import annotations

from brahmastra.ingest.comprehend import Artifact, ChunkUnderstanding
from brahmastra.ingest.evaluate import (
    audit_case,
    load_cases,
    run_case,
    score_against,
)


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


# ---------------------------------------------------------------------------
# The matcher itself, which every comparison depends on
# ---------------------------------------------------------------------------

def test_a_paraphrase_lexical_similarity_cannot_see_is_still_matched():
    """
    Measured on a live run: this pair scores 0.42 lexically and 0.78 by cosine,
    while two genuinely DIFFERENT decisions score 0.39 lexically. The lexical
    ranges overlap, so no threshold separates them -- and the scorer reported
    this real finding as both a miss AND a fabrication.
    """
    scores = score_against(
        [{"kind": "risk",
          "statement": "The Acme contract assumes a March delivery, so slipping may be a breach"}],
        [art("risk", "Potential contract breach with Acme due to delayed delivery.")],
    )
    assert scores["risk"].matched == 1
    assert scores["risk"].recall == 1.0
    assert not scores["risk"].spurious


def test_two_genuinely_different_findings_are_not_matched():
    """The loosening must not make everything match everything."""
    scores = score_against(
        [{"kind": "decision", "statement": "Move the release date to April 15th"}],
        [art("decision", "Raj owns the reconciliation job")],
    )
    assert scores["decision"].matched == 0
    assert scores["decision"].spurious


def test_different_action_items_are_kept_apart():
    scores = score_against(
        [{"kind": "action_item", "statement": "Mei updates the roadmap by Friday"}],
        [art("action_item", "Sarah communicates the release date to the team")],
    )
    assert scores["action_item"].matched == 0


def test_scoring_still_works_without_embeddings(monkeypatch):
    """
    The harness must not become unrunnable because a model file is missing.
    It falls back to lexical with a deliberately high threshold, so it fails
    towards "not a match" rather than quietly inflating recall.
    """
    import brahmastra.embeddings as emb
    monkeypatch.setattr(emb, "embed", lambda texts: None)

    scores = score_against(
        [{"kind": "decision", "statement": "Move the release date to April 15th"}],
        [art("decision", "Move the release date to April 15th")],
    )
    assert scores["decision"].matched == 1


# ---------------------------------------------------------------------------
# Traps, which the case files declared and no code had ever read
# ---------------------------------------------------------------------------

def test_a_trap_is_counted_apart_from_an_unlabelled_finding():
    """
    Precision used to carry both of these at once, which made it useless for
    the decision it exists to inform: the focused variant "lost" 16 points of
    precision on a run whose extra artifacts were, on inspection, all true.

    They are worth wildly different amounts. An unlabelled finding is the label
    set being incomplete and costs nothing. A trap is a false record an
    organisation may act on. One number could not carry both, so the only
    serious failure was hidden inside a figure that moves for harmless reasons.
    """
    scores = score_against(
        expected=[{"kind": "decision", "statement": "Move the release to April 15th"}],
        produced=[art("decision", "Move the release to April 15th"),
                  art("decision", "Adopt the new deployment checklist"),
                  art("decision", "Approve a 40% budget increase")],
        traps=["Approve a 40% budget increase"],
    )
    assert scores["decision"].matched == 1
    assert len(scores["decision"].trapped) == 1, "the trap was not detected"
    assert len(scores["decision"].spurious) == 1, "an unlabelled finding was miscounted"


def test_a_trap_is_recognised_when_it_is_paraphrased():
    """A fabrication does not have to be word-for-word to be a fabrication."""
    scores = score_against(
        expected=[],
        produced=[art("action_item", "Priya finishes the payments integration")],
        traps=["Priya completes the payments integration"],
    )
    assert scores["action_item"].trapped


def test_an_untyped_trap_fires_whatever_kind_it_is_reported_as():
    """Recording an uncommitted migration as a risk is still recording
    something that did not happen."""
    scores = score_against(
        expected=[],
        produced=[art("risk", "Migrate the reporting service off the old cluster")],
        traps=["Migrate the reporting service off the old cluster"],
    )
    assert scores["risk"].trapped


def test_a_typed_trap_only_fires_on_its_own_kind():
    """
    The most valuable traps are MODAL. The meeting genuinely discussed moving
    off the vendor, so reporting it as an OPEN QUESTION is the correct reading
    and reporting it as a DECISION is the fabrication. Untyped, that trap
    collides with the label it exists to be told apart from, and the case
    cannot express the distinction it was written to test.
    """
    traps = [{"kind": "decision",
              "statement": "Move off the webhook vendor this quarter"}]

    wrong = score_against(
        expected=[],
        produced=[art("decision", "Move off the webhook vendor this quarter")],
        traps=traps,
    )
    assert wrong["decision"].trapped, "recording it as decided is the failure"

    right = score_against(
        expected=[{"kind": "open_question",
                   "statement": "Whether to move off the current webhook vendor"}],
        produced=[art("open_question",
                      "Whether to move off the current webhook vendor")],
        traps=traps,
    )
    assert right["open_question"].matched == 1
    assert not right["open_question"].trapped


# ---------------------------------------------------------------------------
# Negation, which cosine places almost on top of the thing it negates
# ---------------------------------------------------------------------------

def test_a_decision_is_not_matched_by_its_own_opposite():
    """
    The seeded case has a trap, "migrate the reporting service this quarter",
    whose nearest LABEL is "leave the reporting service migration out of Q3" --
    the decision the meeting actually made, and the trap's exact opposite.
    Cosine put them at 0.72, over the threshold.

    So a model that read the meeting correctly was scored as having fabricated
    the one thing the case was built to catch, and the case's own note calls
    that trap deliberate.
    """
    scores = score_against(
        expected=[{"kind": "decision",
                   "statement": "Leave the reporting service migration out of Q3"}],
        produced=[art("decision",
                      "Leave the reporting service migration out of Q3")],
        traps=[{"kind": "decision",
                "statement": "Migrate the reporting service off the old cluster this quarter"}],
    )
    assert scores["decision"].matched == 1
    assert not scores["decision"].trapped, (
        "the correct decision was scored as its own opposite"
    )


def test_a_reversed_decision_is_not_credited_as_the_original():
    scores = score_against(
        [{"kind": "decision", "statement": "Rewrite the queue consumer"}],
        [art("decision", "Do not rewrite the queue consumer")],
    )
    assert scores["decision"].matched == 0


def test_a_negated_risk_still_matches_its_paraphrase():
    """
    The guard is scoped to decisions and commitments. In a risk, negation is
    normally descriptive, and guarding it would report one finding as both
    missed and invented -- the exact failure the lexical scorer used to make.
    """
    scores = score_against(
        [{"kind": "risk",
          "statement": "Refunds and reconciliation remain unstarted"}],
        [art("risk", "Refunds and the reconciliation job have not been started")],
    )
    assert scores["risk"].matched == 1
    assert not scores["risk"].spurious


# ---------------------------------------------------------------------------
# Auditing the labels, which every number above depends on
# ---------------------------------------------------------------------------

def test_the_audit_catches_two_labels_that_mean_the_same_thing():
    problems = audit_case({
        "expected": [
            {"kind": "decision", "statement": "Move the release date to April 15th"},
            {"kind": "decision", "statement": "The release date moves to April 15"},
        ],
    })
    assert problems and "confusable" in problems[0]


def test_the_audit_catches_a_trap_that_resembles_a_real_label():
    """Otherwise one output is both the right answer and a fabrication, and
    which it scores as comes down to a rounding error in cosine."""
    problems = audit_case({
        "expected": [{"kind": "risk",
                      "statement": "The Acme contract assumes a March delivery"}],
        "must_not_find": [{"kind": "risk",
                           "statement": "The Acme contract assumed a March delivery date"}],
    })
    assert problems and "both correct and fabricated" in problems[0]


def test_the_audit_passes_a_sound_case():
    assert audit_case({
        "expected": [
            {"kind": "decision", "statement": "Move the release date to April 15th"},
            {"kind": "risk", "statement": "The staging environment is flaky"},
        ],
        "must_not_find": [{"kind": "action_item",
                           "statement": "Priya completes the payments integration"}],
    }) == []


def test_every_seeded_case_is_scoreable():
    """
    A regression guard on the CASES, not on the code.

    Both defects the audit looks for were present the first time it ran: a trap
    scored 0.72 against the real decision it inverts, and two action items both
    beginning "Mei" scored 0.61 against each other. Neither is visible by
    reading the file, and both make a score say something other than what it
    appears to say.
    """
    for case in load_cases():
        assert audit_case(case) == [], f"{case.get('name')} is not scoreable"


def test_the_cost_reported_is_the_calls_made_not_the_chunks_read():
    """
    The comparison is not "is focused better" but "is it better ENOUGH to be
    worth twice the calls", and on a rate-limited tier that is the whole
    decision. Counting chunks made both variants report the same cost, so the
    denominator of that question was missing from its own report.
    """
    case = load_cases()[0]

    def two_pass(chunk, max_tokens=None):
        return ChunkUnderstanding(chunk_index=chunk.index, calls=2)

    result = run_case(case, comprehend=two_pass)
    assert result["calls"] == 2 * result["chunks"]


def test_repeated_runs_report_the_range_not_just_the_mean():
    """
    The range is the point of --runs.

    The same Groq single-pass configuration scored 64% recall on one run and
    36% on the next. Reported as a single number, either one supports a
    confident claim about an architecture; two of them side by side look like
    evidence when they are weather. A configuration whose range spans 28 points
    has not been distinguished from anything.
    """
    from brahmastra.ingest.evaluate import _aggregate

    def scored(matched):
        return {"calls": 1, "scores": {"decision": type(
            "S", (), {"expected": 4, "found": 4, "matched": matched,
                      "recall": matched / 4, "precision": matched / 4,
                      "trapped": [], "spurious": [], "missed": []})()}}

    agg = _aggregate([scored(1), scored(3)])
    mean, low, high = agg["recall"]
    assert (low, high) == (0.25, 0.75)
    assert mean == 0.5
    assert agg["runs"] == 2


def test_the_cases_cover_more_than_one_transcript_and_format():
    """
    One case cannot tell a real difference from sampling noise -- the same
    configuration scored 64% recall on one run and 36% on the next. A second
    case in a second format also means segmentation is exercised on a second
    parser rather than on a second copy of the same one.
    """
    cases = load_cases()
    assert len(cases) >= 2
    assert any(c["transcript"].lstrip().startswith("WEBVTT") for c in cases)
