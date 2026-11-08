"""Splits a transcript into chunks for embedding and search (Step 1.2).

Consecutive speaker turns are grouped until a size budget is reached, so a question and its
answer stay together and every chunk still knows who spoke. Neighbouring chunks share one turn.
No database and no models: a pure function, easy to test.

Sizes are in characters. all-MiniLM-L6-v2 reads at most 256 tokens (about 1,300 characters);
anything longer is silently cut off, so max_chars stays well under that.
"""

import re
from dataclasses import dataclass

import transcript_parser

TARGET_CHARS = 800   # a chunk is closed once adding another turn would pass this
MAX_CHARS = 1100     # hard cap on any chunk, overlap included
MIN_TAIL_CHARS = 150  # a shorter last chunk is merged into the one before it
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    text: str                  # "Speaker: words" lines, as shown in citations
    speakers: tuple[str, ...]  # who speaks in it, in order, without repeats
    turn_start: int            # index of its first turn in the parsed transcript
    turn_end: int              # index of its last turn


@dataclass(frozen=True)
class _Piece:
    turn: int      # index of the turn this piece came from
    speaker: str
    text: str

    @property
    def line(self) -> str:
        return f"{self.speaker}: {self.text}"


def _hard_split(text: str, limit: int) -> list[str]:
    """Cuts text that has no sentence ends into word-bounded parts of at most `limit` characters."""
    parts, current = [], ""
    for word in text.split():
        while len(word) > limit:  # one "word" longer than the limit, e.g. a pasted URL
            if current:
                parts.append(current)
                current = ""
            parts.append(word[:limit])
            word = word[limit:]
        if current and len(current) + 1 + len(word) > limit:
            parts.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        parts.append(current)
    return parts


def _split_long(text: str, limit: int) -> list[str]:
    """Splits one long turn into pieces of at most `limit` characters, on sentence ends where possible."""
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for sentence in SENTENCE_END.split(text):
        for part in _hard_split(sentence, limit):
            if current and len(current) + 1 + len(part) > limit:
                pieces.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
    if current:
        pieces.append(current)
    return pieces


def _pieces(raw: str, max_chars: int) -> list[_Piece]:
    """The transcript's turns (parsed by transcript_parser), with any oversized turn split up."""
    pieces = []
    turns = transcript_parser.to_turns(raw)  # a pandas table with "speaker" and "text" columns
    for turn, row in enumerate(turns.itertuples()):
        speaker = "Unknown" if row.speaker == "UNKNOWN" else row.speaker
        limit = max_chars - len(speaker) - 2  # room for the "Speaker: " prefix
        pieces.extend(_Piece(turn, speaker, part) for part in _split_long(row.text.strip(), limit))
    return pieces


def _size(pieces: list[_Piece], start: int, end: int) -> int:
    """Characters in pieces[start..end] once joined with newlines."""
    return sum(len(p.line) for p in pieces[start:end + 1]) + (end - start)


def _group(pieces: list[_Piece], target_chars: int) -> list[tuple[int, int]]:
    """Greedy, non-overlapping groups of consecutive pieces, as (first, last) index pairs."""
    groups, start = [], 0
    while start < len(pieces):
        end = start
        while end + 1 < len(pieces) and _size(pieces, start, end + 1) <= target_chars:
            end += 1
        groups.append((start, end))
        start = end + 1
    return groups


def _merge_short_tail(pieces, groups, min_tail_chars, max_chars):
    """A tiny last chunk (e.g. just "Thanks everyone") would match everything and mean nothing."""
    if len(groups) > 1:
        (first, _), (last_start, last_end) = groups[-2], groups[-1]
        if _size(pieces, last_start, last_end) < min_tail_chars and _size(pieces, first, last_end) <= max_chars:
            return groups[:-2] + [(first, last_end)]
    return groups


def _add_overlap(pieces, groups, max_chars):
    """Each chunk after the first also starts with the previous chunk's last turn, if it fits."""
    result = [groups[0]]
    for start, end in groups[1:]:
        result.append((start - 1, end) if _size(pieces, start - 1, end) <= max_chars else (start, end))
    return result


def chunk_transcript(raw: str, target_chars: int = TARGET_CHARS, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Chunks for one transcript, in order. An empty transcript gives an empty list."""
    if target_chars > max_chars:
        raise ValueError("target_chars can't be larger than max_chars")
    pieces = _pieces(raw, max_chars)
    if not pieces:
        return []
    groups = _merge_short_tail(pieces, _group(pieces, target_chars), MIN_TAIL_CHARS, max_chars)
    chunks = []
    for start, end in _add_overlap(pieces, groups, max_chars):
        members = pieces[start:end + 1]
        chunks.append(Chunk(
            text="\n".join(p.line for p in members),
            speakers=tuple(dict.fromkeys(p.speaker for p in members)),
            turn_start=members[0].turn,
            turn_end=members[-1].turn,
        ))
    return chunks
