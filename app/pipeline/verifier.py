"""Step 5b — check the whole answer against the passages it cites, before anything is shown.

Per sentence: it must cite at least one passage we actually sent; every multi-digit or decimal
number in it must appear in a cited passage; enough of its content words must appear there; and
if the question names a paper, the citation must come from that paper. Rule-based on purpose:
an LLM judge would spend the same scarce tokens. It cannot judge paraphrased meaning."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ingest.bm25 import tokenize
from app.pipeline.prompts import REFUSAL_TOKEN
from app.pipeline.retrieval import Passage

_CITE = re.compile(r"\[P(\d+)\]")
_LEADING_CITES = re.compile(r"^((?:\[P\d+\]\s*)+)[.]?\s*(.*)$", re.S)
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass
class SentenceCheck:
    text: str
    cited: list[int]
    ok: bool
    reason: str = ""


@dataclass
class Verification:
    refused: bool
    sentences: list[SentenceCheck] = field(default_factory=list)

    @property
    def verified(self) -> list[SentenceCheck]:
        return [s for s in self.sentences if s.ok]

    @property
    def dropped(self) -> list[SentenceCheck]:
        return [s for s in self.sentences if not s.ok]

    @property
    def any_ok(self) -> bool:
        return bool(self.verified)

    def problems(self) -> list[str]:
        return [f"{s.reason}: \"{s.text[:120]}\"" for s in self.dropped]


def split_sentences(answer: str) -> list[str]:
    out: list[str] = []
    for piece in _SPLIT.split(answer.strip()):
        piece = piece.strip()
        if not piece:
            continue
        m = _LEADING_CITES.match(piece)
        if out and m:   # "... 64. [P2] Next ..." -> [P2] belongs to the sentence before
            out[-1] = f"{out[-1]} {m.group(1).strip()}"
            piece = m.group(2).strip()
        if piece:
            out.append(piece)
    return out


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text) if len(n) >= 2}


def check_sentence(sentence: str, passages: list[Passage], named_papers: list[str],
                   min_overlap: float) -> SentenceCheck:
    cited = [int(x) for x in _CITE.findall(sentence)]
    claim = _CITE.sub("", sentence)
    if not cited:
        return SentenceCheck(sentence, cited, False, "no citation")
    if any(i < 1 or i > len(passages) for i in cited):
        return SentenceCheck(sentence, cited, False, "cites a passage that was not provided")
    cited_p = [passages[i - 1] for i in cited]
    if named_papers and not any(p.chunk.paper_id in named_papers for p in cited_p):
        return SentenceCheck(sentence, cited, False, "cites a different paper than the question names")
    source = " ".join(f"{p.citation()} {p.chunk.text}" for p in cited_p)
    source_numbers = {n.replace(",", "") for n in _NUMBER.findall(source)}
    missing = _numbers(claim) - source_numbers
    if missing:
        return SentenceCheck(sentence, cited, False, f"number not in cited passage: {sorted(missing)[0]}")
    words = set(tokenize(claim))
    if words:
        overlap = len(words & set(tokenize(source))) / len(words)
        if overlap < min_overlap:
            return SentenceCheck(sentence, cited, False, f"low overlap with cited passage ({overlap:.2f})")
    return SentenceCheck(sentence, cited, True)


def verify(answer: str, passages: list[Passage], named_papers: list[str] | None = None,
           min_overlap: float = 0.4) -> Verification:
    if REFUSAL_TOKEN in answer:
        return Verification(refused=True)
    return Verification(False, [check_sentence(s, passages, named_papers or [], min_overlap)
                                for s in split_sentences(answer)])


def render(v: Verification, passages: list[Passage]) -> tuple[str, list[str]]:
    """Verified sentences with [Pn] renumbered [1], [2]... by first use, plus the source list."""
    order: dict[int, int] = {}
    lines = []
    for s in v.verified:
        def sub(m):
            i = int(m.group(1))
            order.setdefault(i, len(order) + 1)
            return f"[{order[i]}]"
        lines.append(_CITE.sub(sub, s.text))
    text = " ".join(lines)
    if v.dropped:
        text += (f"\n\n({len(v.dropped)} statement(s) were removed because the cited passages did not "
                 f"support them.)")
    sources = [f"[{n}] {passages[i - 1].citation()}" for i, n in sorted(order.items(), key=lambda kv: kv[1])]
    return text, sources
