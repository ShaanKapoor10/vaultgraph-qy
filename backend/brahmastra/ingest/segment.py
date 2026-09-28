"""
Cut a transcript into pieces a model can actually read.

Chunking is where most of the meaning is won or lost, so this is deliberate
rather than a character count:

  A SPEAKER TURN IS ATOMIC. Splitting mid-turn is how "I don't think we should
  ship in March" becomes "we should ship in March" in the next chunk. Turns are
  never divided, and a single turn too large to fit is split on sentence
  boundaries rather than mid-word.

  CHUNKS OVERLAP. A decision is routinely made across a boundary -- one person
  proposes, another agrees two turns later. Without overlap the chunk holding
  the proposal has no agreement and the chunk holding the agreement has no
  subject, and neither yields a usable decision.

  PROVENANCE SURVIVES. Every chunk keeps its speakers, its timestamps and its
  character span in the original, because a decision nobody can trace back to
  what was actually said is a claim, not a citation.

Formats: WebVTT, SRT, `[00:12:34] Speaker: text`, `Speaker: text`, and plain
prose with no structure at all. Detected, never configured -- a person pasting
a transcript should not have to say which tool produced it.

Deterministic and dependency-free on purpose: this is the part of ingestion
that can be tested exhaustively without spending a single LLM call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterator

# Same estimate the extraction stage budgets with, kept identical on purpose:
# two stages disagreeing about what fits is how a chunk sized here 413s there.
CHARS_PER_TOKEN = 4

# How much of a chunk may be transcript. The comprehension prompt and the JSON
# reply share the same per-minute allowance, so the text cannot claim all of it.
DEFAULT_CHUNK_TOKENS = 1400

# Turns of context repeated from the previous chunk. Two is enough to carry a
# proposal into the chunk where it is agreed to, without duplicating so much
# that the same decision is reported twice.
DEFAULT_OVERLAP_TURNS = 2

_SPEAKER_LINE = re.compile(
    r"""^\s*
    (?:[\[(]?\s*(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*[\])]?\s*)?
    (?P<speaker>[A-Z][\w.'’-]*(?:\s+[\w.'’-]+){0,3})
    \s*:\s(?P<text>.*)$
    """,
    re.VERBOSE,
)

_VTT_CUE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})"
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Turn:
    """One contiguous piece of speech by one person."""
    speaker: str | None
    timestamp: str | None
    text: str
    start_char: int
    end_char: int

    @property
    def rendered(self) -> str:
        """How the turn is shown to the model, with its attribution intact."""
        prefix = ""
        if self.timestamp and self.speaker:
            prefix = f"[{self.timestamp}] {self.speaker}: "
        elif self.speaker:
            prefix = f"{self.speaker}: "
        elif self.timestamp:
            prefix = f"[{self.timestamp}] "
        return prefix + self.text

    def token_estimate(self) -> int:
        return max(1, len(self.rendered) // CHARS_PER_TOKEN)


@dataclass
class Chunk:
    """A window of turns small enough to comprehend in one call."""
    index: int
    turns: list[Turn] = field(default_factory=list)
    # Turns repeated from the previous chunk, counted separately so the model
    # can be told which part it has already been shown.
    overlap: int = 0

    @property
    def text(self) -> str:
        return "\n".join(t.rendered for t in self.turns)

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for t in self.turns:
            if t.speaker and t.speaker not in seen:
                seen.append(t.speaker)
        return seen

    @property
    def start_time(self) -> str | None:
        return next((t.timestamp for t in self.turns if t.timestamp), None)

    @property
    def end_time(self) -> str | None:
        return next((t.timestamp for t in reversed(self.turns) if t.timestamp), None)

    @property
    def start_char(self) -> int:
        return self.turns[0].start_char if self.turns else 0

    @property
    def end_char(self) -> int:
        return self.turns[-1].end_char if self.turns else 0

    def token_estimate(self) -> int:
        return max(1, len(self.text) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Reading the transcript
# ---------------------------------------------------------------------------

def detect_format(text: str) -> str:
    """
    Which shape this transcript has. Detected rather than configured.

    Someone pasting a transcript should not have to know whether their tool
    emitted VTT or plain speaker lines, and getting it wrong silently produces
    one enormous turn containing the whole meeting.
    """
    head = text.lstrip()[:2000]
    if head.startswith("WEBVTT") or _VTT_CUE.search(head):
        return "vtt"
    if re.search(r"^\s*\d+\s*$", head, re.MULTILINE) and _VTT_CUE.search(head):
        return "srt"
    lines = [ln for ln in text.splitlines() if ln.strip()][:40]
    if lines and sum(1 for ln in lines if _SPEAKER_LINE.match(ln)) >= max(2, len(lines) // 4):
        return "speaker"
    return "prose"


# WebVTT names the speaker in a voice tag: `<v Priya Menon>Hi.</v>`, or
# `<v.loud Name>` with a class. Teams, Webex and YouTube all emit this.
_VTT_VOICE = re.compile(r"<v(?:\.[\w.-]+)?\s+([^>]+)>")
_VTT_TAG = re.compile(r"</?[a-z][^>]*>", re.I)
_VTT_SKIP_BLOCKS = ("WEBVTT", "NOTE", "STYLE", "REGION")

# Consecutive cues by one speaker are joined into one turn, up to this many
# characters. Teams cuts speech every second or two -- "Leo and Tom enter
# into a swap" / "contract." -- and a cue per turn handed the model sentence
# fragments. Capped so a lecturer's twenty-minute monologue still keeps
# timestamps every few sentences.
_CUE_MERGE_CHARS = 700


def _speaker_name(raw: str) -> str:
    """One spelling per person: Teams wrote "Priya  Menon" with two spaces."""
    return " ".join(raw.split())


def _cue_payload(lines: list[str]) -> tuple[str | None, str]:
    joined = " ".join(part.strip() for part in lines).strip()
    voice = _VTT_VOICE.search(joined)
    speaker = _speaker_name(voice.group(1)) if voice else None
    body = " ".join(_VTT_TAG.sub(" ", joined).split())
    if speaker is None:
        m = _SPEAKER_LINE.match(body)
        if m:
            speaker, body = _speaker_name(m.group("speaker")), m.group("text").strip()
    return speaker, body


def _parse_cues(text: str) -> list[Turn]:
    """
    WebVTT and SRT, read cue by cue: a block of lines separated by blank lines,
    holding an optional identifier, the timing line, then the payload.

    Only the payload is speech. Everything before the timing line is a cue
    identifier -- SRT's sequence number, or Teams' `3f1c9a20-.../13-0` -- and
    was being read as speech: in a real Teams transcript half the turns the
    model saw were UUIDs, and no turn had a speaker, because the `<v Name>`
    voice tag went unrecognised and stayed in the text.
    """
    # Driven by the TIMING lines, not by blank lines. A file saved with doubled
    # line endings ("\r\r\n", what a Windows text-mode write of a CRLF file
    # produces) puts a blank line after every line, and reading blocks by blank
    # lines then found no cue with a payload at all: zero turns, silently.
    lines: list[tuple[str, int, int]] = []          # (stripped, start, end), blanks marked ""
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        lines.append((line.strip(), offset, offset + len(body)))
        offset += len(line)
    filled = [i for i, (ln, _, _) in enumerate(lines) if ln]

    cues: list[Turn] = []
    ts: str | None = None
    payload: list[tuple[str, int, int]] = []
    skipping = False

    def flush() -> None:
        nonlocal payload
        if ts is not None and payload:
            speaker, spoken = _cue_payload([ln for ln, _, _ in payload])
            if spoken:
                cues.append(Turn(speaker, ts, spoken, payload[0][1], payload[-1][2]))
        payload = []

    for pos, i in enumerate(filled):
        ln = lines[i][0]
        if i > 0 and not lines[i - 1][0] and ln.split(" ", 1)[0] in _VTT_SKIP_BLOCKS:
            skipping = True                  # NOTE / STYLE / REGION run to a blank line
        if skipping:
            if i + 1 < len(lines) and not lines[i + 1][0]:
                skipping = False
            continue
        cue = _VTT_CUE.search(ln)
        if cue:
            flush()
            ts = cue.group("start")
            continue
        nxt = filled[pos + 1] if pos + 1 < len(filled) else None
        after_blank = i > 0 and not lines[i - 1][0]
        if (nxt is not None and _VTT_CUE.search(lines[nxt][0])
                and (ts is None or (payload and after_blank))):
            # A cue identifier, never speech: it only ever comes before the
            # first cue, or after a finished payload and a blank line. A
            # payload line that merely precedes the next timing line is speech.
            continue
        if ln.split(" ", 1)[0] == "WEBVTT":
            continue
        payload.append(lines[i])
    flush()

    turns: list[Turn] = []
    for cue in cues:
        last = turns[-1] if turns else None
        if (last is not None and last.speaker == cue.speaker
                and len(last.text) + len(cue.text) < _CUE_MERGE_CHARS):
            last.text = f"{last.text} {cue.text}"
            last.end_char = cue.end_char
        else:
            turns.append(cue)
    return turns


def _parse_speaker_lines(text: str) -> list[Turn]:
    """
    `Speaker: text`, optionally timestamped.

    Continuation lines belong to the turn above them: a paragraph wrapped over
    several lines is one utterance, and treating each line as its own turn
    fragments sentences that the whole design exists to keep whole.
    """
    turns: list[Turn] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            offset += len(line)
            continue
        m = _SPEAKER_LINE.match(stripped)
        if m:
            body = m.group("text").strip()
            turns.append(Turn(m.group("speaker"), m.group("ts"), body,
                              offset, offset + len(line)))
        elif turns:
            turns[-1].text = f"{turns[-1].text} {stripped}".strip()
            turns[-1].end_char = offset + len(line)
        else:
            turns.append(Turn(None, None, stripped, offset, offset + len(line)))
        offset += len(line)
    return turns


def _parse_prose(text: str) -> list[Turn]:
    """No structure at all: paragraphs are the closest thing to a turn."""
    turns: list[Turn] = []
    offset = 0
    for block in re.split(r"\n\s*\n", text):
        stripped = block.strip()
        if stripped:
            start = text.find(stripped, offset)
            start = start if start >= 0 else offset
            turns.append(Turn(None, None, stripped, start, start + len(stripped)))
            offset = start + len(stripped)
    return turns


def parse_turns(text: str, fmt: str | None = None) -> list[Turn]:
    """Read a transcript into turns, whatever shape it arrived in."""
    fmt = fmt or detect_format(text)
    if fmt in ("vtt", "srt"):
        return _parse_cues(text)
    if fmt == "speaker":
        return _parse_speaker_lines(text)
    return _parse_prose(text)


# ---------------------------------------------------------------------------
# Cutting it up
# ---------------------------------------------------------------------------

def _split_oversized(turn: Turn, budget_tokens: int) -> Iterator[Turn]:
    """
    A single turn larger than a whole chunk. Rare, but it happens: a prepared
    statement, or prose with no paragraph breaks.

    Split on sentence boundaries. Splitting on characters would cut mid-word
    and mid-number, and a model reading "we agreed to 3" where the source said
    "we agreed to 30%" produces a confidently wrong artifact.
    """
    limit = budget_tokens * CHARS_PER_TOKEN
    sentences = _SENTENCE_END.split(turn.text)
    part: list[str] = []
    size = 0
    cursor = turn.start_char

    for sentence in sentences:
        # A single sentence past the limit still has to go somewhere; emitting
        # it whole is better than cutting a number in half.
        if size and size + len(sentence) > limit:
            body = " ".join(part)
            yield Turn(turn.speaker, turn.timestamp, body, cursor, cursor + len(body))
            cursor += len(body)
            part, size = [], 0
        part.append(sentence)
        size += len(sentence) + 1

    if part:
        body = " ".join(part)
        yield Turn(turn.speaker, turn.timestamp, body, cursor, cursor + len(body))


def chunk_turns(
    turns: list[Turn],
    max_tokens: int | None = None,
    overlap_turns: int | None = None,
) -> list[Chunk]:
    """Group turns into overlapping windows that each fit the budget."""
    max_tokens = max_tokens or int(
        os.environ.get("INGEST_CHUNK_TOKENS", "") or DEFAULT_CHUNK_TOKENS
    )
    if overlap_turns is None:
        overlap_turns = int(
            os.environ.get("INGEST_OVERLAP_TURNS", "") or DEFAULT_OVERLAP_TURNS
        )
    overlap_turns = max(0, overlap_turns)

    expanded: list[Turn] = []
    for turn in turns:
        if turn.token_estimate() > max_tokens:
            expanded.extend(_split_oversized(turn, max_tokens))
        else:
            expanded.append(turn)

    chunks: list[Chunk] = []
    current: list[Turn] = []
    size = 0
    carried = 0

    def close() -> None:
        nonlocal current, size, carried
        if current:
            chunks.append(Chunk(index=len(chunks), turns=list(current), overlap=carried))

    for turn in expanded:
        cost = turn.token_estimate()
        if current and size + cost > max_tokens:
            close()
            # Carry the tail forward so a decision spanning the boundary is
            # visible whole in at least one chunk.
            tail = current[-overlap_turns:] if overlap_turns else []
            current = list(tail)
            carried = len(tail)
            size = sum(t.token_estimate() for t in current)
        current.append(turn)
        size += cost

    close()
    return chunks


def segment(
    text: str,
    max_tokens: int | None = None,
    overlap_turns: int | None = None,
    fmt: str | None = None,
) -> list[Chunk]:
    """Parse and chunk in one call. The entry point the pipeline uses."""
    return chunk_turns(parse_turns(text, fmt), max_tokens, overlap_turns)
