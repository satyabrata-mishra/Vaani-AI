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

    def search(self, query: str, fetch: int, papers: tuple[str, ...] | None = None
               ) -> tuple[list[tuple[int, float]], float, float, list[str]]:
        scores = self.bm25.scores(query)
        allowed = [i for i in range(len(scores)) if not papers or self.chunks[i].paper_id in papers]
        order = sorted(allowed, key=lambda i: scores[i], reverse=True)[:fetch]
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
                  rerank: bool | None = None, fetch: int | None = None,
                  papers: tuple[str, ...] | None = None) -> RetrievalResult:
    """`papers` with two or more entries searches each paper separately and keeps the best
    pieces of each, so one paper cannot crowd the others out of a comparison question."""
    s = RETRIEVAL_SETTINGS[setting_name]
    pieces = pieces or s.pieces
    rerank = s.rerank if rerank is None else rerank
    fetch = fetch or (s.fetch if rerank else pieces)
    t0 = time.perf_counter()
    idx = get_index(setting_name)
    groups = [(p,) for p in papers] if papers and len(papers) > 1 else [papers]
    per_group = -(-pieces // len(groups))
    selected: list[Passage] = []
    conf = cov = 0.0
    unknown: list[str] = []
    rerank_s = 0.0
    for g in groups:
        hits, c, cv, unknown = idx.search(query, max(fetch // len(groups), per_group), g)
        conf, cov = max(conf, c), max(cov, cv)
        passages = [Passage(idx.chunks[i], score) for i, score in hits]
        if rerank and passages:
            tr = time.perf_counter()
            scores = list(get_reranker().rerank(query, [_index_text(p.chunk) for p in passages]))
            for p, sc in zip(passages, scores):
                p.rerank = float(sc)
            passages.sort(key=lambda p: p.rerank, reverse=True)
            rerank_s += time.perf_counter() - tr
        selected.extend(passages[:per_group])
    if rerank:
        selected.sort(key=lambda p: p.rerank if p.rerank is not None else -1e9, reverse=True)
    else:
        selected.sort(key=lambda p: p.bm25, reverse=True)
    t2 = time.perf_counter()
    return RetrievalResult(
        setting=setting_name, query=query, passages=selected[:max(pieces, len(groups))], bm25_confidence=conf,
        top_coverage=cov, unknown_terms=unknown, reranked=bool(rerank and selected),
        elapsed_ms=(t2 - t0) * 1000,
        timings={"bm25_ms": (t2 - t0 - rerank_s) * 1000, "rerank_ms": rerank_s * 1000})


class RetrievalTool:
    """Async wrapper with a timeout and optional injected faults (for experiments only).

    The re-ranker is CPU-bound (~1.2 s per query here), so at most `rerank_slots` run at once and
    at most `rerank_slots` more wait; anyone beyond that gets BM25 order instead of queueing."""

    def __init__(self, timeout_s: float, delay_s: float = 0.0, failure_rate: float = 0.0,
                 rng: random.Random | None = None, rerank_slots: int = 1):
        self.timeout_s = timeout_s
        self.delay_s = delay_s
        self.failure_rate = failure_rate
        self.rng = rng or random.Random(0)
        self.rerank_slots = rerank_slots
        self._rerank_sem = asyncio.Semaphore(rerank_slots)
        self.rerank_waiting = 0
        self.rerank_skipped = 0

    def rerank_busy(self) -> bool:
        return self.rerank_waiting >= self.rerank_slots * 2

    async def __call__(self, query: str, setting_name: str, pieces: int | None = None,
                       rerank: bool | None = None, timeout_s: float | None = None,
                       fetch: int | None = None, papers: tuple[str, ...] | None = None) -> RetrievalResult:
        want_rerank = RETRIEVAL_SETTINGS[setting_name].rerank if rerank is None else rerank
        if want_rerank and self.rerank_busy():
            self.rerank_skipped += 1
            want_rerank = False

        async def run():
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.failure_rate and self.rng.random() < self.failure_rate:
                raise ToolError("injected retrieval failure")
            if not want_rerank:
                return await asyncio.to_thread(retrieve_sync, query, setting_name, pieces, False, fetch, papers)
            self.rerank_waiting += 1
            waiting = True
            try:
                async with self._rerank_sem:
                    self.rerank_waiting -= 1
                    waiting = False
                    return await asyncio.to_thread(retrieve_sync, query, setting_name, pieces, True, fetch, papers)
            finally:
                if waiting:
                    self.rerank_waiting -= 1
        try:
            return await asyncio.wait_for(run(), timeout_s or self.timeout_s)
        except asyncio.TimeoutError:
            raise ToolTimeout(f"retrieval exceeded {timeout_s or self.timeout_s:.1f}s") from None
