"""
Is the quote attached to this artifact the RIGHT quote, and did the owner
actually make the commitment it cites?

ONLY THE ATTRIBUTION CHECK IS ON. The evidence ranking below it was measured
against the labelled cases, lost eleven points of recall, and is disabled --
see RANKING WAS MEASURED AND REJECTED.

`quote_is_grounded` proves a quote is real. It cannot prove it is the right
one, and that gap produced a false record in the knowledge base: a 7B wrote the
action item "Move the Q3 release date to April 15th" and cited "I'll update the
roadmap by Friday so nobody's working off the March date." Verbatim, present in
the passage, about something else -- so it passed every check and landed
looking sourced, which is worse than landing unsourced, because the citation
invites trust.

TWO THINGS THAT DID NOT WORK, BOTH MEASURED
-------------------------------------------
COSINE SIMILARITY cannot do it. The fabricated pairing and the correct one both
scored 0.40. That is not a threshold that needs tuning; it is the same number
for a right answer and a wrong one, so no threshold exists.

STRICT ENTAILMENT cannot do it either. A DeBERTa model fine-tuned on
MNLI/FEVER/ANLI returned an entailment probability of 0.00-0.02 for EVERY pair,
correct ones included. Textual entailment asks whether the claim follows from
the premise ALONE, and an artifact legitimately compresses and sharpens: the
quote says "the 27th", the statement says "April 27th", so the model returns
neutral and refuses a true finding. An LLM asked the same question in prose
made the same class of mistake -- it refused "the release moved from March 30th
to April 15th" because the cited quote did not restate March 30th.

RANKING WAS MEASURED AND REJECTED -- IT IS OFF BY DEFAULT
---------------------------------------------------------
The third idea was to stop asking an absolute question and ask a relative one:
"among the sentences in this passage, is the one the model cited the best
evidence for this claim?" Every candidate is scored on the same scale, so the
scale cancels. A six-layer cross-encoder (~80MB, CPU, milliseconds) scored a
hand-built probe of seven pairs beautifully:

    correct pairings      +4.07 to +8.56   (ranked 1st or 2nd)
    wrong quote           -10.25, -11.34   (ranked 7th and 13th)

Fourteen points of clean air. On the labelled cases it cost ELEVEN POINTS OF
RECALL -- 69% [64-79] down to 58% [55-64] -- and caught nothing the cheaper
checks had not already caught. Scored against the real distribution:

    TRUE artifacts        -5.64, -1.94, -11.28, -5.82
    FABRICATED artifact   -10.76

The ranges OVERLAP. A true finding scores -11.28 while the fabrication scores
-10.76, so no floor separates them, and rank does not either (true pairs at
ranks 2, 1, 7 and 2; the fabrication at rank 10).

Why the probe lied: a cross-encoder trained on MS MARCO scores how well a
passage answers a QUERY, and an artifact statement is not a query. It is an
abstracted paraphrase -- "Lack of personnel to move the reporting service" for
"I'd like to, but I don't think we have the people" -- so true pairs score
badly whenever the summary is good. The seven probe pairs happened to be ones
where statement and quote shared surface form.

That is the same error this module's own history records: a small hand-picked
sample, a clean result, a generalisation. Kept here behind INGEST_EVIDENCE=1
because a negative result is worth more written down than deleted, and because
a better-suited model -- MiniCheck and AlignScore are trained for exactly the
(document, claim) grounding setting rather than for query relevance -- may well
succeed where a relevance reranker could not. Anything that replaces it must
beat 69% [64-79] on `--compare --runs 3`, not on a handful of chosen pairs.

WHAT RANKING CANNOT SEE, AND WHY THERE IS A SECOND CHECK
--------------------------------------------------------
Relevance is blind to attribution. "Mei communicates the new release date"
cited to Sarah's "I'll own communicating that to the wider team today" ranks
FIRST, because the quote genuinely is about communicating the release date --
only the person is wrong.

An LLM caught that one, and it is the only thing it caught that this did not.
It does not need a model: a quote in the FIRST PERSON is a commitment by
whoever spoke it, and the segmenter already knows who spoke every turn. So the
owner of a first-person quote must be its speaker. Third-person assignment is
left alone, because "Raj, you own reconciliation" is Sarah speaking and Raj
owning, which is perfectly ordinary and must not be refused.

Together the two checks answered all seven probe pairs correctly, with no LLM
call. Both fail OPEN -- a missing model or an unlocatable quote allows the
artifact through -- because the deterministic checks in comprehend.py remain
the floor and this is defence in depth, not a replacement.
"""

from __future__ import annotations

import os
import re
from typing import Any

# A cited quote must score at least this, on the cross-encoder's own scale.
# Correct pairings measured +4.07 and above; wrong quotes -10.25 and below.
SCORE_FLOOR = 0.0

# ...and must be within this much of the best sentence in the passage. The
# absolute floor catches a quote that supports nothing; the margin catches one
# that supports something, but less well than an obvious alternative. Worst
# correct margin measured 0.48; best wrong margin 16.35.
MAX_MARGIN = 6.0

# Six layers, ~80MB. Trained to score how well a passage answers a query, which
# is the question being asked here.
RERANKER = os.environ.get("INGEST_EVIDENCE_MODEL", "").strip() \
    or "cross-encoder/ms-marco-MiniLM-L-6-v2"

# A commitment by whoever is speaking. Deliberately narrow: only forms where
# the speaker is unambiguously the one taking the action.
_FIRST_PERSON = re.compile(
    r"\b(?:I'?ll|I\s+will|I\s+can|I'?d\b|I'?m\s+going\s+to|I'?ve|I\s+have\s+to|"
    r"I\s+intend|let\s+me)\b",
    re.IGNORECASE,
)

_model: Any = None
_tokeniser: Any = None
_unavailable = False


def _load() -> tuple[Any, Any] | None:
    """The reranker, loaded once. None when it cannot be had."""
    global _model, _tokeniser, _unavailable
    if _unavailable:
        return None
    # OFF unless explicitly asked for. See RANKING WAS MEASURED AND REJECTED
    # in the module docstring: it cost 11 points of recall on the labelled
    # cases and caught nothing the cheaper checks did not.
    if os.environ.get("INGEST_EVIDENCE", "").strip() != "1":
        _unavailable = True
        return None
    if _model is None:
        try:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            import torch  # noqa: F401
            from transformers import (AutoModelForSequenceClassification,
                                      AutoTokenizer)
            # Same cache directory as the embedding model, which is an
            # absolute path on purpose -- a relative one once landed the 87MB
            # sentence-transformers model in backend/backend/.cache.
            from brahmastra.embeddings import cache_dir

            cache = str(cache_dir())
            # Local first, exactly as embeddings.py does it. A cached model
            # must never wait on the network, and a hub check that HANGS
            # rather than fails is not caught by an except block -- that cost
            # a thirty-minute stall once already.
            try:
                _tokeniser = AutoTokenizer.from_pretrained(
                    RERANKER, cache_dir=cache, local_files_only=True)
                _model = AutoModelForSequenceClassification.from_pretrained(
                    RERANKER, cache_dir=cache, local_files_only=True)
            except Exception:
                _tokeniser = AutoTokenizer.from_pretrained(RERANKER, cache_dir=cache)
                _model = AutoModelForSequenceClassification.from_pretrained(
                    RERANKER, cache_dir=cache)
            _model.eval()
        except Exception:
            # Fails OPEN. The deterministic checks in comprehend.py stay the
            # floor, so a missing model costs a defence rather than the run.
            _unavailable = True
            return None
    return _model, _tokeniser


def warm() -> bool:
    """Load the model now, on this thread. See mcp_server._warm_native_imports."""
    return _load() is not None


def _candidates(chunk: Any) -> list[str]:
    """Every turn in the passage, as a separate piece of candidate evidence."""
    turns = getattr(chunk, "turns", None) or []
    lines = [t.text.strip() for t in turns if getattr(t, "text", "").strip()]
    if lines:
        return lines
    return [ln.strip() for ln in getattr(chunk, "text", "").splitlines() if ln.strip()]


def _normalise(text: str) -> str:
    return re.sub(r"\W+", " ", text or "").strip().lower()


def score_evidence(statement: str, quote: str, chunk: Any) -> dict[str, Any] | None:
    """
    How well the cited quote supports the statement, relative to its passage.

    Returns the cited quote's score, the best available score, and its rank, or
    None when the model is unavailable or the quote cannot be located.
    """
    loaded = _load()
    lines = _candidates(chunk)
    if loaded is None or not lines or not statement or not quote:
        return None
    model, tokeniser = loaded

    import torch

    with torch.no_grad():
        batch = tokeniser([statement] * len(lines), lines, padding=True,
                          truncation=True, return_tensors="pt")
        scores = model(**batch).logits.squeeze(-1).tolist()
    if not isinstance(scores, list):
        scores = [scores]

    ranked = sorted(zip(scores, lines), key=lambda p: p[0], reverse=True)
    needle = _normalise(quote)
    if not needle:
        return None

    for position, (score, line) in enumerate(ranked, start=1):
        hay = _normalise(line)
        if needle in hay or hay in needle:
            return {"score": score, "best": ranked[0][0], "rank": position,
                    "best_line": ranked[0][1]}
    return None


def evidence_supports(statement: str, quote: str | None, chunk: Any) -> bool:
    """
    True unless a better piece of evidence for this statement sits in the
    same passage, or the cited one supports nothing at all.

    Fails OPEN: an unavailable model or an unlocatable quote allows it through.
    """
    if not quote:
        return True
    scored = score_evidence(statement, quote, chunk)
    if scored is None:
        return True
    if scored["score"] < SCORE_FLOOR:
        return False
    return (scored["best"] - scored["score"]) <= MAX_MARGIN


def speaker_of(quote: str, chunk: Any) -> str | None:
    """Who said the turn this quote came from, if it can be located."""
    needle = _normalise(quote)[:60]
    if not needle:
        return None
    for turn in getattr(chunk, "turns", None) or []:
        if needle in _normalise(getattr(turn, "text", "")):
            return getattr(turn, "speaker", None)
    return None


def owner_from_speaker(quote: str | None, chunk: Any) -> str | None:
    """
    Who committed, when the quote says "I" and the model left the owner blank.

    The same fact the check below polices, used forwards instead. If Mei says
    "I'll update the roadmap", the owner IS Mei -- the transcript states it and
    the segmenter already recorded it, so asking a model to infer it is work
    nobody needs to do and a chance to get it wrong.

    Only for first person. "Raj, you own reconciliation" names its owner in the
    words and is not the speaker's commitment, so it is left to the model.
    """
    if not quote or not _FIRST_PERSON.search(quote):
        return None
    return speaker_of(quote, chunk)


def attribution_is_consistent(owner: str | None, quote: str | None,
                              chunk: Any) -> bool:
    """
    A first-person commitment belongs to whoever spoke it.

    "I'll own communicating that" said by Sarah cannot support an action item
    owned by Mei. Third-person assignment is untouched: "Raj, you own
    reconciliation" is Sarah speaking and Raj owning, which is ordinary.
    """
    if not owner or not quote:
        return True
    if not _FIRST_PERSON.search(quote):
        return True
    speaker = speaker_of(quote, chunk)
    if not speaker:
        return True
    a, b = owner.strip().lower(), speaker.strip().lower()
    return a == b or a in b or b in a
