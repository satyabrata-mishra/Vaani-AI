"""Split parsed lines into pieces of ~N tokens. Pieces never cross a section boundary, so every
piece has exactly one section to cite, and each keeps the page range it came from."""
from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass

from app.ingest.parse_pdf import Line

# Measured in R1: paper text came out at 4.5-4.95 characters per Groq token. 4.5 errs high.
CHARS_PER_TOKEN = 4.5
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_WORD = re.compile(r"[A-Za-z]+")


def approx_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass
class Chunk:
    chunk_id: str
    paper_id: str
    section: str
    page_start: int
    page_end: int
    text: str
    n_tokens: int


def _join_lines(lines: list[Line], vocab: set[str]) -> tuple[str, list[int], list[int]]:
    """Join lines into one string. A trailing hyphen is removed only when the joined word also
    occurs elsewhere in the paper ("architec-" + "ture" -> "architecture"; "high-" + "quality" stays)."""
    text = ""
    offsets, pages = [], []
    for line in lines:
        t = line.text
        if text.endswith("-") and t:
            head = _WORD.findall(text[-30:])
            tail = _WORD.match(t)
            if head and tail and (head[-1] + tail.group(0)).lower() in vocab:
                text = text[:-1]
            sep = ""
        else:
            sep = " " if text else ""
        offsets.append(len(text) + len(sep))
        pages.append(line.page)
        text += sep + t
    return text, offsets, pages


def _sentences(text: str, offsets: list[int], pages: list[int]):
    pos = 0
    for s in _SENT_SPLIT.split(text):
        start = text.find(s, pos)
        pos = start + len(s)
        i = max(0, bisect.bisect_right(offsets, start) - 1)
        j = max(0, bisect.bisect_right(offsets, max(start, pos - 1)) - 1)
        yield s, pages[i], pages[j]


def _split_long(sentence: str, max_tokens: int) -> list[str]:
    """Tables and equations often have no sentence breaks; split them by words."""
    if approx_tokens(sentence) <= max_tokens:
        return [sentence]
    words, out, cur = sentence.split(), [], []
    for w in words:
        if cur and approx_tokens(" ".join(cur + [w])) > max_tokens:
            out.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(" ".join(cur))
    return out


def chunk_paper(paper_id: str, lines: list[Line], piece_tokens: int, overlap_tokens: int,
                include_references: bool = False) -> list[Chunk]:
    vocab = {w.lower() for l in lines for w in _WORD.findall(l.text)}
    sections: list[list[Line]] = []
    for line in lines:
        if line.in_references and not include_references:
            continue
        if not sections or sections[-1][0].section != line.section:
            sections.append([])
        sections[-1].append(line)

    chunks: list[Chunk] = []
    for sec_lines in sections:
        text, offsets, pages = _join_lines(sec_lines, vocab)
        units = []
        for s, p0, p1 in _sentences(text, offsets, pages):
            units += [(part, p0, p1) for part in _split_long(s, piece_tokens)]
        cur: list[tuple[str, int, int]] = []
        new_since_emit = 0

        def emit():
            body = " ".join(u[0] for u in cur)
            chunks.append(Chunk(
                chunk_id=f"{paper_id}-{len(chunks):04d}", paper_id=paper_id,
                section=sec_lines[0].section, page_start=min(u[1] for u in cur),
                page_end=max(u[2] for u in cur), text=body, n_tokens=approx_tokens(body)))

        for u in units:
            if cur and approx_tokens(" ".join(x[0] for x in cur + [u])) > piece_tokens:
                emit()
                new_since_emit = 0
                keep: list[tuple[str, int, int]] = []
                for x in reversed(cur):  # carry trailing sentences as overlap
                    if approx_tokens(" ".join(y[0] for y in [x] + keep)) > overlap_tokens:
                        break
                    keep.insert(0, x)
                while keep and approx_tokens(" ".join(y[0] for y in keep + [u])) > piece_tokens:
                    keep.pop(0)
                cur = keep
            cur.append(u)
            new_since_emit += 1
        if cur and new_since_emit:
            emit()
    return chunks
