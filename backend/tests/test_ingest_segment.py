"""
Chunking is where the meaning is won or lost.

Everything downstream reads chunks, never the transcript, so a bad cut is
invisible from that point on: the model faithfully comprehends a fragment and
produces a confident, wrong artifact. These tests are exhaustive on purpose --
segmentation is the one part of ingestion that can be verified completely
without spending an LLM call.
"""
from __future__ import annotations

import pytest

from brahmastra.ingest.segment import (
    CHARS_PER_TOKEN,
    Turn,
    chunk_turns,
    detect_format,
    parse_turns,
    segment,
)

SPEAKER = """\
Sarah: Let's talk about the March release.
Mei: I don't think we should ship in March.
Sarah: Why not?
Mei: The payments integration isn't done, and Priya is out until the 20th.
"""

TIMESTAMPED = """\
[00:01:12] Sarah: Let's talk about the March release.
[00:01:20] Mei: I don't think we should ship in March.
"""

VTT = """\
WEBVTT

00:00:01.000 --> 00:00:04.000
Sarah: Let's talk about the March release.

00:00:04.500 --> 00:00:09.000
Mei: I don't think we should ship in March.
"""

PROSE = """\
The team met to discuss the March release.

Consensus was that payments would not be ready, so the date moves to April.
"""


# ---------------------------------------------------------------------------
# Reading whatever arrived
# ---------------------------------------------------------------------------

def test_speaker_lines_are_detected():
    assert detect_format(SPEAKER) == "speaker"


def test_timestamped_speaker_lines_are_detected():
    assert detect_format(TIMESTAMPED) == "speaker"


def test_vtt_is_detected():
    assert detect_format(VTT) == "vtt"


def test_unstructured_text_is_detected_as_prose():
    assert detect_format(PROSE) == "prose"


def test_the_format_is_never_configured(monkeypatch):
    """
    Someone pasting a transcript should not have to know which tool made it,
    and guessing wrong yields one enormous turn holding the whole meeting.
    """
    for text in (SPEAKER, TIMESTAMPED, VTT, PROSE):
        assert len(parse_turns(text)) >= 2


def test_speakers_and_text_are_separated():
    turns = parse_turns(SPEAKER)
    assert [t.speaker for t in turns] == ["Sarah", "Mei", "Sarah", "Mei"]
    assert turns[1].text == "I don't think we should ship in March."


def test_timestamps_are_kept():
    turns = parse_turns(TIMESTAMPED)
    assert turns[0].timestamp == "00:01:12"
    assert turns[0].speaker == "Sarah"


def test_vtt_cues_become_turns_with_their_start_time():
    turns = parse_turns(VTT)
    assert len(turns) == 2
    assert turns[0].timestamp == "00:00:01.000"
    assert turns[0].speaker == "Sarah"
    assert "March release" in turns[0].text


def test_a_wrapped_paragraph_stays_one_turn():
    """
    A continuation line belongs to the turn above it. Treating each line as its
    own turn fragments the sentences this whole design exists to keep whole.
    """
    wrapped = "Sarah: We looked at the numbers\nand they do not support a March date.\nMei: Agreed.\n"
    turns = parse_turns(wrapped)
    assert len(turns) == 2
    assert "numbers and they do not support" in turns[0].text


def test_a_colon_in_ordinary_prose_is_not_read_as_a_speaker():
    """Otherwise half the sentences in a prose transcript become speakers."""
    assert detect_format("The plan was simple: ship in March.\n\nIt was not.") == "prose"


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def _turns(n: int, words: int = 40) -> list[Turn]:
    body = " ".join(["word"] * words)
    return [Turn(f"P{i}", None, f"{body} {i}", i * 100, i * 100 + 100) for i in range(n)]


def test_every_chunk_fits_the_budget():
    """The one hard requirement: a chunk over budget 413s and is never retried."""
    chunks = chunk_turns(_turns(60), max_tokens=200, overlap_turns=0)
    assert chunks
    for c in chunks:
        assert c.token_estimate() <= 200 * 1.1, "a chunk exceeded the token budget"


def test_a_turn_is_never_split_across_chunks():
    """
    Splitting mid-turn is how "I don't think we should ship in March" becomes
    "we should ship in March" in the next chunk.
    """
    turns = _turns(30)
    chunks = chunk_turns(turns, max_tokens=200, overlap_turns=0)
    rendered = [t.rendered for c in chunks for t in c.turns]
    for original in turns:
        assert original.rendered in rendered


def test_nothing_is_dropped():
    turns = _turns(30)
    chunks = chunk_turns(turns, max_tokens=200, overlap_turns=0)
    assert sum(len(c.turns) for c in chunks) == len(turns)


def test_chunks_overlap_so_a_decision_can_span_a_boundary():
    """
    One person proposes and another agrees two turns later. Without overlap the
    chunk with the proposal has no agreement and the chunk with the agreement
    has no subject, and neither yields a usable decision.
    """
    chunks = chunk_turns(_turns(40), max_tokens=200, overlap_turns=2)
    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:]):
        assert following.overlap == 2
        carried = [t.rendered for t in following.turns[:2]]
        assert carried == [t.rendered for t in previous.turns[-2:]]


def test_overlap_can_be_switched_off():
    chunks = chunk_turns(_turns(40), max_tokens=200, overlap_turns=0)
    assert all(c.overlap == 0 for c in chunks)


def test_a_single_oversized_turn_is_split_on_sentences():
    """
    Splitting on characters cuts mid-word and mid-number, and a model reading
    "we agreed to 3" where the source said "we agreed to 30%" produces a
    confidently wrong artifact.
    """
    long_turn = Turn("Sarah", None, " ".join(f"Sentence number {i}." for i in range(300)), 0, 0)
    chunks = chunk_turns([long_turn], max_tokens=200, overlap_turns=0)

    assert len(chunks) > 1
    for c in chunks:
        assert c.token_estimate() <= 200 * 1.2
    rebuilt = " ".join(t.text for c in chunks for t in c.turns)
    assert "Sentence number 0." in rebuilt
    assert "Sentence number 299." in rebuilt
    assert "numbe r" not in rebuilt


def test_an_oversized_turn_keeps_its_speaker_in_every_piece():
    """A fragment with no attribution cannot be cited."""
    long_turn = Turn("Mei", "00:04:00", " ".join(f"Sentence {i}." for i in range(300)), 0, 0)
    chunks = chunk_turns([long_turn], max_tokens=200, overlap_turns=0)
    for c in chunks:
        for t in c.turns:
            assert t.speaker == "Mei"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_a_chunk_reports_who_spoke_in_it():
    chunk = segment(SPEAKER, max_tokens=4000)[0]
    assert chunk.speakers == ["Sarah", "Mei"]


def test_a_chunk_reports_its_time_span():
    chunk = segment(TIMESTAMPED, max_tokens=4000)[0]
    assert chunk.start_time == "00:01:12"
    assert chunk.end_time == "00:01:20"


def test_a_chunk_keeps_its_place_in_the_original():
    """A decision nobody can trace back to what was said is a claim, not a citation."""
    chunks = segment(SPEAKER, max_tokens=4000)
    assert chunks[0].start_char == 0
    assert chunks[0].end_char > 0


def test_the_rendered_text_keeps_attribution():
    """The model must see who said what, or every artifact loses its owner."""
    text = segment(TIMESTAMPED, max_tokens=4000)[0].text
    assert "[00:01:12] Sarah:" in text


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------

def test_an_empty_transcript_yields_no_chunks():
    assert segment("") == []
    assert segment("   \n\n  ") == []


def test_a_transcript_smaller_than_one_chunk_stays_whole():
    chunks = segment(SPEAKER, max_tokens=4000)
    assert len(chunks) == 1


def test_a_realistic_hour_long_transcript_is_chunked_sanely():
    """
    ~60k characters, which is the case the module exists for and the case the
    existing pipeline rejects outright.
    """
    turns = [
        f"{'Sarah' if i % 2 else 'Mei'}: {' '.join(['discussion'] * 30)} point {i}."
        for i in range(400)
    ]
    chunks = segment("\n".join(turns), max_tokens=1400, overlap_turns=2)

    assert len(chunks) > 5
    for c in chunks:
        assert c.token_estimate() <= 1400 * 1.1
        assert c.speakers
    assert chunks[0].index == 0
    assert [c.index for c in chunks] == list(range(len(chunks)))
