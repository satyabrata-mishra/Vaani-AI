"""Step 2 — understand the question, deterministically (no model call): validate it and detect
which papers it names, so retrieval can search each named paper and verification can check that
citations come from the paper that was asked about."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_CHARS, MAX_CHARS = 3, 1000

STRONG_ALIASES = {
    "attention": [r"attention is all you need", r"\bvaswani", r"\b(original|base|big|vanilla) transformer",
                  r"transformer paper"],
    "moshi": [r"\bmoshi\b", r"\bmimi\b", r"\bhelium\b", r"inner monologue"],
    "hnet": [r"\bh-?net\b", r"dynamic chunking"],
}
# "Transformer" alone is ambiguous (Moshi and H-Net both use and compare against Transformers);
# it only points at the Attention paper when no other paper is named.
WEAK_ATTENTION = r"\btransformer"


@dataclass
class Analysis:
    question: str
    papers: list[str] = field(default_factory=list)

    @property
    def multi_paper(self) -> bool:
        return len(self.papers) > 1


def analyze(question: str) -> Analysis:
    q = " ".join((question or "").split())
    if len(q) < MIN_CHARS or len(q) > MAX_CHARS:
        raise ValueError(f"question must be {MIN_CHARS}-{MAX_CHARS} characters")
    low = q.lower()
    papers = [pid for pid, pats in STRONG_ALIASES.items() if any(re.search(p, low) for p in pats)]
    if not papers and re.search(WEAK_ATTENTION, low):
        papers = ["attention"]
    return Analysis(q, papers)
