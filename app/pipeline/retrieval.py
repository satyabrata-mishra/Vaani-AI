"""Step 3 — the retrieval TOOL. BM25 over pre-built pieces, optional cross-encoder re-rank.
It runs in a worker thread behind a timeout, and can be made slow or failing on purpose
(the brief: "tools time out")."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from functools import lru_cache

from app.config import INDEX_DIR, ROOT, RETRIEVAL_SETTINGS, RetrievalSetting
from app.ingest.bm25 import BM25
from app.ingest.chunker import Chunk
from app.ingest.parse_pdf import PAPERS

RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class ToolError(Exception):
    pass


class ToolTimeout(ToolError):
    pass


@dataclass
class Passage:
    chunk: Chunk
    bm25: float
    rerank: float | None = None

    def citation(self) -> str:
        c = self.chunk
        pages = f"p.{c.page_start}" if c.page_start == c.page_end else f"pp.{c.page_start}-{c.page_end}"
        return f"{PAPERS[c.paper_id]['short']} (arXiv {PAPERS[c.paper_id]['arxiv']}), §{c.section}, {pages}"


@dataclass
class RetrievalResult:
    setting: str
    query: str
    passages: list[Passage]
    bm25_confidence: float      # top BM25 score / best possible score for this query (0-1)
    top_coverage: float         # fraction of known query terms found in the top piece
    unknown_terms: list[str]    # query terms that appear nowhere in the library
    reranked: bool
    elapsed_ms: float
    timings: dict = field(default_factory=dict)

    @property
    def top_rerank(self) -> float | None:
        return self.passages[0].rerank if self.passages and self.passages[0].rerank is not None else None


def _index_text(c: Chunk) -> str:
    # Section title and paper short name are indexed with the text, so "Moshi" or a section name
    # in the question can steer retrieval to the right paper.
    return f"{PAPERS[c.paper_id]['short']}. {c.section}. {c.text}"


class Index:
    def __init__(self, setting: RetrievalSetting):
        path = INDEX_DIR / f"{setting.name}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing — run: python -m app.ingest.build_index")
        self.setting = setting
        self.chunks = [Chunk(**json.loads(l)) for l in path.read_text(encoding="utf-8").splitlines()]
        self.bm25 = BM25([_index_text(c) for c in self.chunks])

    def search(self, query: str, fetch: int) -> tuple[list[tuple[int, float]], float, float, list[str]]:
        scores = self.bm25.scores(query)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:fetch]
        top = [(i, scores[i]) for i in order if scores[i] > 0]
        best = self.bm25.max_possible(query)
        conf = (top[0][1] / best) if top and best else 0.0
        cov = self.bm25.coverage(query, top[0][0]) if top else 0.0
        return top, conf, cov, self.bm25.unknown_terms(query)


@lru_cache(maxsize=None)
def get_index(setting_name: str) -> Index:
    return Index(RETRIEVAL_SETTINGS[setting_name])


@lru_cache(maxsize=1)
def get_reranker():
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    return TextCrossEncoder(RERANK_MODEL, cache_dir=str(ROOT / ".cache" / "fastembed"))


def retrieve_sync(query: str, setting_name: str, pieces: int | None = None,
                  rerank: bool | None = None) -> RetrievalResult:
    s = RETRIEVAL_SETTINGS[setting_name]
    pieces = pieces or s.pieces
    rerank = s.rerank if rerank is None else rerank
    t0 = time.perf_counter()
    idx = get_index(setting_name)
    hits, conf, cov, unknown = idx.search(query, s.fetch if rerank else pieces)
    t1 = time.perf_counter()
    passages = [Passage(idx.chunks[i], score) for i, score in hits]
    if rerank and passages:
        scores = list(get_reranker().rerank(query, [_index_text(p.chunk) for p in passages]))
        for p, sc in zip(passages, scores):
            p.rerank = float(sc)
        passages.sort(key=lambda p: p.rerank, reverse=True)
    t2 = time.perf_counter()
    return RetrievalResult(
        setting=setting_name, query=query, passages=passages[:pieces], bm25_confidence=conf,
        top_coverage=cov, unknown_terms=unknown, reranked=bool(rerank and passages),
        elapsed_ms=(t2 - t0) * 1000, timings={"bm25_ms": (t1 - t0) * 1000, "rerank_ms": (t2 - t1) * 1000})


class RetrievalTool:
    """Async wrapper with a timeout and optional injected faults (for experiments only)."""

    def __init__(self, timeout_s: float, delay_s: float = 0.0, failure_rate: float = 0.0,
                 rng: random.Random | None = None):
        self.timeout_s = timeout_s
        self.delay_s = delay_s
        self.failure_rate = failure_rate
        self.rng = rng or random.Random(0)

    async def __call__(self, query: str, setting_name: str, pieces: int | None = None,
                       rerank: bool | None = None, timeout_s: float | None = None) -> RetrievalResult:
        async def run():
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.failure_rate and self.rng.random() < self.failure_rate:
                raise ToolError("injected retrieval failure")
            return await asyncio.to_thread(retrieve_sync, query, setting_name, pieces, rerank)
        try:
            return await asyncio.wait_for(run(), timeout_s or self.timeout_s)
        except asyncio.TimeoutError:
            raise ToolTimeout(f"retrieval exceeded {timeout_s or self.timeout_s:.1f}s") from None
