"""Retrieval-only evaluation (no model calls, zero quota): for each setting, does any retrieved
piece come from an expected location? Also prints the signals the decide-step can use to refuse.

    python -m experiments.retrieval_offline
"""
from __future__ import annotations

import json

from app.config import DATA_DIR, RESULTS_DIR, RETRIEVAL_SETTINGS
from app.pipeline.retrieval import retrieve_sync


def location_hit(question: dict, passages) -> bool:
    for loc in question["locations"]:
        for p in passages:
            c = p.chunk
            if c.paper_id != loc["paper"]:
                continue
            sec = loc.get("section")
            if sec:  # when a section is expected, the page alone is not enough (§6.3 shares p.9 with §6.2)
                if c.section == sec or c.section.split()[0] == sec:
                    return True
            elif c.page_start <= loc["page"] <= c.page_end:
                return True
    return False


def main():
    questions = json.loads((DATA_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
    out = {}
    for name in RETRIEVAL_SETTINGS:
        rows = []
        for q in questions:
            r = retrieve_sync(q["question"], name)
            rows.append({
                "id": q["id"], "kind": q["kind"],
                "hit": location_hit(q, r.passages) if q["locations"] else None,
                "top_rerank": r.top_rerank, "bm25_confidence": r.bm25_confidence,
                "passage_tokens": sum(p.chunk.n_tokens for p in r.passages),
                "retrieval_ms": r.elapsed_ms,
                "top": [f"{p.chunk.paper_id} §{p.chunk.section.split()[0]} p.{p.chunk.page_start}" for p in r.passages],
            })
        answerable = [x for x in rows if x["kind"] == "answerable"]
        hits = sum(x["hit"] for x in answerable)
        print(f"== {name}: location hit {hits}/{len(answerable)}; "
              f"passage tokens p50 {sorted(x['passage_tokens'] for x in rows)[len(rows) // 2]}")
        for x in rows:
            rr = "-" if x["top_rerank"] is None else f"{x['top_rerank']:+.1f}"
            mark = {True: "HIT ", False: "MISS", None: "n/a "}[x["hit"]]
            print(f"   {x['id']:<3} {mark} rerank={rr:>6} bm25conf={x['bm25_confidence']:.2f} "
                  f"tok={x['passage_tokens']:<5} {x['retrieval_ms']:6.0f}ms  {', '.join(x['top'][:3])}")
        out[name] = rows
    (RESULTS_DIR / "retrieval_offline.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
