"""Parse the papers once and write the pieces for every retrieval setting.

    python -m app.ingest.build_index
"""
from __future__ import annotations

import json
from dataclasses import asdict

from app.config import INDEX_DIR, RETRIEVAL_SETTINGS
from app.evaluation.stats import percentile
from app.ingest.chunker import chunk_paper
from app.ingest.parse_pdf import parse_all


def build() -> dict:
    parsed = parse_all()
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    stats = {}
    for name, s in RETRIEVAL_SETTINGS.items():
        chunks = []
        for pid, lines in parsed.items():
            chunks += chunk_paper(pid, lines, s.piece_tokens, s.overlap_tokens)
        (INDEX_DIR / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(asdict(c)) for c in chunks), encoding="utf-8")
        sizes = [c.n_tokens for c in chunks]
        stats[name] = {
            "pieces": len(chunks),
            "by_paper": {pid: sum(c.paper_id == pid for c in chunks) for pid in parsed},
            "tokens_p50": percentile(sizes, 50), "tokens_p95": percentile(sizes, 95),
            "tokens_max": max(sizes), "tokens_min": min(sizes),
            "prompt_passage_tokens_est": s.pieces * percentile(sizes, 50),
        }
    (INDEX_DIR / "stats.json").write_text(json.dumps(stats, indent=1))
    return stats


if __name__ == "__main__":
    for name, st in build().items():
        print(name, json.dumps(st))
