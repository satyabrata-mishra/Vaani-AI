"""Okapi BM25, in-repo so every scoring detail is visible and explainable."""
from __future__ import annotations

import math
import re
from collections import Counter

from py_rust_stemmers import SnowballStemmer

_TOKEN = re.compile(r"[a-z0-9]+")
_STEM = SnowballStemmer("english")
STOPWORDS = frozenset("""a an and are as at be by can do does for from has have how in is it its
of on or that the their this to was were what when where which who why with we our you your
they them there these those than then also into about not no but if so such use used using""".split())


def tokenize(text: str) -> list[str]:
    return [_STEM.stem_word(t) for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(d)) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avglen = sum(self.lens) / max(1, len(self.lens))
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query: str) -> list[float]:
        q = [t for t in dict.fromkeys(tokenize(query)) if t in self.idf]
        out = []
        for tf, ln in zip(self.tfs, self.lens):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * ln / self.avglen)
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + norm)
            out.append(s)
        return out

    def max_possible(self, query: str) -> float:
        """Upper bound on any document's score for this query (each term at saturation).
        Used to turn a raw BM25 score into a 0-1 confidence that is comparable across queries."""
        return sum(self.idf[t] * (self.k1 + 1) for t in dict.fromkeys(tokenize(query)) if t in self.idf)

    def coverage(self, query: str, doc_index: int) -> float:
        """Fraction of the query's known terms that appear in the document."""
        q = [t for t in dict.fromkeys(tokenize(query)) if t in self.idf]
        if not q:
            return 0.0
        return sum(1 for t in q if t in self.tfs[doc_index]) / len(q)

    def unknown_terms(self, query: str) -> list[str]:
        return [t for t in dict.fromkeys(tokenize(query)) if t not in self.idf]
